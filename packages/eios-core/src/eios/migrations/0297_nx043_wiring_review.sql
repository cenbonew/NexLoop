-- NX-043 X5 wiring review (L6, dispatcher 2026-10-21). The migration number lives only in the file name and the catalog.
-- 2.1 (confirmed, real): a Run's Consumer comes from runtime.nexloop_evo_run_consumer (message route, fallback binding, role binding),
--     not the role binding alone. D5 decision (dispatcher 2026-10-21): the experiment cap (0-100 CNY) is the candidate strategy's
--     increment; the control group is normal business - its model spend is written to the experiment ledger with arm control, for the
--     report only, and never counts toward the cap. A simulation or shadow world's Runs are the experiment's (arm null, counted).
-- 2.2 (real): the model accounting runs in its own block with a 200 ms lock_timeout; any error there is only counted
--     (runtime.nexloop_experiment_accounting_failures) and never fails the real reservation. The incentive refusal (NXB01) stays outside.
--     The old trigger and function are replaced (new name), not redefined.
-- 2.3: the guard tick isolates each experiment; a failing guard is counted (failed), the others and the alerts go on.
-- 1.2: on erasure every cluster key the erased Consumer's rows carried is replaced, so no session key of theirs stays on others.
-- renewal_reminder (owner 2026-10-21): outreach purpose list v2 makes it a marketing purpose (deploy/configuration/outreach-purposes.v2.json).
-- Re-created functions take their latest bodies (0292, 0295, 0296) with only these changes (registered in tests/test_function_copy_guard.py).

alter table control.nexloop_experiment_spend add column arm text check(arm in ('candidate','control'));

create table runtime.nexloop_experiment_accounting_failures (
 event_id bigint generated always as identity primary key,
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null,
 consumption_id text not null,sqlstate text not null check(sqlstate~'^[0-9A-Z]{5}$'),
 recorded_at timestamptz not null default clock_timestamp());
alter table runtime.nexloop_experiment_accounting_failures owner to nexloop_owner;
alter table runtime.nexloop_experiment_accounting_failures enable row level security;
alter table runtime.nexloop_experiment_accounting_failures force row level security;
create policy tenant_boundary on runtime.nexloop_experiment_accounting_failures to nexloop_owner using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_experiment_accounting_failures from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_stand_in_worker,nexloop_twin_port,nexloop_sim_worker,nexloop_sim_guard,nexloop_sim_api;

drop trigger nx043_experiment_budget on control.nexloop_budget_consumption;
drop function control.nexloop_experiment_budget_gate();
create function control.nexloop_experiment_budget_attribution() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);v_exp text;v_arm text;v_consumer text;r jsonb;v_total numeric;v_cap numeric;
 v_amount numeric:=round(new.amount,4);v_state text;
