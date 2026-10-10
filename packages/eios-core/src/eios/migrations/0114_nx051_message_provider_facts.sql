-- NX-051 (temporary number 0120): provider order and reply links of Messages (AT-014), as runtime evidence.
--
-- Dispatcher rulings (2026-10-10): provider facts and reply links live in runtime tables and are read by derivation;
-- the formal Message type stays v1 (no Schema publication); the native WebChat client's sequence and time are
-- client-level evidence only (never ordering, never a time anchor); signed channels are an extension point only.
--
-- * received order is the existing Message `sequence` (assigned at commit, never rewritten); `accepted_at` likewise.
-- * runtime.nexloop_message_provider_facts: at most one row per Message, append-only: namespace, provider ref,
--   provider sequence, provider time, trust (server | signed | client) and a skew flag (time outside the namespace's
--   window: kept as stated, flagged, never rejected).
-- * runtime.nexloop_message_reply_links: append-only events (raw reference kept; resolved / pending /
--   foreign_conversation / unknown); a late target appends a 'resolved' event, nothing is rewritten. A reference to
--   another conversation never links and never reveals the other side.
-- * Defaults are written at COMMIT by a deferred constraint trigger (same transaction, "persist before ACK"): native
--   WebChat messages get 'native.webchat' (client) with their provider_event_id, other inbound messages
--   'nexloop.api' (server), Agent replies 'nexloop.agent' (server) with the channel receipt's provider reference and
--   a server-derived reply link equal to their trigger_message_id (the ADR-023 binding itself is untouched).
-- * The native v2 client fields are recorded by authz.nexloop_native_client_facts in the same transaction as the
--   message commit (0055 and 0046 bodies unchanged).
-- * Reads: authz.nexloop_message_provider_read (the caller's own current Message READ proofs, the existing derivation)
--   and authz.nexloop_conversation_messages_projection (the owner's conversation read, plus provider/reply fields).

-- 1. Namespace registry (global, versioned by trusted configuration) -----------------------------------------------
create table control.nexloop_provider_namespaces (
 namespace text primary key check(namespace~'^[a-z][a-z0-9.-]{1,63}$'),trust text not null check(trust in ('server','signed','client')),
 skew_seconds integer not null check(skew_seconds between 0 and 86400),sequence_resets boolean not null default false,
 published_by text not null,published_at timestamptz not null default clock_timestamp()
);
alter table control.nexloop_provider_namespaces owner to nexloop_owner;
create trigger nx051_append_only before update or delete on control.nexloop_provider_namespaces for each row execute function control.nexloop_nx022_append_only();
revoke all on control.nexloop_provider_namespaces from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator;
insert into control.nexloop_provider_namespaces(namespace,trust,skew_seconds,published_by) values
 ('native.webchat','client',300,'migration 0120'),('nexloop.api','server',0,'migration 0120'),('nexloop.agent','server',0,'migration 0120');

-- 2. Facts and reply-link events ------------------------------------------------------------------------------
create table runtime.nexloop_message_provider_facts (
 tenant_id text not null,world text not null,message_id text not null check(message_id~'^[a-f0-9]{64}$'),conversation_id text not null,
 direction text not null check(direction in ('inbound','outbound')),
 provider_namespace text not null references control.nexloop_provider_namespaces(namespace),
 provider_message_ref text check(provider_message_ref is null or length(provider_message_ref) between 1 and 256),
 provider_sequence bigint check(provider_sequence is null or provider_sequence between 1 and 9007199254740991),
 provider_sent_at timestamptz,trust text not null check(trust in ('server','signed','client')),skewed boolean not null default false,
 recorded_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,message_id)
);
create index nexloop_message_provider_ref on runtime.nexloop_message_provider_facts(tenant_id,world,conversation_id,provider_message_ref) where provider_message_ref is not null;
create table runtime.nexloop_message_reply_links (
 event_id bigint generated always as identity primary key,tenant_id text not null,world text not null,
 message_id text not null,conversation_id text not null,raw_kind text not null check(raw_kind in ('message','provider_ref')),
 raw_ref text not null check(length(raw_ref) between 1 and 256),reply_to_message_id text check(reply_to_message_id is null or reply_to_message_id~'^[a-f0-9]{64}$'),
 resolution text not null check(resolution in ('resolved','pending','foreign_conversation','unknown')),
 source text not null check(source in ('provider','server')),recorded_at timestamptz not null default clock_timestamp(),
 check((resolution='resolved')=(reply_to_message_id is not null))
);
create index nexloop_message_reply_latest on runtime.nexloop_message_reply_links(tenant_id,world,message_id,event_id desc);
create index nexloop_message_reply_pending on runtime.nexloop_message_reply_links(tenant_id,world,conversation_id,raw_ref) where resolution='pending';
create function runtime.nexloop_nx051_append_only() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin raise exception '% is append-only',tg_table_name using errcode='42501';end $$;
alter function runtime.nexloop_nx051_append_only() owner to nexloop_owner;
revoke all on function runtime.nexloop_nx051_append_only() from public;
do $tables$
declare t text;
begin
 foreach t in array array['runtime.nexloop_message_provider_facts','runtime.nexloop_message_reply_links'] loop
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy tenant_boundary on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('create trigger nx051_append_only before update or delete on %s for each row execute function runtime.nexloop_nx051_append_only()',t);
  execute format('revoke all on %s from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator',t);
 end loop;
end $tables$;

-- 3. Internal writers (owner-only; the extension point a signed channel adapter calls in its commit transaction) ----
create function runtime.nexloop_record_reply_link(p_tenant text,p_world text,p_message text,p_kind text,p_ref text,p_source text) returns text
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);m runtime.nexloop_conversation_messages;v_target text;v_resolution text;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into m from runtime.nexloop_conversation_messages where tenant_id=p_tenant and world=p_world and message_id=p_message;
 if not found or p_kind not in ('message','provider_ref') or p_ref is null or length(p_ref) not between 1 and 256 then
  raise exception 'reply link invalid' using errcode='22023';end if;
 if p_kind='message' then
  select message_id into v_target from runtime.nexloop_conversation_messages where tenant_id=p_tenant and world=p_world and message_id=p_ref and conversation_id=m.conversation_id;
  if not found and exists(select 1 from runtime.nexloop_conversation_messages where tenant_id=p_tenant and world=p_world and message_id=p_ref) then v_resolution:='foreign_conversation';end if;
 else
  select message_id into v_target from runtime.nexloop_message_provider_facts where tenant_id=p_tenant and world=p_world and conversation_id=m.conversation_id and provider_message_ref=p_ref
   order by recorded_at limit 1;
  if not found and exists(select 1 from runtime.nexloop_message_provider_facts where tenant_id=p_tenant and world=p_world and provider_message_ref=p_ref) then v_resolution:='foreign_conversation';end if;
 end if;
 if v_target=p_message then v_target:=null;v_resolution:='unknown';end if;  -- a message never replies to itself
 v_resolution:=coalesce(v_resolution,case when v_target is not null then 'resolved' else 'pending' end);
 insert into runtime.nexloop_message_reply_links(tenant_id,world,message_id,conversation_id,raw_kind,raw_ref,reply_to_message_id,resolution,source)
  values(p_tenant,p_world,p_message,m.conversation_id,p_kind,p_ref,case when v_resolution='resolved' then v_target end,v_resolution,p_source);
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v_resolution;
end $$;

