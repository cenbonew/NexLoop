-- NX-047 (ADR-020 §2): Agent outbound conversation messages.
-- 1. An accepted nexloop.service.request intent whose Run was issued for a consumer
--    Message and whose parameters carry a reply text is recorded as an outbound record
--    in the SAME transaction as the intent (Action-approved content only; drafts and
--    rejected requests never get a row). It never enters the relay's inbound outbox.
-- 2. Delivery state advances monotonically, only from the effect ledger (attempts,
--    observations, intent state, reconcile writes them); no caller sets it directly.
-- 3. Once the channel accepted/delivered it, a governed Message.agent_create:1 Action
--    (outbound recorder service) materializes the Message object and appends it to the
--    conversation stream with the next sequence. Persisted is not delivered: the
--    outbound record exists from acceptance; the visible Message only after acceptance
--    by the channel.
create table runtime.nexloop_outbound_messages (
 tenant_id text not null,world text not null check(world='real'),intent_id uuid not null,receipt_id uuid not null,
 conversation_id text not null,trigger_message_id text not null,run_id uuid not null,
 sender_kind text not null check(sender_kind='agent'),sender_principal text not null,role_ref text not null,
 body_digest text not null check(body_digest~'^[0-9a-f]{64}$'),
 delivery_state text not null default 'persisted' check(delivery_state in ('persisted','dispatching','provider_accepted','delivered','unknown','failed')),
 persisted_at timestamptz not null default clock_timestamp(),delivery_changed_at timestamptz not null default clock_timestamp(),
 message_id text,sequence bigint,materialized_at timestamptz,
 primary key(tenant_id,world,intent_id),unique(tenant_id,world,message_id),
 check((message_id is null)=(sequence is null) and (sequence is null)=(materialized_at is null)),
 check(message_id is null or delivery_state in ('provider_accepted','delivered')),
 foreign key(intent_id,tenant_id,world) references runtime.nexloop_effect_intents(intent_id,tenant_id,world),
 foreign key(tenant_id,world,conversation_id) references runtime.nexloop_conversations
);
create table runtime.nexloop_outbound_delivery_events (
 tenant_id text not null,world text not null,intent_id uuid not null,event_number bigint generated always as identity,
 from_state text not null,to_state text not null,source text not null check(source in ('intent','attempt','observation')),
 recorded_at timestamptz not null default clock_timestamp(),primary key(event_number),
 foreign key(tenant_id,world,intent_id) references runtime.nexloop_outbound_messages(tenant_id,world,intent_id)
);
alter table runtime.nexloop_outbound_messages owner to nexloop_owner;
alter table runtime.nexloop_outbound_delivery_events owner to nexloop_owner;
alter table runtime.nexloop_outbound_messages enable row level security;
alter table runtime.nexloop_outbound_messages force row level security;
alter table runtime.nexloop_outbound_delivery_events enable row level security;
alter table runtime.nexloop_outbound_delivery_events force row level security;
create policy outbound_message_tenant on runtime.nexloop_outbound_messages to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
create policy outbound_delivery_event_tenant on runtime.nexloop_outbound_delivery_events to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_outbound_messages,runtime.nexloop_outbound_delivery_events
 from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;

-- Only forward transitions; terminal states never change. Identity columns immutable.
create function authz.nexloop_outbound_transition_allowed(p_from text,p_to text) returns boolean
 language sql immutable set search_path=pg_catalog as $$
 select p_from=p_to or (p_from,p_to) in (
  ('persisted','dispatching'),('persisted','provider_accepted'),('persisted','delivered'),('persisted','unknown'),('persisted','failed'),
  ('dispatching','provider_accepted'),('dispatching','delivered'),('dispatching','unknown'),('dispatching','failed'),
  ('provider_accepted','delivered'),
  ('unknown','provider_accepted'),('unknown','delivered'),('unknown','failed'))
