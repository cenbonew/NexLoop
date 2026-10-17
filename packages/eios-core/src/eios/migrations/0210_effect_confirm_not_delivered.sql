-- The owner confirms an unknown effect did not happen (temporary number 0210; design docs/design/confirm-effect-not-delivered.md,
-- owner 2026-10-18: all five points as recommended). AGENTS: an unknown external result is queried and reconciled first, never
-- blindly retried; this Action sends nothing, creates no intent and never touches an Action claim.
-- 1. runtime.nexloop_effect_determinations: append-only, one per intent, kind confirmed_not_delivered (Q1: the intent keeps the
--    existing terminal state failed; the record says who decided and on which not_found observation). Q4: no new effect event
--    kind; the record is the ledger of the decision.
-- 2. runtime.nexloop_effect_determination_check(tenant, world, intent): one code for whether the owner may decide now, used by
--    the Action and by the owner's read-only precheck: eligible, intent_unknown, already_determined, not_unknown, effect_busy,
--    no_attempt, no_not_found_evidence, wait_period. Evidence: the latest observation of the current attempt is not_found, after
--    its dispatch, and no accepted/fulfilled observation exists. Q2 wait: 24 h after the first unknown event (the dispatch time
--    when none) and 10 min after that not_found observation.
-- 3. Governed human Action service.confirm_not_delivered / confirm_effect_not_delivered (0124 registry, workbench owner only):
--    reason 1..500, confirmation reference, Q5 provider_retention_checked must be true. In one transaction: outbox → failed
--    (lease cleared), intent unknown → failed (0079 trigger: the outbound reply unknown → failed), the determination row. The
--    attempt row keeps its last state (its 0042 states have no failed; the intent is the terminal fact).
--    Q3 reply guarantee: owner_follow_up (default) escalates the reply's inbound message with reason owner_follow_up (the reply
--    worker settles it, no fallback); fallback_reply leaves the message to the reply guarantee for its one fallback, and is
--    refused (fallback_unavailable) when the message is already escalated or answered.
--    A refusal is errcode 22023 with the fixed text 'effect determination: <code>' (the workbench answers 409 with the code).
-- 4. Workbench observe verb effect_determination (owner only): the check, its times and the determination (never its reason).