-- A newly known Message (or provider ref) resolves earlier pending references to it: one appended event each.
create function runtime.nexloop_resolve_pending_replies(p_tenant text,p_world text,p_conversation text,p_message text,p_ref text) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r record;
begin
 for r in select distinct on (l.message_id) l.* from runtime.nexloop_message_reply_links l
  where l.tenant_id=p_tenant and l.world=p_world and l.conversation_id=p_conversation and l.resolution='pending'
   and ((l.raw_kind='message' and l.raw_ref=p_message) or (l.raw_kind='provider_ref' and p_ref is not null and l.raw_ref=p_ref))
   and not exists(select 1 from runtime.nexloop_message_reply_links later where later.tenant_id=l.tenant_id and later.world=l.world
     and later.message_id=l.message_id and later.event_id>l.event_id)
  order by l.message_id,l.event_id desc loop
  if r.message_id<>p_message then
   insert into runtime.nexloop_message_reply_links(tenant_id,world,message_id,conversation_id,raw_kind,raw_ref,reply_to_message_id,resolution,source)
    values(r.tenant_id,r.world,r.message_id,r.conversation_id,r.raw_kind,r.raw_ref,p_message,'resolved',r.source);
  end if;
 end loop;
end $$;

create function runtime.nexloop_record_provider_facts(p_tenant text,p_world text,p_message text,p_namespace text,p_ref text,p_sequence bigint,p_sent_at timestamptz)
 returns boolean language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);m runtime.nexloop_conversation_messages;ns control.nexloop_provider_namespaces;v_inserted boolean;v_accepted timestamptz;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into m from runtime.nexloop_conversation_messages where tenant_id=p_tenant and world=p_world and message_id=p_message;
 select * into ns from control.nexloop_provider_namespaces where namespace=p_namespace;
 if m.message_id is null or ns.namespace is null then raise exception 'provider facts invalid' using errcode='22023';end if;
 v_accepted:=coalesce((m.record->>'accepted_at')::timestamptz,clock_timestamp());
 insert into runtime.nexloop_message_provider_facts(tenant_id,world,message_id,conversation_id,direction,provider_namespace,provider_message_ref,provider_sequence,provider_sent_at,trust,skewed)
  values(p_tenant,p_world,p_message,m.conversation_id,case when m.record ? 'direction' then m.record->>'direction' else 'inbound' end,p_namespace,p_ref,p_sequence,p_sent_at,ns.trust,
   p_sent_at is not null and abs(extract(epoch from (p_sent_at-v_accepted)))>ns.skew_seconds)
  on conflict do nothing returning true into v_inserted;
 if coalesce(v_inserted,false) then perform runtime.nexloop_resolve_pending_replies(p_tenant,p_world,m.conversation_id,p_message,p_ref);end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return coalesce(v_inserted,false);