begin
 if new.budget_kind not in ('model','incentive') then return new;end if;
 perform set_config('eios.tenant_id',new.tenant_id,true);
 -- D1, D5: no incentive in the first batch for a Consumer of a started experiment (either group). A refusal, outside the safe block.
 if new.budget_kind='incentive' and new.consumption_id like 'incentive:%' then
  select i.consumer_id into v_consumer from runtime.nexloop_incentive_issues i where i.tenant_id=new.tenant_id and i.world=new.world
   and i.intent_id=substr(new.consumption_id,11);
  if v_consumer is not null and exists(select 1 from control.nexloop_experiment_allocations a join control.nexloop_experiments e
    on e.tenant_id=a.tenant_id and e.experiment_id=a.experiment_id where a.tenant_id=new.tenant_id and a.consumer_id=v_consumer and a.arm is not null
    and e.registration->>'world'=new.world and runtime.nexloop_experiment_status(new.tenant_id,e.experiment_id)='started') then
   perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
   raise exception 'experiment incentive budget is 0 (D1, D5)' using errcode='NXB01';
  end if;
  perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
  return new;
 end if;
 if new.consumption_id!~'^run:[0-9a-f-]{36}$' or new.unit<>'CNY' or v_amount<=0 then
  perform set_config('eios.tenant_id',coalesce(v_prior,''),true);return new;end if;
 begin
  perform set_config('lock_timeout','200ms',true);
  if new.world='real' then
   v_consumer:=runtime.nexloop_evo_run_consumer(new.tenant_id,'real',substr(new.consumption_id,5));
   select a.experiment_id,a.arm into v_exp,v_arm from control.nexloop_experiment_allocations a
    join control.nexloop_experiments e on e.tenant_id=a.tenant_id and e.experiment_id=a.experiment_id and e.registration->>'world'='real'
    where a.tenant_id=new.tenant_id and a.consumer_id=v_consumer and a.arm is not null and runtime.nexloop_experiment_status(new.tenant_id,a.experiment_id)='started'
    order by a.allocated_at limit 1;
  else
   select e.experiment_id into v_exp from control.nexloop_experiments e where e.tenant_id=new.tenant_id and e.registration->>'world'=new.world
    and runtime.nexloop_experiment_status(new.tenant_id,e.experiment_id)='started' order by e.registered_at limit 1;
  end if;
  if v_exp is not null then
   perform pg_advisory_xact_lock(hashtextextended('nexloop:experiment:'||new.tenant_id||':'||v_exp,0));
   if v_arm='control' then
    -- Normal business: reported beside the candidate's increment, never against the cap.
    insert into control.nexloop_experiment_spend(tenant_id,experiment_id,category,amount_cny,spend_ref,arm) values(new.tenant_id,v_exp,'model',v_amount,new.consumption_id,'control')
     on conflict do nothing;
   else
    select e.registration into r from control.nexloop_experiments e where e.tenant_id=new.tenant_id and e.experiment_id=v_exp;
    v_cap:=(r->'budget'->>'model_cny')::numeric;
    select coalesce(sum(amount_cny),0) into v_total from control.nexloop_experiment_spend where tenant_id=new.tenant_id and experiment_id=v_exp
     and category='model' and arm is distinct from 'control';
    if v_total+v_amount>v_cap then
     perform runtime.nexloop_experiment_autostop(new.tenant_id,v_exp,'budget_exhausted',runtime.nexloop_experiment_metrics(new.tenant_id,v_exp));
    else
     insert into control.nexloop_experiment_spend(tenant_id,experiment_id,category,amount_cny,spend_ref,arm) values(new.tenant_id,v_exp,'model',v_amount,new.consumption_id,v_arm)
      on conflict do nothing;
     if v_total+v_amount=v_cap then
      perform runtime.nexloop_experiment_autostop(new.tenant_id,v_exp,'budget_exhausted',runtime.nexloop_experiment_metrics(new.tenant_id,v_exp));end if;
    end if;
   end if;
  end if;
 exception when others then
  get stacked diagnostics v_state=returned_sqlstate;
  insert into runtime.nexloop_experiment_accounting_failures(tenant_id,world,consumption_id,sqlstate) values(new.tenant_id,new.world,new.consumption_id,v_state);
 end;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return new;
end $$;
alter function control.nexloop_experiment_budget_attribution() owner to nexloop_owner;
revoke all on function control.nexloop_experiment_budget_attribution() from public;
create trigger nx043_experiment_budget before insert on control.nexloop_budget_consumption for each row execute function control.nexloop_experiment_budget_attribution();

