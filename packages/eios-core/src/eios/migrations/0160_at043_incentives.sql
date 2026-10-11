-- AT-043 incentives, part 1 (temporary number 0160): issuing an incentive is a governed effect Action
-- (nexloop.incentive.issue:1), budgeted at dispatch. docs/implementation/AT-043-incentive-design.md; dispatcher rulings
-- 2026-10-11: D1 (own Action), D3 (Role ceiling shape unchanged), D6 (failure appends a negative discount entry), D8.
-- D2/D4/D5/D7/D9 wait for the owner; this part does not depend on them.
--
-- 1. Catalog: IncentiveOffering / ConsumerIncentiveOffering formal objects (written by the existing governed object
--    Actions, only_edit_via_actions). runtime.nexloop_incentive_catalog asserts the exact shape, current revision,
--    activity, validity, currency and the single-issue maximum; nothing outside it is ever issued (AT-010).
-- 2. runtime.nexloop_incentive_issues: one row per governed intent (accepted → dispatching → fulfilled | failed |
--    unknown; or refused before any provider request). Business fields are immutable; the intent id is the governed
--    entry's intent. A control snapshot (consumer and action scopes, catalog object revisions, the incentive budget) is
--    captured at acceptance from stored rows.
-- 3. Human entry: capability incentive.issue / operation issue_incentive on the 0124 registry (human only). It checks
--    the catalog at once (no intent outside the catalog) and records the intent; nothing is reserved yet.
-- 4. Executor port authz.nexloop_incentive_command (nexloop-incentive-v1; EXECUTE on nexloop.incentive.execute:1, granted by
--    trusted configuration to the incentive issuer service; domain worker services only, never Agents, Run credentials or browser sessions):
--    due    - accepted intents, unknown ones and dispatching ones whose lease expired (to be queried, never re-sent);
--    admit  - at dispatch, in one transaction under the intent's row lock: consumer not erasing, owner controls
--             (authz.nexloop_assert_dispatch_controls on the stored snapshot: pause, later control event, changed
--             catalog object), contact restriction with a notification (ADR-023 §3 / NX-026 D6: refused, one intent
--             is one provider request), catalog recheck, the per-Consumer period limit, then the tenant incentive
--             budget (authz.nexloop_reserve_budget 'incentive:<intent>'; NXB01 / NXB03 refuse). A refusal is recorded
--             with its code and the provider gets zero requests; an admitted intent leases a fence;
--    observe - the provider's answer under the current fence: fulfilled (settled fully_used), failed (settled released:
--             a ':settle' release row and, D6, a negative discount cost entry 'discount:incentive:<intent>:settle'),
--             unknown (kept reserved; the executor queries the provider by intent id and never issues again);
--    status - counts and one intent's state, without notification text.
-- 5. Cost entries: basis 'budget_release' may carry a negative amount (D6); every other basis stays >= 0.
-- Published migrations are not edited.

-- 5. Cost entries -----------------------------------------------------------------------------------------------------
alter table runtime.nexloop_cost_entries drop constraint nexloop_cost_entries_basis_check,
 add constraint nexloop_cost_entries_basis_check check(basis in ('provider_reported','configured_rate','unpriced','budget_reservation','operator_entered','budget_release'));
alter table runtime.nexloop_cost_entries drop constraint nexloop_cost_entries_amount_check,
 add constraint nexloop_cost_entries_amount_check check(amount>=0 or (basis='budget_release' and amount<0));

