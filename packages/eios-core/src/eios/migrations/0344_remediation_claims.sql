-- NX-057 R4 (docs/design/remediation-intents.md v4 §3.4, §8, §10, §11, §13 R4; owner Q-4 A): the action claim of a finally failed
-- effect ends permanent_failure, the outcomes block reports remediation, and the workbench shows a remediation's evidence chain.
-- 1. A trigger after each runtime.nexloop_effect_failures row (the three failure paths, in their own transaction): the effect intent's
--    action claim (runtime.nexloop_action_claims, key action name + intent id) that is not terminal ends terminal with status
--    permanent_failure (outcome id = the failure id, digest over intent, failure and reason). An intent never reserved has no claim and
--    nothing changes; a terminal claim is left as it is. A later successful remediation never turns it into compensated (Q-4 A): the
--    failure stays a failure. A permanent_failure claim is no generation condition and no failure count (design §3.4): remediation and
--    the failure rate read effect_failures only.
-- 2. The metrics snapshot's `outcomes` gains `remediation` (counts only): state changes of the last hour by target state, and
--    unremediated_defects - the defect failures of the hour (origin_expired, confirmed_not_delivered) that have no remediation, in total
--    and by the planner's decision (not yet considered, no rule, switch closed, rate capped, rule held, ...).
-- 3. authz.nexloop_workbench_read gains the verb remediation_evidence (rename and wrap; every other verb unchanged): one remediation's
--    chain as ids, codes and times only - the source intent, its failure record and evidence ids, the action claim's terminal status,
--    the rule version and digest, the woken plan and the consuming Run, the origin remediation, and every state change (no reason
--    text). The Consumer id only where the member's role derives READ; nothing for a Consumer being erased.

-- 1. The action claim ends with the failure --------------------------------------------------------------------------------------
create function runtime.nexloop_effect_failure_claim_terminal() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v_action text;
begin
 perform set_config('eios.tenant_id',new.tenant_id,true);
 select i.action_name into v_action from runtime.nexloop_effect_intents i where i.intent_id=new.intent_id and i.tenant_id=new.tenant_id and i.world=new.world;
 update runtime.nexloop_action_claims c set claim=c.claim||jsonb_build_object('state','terminal','terminal_outcome',jsonb_build_object(
    'outcome_id',new.failure_id::text,'outcome_revision',1,'status','permanent_failure',
    'outcome_digest',encode(sha256(convert_to(jsonb_build_object('intent_id',new.intent_id,'failure_id',new.failure_id,'reason_code',new.reason_code)::text,'UTF8')),'hex'),
    'finalized_at',to_jsonb(new.recorded_at)))
  where c.tenant_id=new.tenant_id and c.world=new.world and c.action_name=v_action and c.intent_id=new.intent_id::text
   and c.claim->>'state' is distinct from 'terminal';
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return null;
end $$;
alter function runtime.nexloop_effect_failure_claim_terminal() owner to nexloop_owner;
revoke all on function runtime.nexloop_effect_failure_claim_terminal() from public;
create trigger nx057_effect_failure_claim_terminal after insert on runtime.nexloop_effect_failures
 for each row execute function runtime.nexloop_effect_failure_claim_terminal();
-- This effect-path function writes the action claims: declared for the NX-041 outbound guard (a governed Action's claim record; nothing
-- leaves the system through it, so not guarded: simulation worlds hold claims too).
insert into control.nexloop_real_effect_tables(table_name,guarded,reason) values
 ('runtime.nexloop_action_claims',false,'the claim record of a governed Action (idempotency and terminal outcome); nothing leaves the system through it (NX-057 R4 ends a failed effect''s claim)');

