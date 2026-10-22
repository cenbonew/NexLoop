-- NX-057 R0 (docs/design/remediation-intents.md v3 §8, §10; ADR-040 draft): the evidence of a final effect failure, and the effect
-- outcomes counted by event time.
-- 1. runtime.nexloop_effect_failures: one row per effect intent that ends failed, written in the same transaction by the three paths
--    that fail an intent, with its reason and the ids of its evidence (ids and codes only, no text):
--    * 0152 authz.nexloop_fail_refused_effect: a one-reply intent refused before dispatch -> 'refused_one_reply:<reason>';
--    * 0174 authz.nexloop_effect_origin_expired: the origin Run expired before any dispatch -> 'origin_expired' (origin_run_id);
--    * 0210 runtime.nexloop_governed_confirm_not_delivered: the owner's confirmation with evidence -> 'confirmed_not_delivered'
--      (determination_id, observation_id).
--    The effect event table is unchanged. A deferred constraint trigger holds the rule at commit: an intent set failed without its
--    failure row is refused, so a later fourth path cannot fail an intent silently. A failure is a fact of a real effect intent:
--    declared guarded in control.nexloop_real_effect_tables (no simulation or shadow row).
-- 2. The 0324 snapshot wrapper re-created: the effect part of `outcomes` is counted by event time, numerator and denominator alike
--    (succeeded = fulfilled in the hour, outcome_unknown = first unknown in the hour, defect = a failure row of reason
--    origin_expired or confirmed_not_delivered recorded in the hour); the count of intents by created_at is gone. Refusals and
--    dead letters unchanged. The effect tables are read with the tenant set (FORCE RLS), restored afterwards.
create table runtime.nexloop_effect_failures (
 failure_id uuid primary key default gen_random_uuid(),
 intent_id uuid not null unique,
 tenant_id text not null,world text not null,
 reason_code text not null check(reason_code in ('origin_expired','confirmed_not_delivered') or reason_code ~ '^refused_one_reply:[a-z_]{1,64}$'),
 evidence jsonb not null check(jsonb_typeof(evidence)='object'),
 recorded_at timestamptz not null default clock_timestamp(),
 foreign key(intent_id,tenant_id,world) references runtime.nexloop_effect_intents(intent_id,tenant_id,world)
);
create index nexloop_effect_failures_recent on runtime.nexloop_effect_failures(tenant_id,world,recorded_at);
alter table runtime.nexloop_effect_failures owner to nexloop_owner;
alter table runtime.nexloop_effect_failures enable row level security;
alter table runtime.nexloop_effect_failures force row level security;
create policy effect_failure_tenant on runtime.nexloop_effect_failures to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_effect_failures from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,
 nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_stand_in_worker;
create trigger nx057_effect_failures_append_only before update or delete on runtime.nexloop_effect_failures
 for each row execute function control.nexloop_nx022_append_only();
insert into control.nexloop_real_effect_tables(table_name,guarded,reason) values
 ('runtime.nexloop_effect_failures',true,'the final failure of a real effect intent and its evidence (NX-057 R0)');
alter table runtime.nexloop_effect_failures add constraint nx041_real_effect_world
 check(coalesce(runtime.nexloop_world_kind(world),'') not in ('simulation','shadow'));

create function runtime.nexloop_effect_failure_required() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v_found boolean;
begin
 perform set_config('eios.tenant_id',new.tenant_id,true);
 v_found:=exists(select 1 from runtime.nexloop_effect_failures f where f.intent_id=new.intent_id);
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 if not v_found then raise exception 'effect failure evidence missing' using errcode='23514';end if;
 return null;
end $$;
alter function runtime.nexloop_effect_failure_required() owner to nexloop_owner;
revoke all on function runtime.nexloop_effect_failure_required() from public;
create constraint trigger nx057_effect_failure_required after update of state on runtime.nexloop_effect_intents
 deferrable initially deferred for each row when (new.state='failed' and old.state is distinct from 'failed')
 execute function runtime.nexloop_effect_failure_required();