-- 1. Determinations ----------------------------------------------------------------------------------------------------------
create table runtime.nexloop_effect_determinations (
 determination_id uuid primary key default gen_random_uuid(),
 tenant_id text not null,world text not null,intent_id uuid not null,
 kind text not null check(kind='confirmed_not_delivered'),
 observation_id uuid not null references runtime.nexloop_effect_observations(observation_id),
 confirmation_ref text not null check(confirmation_ref~'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),
 reason text not null check(length(reason) between 1 and 500),
 provider_retention_checked boolean not null check(provider_retention_checked),
 reply_followup text not null check(reply_followup in ('owner_follow_up','fallback_reply','none')),
 decided_by text not null,request_intent text not null,decided_at timestamptz not null default clock_timestamp(),
 unique(tenant_id,world,intent_id),
 foreign key(intent_id,tenant_id,world) references runtime.nexloop_effect_intents(intent_id,tenant_id,world)
);
alter table runtime.nexloop_effect_determinations owner to nexloop_owner;
alter table runtime.nexloop_effect_determinations enable row level security;
alter table runtime.nexloop_effect_determinations force row level security;
create policy effect_determination_tenant on runtime.nexloop_effect_determinations to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
create trigger nx_effect_determination_append_only before update or delete on runtime.nexloop_effect_determinations
 for each row execute function control.nexloop_nx022_append_only();
revoke all on runtime.nexloop_effect_determinations from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;

-- 2. The check ---------------------------------------------------------------------------------------------------------------
create function runtime.nexloop_effect_determination_check(p_tenant text,p_world text,p_intent uuid) returns jsonb
 language plpgsql stable set search_path=pg_catalog,pg_temp as $$
declare i runtime.nexloop_effect_intents;o runtime.nexloop_effect_outbox;a runtime.nexloop_effect_attempts;nf runtime.nexloop_effect_observations;
 latest runtime.nexloop_effect_observations;d runtime.nexloop_effect_determinations;v_first timestamptz;v_wait timestamptz;v_code text;
begin
 select * into i from runtime.nexloop_effect_intents where intent_id=p_intent and tenant_id=p_tenant and world=p_world;
 if not found then return jsonb_build_object('intent_id',p_intent,'code','intent_unknown','eligible',false);end if;
 select * into d from runtime.nexloop_effect_determinations where tenant_id=p_tenant and world=p_world and intent_id=p_intent;
 select * into o from runtime.nexloop_effect_outbox where intent_id=p_intent;
 select * into a from runtime.nexloop_effect_attempts where intent_id=p_intent and tenant_id=p_tenant and world=p_world;
 if a.intent_id is not null then
  select * into latest from runtime.nexloop_effect_observations x where x.intent_id=p_intent and x.attempt_revision=a.attempt_revision
   order by x.observed_at desc,x.observation_id desc limit 1;
  if latest.provider_state='not_found' and latest.observed_at>=a.dispatched_at
   and not exists(select 1 from runtime.nexloop_effect_observations x where x.intent_id=p_intent and x.provider_state in ('accepted','fulfilled')) then
   nf:=latest;end if;
 end if;
 v_first:=coalesce((select min(e.created_at) from runtime.nexloop_effect_events e where e.intent_id=p_intent and e.event_kind='unknown'),a.dispatched_at);
 v_wait:=greatest(v_first+interval '24 hours',nf.observed_at+interval '10 minutes');
 v_code:=case
  when d.determination_id is not null then 'already_determined'
  when i.state<>'unknown' then 'not_unknown'
  when o.lease_until is not null and o.lease_until>clock_timestamp() then 'effect_busy'
  when a.intent_id is null then 'no_attempt'
  when nf.observation_id is null then 'no_not_found_evidence'
  when v_wait>clock_timestamp() then 'wait_period'
  else 'eligible' end;
 return jsonb_build_object('intent_id',p_intent,'code',v_code,'eligible',v_code='eligible','state',i.state,'outbox_state',o.state,
  'first_unknown_at',v_first,'latest_observation',case when latest.observation_id is null then null else
   jsonb_build_object('provider_state',latest.provider_state,'observed_at',latest.observed_at) end,
  'not_found_observation_id',nf.observation_id,'wait_until',v_wait,
  'determination',case when d.determination_id is null then null else jsonb_build_object('determination_id',d.determination_id,'kind',d.kind,
   'confirmation_ref',d.confirmation_ref,'reply_followup',d.reply_followup,'decided_by',d.decided_by,'decided_at',d.decided_at) end);
end $$;
alter function runtime.nexloop_effect_determination_check(text,text,uuid) owner to nexloop_owner;
revoke all on function runtime.nexloop_effect_determination_check(text,text,uuid) from public;

-- 3. The owner's Action ------------------------------------------------------------------------------------------------------
create function runtime.nexloop_governed_confirm_not_delivered(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare v_intent uuid;chk jsonb;om runtime.nexloop_outbound_messages;cv runtime.nexloop_conversations;v_followup text;v_id uuid;v_state text;
begin
 if body->>'operation' is distinct from 'confirm_effect_not_delivered' or coalesce(body->>'intent_id','')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
  or coalesce(body->>'confirmation_ref','')!~'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$' or coalesce(length(body->>'reason'),0) not between 1 and 500
  or jsonb_typeof(body->'provider_retention_checked') is distinct from 'boolean'
  or coalesce(body->>'reply_followup','') not in ('owner_follow_up','fallback_reply') then
  raise exception 'effect determination: invalid' using errcode='22023';end if;
 if (body->>'provider_retention_checked')::boolean is not true then raise exception 'effect determination: retention_unconfirmed' using errcode='22023';end if;
 if control.nexloop_workbench_role(p_tenant,p_principal) is distinct from 'owner' then raise exception 'effect determination is owner only' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 v_intent:=(body->>'intent_id')::uuid;
 -- The intent, then its outbox, locked in the effect worker's order; the check reads them under these locks.
 perform 1 from runtime.nexloop_effect_intents where intent_id=v_intent and tenant_id=p_tenant and world=p_world for update;
 perform 1 from runtime.nexloop_effect_outbox where intent_id=v_intent for update;
 chk:=runtime.nexloop_effect_determination_check(p_tenant,p_world,v_intent);
 if chk->>'code'<>'eligible' then raise exception 'effect determination: %',chk->>'code' using errcode='22023';end if;
 select * into om from runtime.nexloop_outbound_messages where tenant_id=p_tenant and world=p_world and intent_id=v_intent;
 v_followup:=case when om.intent_id is null or om.trigger_message_id is null then 'none' else body->>'reply_followup' end;
 if v_followup='fallback_reply' and (exists(select 1 from control.nexloop_reply_escalations e where e.tenant_id=p_tenant and e.world=p_world and e.message_id=om.trigger_message_id)
   or exists(select 1 from runtime.nexloop_outbound_messages x where x.tenant_id=p_tenant and x.world=p_world and x.trigger_message_id=om.trigger_message_id
     and x.delivery_state in ('provider_accepted','delivered'))) then
  raise exception 'effect determination: fallback_unavailable' using errcode='22023';end if;
 update runtime.nexloop_effect_outbox set state='failed',lease_until=null,lease_credential=null,lease_principal=null where intent_id=v_intent;
 update runtime.nexloop_effect_intents set state='failed' where intent_id=v_intent;
 insert into runtime.nexloop_effect_determinations(tenant_id,world,intent_id,kind,observation_id,confirmation_ref,reason,provider_retention_checked,reply_followup,decided_by,request_intent)
  values(p_tenant,p_world,v_intent,'confirmed_not_delivered',(chk->>'not_found_observation_id')::uuid,body->>'confirmation_ref',body->>'reason',true,v_followup,p_principal,p_intent)
  returning determination_id into v_id;
 if v_followup='owner_follow_up' then
  select * into cv from runtime.nexloop_conversations where tenant_id=p_tenant and world=p_world and conversation_id=om.conversation_id;
  insert into control.nexloop_reply_escalations(tenant_id,world,message_id,conversation_id,consumer_id,reason,detail)
   values(p_tenant,p_world,om.trigger_message_id,om.conversation_id,cv.consumer_id,'owner_follow_up',
    jsonb_build_object('determination_id',v_id,'intent_id',v_intent)) on conflict do nothing;
 end if;
 select delivery_state into v_state from runtime.nexloop_outbound_messages where tenant_id=p_tenant and world=p_world and intent_id=v_intent;
 return jsonb_build_object('intent_id',v_intent,'determination_id',v_id,'kind','confirmed_not_delivered','intent_state','failed',
  'reply_followup',v_followup,'outbound_state',v_state);
end $$;
alter function runtime.nexloop_governed_confirm_not_delivered(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function runtime.nexloop_governed_confirm_not_delivered(text,text,text,text,text,jsonb) from public;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('service.confirm_not_delivered','confirm_effect_not_delivered','human','runtime.nexloop_governed_confirm_not_delivered(text,text,text,text,text,jsonb)','NX-047');

-- 4. The owner's precheck (workbench observe; 0133 body kept for every other verb) -------------------------------------------
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) rename to nexloop_workbench_observe_read_before_determination_v0210;
revoke all on function authz.nexloop_workbench_observe_read_before_determination_v0210(text,text,text,text,text) from public,nexloop_api;
create function authz.nexloop_workbench_observe_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;w jsonb;cfg control.nexloop_workbench_configurations;v_tenant text;
 v_action text:='eios:action:nexloop.workbench.read:1';
begin
 if c->>'verb' is distinct from 'effect_determination' then
  return authz.nexloop_workbench_observe_read_before_determination_v0210(p_digest,p_world,p_text,p_signature,p_payload);end if;
 -- The 0133 verification, for this verb.
 if octet_length(p_text)>1048576 or octet_length(p_payload)>4096
  or a->>'protocol' is distinct from 'nexloop-workbench-observe-v1' or a->>'operation' is distinct from 'execute'
  or a->>'action_resource' is distinct from v_action or a->>'resource_id' is distinct from v_action
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'workbench observe unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-workbench-observe-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'workbench observe unavailable' using errcode='42501';end if;
 w:=authz.nexloop_workbench_member(p_digest,p_world);v_tenant:=w->>'tenant_id';cfg:=control.nexloop_workbench_current(v_tenant);
 if a->>'tenant_id' is distinct from v_tenant or a->>'principal_id' is distinct from w->>'principal_id' or not (cfg.roles->(w->>'role') ? v_action)
  or w->>'role'<>'owner' then
  raise exception 'workbench observe forbidden' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 if coalesce(c->>'intent_id','')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' or (select count(*) from jsonb_object_keys(c))<>2 then
  raise exception 'workbench observe invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 return runtime.nexloop_effect_determination_check(v_tenant,p_world,(c->>'intent_id')::uuid)
  ||jsonb_build_object('outbound_state',(select o.delivery_state from runtime.nexloop_outbound_messages o where o.tenant_id=v_tenant and o.world=p_world and o.intent_id=(c->>'intent_id')::uuid),
   'reply_message_escalated',(select exists(select 1 from control.nexloop_reply_escalations e where e.tenant_id=v_tenant and e.world=p_world and e.message_id=o.trigger_message_id)
     from runtime.nexloop_outbound_messages o where o.tenant_id=v_tenant and o.world=p_world and o.intent_id=(c->>'intent_id')::uuid and o.trigger_message_id is not null));
end $$;
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_observe_read(text,text,text,text,text) from public;
grant execute on function authz.nexloop_workbench_observe_read(text,text,text,text,text) to nexloop_api;