-- The snapshot's experiments block gains the accounting failures of the last 24 hours (numbers only).
alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_experiment_failures;
revoke all on function runtime.nexloop_metrics_snapshot_before_experiment_failures(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare s jsonb:=runtime.nexloop_metrics_snapshot_before_experiment_failures(p_tenant,p_world);v_prior text:=current_setting('eios.tenant_id',true);n bigint;
begin
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 select count(*) into n from runtime.nexloop_experiment_accounting_failures f where f.tenant_id=p_tenant and f.recorded_at>=clock_timestamp()-interval '24 hours';
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return jsonb_set(s,'{experiments}',coalesce(s->'experiments','{}'::jsonb)||jsonb_build_object('accounting_failures_24h',n));
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;

-- renewal_reminder (owner 2026-10-21): the purpose list v2 (trusted configuration; v1 stays as published).
insert into control.nexloop_outreach_purpose_lists(version,definition,definition_digest,published_by)
 values(2,'{"schema":"nexloop-outreach-purposes/1","version":2,"decision":"Outreach design §2 and owner decisions P2/P5/P8 (2026-10-19), outreach slice 3: the purposes proactive outreach may use. Trusted configuration: a new purpose (including a plan-defined one) is a new version of this file; versions are only extended, never edited. The owner enables or disables a listed purpose for the tenant (nexloop.outreach_purposes.set:1); default_enabled applies until then. No purpose needs approval by default (P5; approval is slice 6). Marketing purposes follow the tenant consent mode unless requires_consent (opt-in for that purpose); they carry the server-appended unsubscribe notice and count against the marketing allowance. v2 (owner 2026-10-21, NX-043 X1 review 2.3): renewal_reminder is a marketing purpose - marketing kind and topic, the marketing frequency class and allowance, opt-in consent even when the tenant is opt-out, the unsubscribe notice; it is therefore never an experiment lever. v1 stays as published.","purposes":[{"purpose_id":"commitment_followup","kind":"commitment_followup","trigger":"commitment_due","topic":"commitment_followup","frequency_class":"service","requires_consent":false,"requires_approval":false,"default_enabled":true,"content_rules":{"max_chars":300,"unsubscribe_notice_required":false}},{"purpose_id":"service_followup","kind":"service_followup","trigger":"service_receipt","topic":"service_followup","frequency_class":"service","requires_consent":false,"requires_approval":false,"default_enabled":true,"content_rules":{"max_chars":300,"unsubscribe_notice_required":false}},{"purpose_id":"renewal_reminder","kind":"marketing","trigger":"plan_reassess","topic":"marketing.renewal_reminder","frequency_class":"marketing","requires_consent":true,"requires_approval":false,"default_enabled":false,"content_rules":{"max_chars":300,"unsubscribe_notice_required":true}},{"purpose_id":"marketing_general","kind":"marketing","trigger":"owner_request","topic":"marketing","frequency_class":"marketing","requires_consent":false,"requires_approval":false,"default_enabled":false,"content_rules":{"max_chars":300,"unsubscribe_notice_required":true}}]}'::jsonb,encode(sha256(convert_to(runtime.nexloop_canonical_json('{"schema":"nexloop-outreach-purposes/1","version":2,"decision":"Outreach design §2 and owner decisions P2/P5/P8 (2026-10-19), outreach slice 3: the purposes proactive outreach may use. Trusted configuration: a new purpose (including a plan-defined one) is a new version of this file; versions are only extended, never edited. The owner enables or disables a listed purpose for the tenant (nexloop.outreach_purposes.set:1); default_enabled applies until then. No purpose needs approval by default (P5; approval is slice 6). Marketing purposes follow the tenant consent mode unless requires_consent (opt-in for that purpose); they carry the server-appended unsubscribe notice and count against the marketing allowance. v2 (owner 2026-10-21, NX-043 X1 review 2.3): renewal_reminder is a marketing purpose - marketing kind and topic, the marketing frequency class and allowance, opt-in consent even when the tenant is opt-out, the unsubscribe notice; it is therefore never an experiment lever. v1 stays as published.","purposes":[{"purpose_id":"commitment_followup","kind":"commitment_followup","trigger":"commitment_due","topic":"commitment_followup","frequency_class":"service","requires_consent":false,"requires_approval":false,"default_enabled":true,"content_rules":{"max_chars":300,"unsubscribe_notice_required":false}},{"purpose_id":"service_followup","kind":"service_followup","trigger":"service_receipt","topic":"service_followup","frequency_class":"service","requires_consent":false,"requires_approval":false,"default_enabled":true,"content_rules":{"max_chars":300,"unsubscribe_notice_required":false}},{"purpose_id":"renewal_reminder","kind":"marketing","trigger":"plan_reassess","topic":"marketing.renewal_reminder","frequency_class":"marketing","requires_consent":true,"requires_approval":false,"default_enabled":false,"content_rules":{"max_chars":300,"unsubscribe_notice_required":true}},{"purpose_id":"marketing_general","kind":"marketing","trigger":"owner_request","topic":"marketing","frequency_class":"marketing","requires_consent":false,"requires_approval":false,"default_enabled":false,"content_rules":{"max_chars":300,"unsubscribe_notice_required":true}}]}'::jsonb),'UTF8')),'hex'),'migration:outreach-purposes.v2');

create or replace function authz.nexloop_experiment_guard_tick(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-experiment-guard-v1','eios:action:nexloop.alert.evaluate:1',
  array['nexloop_domain_worker']);
 x record;v jsonb;v_checked integer:=0;v_stopped integer:=0;v_failed integer:=0;
begin
 if p_world is distinct from 'real' then return jsonb_build_object('evaluated',false,'reason','real_world_only');end if;
 for x in select e.experiment_id from control.nexloop_experiments e where e.tenant_id=t
   and runtime.nexloop_experiment_status(t,e.experiment_id)='started' order by e.registered_at limit 200 loop
  begin v:=runtime.nexloop_experiment_guard(t,x.experiment_id);exception when others then v_failed:=v_failed+1;continue;end;  -- review 2.3: one failure stops no other
  v_checked:=v_checked+1;
  if (v->>'stopped')::boolean then v_stopped:=v_stopped+1;end if;
 end loop;
 return jsonb_build_object('evaluated',true,'checked',v_checked,'stopped',v_stopped,'failed',v_failed);
end $$;

create or replace function control.nexloop_experiment_erasure() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);v_pseudonym text:='pseudonym:'||encode(extensions.gen_random_bytes(16),'hex');v_keys text[];
begin
 if new.state<>'purging' or old.state='purging' then return null;end if;
 perform set_config('eios.tenant_id',new.tenant_id,true);
 perform runtime.nexloop_erasure_open('control.nexloop_experiment_allocations',array['consumer_id','cluster_key'],false,'consumer-erasure:'||new.request_id);
 select array_agg(distinct a.cluster_key) into v_keys from control.nexloop_experiment_allocations a where a.tenant_id=new.tenant_id and a.consumer_id=new.consumer_id;  -- review 1.2
 update control.nexloop_experiment_allocations a set consumer_id=v_pseudonym,cluster_key=v_pseudonym
  where a.tenant_id=new.tenant_id and a.consumer_id=new.consumer_id
   and exists(select 1 from control.nexloop_experiments e where e.tenant_id=a.tenant_id and e.experiment_id=a.experiment_id and e.registration->>'world'=new.world);
 update control.nexloop_experiment_allocations a set cluster_key=v_pseudonym
  where a.tenant_id=new.tenant_id and (a.cluster_key=new.consumer_id or a.cluster_key=any(coalesce(v_keys,'{}')))
   and exists(select 1 from control.nexloop_experiments e where e.tenant_id=a.tenant_id and e.experiment_id=a.experiment_id and e.registration->>'world'=new.world);
 -- Only this table's scope is closed: the erasure step may hold others open in the same transaction.
 delete from control.nexloop_erasure_scope where txid=txid_current() and table_name='control.nexloop_experiment_allocations';
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return null;
end $$;

