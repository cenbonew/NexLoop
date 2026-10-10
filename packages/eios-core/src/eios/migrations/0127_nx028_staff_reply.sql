-- NX-028 slice 3 (temporary number 0153): staff replies during a takeover — dispatcher ruling B (v0.1 simplification).
--
-- A staff reply is the governed human Action message.staff_send (0150 registry) that writes the Message object and the
-- conversation stream entry directly, NOT through the effect ledger: the native WebChat conversation is itself the channel
-- (the customer reads the stream). The Action refuses any other channel (NXC07); connecting an external channel requires
-- design A (a takeover Run on the effect ledger, like the 0110 fallback Run) and is out of scope here.
-- Rules (all checked in SQL, in the Action's transaction):
-- * only the author of the active takeover covering the conversation may reply (42501 otherwise);
-- * the reply is bound to one inbound message of that conversation (reply_to required in v0.1);
-- * ADR-023 §2.6 for a restricted Consumer: inside the reply window and at most one reply per inbound message, counted over
--   the same reply records the Agent and fallback replies use (outbound ledger in flight/accepted, and the stream); the Agent
--   dispatch check (0152 wrapper) counts a staff reply the same way (NXC05);
-- * ADR-023 §2.7: the bound message's pending reply is settled in the same transaction (so no fallback Run starts);
-- * evidence: Message actor and stream sender = the staff principal, sender_kind human_takeover; provenance in
--   runtime.nexloop_staff_replies; provider facts namespace nexloop.staff (server) with a server reply link (NX-051).
-- Message READ derivation (0080 outbound branch, 0102 purpose reads) and NX-026 commitment registration accept a staff reply
-- as a delivered enterprise message (function bodies copied from their latest version with only these additions).

insert into control.nexloop_provider_namespaces(namespace,trust,skew_seconds,published_by) values('nexloop.staff','server',0,'0153 NX-028 staff replies');

-- Provenance of each staff reply (owner-only, append-only).
create table runtime.nexloop_staff_replies (
 tenant_id text not null,world text not null,message_id text not null check(message_id~'^[a-f0-9]{64}$'),conversation_id text not null,
 takeover_id uuid not null,staff_principal text not null,trigger_message_id text not null,request_intent text not null,
 created_at timestamptz not null default clock_timestamp(),primary key(tenant_id,world,message_id),unique(tenant_id,world,request_intent)
);
alter table runtime.nexloop_staff_replies owner to nexloop_owner;
alter table runtime.nexloop_staff_replies enable row level security;
alter table runtime.nexloop_staff_replies force row level security;
create policy tenant_boundary on runtime.nexloop_staff_replies to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
create trigger nx028_append_only before update or delete on runtime.nexloop_staff_replies for each row execute function control.nexloop_nx022_append_only();
revoke all on runtime.nexloop_staff_replies from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;

-- A staff Message is accepted when its stream entry, provenance and the author's takeover all agree.
create function runtime.nexloop_staff_message_accepted(p_tenant text,p_world text,p_message text) returns boolean
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v boolean;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select exists(select 1 from runtime.nexloop_staff_replies r join runtime.nexloop_conversation_messages cm on cm.tenant_id=r.tenant_id and cm.world=r.world and cm.message_id=r.message_id
   join control.nexloop_takeovers x on x.takeover_id=r.takeover_id and x.tenant_id=r.tenant_id and x.world=r.world
  where r.tenant_id=p_tenant and r.world=p_world and r.message_id=p_message and cm.conversation_id=r.conversation_id
   and cm.record->>'direction'='outbound' and cm.record->>'sender_kind'='human_takeover' and cm.record->>'actor'=r.staff_principal and x.taken_by=r.staff_principal
   and (cm.record->>'accepted_at')::timestamptz>=x.started_at and (cm.record->>'accepted_at')::timestamptz<coalesce(x.ended_at,x.expires_at)) into v;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
-- Whether a staff reply bound to this inbound message exists (the stream is the record).
create function runtime.nexloop_staff_replied(p_tenant text,p_world text,p_message text) returns boolean
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select exists(select 1 from runtime.nexloop_conversation_messages cm where cm.tenant_id=p_tenant and cm.world=p_world
  and cm.record->>'direction'='outbound' and cm.record->>'sender_kind'='human_takeover' and cm.record->>'trigger_message_id'=p_message)
$$;
-- Any reply to this inbound message: Agent / fallback in flight or channel-accepted (outbound ledger), or a staff reply.
create function runtime.nexloop_inbound_answered(p_tenant text,p_world text,p_message text) returns boolean
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select exists(select 1 from runtime.nexloop_outbound_messages x where x.tenant_id=p_tenant and x.world=p_world and x.trigger_message_id=p_message
   and x.delivery_state in ('dispatching','provider_accepted','delivered','unknown')) or runtime.nexloop_staff_replied(p_tenant,p_world,p_message)
$$;
-- Native WebChat conversation: every inbound message came in through the WebChat namespaces (NX-051 provider facts).
create function runtime.nexloop_conversation_is_webchat(p_tenant text,p_world text,p_conversation text) returns boolean
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select not exists(select 1 from runtime.nexloop_conversation_messages cm left join runtime.nexloop_message_provider_facts f
   on f.tenant_id=cm.tenant_id and f.world=cm.world and f.message_id=cm.message_id
  where cm.tenant_id=p_tenant and cm.world=p_world and cm.conversation_id=p_conversation and not (cm.record ? 'direction')
   and coalesce(f.provider_namespace,'') not in ('native.webchat','nexloop.api'))
$$;
do $owners$
declare f text;
begin
 foreach f in array array['runtime.nexloop_staff_message_accepted(text,text,text)','runtime.nexloop_staff_replied(text,text,text)',
   'runtime.nexloop_inbound_answered(text,text,text)','runtime.nexloop_conversation_is_webchat(text,text,text)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';
  execute 'revoke all on function '||f||' from public';
 end loop;
end $owners$;

-- The governed human Action (registry handler).
create function runtime.nexloop_governed_staff_reply(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare cv runtime.nexloop_conversations;x control.nexloop_takeovers;m runtime.nexloop_conversation_messages;v_seq bigint;v_id text;v_at timestamptz:=clock_timestamp();
 v_record jsonb;v_text text:=body->>'text';v_restricted boolean;
begin
 if body->>'operation' is distinct from 'send_staff_reply' or coalesce(body->>'conversation_id','')!~'^[a-f0-9]{64}$' or coalesce(body->>'reply_to','')!~'^[a-f0-9]{64}$'
  or jsonb_typeof(body->'text') is distinct from 'string' or length(v_text) not between 1 and 8192 then raise exception 'staff reply invalid' using errcode='22023';end if;
 -- Replay of the same governed request: the same Message, nothing new.
 if exists(select 1 from runtime.nexloop_staff_replies r where r.tenant_id=p_tenant and r.world=p_world and r.request_intent=p_intent) then
  select cm.record into v_record from runtime.nexloop_staff_replies r join runtime.nexloop_conversation_messages cm on cm.tenant_id=r.tenant_id and cm.world=r.world and cm.message_id=r.message_id
   where r.tenant_id=p_tenant and r.world=p_world and r.request_intent=p_intent;
  return jsonb_build_object('message',v_record,'created',false);
 end if;
 -- Same lock order as inbound acceptance and Agent materialization: the conversation row first.
 select * into cv from runtime.nexloop_conversations where tenant_id=p_tenant and world=p_world and conversation_id=body->>'conversation_id' for update;
 if not found then raise exception 'conversation unavailable' using errcode='22023';end if;
 select * into x from control.nexloop_takeovers t where t.tenant_id=p_tenant and t.world=p_world and t.ended_at is null and t.expires_at>v_at
  and ((t.scope_kind='conversation' and t.scope_ref=cv.conversation_id) or (t.scope_kind='consumer' and t.scope_ref=cv.consumer_id)) for share;
 if not found or x.taken_by is distinct from p_principal then raise exception 'only the author of the active takeover may reply' using errcode='42501';end if;
 if not runtime.nexloop_conversation_is_webchat(p_tenant,p_world,cv.conversation_id) then
  raise exception 'staff replies are native WebChat only (an external channel needs the effect ledger)' using errcode='NXC07';end if;
 select * into m from runtime.nexloop_conversation_messages where tenant_id=p_tenant and world=p_world and conversation_id=cv.conversation_id
  and message_id=body->>'reply_to' and not (record ? 'direction') and idempotency_key not like 'agent-%';
 if not found then raise exception 'reply_to is not an inbound message of this conversation' using errcode='NXC05';end if;
 v_restricted:=exists(select 1 from control.nexloop_contact_restrictions r where r.tenant_id=p_tenant and r.world=p_world and r.consumer_id=cv.consumer_id and r.active);
 if v_restricted then
  -- ADR-023 §2.6, the same rules and the same reply records as the Agent and fallback replies.
  if (m.record->>'accepted_at')::timestamptz+make_interval(secs=>control.nexloop_reply_window_seconds())<=v_at then
   raise exception 'contact restricted: reply window closed' using errcode='NXC05';end if;
  perform pg_advisory_xact_lock(hashtextextended(p_tenant||':'||p_world||':reply:'||m.message_id,0));
  if runtime.nexloop_inbound_answered(p_tenant,p_world,m.message_id) then raise exception 'inbound message already answered' using errcode='NXC05';end if;
 end if;
 v_seq:=cv.last_sequence+1;
 v_id:=encode(sha256(convert_to(jsonb_build_array(p_tenant,p_world,'Message','staff-message-'||p_intent)::text,'UTF8')),'hex');
 insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
  values(p_tenant,p_world,'Message',v_id,1,jsonb_build_object('conversation_id',cv.conversation_id,'sequence',v_seq,'actor',p_principal,'body',v_text,'accepted_at',v_at::text),
   'nexloop-action','staff-message-'||p_intent,v_at,v_at);
 v_record:=jsonb_build_object('id',v_id,'conversation_id',cv.conversation_id,'sequence',v_seq,'actor',p_principal,'body',v_text,'accepted_at',v_at::text,
  'status','accepted','direction','outbound','sender_kind','human_takeover','trigger_message_id',m.message_id);
 -- No relay inbox/outbox row: the relay never plans for a person's own words.
 insert into runtime.nexloop_conversation_messages values(p_tenant,p_world,cv.conversation_id,v_seq,v_id,'staff-'||p_intent,
  encode(sha256(convert_to(jsonb_build_object('body',v_text)::text,'UTF8')),'hex'),v_record);
 update runtime.nexloop_conversations set last_sequence=v_seq where tenant_id=p_tenant and world=p_world and conversation_id=cv.conversation_id;
 insert into runtime.nexloop_staff_replies(tenant_id,world,message_id,conversation_id,takeover_id,staff_principal,trigger_message_id,request_intent)
  values(p_tenant,p_world,v_id,cv.conversation_id,x.takeover_id,p_principal,m.message_id,p_intent);
 -- ADR-023 §2.7: the bound message is answered.
 delete from runtime.nexloop_work_feed where tenant_id=p_tenant and world=p_world and feed='reply-due' and item_key='reply:'||m.message_id;
 return jsonb_build_object('message',v_record,'created',true);
end $$;
alter function runtime.nexloop_governed_staff_reply(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function runtime.nexloop_governed_staff_reply(text,text,text,text,text,jsonb) from public;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('message.staff_send','send_staff_reply','human','runtime.nexloop_governed_staff_reply(text,text,text,text,text,jsonb)','NX-028');

-- Bodies replaced in place (latest version + the NX-028 additions marked in each) -------------------------------------
create or replace function authz.nexloop_assert_purpose_message_read(p_digest text,p_world text,p_claims jsonb) returns jsonb
language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;tenant text;principal text;basis jsonb:=p_claims->'derivation_basis';v_purpose text;rule jsonb;held jsonb;
 v_kind text;v_id text;v_field text;v_conversation text;m ontology.objects;cm runtime.nexloop_conversation_messages;j runtime.jobs;cl ontology.nexloop_claims;
 v_targets text[];
begin
 v_kind:=substring(p_claims->>'resource_id' from '^eios:(?:object|property):(Message|Conversation)/[a-f0-9]{64}(?:/[a-z_]{1,64})?$');
 v_id:=substring(p_claims->>'resource_id' from '^eios:(?:object|property):(?:Message|Conversation)/([a-f0-9]{64})(?:/[a-z_]{1,64})?$');
 v_field:=substring(p_claims->>'resource_id' from '^eios:property:(?:Message|Conversation)/[a-f0-9]{64}/([a-z_]{1,64})$');
 perform authz.nexloop_lock_credential(p_digest,p_world,p_claims->>'resource_id');
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 tenant:=ident->'binding'->>'tenant_id';principal:=ident->'binding'->>'subject_principal_id';v_purpose:=basis->>'purpose';
 if p_claims->>'derivation' is distinct from 'purpose-message-v1' or v_kind is null or v_id is null
  or (p_claims->>'resource_id' like 'eios:property:%') is distinct from (v_field is not null)
  or p_claims->>'tenant_id' is distinct from tenant or p_claims->>'principal_id' is distinct from principal
  or p_claims->>'credential_id' is distinct from ident->'binding'->>'credential_id'
  or p_claims->>'world' is distinct from p_world or p_world is distinct from 'real'
  or p_claims->>'directory_hash' is distinct from ident->>'directory_hash'
  or p_claims->>'resource_id' is distinct from p_claims->>'target_resource' or p_claims->>'operation' is distinct from 'read'
  or p_claims->>'expires_at' is null or (p_claims->>'expires_at')::timestamptz<=clock_timestamp()
  or p_claims->'facts' is distinct from '[]'::jsonb
  or ident->'binding'->>'subject_kind' is distinct from 'service' or (ident->'run_context' is not null and ident->'run_context'<>'null'::jsonb)
  or jsonb_typeof(basis) is distinct from 'object' or v_purpose is null or v_purpose not in ('claim_extraction','claim_matching')
  or (v_purpose='claim_extraction' and (basis->>'job_id' is null or coalesce(basis->>'fence','') !~ '^[0-9]{1,18}$'))
  or (v_purpose='claim_matching' and coalesce(basis->>'claim_id','') !~ '^[0-9a-f]{64}$') then
  raise exception 'purpose message read invalid' using errcode='42501';end if;
 -- Message metadata of the rule; Conversation object (both purposes) and its two
 -- properties only while extracting. Nothing else is ever derived.
 if (v_kind='Message' and v_field is not null and v_field not in ('accepted_at','actor','body','conversation_id','sequence'))
  or (v_kind='Conversation' and v_field is not null and (v_field not in ('consumer_id','owner_principal') or v_purpose<>'claim_extraction')) then
  raise exception 'purpose message field unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',tenant,true);
 -- This principal's configured grants on the object or any of its fields win.
 v_targets:=case v_kind when 'Message' then array['eios:object:Message/'||v_id,'eios:property:Message/'||v_id||'/accepted_at','eios:property:Message/'||v_id||'/actor',
   'eios:property:Message/'||v_id||'/body','eios:property:Message/'||v_id||'/conversation_id','eios:property:Message/'||v_id||'/sequence']
  else array['eios:object:Conversation/'||v_id,'eios:property:Conversation/'||v_id||'/consumer_id','eios:property:Conversation/'||v_id||'/owner_principal'] end;
 if exists(select 1 from authz.nexloop_authority_facts where tenant_id=tenant and fact_kind='grants' and entity_key[1]=principal and entity_key[2]=any(v_targets)) then
  raise exception 'purpose message read superseded' using errcode='42501';end if;
 select payload into rule from authz.nexloop_authority_facts where tenant_id=tenant and fact_kind='message_purpose_rule' and entity_key=array[principal,v_purpose] for share;
 if not found or encode(sha256(convert_to(rule::text,'UTF8')),'hex') is distinct from basis->>'rule_hash'
  or rule->'active' is distinct from 'true'::jsonb or (rule->>'valid_until')::timestamptz<=clock_timestamp()
  or (p_claims->>'expires_at')::timestamptz>(rule->>'valid_until')::timestamptz
  or (v_kind='Message' and v_field is not null and not (rule->'fields' ? v_field))
  or (v_kind='Conversation' and not (rule->'fields' ? 'conversation_id')) then
  raise exception 'purpose message read rule unavailable' using errcode='42501';end if;
 -- The principal still holds EXECUTE on the purpose Action (configured, revocable).
 select payload into held from authz.nexloop_authority_facts where tenant_id=tenant and fact_kind='grants'
  and entity_key=array[principal,authz.nexloop_message_purpose_action(v_purpose)] for share;
 if not found or jsonb_typeof(held->'grants') is distinct from 'array' or jsonb_array_length(held->'grants')=0 then
  raise exception 'purpose message read action revoked' using errcode='42501';end if;
 if v_kind='Message' then
  select * into m from ontology.objects where tenant_id=tenant and world=p_world and type_name='Message' and object_id=v_id for share;
  if not found then raise exception 'purpose message unavailable' using errcode='42501';end if;
  if v_field is not null and not (m.properties ? v_field) then raise exception 'purpose message field unavailable' using errcode='42501';end if;
  -- Accepted inbound (0046 outbox) or a materialized channel-accepted outbound reply (0079).
  perform 1 from runtime.nexloop_message_outbox where tenant_id=tenant and world=p_world and message_id=v_id for share;
  if not found then
   perform 1 from runtime.nexloop_outbound_messages where tenant_id=tenant and world=p_world and message_id=v_id for share;
   -- NX-028 (ruling B): a staff reply written during the author's own takeover is accepted too.
   if not found and not runtime.nexloop_staff_message_accepted(tenant,p_world,v_id) then raise exception 'purpose message not accepted' using errcode='42501';end if;
  end if;
  select * into cm from runtime.nexloop_conversation_messages where tenant_id=tenant and world=p_world and message_id=v_id for share;
  if not found then raise exception 'purpose message conversation changed' using errcode='42501';end if;
  v_conversation:=cm.conversation_id;
 else
  perform 1 from ontology.objects where tenant_id=tenant and world=p_world and type_name='Conversation' and object_id=v_id for share;
  if not found then raise exception 'purpose conversation unavailable' using errcode='42501';end if;
  v_conversation:=v_id;
 end if;
 perform 1 from runtime.nexloop_conversations where tenant_id=tenant and world=p_world and conversation_id=v_conversation for share;
 if not found or basis->>'conversation_id' is distinct from v_conversation then raise exception 'purpose message conversation changed' using errcode='42501';end if;
 if v_purpose='claim_extraction' then
  -- The extraction task this exact credential leases now, referencing this Conversation/Message.
  select * into j from runtime.jobs where tenant_id=tenant and job_id=basis->>'job_id' for share;
  if not found or j.world is distinct from p_world or j.queue is distinct from 'claim-extraction' or j.capability_name is distinct from 'NexLoop.event'
   or j.status is distinct from 'running' or j.lease_until is null or j.lease_until<=clock_timestamp() or j.lease_credential is distinct from p_digest
   or j.fencing_token is distinct from (basis->>'fence')::bigint
   or j.normalized_input->>'conversation_id' is distinct from v_conversation
   or (v_kind='Message' and not coalesce(j.normalized_input->'message_ids' ? v_id,false))
   or (p_claims->>'expires_at')::timestamptz>j.lease_until then
   raise exception 'purpose extraction task unavailable' using errcode='42501';end if;
 else
  -- A Claim of this Conversation still to be matched, citing this Message as its evidence.
  select * into cl from ontology.nexloop_claims where tenant_id=tenant and world=p_world and claim_id=basis->>'claim_id' for share;
  if not found or cl.conversation_id is distinct from v_conversation or (v_kind='Message' and cl.source_message_id is distinct from v_id)
   or not authz.nexloop_claim_match_pending(cl.tenant_id,cl.world,cl.claim_id,cl.resolution_state) then
   raise exception 'purpose matching claim unavailable' using errcode='42501';end if;
 end if;
 if (p_claims->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'purpose message read expired' using errcode='42501';end if;
 return ident->'binding';
end $$;

-- Nested Consumer READ (kept from 0080 unchanged): the signed Consumer envelope is checked by the innermost configured-grant
-- layer authz.nexloop_assert_read_authority_before_message_read_v0072, like 0077/0080/0084/0086 do. Deliberately not the public
-- entry: the outer layers are derivations (0077 message, 0084 property, 0123 workbench member) and the memo, so the public
-- entry could accept a derived Consumer READ here (derivations must not chain). Today no outer layer refuses, so nothing is
-- skipped. NX-029 (refuse reads while erasing) must cover this layer too, not only wrap the public entry: these five nested
-- call sites never pass through an outer wrapper.
create or replace function authz.nexloop_assert_derived_message_read_actor_body_v0080(p_digest text,p_world text,p_claims jsonb) returns jsonb
language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;tenant text;principal text;basis jsonb:=p_claims->'derivation_basis';message text;rule jsonb;
 m ontology.objects;cm runtime.nexloop_conversation_messages;conv runtime.nexloop_conversations;o runtime.nexloop_outbound_messages;
 consumer_env jsonb;consumer jsonb;k bytea;
begin
 message:=substring(p_claims->>'resource_id' from '^eios:(?:object|property):Message/([a-f0-9]{64})(?:/(?:actor|body))?$');
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 tenant:=ident->'binding'->>'tenant_id';principal:=ident->'binding'->>'subject_principal_id';
 perform set_config('eios.tenant_id',tenant,true);
 -- Inbound (or anything that is not a materialized outbound reply): unchanged 0077 rules.
 if message is null or (not exists(select 1 from runtime.nexloop_outbound_messages x where x.tenant_id=tenant and x.world=p_world and x.message_id=message)
   and not runtime.nexloop_staff_message_accepted(tenant,p_world,message)) then
  return authz.nexloop_assert_derived_message_read_inbound_v0077(p_digest,p_world,p_claims);
 end if;
 perform authz.nexloop_lock_credential(p_digest,p_world,p_claims->>'resource_id');
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if p_claims->>'derivation' is distinct from 'accepted-message-v1'
  or p_claims->>'tenant_id' is distinct from tenant or p_claims->>'principal_id' is distinct from principal
  or p_claims->>'credential_id' is distinct from ident->'binding'->>'credential_id'
  or p_claims->>'world' is distinct from p_world or p_world is distinct from 'real'
  or p_claims->>'directory_hash' is distinct from ident->>'directory_hash'
  or p_claims->>'resource_id' is distinct from p_claims->>'target_resource' or p_claims->>'operation' is distinct from 'read'
  or p_claims->>'expires_at' is null or (p_claims->>'expires_at')::timestamptz<=clock_timestamp()
  or p_claims->'facts' is distinct from '[]'::jsonb
  or ident->'binding'->>'subject_kind' is distinct from 'service' or (ident->'run_context' is not null and ident->'run_context'<>'null'::jsonb)
  or jsonb_typeof(basis) is distinct from 'object' then raise exception 'derived message read invalid' using errcode='42501';end if;
 if exists(select 1 from authz.nexloop_authority_facts where tenant_id=tenant and fact_kind='grants' and entity_key in
   (array[principal,'eios:object:Message/'||message],array[principal,'eios:property:Message/'||message||'/actor'],array[principal,'eios:property:Message/'||message||'/body'])) then
  raise exception 'derived message read superseded' using errcode='42501';end if;
 select payload into rule from authz.nexloop_authority_facts where tenant_id=tenant and fact_kind='message_read_rule' and entity_key=array[principal] for share;
 if not found or encode(sha256(convert_to(rule::text,'UTF8')),'hex') is distinct from basis->>'rule_hash'
  or rule->'active' is distinct from 'true'::jsonb or (rule->>'valid_until')::timestamptz<=clock_timestamp()
  or (p_claims->>'expires_at')::timestamptz>(rule->>'valid_until')::timestamptz then raise exception 'derived message read rule unavailable' using errcode='42501';end if;
 select * into m from ontology.objects where tenant_id=tenant and world=p_world and type_name='Message' and object_id=message for share;
 if not found then raise exception 'derived message unavailable' using errcode='42501';end if;
 -- Outbound acceptance: materialized, channel-accepted, Agent sender, same conversation.
 select * into o from runtime.nexloop_outbound_messages where tenant_id=tenant and world=p_world and message_id=message for share;
 select * into cm from runtime.nexloop_conversation_messages where tenant_id=tenant and world=p_world and message_id=message for share;
 if o.intent_id is null then
  -- NX-028 (ruling B): a staff reply of its author's own takeover; actor = the staff principal of the stream record.
  if cm.message_id is null or not runtime.nexloop_staff_message_accepted(tenant,p_world,message) or m.properties->>'actor' is distinct from cm.record->>'actor' then
   raise exception 'derived message not accepted' using errcode='42501';end if;
  if cm.conversation_id is distinct from basis->>'conversation_id' then raise exception 'derived message conversation changed' using errcode='42501';end if;
 else
 if o.delivery_state not in ('provider_accepted','delivered') or m.properties->>'actor' is distinct from o.sender_principal then
  raise exception 'derived message not accepted' using errcode='42501';end if;
 if cm.message_id is null or cm.conversation_id is distinct from basis->>'conversation_id' or o.conversation_id is distinct from cm.conversation_id
  or cm.sequence is distinct from o.sequence then raise exception 'derived message conversation changed' using errcode='42501';end if;
 end if;
 select * into conv from runtime.nexloop_conversations where tenant_id=tenant and world=p_world and conversation_id=cm.conversation_id for share;
 if not found or conv.consumer_id is distinct from basis->>'consumer_id' then raise exception 'derived message consumer changed' using errcode='42501';end if;
 consumer_env:=basis->'consumer_read';consumer:=(consumer_env->>'text')::jsonb;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=consumer->>'key_id' and active;
 if not found or consumer->>'protocol' is distinct from 'nexloop-object-read-v1'
  or consumer_env->>'signature' is distinct from encode(extensions.hmac(convert_to('nexloop-object-read-v1:'||(consumer_env->>'text'),'UTF8'),k,'sha256'),'hex')
  or consumer->>'type_name' is distinct from 'Consumer' or consumer->>'object_id' is distinct from conv.consumer_id
  or consumer->>'resource_id' is distinct from 'eios:object:Consumer/'||conv.consumer_id
  or (p_claims->>'expires_at')::timestamptz>(consumer->>'expires_at')::timestamptz then raise exception 'derived message consumer READ required' using errcode='42501';end if;
 -- Configured-grant READ of the Consumer only (see the note above the function; NX-029 must cover this layer).
 perform authz.nexloop_assert_read_authority_before_message_read_v0072(p_digest,p_world,consumer);
 if (p_claims->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'derived message read expired' using errcode='42501';end if;
 return ident->'binding';
end $$;

create or replace function runtime.nexloop_message_provider_default() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v_event uuid;v_ref text;v_at timestamptz;
begin
 perform set_config('eios.tenant_id',new.tenant_id,true);
 if new.record->>'direction'='outbound' and new.record->>'sender_kind'='human_takeover' then
  -- NX-028 (ruling B): a staff reply written here is its own channel event (native WebChat only).
  perform runtime.nexloop_record_provider_facts(new.tenant_id,new.world,new.message_id,'nexloop.staff',null,null,null);
  if new.record->>'trigger_message_id' is not null then
   perform runtime.nexloop_record_reply_link(new.tenant_id,new.world,new.message_id,'message',new.record->>'trigger_message_id','server');
  end if;
 elsif new.record->>'direction'='outbound' then
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

create or replace function runtime.nexloop_commitment_prepare_claim(p_tenant text,p_world text,p_claim text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare cl ontology.nexloop_claims;om runtime.nexloop_outbound_messages;cm runtime.nexloop_conversation_messages;r runtime.nexloop_commitments;
 x runtime.nexloop_commitments;vt jsonb;v_due timestamptz;v_precision text;v_promised timestamptz;v_goal text;v_dedupe text;v_intent text;v_id text;
 v_props jsonb;v_supersedes text;v_existing jsonb;v_staff_takeover text;v_staff_principal text;
begin
 select * into cl from ontology.nexloop_claims where tenant_id=p_tenant and world=p_world and claim_id=p_claim for update;
 if not found or cl.epistemic_kind<>'commitment' then raise exception 'commitment claim unavailable' using errcode='42501';end if;
 select * into r from runtime.nexloop_commitments c where c.tenant_id=p_tenant and c.world=p_world and c.claim_id=p_claim;
 if found then
  if r.registered_at is not null then return jsonb_build_object('action','registered','commitment_id',r.commitment_id);end if;
  return jsonb_build_object('action','create','commitment_id',r.commitment_id,'intent_id',r.intent_id,'properties',r.properties);
 end if;
 if exists(select 1 from runtime.nexloop_commitment_events e where e.tenant_id=p_tenant and e.world=p_world and e.subject_ref='claim:'||p_claim
   and e.kind in ('reaffirmed','skipped')) then return jsonb_build_object('action','done');end if;
 -- Only an enterprise's asserted or conditional promise from a delivered outbound Message is a commitment.
 if cl.speaker<>'agent' or cl.polarity<>'affirmed' or cl.modality not in ('asserted','conditional') or cl.resolution_state not in ('unresolved','needs_resolution') then
  insert into runtime.nexloop_commitment_events(tenant_id,world,subject_ref,kind,detail)
   values(p_tenant,p_world,'claim:'||p_claim,'skipped',jsonb_build_object('reason','not_a_commitment','speaker',cl.speaker,'polarity',cl.polarity,
    'modality',cl.modality,'resolution_state',cl.resolution_state));
  return jsonb_build_object('action','skipped','reason','not_a_commitment');
 end if;
 select * into om from runtime.nexloop_outbound_messages o where o.tenant_id=p_tenant and o.world=p_world and o.message_id=cl.source_message_id
  and o.delivery_state in ('provider_accepted','delivered');
 select * into cm from runtime.nexloop_conversation_messages m where m.tenant_id=p_tenant and m.world=p_world and m.message_id=cl.source_message_id
  and m.record->>'direction'='outbound';
 -- NX-028 (ruling B): a staff reply written during its author's takeover is a delivered enterprise message too.
 if om.intent_id is null and cm.message_id is not null and cm.record->>'sender_kind'='human_takeover' and runtime.nexloop_staff_message_accepted(p_tenant,p_world,cm.message_id) then
  select sr.takeover_id::text,sr.staff_principal into v_staff_takeover,v_staff_principal from runtime.nexloop_staff_replies sr
   where sr.tenant_id=p_tenant and sr.world=p_world and sr.message_id=cm.message_id;
 elsif om.intent_id is null or cm.message_id is null then
  insert into runtime.nexloop_commitment_events(tenant_id,world,subject_ref,kind,detail)
   values(p_tenant,p_world,'claim:'||p_claim,'skipped',jsonb_build_object('reason','source_not_delivered_outbound'));
  return jsonb_build_object('action','skipped','reason','source_not_delivered_outbound');
 end if;
 vt:=cl.valid_time;
 if vt->>'kind' in ('point','deadline','interval') and vt->>'status'='resolved' then
  v_due:=coalesce((case when vt->>'kind'='point' then vt->>'start' else vt->>'end' end)::timestamptz,(vt->>'start')::timestamptz);v_precision:='exact';
 elsif vt->>'kind' in ('point','deadline','interval') and vt->>'status'='ambiguous' then
  v_due:=coalesce((vt->'latest_bound_window'->>1)::timestamptz,(vt->>'end')::timestamptz);v_precision:='latest_bound';
 end if;
 if v_due is null then v_precision:='unspecified';end if;
 v_promised:=(cm.record->>'accepted_at')::timestamptz;
 select 'goal:'||b.goal_id||'@'||b.goal_version into v_goal from control.nexloop_run_goal_bindings b
  where b.tenant_id=p_tenant and b.world=p_world and b.run_id=om.run_id order by b.bound_at desc,b.goal_id limit 1;
 -- The same promise again in a later delivered message: reaffirmed, or a new commitment when the due date differs.
 select c.* into x from runtime.nexloop_commitments c join ontology.objects o on o.tenant_id=c.tenant_id and o.world=c.world
   and o.type_name='Commitment' and o.object_id=c.commitment_id
  where c.tenant_id=p_tenant and c.world=p_world and c.consumer_id=cl.consumer_id and c.correlation_key=cl.correlation_key
   and c.message_id<>cl.source_message_id and o.properties->>'status' in ('conditional','open','in_progress')
  order by c.prepared_at desc limit 1;
 if found then
  if (x.properties->>'due_at')::timestamptz is not distinct from v_due then
   insert into runtime.nexloop_commitment_events(tenant_id,world,subject_ref,kind,ref,detail)
    values(p_tenant,p_world,'commitment:'||x.commitment_id,'reaffirmed','claim:'||p_claim,jsonb_build_object('message_id',cl.source_message_id));
   insert into runtime.nexloop_commitment_events(tenant_id,world,subject_ref,kind,ref)
    values(p_tenant,p_world,'claim:'||p_claim,'reaffirmed','commitment:'||x.commitment_id);
   update ontology.nexloop_claims set resolution_state='resolved' where tenant_id=p_tenant and world=p_world and claim_id=p_claim;
   return jsonb_build_object('action','reaffirmed','commitment_id',x.commitment_id);
  end if;
  v_supersedes:=x.commitment_id;
 end if;
 v_dedupe:=encode(sha256(convert_to(p_tenant||'|'||p_world||'|'||cl.source_message_id||'|'||cl.correlation_key,'UTF8')),'hex');
 select * into r from runtime.nexloop_commitments c where c.tenant_id=p_tenant and c.world=p_world and c.dedupe_key=v_dedupe;
 if found then
  -- Another Claim of the same message and promise (re-extraction): the same commitment.
  if r.registered_at is not null then
   update ontology.nexloop_claims set resolution_state='resolved' where tenant_id=p_tenant and world=p_world and claim_id=p_claim and resolution_state in ('unresolved','needs_resolution');
   insert into runtime.nexloop_commitment_events(tenant_id,world,subject_ref,kind,ref)
    values(p_tenant,p_world,'claim:'||p_claim,'reaffirmed','commitment:'||r.commitment_id);
   return jsonb_build_object('action','reaffirmed','commitment_id',r.commitment_id);
  end if;
  return jsonb_build_object('action','create','commitment_id',r.commitment_id,'intent_id',r.intent_id,'properties',r.properties);
 end if;
 v_intent:='commitment-'||v_dedupe;
 v_id:=encode(sha256(convert_to(jsonb_build_array(p_tenant,p_world,'Commitment',v_intent)::text,'UTF8')),'hex');
 v_props:=jsonb_build_object('made_to',cl.consumer_id,
  'made_by',case when v_staff_takeover is not null then jsonb_build_object('sender_kind','human_takeover','sender_principal',v_staff_principal,'takeover_id',v_staff_takeover)
   else jsonb_build_object('sender_kind',om.sender_kind,'sender_principal',om.sender_principal,'role_ref',om.role_ref,'run_id',om.run_id::text,'intent_id',om.intent_id::text) end,
  'content_ref',jsonb_build_object('claim_id',cl.claim_id,'message_id',cl.source_message_id,'span',jsonb_build_array(cl.span_start,cl.span_end),'content_hash',cl.source_content_hash),
  'promised_at',runtime.nexloop_commitment_ts(v_promised),'due_at',case when v_due is null then null else runtime.nexloop_commitment_ts(v_due) end,
  'due_precision',v_precision,'condition',case when cl.modality='conditional' then cl.condition_text end,
  'status',case when cl.modality='conditional' then 'conditional' else 'open' end,'late',false,
  'fulfillment_basis','undetermined','fulfillment_evidence','[]'::jsonb,'related_goal_ref',v_goal,'supersedes',v_supersedes);
 insert into runtime.nexloop_commitments(tenant_id,world,commitment_id,dedupe_key,origin,claim_id,message_id,consumer_id,correlation_key,supersedes,intent_id,properties)
  values(p_tenant,p_world,v_id,v_dedupe,'claim',p_claim,cl.source_message_id,cl.consumer_id,cl.correlation_key,v_supersedes,v_intent,v_props);
 return jsonb_build_object('action','create','commitment_id',v_id,'intent_id',v_intent,'properties',v_props);
end $$;

create or replace function control.nexloop_contact_assert_intent(p_tenant text,p_world text,p_intent uuid) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);i runtime.nexloop_effect_intents;v_conversation text;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into i from runtime.nexloop_effect_intents where intent_id=p_intent and tenant_id=p_tenant and world=p_world;
 if found then
  select o.conversation_id into v_conversation from runtime.nexloop_outbound_messages o where o.tenant_id=p_tenant and o.world=p_world and o.intent_id=p_intent;
  -- Consumer scope: no Agent effect at all; conversation scope: no Agent reply in that conversation.
  if exists(select 1 from control.nexloop_takeovers x where x.tenant_id=p_tenant and x.world=p_world and x.ended_at is null and x.expires_at>clock_timestamp()
    and ((x.scope_kind='consumer' and x.scope_ref=i.consumer_id) or (x.scope_kind='conversation' and x.scope_ref=v_conversation))) then
   raise exception 'taken over by a person: the Agent does not reply' using errcode='NXC06';end if;
  -- ADR-023 §2.6 one reply per inbound message (restricted Consumer or fallback reply): a staff reply counts like any reply.
  if exists(select 1 from runtime.nexloop_outbound_messages o where o.tenant_id=p_tenant and o.world=p_world and o.intent_id=p_intent
     and runtime.nexloop_staff_replied(p_tenant,p_world,o.trigger_message_id)
     and (authz.nexloop_is_fallback_run(o.run_id) or exists(select 1 from control.nexloop_contact_restrictions r where r.tenant_id=p_tenant and r.world=p_world
      and r.consumer_id=i.consumer_id and r.active))) then
   raise exception 'inbound message already answered' using errcode='NXC05';end if;
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 perform control.nexloop_contact_assert_intent_before_takeover_v0112(p_tenant,p_world,p_intent);
end $$;
