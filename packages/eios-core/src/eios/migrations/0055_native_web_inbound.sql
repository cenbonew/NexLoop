-- Explicit native WebChat provider identity; formal objects still use original governed Actions.
create table runtime.nexloop_native_web_events (
 tenant_id text not null,world text not null check(world='real'),provider_namespace text not null check(provider_namespace='native.webchat'),
 principal_id text not null,provider_event_id uuid not null,conversation_id text not null,consumer_id text not null,
 message_id text not null,payload_digest text not null,first_transport_key text not null,accepted_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,provider_namespace,principal_id,provider_event_id),unique(tenant_id,world,message_id)
);
alter table runtime.nexloop_native_web_events owner to nexloop_owner;
alter table runtime.nexloop_native_web_events enable row level security;
alter table runtime.nexloop_native_web_events force row level security;
create policy native_web_tenant on runtime.nexloop_native_web_events to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_native_web_events from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;
create table runtime.nexloop_native_web_transports (
 tenant_id text not null,world text not null,provider_namespace text not null,principal_id text not null,
 transport_key text not null,provider_event_id uuid not null,conversation_id text not null,payload_digest text not null,
 primary key(tenant_id,world,provider_namespace,principal_id,transport_key),
 foreign key(tenant_id,world,provider_namespace,principal_id,provider_event_id) references runtime.nexloop_native_web_events(tenant_id,world,provider_namespace,principal_id,provider_event_id)
);
alter table runtime.nexloop_native_web_transports owner to nexloop_owner;
alter table runtime.nexloop_native_web_transports enable row level security;
alter table runtime.nexloop_native_web_transports force row level security;
create policy native_web_transport_tenant on runtime.nexloop_native_web_transports to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_native_web_transports from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;
create function authz.nexloop_native_web_message(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;v_result jsonb;v_existing runtime.nexloop_native_web_events%rowtype;
 v_tenant text:=a->>'tenant_id';v_principal text:=a->>'principal_id';v_transport runtime.nexloop_native_web_transports%rowtype;v_event uuid;v_payload_digest text;v_consumer text;v_event_text text:=c->>'event_key_text';
begin
 if session_user is distinct from 'nexloop_api' or p_world is distinct from 'real'
  or octet_length(p_text)>1048576 or octet_length(p_payload)>262144
  or a->>'protocol' is distinct from 'nexloop-conversation-v1'
  or a->>'action_resource' is distinct from 'eios:action:Message.create:1'
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or c->>'verb' not in ('prepare_message','commit_message') or c->>'verb' is null
  or c->>'provider_namespace' is distinct from 'native.webchat'
  or jsonb_typeof(c->'provider_event_id') is distinct from 'string'
  or c->>'provider_event_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
  or jsonb_typeof(c->'transport_key') is distinct from 'string' or length(c->>'transport_key') not between 16 and 200
  or c->>'transport_key' !~ '^[A-Za-z0-9._~-]+$' or jsonb_typeof(c->'body') is distinct from 'string'
  or v_event_text is null then raise exception 'conversation unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-conversation-v1:'||p_text,'UTF8'),k,'sha256'),'hex') then
  raise exception 'conversation unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 if not exists(select 1 from control.nexloop_browser_sessions where encode(session_token_digest,'hex')=p_digest) then
  raise exception 'conversation unavailable' using errcode='42501';end if;
 v_event:=(c->>'provider_event_id')::uuid;
 if v_event_text::jsonb is distinct from jsonb_build_array(v_tenant,p_world,'native.webchat',v_principal,v_event::text)
  or c->>'idempotency_key' is distinct from 'native-'||encode(sha256(convert_to(v_event_text,'UTF8')),'hex') then
  raise exception 'conversation unavailable' using errcode='42501';end if;
 -- Stable provider event serializes even when competing conversations have different row locks.
 perform pg_advisory_xact_lock(hashtextextended(v_tenant||':'||v_principal||':transport:'||(c->>'transport_key'),55));
 perform pg_advisory_xact_lock(hashtextextended(v_event_text,55));
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 v_payload_digest:=encode(sha256(convert_to(c->>'body','UTF8')),'hex');
 select * into v_transport from runtime.nexloop_native_web_transports where tenant_id=v_tenant and world=p_world and provider_namespace='native.webchat' and principal_id=v_principal and transport_key=c->>'transport_key' for update;
 if found and (v_transport.provider_event_id is distinct from v_event or v_transport.conversation_id is distinct from c->>'conversation_id' or v_transport.payload_digest is distinct from v_payload_digest) then
  raise exception 'conversation_payload_conflict' using errcode='P0001';end if;
 select * into v_existing from runtime.nexloop_native_web_events where tenant_id=v_tenant and world=p_world
  and provider_namespace='native.webchat' and principal_id=v_principal and provider_event_id=v_event for update;
 if found and (v_existing.conversation_id is distinct from c->>'conversation_id' or v_existing.payload_digest is distinct from v_payload_digest) then
  raise exception 'conversation_payload_conflict' using errcode='P0001';end if;
 -- Original protected definer proves current ownership/definition/claim and writes Message/inbox/outbox atomically.
 v_result:=authz.nexloop_conversation_command(p_digest,p_world,p_text,p_signature,p_payload);
 if c->>'verb'='prepare_message' and v_result->'replay'='true'::jsonb and (v_existing.message_id is null or v_existing.message_id is distinct from v_result->'result'->'message'->>'id') then
  raise exception 'conversation unavailable' using errcode='42501';end if;
 if c->>'verb'='commit_message' then
  select consumer_id into v_consumer from runtime.nexloop_conversations where tenant_id=v_tenant and world=p_world and conversation_id=c->>'conversation_id' for share;
  if not found or v_result->'message'->>'id' is null then raise exception 'conversation unavailable' using errcode='42501';end if;
  insert into runtime.nexloop_native_web_events(tenant_id,world,provider_namespace,principal_id,provider_event_id,conversation_id,consumer_id,message_id,payload_digest,first_transport_key)
   values(v_tenant,p_world,'native.webchat',v_principal,v_event,c->>'conversation_id',v_consumer,v_result->'message'->>'id',v_payload_digest,c->>'transport_key')
   on conflict(tenant_id,world,provider_namespace,principal_id,provider_event_id) do nothing;
  select * into v_existing from runtime.nexloop_native_web_events where tenant_id=v_tenant and world=p_world and principal_id=v_principal and provider_namespace='native.webchat' and provider_event_id=v_event;
  if v_existing.message_id is distinct from v_result->'message'->>'id' then raise exception 'conversation unavailable' using errcode='42501';end if;
 end if;
 if c->>'verb'='commit_message' or v_result->'replay'='true'::jsonb then
  insert into runtime.nexloop_native_web_transports values(v_tenant,p_world,'native.webchat',v_principal,c->>'transport_key',v_event,c->>'conversation_id',v_payload_digest)
   on conflict(tenant_id,world,provider_namespace,principal_id,transport_key) do nothing;
 end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 return v_result;
end $$;
alter function authz.nexloop_native_web_message(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_native_web_message(text,text,text,text,text) from public,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;
grant execute on function authz.nexloop_native_web_message(text,text,text,text,text) to nexloop_api;