end $$;
alter function runtime.nexloop_record_reply_link(text,text,text,text,text,text) owner to nexloop_owner;
alter function runtime.nexloop_resolve_pending_replies(text,text,text,text,text) owner to nexloop_owner;
alter function runtime.nexloop_record_provider_facts(text,text,text,text,text,bigint,timestamptz) owner to nexloop_owner;
revoke all on function runtime.nexloop_record_reply_link(text,text,text,text,text,text),runtime.nexloop_resolve_pending_replies(text,text,text,text,text),
 runtime.nexloop_record_provider_facts(text,text,text,text,text,bigint,timestamptz) from public;

-- 4. Defaults at commit (deferred: sees rows written later in the same transaction, e.g. the 0055 native event) ------
create function runtime.nexloop_message_provider_default() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v_event uuid;v_ref text;v_at timestamptz;
begin
 perform set_config('eios.tenant_id',new.tenant_id,true);
 if new.record->>'direction'='outbound' then
  select ob.provider_reference,ob.observed_at into v_ref,v_at from runtime.nexloop_effect_observations ob
   where ob.tenant_id=new.tenant_id and ob.world=new.world and ob.intent_id=(new.record->>'intent_id')::uuid and ob.provider_reference is not null
   order by ob.observed_at desc limit 1;
  perform runtime.nexloop_record_provider_facts(new.tenant_id,new.world,new.message_id,'nexloop.agent',v_ref,null,v_at);
  -- The reply link of an Agent reply is its server-derived trigger (NX-047 / ADR-023), never a model choice.
  if new.record->>'trigger_message_id' is not null and not exists(select 1 from runtime.nexloop_message_reply_links l where l.tenant_id=new.tenant_id and l.world=new.world and l.message_id=new.message_id) then
   perform runtime.nexloop_record_reply_link(new.tenant_id,new.world,new.message_id,'message',new.record->>'trigger_message_id','server');
  end if;
 else
  select e.provider_event_id into v_event from runtime.nexloop_native_web_events e where e.tenant_id=new.tenant_id and e.world=new.world and e.message_id=new.message_id;
  if found then perform runtime.nexloop_record_provider_facts(new.tenant_id,new.world,new.message_id,'native.webchat',v_event::text,null,null);
  else perform runtime.nexloop_record_provider_facts(new.tenant_id,new.world,new.message_id,'nexloop.api',null,null,null);end if;
 end if;
 perform runtime.nexloop_resolve_pending_replies(new.tenant_id,new.world,new.conversation_id,new.message_id,null);
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return null;
end $$;
alter function runtime.nexloop_message_provider_default() owner to nexloop_owner;
revoke all on function runtime.nexloop_message_provider_default() from public;
create constraint trigger nx051_message_provider_default after insert on runtime.nexloop_conversation_messages
 deferrable initially deferred for each row execute function runtime.nexloop_message_provider_default();