create or replace function runtime.nexloop_experiment_metrics(p_tenant text,p_experiment text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);r jsonb;v jsonb;
begin
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 select e.registration into r from control.nexloop_experiments e where e.tenant_id=p_tenant and e.experiment_id=p_experiment;
 with a as (select o.consumer_id,o.arm,o.allocated_at from control.nexloop_experiment_allocations o
   where o.tenant_id=p_tenant and o.experiment_id=p_experiment and o.arm is not null),
  refused as (select a.consumer_id,a.arm from a where exists(select 1 from control.nexloop_contact_restrictions c where c.tenant_id=p_tenant
    and c.world=r->>'world' and c.consumer_id=a.consumer_id and c.restricted_at>=a.allocated_at)
   or exists(select 1 from runtime.nexloop_marketing_consents m where m.tenant_id=p_tenant and m.world=r->>'world' and m.consumer_id=a.consumer_id
    and m.kind='withdrawn' and m.recorded_at>=a.allocated_at)),
  anomalies as (select count(*) n from runtime.nexloop_effect_intents i join a on a.consumer_id=i.consumer_id
   where i.tenant_id=p_tenant and i.world=r->>'world' and i.created_at>=a.allocated_at
    and (i.state='unknown' or exists(select 1 from runtime.nexloop_effect_determinations d where d.tenant_id=i.tenant_id and d.world=i.world and d.intent_id=i.intent_id)))
 select jsonb_build_object(
   'candidate',(select count(*) from a where arm='candidate'),'control',(select count(*) from a where arm='control'),
   'candidate_refused',(select count(*) from refused where arm='candidate'),'control_refused',(select count(*) from refused where arm='control'),
   'reconciliation_anomalies',(select n from anomalies),
   'model_cny',(select coalesce(sum(amount_cny),0) from control.nexloop_experiment_spend s where s.tenant_id=p_tenant and s.experiment_id=p_experiment and s.category='model' and s.arm is distinct from 'control'),
   'control_model_cny',(select coalesce(sum(amount_cny),0) from control.nexloop_experiment_spend s where s.tenant_id=p_tenant and s.experiment_id=p_experiment and s.category='model' and s.arm='control'),
   'incentive_cny',(select coalesce(sum(amount_cny),0) from control.nexloop_experiment_spend s where s.tenant_id=p_tenant and s.experiment_id=p_experiment and s.category='incentive'))
  into v;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return v;
