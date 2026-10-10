-- NX-029 slice 2 (temporary number 0142): Consumer erasure, Message erasure, retention holds, erasing read refusal,
-- Run file purge queue and the owner-internal cross-principal Artifact cleanup port.
-- docs/implementation/NX-029-design.md §4, §4.2a, §4.4, §5, §7 (owner D1–D7, dispatcher D8/D9 and the Artifact ruling).
--
-- 1. control.nexloop_consumer_erasures: one request per Consumer (state machine requested → blocking → settling →
--    waiting_unknown_effects → purging → completed / completed_with_holds). A Consumer is "erasing" from blocking on.
--    D1: a request that came from the Consumer stays 'pending_confirmation' (not erasing) until the owner confirms.
-- 2. Erasing refusal at two layers (§4.2a): the public entry authz.nexloop_assert_read_authority and the inner
--    authz.nexloop_assert_read_authority_before_message_read_v0072, which five derivations call directly (0077, 0080,
--    0084, 0086, 0127). Both are renamed and kept; same-name wrappers refuse when the read concerns an erasing Consumer
--    (the Consumer itself, its properties, its Conversations or Messages) and otherwise call the kept function.
-- 3. Blocking in the request's own transaction: an active contact restriction (rule consumer_erasure; a release is
--    refused while erasing), the Consumer's active plans closed. Settling waits for unknown external effects (never
--    retried blindly). Purging runs bounded phases through the retention keeper; every phase is idempotent.
-- 4. Human Actions on the governed entry registry (0124): Consumer.erase, Message.erase, Retention.hold,
--    Retention.release_hold (human only).
-- 5. Run files (D8): runtime.nexloop_run_purge_items, executed by the keeper through the Agent Host's loopback
--    runs/purge; a failed purge stays pending. Artifacts (dispatcher ruling): one owner-internal port the keeper alone
--    may call (nexloop.erasure.execute, trusted configuration); every id is re-verified in SQL (expired, or of an erasing
--    Consumer), a tombstone and an audit event are written only after the file is gone.
-- Published migrations are not edited.

-- 0. Erasure values may also be the NX-027 pseudonym form (D6: one random pseudonym, no mapping kept).
create or replace function runtime.nexloop_erasure_permits(p_user text,p_table text,p_op text,p_old jsonb,p_new jsonb) returns boolean
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp as $$
declare s control.nexloop_erasure_scope;k text;
begin
 if p_user is distinct from 'nexloop_owner' then return false;end if;
 select * into s from control.nexloop_erasure_scope where txid=txid_current() and table_name=p_table;
 if not found then return false;end if;
 if p_op='DELETE' then return s.allow_delete;end if;
 if p_op<>'UPDATE' or p_new is null then return false;end if;
 for k in select key from jsonb_each(p_new) loop
  if p_old->k is distinct from p_new->k then
   if not (k=any(s.columns)) or not (p_new->k='null'::jsonb or p_new->k='""'::jsonb
     or (jsonb_typeof(p_new->k)='object' and p_new->k->>'erased'='true')
     or (jsonb_typeof(p_new->k)='string' and (p_new->>k)~'^pseudonym:[0-9a-f]{32}$')) then return false;end if;
  end if;
 end loop;
 return true;
end $$;

-- 1. Requests, holds, queues ---------------------------------------------------------------------------------------------
create table control.nexloop_consumer_erasures (
 tenant_id text not null,world text not null,request_id text not null check(request_id~'^[A-Za-z0-9._:-]{1,190}$'),
 consumer_id text not null check(consumer_id~'^[a-f0-9]{64}$'),
 source text not null check(source in ('owner','consumer_request')),requested_by text not null,reason text not null check(char_length(reason) between 1 and 500),
 state text not null check(state in ('pending_confirmation','blocking','settling','waiting_unknown_effects','purging','completed','completed_with_holds')),
 phase text,counts jsonb not null default '{}'::jsonb,watermark bigint not null,
 created_at timestamptz not null default clock_timestamp(),updated_at timestamptz not null default clock_timestamp(),completed_at timestamptz,
 primary key(tenant_id,world,request_id)
);
create unique index nexloop_consumer_erasures_one on control.nexloop_consumer_erasures(tenant_id,world,consumer_id);
create sequence control.nexloop_deletion_watermark;
create table control.nexloop_retention_holds (
 tenant_id text not null,world text not null,hold_id text not null check(hold_id~'^[A-Za-z0-9._:-]{1,190}$'),
 scope_kind text not null check(scope_kind in ('consumer','item_class')),scope_ref text not null check(char_length(scope_ref) between 1 and 200),
 basis text not null check(char_length(basis) between 1 and 1000),held_by text not null,valid_until timestamptz,
 created_at timestamptz not null default clock_timestamp(),released_at timestamptz,released_by text,release_reason text,
 primary key(tenant_id,world,hold_id),check((released_at is null)=(released_by is null))
);
create table runtime.nexloop_run_purge_items (
 tenant_id text not null,world text not null,run_id uuid not null,request_ref text not null,reason text not null check(reason in ('consumer_erasure','retention_expiry')),
 state text not null default 'pending' check(state in ('pending','purged')),attempts integer not null default 0,last_code text,
 created_at timestamptz not null default clock_timestamp(),purged_at timestamptz,primary key(tenant_id,world,run_id)
);
create table runtime.nexloop_erasure_artifacts (
 tenant_id text not null,world text not null,artifact_id text not null check(artifact_id~'^[a-f0-9]{32}$'),request_id text not null,
 created_at timestamptz not null default clock_timestamp(),primary key(tenant_id,world,artifact_id)
);
create table control.nexloop_erasure_salts (tenant_id text primary key,salt bytea not null);
create table control.nexloop_erased_contact_keys (
 tenant_id text not null,world text not null,key_digest text not null check(key_digest~'^[0-9a-f]{64}$'),request_id text not null,
 created_at timestamptz not null default clock_timestamp(),revoked_at timestamptz,revoked_by text,primary key(tenant_id,world,key_digest)
);
do $tables$
declare t text;
begin
 foreach t in array array['control.nexloop_consumer_erasures','control.nexloop_retention_holds','runtime.nexloop_run_purge_items',
   'runtime.nexloop_erasure_artifacts','control.nexloop_erased_contact_keys'] loop
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy tenant_boundary on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on %s from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator',t);
 end loop;