-- 1. Catalog ----------------------------------------------------------------------------------------------------------
create function runtime.nexloop_incentive_catalog(p_tenant text,p_world text,p_consumer text,p_offering text,p_revision bigint,p_binding text,
  p_amount bigint,p_currency text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare o ontology.objects;b ontology.objects;p jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into o from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='IncentiveOffering' and object_id=p_offering;
 if not found or o.nexloop_revision is distinct from p_revision then raise exception 'incentive outside catalog: offering' using errcode='42501';end if;
 p:=o.properties;
 if (select array_agg(k order by k) from jsonb_object_keys(p) k) is distinct from
   array['active','currency','eligibility','incentive_code','kind','max_amount_minor','per_consumer_period_limit_minor','title','valid_until']
  or p->'active' is distinct from 'true'::jsonb or p->>'kind' is distinct from 'fixed_amount'
  or coalesce(p->>'incentive_code','')!~'^[a-z0-9][a-z0-9._-]{0,63}$' or coalesce(p->>'currency','')!~'^[A-Z]{3}$'
  or coalesce(p->>'max_amount_minor','')!~'^[1-9][0-9]{0,15}$' or coalesce(p->>'per_consumer_period_limit_minor','')!~'^[1-9][0-9]{0,15}$'
  or jsonb_typeof(p->'title') is distinct from 'string' or length(p->>'title') not between 1 and 256
  or jsonb_typeof(p->'eligibility') is distinct from 'string' or length(p->>'eligibility') not between 1 and 128
  or coalesce(p->>'valid_until','')!~'(Z|\+00:00)$' or (p->>'valid_until')::timestamptz<=clock_timestamp() then
  raise exception 'incentive outside catalog: offering inactive or invalid' using errcode='42501';end if;
 select * into b from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='ConsumerIncentiveOffering' and object_id=p_binding;
 if not found or b.properties is distinct from jsonb_build_object('consumer_id',p_consumer,'offering_id',p_offering,'offering_revision',p_revision,
   'source_principal',b.properties->>'source_principal','active',true) or coalesce(b.properties->>'source_principal','')='' then
  raise exception 'incentive outside catalog: binding' using errcode='42501';end if;
 if p_currency is distinct from p->>'currency' or p_amount is null or p_amount<1 or p_amount>(p->>'max_amount_minor')::bigint then
  raise exception 'incentive outside catalog: amount or currency' using errcode='42501';end if;
 return jsonb_build_object('incentive_code',p->>'incentive_code','currency',p->>'currency','max_amount_minor',(p->>'max_amount_minor')::bigint,
  'per_consumer_period_limit_minor',(p->>'per_consumer_period_limit_minor')::bigint,'offering_revision',o.nexloop_revision,'binding_revision',b.nexloop_revision);
end $$;
alter function runtime.nexloop_incentive_catalog(text,text,text,text,bigint,text,bigint,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_incentive_catalog(text,text,text,text,bigint,text,bigint,text) from public;

-- 2. Intents ----------------------------------------------------------------------------------------------------------
create table runtime.nexloop_incentive_issues (
 tenant_id text not null,world text not null,intent_id text not null check(intent_id~'^[A-Za-z0-9._:-]{1,190}$'),
 consumer_id text not null check(consumer_id~'^[a-f0-9]{64}$'),offering_id text not null check(offering_id~'^[a-f0-9]{64}$'),
 offering_revision bigint not null check(offering_revision>=1),binding_id text not null check(binding_id~'^[a-f0-9]{64}$'),
 amount_minor bigint not null check(amount_minor>=1),currency text not null check(currency~'^[A-Z]{3}$'),
 notification_text text not null check(length(notification_text)<=2000),
 initiator text not null check(initiator in ('human','agent')),principal_id text not null,control_snapshot jsonb not null check(jsonb_typeof(control_snapshot)='object'),
 state text not null default 'accepted' check(state in ('accepted','dispatching','unknown','fulfilled','failed','refused')),
 refusal_code text check(refusal_code~'^[a-z_]{1,64}$'),fence bigint not null default 0 check(fence>=0),lease_until timestamptz,
 provider_reference text check(char_length(provider_reference) between 1 and 200),
 created_at timestamptz not null default clock_timestamp(),admitted_at timestamptz,finished_at timestamptz,updated_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,intent_id),
 check((state='refused')=(refusal_code is not null)),check((state in ('fulfilled','failed','refused'))=(finished_at is not null))
);
create index nexloop_incentive_issues_due on runtime.nexloop_incentive_issues(tenant_id,world,state,updated_at) where state in ('accepted','dispatching','unknown');
create index nexloop_incentive_issues_consumer on runtime.nexloop_incentive_issues(tenant_id,world,consumer_id,admitted_at);
alter table runtime.nexloop_incentive_issues owner to nexloop_owner;
alter table runtime.nexloop_incentive_issues enable row level security;
alter table runtime.nexloop_incentive_issues force row level security;
create policy tenant_boundary on runtime.nexloop_incentive_issues to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_incentive_issues from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;
-- What was asked for never changes; only the execution columns move, and a terminal intent never moves again.
create function runtime.nexloop_incentive_issue_guard() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if tg_op='DELETE' then
  if runtime.nexloop_erasure_permits(current_user::text,'runtime.nexloop_incentive_issues','DELETE',to_jsonb(old),null) then return old;end if;
  raise exception 'incentive intents are never deleted' using errcode='42501';end if;
 if (to_jsonb(new)-array['state','refusal_code','fence','lease_until','provider_reference','admitted_at','finished_at','updated_at','notification_text'])
   is distinct from (to_jsonb(old)-array['state','refusal_code','fence','lease_until','provider_reference','admitted_at','finished_at','updated_at','notification_text'])
  or (new.notification_text is distinct from old.notification_text and new.notification_text<>'')
  or (old.state in ('fulfilled','failed','refused') and (new.state,new.fence) is distinct from (old.state,old.fence)) then
  raise exception 'incentive intent is immutable' using errcode='42501';end if;
 return new;
end $$;
alter function runtime.nexloop_incentive_issue_guard() owner to nexloop_owner;
revoke all on function runtime.nexloop_incentive_issue_guard() from public;
create trigger at043_issue_guard before update or delete on runtime.nexloop_incentive_issues for each row execute function runtime.nexloop_incentive_issue_guard();

-- 3. Human entry (0124 registry) --------------------------------------------------------------------------------------
create function control.nexloop_governed_issue_incentive(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_amount bigint;v_rev bigint;cat jsonb;v_head bigint;old runtime.nexloop_incentive_issues;v_consumer text:=body->>'consumer_id';
begin
 if exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','consumer_id','incentive_offering_id','offering_revision',
   'binding_id','amount_minor','currency','notification_text'))
  or coalesce(v_consumer,'')!~'^[a-f0-9]{64}$' or coalesce(body->>'incentive_offering_id','')!~'^[a-f0-9]{64}$' or coalesce(body->>'binding_id','')!~'^[a-f0-9]{64}$'
  or jsonb_typeof(body->'offering_revision') is distinct from 'number' or coalesce(body->>'offering_revision','')!~'^[1-9][0-9]{0,15}$'
  or jsonb_typeof(body->'amount_minor') is distinct from 'string' or coalesce(body->>'amount_minor','')!~'^[1-9][0-9]{0,15}$'
  or coalesce(body->>'currency','')!~'^[A-Z]{3}$' or jsonb_typeof(body->'notification_text') is distinct from 'string' or length(body->>'notification_text')>2000 then
  raise exception 'incentive issue invalid' using errcode='22023';end if;
 v_amount:=(body->>'amount_minor')::bigint;v_rev:=(body->>'offering_revision')::bigint;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into old from runtime.nexloop_incentive_issues where tenant_id=p_tenant and world=p_world and intent_id=p_intent;
 if found then
  if (old.consumer_id,old.offering_id,old.offering_revision,old.binding_id,old.amount_minor,old.currency)
    is distinct from (v_consumer,body->>'incentive_offering_id',v_rev,body->>'binding_id',v_amount,body->>'currency') then
   raise exception 'incentive intent reused with other parameters' using errcode='NXB02';end if;
  return jsonb_build_object('intent_id',old.intent_id,'state',old.state,'replayed',true);
 end if;
 if not exists(select 1 from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='Consumer' and object_id=v_consumer)
  or runtime.nexloop_consumer_erasing(p_tenant,p_world,v_consumer) then raise exception 'consumer unavailable' using errcode='42501';end if;
 -- Outside the catalog nothing is accepted (AT-010): no intent, no reservation, no provider request.
 cat:=runtime.nexloop_incentive_catalog(p_tenant,p_world,v_consumer,body->>'incentive_offering_id',v_rev,body->>'binding_id',v_amount,body->>'currency');
 perform set_config('eios.tenant_id',p_tenant,true);
 select revision into v_head from control.nexloop_control_heads where tenant_id=p_tenant and world=p_world;
 insert into runtime.nexloop_incentive_issues(tenant_id,world,intent_id,consumer_id,offering_id,offering_revision,binding_id,amount_minor,currency,
   notification_text,initiator,principal_id,control_snapshot)
  values(p_tenant,p_world,p_intent,v_consumer,body->>'incentive_offering_id',v_rev,body->>'binding_id',v_amount,body->>'currency',body->>'notification_text',
   'human',p_principal,jsonb_build_object('control_revision',coalesce(v_head,0),
    'scopes',jsonb_build_array(jsonb_build_object('kind','consumer','ref',v_consumer),jsonb_build_object('kind','consumer','ref','consumer:'||v_consumer),
      jsonb_build_object('kind','action_type','ref','nexloop.incentive.issue')),
    'goals','[]'::jsonb,
    'objects',jsonb_build_array(jsonb_build_object('type_name','IncentiveOffering','object_id',body->>'incentive_offering_id','revision',v_rev),
      jsonb_build_object('type_name','ConsumerIncentiveOffering','object_id',body->>'binding_id','revision',(cat->>'binding_revision')::bigint)),
    'budgets',jsonb_build_array('incentive')));
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('incentive-issue:'||p_intent,'nexloop.incentive.issue.accepted',p_tenant,p_principal,'incentive',p_intent,
   jsonb_build_object('consumer_id',v_consumer,'incentive_code',cat->>'incentive_code','amount_minor',v_amount,'currency',body->>'currency'),clock_timestamp())
  on conflict do nothing;
 return jsonb_build_object('intent_id',p_intent,'state','accepted','replayed',false);