end $$;

create or replace function runtime.nexloop_experiment_spend(p_tenant text,p_experiment text,p_category text,p_amount numeric,p_ref text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);r jsonb;v_cap numeric;v_total numeric;v_stopped boolean:=false;s control.nexloop_experiment_spend;
begin
 p_amount:=round(p_amount,4);  -- review 2.2.a: decide on the amount the ledger keeps
 if coalesce(p_category,'') not in ('model','incentive') or p_amount is null or p_amount<=0 or coalesce(p_ref,'')!~'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$' then
  raise exception 'experiment: invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 select e.registration into r from control.nexloop_experiments e where e.tenant_id=p_tenant and e.experiment_id=p_experiment;
 if r is null then raise exception 'experiment: not_found' using errcode='22023';end if;
 perform pg_advisory_xact_lock(hashtextextended('nexloop:experiment:'||p_tenant||':'||p_experiment,0));
 select * into s from control.nexloop_experiment_spend where tenant_id=p_tenant and experiment_id=p_experiment and spend_ref=p_ref;
 if found then
  if (s.category,s.amount_cny) is distinct from (p_category,p_amount::numeric(12,4)) then raise exception 'experiment: request_reused' using errcode='22023';end if;
  perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
  return jsonb_build_object('allowed',true,'replayed',true,'stopped',false);
 end if;
 if runtime.nexloop_experiment_status(p_tenant,p_experiment) is distinct from 'started' then
  perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
  return jsonb_build_object('allowed',false,'reason','not_running','stopped',false);
 end if;
 v_cap:=(r->'budget'->>(p_category||'_cny'))::numeric;
 select coalesce(sum(amount_cny),0) into v_total from control.nexloop_experiment_spend where tenant_id=p_tenant and experiment_id=p_experiment and category=p_category and arm is distinct from 'control';
 if v_total+p_amount>v_cap then
  v_stopped:=runtime.nexloop_experiment_autostop(p_tenant,p_experiment,'budget_exhausted',runtime.nexloop_experiment_metrics(p_tenant,p_experiment));
  perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
  return jsonb_build_object('allowed',false,'reason','budget_exhausted','stopped',v_stopped);
 end if;
 insert into control.nexloop_experiment_spend(tenant_id,experiment_id,category,amount_cny,spend_ref) values(p_tenant,p_experiment,p_category,p_amount,p_ref);
 if v_total+p_amount=v_cap then
  v_stopped:=runtime.nexloop_experiment_autostop(p_tenant,p_experiment,'budget_exhausted',runtime.nexloop_experiment_metrics(p_tenant,p_experiment));end if;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return jsonb_build_object('allowed',true,'replayed',false,'stopped',v_stopped);
end $$;