-- 2. outcomes.remediation ---------------------------------------------------------------------------------------------------------
alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_remediation_claims;
revoke all on function runtime.nexloop_metrics_snapshot_before_remediation_claims(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare s jsonb:=runtime.nexloop_metrics_snapshot_before_remediation_claims(p_tenant,p_world);prior text:=current_setting('eios.tenant_id',true);
 now_at timestamptz:=clock_timestamp();v jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 with defects as (
   select f.failure_id,(select sc.decision from runtime.nexloop_remediation_scans sc where sc.failure_id=f.failure_id) decision
   from runtime.nexloop_effect_failures f
   where f.tenant_id=p_tenant and f.world=p_world and f.reason_code in ('origin_expired','confirmed_not_delivered') and f.recorded_at>now_at-interval '1 hour'
    and not exists(select 1 from runtime.nexloop_remediation_intents q where q.failure_id=f.failure_id))
 select jsonb_build_object(
   'window_seconds',3600,
   'changes',coalesce((select jsonb_object_agg(e.to_state,e.n) from (select to_state,count(*) n from runtime.nexloop_remediation_events
     where tenant_id=p_tenant and world=p_world and recorded_at>now_at-interval '1 hour' group by to_state) e),'{}'::jsonb),
   'unremediated_defects',(select count(*) from defects),
   'unremediated_by_decision',coalesce((select jsonb_object_agg(d.k,d.n) from (select coalesce(decision,'not_considered') k,count(*) n from defects group by 1) d),'{}'::jsonb))
  into v;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return s||jsonb_build_object('outcomes',coalesce(s->'outcomes','{}'::jsonb)||jsonb_build_object('remediation',v));
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;

-- 3. The evidence chain of one remediation ----------------------------------------------------------------------------------------
create function authz.nexloop_workbench_remediation_evidence(p_tenant text,p_world text,p_role text,p_remediation uuid) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);q runtime.nexloop_remediation_intents;i runtime.nexloop_effect_intents;
 f runtime.nexloop_effect_failures;m control.nexloop_remediation_rules;c runtime.nexloop_action_claims;v jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into q from runtime.nexloop_remediation_intents where remediation_id=p_remediation and tenant_id=p_tenant and world=p_world;
 if found then
  select * into i from runtime.nexloop_effect_intents where intent_id=q.source_intent_id and tenant_id=p_tenant and world=p_world;
 end if;
 -- Unknown, or of a Consumer being erased (NX-029): no chain at all.
 if q.remediation_id is null or runtime.nexloop_consumer_erasing(p_tenant,p_world,i.consumer_id) then
  perform set_config('eios.tenant_id',coalesce(prior,''),true);return null;end if;
 select * into f from runtime.nexloop_effect_failures where failure_id=q.failure_id;
 select * into m from control.nexloop_remediation_rules where tenant_id=p_tenant and world=p_world and manifest_version=q.rule_version;
 select * into c from runtime.nexloop_action_claims where tenant_id=p_tenant and world=p_world and action_name=i.action_name and intent_id=i.intent_id::text;
 v:=jsonb_build_object(
  'remediation',jsonb_build_object('remediation_id',q.remediation_id,'kind',q.kind,'mode',q.mode,'state',q.state,'last_code',q.last_code,
    'due_by',q.due_by,'created_at',q.created_at,'updated_at',q.updated_at,'origin_remediation_id',q.origin_remediation_id),
  'source_intent',jsonb_build_object('intent_id',i.intent_id,'action_name',i.action_name,'action_version',i.action_version,'state',i.state,
    'created_at',i.created_at,
    'consumer_id',case when authz.nexloop_workbench_member_may_read(p_tenant,p_world,p_role,'eios:object:Consumer/'||i.consumer_id) then i.consumer_id end),
  'failure',jsonb_build_object('failure_id',f.failure_id,'reason_code',f.reason_code,'recorded_at',f.recorded_at,'evidence',f.evidence),
  'claim',case when c.intent_id is null then null else jsonb_build_object('state',c.claim->>'state','status',c.claim->'terminal_outcome'->>'status',
    'finalized_at',c.claim->'terminal_outcome'->>'finalized_at') end,
  'rule',jsonb_build_object('rule_id',q.rule_id,'manifest_version',q.rule_version,'digest',q.rule_digest,
    'kind',(select r->>'kind' from jsonb_array_elements(coalesce(m.manifest->'rules','[]'::jsonb)) r where r->>'rule_id'=q.rule_id limit 1)),
  'plan',case when q.plan_id is null then null else jsonb_build_object('plan_id',q.plan_id,'woken_at',q.woken_at,'consumed_by_run',q.consumed_by_run) end,
  'events',coalesce((select jsonb_agg(jsonb_build_object('from_state',e.from_state,'to_state',e.to_state,'code',e.code,'actor_kind',e.actor_kind,
    'recorded_at',e.recorded_at) order by e.recorded_at,e.event_id) from runtime.nexloop_remediation_events e where e.remediation_id=q.remediation_id),'[]'::jsonb));
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
alter function authz.nexloop_workbench_remediation_evidence(text,text,text,uuid) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_remediation_evidence(text,text,text,uuid) from public;

alter function authz.nexloop_workbench_read(text,text,text,text,text) rename to nexloop_workbench_read_before_remediation_evidence;
revoke all on function authz.nexloop_workbench_read_before_remediation_evidence(text,text,text,text,text) from public,nexloop_api;
create function authz.nexloop_workbench_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;w jsonb;cfg control.nexloop_workbench_configurations;v_tenant text;
 v_action text:='eios:action:nexloop.workbench.read:1';
begin
 if c->>'verb' is distinct from 'remediation_evidence' then
  return authz.nexloop_workbench_read_before_remediation_evidence(p_digest,p_world,p_text,p_signature,p_payload);end if;
 -- The 0130 verification, for this verb (as the object_fields and remediations wrappers).
 if session_user is distinct from 'nexloop_api' or p_world is distinct from 'real'
  or octet_length(p_text)>1048576 or octet_length(p_payload)>8192
  or a->>'protocol' is distinct from 'nexloop-workbench-read-v1' or a->>'operation' is distinct from 'execute'
  or a->>'action_resource' is distinct from v_action or a->>'resource_id' is distinct from v_action
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'workbench read unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-workbench-read-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'workbench read unavailable' using errcode='42501';end if;
 w:=authz.nexloop_workbench_member(p_digest,p_world);v_tenant:=w->>'tenant_id';cfg:=control.nexloop_workbench_current(v_tenant);
 if a->>'tenant_id' is distinct from v_tenant or a->>'principal_id' is distinct from w->>'principal_id' or not (cfg.roles->(w->>'role') ? v_action) then
  raise exception 'workbench read forbidden' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 if (select array_agg(x order by x) from jsonb_object_keys(c) x) is distinct from array['remediation_id','verb']
  or coalesce(c->>'remediation_id','')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' then
  raise exception 'workbench read invalid' using errcode='22023';end if;
 return authz.nexloop_workbench_remediation_evidence(v_tenant,p_world,w->>'role',(c->>'remediation_id')::uuid);
end $$;
alter function authz.nexloop_workbench_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_read(text,text,text,text,text) from public;
grant execute on function authz.nexloop_workbench_read(text,text,text,text,text) to nexloop_api;