end $tables$;
alter table control.nexloop_erasure_salts owner to nexloop_owner;
revoke all on control.nexloop_erasure_salts from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;
alter sequence control.nexloop_deletion_watermark owner to nexloop_owner;
revoke all on sequence control.nexloop_deletion_watermark from public;
create trigger nx029_no_delete before delete on control.nexloop_consumer_erasures for each row execute function control.nexloop_nx022_append_only();
create trigger nx029_no_delete before delete on control.nexloop_retention_holds for each row execute function control.nexloop_nx022_append_only();

-- 2. Erasing and the two read layers ---------------------------------------------------------------------------------
-- Helpers read tenant-bound (FORCE RLS) tables: each sets the tenant for its own query and restores the caller's.
create function runtime.nexloop_consumer_erasing(p_tenant text,p_world text,p_consumer text) returns boolean
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v boolean;
begin
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 v:=exists(select 1 from control.nexloop_consumer_erasures e where e.tenant_id=p_tenant and e.world=p_world and e.consumer_id=p_consumer
  and e.state<>'pending_confirmation');
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
-- The Consumer a read proof concerns: the Consumer itself or one of its properties, a Conversation or Message of it.
create function runtime.nexloop_read_consumer(p_world text,p_claims jsonb) returns text
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r text:=coalesce(p_claims->>'resource_id',p_claims->>'target_resource','');t text:=p_claims->>'tenant_id';v text;c text;
 prior text:=current_setting('eios.tenant_id',true);
begin
 v:=substring(r from '^eios:(?:object|property):Consumer/([a-f0-9]{64})');
 if v is not null then return v;end if;
 if p_claims->>'type_name'='Consumer' then return p_claims->>'object_id';end if;
 perform set_config('eios.tenant_id',coalesce(t,''),true);
 v:=substring(r from '^eios:(?:object|property):Conversation/([a-f0-9]{64})');
 if v is not null then
  select consumer_id into c from runtime.nexloop_conversations where tenant_id=t and world=p_world and conversation_id=v;
 else
  v:=substring(r from '^eios:(?:object|property):Message/([a-f0-9]{64})');
  if v is not null then
   select cv.consumer_id into c from runtime.nexloop_conversation_messages m join runtime.nexloop_conversations cv
    on cv.tenant_id=m.tenant_id and cv.world=m.world and cv.conversation_id=m.conversation_id where m.tenant_id=t and m.world=p_world and m.message_id=v limit 1;
  end if;
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return c;
end $$;
create function runtime.nexloop_assert_not_erasing(p_world text,p_claims jsonb) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
declare v text:=runtime.nexloop_read_consumer(p_world,p_claims);
begin
 if v is not null and runtime.nexloop_consumer_erasing(p_claims->>'tenant_id',p_world,v) then
  raise exception 'consumer is being erased' using errcode='42501';end if;
