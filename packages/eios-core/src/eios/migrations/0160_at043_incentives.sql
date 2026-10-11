-- AT-043 incentives (temporary number 0160): issuing an incentive is a governed effect Action (nexloop.incentive.issue:1),
-- budgeted at dispatch. docs/implementation/AT-043-incentive-design.md; dispatcher rulings 2026-10-11 (D1 own Action, D3 Role
-- ceiling shape unchanged, D6 negative discount entry on failure, D8 incentives in the v6 context) and owner decisions
-- 2026-10-11 "全部按推荐" (D2, D4, D5, D7, D9; D2's risk level is recorded as low here, see the implementation note §4).
--
-- 1. Catalog: IncentiveOffering / ConsumerIncentiveOffering formal objects (written by the existing governed object
--    Actions, only_edit_via_actions). runtime.nexloop_incentive_catalog asserts the exact shape, current revision,
--    activity, validity, currency and the single-issue maximum (NXI01 outside it); nothing outside it is accepted (AT-010).
--    D4: the owner too issues only from the catalog (adding or changing an entry is a governed object Action first).
-- 2. runtime.nexloop_incentive_issues: one row per governed intent (accepted -> dispatching -> fulfilled | failed | unknown;
--    or refused before any provider request), with its origin (human, reply, plan, agent). Business fields are immutable.
--    A control snapshot (consumer and action scopes, catalog object revisions, the incentive budget) is captured at
--    acceptance from stored rows (runtime.nexloop_incentive_accept, shared by both entries).
-- 3. Entries: (a) the human owner through the 0124 registry (incentive.issue / issue_incentive, human only); (b) the Agent
--    through the runtime guard (authz.nexloop_run_incentive_command, nexloop.incentive.run_submit:1 held by the runtime
--    worker): the Run's live activation under the caller's own task lease and the exact command (as the NX-024
--    run-outcome port), the Run credential naming nexloop.incentive.issue:1, the Role ceiling for a Role Run (D3), the
--    Run's own Consumer; origin 'reply' for a message Run answering that inbound message, 'plan' for a plan reevaluation
--    Run, otherwise 'agent'; one issue per Run and offering. Nothing is reserved at acceptance.
-- 4. Executor port authz.nexloop_incentive_command (nexloop-incentive-v1; EXECUTE on nexloop.incentive.execute:1, granted by
--    trusted configuration to the incentive issuer service; domain worker services only, never Agents, Run credentials
--    or browser sessions):
--    due    - accepted intents, unknown ones and dispatching ones whose lease expired (to be queried, never re-sent);
--    admit  - at dispatch, in one transaction under the intent's row lock: consumer not erasing, owner controls
--             (authz.nexloop_assert_dispatch_controls on the stored snapshot), a notification under a contact restriction
--             (ADR-023 §3 / NX-026 D6: refused whole), D7 (a restricted customer receives only what the owner issues or
--             what answers the customer's own message: 'plan' and 'agent' origins are refused), catalog recheck, the
--             per-Consumer period limit, then the tenant incentive budget (authz.nexloop_reserve_budget 'incentive:<intent>';
--             NXB01 / NXB03 refuse). A refusal is recorded with its code; the provider gets zero requests;
--    observe - the provider's answer under the current fence: fulfilled (settled fully_used), failed (released: a ':settle'
--             release row and, D6, a negative discount cost entry), unknown (kept reserved; queried, never issued again);
--    status - one intent's state, without notification text.
-- 5. Cost entries: basis 'budget_release' may carry a negative amount (D6); every other basis stays >= 0.
-- 6. D5: a commitment Claim promising an incentive that the customer's catalog does not cover is an exception for the owner
--    (unauthorized_incentive_promise), never a Commitment and never issued (NX-026 registration, 0127 body + one check).
-- 7. D8: the incentives the Consumer may still be issued are open-work items of Context v6 (catalog entry and issuable range,
--    never the budget balance), re-derived by SQL at bind (0113 bodies + one branch each).
-- D9: no real incentive channel; only loopback providers exist (nexloop_eios.incentives).
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
 if not found or o.nexloop_revision is distinct from p_revision then raise exception 'incentive outside catalog: offering' using errcode='NXI01';end if;
 p:=o.properties;
 if (select array_agg(k order by k) from jsonb_object_keys(p) k) is distinct from
   array['active','currency','eligibility','incentive_code','kind','max_amount_minor','per_consumer_period_limit_minor','title','valid_until']
  or p->'active' is distinct from 'true'::jsonb or p->>'kind' is distinct from 'fixed_amount'
  or coalesce(p->>'incentive_code','')!~'^[a-z0-9][a-z0-9._-]{0,63}$' or coalesce(p->>'currency','')!~'^[A-Z]{3}$'
  or coalesce(p->>'max_amount_minor','')!~'^[1-9][0-9]{0,15}$' or coalesce(p->>'per_consumer_period_limit_minor','')!~'^[1-9][0-9]{0,15}$'
  or jsonb_typeof(p->'title') is distinct from 'string' or length(p->>'title') not between 1 and 256
  or jsonb_typeof(p->'eligibility') is distinct from 'string' or length(p->>'eligibility') not between 1 and 128
  or coalesce(p->>'valid_until','')!~'(Z|\+00:00)$' or (p->>'valid_until')::timestamptz<=clock_timestamp() then
  raise exception 'incentive outside catalog: offering inactive or invalid' using errcode='NXI01';end if;
 select * into b from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='ConsumerIncentiveOffering' and object_id=p_binding;
 if not found or b.properties is distinct from jsonb_build_object('consumer_id',p_consumer,'offering_id',p_offering,'offering_revision',p_revision,
   'source_principal',b.properties->>'source_principal','active',true) or coalesce(b.properties->>'source_principal','')='' then
  raise exception 'incentive outside catalog: binding' using errcode='NXI01';end if;
 if p_currency is distinct from p->>'currency' or p_amount is null or p_amount<1 or p_amount>(p->>'max_amount_minor')::bigint then
  raise exception 'incentive outside catalog: amount or currency' using errcode='NXI01';end if;
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
 initiator text not null check(initiator in ('human','agent')),principal_id text not null,
 origin text not null check(origin in ('human','reply','plan','agent')),run_id uuid,reply_to_message_id text check(reply_to_message_id~'^[a-f0-9]{64}$'),control_snapshot jsonb not null check(jsonb_typeof(control_snapshot)='object'),
 state text not null default 'accepted' check(state in ('accepted','dispatching','unknown','fulfilled','failed','refused')),
 refusal_code text check(refusal_code~'^[a-z_]{1,64}$'),fence bigint not null default 0 check(fence>=0),lease_until timestamptz,
 provider_reference text check(char_length(provider_reference) between 1 and 200),
 created_at timestamptz not null default clock_timestamp(),admitted_at timestamptz,finished_at timestamptz,updated_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,intent_id),
 check((initiator='human')=(origin='human') and (origin='human')=(run_id is null) and (origin='reply')=(reply_to_message_id is not null)),
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