-- The three failure paths: the base bodies, each with its failure row in the same transaction (registered in the copy guard).
create or replace function authz.nexloop_fail_refused_effect(p_digest text,p_world text,p_intent uuid,p_code text,p_reason text) returns boolean
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;v_tenant text;v_principal text;i runtime.nexloop_effect_intents;
begin
 if session_user is distinct from 'nexloop_action_worker' or p_code<>'NXC05'
  or p_reason not in ('already_answered','reply_window_closed','fallback_bound_elsewhere') then
  raise exception 'effect refusal unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 v_tenant:=ident->'binding'->>'tenant_id';v_principal:=ident->'binding'->>'subject_principal_id';
 perform set_config('eios.tenant_id',v_tenant,true);
 select * into i from runtime.nexloop_effect_intents x where x.intent_id=p_intent and x.tenant_id=v_tenant and x.world=p_world and x.executor_principal=v_principal for update;
 if not found then raise exception 'effect refusal unavailable' using errcode='42501';end if;
 -- Only a refusal this executor recorded (0131), only a reply bound to an inbound message, and only an intent never
 -- dispatched (no attempt): an attempted intent is reconciled by query, never failed by a refusal.
 if not exists(select 1 from runtime.nexloop_dispatch_refusals r where r.tenant_id=v_tenant and r.world=p_world and r.intent_id=p_intent and r.code=p_code)
  or not exists(select 1 from runtime.nexloop_outbound_messages o where o.tenant_id=v_tenant and o.world=p_world and o.intent_id=p_intent and o.trigger_message_id is not null)
  or i.state<>'accepted' or exists(select 1 from runtime.nexloop_effect_attempts a where a.intent_id=p_intent) then return false;end if;
 -- The already-answered reason is re-derived here, not trusted: another reply to the same message may have arrived.
 if p_reason='already_answered' and not exists(select 1 from runtime.nexloop_outbound_messages o join runtime.nexloop_outbound_messages x
   on x.tenant_id=o.tenant_id and x.world=o.world and x.trigger_message_id=o.trigger_message_id and x.intent_id<>o.intent_id
   where o.tenant_id=v_tenant and o.world=p_world and o.intent_id=p_intent and x.delivery_state in ('dispatching','provider_accepted','delivered','unknown')) then
  return false;end if;
 update runtime.nexloop_effect_outbox set state='failed',lease_until=null,lease_credential=null,lease_principal=null
  where intent_id=p_intent and state in ('pending','leased');
 update runtime.nexloop_effect_intents set state='failed' where intent_id=p_intent;
 insert into runtime.nexloop_effect_failures(intent_id,tenant_id,world,reason_code,evidence)  -- NX-057 R0
  values(p_intent,v_tenant,p_world,'refused_one_reply:'||p_reason,jsonb_build_object('refusal_code',p_code,'reason',p_reason));
 return true;
end $$;