end $$;
do $owners$
declare f text;
begin
 foreach f in array array['runtime.nexloop_consumer_erasing(text,text,text)','runtime.nexloop_read_consumer(text,jsonb)','runtime.nexloop_assert_not_erasing(text,jsonb)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';execute 'revoke all on function '||f||' from public';
 end loop;
end $owners$;

-- Layer 1: the public entry.
alter function authz.nexloop_assert_read_authority(text,text,jsonb) rename to nexloop_assert_read_authority_before_erasure_v0142;
revoke all on function authz.nexloop_assert_read_authority_before_erasure_v0142(text,text,jsonb) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator;
create function authz.nexloop_assert_read_authority(p_digest text,p_world text,p_claims jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 perform runtime.nexloop_assert_not_erasing(p_world,p_claims);
 return authz.nexloop_assert_read_authority_before_erasure_v0142(p_digest,p_world,p_claims);
end $$;
alter function authz.nexloop_assert_read_authority(text,text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_assert_read_authority(text,text,jsonb) from public;
-- Layer 2: the inner function the five derivations call directly (they resolve it by name at execution).
alter function authz.nexloop_assert_read_authority_before_message_read_v0072(text,text,jsonb) rename to nexloop_assert_read_authority_inner_before_erasure_v0142;
revoke all on function authz.nexloop_assert_read_authority_inner_before_erasure_v0142(text,text,jsonb) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator;
create function authz.nexloop_assert_read_authority_before_message_read_v0072(p_digest text,p_world text,p_claims jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 perform runtime.nexloop_assert_not_erasing(p_world,p_claims);
 return authz.nexloop_assert_read_authority_inner_before_erasure_v0142(p_digest,p_world,p_claims);
end $$;
alter function authz.nexloop_assert_read_authority_before_message_read_v0072(text,text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_assert_read_authority_before_message_read_v0072(text,text,jsonb) from public;

-- The 0084 call site hands v0072 a type-level READ (eios:object_type:Consumer), which names no Consumer: its own claims
-- (the property of one Consumer) are checked on the function itself (renamed and kept, read and edit alike).
alter function authz.nexloop_assert_derived_property_access(text,text,jsonb,text) rename to nexloop_assert_derived_property_access_before_erasure_v0142;
revoke all on function authz.nexloop_assert_derived_property_access_before_erasure_v0142(text,text,jsonb,text) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator;
create function authz.nexloop_assert_derived_property_access(p_digest text,p_world text,p_claims jsonb,p_operation text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 perform runtime.nexloop_assert_not_erasing(p_world,p_claims);
 return authz.nexloop_assert_derived_property_access_before_erasure_v0142(p_digest,p_world,p_claims,p_operation);
end $$;
alter function authz.nexloop_assert_derived_property_access(text,text,jsonb,text) owner to nexloop_owner;
revoke all on function authz.nexloop_assert_derived_property_access(text,text,jsonb,text) from public;
-- Edits of an erasing Consumer are refused as well (the public edit entry).
alter function authz.nexloop_assert_edit_authority(text,text,jsonb) rename to nexloop_assert_edit_authority_before_erasure_v0142;
revoke all on function authz.nexloop_assert_edit_authority_before_erasure_v0142(text,text,jsonb) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator;
create function authz.nexloop_assert_edit_authority(p_digest text,p_world text,p_claims jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 perform runtime.nexloop_assert_not_erasing(p_world,p_claims);
 return authz.nexloop_assert_edit_authority_before_erasure_v0142(p_digest,p_world,p_claims);
end $$;
alter function authz.nexloop_assert_edit_authority(text,text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_assert_edit_authority(text,text,jsonb) from public;

-- 3. A contact restriction of an erasing Consumer cannot be released (0124 adapter body replaced in place).
create or replace function control.nexloop_governed_contact_release(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if runtime.nexloop_consumer_erasing(p_tenant,p_world,body->>'consumer_id') then
  raise exception 'contact restriction of an erasing consumer stays' using errcode='42501';end if;
 return control.nexloop_contact_release(p_tenant,p_world,p_principal,p_intent,body);
end $$;

-- A Commitment of an erased Consumer keeps its state until its own expiry (D4); made_to becomes the deletion request
-- reference and the condition text is cleared. Only inside a registered erasure (0111 guard body, erasure branch added).
create or replace function ontology.nexloop_commitment_guard() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);r runtime.nexloop_commitments;patch jsonb;
begin
 if tg_op='DELETE' then
  if old.type_name='Commitment' then raise exception 'a Commitment is never deleted' using errcode='42501';end if;
  return old;
 end if;
 if new.type_name<>'Commitment' and (tg_op='INSERT' or old.type_name<>'Commitment') then return new;end if;
 if tg_op='UPDATE' and (old.type_name<>'Commitment' or new.type_name<>'Commitment' or new.object_id<>old.object_id
   or new.tenant_id<>old.tenant_id or new.world<>old.world) then raise exception 'commitment identity is immutable' using errcode='42501';end if;
 if tg_op='UPDATE' and exists(select 1 from control.nexloop_erasure_scope s where s.txid=txid_current() and s.table_name='ontology.objects' and 'commitment_erasure'=any(s.columns))
  and (new.properties-'made_to'-'condition')=(old.properties-'made_to'-'condition') and new.properties->>'made_to'~'^erasure:[A-Za-z0-9._:-]{1,190}$'
  and coalesce(new.properties->>'condition','')='' then
  return new;
 end if;
 perform set_config('eios.tenant_id',new.tenant_id,true);
 if tg_op='INSERT' then
  select * into r from runtime.nexloop_commitments c where c.tenant_id=new.tenant_id and c.world=new.world and c.commitment_id=new.object_id;
  if not found or r.intent_id is distinct from new.source_ref or r.properties is distinct from new.properties or r.registered_at is not null then
   raise exception 'commitment creation was not prepared from its evidence' using errcode='42501';end if;
 elsif new.properties is distinct from old.properties then
  patch:=runtime.nexloop_commitment_target(new.tenant_id,new.world,new.object_id,old.properties,clock_timestamp());
  if patch='{}'::jsonb or new.properties is distinct from old.properties||patch then
   raise exception 'commitment transition is not justified by its evidence' using errcode='23514';end if;
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return new;
end $$;

-- 4. Message erasure (Message.erase and the Consumer purge) ----------------------------------------------------------
-- Text gone everywhere it is kept: stream record and relay outbox (erased), Claim quotes, the Message object (deleted,
-- so NX-026 records source_deleted and NX-021 removes it from recall), prompt text of Runs whose Context cited it.
create function runtime.nexloop_erase_message(p_tenant text,p_world text,p_message text,p_ref text,p_reason text,p_by text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare n_stream integer;n_outbox integer;n_claims integer;n_object integer;n_prompts integer;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 update runtime.nexloop_conversation_messages set record=record||'{"body":"","erased":true}'::jsonb
  where tenant_id=p_tenant and world=p_world and message_id=p_message and record->>'body'<>'';
 get diagnostics n_stream=row_count;
 update runtime.nexloop_message_outbox set record=record||'{"body":"","erased":true}'::jsonb
  where tenant_id=p_tenant and world=p_world and message_id=p_message and record->>'body'<>'';
 get diagnostics n_outbox=row_count;
 update ontology.nexloop_claims set quote='' where tenant_id=p_tenant and world=p_world and source_message_id=p_message and quote<>'';
 get diagnostics n_claims=row_count;
 perform runtime.nexloop_erasure_open('runtime.nexloop_model_request_prompts',array['request_text'],false,p_ref);
 update runtime.nexloop_model_request_prompts p set request_text='' from runtime.nexloop_context_packs k
  where p.tenant_id=p_tenant and p.world=p_world and k.tenant_id=p.tenant_id and k.world=p.world and k.run_id=p.run_id and p.request_text<>''
   and exists(select 1 from runtime.nexloop_context_sources s where s.tenant_id=k.tenant_id and s.world=k.world and s.context_id=k.context_id
    and s.ref like '%'||p_message||'%');
 get diagnostics n_prompts=row_count;
 delete from ontology.events where tenant_id=p_tenant and object_type='Message' and object_id=p_message;
 delete from ontology.relations where tenant_id=p_tenant and ((source_type='Message' and source_object_id=p_message) or (target_type='Message' and target_object_id=p_message));
 delete from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='Message' and object_id=p_message;
 get diagnostics n_object=row_count;
 perform runtime.nexloop_erasure_close();
 perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,p_reason,p_ref,'message','message',p_message,'redacted',
  jsonb_build_object('stream',n_stream,'outbox',n_outbox,'claims',n_claims,'prompts',n_prompts,'object',n_object),p_by);
 return jsonb_build_object('message_id',p_message,'stream',n_stream,'outbox',n_outbox,'claims',n_claims,'prompts',n_prompts,'object',n_object);
end $$;
alter function runtime.nexloop_erase_message(text,text,text,text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_erase_message(text,text,text,text,text,text) from public;

-- 5. Governed handlers (registry signature) ----------------------------------------------------------------------------
create function control.nexloop_governed_erase_consumer(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare v_consumer text:=body->>'consumer_id';cur control.nexloop_consumer_erasures;rev bigint;p record;n_plans integer:=0;
begin
 if exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','consumer_id','reason','confirm_request'))
  or coalesce(v_consumer,'')!~'^[a-f0-9]{64}$' or length(coalesce(body->>'reason','')) not between 1 and 500 then
  raise exception 'consumer erasure invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 if not exists(select 1 from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='Consumer' and object_id=v_consumer) then
  raise exception 'consumer unavailable' using errcode='42501';end if;
 select * into cur from control.nexloop_consumer_erasures where tenant_id=p_tenant and world=p_world and consumer_id=v_consumer for update;
 if found and cur.state<>'pending_confirmation' then
  return jsonb_build_object('request_id',cur.request_id,'state',cur.state,'replayed',true);end if;
 if found then
  -- D1: the owner confirms a Consumer's own request; the request keeps its id.
  update control.nexloop_consumer_erasures set state='blocking',requested_by=p_principal,updated_at=clock_timestamp() where tenant_id=p_tenant and world=p_world and request_id=cur.request_id;
 else
  insert into control.nexloop_consumer_erasures(tenant_id,world,request_id,consumer_id,source,requested_by,reason,state,watermark)
   values(p_tenant,p_world,p_intent,v_consumer,'owner',p_principal,body->>'reason','blocking',nextval('control.nexloop_deletion_watermark')) returning * into cur;
 end if;
 -- Blocking, same transaction: no new contact, no new planning.
 rev:=control.nexloop_nx022_bump(p_tenant,p_world,'contact_restricted','consumer',v_consumer,jsonb_build_object('rule_id','consumer_erasure'),p_principal,'erasure:'||cur.request_id);
 insert into control.nexloop_contact_restrictions(tenant_id,world,consumer_id,active,control_revision,message_id,conversation_id,rule_version,rule_id,certainty,matched_text,restricted_at)
  values(p_tenant,p_world,v_consumer,true,rev,'erasure:'||cur.request_id,'',0,'consumer_erasure','refuse','',clock_timestamp())
  on conflict(tenant_id,world,consumer_id) do update set active=true,control_revision=excluded.control_revision,message_id=excluded.message_id,
   conversation_id=excluded.conversation_id,rule_version=excluded.rule_version,rule_id=excluded.rule_id,certainty=excluded.certainty,
   matched_text=excluded.matched_text,restricted_at=excluded.restricted_at,released_at=null,released_by=null,release_intent=null,release_reason=null;
 for p in select a.plan_id,a.version from runtime.nexloop_active_plans(p_tenant,p_world) a where a.consumer_id=v_consumer loop
  insert into runtime.nexloop_plan_events(tenant_id,world,plan_id,version,status,reason) values(p_tenant,p_world,p.plan_id,p.version,'closed','consumer_erasure');
  n_plans:=n_plans+1;
 end loop;
 delete from runtime.nexloop_work_feed where tenant_id=p_tenant and world=p_world and feed='plan-reevaluate'
  and item_key in (select 'plan:'||plan_id from runtime.nexloop_plans where tenant_id=p_tenant and world=p_world and consumer_id=v_consumer);
 update control.nexloop_consumer_erasures set state='settling',updated_at=clock_timestamp(),counts=counts||jsonb_build_object('plans_closed',n_plans)
  where tenant_id=p_tenant and world=p_world and request_id=cur.request_id;
 return jsonb_build_object('request_id',cur.request_id,'state','settling','watermark',cur.watermark,'plans_closed',n_plans,'replayed',false);
end $$;

create function control.nexloop_governed_erase_message(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','message_id','reason'))
  or coalesce(body->>'message_id','')!~'^[a-f0-9]{64}$' or length(coalesce(body->>'reason','')) not between 1 and 500 then
  raise exception 'message erasure invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 if not exists(select 1 from runtime.nexloop_conversation_messages where tenant_id=p_tenant and world=p_world and message_id=body->>'message_id') then
  raise exception 'message unavailable' using errcode='42501';end if;
 if exists(select 1 from control.nexloop_retention_holds h join runtime.nexloop_conversation_messages m on m.tenant_id=h.tenant_id and m.world=h.world
   join runtime.nexloop_conversations c on c.tenant_id=m.tenant_id and c.world=m.world and c.conversation_id=m.conversation_id
   where h.tenant_id=p_tenant and h.world=p_world and h.released_at is null and (h.valid_until is null or h.valid_until>clock_timestamp())
    and h.scope_kind='consumer' and h.scope_ref=c.consumer_id and m.message_id=body->>'message_id') then
  raise exception 'message is under a retention hold' using errcode='42501';end if;
 return runtime.nexloop_erase_message(p_tenant,p_world,body->>'message_id','message-erasure:'||p_intent,'message_erasure','human:'||p_principal);
end $$;

create function control.nexloop_governed_retention_hold(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare n integer;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 if body->>'operation'='hold_retention' then
  if exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','scope_kind','scope_ref','basis','valid_until'))
   or body->>'scope_kind' not in ('consumer','item_class')
   or (body->>'scope_kind'='consumer' and coalesce(body->>'scope_ref','')!~'^[a-f0-9]{64}$')
   or (body->>'scope_kind'='item_class' and not (body->>'scope_ref'=any(runtime.nexloop_retention_classes())))
   or length(coalesce(body->>'basis','')) not between 1 and 1000
   or (body ? 'valid_until' and (body->>'valid_until')::timestamptz<=clock_timestamp()) then
   raise exception 'retention hold invalid' using errcode='22023';end if;
  insert into control.nexloop_retention_holds(tenant_id,world,hold_id,scope_kind,scope_ref,basis,held_by,valid_until)
   values(p_tenant,p_world,p_intent,body->>'scope_kind',body->>'scope_ref',body->>'basis',p_principal,(body->>'valid_until')::timestamptz) on conflict do nothing;
  return jsonb_build_object('hold_id',p_intent,'scope_kind',body->>'scope_kind');
 end if;
 if exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','hold_id','reason'))
  or coalesce(body->>'hold_id','')='' or length(coalesce(body->>'reason','')) not between 1 and 500 then
  raise exception 'retention hold release invalid' using errcode='22023';end if;
 update control.nexloop_retention_holds set released_at=clock_timestamp(),released_by=p_principal,release_reason=body->>'reason'
  where tenant_id=p_tenant and world=p_world and hold_id=body->>'hold_id' and released_at is null;
 get diagnostics n=row_count;
 if n=0 then raise exception 'retention hold unavailable' using errcode='42501';end if;
 return jsonb_build_object('hold_id',body->>'hold_id','released',true);
end $$;
do $owners$
declare f text;
begin
 foreach f in array array['control.nexloop_governed_erase_consumer(text,text,text,text,text,jsonb)','control.nexloop_governed_erase_message(text,text,text,text,text,jsonb)',
   'control.nexloop_governed_retention_hold(text,text,text,text,text,jsonb)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';execute 'revoke all on function '||f||' from public';
 end loop;
end $owners$;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('consumer.erase','erase_consumer','human','control.nexloop_governed_erase_consumer(text,text,text,text,text,jsonb)','NX-029'),
 ('message.erase','erase_message','human','control.nexloop_governed_erase_message(text,text,text,text,text,jsonb)','NX-029'),
 ('retention.hold','hold_retention','human','control.nexloop_governed_retention_hold(text,text,text,text,text,jsonb)','NX-029'),
 ('retention.release_hold','release_retention_hold','human','control.nexloop_governed_retention_hold(text,text,text,text,text,jsonb)','NX-029');

-- D1: a Consumer's own request (from NX-019 data-rights Claims or WebChat) only waits for the owner's confirmation.
create function runtime.nexloop_consumer_erasure_requested(p_tenant text,p_world text,p_consumer text,p_ref text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 insert into control.nexloop_consumer_erasures(tenant_id,world,request_id,consumer_id,source,requested_by,reason,state,watermark)
  values(p_tenant,p_world,'consumer-request:'||p_ref,p_consumer,'consumer_request','consumer','consumer asked for deletion','pending_confirmation',
   nextval('control.nexloop_deletion_watermark')) on conflict do nothing;
 return (select jsonb_build_object('request_id',request_id,'state',state) from control.nexloop_consumer_erasures where tenant_id=p_tenant and world=p_world and consumer_id=p_consumer);
end $$;
alter function runtime.nexloop_consumer_erasure_requested(text,text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_consumer_erasure_requested(text,text,text,text) from public;

-- 6. Holds in the expiry sweeps: a held class does nothing (0141 sweep renamed and kept).
alter function runtime.nexloop_retention_sweep(text,text,text,integer,text,text) rename to nexloop_retention_sweep_before_holds_v0142;
create function runtime.nexloop_retention_held(p_tenant text,p_world text,p_kind text,p_ref text) returns boolean
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v boolean;
begin
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 v:=exists(select 1 from control.nexloop_retention_holds h where h.tenant_id=p_tenant and h.world=p_world and h.scope_kind=p_kind and h.scope_ref=p_ref
  and h.released_at is null and (h.valid_until is null or h.valid_until>clock_timestamp()));
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
create function runtime.nexloop_retention_sweep(p_tenant text,p_world text,p_class text,p_batch integer,p_ref text,p_by text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if runtime.nexloop_retention_held(p_tenant,p_world,'item_class',p_class) then
  perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,'retention_expiry',p_ref,p_class,'batch',p_class,'skipped_hold','{}'::jsonb,p_by);
  return jsonb_build_object('item_class',p_class,'processed',0,'more',false,'counts','{}'::jsonb,'held',true);
 end if;
 return runtime.nexloop_retention_sweep_before_holds_v0142(p_tenant,p_world,p_class,p_batch,p_ref,p_by);
end $$;
do $owners$
declare f text;
begin
 foreach f in array array['runtime.nexloop_retention_sweep(text,text,text,integer,text,text)','runtime.nexloop_retention_held(text,text,text,text)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';execute 'revoke all on function '||f||' from public';
 end loop;
end $owners$;

-- 7. Erasure steps (the keeper drives them) ------------------------------------------------------------------------------
-- Runs of a Consumer: role Runs and message Runs (run_command.consumer_ref). Callers have set the tenant.
create function runtime.nexloop_consumer_runs(p_tenant text,p_world text,p_consumer text) returns setof uuid
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select run_id from authz.nexloop_role_run_bindings where tenant_id=p_tenant and world=p_world and consumer_id=p_consumer
 union
 select b.run_id from authz.nexloop_runtime_run_bindings b join runtime.jobs j on j.tenant_id=b.tenant_id and j.job_id=b.task_id
  where b.tenant_id=p_tenant and b.world=p_world and j.normalized_input->'run_command'->>'consumer_ref'='consumer:'||p_consumer
$$;
alter function runtime.nexloop_consumer_runs(text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_consumer_runs(text,text,text) from public;

create function runtime.nexloop_erasure_step(p_tenant text,p_world text,p_request text,p_batch integer,p_by text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare e control.nexloop_consumer_erasures;v_ref text;n integer:=0;m integer:=0;x record;v_phases text[]:=array['messages','claims','runs_text','commercial',
 'commitments','runs_files','artifacts','consumer'];v_next text;v_pseudonym text;v_counts jsonb:='{}'::jsonb;k bytea;v_held boolean;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into e from control.nexloop_consumer_erasures where tenant_id=p_tenant and world=p_world and request_id=p_request for update skip locked;
 if not found or e.state in ('pending_confirmation','completed','completed_with_holds') then return jsonb_build_object('request_id',p_request,'state',e.state,'progress',false);end if;
 v_ref:='erasure:'||e.request_id;
 v_held:=runtime.nexloop_retention_held(p_tenant,p_world,'consumer',e.consumer_id);
 if e.state in ('blocking','settling','waiting_unknown_effects') then
  -- Unknown external results are reconciled first (0065); never resent blindly.
  if exists(select 1 from runtime.nexloop_effect_intents i where i.tenant_id=p_tenant and i.world=p_world and i.consumer_id=e.consumer_id
    and i.state in ('dispatching','unknown')) then
   update control.nexloop_consumer_erasures set state='waiting_unknown_effects',updated_at=clock_timestamp() where tenant_id=p_tenant and world=p_world and request_id=p_request;
   return jsonb_build_object('request_id',p_request,'state','waiting_unknown_effects','progress',false);
  end if;
  update control.nexloop_consumer_erasures set state='purging',phase='messages',updated_at=clock_timestamp() where tenant_id=p_tenant and world=p_world and request_id=p_request
   returning * into e;
 end if;
 if v_held then
  -- A Consumer hold keeps everything; the request completes with the hold recorded and nothing purged.
  perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,'consumer_erasure',v_ref,'consumer','consumer',e.consumer_id,'skipped_hold','{}'::jsonb,p_by);
  update control.nexloop_consumer_erasures set state='completed_with_holds',phase=null,completed_at=clock_timestamp(),updated_at=clock_timestamp()
   where tenant_id=p_tenant and world=p_world and request_id=p_request;
  return jsonb_build_object('request_id',p_request,'state','completed_with_holds','progress',true);
 end if;
 if e.phase='messages' then
  for x in select m.message_id from runtime.nexloop_conversation_messages m join runtime.nexloop_conversations c on c.tenant_id=m.tenant_id and c.world=m.world
    and c.conversation_id=m.conversation_id where m.tenant_id=p_tenant and m.world=p_world and c.consumer_id=e.consumer_id
    and (m.record->>'body'<>'' or exists(select 1 from ontology.objects o where o.tenant_id=m.tenant_id and o.world=m.world and o.type_name='Message' and o.object_id=m.message_id))
    order by m.conversation_id,m.sequence limit p_batch loop
   perform runtime.nexloop_erase_message(p_tenant,p_world,x.message_id,v_ref,'consumer_erasure',p_by);n:=n+1;
  end loop;
  v_counts:=jsonb_build_object('messages',n);
 elsif e.phase='claims' then
  with due as (select claim_id from ontology.nexloop_claims where tenant_id=p_tenant and world=p_world and consumer_id=e.consumer_id
    and not (value ? 'erased') limit p_batch for update skip locked)
  update ontology.nexloop_claims c set quote='',subject_text='',condition_text='',time_expression='',value='{"type":"erased","value":null,"erased":true}'::jsonb
   from due where c.tenant_id=p_tenant and c.world=p_world and c.claim_id=due.claim_id;
  get diagnostics n=row_count;v_counts:=jsonb_build_object('claims',n);
 elsif e.phase='runs_text' then
  perform runtime.nexloop_erasure_open('runtime.nexloop_model_request_prompts',array['request_text'],false,v_ref);
  update runtime.nexloop_model_request_prompts set request_text='' where tenant_id=p_tenant and world=p_world and request_text<>''
   and run_id in (select runtime.nexloop_consumer_runs(p_tenant,p_world,e.consumer_id));
  get diagnostics n=row_count;
  update runtime.jobs j set normalized_input=j.normalized_input||'{"input":"","input_erased":true}'::jsonb from authz.nexloop_runtime_run_bindings b
   where b.tenant_id=p_tenant and b.world=p_world and j.tenant_id=b.tenant_id and j.job_id=b.task_id and coalesce(j.normalized_input->>'input','')<>''
    and b.run_id in (select runtime.nexloop_consumer_runs(p_tenant,p_world,e.consumer_id));
  get diagnostics m=row_count;
  perform runtime.nexloop_erasure_close();
  v_counts:=jsonb_build_object('prompts',n,'jobs',m);n:=0;
 elsif e.phase='commercial' then
  v_counts:=runtime.nexloop_commercial_pseudonymize_consumer(p_tenant,p_world,e.consumer_id);
  perform set_config('eios.tenant_id',p_tenant,true);
  v_pseudonym:='pseudonym:'||encode(extensions.gen_random_bytes(16),'hex');
  perform runtime.nexloop_erasure_open('runtime.nexloop_cost_entries',array['consumer_ref'],false,v_ref);
  perform runtime.nexloop_erasure_open('control.nexloop_metric_cohorts',array['member_ref'],false,v_ref);
  update runtime.nexloop_cost_entries set consumer_ref=null where tenant_id=p_tenant and world=p_world and consumer_ref=e.consumer_id;
  get diagnostics n=row_count;
  update control.nexloop_metric_cohorts set member_ref=v_pseudonym where tenant_id=p_tenant and world=p_world and member_ref=e.consumer_id;
  get diagnostics m=row_count;
  perform runtime.nexloop_erasure_close();
  v_counts:=v_counts||jsonb_build_object('cost_entries',n,'cohort_members',m);n:=0;
 elsif e.phase='commitments' then
  perform runtime.nexloop_erasure_open('ontology.objects',array['commitment_erasure'],false,v_ref);
  update ontology.objects set properties=properties||jsonb_build_object('made_to','erasure:'||e.request_id,'condition',null),updated_at=clock_timestamp()
   where tenant_id=p_tenant and world=p_world and type_name='Commitment' and properties->>'made_to'=e.consumer_id;
  get diagnostics n=row_count;
  perform runtime.nexloop_erasure_close();
  v_counts:=jsonb_build_object('commitments',n);n:=0;
 elsif e.phase='runs_files' then
  insert into runtime.nexloop_run_purge_items(tenant_id,world,run_id,request_ref,reason)
   select p_tenant,p_world,r,v_ref,'consumer_erasure' from runtime.nexloop_consumer_runs(p_tenant,p_world,e.consumer_id) r on conflict do nothing;
  get diagnostics m=row_count;v_counts:=jsonb_build_object('run_purges_queued',m);
 elsif e.phase='artifacts' then
  insert into runtime.nexloop_erasure_artifacts(tenant_id,world,artifact_id,request_id)
   select p_tenant,p_world,k2.artifact_id,e.request_id from runtime.nexloop_context_packs k2 where k2.tenant_id=p_tenant and k2.world=p_world
    and k2.run_id in (select runtime.nexloop_consumer_runs(p_tenant,p_world,e.consumer_id))
   union select p_tenant,p_world,q.prompt_artifact_id,e.request_id from runtime.nexloop_model_requests q where q.tenant_id=p_tenant and q.world=p_world
    and q.run_id in (select runtime.nexloop_consumer_runs(p_tenant,p_world,e.consumer_id))
   on conflict do nothing;
  get diagnostics m=row_count;v_counts:=jsonb_build_object('artifacts_queued',m);
 elsif e.phase='consumer' then
  -- Files first: the request completes only when its Run files and Artifacts are gone (a failed purge keeps it open).
  if exists(select 1 from runtime.nexloop_run_purge_items where tenant_id=p_tenant and world=p_world and request_ref=v_ref and state='pending')
   or exists(select 1 from runtime.nexloop_erasure_artifacts a join runtime.nexloop_local_artifacts l on l.tenant_id=a.tenant_id and l.world=a.world
     and l.artifact_id=a.artifact_id where a.tenant_id=p_tenant and a.world=p_world and a.request_id=e.request_id and l.status<>'deleted') then
   return jsonb_build_object('request_id',p_request,'state','purging','phase','consumer','progress',false,'waiting','files');
  end if;
  -- D5: an identity-free key per external reference (salted per tenant, owner side) keeps a re-imported identity uncontacted.
  insert into control.nexloop_erasure_salts(tenant_id,salt) values(p_tenant,extensions.gen_random_bytes(32)) on conflict do nothing;
  select salt into k from control.nexloop_erasure_salts where tenant_id=p_tenant;
  insert into control.nexloop_erased_contact_keys(tenant_id,world,key_digest,request_id)
   select p_tenant,p_world,encode(extensions.hmac(convert_to(ref.value#>>'{}','UTF8'),k,'sha256'),'hex'),e.request_id
   from ontology.objects o,jsonb_array_elements(case when jsonb_typeof(o.properties->'external_id_refs')='array' then o.properties->'external_id_refs' else '[]'::jsonb end) ref
   where o.tenant_id=p_tenant and o.world=p_world and o.type_name='Consumer' and o.object_id=e.consumer_id on conflict do nothing;
  get diagnostics m=row_count;
  delete from ontology.events where tenant_id=p_tenant and object_type='Consumer' and object_id=e.consumer_id;
  delete from ontology.relations where tenant_id=p_tenant and ((source_type='Consumer' and source_object_id=e.consumer_id) or (target_type='Consumer' and target_object_id=e.consumer_id));
  delete from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='Consumer' and object_id=e.consumer_id;
  get diagnostics n=row_count;
  perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,'consumer_erasure',v_ref,'consumer','consumer',e.consumer_id,'deleted',
   e.counts||jsonb_build_object('consumer_object',n,'contact_keys',m),p_by);
  update control.nexloop_consumer_erasures set state='completed',phase=null,completed_at=clock_timestamp(),updated_at=clock_timestamp(),
   counts=counts||jsonb_build_object('consumer_object',n,'contact_keys',m) where tenant_id=p_tenant and world=p_world and request_id=p_request;
  return jsonb_build_object('request_id',p_request,'state','completed','progress',true);
 end if;
 -- A phase is done when its batch was not full; then the next phase.
 v_next:=case when n>=p_batch then e.phase else v_phases[array_position(v_phases,e.phase)+1] end;
 update control.nexloop_consumer_erasures set phase=v_next,updated_at=clock_timestamp(),
  counts=counts||(select coalesce(jsonb_object_agg(key,coalesce((counts->>key)::bigint,0)+(value#>>'{}')::bigint),'{}'::jsonb) from jsonb_each(v_counts))
  where tenant_id=p_tenant and world=p_world and request_id=p_request;
 return jsonb_build_object('request_id',p_request,'state','purging','phase',v_next,'progress',true,'counts',v_counts);
end $$;
alter function runtime.nexloop_erasure_step(text,text,text,integer,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_erasure_step(text,text,text,integer,text) from public;

-- 8. Artifact eligibility (dispatcher ruling): expired, or of an erasing Consumer; Runtime references only from terminal
-- jobs (the erasure redacts their inputs); never an invocation reference.
create function runtime.nexloop_artifact_purgeable(p_tenant text,p_world text,p_artifact text) returns boolean
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select exists(select 1 from runtime.nexloop_local_artifacts a where a.tenant_id=p_tenant and a.world=p_world and a.artifact_id=p_artifact
   and (a.status='available' or (a.status='deleting' and a.lease_until<=clock_timestamp()))
   and (a.retention_until<=clock_timestamp() or exists(select 1 from runtime.nexloop_erasure_artifacts x join control.nexloop_consumer_erasures e
     on e.tenant_id=x.tenant_id and e.world=x.world and e.request_id=x.request_id
     where x.tenant_id=a.tenant_id and x.world=a.world and x.artifact_id=a.artifact_id and e.state not in ('pending_confirmation','completed_with_holds')))
   and not exists(select 1 from runtime.invocations i where i.tenant_id=a.tenant_id and i.artifact_refs::text like '%'||a.artifact_id||'%')
   and not exists(select 1 from runtime.jobs j where j.tenant_id=a.tenant_id and j.status not in ('succeeded','failed','dead_lettered','cancelled')
     and to_jsonb(j)::text like '%'||a.artifact_id||'%'))
$$;
alter function runtime.nexloop_artifact_purgeable(text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_artifact_purgeable(text,text,text) from public;

-- 9. Keeper port for erasure work (nexloop.erasure.execute:1; trusted configuration grants it to retention_keeper only;
-- the signed service port refuses Agents, Run credentials and browser sessions). Counts and ids only, never content.
create function authz.nexloop_erasure_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-erasure-v1',
  'eios:action:nexloop.erasure.execute:1',array['nexloop_domain_worker']);c jsonb:=p_payload::jsonb;v jsonb;v_by text:='service:'||((p_text::jsonb)->>'principal_id');
 art runtime.nexloop_local_artifacts;n integer;v_limit integer;v_ref text;
begin
 perform set_config('eios.tenant_id',t,true);
 v_limit:=coalesce((c->>'limit')::integer,50);
 if v_limit not between 1 and 200 then raise exception 'erasure command invalid' using errcode='22023';end if;
 if c->>'verb'='requests' then
  select coalesce(jsonb_agg(request_id order by created_at),'[]'::jsonb) into v from (select request_id,created_at from control.nexloop_consumer_erasures
   where tenant_id=t and world=p_world and state in ('blocking','settling','waiting_unknown_effects','purging') order by created_at limit v_limit) r;
  return jsonb_build_object('requests',v);
 elsif c->>'verb'='step' then
  if coalesce(c->>'request_id','')!~'^[A-Za-z0-9._:-]{1,190}$' then raise exception 'erasure command invalid' using errcode='22023';end if;
  return runtime.nexloop_erasure_step(t,p_world,c->>'request_id',(runtime.nexloop_retention_settings_current()->'sweep'->>'batch')::integer,v_by);
 elsif c->>'verb'='run_purges' then
  -- Only Runs whose task is terminal (or that have none); the Host refuses an active Run anyway.
  select coalesce(jsonb_agg(p.run_id order by p.created_at),'[]'::jsonb) into v from (select i.run_id,i.created_at from runtime.nexloop_run_purge_items i
   where i.tenant_id=t and i.world=p_world and i.state='pending'
    and not exists(select 1 from authz.nexloop_runtime_run_bindings b join runtime.jobs j on j.tenant_id=b.tenant_id and j.job_id=b.task_id
     where b.tenant_id=t and b.run_id=i.run_id and j.status not in ('succeeded','failed','dead_lettered','cancelled'))
   order by i.created_at limit v_limit) p;
  return jsonb_build_object('runs',v);
 elsif c->>'verb'='run_purged' then
  if coalesce(c->>'run_id','')!~'^[0-9a-f-]{36}$' or c->>'outcome' not in ('purged','absent','failed') then raise exception 'erasure command invalid' using errcode='22023';end if;
  if c->>'outcome'='failed' then
   update runtime.nexloop_run_purge_items set attempts=attempts+1,last_code='host_purge_failed' where tenant_id=t and world=p_world and run_id=(c->>'run_id')::uuid and state='pending';
   return jsonb_build_object('run_id',c->>'run_id','state','pending');
  end if;
  update runtime.nexloop_run_purge_items set state='purged',purged_at=clock_timestamp() where tenant_id=t and world=p_world and run_id=(c->>'run_id')::uuid and state='pending'
   returning request_ref into v_ref;
  if v_ref is not null then
   perform runtime.nexloop_erasure_tombstone(t,p_world,'consumer_erasure',v_ref,'run_file','pi_run',c->>'run_id','deleted',jsonb_build_object('outcome',c->>'outcome'),v_by);
   insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
    values('run-purge:'||(c->>'run_id'),'nexloop.erasure.run_purged',t,v_by,'erasure',v_ref,jsonb_build_object('run_id',c->>'run_id','outcome',c->>'outcome'),clock_timestamp())
    on conflict do nothing;
  end if;
  return jsonb_build_object('run_id',c->>'run_id','state','purged');
 elsif c->>'verb'='artifact_candidates' then
  select coalesce(jsonb_agg(x.artifact_id order by x.artifact_id),'[]'::jsonb) into v from (select a.artifact_id from runtime.nexloop_local_artifacts a
   where a.tenant_id=t and a.world=p_world and a.status in ('available','deleting') and runtime.nexloop_artifact_purgeable(t,p_world,a.artifact_id)
   order by a.artifact_id limit v_limit) x;
  return jsonb_build_object('artifacts',v);
 elsif c->>'verb'='artifact_claim' then
  if coalesce(c->>'artifact_id','')!~'^[a-f0-9]{32}$' or coalesce(c->>'worker','')!~'^[a-f0-9]{32}$' then raise exception 'erasure command invalid' using errcode='22023';end if;
  select * into art from runtime.nexloop_local_artifacts where tenant_id=t and world=p_world and artifact_id=c->>'artifact_id' for update;
  -- Re-verified here, whatever the keeper computed.
  if not found or not runtime.nexloop_artifact_purgeable(t,p_world,art.artifact_id) then raise exception 'artifact not purgeable' using errcode='42501';end if;
  update runtime.nexloop_local_artifacts set status='deleting',upload_worker=c->>'worker',upload_token=encode(extensions.gen_random_bytes(24),'hex'),
   upload_fence=upload_fence+1,lease_until=clock_timestamp()+interval '30 seconds',updated_at=clock_timestamp()
   where tenant_id=t and world=p_world and artifact_id=art.artifact_id returning * into art;
  return jsonb_build_object('artifact_id',art.artifact_id,'sha256',art.sha256,'size_bytes',art.size_bytes,'media_type',art.media_type,'fence',art.upload_fence);
 elsif c->>'verb'='artifact_deleted' then
  if coalesce(c->>'artifact_id','')!~'^[a-f0-9]{32}$' or coalesce(c->>'worker','')!~'^[a-f0-9]{32}$' then raise exception 'erasure command invalid' using errcode='22023';end if;
  select * into art from runtime.nexloop_local_artifacts where tenant_id=t and world=p_world and artifact_id=c->>'artifact_id' for update;
  if not found or art.status<>'deleting' or art.upload_worker is distinct from c->>'worker' or art.upload_fence is distinct from (c->>'fence')::bigint
   or art.lease_until<=clock_timestamp() then raise exception 'artifact deletion fenced' using errcode='40001';end if;
  update runtime.nexloop_local_artifacts set status='deleted',updated_at=clock_timestamp() where tenant_id=t and world=p_world and artifact_id=art.artifact_id;
  perform runtime.nexloop_erasure_tombstone(t,p_world,case when art.retention_until<=clock_timestamp() then 'retention_expiry' else 'consumer_erasure' end,
   'artifact:'||art.artifact_id,'artifact','local_artifact',art.artifact_id,'deleted',jsonb_build_object('size_bytes',art.size_bytes),v_by);
  insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
   values('artifact-purge:'||art.artifact_id,'nexloop.erasure.artifact_deleted',t,v_by,'erasure','artifact:'||art.artifact_id,
    jsonb_build_object('artifact_id',art.artifact_id,'principal_id',art.principal_id),clock_timestamp()) on conflict do nothing;
  return jsonb_build_object('artifact_id',art.artifact_id,'status','deleted');
 elsif c->>'verb'='status' then
  return jsonb_build_object('world',p_world,
   'requests',(select coalesce(jsonb_agg(jsonb_build_object('request_id',request_id,'state',state,'phase',phase,'counts',counts,'watermark',watermark) order by created_at),'[]'::jsonb)
     from control.nexloop_consumer_erasures where tenant_id=t and world=p_world),
   'run_purges_pending',(select count(*) from runtime.nexloop_run_purge_items where tenant_id=t and world=p_world and state='pending'));
 end if;
 raise exception 'erasure command invalid' using errcode='22023';
end $$;
alter function authz.nexloop_erasure_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_erasure_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_erasure_command(text,text,text,text,text) to nexloop_domain_worker;