-- 3. Acceptance (shared by the human entry and the Run entry) -----------------------------------------------------------
-- Catalog first (outside it nothing is accepted: no intent, no reservation, no provider request; AT-010), then the
-- intent with a control snapshot taken from stored rows. A replay with the same parameters returns the stored intent.
create function runtime.nexloop_incentive_accept(p_tenant text,p_world text,p_intent text,p_consumer text,p_offering text,p_revision bigint,p_binding text,
  p_amount bigint,p_currency text,p_notification text,p_initiator text,p_principal text,p_origin text,p_run uuid,p_reply_to text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare cat jsonb;v_head bigint;old runtime.nexloop_incentive_issues;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into old from runtime.nexloop_incentive_issues where tenant_id=p_tenant and world=p_world and intent_id=p_intent;
 if found then
  if (old.consumer_id,old.offering_id,old.offering_revision,old.binding_id,old.amount_minor,old.currency,old.origin)
    is distinct from (p_consumer,p_offering,p_revision,p_binding,p_amount,p_currency,p_origin) then
   raise exception 'incentive intent reused with other parameters' using errcode='NXB02';end if;
  return jsonb_build_object('intent_id',old.intent_id,'state',old.state,'replayed',true);
 end if;
 if not exists(select 1 from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='Consumer' and object_id=p_consumer)
  or runtime.nexloop_consumer_erasing(p_tenant,p_world,p_consumer) then raise exception 'consumer unavailable' using errcode='42501';end if;
 cat:=runtime.nexloop_incentive_catalog(p_tenant,p_world,p_consumer,p_offering,p_revision,p_binding,p_amount,p_currency);
 perform set_config('eios.tenant_id',p_tenant,true);
 select revision into v_head from control.nexloop_control_heads where tenant_id=p_tenant and world=p_world;
 insert into runtime.nexloop_incentive_issues(tenant_id,world,intent_id,consumer_id,offering_id,offering_revision,binding_id,amount_minor,currency,
   notification_text,initiator,principal_id,origin,run_id,reply_to_message_id,control_snapshot)
  values(p_tenant,p_world,p_intent,p_consumer,p_offering,p_revision,p_binding,p_amount,p_currency,p_notification,p_initiator,p_principal,p_origin,p_run,p_reply_to,
   jsonb_build_object('control_revision',coalesce(v_head,0),
    'scopes',jsonb_build_array(jsonb_build_object('kind','consumer','ref',p_consumer),jsonb_build_object('kind','consumer','ref','consumer:'||p_consumer),
      jsonb_build_object('kind','action_type','ref','nexloop.incentive.issue')),
    'goals','[]'::jsonb,
    'objects',jsonb_build_array(jsonb_build_object('type_name','IncentiveOffering','object_id',p_offering,'revision',p_revision),
      jsonb_build_object('type_name','ConsumerIncentiveOffering','object_id',p_binding,'revision',(cat->>'binding_revision')::bigint)),
    'budgets',jsonb_build_array('incentive')));
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('incentive-issue:'||p_intent,'nexloop.incentive.issue.accepted',p_tenant,p_principal,'incentive',p_intent,
   jsonb_build_object('consumer_id',p_consumer,'incentive_code',cat->>'incentive_code','amount_minor',p_amount,'currency',p_currency,'origin',p_origin),clock_timestamp())
  on conflict do nothing;
 return jsonb_build_object('intent_id',p_intent,'state','accepted','replayed',false);
end $$;
alter function runtime.nexloop_incentive_accept(text,text,text,text,text,bigint,text,bigint,text,text,text,text,text,uuid,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_incentive_accept(text,text,text,text,text,bigint,text,bigint,text,text,text,text,text,uuid,text) from public;

-- Shape of the request parameters (human body and Run tool arguments alike).
create function runtime.nexloop_incentive_parameters_valid(p jsonb,p_keys text[]) returns boolean
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select jsonb_typeof(p)='object' and not exists(select 1 from jsonb_object_keys(p) k where not (k=any(p_keys)))
  and coalesce(p->>'incentive_offering_id','')~'^[a-f0-9]{64}$' and coalesce(p->>'binding_id','')~'^[a-f0-9]{64}$'
  and jsonb_typeof(p->'offering_revision')='number' and coalesce(p->>'offering_revision','')~'^[1-9][0-9]{0,15}$'
  and jsonb_typeof(p->'amount_minor')='string' and coalesce(p->>'amount_minor','')~'^[1-9][0-9]{0,15}$'
  and coalesce(p->>'currency','')~'^[A-Z]{3}$' and jsonb_typeof(p->'notification_text')='string' and length(p->>'notification_text')<=2000
$$;
alter function runtime.nexloop_incentive_parameters_valid(jsonb,text[]) owner to nexloop_owner;

-- 3a. Human entry (0124 registry) -------------------------------------------------------------------------------------
create function control.nexloop_governed_issue_incentive(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if not runtime.nexloop_incentive_parameters_valid(body,array['operation','request_id','consumer_id','incentive_offering_id','offering_revision','binding_id',
   'amount_minor','currency','notification_text']) or coalesce(body->>'consumer_id','')!~'^[a-f0-9]{64}$' then
  raise exception 'incentive issue invalid' using errcode='22023';end if;
 return runtime.nexloop_incentive_accept(p_tenant,p_world,p_intent,body->>'consumer_id',body->>'incentive_offering_id',(body->>'offering_revision')::bigint,
  body->>'binding_id',(body->>'amount_minor')::bigint,body->>'currency',body->>'notification_text','human',p_principal,'human',null,null);
end $$;
alter function control.nexloop_governed_issue_incentive(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_governed_issue_incentive(text,text,text,text,text,jsonb) from public;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('incentive.issue','issue_incentive','human','control.nexloop_governed_issue_incentive(text,text,text,text,text,jsonb)','NX-027');

-- 3b. Run entry: the Agent's tool through the runtime guard (nexloop-run-incentive-v1; EXECUTE on
-- nexloop.incentive.run_submit:1, held by the runtime worker service). As the NX-024 run-outcome port: the Run's live
-- activation under the caller's own current task lease and the exact stored command. The Agent may issue only inside
-- its Run (the Run credential's allowed resources name nexloop.incentive.issue:1) and, for a Role Run, inside its Role
-- execution ceiling (action_resources; D3: the ceiling shape is unchanged). The Consumer is the Run's own. Origin
-- (D7): a message Run answers that inbound message ('reply'); a plan reevaluation Run is 'plan'; anything else 'agent'.
-- One issue per Run and offering: the business key is derived, never chosen by the Agent.
create function authz.nexloop_run_incentive_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-run-incentive-v1','eios:action:nexloop.incentive.run_submit:1',
  array['nexloop_scheduler','nexloop_domain_worker']);
 c jsonb:=p_payload::jsonb;p jsonb:=c->'parameters';act authz.nexloop_runtime_activations%rowtype;j runtime.jobs%rowtype;b authz.nexloop_runtime_run_bindings%rowtype;
 v_command jsonb;v_consumer text;v_origin text:='agent';v_reply text;rb authz.nexloop_role_run_bindings%rowtype;ceiling jsonb;
begin
 select * into act from authz.nexloop_runtime_activations where activation_ref=c->>'activation_ref' and tenant_id=t and world=p_world;
 if not found then raise exception 'incentive activation unavailable' using errcode='42501';end if;
 select * into j from runtime.jobs where tenant_id=t and job_id=act.task_id for share;
 if not found or j.status<>'running' or j.lease_until<=clock_timestamp() or j.lease_credential is distinct from p_digest
  or act.lease_credential is distinct from p_digest or act.fence is distinct from j.fencing_token then
  raise exception 'incentive lease unavailable' using errcode='42501';end if;
 select * into b from authz.nexloop_runtime_run_bindings where run_id=act.run_id;
 v_command:=(c->>'command_text')::jsonb;
 if b.command_digest is distinct from encode(sha256(convert_to(c->>'command_text','UTF8')),'hex') or v_command->>'run_id' is distinct from act.run_id::text then
  raise exception 'incentive command mismatch' using errcode='42501';end if;
 if not runtime.nexloop_incentive_parameters_valid(p,array['incentive_offering_id','offering_revision','binding_id','amount_minor','currency','notification_text']) then
  raise exception 'incentive issue invalid' using errcode='22023';end if;
 v_consumer:=substring(coalesce(v_command->>'consumer_ref','') from '^consumer:([a-f0-9]{64})$');
 if v_consumer is null then raise exception 'incentive needs the Run''s Consumer' using errcode='42501';end if;
 if not exists(select 1 from authz.nexloop_run_credentials r where r.run_id=act.run_id and r.status='active'
   and 'eios:action:nexloop.incentive.issue:1'=any(r.allowed_resources)) then raise exception 'incentive outside the Run' using errcode='42501';end if;
 perform set_config('eios.tenant_id',t,true);
 select * into rb from authz.nexloop_role_run_bindings where run_id=act.run_id;
 if found then
  select o.properties into ceiling from ontology.objects d join ontology.objects o on o.tenant_id=d.tenant_id and o.world=d.world
    and o.type_name='RoleExecutionCeiling' and o.object_id=d.properties->>'ceiling_ref'
   where d.tenant_id=t and d.world=p_world and d.type_name='RoleDefinition' and d.object_id=rb.role_id;
  if ceiling is null or not coalesce(ceiling->'action_resources','[]'::jsonb) ? 'eios:action:nexloop.incentive.issue:1'
   or rb.consumer_id is distinct from v_consumer then raise exception 'incentive outside the Role ceiling' using errcode='42501';end if;
 end if;
 select i.message_id into v_reply from authz.nexloop_message_run_issuances i join runtime.nexloop_conversation_messages m on m.tenant_id=i.tenant_id
   and m.world=i.world and m.message_id=i.message_id join runtime.nexloop_conversations cv on cv.tenant_id=m.tenant_id and cv.world=m.world
   and cv.conversation_id=m.conversation_id
  where i.run_id=act.run_id and i.tenant_id=t and i.world=p_world and cv.consumer_id=v_consumer and m.record->>'direction' is null;
 if v_reply is not null then v_origin:='reply';
 elsif exists(select 1 from runtime.nexloop_plan_runs pr where pr.tenant_id=t and pr.world=p_world and pr.run_id=act.run_id) then v_origin:='plan';end if;
 return runtime.nexloop_incentive_accept(t,p_world,'incentive-run:'||act.run_id||':'||(p->>'incentive_offering_id'),v_consumer,p->>'incentive_offering_id',
  (p->>'offering_revision')::bigint,p->>'binding_id',(p->>'amount_minor')::bigint,p->>'currency',p->>'notification_text','agent',
  'run:'||act.run_id,v_origin,act.run_id,v_reply)||jsonb_build_object('run_id',act.run_id,'origin',v_origin);
end $$;
alter function authz.nexloop_run_incentive_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_run_incentive_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_run_incentive_command(text,text,text,text,text) to nexloop_scheduler,nexloop_domain_worker;

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
   -- D7: a restricted customer receives only what the owner issues or what answers the customer's own message; a plan
   -- reevaluation working for a KR (or any other Agent origin) does not issue to a restricted customer.
   if i.origin not in ('human','reply') and exists(select 1 from control.nexloop_contact_restrictions r where r.tenant_id=t and r.world=p_world
     and r.consumer_id=i.consumer_id and r.active) then raise exception 'restricted' using errcode='42501',hint='contact_restricted';end if;
   begin
    cat:=runtime.nexloop_incentive_catalog(t,p_world,i.consumer_id,i.offering_id,i.offering_revision,i.binding_id,i.amount_minor,i.currency);
   exception when sqlstate 'NXI01' then raise exception 'catalog' using errcode='42501',hint='outside_catalog_terms';
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

-- 6. D5: promised incentives (NX-026 registration, 0127 body replaced in place with one check) -----------------------------
-- Rule incentive-promise-v1 (versioned, deterministic, on the enterprise's own delivered words): a commitment Claim whose
-- predicate or quote names a discount, coupon, voucher, cash back, waiver or gift. It is covered only by an active, valid
-- IncentiveOffering bound to the same Consumer; a money value must fit its single-issue maximum in its currency (major
-- units of a two-decimal currency).
create function runtime.nexloop_incentive_promise(p_predicate text,p_quote text) returns boolean
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select coalesce(p_predicate,'')||' '||coalesce(p_quote,'') ~* '(优惠|折扣|打折|[0-9一二三四五六七八九]+(\.[0-9])?折|优惠券|代金券|抵用券|返现|立减|减免|红包|赠送|免单|discount|coupon|voucher|cash ?back|rebate)'
$$;
alter function runtime.nexloop_incentive_promise(text,text) owner to nexloop_owner;
create function runtime.nexloop_incentive_promise_covered(p_tenant text,p_world text,p_consumer text,p_value jsonb) returns boolean
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v boolean;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select exists(select 1 from ontology.objects b join ontology.objects o on o.tenant_id=b.tenant_id and o.world=b.world and o.type_name='IncentiveOffering'
   and o.object_id=b.properties->>'offering_id' and o.nexloop_revision::text=b.properties->>'offering_revision'
  where b.tenant_id=p_tenant and b.world=p_world and b.type_name='ConsumerIncentiveOffering' and b.properties->>'consumer_id'=p_consumer
   and b.properties->'active'='true'::jsonb and o.properties->'active'='true'::jsonb
   and coalesce(o.properties->>'valid_until','')~'(Z|\+00:00)$' and (o.properties->>'valid_until')::timestamptz>clock_timestamp()
   and (p_value->>'type' is distinct from 'money' or (p_value->'value'->>'currency'=o.properties->>'currency'
     and (p_value->'value'->>'amount')::numeric*100<=(o.properties->>'max_amount_minor')::numeric))) into v;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
alter function runtime.nexloop_incentive_promise_covered(text,text,text,jsonb) owner to nexloop_owner;
revoke all on function runtime.nexloop_incentive_promise_covered(text,text,text,jsonb) from public;
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
 -- AT-043 D5: a promised incentive must be in the customer's catalog; otherwise it is an exception for the owner, never a
 -- Commitment to fulfil and never issued automatically.
 if runtime.nexloop_incentive_promise(cl.predicate,cl.quote) and not runtime.nexloop_incentive_promise_covered(p_tenant,p_world,cl.consumer_id,cl.value) then
  perform runtime.nexloop_commitment_raise(p_tenant,p_world,'claim:'||p_claim,cl.consumer_id,'unauthorized_incentive_promise',
   jsonb_build_object('message_id',cl.source_message_id,'rule','incentive-promise-v1'));
  perform set_config('eios.tenant_id',p_tenant,true);
  insert into runtime.nexloop_commitment_events(tenant_id,world,subject_ref,kind,detail)
   values(p_tenant,p_world,'claim:'||p_claim,'skipped',jsonb_build_object('reason','unauthorized_incentive_promise'));
  return jsonb_build_object('action','skipped','reason','unauthorized_incentive_promise');
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

-- 7. D8: incentives in the v6 context (0113 open-work read and source check, bodies replaced in place) ------------------
-- One entry per bound, active, valid IncentiveOffering of the Consumer while the tenant has an incentive budget: the
-- catalog entry and the range still issuable to this Consumer in the budget period (single-issue maximum and the
-- per-Consumer period limit less what is dispatching, unknown or issued). The budget balance is never shown.
create function runtime.nexloop_incentive_catalog_for(p_tenant text,p_world text,p_consumer text)
 returns table(offering_id text,revision text,content jsonb)
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);lim control.nexloop_budget_limits;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into lim from control.nexloop_budget_limits l where l.tenant_id=p_tenant and l.world=p_world and l.budget_kind='incentive';
 if found then
  return query select o.object_id,o.nexloop_revision::text,jsonb_build_object('incentive_code',o.properties->>'incentive_code','title',o.properties->>'title',
    'kind',o.properties->>'kind','currency',o.properties->>'currency','offering_revision',o.nexloop_revision,'binding_id',b.object_id,
    'valid_until',o.properties->>'valid_until','issuable_minor',jsonb_build_object('min','1','max',r.room::text))
   from ontology.objects b join ontology.objects o on o.tenant_id=b.tenant_id and o.world=b.world and o.type_name='IncentiveOffering'
     and o.object_id=b.properties->>'offering_id' and o.nexloop_revision::text=b.properties->>'offering_revision'
   cross join lateral (select least((o.properties->>'max_amount_minor')::bigint,(o.properties->>'per_consumer_period_limit_minor')::bigint
     -coalesce((select sum(i.amount_minor) from runtime.nexloop_incentive_issues i where i.tenant_id=p_tenant and i.world=p_world
       and i.consumer_id=p_consumer and i.offering_id=o.object_id and i.state in ('dispatching','unknown','fulfilled')
       and i.admitted_at>=lim.period_start and i.admitted_at<lim.period_end),0)) room) r
   where b.tenant_id=p_tenant and b.world=p_world and b.type_name='ConsumerIncentiveOffering' and b.properties->>'consumer_id'=p_consumer
    and b.properties->'active'='true'::jsonb and o.properties->'active'='true'::jsonb and o.properties->>'kind'='fixed_amount'
    and coalesce(o.properties->>'valid_until','')~'(Z|\+00:00)$' and (o.properties->>'valid_until')::timestamptz>clock_timestamp()
    and coalesce(o.properties->>'max_amount_minor','')~'^[1-9][0-9]{0,15}$' and coalesce(o.properties->>'per_consumer_period_limit_minor','')~'^[1-9][0-9]{0,15}$'
    and r.room>=1 and clock_timestamp()>=lim.period_start and clock_timestamp()<lim.period_end;
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
end $$;
alter function runtime.nexloop_incentive_catalog_for(text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_incentive_catalog_for(text,text,text) from public;
create or replace function authz.nexloop_context_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;v_verb text:=c->>'verb';ident jsonb;v_tenant text;
 v_result jsonb;v_row control.nexloop_context_strategies%rowtype;proof jsonb;ref text;gate text;v_type text;v_prop text;v_value text;
 v_def jsonb;v_version integer;v_items jsonb:='[]'::jsonb;v_props jsonb;v_consumer text;
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker') or octet_length(p_text)>4194304 or octet_length(p_payload)>65536
  or a->>'protocol' is distinct from 'nexloop-context-read-v1' or v_verb not in ('strategy','definitions','open_work') or v_verb is null
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'context read unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-context-read-v1:'||p_text,'UTF8'),k,'sha256'),'hex') then
  raise exception 'context read unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);v_tenant:=ident->'binding'->>'tenant_id';
 if a->>'tenant_id' is distinct from v_tenant or a->>'world' is distinct from p_world then raise exception 'context read unavailable' using errcode='42501';end if;
 if v_verb='strategy' then
  if a->>'action_resource' is distinct from 'eios:action:nexloop.context.assemble:1' or c->>'strategy_id' !~ '^[a-z][a-z0-9_]{0,63}$'
   or (c ? 'version' and jsonb_typeof(c->'version') is distinct from 'number') then raise exception 'context read unavailable' using errcode='42501';end if;
  perform authz.nexloop_assert_context_assemble(p_digest,p_world,a,null);
  select * into v_row from control.nexloop_context_strategies where tenant_id=v_tenant and world=p_world and strategy_id=c->>'strategy_id'
   and (not c ? 'version' or version=(c->>'version')::integer) order by version desc limit 1;
  if not found then v_result:=null;else
   v_result:=jsonb_build_object('strategy_ref','context-strategy:'||v_row.strategy_id||'@'||v_row.version,'definition',v_row.definition,
    'definition_digest',v_row.definition_digest,'published_at',v_row.published_at);end if;
  perform authz.nexloop_assert_context_assemble(p_digest,p_world,a,null);
  return v_result;
 end if;
 if jsonb_typeof(a->'read_proofs') is distinct from 'array' or jsonb_array_length(a->'read_proofs')>256 then raise exception 'context read unavailable' using errcode='42501';end if;
 -- Every proof must be the caller's own, current, and for exactly the resource it names.
 for proof in select value from jsonb_array_elements(a->'read_proofs') loop
  if proof->>'tenant_id' is distinct from v_tenant or proof->>'principal_id' is distinct from ident->'binding'->>'subject_principal_id'
   or proof->>'operation' is distinct from 'read' or proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp() then
   raise exception 'context read proof invalid' using errcode='42501';end if;
  perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
 end loop;
 perform set_config('eios.tenant_id',v_tenant,true);
 if v_verb='definitions' then
  if jsonb_typeof(c->'refs') is distinct from 'array' or jsonb_array_length(c->'refs') not between 1 and 64 then raise exception 'context read unavailable' using errcode='42501';end if;
  for ref in select value#>>'{}' from jsonb_array_elements(c->'refs') loop
   v_type:=null;v_prop:=null;v_value:=null;v_props:=null;
   if ref ~ '^eios:object_type:[A-Za-z][A-Za-z0-9_]{0,63}$' then v_type:=substring(ref from 18);
   elsif ref ~ '^eios:property:[A-Za-z][A-Za-z0-9_]{0,63}/[A-Za-z][A-Za-z0-9_]{0,63}$' then
    v_type:=split_part(substring(ref from 15),'/',1);v_prop:=split_part(substring(ref from 15),'/',2);
   elsif ref ~ '^nexloop:vocabulary:[A-Za-z][A-Za-z0-9_]{0,63}/[A-Za-z][A-Za-z0-9_]{0,63}/.+$' then
    v_type:=split_part(substring(ref from 20),'/',1);v_prop:=split_part(substring(ref from 20),'/',2);
    v_value:=substring(substring(ref from 20) from length(v_type)+length(v_prop)+3);
   else raise exception 'context definition ref invalid' using errcode='22023';end if;
   -- Gates: the type always; the property for property/vocabulary refs (same as NX-021 recall gates).
   foreach gate in array array_remove(array['eios:object_type:'||v_type,case when v_prop is null then null else 'eios:property:'||v_type||'/'||v_prop end],null) loop
    if not exists(select 1 from jsonb_array_elements(a->'read_proofs') q where q->>'target_resource'=gate and q->>'resource_id'=gate) then
     raise exception 'context definition READ required' using errcode='42501';end if;
   end loop;
   select definition,version into v_def,v_version from ontology.object_type_versions where tenant_id=v_tenant and type_name=v_type order by version desc limit 1;
   if not found then raise exception 'context definition unavailable' using errcode='42501';end if;
   if v_prop is null then
    -- A type view lists only the properties the caller may READ.
    select coalesce(jsonb_agg(p->>'property_name' order by p->>'property_name'),'[]'::jsonb) into v_props from jsonb_array_elements(v_def->'properties') p
     where exists(select 1 from jsonb_array_elements(a->'read_proofs') q where q->>'target_resource'='eios:property:'||v_type||'/'||(p->>'property_name'));
    v_items:=v_items||jsonb_build_array(jsonb_build_object('ref',ref,'kind','object_type','schema_version',v_version,
     'display_name',v_def->>'display_name','description',coalesce(v_def->>'description',''),'readable_properties',v_props,
     'aliases',(select coalesce(jsonb_agg(x.alias_text order by x.alias_text),'[]'::jsonb) from ontology.nexloop_definition_aliases x where x.tenant_id=v_tenant and x.world=p_world and x.canonical_ref=ref)));
   else
    select value into v_props from jsonb_array_elements(v_def->'properties') where value->>'property_name'=v_prop;
    if v_props is null then raise exception 'context definition unavailable' using errcode='42501';end if;
    if v_value is not null and not coalesce(v_props->'type_descriptor'->'enum','[]'::jsonb) ? replace(replace(replace(replace(replace(replace(replace(v_value,'%20',' '),'%09',chr(9)),'%0A',chr(10)),'%0D',chr(13)),'%0C',chr(12)),'%0B',chr(11)),'%25','%') then
     raise exception 'context definition unavailable' using errcode='42501';end if;
    v_items:=v_items||jsonb_build_array(jsonb_build_object('ref',ref,'kind',case when v_value is null then 'property' else 'vocabulary_value' end,
     'schema_version',v_version,'property_ref','eios:property:'||v_type||'/'||v_prop,'display_name',v_props->>'display_name','description',coalesce(v_props->>'description',''),
     'value_type',v_props->>'value_type','closed_vocabulary',coalesce(v_props->'type_descriptor'->'enum','[]'::jsonb),
     'group',(select g->>'group_name' from jsonb_array_elements(coalesce(v_def->'property_groups','[]'::jsonb)) g where g->'property_names' ? v_prop limit 1),
     'aliases',(select coalesce(jsonb_agg(x.alias_text order by x.alias_text),'[]'::jsonb) from ontology.nexloop_definition_aliases x where x.tenant_id=v_tenant and x.world=p_world and x.canonical_ref=ref)));
   end if;
  end loop;
  v_result:=jsonb_build_object('items',v_items);
 else
  v_consumer:=c->>'consumer_id';
  if v_consumer !~ '^[0-9a-f]{64}$' or v_consumer is null or not exists(select 1 from jsonb_array_elements(a->'read_proofs') q
    where q->>'target_resource'='eios:object:Consumer/'||v_consumer and q->>'resource_id'='eios:object:Consumer/'||v_consumer) then
   raise exception 'context open work requires Consumer READ' using errcode='42501';end if;
  if not exists(select 1 from ontology.objects where tenant_id=v_tenant and world=p_world and type_name='Consumer' and object_id=v_consumer) then
   raise exception 'context consumer unavailable' using errcode='42501';end if;
  v_result:=jsonb_build_object(
   'intents',(select coalesce(jsonb_agg(jsonb_build_object('intent_id',i.intent_id::text,'action',i.action_name||':'||i.action_version,'state',i.state,
      'created_at',i.created_at) order by i.created_at,i.intent_id),'[]'::jsonb) from runtime.nexloop_effect_intents i
     where i.tenant_id=v_tenant and i.world=p_world and i.consumer_id=v_consumer and i.state in ('accepted','dispatching','unknown')),
   'outbound',(select coalesce(jsonb_agg(jsonb_build_object('intent_id',o.intent_id::text,'conversation_id',o.conversation_id,'delivery_state',o.delivery_state,
      'persisted_at',o.persisted_at,'delivery_changed_at',o.delivery_changed_at,'message_id',o.message_id,'body_digest',o.body_digest)
      order by o.persisted_at,o.intent_id),'[]'::jsonb) from runtime.nexloop_outbound_messages o join runtime.nexloop_conversations cv
      on cv.tenant_id=o.tenant_id and cv.world=o.world and cv.conversation_id=o.conversation_id
     where o.tenant_id=v_tenant and o.world=p_world and cv.consumer_id=v_consumer),
   -- NX-025 (G4): the Consumer's active plans at their current version, read-only, exactly as the v6 bind re-derives them.
   'plans',(select coalesce(jsonb_agg(jsonb_build_object('plan_id',pl.plan_id::text,'version',pl.version,'created_at',pl.created_at,
      'content',runtime.nexloop_plan_context(pl.tenant_id,pl.world,pl.plan_id,pl.version)) order by pl.created_at,pl.plan_id),'[]'::jsonb)
     from runtime.nexloop_active_plans(v_tenant,p_world) pl where pl.consumer_id=v_consumer),
   -- NX-026: the Consumer's live commitments (formal objects), exactly as the v6 bind re-derives them.
   'commitments',(select coalesce(jsonb_agg(jsonb_build_object('commitment_id',o.object_id,'revision',o.nexloop_revision,'registered_at',r.registered_at,
      'content',runtime.nexloop_commitment_context(o.tenant_id,o.world,o.object_id)) order by r.registered_at,o.object_id),'[]'::jsonb)
     from runtime.nexloop_commitments r join ontology.objects o on o.tenant_id=r.tenant_id and o.world=r.world and o.type_name='Commitment' and o.object_id=r.commitment_id
     where r.tenant_id=v_tenant and r.world=p_world and r.consumer_id=v_consumer and o.properties->>'made_to'=v_consumer
      and o.properties->>'status' in ('conditional','open','in_progress','breached')),
   -- AT-043 D8: the incentives this Consumer may still be issued (bound, active, valid catalog entries with an issuable range;
   -- never the budget balance), exactly as the v6 bind re-derives them.
   'incentives',(select coalesce(jsonb_agg(jsonb_build_object('offering_id',ic.offering_id,'revision',ic.revision,'content',ic.content)
      order by ic.offering_id),'[]'::jsonb) from runtime.nexloop_incentive_catalog_for(v_tenant,p_world,v_consumer) ic));
 end if;
 for proof in select value from jsonb_array_elements(a->'read_proofs') loop
  perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
 end loop;
 return v_result;
end $$;

create or replace function authz.nexloop_context_v6_source_in(p_tenant text,p_world text,p_conversations text[],p_consumer text,p_section text,p_item jsonb,p_proofs jsonb)
returns jsonb language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ref text:=p_item->>'ref';pl runtime.nexloop_plans;cm ontology.objects;cur record;proof jsonb;x ontology.nexloop_claims;i runtime.nexloop_effect_intents;o runtime.nexloop_outbound_messages;
 obj ontology.objects;expected jsonb;kind text;sub text;revision text;tags text[]:='{}';prop text;
begin
 if jsonb_typeof(p_item) is distinct from 'object' or (select count(*) from jsonb_object_keys(p_item))<>10
  or not p_item ?& array['subsection','ref','revision','content','content_hash','evidence_kind','access_decision_ref','relevance_permille','at','tags']
  or jsonb_typeof(p_item->'relevance_permille') is distinct from 'number' or (p_item->>'relevance_permille')!~'^[0-9]{1,4}$' or (p_item->>'relevance_permille')::integer>1000
  or jsonb_typeof(p_item->'at') is distinct from 'string' or length(p_item->>'at')>64 or jsonb_typeof(p_item->'tags') is distinct from 'array'
  or jsonb_typeof(p_item->'revision') is distinct from 'string' then raise exception 'context source shape invalid' using errcode='42501';end if;
 if p_item->>'content_hash' is distinct from runtime.nexloop_context_hash(p_item->'content') then raise exception 'context source hash mismatch' using errcode='42501';end if;
 proof:=p_proofs->(p_item->>'access_decision_ref');
 if proof is null then raise exception 'context source decision unknown' using errcode='42501';end if;
 if ref ~ '^claim:[0-9a-f]{64}$' then
  -- AT-064: a Claim is evidence only; never formal state, never a planning fact.
  if p_section<>'evidence' then raise exception 'context Claim outside evidence' using errcode='42501';end if;
  select * into x from ontology.nexloop_claims where tenant_id=p_tenant and world=p_world and claim_id=substr(ref,7) and conversation_id=any(p_conversations);
  if not found then raise exception 'context Claim unavailable' using errcode='42501';end if;
  if proof->>'target_resource' is distinct from 'eios:object:Conversation/'||x.conversation_id then raise exception 'context source decision mismatch' using errcode='42501';end if;
  if x.epistemic_kind='hypothesis' then
   -- Real-world strategies cannot include hypotheses (0088); nothing else may carry one.
   raise exception 'context hypothesis excluded' using errcode='42501';
  end if;
  if x.source_message_id is null or x.resolution_state not in ('unresolved','needs_resolution','awaiting_definition','resolved') then
   raise exception 'context Claim not admissible evidence' using errcode='42501';end if;
  sub:='claim_evidence';revision:=x.extractor_version;kind:=case when x.speaker='consumer' then 'user_statement' else 'conversation' end;
  -- Original text only: no structured value can act as a formal property.
  expected:=jsonb_build_object('quote',x.quote,'message_ref','eios:object:Message/'||x.source_message_id,'span',jsonb_build_array(x.span_start,x.span_end),
   'speaker',x.speaker,'epistemic_kind',x.epistemic_kind,'resolution_state',x.resolution_state,'modality',x.modality,'condition',x.condition_text);
  if x.polarity='negated' then tags:=tags||'negation'::text;end if;
  if x.epistemic_kind='constraint' then tags:=tags||'contact_limit'::text;end if;
 elsif ref ~ '^nexloop:intent:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' then
  if p_section<>'open_work' then raise exception 'context execution state outside open work' using errcode='42501';end if;
  select * into i from runtime.nexloop_effect_intents where intent_id=substr(ref,16)::uuid and tenant_id=p_tenant and world=p_world and consumer_id=p_consumer;
  if not found or i.state not in ('accepted','dispatching','unknown') then raise exception 'context intent unavailable' using errcode='42501';end if;
  if proof->>'target_resource' is distinct from 'eios:object:Consumer/'||p_consumer then raise exception 'context source decision mismatch' using errcode='42501';end if;
  sub:='intents';revision:=i.state;kind:='execution_state';tags:=array['unconfirmed'];
  expected:=jsonb_build_object('action',i.action_name||':'||i.action_version,'state',i.state,'since',i.created_at);
 elsif ref ~ '^nexloop:outbound:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' then
  if p_section<>'open_work' then raise exception 'context execution state outside open work' using errcode='42501';end if;
  select o2.* into o from runtime.nexloop_outbound_messages o2 join runtime.nexloop_conversations cv on cv.tenant_id=o2.tenant_id and cv.world=o2.world and cv.conversation_id=o2.conversation_id
   where o2.tenant_id=p_tenant and o2.world=p_world and o2.intent_id=substr(ref,18)::uuid and cv.consumer_id=p_consumer;
  -- Channel-accepted replies are conversation, never unconfirmed execution (ADR-020 §2).
  if not found or o.delivery_state in ('provider_accepted','delivered') then raise exception 'context outbound unavailable' using errcode='42501';end if;
  if proof->>'target_resource' is distinct from 'eios:object:Consumer/'||p_consumer then raise exception 'context source decision mismatch' using errcode='42501';end if;
  sub:='outbound';revision:=o.delivery_state;kind:='execution_state';
  if o.delivery_state in ('persisted','dispatching','unknown') then tags:=array['unconfirmed'];end if;
  expected:=jsonb_build_object('delivery_state',o.delivery_state,'since',o.delivery_changed_at,'note','not confirmed as delivered to the consumer');
 elsif ref ~ '^nexloop:plan:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}@[1-9][0-9]{0,5}$' then
  -- NX-025 (G4): a plan is open work of its Consumer; only its current active version, read-only.
  if p_section<>'open_work' then raise exception 'context plan outside open work' using errcode='42501';end if;
  select * into pl from runtime.nexloop_plans where tenant_id=p_tenant and world=p_world and plan_id=split_part(substr(ref,14),'@',1)::uuid
   and version=split_part(ref,'@',2)::integer and consumer_id=p_consumer;
  if not found then raise exception 'context plan unavailable' using errcode='42501';end if;
  select * into cur from runtime.nexloop_plan_current(p_tenant,p_world,pl.plan_id);
  if cur.status is distinct from 'active' or cur.version is distinct from pl.version then raise exception 'context plan not current' using errcode='42501';end if;
  if proof->>'target_resource' is distinct from 'eios:object:Consumer/'||p_consumer then raise exception 'context source decision mismatch' using errcode='42501';end if;
  sub:='plan';revision:=pl.version::text;kind:='policy';
  expected:=runtime.nexloop_plan_context(p_tenant,p_world,pl.plan_id,pl.version);
 elsif ref ~ '^eios:object:Commitment/[0-9a-f]{64}$' then
  -- NX-026: a live commitment made to this Consumer is open work; its current revision, read-only.
  if p_section<>'open_work' then raise exception 'context commitment outside open work' using errcode='42501';end if;
  select * into cm from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='Commitment' and object_id=substr(ref,24)
   and properties->>'made_to'=p_consumer and properties->>'status' in ('conditional','open','in_progress','breached');
  if not found then raise exception 'context commitment unavailable' using errcode='42501';end if;
  if proof->>'target_resource' is distinct from 'eios:object:Consumer/'||p_consumer then raise exception 'context source decision mismatch' using errcode='42501';end if;
  sub:='commitment';revision:=cm.nexloop_revision::text;kind:='formal_object';
  expected:=runtime.nexloop_commitment_context(p_tenant,p_world,cm.object_id);
 elsif ref ~ '^eios:object:IncentiveOffering/[0-9a-f]{64}$' then
  -- AT-043 D8: an incentive this Consumer may be issued is open work; re-derived now (catalog and issuable range).
  if p_section<>'open_work' then raise exception 'context incentive outside open work' using errcode='42501';end if;
  if proof->>'target_resource' is distinct from 'eios:object:Consumer/'||p_consumer then raise exception 'context source decision mismatch' using errcode='42501';end if;
  select ic.revision,ic.content into revision,expected from runtime.nexloop_incentive_catalog_for(p_tenant,p_world,p_consumer) ic where ic.offering_id=substr(ref,31);
  if not found then raise exception 'context incentive unavailable' using errcode='42501';end if;
  sub:='incentive';kind:='formal_object';
 elsif ref ~ '^eios:object:Consumer/[0-9a-f]{64}$' then
  if p_section not in ('constraints','consumer_state') or substr(ref,22) is distinct from p_consumer then raise exception 'context formal object outside formal state' using errcode='42501';end if;
  select * into obj from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='Consumer' and object_id=p_consumer;
  if not found or proof->>'target_resource' is distinct from 'eios:object:Consumer/'||p_consumer then raise exception 'context source decision mismatch' using errcode='42501';end if;
  if jsonb_typeof(p_item->'content'->'properties') is distinct from 'object' then raise exception 'context formal projection invalid' using errcode='42501';end if;
  -- Projection only: every shown property is current and was itself READ-authorized.
  for prop in select jsonb_object_keys(p_item->'content'->'properties') loop
   if not obj.properties ? prop or not exists(select 1 from jsonb_each(p_proofs) q where q.value->>'target_resource'='eios:property:Consumer/'||p_consumer||'/'||prop) then
    raise exception 'context formal property unreadable' using errcode='42501';end if;
  end loop;
  sub:='Consumer';revision:=obj.nexloop_revision::text;kind:='formal_object';
  expected:=jsonb_build_object('type','Consumer','properties',(select coalesce(jsonb_object_agg(k,obj.properties->k),'{}'::jsonb) from jsonb_object_keys(p_item->'content'->'properties') k));
 else
  raise exception 'context source kind unsupported' using errcode='42501';
 end if;
 if p_item->>'subsection' is distinct from sub or p_item->>'evidence_kind' is distinct from kind or p_item->>'revision' is distinct from revision
  or p_item->'content' is distinct from expected then raise exception 'context source content mismatch' using errcode='42501';end if;
 return to_jsonb(array(select t from unnest(tags) t order by t));
end $$;