$$;
alter function authz.nexloop_outbound_transition_allowed(text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_outbound_transition_allowed(text,text) from public;
create function authz.nexloop_outbound_guard() returns trigger
 language plpgsql set search_path=pg_catalog as $$
begin
 if (new.tenant_id,new.world,new.intent_id,new.receipt_id,new.conversation_id,new.trigger_message_id,new.run_id,new.sender_kind,new.sender_principal,new.role_ref,new.body_digest,new.persisted_at)
  is distinct from (old.tenant_id,old.world,old.intent_id,old.receipt_id,old.conversation_id,old.trigger_message_id,old.run_id,old.sender_kind,old.sender_principal,old.role_ref,old.body_digest,old.persisted_at)
  or (old.message_id is not null and (new.message_id,new.sequence,new.materialized_at) is distinct from (old.message_id,old.sequence,old.materialized_at))
  or not authz.nexloop_outbound_transition_allowed(old.delivery_state,new.delivery_state) then
  raise exception 'outbound message transition rejected' using errcode='22023';end if;
 return new;
end $$;
alter function authz.nexloop_outbound_guard() owner to nexloop_owner;
revoke all on function authz.nexloop_outbound_guard() from public;
create trigger nexloop_outbound_guard before update on runtime.nexloop_outbound_messages for each row execute function authz.nexloop_outbound_guard();
create function authz.nexloop_outbound_no_delete() returns trigger language plpgsql set search_path=pg_catalog as $$
begin raise exception 'outbound messages are append-only' using errcode='22023';end $$;
alter function authz.nexloop_outbound_no_delete() owner to nexloop_owner;
revoke all on function authz.nexloop_outbound_no_delete() from public;
create trigger nexloop_outbound_no_delete before delete on runtime.nexloop_outbound_messages for each row execute function authz.nexloop_outbound_no_delete();

-- Ledger-driven advance. A ledger event that would move backwards is ignored here
-- (the ledger itself is authoritative); it never rewrites an earlier delivery fact.
create function authz.nexloop_outbound_advance(p_tenant text,p_world text,p_intent uuid,p_target text,p_source text) returns void
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare o runtime.nexloop_outbound_messages%rowtype;
begin
 if p_target is null then return;end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into o from runtime.nexloop_outbound_messages where tenant_id=p_tenant and world=p_world and intent_id=p_intent for update;
 if not found or o.delivery_state=p_target or not authz.nexloop_outbound_transition_allowed(o.delivery_state,p_target) then return;end if;
 update runtime.nexloop_outbound_messages set delivery_state=p_target,delivery_changed_at=clock_timestamp()
  where tenant_id=p_tenant and world=p_world and intent_id=p_intent;
 insert into runtime.nexloop_outbound_delivery_events(tenant_id,world,intent_id,from_state,to_state,source) values(p_tenant,p_world,p_intent,o.delivery_state,p_target,p_source);
end $$;
alter function authz.nexloop_outbound_advance(text,text,uuid,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_outbound_advance(text,text,uuid,text,text) from public;
create function authz.nexloop_outbound_from_intent() returns trigger language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
begin
 perform authz.nexloop_outbound_advance(new.tenant_id,new.world,new.intent_id,case new.state when 'dispatching' then 'dispatching' when 'unknown' then 'unknown'
  when 'fulfilled' then 'delivered' when 'confirmed' then 'delivered' when 'failed' then 'failed' else null end,'intent');
 return new;
end $$;
create function authz.nexloop_outbound_from_attempt() returns trigger language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
begin
 perform authz.nexloop_outbound_advance(new.tenant_id,new.world,new.intent_id,case new.state when 'dispatching' then 'dispatching' when 'unknown' then 'unknown'
  when 'provider_accepted' then 'provider_accepted' when 'observed_fulfilled' then 'delivered' when 'fulfilled' then 'delivered' else null end,'attempt');
 return new;
end $$;
create function authz.nexloop_outbound_from_observation() returns trigger language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
begin
 -- not_found alone proves nothing; failure comes only from the terminal intent state.
 perform authz.nexloop_outbound_advance(new.tenant_id,new.world,new.intent_id,case new.provider_state when 'accepted' then 'provider_accepted'
  when 'fulfilled' then 'delivered' else null end,'observation');
 return new;
end $$;
alter function authz.nexloop_outbound_from_intent() owner to nexloop_owner;
alter function authz.nexloop_outbound_from_attempt() owner to nexloop_owner;
alter function authz.nexloop_outbound_from_observation() owner to nexloop_owner;
revoke all on function authz.nexloop_outbound_from_intent(),authz.nexloop_outbound_from_attempt(),authz.nexloop_outbound_from_observation() from public;
create trigger nexloop_outbound_intent after update of state on runtime.nexloop_effect_intents for each row execute function authz.nexloop_outbound_from_intent();
create trigger nexloop_outbound_attempt after insert or update of state on runtime.nexloop_effect_attempts for each row execute function authz.nexloop_outbound_from_attempt();
create trigger nexloop_outbound_observation after insert on runtime.nexloop_effect_observations for each row execute function authz.nexloop_outbound_from_observation();

-- Intent admission: record the outbound reply in the same transaction as the intent.
alter function authz.nexloop_effect_intent_command(text,text,text,text,text) rename to nexloop_effect_intent_command_before_outbound_v0077;
revoke all on function authz.nexloop_effect_intent_command_before_outbound_v0077(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;
create function authz.nexloop_effect_intent_command(d text,w text,t text,s text,body text) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare c jsonb:=t::jsonb;p jsonb:=body::jsonb;result jsonb;i runtime.nexloop_effect_intents%rowtype;run uuid;issued authz.nexloop_message_run_issuances%rowtype;
 cm runtime.nexloop_conversation_messages%rowtype;reply text;
begin
 result:=authz.nexloop_effect_intent_command_before_outbound_v0077(d,w,t,s,body);
 if p->>'verb' is distinct from 'submit' or result->>'intent_id' is null then return result;end if;
 select * into i from runtime.nexloop_effect_intents where intent_id=(result->>'intent_id')::uuid;
 reply:=i.frozen_request->'parameters'->>'message';
 if reply is null or jsonb_typeof(i.frozen_request->'parameters'->'message') is distinct from 'string' then return result;end if;
 select run_id into run from authz.nexloop_run_credentials where token_digest=d;
 select * into issued from authz.nexloop_message_run_issuances where run_id=run and tenant_id=i.tenant_id and world=i.world;
 if not found then return result;end if;  -- not a conversation reply Run
 select * into cm from runtime.nexloop_conversation_messages where tenant_id=i.tenant_id and world=i.world and message_id=issued.message_id;
 if not found then raise exception 'outbound trigger message unavailable' using errcode='42501';end if;
 insert into runtime.nexloop_outbound_messages(tenant_id,world,intent_id,receipt_id,conversation_id,trigger_message_id,run_id,sender_kind,sender_principal,role_ref,body_digest)
  values(i.tenant_id,i.world,i.intent_id,i.receipt_id,cm.conversation_id,cm.message_id,run,'agent',c->>'principal_id',
   coalesce(c->'role_envelope'->>'role_ref',''),encode(sha256(convert_to(reply,'UTF8')),'hex'))
  on conflict(tenant_id,world,intent_id) do nothing;
 return result;
end $$;
alter function authz.nexloop_effect_intent_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_intent_command(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;
grant execute on function authz.nexloop_effect_intent_command(text,text,text,text,text) to nexloop_api,nexloop_domain_worker;

-- Outbound recorder: governed Message.agent_create:1 materialization of channel-accepted replies.
create function authz.nexloop_outbound_message_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;v_verb text:=c->>'verb';v_tenant text:=a->>'tenant_id';
 o runtime.nexloop_outbound_messages%rowtype;i runtime.nexloop_effect_intents%rowtype;v_conversation runtime.nexloop_conversations%rowtype;
 v_definition jsonb;v_capability jsonb;v_body jsonb;v_properties jsonb;v_create jsonb:=c->'create';v_result jsonb;v_id text;v_sequence bigint;v_record jsonb;v_limit integer;
begin
 if session_user<>'nexloop_api' or p_world is distinct from 'real' or octet_length(p_text)>1048576 or octet_length(p_payload)>262144
  or a->>'protocol' is distinct from 'nexloop-outbound-message-v1' or a->>'operation' is distinct from 'execute'
  or a->>'action_resource' is distinct from 'eios:action:Message.agent_create:1'
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') or v_verb not in ('pending','prepare','commit') or v_verb is null then
  raise exception 'outbound message unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-outbound-message-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'outbound message unavailable' using errcode='42501';end if;
 if exists(select 1 from authz.nexloop_browser_token_realms where token_digest=p_digest)
  or not exists(select 1 from authz.nexloop_service_credentials t where t.token_digest=p_digest and t.tenant_id=v_tenant and t.binding->>'subject_kind'='service' and t.status='active') then
  raise exception 'outbound message unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 select definition,capability into v_definition,v_capability from control.nexloop_action_definitions
  where tenant_id=v_tenant and world=p_world and resource_id=a->>'action_resource' and active for share;
 if not found or v_definition is distinct from a->'definition' or v_capability is distinct from a->'capability'
  or v_definition->'governance'->>'approval_mode' is distinct from 'none' or v_definition->'governance'->>'risk_level' is distinct from 'low'
  or jsonb_array_length(v_definition->'governance'->'policy_refs')<>0 then
  raise exception 'outbound message unavailable' using errcode='42501';end if;
 if v_verb='pending' then
  v_limit:=(c->>'limit')::integer;
  if v_limit not between 1 and 100 then raise exception 'outbound message unavailable' using errcode='42501';end if;
  select coalesce(jsonb_agg(x.intent_id::text order by x.delivery_changed_at,x.intent_id),'[]'::jsonb) into v_result from
   (select intent_id,delivery_changed_at from runtime.nexloop_outbound_messages where tenant_id=v_tenant and world=p_world and message_id is null
     and delivery_state in ('provider_accepted','delivered') order by delivery_changed_at,intent_id limit v_limit) x;
  perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
  return v_result;
 end if;
 if c->>'intent_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' or c->>'intent_id' is null then
  raise exception 'outbound message unavailable' using errcode='42501';end if;
 select * into o from runtime.nexloop_outbound_messages where tenant_id=v_tenant and world=p_world and intent_id=(c->>'intent_id')::uuid;
 if not found then raise exception 'outbound message unavailable' using errcode='42501';end if;
 -- Same lock order as inbound acceptance: conversation row first, then the outbound row.
 select * into v_conversation from runtime.nexloop_conversations where tenant_id=v_tenant and world=p_world and conversation_id=o.conversation_id for update;
 select * into o from runtime.nexloop_outbound_messages where tenant_id=v_tenant and world=p_world and intent_id=(c->>'intent_id')::uuid for update;
 if o.message_id is not null then
  select record into v_record from runtime.nexloop_conversation_messages where tenant_id=v_tenant and world=p_world and message_id=o.message_id;
  perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
  return jsonb_build_object('replay',true,'result',jsonb_build_object('message',v_record,'created',false));
 end if;
 if o.delivery_state not in ('provider_accepted','delivered') then raise exception 'outbound message not channel-accepted' using errcode='42501';end if;
 select * into i from runtime.nexloop_effect_intents where intent_id=o.intent_id and tenant_id=v_tenant and world=p_world;
 if not found or encode(sha256(convert_to(i.frozen_request->'parameters'->>'message','UTF8')),'hex') is distinct from o.body_digest then
  raise exception 'outbound message content changed' using errcode='42501';end if;
 v_sequence:=v_conversation.last_sequence+1;
 v_body=jsonb_build_object('request_id','agent-message-'||o.intent_id::text,'type_name','Message',
  'properties',jsonb_build_object('conversation_id',o.conversation_id,'sequence',v_sequence,'actor',o.sender_principal,
   'body',i.frozen_request->'parameters'->>'message','accepted_at',clock_timestamp()::text));
 if v_verb='prepare' then
  perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
  return jsonb_build_object('replay',false,'payload',v_body);
 end if;
 if jsonb_typeof(v_create) is distinct from 'object' or v_create->>'text' is null or v_create->>'signature' is null or v_create->>'payload' is null then
  raise exception 'outbound message unavailable' using errcode='42501';end if;
 v_properties=(v_create->>'payload')::jsonb->'properties';
 if v_properties-'accepted_at' is distinct from (v_body->'properties')-'accepted_at' or jsonb_typeof(v_properties->'accepted_at') is distinct from 'string'
  or (v_properties->>'accepted_at')::timestamptz>clock_timestamp() or (v_properties->>'accepted_at')::timestamptz<transaction_timestamp()-interval '30 seconds' then
  raise exception 'outbound message unavailable' using errcode='42501';end if;
 v_body=jsonb_set(v_body,'{properties,accepted_at}',v_properties->'accepted_at');
 if (v_create->>'payload')::jsonb is distinct from v_body then raise exception 'outbound message unavailable' using errcode='42501';end if;
 v_result=authz.nexloop_create_object_action(p_digest,p_world,v_create->>'text',v_create->>'signature',v_create->>'payload');
 v_id=v_result->>'object_id';
 v_record=jsonb_build_object('id',v_id,'conversation_id',o.conversation_id,'sequence',v_sequence,'actor',o.sender_principal,
  'body',i.frozen_request->'parameters'->>'message','accepted_at',v_properties->>'accepted_at','status','accepted',
  'direction','outbound','sender_kind',o.sender_kind,'intent_id',o.intent_id::text,'trigger_message_id',o.trigger_message_id);
 -- Deliberately no nexloop_message_inbox/outbox row: the relay must never plan for the Agent's own words.
 insert into runtime.nexloop_conversation_messages values(v_tenant,p_world,o.conversation_id,v_sequence,v_id,'agent-'||o.intent_id::text,
  encode(sha256(convert_to(jsonb_build_object('body',i.frozen_request->'parameters'->>'message')::text,'UTF8')),'hex'),v_record);
 update runtime.nexloop_conversations set last_sequence=v_sequence where tenant_id=v_tenant and world=p_world and conversation_id=o.conversation_id;
 update runtime.nexloop_outbound_messages set message_id=v_id,sequence=v_sequence,materialized_at=clock_timestamp()
  where tenant_id=v_tenant and world=p_world and intent_id=o.intent_id;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 return jsonb_build_object('message',v_record,'created',true);
end $$;
alter function authz.nexloop_outbound_message_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_outbound_message_command(text,text,text,text,text) from public,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;
grant execute on function authz.nexloop_outbound_message_command(text,text,text,text,text) to nexloop_api;
