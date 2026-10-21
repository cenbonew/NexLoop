-- NX-043 X5 review of d4de09d2 (L6, dispatcher 2026-10-21). The migration number lives only in the file name and the catalog.
-- 2.1 (confirmed, real): the model accounting block set lock_timeout to 200 ms and left it for the rest of the caller's transaction
--     (the real model reservation, the Run issue). It now restores the caller's value when the block ends normally; on an error the
--     block's subtransaction is rolled back and its setting with it.
-- 3.2: the guard tick reports the SQLSTATE of every failing experiment (failed_sqlstates).
-- 2.3 (optional, not changed): an error in the incentive branch fails the incentive reservation - the safe direction for an effect.
-- Both functions are re-created from their 0297 bodies with only these changes (registered in tests/test_function_copy_guard.py).

create or replace function control.nexloop_experiment_budget_attribution() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);v_exp text;v_arm text;v_consumer text;r jsonb;v_total numeric;v_cap numeric;
 v_amount numeric:=round(new.amount,4);v_state text;v_lock text:=current_setting('lock_timeout');
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
  perform set_config('lock_timeout',v_lock,true);  -- review 2.1: the caller's lock_timeout again for the rest of its transaction
 exception when others then
  get stacked diagnostics v_state=returned_sqlstate;
  insert into runtime.nexloop_experiment_accounting_failures(tenant_id,world,consumption_id,sqlstate) values(new.tenant_id,new.world,new.consumption_id,v_state);
 end;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return new;
end $$;

create or replace function authz.nexloop_experiment_guard_tick(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-experiment-guard-v1','eios:action:nexloop.alert.evaluate:1',
  array['nexloop_domain_worker']);
 x record;v jsonb;v_checked integer:=0;v_stopped integer:=0;v_failed integer:=0;v_states text[]:='{}';v_state text;
begin
 if p_world is distinct from 'real' then return jsonb_build_object('evaluated',false,'reason','real_world_only');end if;
 for x in select e.experiment_id from control.nexloop_experiments e where e.tenant_id=t
   and runtime.nexloop_experiment_status(t,e.experiment_id)='started' order by e.registered_at limit 200 loop
  begin v:=runtime.nexloop_experiment_guard(t,x.experiment_id);exception when others then get stacked diagnostics v_state=returned_sqlstate;v_failed:=v_failed+1;v_states:=v_states||v_state;continue;end;  -- review 2.3: one failure stops no other
  v_checked:=v_checked+1;
  if (v->>'stopped')::boolean then v_stopped:=v_stopped+1;end if;
 end loop;
 return jsonb_build_object('evaluated',true,'checked',v_checked,'stopped',v_stopped,'failed',v_failed,'failed_sqlstates',to_jsonb(v_states));
end $$;
