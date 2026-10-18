-- A provider's not_found is recorded as an observation (temporary number 0213; 10/18 stage, release 16: two owner requery orders
-- armed, queried, and left no observation, so the owner's confirm-not-delivered (0210) had no evidence). The effect worker
-- turned a not_found answer into a plain unknown (0042 verb 'unknown'): the provider's answer was lost. Now the worker records
-- it through the existing reconcile query path (0065 observe_query → 0042 observe, which accepts not_found) and then calls
-- authz.nexloop_effect_not_found_unknown: the observe branch leaves the outbox query_pending, due in 1 s, which would query the
-- provider every second; this puts the outbox back to unknown with an unknown event (evidence: the observation), so the 0174
-- back-off and the 0199 6 h window apply exactly as before. Nothing is sent, nothing is retried, the intent stays unknown.
-- runtime.nexloop_effect_determination_check (0210 body otherwise unchanged): the not_found evidence is the latest observation
-- of the intent's attempt; 0210 compared it with the attempt's current revision, which recording the observation advances, so
-- no observation could ever count.
create function authz.nexloop_effect_not_found_unknown(p_digest text,p_world text,p_intent uuid) returns boolean
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;v_tenant text;v_principal text;i runtime.nexloop_effect_intents;o runtime.nexloop_effect_outbox;x runtime.nexloop_effect_observations;
begin
 if session_user is distinct from 'nexloop_action_worker' then raise exception 'effect not_found unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 v_tenant:=ident->'binding'->>'tenant_id';v_principal:=ident->'binding'->>'subject_principal_id';
 perform set_config('eios.tenant_id',v_tenant,true);
 select * into i from runtime.nexloop_effect_intents where intent_id=p_intent and tenant_id=v_tenant and world=p_world and executor_principal=v_principal for update;
 if not found or i.state<>'unknown' then return false;end if;
 select * into o from runtime.nexloop_effect_outbox where intent_id=p_intent for update;
 if o.state<>'query_pending' or (o.lease_until is not null and o.lease_until>clock_timestamp()) then return false;end if;
 -- Only right after this executor's own not_found observation of the current attempt.
 select * into x from runtime.nexloop_effect_observations where intent_id=p_intent order by observed_at desc,observation_id desc limit 1;
 if x.observation_id is null or x.provider_state<>'not_found' or x.observer_principal is distinct from v_principal
  or x.attempt_revision+1 is distinct from (select a.attempt_revision from runtime.nexloop_effect_attempts a where a.intent_id=p_intent) then return false;end if;
 update runtime.nexloop_effect_outbox set state='unknown',available_at=clock_timestamp() where intent_id=p_intent;
 insert into runtime.nexloop_effect_events values(gen_random_uuid(),p_intent,v_tenant,p_world,'unknown',o.fence,clock_timestamp(),
  jsonb_build_object('observation_id',x.observation_id));
 return true;
end $$;
alter function authz.nexloop_effect_not_found_unknown(text,text,uuid) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_not_found_unknown(text,text,uuid) from public;
grant execute on function authz.nexloop_effect_not_found_unknown(text,text,uuid) to nexloop_action_worker;

create or replace function runtime.nexloop_effect_determination_check(p_tenant text,p_world text,p_intent uuid) returns jsonb
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
  -- 0213: an observation is stored with the attempt revision it observed, and recording it advances the attempt (0042
  -- observe), so the current attempt's evidence is the latest observation of this intent's one attempt, not an equal revision.
  select * into latest from runtime.nexloop_effect_observations x where x.intent_id=p_intent and x.attempt_revision<a.attempt_revision
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