end $$;
alter function control.nexloop_governed_issue_incentive(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_governed_issue_incentive(text,text,text,text,text,jsonb) from public;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('incentive.issue','issue_incentive','human','control.nexloop_governed_issue_incentive(text,text,text,text,text,jsonb)','NX-027');

-- 4. Settlement of one incentive reservation (fulfilled: fully used; failed: released, D6 negative discount entry) -------
create function runtime.nexloop_incentive_settle(p_tenant text,p_world text,p_intent text,p_outcome text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare res control.nexloop_budget_consumption;e runtime.nexloop_cost_entries;v_id text:='incentive:'||p_intent;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into res from control.nexloop_budget_consumption where tenant_id=p_tenant and world=p_world and budget_kind='incentive' and consumption_id=v_id;
 if not found or exists(select 1 from runtime.nexloop_budget_settlements where tenant_id=p_tenant and world=p_world and consumption_id=v_id) then
  return jsonb_build_object('settled',false);end if;
 if p_outcome='released' then
  insert into control.nexloop_budget_consumption(tenant_id,world,budget_kind,consumption_id,amount,unit,source_ref,principal_id,recorded_at)
   values(p_tenant,p_world,'incentive',v_id||':settle',-res.amount,res.unit,'settle:'||res.source_ref,'nexloop:budget-settlement',res.recorded_at);
  e.tenant_id:=p_tenant;e.world:=p_world;e.entry_id:='discount:'||v_id||':settle';e.data_mode:=runtime.nexloop_cost_data_mode(p_world);
  e.cost_kind:='discount';e.units:=1;e.amount:=-res.amount;e.currency:=res.unit;e.amount_unit:='minor';e.basis:='budget_release';
  e.source_ref:='incentive:'||v_id||'@settle';e.occurred_at:=res.recorded_at;e.recorded_by:='nexloop:budget-settlement';
  perform runtime.nexloop_cost_record(e);
  perform set_config('eios.tenant_id',p_tenant,true);
 end if;
 insert into runtime.nexloop_budget_settlements(tenant_id,world,consumption_id,reserved,actual,released,unit,outcome)
  values(p_tenant,p_world,v_id,res.amount,case when p_outcome='released' then 0 else res.amount end,case when p_outcome='released' then res.amount else 0 end,
   res.unit,p_outcome);
 return jsonb_build_object('settled',true,'outcome',p_outcome);
end $$;
alter function runtime.nexloop_incentive_settle(text,text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_incentive_settle(text,text,text,text) from public;

-- 4. Executor port --------------------------------------------------------------------------------------------------
create function authz.nexloop_incentive_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-incentive-v1',
  'eios:action:nexloop.incentive.execute:1',array['nexloop_domain_worker']);c jsonb:=p_payload::jsonb;v jsonb;i runtime.nexloop_incentive_issues;
 cat jsonb;lim control.nexloop_budget_limits;v_used bigint;v_code text;v_limit integer;v_state text;
begin
 perform set_config('eios.tenant_id',t,true);
 v_limit:=coalesce((c->>'limit')::integer,50);
 if v_limit not between 1 and 200 then raise exception 'incentive command invalid' using errcode='22023';end if;
 if c->>'verb'='due' then
  select coalesce(jsonb_agg(jsonb_build_object('intent_id',x.intent_id,'state',x.state) order by x.updated_at),'[]'::jsonb) into v
   from (select intent_id,state,updated_at from runtime.nexloop_incentive_issues where tenant_id=t and world=p_world
     and (state in ('accepted','unknown') or (state='dispatching' and lease_until<=clock_timestamp())) order by updated_at limit v_limit) x;
  return jsonb_build_object('intents',v);
 end if;
 if coalesce(c->>'intent_id','')!~'^[A-Za-z0-9._:-]{1,190}$' then raise exception 'incentive command invalid' using errcode='22023';end if;
 select * into i from runtime.nexloop_incentive_issues where tenant_id=t and world=p_world and intent_id=c->>'intent_id' for update;
 if not found then raise exception 'incentive intent unavailable' using errcode='42501';end if;
 if c->>'verb'='admit' then
  -- Only an accepted intent is ever admitted; dispatching / unknown are queried, never sent again.
  if i.state<>'accepted' then return jsonb_build_object('intent_id',i.intent_id,'admitted',false,'state',i.state,'fence',i.fence);end if;
  begin
   if runtime.nexloop_consumer_erasing(t,p_world,i.consumer_id) then raise exception 'consumer erasing' using errcode='42501',hint='consumer_erasing';end if;
   perform set_config('eios.tenant_id',t,true);
   begin
    perform authz.nexloop_assert_dispatch_controls(p_digest,p_world,i.control_snapshot);
   exception when sqlstate 'NXC01' or sqlstate 'NXC02' or sqlstate 'NXC04' then
    raise exception 'control' using errcode='42501',hint=case sqlstate when 'NXC01' then 'control_paused' when 'NXC02' then 'control_revision_stale' else 'object_revision_stale' end;
   end;
   perform set_config('eios.tenant_id',t,true);
   -- ADR-023 §3 / NX-026 D6: the issue itself reaches no one; an attached notification does, and one intent cannot be split.
   if i.notification_text<>'' and exists(select 1 from control.nexloop_contact_restrictions r where r.tenant_id=t and r.world=p_world
     and r.consumer_id=i.consumer_id and r.active) then raise exception 'notification' using errcode='42501',hint='attached_notification';end if;
   begin
    cat:=runtime.nexloop_incentive_catalog(t,p_world,i.consumer_id,i.offering_id,i.offering_revision,i.binding_id,i.amount_minor,i.currency);
   exception when insufficient_privilege then raise exception 'catalog' using errcode='42501',hint='outside_catalog_terms';
   end;
   perform set_config('eios.tenant_id',t,true);
   select * into lim from control.nexloop_budget_limits where tenant_id=t and world=p_world and budget_kind='incentive';
   if found then
    select coalesce(sum(amount_minor),0) into v_used from runtime.nexloop_incentive_issues where tenant_id=t and world=p_world and consumer_id=i.consumer_id
     and offering_id=i.offering_id and state in ('dispatching','unknown','fulfilled') and admitted_at>=lim.period_start and admitted_at<lim.period_end;
    if v_used+i.amount_minor>(cat->>'per_consumer_period_limit_minor')::bigint then
     raise exception 'consumer limit' using errcode='42501',hint='consumer_incentive_limit';end if;
   end if;
   begin
    perform authz.nexloop_reserve_budget(p_digest,p_world,'incentive','incentive:'||i.intent_id,i.amount_minor::text,i.currency,'effect:'||i.intent_id);
   exception when sqlstate 'NXB01' then raise exception 'budget' using errcode='42501',hint='budget_exhausted';
    when sqlstate 'NXB03' then raise exception 'budget' using errcode='42501',hint='budget_unconfigured';
   end;
  exception when insufficient_privilege then
   get stacked diagnostics v_code=pg_exception_hint;
   if coalesce(v_code,'')='' then raise;end if;
  end;
  perform set_config('eios.tenant_id',t,true);
  if v_code is not null and v_code<>'' then
   update runtime.nexloop_incentive_issues set state='refused',refusal_code=v_code,finished_at=clock_timestamp(),updated_at=clock_timestamp()
    where tenant_id=t and world=p_world and intent_id=i.intent_id;
   insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
    values('incentive-refused:'||i.intent_id,'nexloop.incentive.issue.refused',t,'service:'||((p_text::jsonb)->>'principal_id'),'incentive',i.intent_id,
     jsonb_build_object('code',v_code),clock_timestamp()) on conflict do nothing;
   return jsonb_build_object('intent_id',i.intent_id,'admitted',false,'state','refused','code',v_code);
  end if;
  update runtime.nexloop_incentive_issues set state='dispatching',fence=fence+1,lease_until=clock_timestamp()+interval '30 seconds',
   admitted_at=clock_timestamp(),updated_at=clock_timestamp() where tenant_id=t and world=p_world and intent_id=i.intent_id returning * into i;
  return jsonb_build_object('intent_id',i.intent_id,'admitted',true,'state',i.state,'fence',i.fence,
   'request',jsonb_build_object('intent_id',i.intent_id,'consumer_ref',i.consumer_id,'incentive_code',cat->>'incentive_code',
    'amount_minor',i.amount_minor::text,'currency',i.currency,'notification_text',i.notification_text));
 elsif c->>'verb'='observe' then
  if c->>'outcome' not in ('fulfilled','failed','unknown') or jsonb_typeof(c->'fence') is distinct from 'number'
   or (c ? 'provider_reference' and coalesce(c->>'provider_reference','')!~'^[A-Za-z0-9._:-]{1,200}$') then
   raise exception 'incentive command invalid' using errcode='22023';end if;
  if i.state not in ('dispatching','unknown') or i.fence<>(c->>'fence')::bigint then
   if i.state in ('fulfilled','failed') and i.state=c->>'outcome' then return jsonb_build_object('intent_id',i.intent_id,'state',i.state,'replayed',true);end if;
   raise exception 'incentive observation fenced' using errcode='40001';end if;
  v_state:=c->>'outcome';
  update runtime.nexloop_incentive_issues set state=v_state,provider_reference=coalesce(c->>'provider_reference',provider_reference),
   lease_until=null,finished_at=case when v_state in ('fulfilled','failed') then clock_timestamp() end,updated_at=clock_timestamp()
   where tenant_id=t and world=p_world and intent_id=i.intent_id;
  if v_state='fulfilled' then perform runtime.nexloop_incentive_settle(t,p_world,i.intent_id,'fully_used');
  elsif v_state='failed' then perform runtime.nexloop_incentive_settle(t,p_world,i.intent_id,'released');end if;
  perform set_config('eios.tenant_id',t,true);
  insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
   values('incentive-'||v_state||':'||i.intent_id||':'||i.fence,'nexloop.incentive.issue.'||v_state,t,'service:'||((p_text::jsonb)->>'principal_id'),'incentive',
    i.intent_id,jsonb_build_object('fence',i.fence),clock_timestamp()) on conflict do nothing;
  return jsonb_build_object('intent_id',i.intent_id,'state',v_state,'fence',i.fence);
 elsif c->>'verb'='status' then
  return jsonb_build_object('intent_id',i.intent_id,'state',i.state,'refusal_code',i.refusal_code,'fence',i.fence,'amount_minor',i.amount_minor::text,
   'currency',i.currency,'provider_reference',i.provider_reference);
 end if;
 raise exception 'incentive command invalid' using errcode='22023';
end $$;
alter function authz.nexloop_incentive_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_incentive_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_incentive_command(text,text,text,text,text) to nexloop_domain_worker;