-- 5. Native WebChat v2 client facts, in the message's own commit transaction ------------------------------------
create function authz.nexloop_native_client_facts(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;v_tenant text:=a->>'tenant_id';v_principal text:=a->>'principal_id';
 e runtime.nexloop_native_web_events;v_resolution text;v_sent timestamptz;
begin
 if session_user is distinct from 'nexloop_api' or p_world is distinct from 'real' or octet_length(p_text)>1048576 or octet_length(p_payload)>4096
  or a->>'protocol' is distinct from 'nexloop-native-client-v1' or a->>'action_resource' is distinct from 'eios:action:Message.create:1'
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or (select array_agg(x order by x) from jsonb_object_keys(c) x) is distinct from array['client_sent_at','client_sequence','message_id','provider_event_id','reply_to']
  or coalesce(c->>'message_id','')!~'^[a-f0-9]{64}$' or coalesce(c->>'provider_event_id','')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
  or (jsonb_typeof(c->'client_sequence')<>'null' and (jsonb_typeof(c->'client_sequence')<>'number' or c->>'client_sequence'!~'^[1-9][0-9]{0,15}$'))
  or (jsonb_typeof(c->'client_sent_at')<>'null' and (jsonb_typeof(c->'client_sent_at')<>'string' or c->>'client_sent_at'!~'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$'))
  or (jsonb_typeof(c->'reply_to')<>'null' and (jsonb_typeof(c->'reply_to') is distinct from 'object'
   or (select array_agg(x order by x) from jsonb_object_keys(c->'reply_to') x) is distinct from array['kind','ref']
   or not ((c->'reply_to'->>'kind'='message' and c->'reply_to'->>'ref'~'^[a-f0-9]{64}$')
     or (c->'reply_to'->>'kind'='provider_event' and c->'reply_to'->>'ref'~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')))) then
  raise exception 'native client facts invalid' using errcode='22023';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-native-client-v1:'||p_text,'UTF8'),k,'sha256'),'hex') then
  raise exception 'conversation unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 if not exists(select 1 from control.nexloop_browser_sessions where encode(session_token_digest,'hex')=p_digest) then
  raise exception 'conversation unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 -- Only the sender's own native event, just committed in this transaction, for this very message.
 select * into e from runtime.nexloop_native_web_events where tenant_id=v_tenant and world=p_world and provider_namespace='native.webchat'
  and principal_id=v_principal and provider_event_id=(c->>'provider_event_id')::uuid;
 if not found or e.message_id is distinct from c->>'message_id' then raise exception 'conversation unavailable' using errcode='42501';end if;
 v_sent:=case when jsonb_typeof(c->'client_sent_at')='string' then (c->>'client_sent_at')::timestamptz end;
 if not runtime.nexloop_record_provider_facts(v_tenant,p_world,e.message_id,'native.webchat',e.provider_event_id::text,
   case when jsonb_typeof(c->'client_sequence')='number' then (c->>'client_sequence')::bigint end,v_sent) then
  return jsonb_build_object('recorded',false);  -- replay of an already recorded message: facts are immutable
 end if;
 if jsonb_typeof(c->'reply_to')='object' then
  v_resolution:=runtime.nexloop_record_reply_link(v_tenant,p_world,e.message_id,case when c->'reply_to'->>'kind'='message' then 'message' else 'provider_ref' end,
   c->'reply_to'->>'ref','provider');
 end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 return jsonb_build_object('recorded',true,'reply_resolution',v_resolution);
end $$;
alter function authz.nexloop_native_client_facts(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_native_client_facts(text,text,text,text,text) from public;
grant execute on function authz.nexloop_native_client_facts(text,text,text,text,text) to nexloop_api;

-- 6. Derived reads --------------------------------------------------------------------------------------------
create function runtime.nexloop_message_projection(p_tenant text,p_world text,p_message text) returns jsonb
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select jsonb_build_object(
  'provider',(select jsonb_build_object('namespace',f.provider_namespace,'message_ref',f.provider_message_ref,'sequence',f.provider_sequence,
     'sent_at',f.provider_sent_at,'trust',f.trust,'skewed',f.skewed) from runtime.nexloop_message_provider_facts f
   where f.tenant_id=p_tenant and f.world=p_world and f.message_id=p_message),
  'reply',(select jsonb_build_object('reply_to_message_id',l.reply_to_message_id,'resolution',l.resolution,'source',l.source)
   from runtime.nexloop_message_reply_links l where l.tenant_id=p_tenant and l.world=p_world and l.message_id=p_message order by l.event_id desc limit 1)) $$;
alter function runtime.nexloop_message_projection(text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_message_projection(text,text,text) from public;

-- The caller's own current Message READ proofs (the existing 0077/0086 derivation decides them); nothing else.
create function authz.nexloop_message_provider_read(p_digest text,p_world text,p_proofs jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;v_tenant text;proof jsonb;v_message text;v_result jsonb:='{}'::jsonb;
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker') or jsonb_typeof(p_proofs) is distinct from 'array' or jsonb_array_length(p_proofs) not between 1 and 200 then
  raise exception 'message provider read unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);v_tenant:=ident->'binding'->>'tenant_id';
 for proof in select value from jsonb_array_elements(p_proofs) loop
  if proof->>'tenant_id' is distinct from v_tenant or proof->>'principal_id' is distinct from ident->'binding'->>'subject_principal_id'
   or proof->>'operation' is distinct from 'read' or coalesce(proof->>'target_resource','')!~'^eios:object:Message/[a-f0-9]{64}$'
   or proof->>'resource_id' is distinct from proof->>'target_resource' or proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp() then
   raise exception 'message provider read proof invalid' using errcode='42501';end if;
  perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
  v_message:=substr(proof->>'target_resource',length('eios:object:Message/')+1);
  v_result:=v_result||jsonb_build_object(v_message,runtime.nexloop_message_projection(v_tenant,p_world,v_message));
 end loop;
 return v_result;
end $$;
alter function authz.nexloop_message_provider_read(text,text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_message_provider_read(text,text,jsonb) from public;
grant execute on function authz.nexloop_message_provider_read(text,text,jsonb) to nexloop_api,nexloop_domain_worker;

-- The owner's conversation read (0046, unchanged and called first with the same signed envelope) plus projection fields.
create function authz.nexloop_conversation_messages_projection(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v jsonb;item jsonb;out jsonb:='[]'::jsonb;x jsonb;msg jsonb;v_tenant text:=(p_text::jsonb)->>'tenant_id';
begin
 if (p_payload::jsonb)->>'verb' is distinct from 'messages' then raise exception 'conversation unavailable' using errcode='42501';end if;
 v:=authz.nexloop_conversation_command(p_digest,p_world,p_text,p_signature,p_payload);
 for item in select value from jsonb_array_elements(v->'items') loop
  x:=runtime.nexloop_message_projection(v_tenant,p_world,item->>'id');
  msg:=item||jsonb_build_object('reply_to_message_id',x->'reply'->'reply_to_message_id','provider',x->'provider');
  out:=out||jsonb_build_array(msg);
 end loop;
 return jsonb_set(v,'{items}',out);
end $$;
alter function authz.nexloop_conversation_messages_projection(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_conversation_messages_projection(text,text,text,text,text) from public;
grant execute on function authz.nexloop_conversation_messages_projection(text,text,text,text,text) to nexloop_api;