create or replace function authz.nexloop_effect_origin_expired(p_digest text,p_world text,p_intent uuid) returns text
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;v_tenant text;v_principal text;i runtime.nexloop_effect_intents;v_origin uuid;
begin
 if session_user is distinct from 'nexloop_action_worker' then raise exception 'effect origin unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 v_tenant:=ident->'binding'->>'tenant_id';v_principal:=ident->'binding'->>'subject_principal_id';
 perform set_config('eios.tenant_id',v_tenant,true);
 select * into i from runtime.nexloop_effect_intents x where x.intent_id=p_intent and x.tenant_id=v_tenant and x.world=p_world and x.executor_principal=v_principal for update;
 if not found then raise exception 'effect origin unavailable' using errcode='42501';end if;
 -- The origin is the one the claim uses (0042): the recorded origin_run_id, else the earliest submission's Run.
 v_origin:=coalesce(i.origin_run_id,(select s.run_id from runtime.nexloop_effect_submissions s where s.intent_id=p_intent order by s.created_at,s.run_id limit 1));
 -- Only an expired credential is permanent: a revocation may be undone (a restore revokes every Run credential and the
 -- owner's opening re-issues them), so a revoked origin is left to wait like any other refused admission.
 if v_origin is null or not exists(select 1 from authz.nexloop_run_credentials r where r.run_id=v_origin and r.expires_at<=clock_timestamp()) then
  return 'none';end if;
 -- Dispatch controls come first: while a restore fence, a paused channel, a closed real-dispatch switch or any other
 -- control refuses this intent, it stays blocked exactly as before (the owner's decision), never failed or stopped here.
 begin perform authz.nexloop_assert_intent_dispatch_controls(p_digest,p_world,p_intent);
 exception when others then return 'none';end;
 if i.state='accepted' and not exists(select 1 from runtime.nexloop_effect_attempts a where a.intent_id=p_intent) then
  update runtime.nexloop_effect_outbox set state='failed',lease_until=null,lease_credential=null,lease_principal=null
   where intent_id=p_intent and state in ('pending','leased');
  update runtime.nexloop_effect_intents set state='failed' where intent_id=p_intent;
  insert into runtime.nexloop_effect_failures(intent_id,tenant_id,world,reason_code,evidence)  -- NX-057 R0
   values(p_intent,v_tenant,p_world,'origin_expired',jsonb_build_object('origin_run_id',v_origin));
  return 'failed';
 end if;
 if i.state in ('dispatching','unknown') then
  update runtime.nexloop_effect_outbox set available_at='infinity',lease_until=null,lease_credential=null,lease_principal=null
   where intent_id=p_intent and state not in ('fulfilled','failed');
  return 'stopped';
 end if;
 return 'none';
end $$;

create or replace function runtime.nexloop_governed_confirm_not_delivered(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
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
 insert into runtime.nexloop_effect_failures(intent_id,tenant_id,world,reason_code,evidence)  -- NX-057 R0
  values(v_intent,p_tenant,p_world,'confirmed_not_delivered',jsonb_build_object('determination_id',v_id,'observation_id',chk->>'not_found_observation_id'));
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

-- The snapshot wrapper of 0324 (registered in the copy guard).
create or replace function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare s jsonb:=runtime.nexloop_metrics_snapshot_before_outcomes(p_tenant,p_world);now_at timestamptz:=clock_timestamp();
 classes jsonb;failures numeric;decided numeric;protected numeric;
 prior text:=current_setting('eios.tenant_id',true);
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 with refusals as (
   select coalesce(c.outcome_class,'unresolved') k,count(*) n from runtime.nexloop_dispatch_refusals r
    left join control.nexloop_outcome_code_classes c on c.code=r.code
    where r.tenant_id=p_tenant and r.world=p_world and r.recorded_at>now_at-interval '1 hour' group by 1),
  -- NX-057 R0: the effect outcomes by the time of their event, numerator and denominator alike (the 0324 count by the intent's
  -- created_at missed every failure decided later than an hour after the intent was made, and counted a failed intent with a
  -- refusal of its own once more): an intent fulfilled in the hour, an intent whose first unknown came in the hour, and a final
  -- failure recorded in the hour whose reason is a defect (a refused one-reply intent is already counted under its refusal).
  intents as (
   select 'succeeded' k,count(distinct e.intent_id) n from runtime.nexloop_effect_events e
    where e.tenant_id=p_tenant and e.world=p_world and e.event_kind='fulfilled' and e.created_at>now_at-interval '1 hour'
   union all
   select 'outcome_unknown',count(*) from (select e.intent_id from runtime.nexloop_effect_events e
     where e.tenant_id=p_tenant and e.world=p_world and e.event_kind='unknown' group by e.intent_id
     having min(e.created_at)>now_at-interval '1 hour') u
   union all
   select 'defect',count(*) from runtime.nexloop_effect_failures f
    where f.tenant_id=p_tenant and f.world=p_world and f.reason_code in ('origin_expired','confirmed_not_delivered')
     and f.recorded_at>now_at-interval '1 hour'),
  dead as (select 'defect' k,coalesce(sum((q.value->>'dead_lettered_last_hour')::numeric),0) n from jsonb_each(coalesce(s->'queues','{}'::jsonb)) q
    where jsonb_typeof(q.value)='object' and jsonb_typeof(q.value->'dead_lettered_last_hour')='number'),
  allk as (select k,sum(n) n from (select * from refusals union all select * from intents union all select * from dead) x group by k)
 select coalesce(jsonb_object_agg(k,n),'{}'::jsonb) into classes from allk where n>0;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 failures:=coalesce((classes->>'transient')::numeric,0)+coalesce((classes->>'authorization')::numeric,0)+coalesce((classes->>'defect')::numeric,0)
  +coalesce((classes->>'unresolved')::numeric,0);
 protected:=coalesce((classes->>'conflict_protected')::numeric,0)+coalesce((classes->>'superseded')::numeric,0);
 decided:=failures+protected+coalesce((classes->>'succeeded')::numeric,0)+coalesce((classes->>'no_op')::numeric,0)+coalesce((classes->>'outcome_unknown')::numeric,0);
 return s||jsonb_build_object('outcomes',jsonb_build_object('window_seconds',3600,'classes',classes,'decided',decided,'failures',failures,
  'failure_rate',case when decided=0 then 0 else round(failures/decided,4) end,
  'failure_rate_counting_refusals',case when decided=0 then 0 else round((failures+protected)/decided,4) end,
  'unresolved',coalesce((classes->>'unresolved')::numeric,0)));
end $$;
