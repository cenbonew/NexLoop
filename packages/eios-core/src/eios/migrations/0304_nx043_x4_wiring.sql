-- NX-043 X4 wiring (dispatcher 2026-10-21): runtime.nexloop_experiment_strategy_for reaches the decision paths. The migration number
-- lives only in the file name and the catalog. Only a started online experiment (world real) has effect - online stays
-- online_not_available at start, so on stage nothing changes until it opens; the real dispatch switches are never read or written and
-- no new send path exists.
-- 1. runtime.nexloop_experiment_levers(tenant, world, consumer): read only (it never allocates). The levers of the candidate strategy
--    for a Consumer allocated to the candidate group of a started online experiment before its end, while the candidate strategy is
--    published (and a selected twin selectable) and the Consumer is not being erased; null otherwise - the control group, an excluded
--    Consumer (erasing, in_same_comparison: no arm), a retired strategy, a stopped experiment, another world: the current policy.
-- 2. Allocation on first sight: a real inbound message of a Consumer, while the tenant has a started online experiment, runs
--    runtime.nexloop_experiment_strategy_for (allocation; the cluster key is the Consumer itself, derived server side, never a caller
--    value). The switch is read first outside any subtransaction; the allocation runs in a savepoint with a 200 ms lock timeout and a
--    failure is only counted (runtime.nexloop_experiment_accounting_failures, consumption_id allocation:inbound) - the real message is
--    always written.
-- 3. The levers in the decision paths (re-created from their latest bodies with only these changes; tests/test_function_copy_guard.py):
--    persona: runtime.nexloop_operator_persona_section (0228) - the strategy's persona version; source stays the current one, since
--    context-pack v6 is published (dispatcher 2026-10-21, decision B) - the experiment is recorded in allocations and audit, not the pack;
--    outreach purposes: runtime.nexloop_outreach_gates (0263) - a non-marketing purpose on or off, with an experiment_strategy gate entry;
--    follow-up timing: runtime.nexloop_outreach_candidates (0232) - service follow-ups after follow_up_delay_hours.
-- 4. Evidence: runtime.nexloop_outreach_accept (0263, re-created) freezes the decision's strategy (experiment, strategy, version) in the
--    intent's experiment_strategy column; its insert appends audit nexloop.experiment.strategy_applied from that column (a later
--    retirement does not change it), and a failed append is counted (evidence:outreach_send); the Context pack's persona section carries the version; allocations carry the arm (X6).

create function runtime.nexloop_experiment_levers(p_tenant text,p_world text,p_consumer text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);x record;out jsonb;
begin
 if p_world is distinct from 'real' or p_consumer is null then return null;end if;
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 select e.experiment_id,e.registration,o.arm into x from control.nexloop_experiment_allocations o
  join control.nexloop_experiments e on e.tenant_id=o.tenant_id and e.experiment_id=o.experiment_id
  where o.tenant_id=p_tenant and o.consumer_id=p_consumer and o.arm is not null and e.registration->>'world'='real' and e.registration->>'stage'='online'
   and runtime.nexloop_experiment_status(p_tenant,e.experiment_id)='started' and (e.registration->>'ends_at')::timestamptz>clock_timestamp()
  order by o.allocated_at desc limit 1;
 if found and x.arm='candidate' and not runtime.nexloop_consumer_erasing(p_tenant,'real',p_consumer)
  and runtime.nexloop_strategy_status(p_tenant,x.registration->'candidate'->>'strategy_id',(x.registration->'candidate'->>'version')::integer)='published'
  and (x.registration->'twin'='null'::jsonb or runtime.nexloop_consumer_twin_selectable(p_tenant,x.registration->'twin'->>'twin_id',(x.registration->'twin'->>'version')::integer)) then
  select jsonb_build_object('experiment_id',x.experiment_id,'strategy_id',s.strategy_id,'version',s.version,'levers',s.definition->'levers') into out
   from control.nexloop_strategy_versions s where s.tenant_id=p_tenant and s.strategy_id=x.registration->'candidate'->>'strategy_id'
    and s.version=(x.registration->'candidate'->>'version')::integer;
 end if;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return out;
end $$;
alter function runtime.nexloop_experiment_levers(text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_experiment_levers(text,text,text) from public;

create function runtime.nexloop_experiment_online_running(p_tenant text) returns boolean
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);v boolean;
begin
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 select exists(select 1 from control.nexloop_experiments e where e.tenant_id=p_tenant and e.registration->>'world'='real' and e.registration->>'stage'='online'
   and (e.registration->>'ends_at')::timestamptz>clock_timestamp() and runtime.nexloop_experiment_status(p_tenant,e.experiment_id)='started') into v;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return v;
end $$;
alter function runtime.nexloop_experiment_online_running(text) owner to nexloop_owner;
revoke all on function runtime.nexloop_experiment_online_running(text) from public;

create function runtime.nexloop_experiment_allocate_on_inbound() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_consumer text;v_lock text;v_prior text:=current_setting('eios.tenant_id',true);
begin
 if new.world<>'real' or new.record ? 'direction' then return null;end if;  -- a Consumer's own message carries no direction (0046)
 if not runtime.nexloop_experiment_online_running(new.tenant_id) then return null;end if;  -- no subtransaction while none runs
 v_lock:=current_setting('lock_timeout');
 begin
  perform set_config('lock_timeout','200ms',true);
  perform set_config('eios.tenant_id',new.tenant_id,true);
  select cv.consumer_id into v_consumer from runtime.nexloop_conversations cv where cv.tenant_id=new.tenant_id and cv.world=new.world and cv.conversation_id=new.conversation_id;
  if v_consumer is not null then perform runtime.nexloop_experiment_strategy_for(new.tenant_id,'real',v_consumer,null);end if;
  perform set_config('lock_timeout',v_lock,true);
 exception when others then
  begin
   perform set_config('eios.tenant_id',new.tenant_id,true);
   insert into runtime.nexloop_experiment_accounting_failures(tenant_id,world,consumption_id,sqlstate) values(new.tenant_id,'real','allocation:inbound',sqlstate);
  exception when others then null;
  end;
 end;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return null;
end $$;
alter function runtime.nexloop_experiment_allocate_on_inbound() owner to nexloop_owner;
revoke all on function runtime.nexloop_experiment_allocate_on_inbound() from public;
create trigger nx043_experiment_allocate after insert on runtime.nexloop_conversation_messages for each row execute function runtime.nexloop_experiment_allocate_on_inbound();

create function runtime.nexloop_experiment_strategy_evidence() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v jsonb:=new.experiment_strategy;v_prior text:=current_setting('eios.tenant_id',true);
begin
 if new.world<>'real' or v is null then return null;end if;
 begin
  perform set_config('eios.tenant_id',new.tenant_id,true);
  insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
   values('experiment-strategy:'||new.intent_id,'nexloop.experiment.strategy_applied',new.tenant_id,'nexloop:experiment','experiment',new.intent_id,
    jsonb_build_object('experiment_id',v->>'experiment_id','strategy_id',v->>'strategy_id','version',(v->>'version')::integer,'decision','outreach_send',
     'intent_id',new.intent_id,'purpose',new.purpose_id),clock_timestamp()) on conflict do nothing;
 exception when others then  -- evidence only: the real send is never failed by it, but a miss is counted
  begin
   perform set_config('eios.tenant_id',new.tenant_id,true);
   insert into runtime.nexloop_experiment_accounting_failures(tenant_id,world,consumption_id,sqlstate) values(new.tenant_id,'real','evidence:outreach_send',sqlstate);
  exception when others then null;
  end;
 end;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return null;
end $$;
alter function runtime.nexloop_experiment_strategy_evidence() owner to nexloop_owner;
revoke all on function runtime.nexloop_experiment_strategy_evidence() from public;
alter table runtime.nexloop_outreach_sends add column experiment_strategy jsonb
 check(experiment_strategy is null or (jsonb_typeof(experiment_strategy)='object' and experiment_strategy ?& array['experiment_id','strategy_id','version']));
create trigger nx043_experiment_strategy_evidence after insert on runtime.nexloop_outreach_sends for each row execute function runtime.nexloop_experiment_strategy_evidence();

create or replace function runtime.nexloop_operator_persona_section(p_tenant text,p_world text,p_consumer text) returns jsonb
 language plpgsql stable set search_path=pg_catalog,pg_temp as $$
declare d record;v integer;doc jsonb;prefs jsonb;v_lever jsonb;
begin
 select * into d from runtime.nexloop_operator_persona_desired(p_tenant,p_world,p_consumer);
 -- NX-043 X4 wiring: a candidate-group Consumer of a started online experiment speaks with the strategy's persona version, while it is
 -- published and the Consumer has a current persona (runtime.nexloop_experiment_levers; otherwise the current persona). Settlement and its events keep the current one.
 v_lever:=runtime.nexloop_experiment_levers(p_tenant,p_world,p_consumer)->'levers'->'persona';
 if v_lever is not null and d.persona_id is not null and runtime.nexloop_strategy_persona_published(p_tenant,v_lever->>'persona_id',(v_lever->>'version')::integer) then
  d.persona_id:=v_lever->>'persona_id';  -- source stays the current one: context-pack v6 is published (decision B)
 else v_lever:=null;end if;
 if d.persona_id is null then return null;end if;
 v:=coalesce((v_lever->>'version')::integer,runtime.nexloop_operator_persona_usable(p_tenant,d.persona_id));
 select document into doc from control.nexloop_operator_personas where tenant_id=p_tenant and persona_id=d.persona_id and version=v;
 -- 0228: the consumer's own, not dismissed communication preferences (claim id, dimension, polarity, value), if any.
 select coalesce(jsonb_agg(x-'dismissed'),'[]'::jsonb) into prefs from jsonb_array_elements(runtime.nexloop_contact_preferences(p_tenant,p_world,p_consumer)) x
  where not (x->>'dismissed')::boolean;
 return jsonb_build_object('persona_ref','operator-persona:'||d.persona_id||'@'||v,'persona_id',d.persona_id,'version',v,'source',d.source,
  'document',doc,'ai_disclosure',runtime.nexloop_ai_disclosure_setting(p_tenant)-'revision')
  ||case when jsonb_array_length(prefs)>0 then jsonb_build_object('preferences',prefs) else '{}'::jsonb end;
end $$;

create or replace function runtime.nexloop_outreach_candidates(p_tenant text,p_world text,p_at timestamptz) returns jsonb
 language plpgsql stable set search_path=pg_catalog,pg_temp as $$
declare v_lead integer:=coalesce((select (definition->>'lead_seconds')::integer from control.nexloop_commitment_settings order by version desc limit 1),3600);
 x record;out jsonb:='[]'::jsonb;gates jsonb;v_lever jsonb;
begin
 for x in
  select c.consumer_id,'commitment_followup' purpose,'commitment:'||c.commitment_id origin_ref,c.commitment_id||':due_soon' dedupe_key,
   (o.properties->>'due_at')::timestamptz at_time
  from runtime.nexloop_commitments c join ontology.objects o on o.tenant_id=c.tenant_id and o.world=c.world and o.type_name='Commitment' and o.object_id=c.commitment_id
  where c.tenant_id=p_tenant and c.world=p_world and o.properties->>'status' in ('open','in_progress') and o.properties->>'due_at' is not null
   and (o.properties->>'due_at')::timestamptz>p_at and (o.properties->>'due_at')::timestamptz-make_interval(secs=>v_lead)<=p_at
  union all
  select i.consumer_id,'service_followup','intent:'||i.intent_id::text,i.intent_id::text||':'||i.state,
   coalesce((select max(e.created_at) from runtime.nexloop_effect_events e where e.tenant_id=i.tenant_id and e.world=i.world and e.intent_id=i.intent_id),i.created_at)
  from runtime.nexloop_effect_intents i
  where i.tenant_id=p_tenant and i.world=p_world and i.state in ('fulfilled','confirmed','failed')
   and not exists(select 1 from runtime.nexloop_outbound_messages m where m.tenant_id=i.tenant_id and m.world=i.world and m.intent_id=i.intent_id)
   and coalesce((select max(e.created_at) from runtime.nexloop_effect_events e where e.tenant_id=i.tenant_id and e.world=i.world and e.intent_id=i.intent_id),i.created_at)>p_at-interval '336 hours'
  order by 5,3
 loop
  -- NX-043 X4 wiring: a service follow-up of a candidate-group Consumer comes after the strategy's follow_up_delay_hours (1-336 h);
  -- everyone else keeps the 24 hours window.
  if x.purpose='service_followup' then
   v_lever:=runtime.nexloop_experiment_levers(p_tenant,p_world,x.consumer_id)->'levers';
   if v_lever ? 'follow_up_delay_hours' then
    continue when x.at_time+make_interval(hours=>(v_lever->>'follow_up_delay_hours')::integer)>p_at;
   else
    continue when x.at_time<=p_at-interval '24 hours';
   end if;
  end if;
  gates:=runtime.nexloop_outreach_gates(p_tenant,p_world,x.consumer_id,x.purpose,p_at);
  out:=out||jsonb_build_object('consumer_id',x.consumer_id,'purpose',x.purpose,'origin_ref',x.origin_ref,'dedupe_key',x.dedupe_key,'at',x.at_time,'gates',gates,
   'would_dispatch',not exists(select 1 from jsonb_array_elements(gates) y where y->>'result'<>'pass'),
   'blocked_by',(select coalesce(jsonb_agg(y->>'gate'),'[]'::jsonb) from jsonb_array_elements(gates) y where y->>'result'='refuse'));
 end loop;
 return jsonb_build_object('at',p_at,'settings',runtime.nexloop_outreach_settings(p_tenant),'candidates',out,
  'note','dry run: no outreach Action is published yet (slice 4); nothing is sent');
end $$;

CREATE OR REPLACE FUNCTION runtime.nexloop_outreach_gates(p_tenant text, p_world text, p_consumer text, p_purpose text, p_at timestamp with time zone)
 RETURNS jsonb
 LANGUAGE plpgsql
 STABLE
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
declare d jsonb:=runtime.nexloop_outreach_settings(p_tenant);g jsonb:='[]'::jsonb;v_local time:=(p_at at time zone (d->>'time_zone'))::time;
 v_from time:=split_part(d->>'quiet_hours','-',1)::time;v_to time:=split_part(d->>'quiet_hours','-',2)::time;v_n24 bigint;v_n7 bigint;v_code text;v_layer text;v_purpose jsonb;
 v_marketing boolean;v_m7 bigint;lim control.nexloop_budget_limits;v_used numeric;v_state jsonb;v_lever jsonb;
 v_budget_at timestamptz:=case when runtime.nexloop_world_kind(p_world) in ('simulation','shadow') then clock_timestamp() else p_at end;
begin
 g:=g||jsonb_build_object('gate','contact_restricted','result',case when exists(select 1 from control.nexloop_contact_restrictions r where r.tenant_id=p_tenant
   and r.world=p_world and r.consumer_id=p_consumer and r.active) then 'refuse' else 'pass' end);
 g:=g||jsonb_build_object('gate','control_paused','result',case when exists(select 1 from control.nexloop_control_scopes s where s.tenant_id=p_tenant and s.world=p_world and s.paused
   and ((s.scope_kind='tenant' and s.scope_ref='*') or (s.scope_kind='consumer' and s.scope_ref in (p_consumer,'consumer:'||p_consumer)))) then 'refuse' else 'pass' end);
 -- Outreach slice 3: the purpose must be in the current purpose list and enabled for the tenant. Outreach slice 6: a purpose that
 -- requires approval refuses with approval_required; the dispatch lets it pass only for an intent whose approval was granted.
 v_purpose:=runtime.nexloop_outreach_purpose(p_tenant,p_purpose);
 -- NX-043 X4 wiring: the strategy's outreach_purposes lever of a candidate-group Consumer only switches off a listed non-marketing purpose
 -- (no approval, no consent) the tenant has on - never on (dispatcher 2026-10-21, L6 review 3.1); it never touches a marketing purpose. Decision and dispatch both read these gates.
 v_lever:=runtime.nexloop_experiment_levers(p_tenant,p_world,p_consumer);
 if v_lever->'levers'->'outreach_purposes' ? p_purpose and runtime.nexloop_strategy_purpose_allowed(p_purpose) and (v_purpose->>'listed')::boolean
  and not coalesce((v_purpose->'definition'->>'requires_approval')::boolean,false) and not coalesce((v_purpose->'definition'->>'requires_consent')::boolean,false)
  and coalesce(v_purpose->'definition'->>'frequency_class','service')<>'marketing' then
  v_purpose:=v_purpose||jsonb_build_object('enabled',(v_purpose->>'enabled')::boolean and (v_lever->'levers'->'outreach_purposes'->>p_purpose)::boolean);
  g:=g||jsonb_build_object('gate','experiment_strategy','result','pass','detail','strategy:'||(v_lever->>'strategy_id')||'@'||(v_lever->>'version'));
 end if;
 g:=g||jsonb_build_object('gate','purpose','result',case when not (v_purpose->>'listed')::boolean or not (v_purpose->>'enabled')::boolean then 'refuse'
   when (v_purpose->'definition'->>'requires_approval')::boolean then 'refuse' else 'pass' end,  -- Outreach slice 6: refused until approved (admit)
  'detail',case when not (v_purpose->>'listed')::boolean then 'purpose_not_allowed' when not (v_purpose->>'enabled')::boolean then 'purpose_disabled'
   when (v_purpose->'definition'->>'requires_approval')::boolean then 'approval_required' else 'list v'||(v_purpose->>'list_version') end);
 -- Outreach slice 4, gate 4: marketing needs the tenant's marketing switch; opt-in (tenant or purpose) needs a consent record, and
 -- consent records arrive with slice 7, so it refuses until then. Opt-out refusals are gates 1 (restriction) and 8 (topic).
 v_marketing:=coalesce(v_purpose->'definition'->>'frequency_class','service')='marketing';
 -- Outreach slice 7: opt-out refuses after an unsubscribe; opt-in (tenant or purpose) needs the latest consent event to be granted.
 v_state:=runtime.nexloop_marketing_state(p_tenant,p_world,p_consumer);
 g:=g||jsonb_build_object('gate','marketing','result',case when not v_marketing then 'pass' when not (d->'marketing'->>'enabled')::boolean then 'refuse'
   when d->'marketing'->>'consent_mode'='opt_in' or coalesce((v_purpose->'definition'->>'requires_consent')::boolean,false) then
    case when (v_state->>'consent')::boolean then 'pass' else 'refuse' end
   when (v_state->>'opted_out')::boolean then 'refuse' else 'pass' end,
  'detail',case when not v_marketing then 'not_marketing' when not (d->'marketing'->>'enabled')::boolean then 'marketing_disabled'
   when d->'marketing'->>'consent_mode'='opt_in' or coalesce((v_purpose->'definition'->>'requires_consent')::boolean,false) then
    case when (v_state->>'consent')::boolean then 'consent_granted' else 'marketing_consent_missing' end
   when (v_state->>'opted_out')::boolean then 'marketing_opted_out' else 'opt_out' end);
 g:=g||jsonb_build_object('gate','persona_excluded','result',case when runtime.nexloop_consumer_persona(p_tenant,p_world,p_consumer)<>'real' then 'refuse' else 'pass' end,
   'detail',runtime.nexloop_consumer_persona(p_tenant,p_world,p_consumer));
 g:=g||jsonb_build_object('gate','quiet_hours','result',case when (v_from<=v_to and v_local>=v_from and v_local<v_to) or (v_from>v_to and (v_local>=v_from or v_local<v_to))
   then 'refuse' else 'pass' end,'detail',d->>'quiet_hours');
 v_n24:=runtime.nexloop_outreach_contacts_since(p_tenant,p_world,p_consumer,p_at-interval '24 hours');
 v_n7:=runtime.nexloop_outreach_contacts_since(p_tenant,p_world,p_consumer,p_at-interval '7 days');
 -- Outreach slice 4: the marketing allowance (P8) counts admitted marketing sends.
 select count(*) into v_m7 from runtime.nexloop_outreach_sends s where s.tenant_id=p_tenant and s.world=p_world and s.consumer_id=p_consumer
  and s.frequency_class='marketing' and s.admitted_at>p_at-interval '7 days' and s.state in ('dispatching','unknown','fulfilled');
 g:=g||jsonb_build_object('gate','frequency','result',case when v_n24>=(d->>'per_24h')::integer or v_n7>=(d->>'per_7d')::integer
   or (v_marketing and v_m7>=(d->'marketing'->>'per_7d')::integer) then 'refuse' else 'pass' end,
   'detail',jsonb_build_object('last_24h',v_n24,'last_7d',v_n7,'marketing_7d',v_m7));
 v_code:=runtime.nexloop_contact_preference_refusal(p_tenant,p_world,p_consumer,'native.webchat',coalesce(v_purpose->'definition'->>'topic',p_purpose),gen_random_uuid(),p_at);
 g:=g||jsonb_build_object('gate','consumer_preference','result',case when v_code is null then 'pass' else 'refuse' end,'detail',v_code);
 v_layer:=control.nexloop_real_dispatch_closed_layer(p_tenant,p_world,'eios:action:nexloop.outreach.send:1');
 g:=g||jsonb_build_object('gate','real_dispatch','result',case when v_layer is null then 'pass' else 'refuse' end,'detail',coalesce(v_layer,'open'));
 -- Outreach slice 4, gate 10: the tenant model budget must be configured, in its period and not used up.
 select * into lim from control.nexloop_budget_limits where tenant_id=p_tenant and world=p_world and budget_kind='model';
 if found then
  select coalesce(sum(amount),0) into v_used from control.nexloop_budget_consumption where tenant_id=p_tenant and world=p_world and budget_kind='model'
   and recorded_at>=lim.period_start and recorded_at<lim.period_end;
 end if;
 g:=g||jsonb_build_object('gate','budget','result',case when lim.tenant_id is null or v_budget_at<lim.period_start or v_budget_at>=lim.period_end or v_used>=lim.limit_amount
   then 'refuse' else 'pass' end,'detail',case when lim.tenant_id is null then 'budget_unconfigured'
   when v_budget_at<lim.period_start or v_budget_at>=lim.period_end or v_used>=lim.limit_amount then 'budget_exhausted' else 'model' end);
 return g;
end $function$;

CREATE OR REPLACE FUNCTION runtime.nexloop_outreach_accept(p_tenant text, p_world text, p_consumer text, p_purpose text, p_origin text, p_body text, p_conversation text, p_initiator text, p_principal text, p_intent text, p_run uuid)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare v_purpose jsonb;v_id text;v_digest text;v_head bigint;old runtime.nexloop_outreach_sends;v_day text;
 v_approval boolean;  -- Outreach slice 6
 v_lever jsonb;  -- NX-043 X4 wiring
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 if not exists(select 1 from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='Consumer' and object_id=p_consumer)
  or runtime.nexloop_consumer_erasing(p_tenant,p_world,p_consumer) then raise exception 'outreach send: consumer_unavailable' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 if p_conversation is not null and not exists(select 1 from runtime.nexloop_conversations cv where cv.tenant_id=p_tenant and cv.world=p_world
   and cv.conversation_id=p_conversation and cv.consumer_id=p_consumer) then raise exception 'outreach send: conversation_unavailable' using errcode='22023';end if;
 -- Topic, version and frequency class are frozen from the current list; whether the purpose is enabled is a dispatch gate.
 v_purpose:=runtime.nexloop_outreach_purpose(p_tenant,p_purpose);
 if not (v_purpose->>'listed')::boolean then raise exception 'outreach send: purpose_not_allowed' using errcode='22023';end if;
 if char_length(p_body)>(v_purpose->'definition'->'content_rules'->>'max_chars')::integer then raise exception 'outreach send: body_too_long' using errcode='22023';end if;
 -- Outreach slice 6 (P5): a purpose configured to need approval waits for it, whoever asked (owner or reevaluation Run); nothing is admitted before.
 v_approval:=coalesce((v_purpose->'definition'->>'requires_approval')::boolean,false);
 v_day:=to_char(runtime.nexloop_world_now(p_tenant,p_world) at time zone (runtime.nexloop_outreach_settings(p_tenant)->>'time_zone'),'YYYY-MM-DD');
 v_id:='outreach:'||encode(sha256(convert_to(jsonb_build_array(p_tenant,p_consumer,p_purpose,p_origin,v_day)::text,'UTF8')),'hex');
 v_digest:=encode(sha256(convert_to(p_body,'UTF8')),'hex');
 select * into old from runtime.nexloop_outreach_sends where tenant_id=p_tenant and world=p_world and intent_id=v_id;
 if found then
  if old.body_digest is distinct from v_digest or old.conversation_id is distinct from p_conversation then
   raise exception 'outreach send: intent_exists' using errcode='22023';end if;
  return jsonb_build_object('intent_id',old.intent_id,'state',old.state,'replayed',true);
 end if;
 select revision into v_head from control.nexloop_control_heads where tenant_id=p_tenant and world=p_world;
 -- NX-043 X4 wiring: the candidate strategy this decision used, frozen in the intent (the strategy_applied audit reads it).
 v_lever:=runtime.nexloop_experiment_levers(p_tenant,p_world,p_consumer)-'levers';
 insert into runtime.nexloop_outreach_sends(tenant_id,world,intent_id,consumer_id,purpose_id,purpose_version,topic,frequency_class,channel,origin_ref,conversation_id,
   body,body_digest,initiator,principal_id,request_intent,control_snapshot,run_id,state,experiment_strategy)
  values(p_tenant,p_world,v_id,p_consumer,p_purpose,(v_purpose->>'list_version')::integer,v_purpose->'definition'->>'topic',
   v_purpose->'definition'->>'frequency_class','native.webchat',p_origin,p_conversation,p_body,v_digest,p_initiator,p_principal,p_intent,
   jsonb_build_object('control_revision',coalesce(v_head,0),
    'scopes',jsonb_build_array(jsonb_build_object('kind','consumer','ref',p_consumer),jsonb_build_object('kind','consumer','ref','consumer:'||p_consumer),
      jsonb_build_object('kind','action_type','ref','nexloop.outreach.send')),'goals','[]'::jsonb,'objects','[]'::jsonb,'budgets','[]'::jsonb),p_run,
   case when v_approval then 'pending_approval' else 'accepted' end,v_lever);
 if v_approval then  -- Outreach slice 6: the approval window starts at acceptance
  insert into runtime.nexloop_outreach_approvals(tenant_id,world,intent_id,requested_by,expires_at)
   values(p_tenant,p_world,v_id,p_principal,runtime.nexloop_world_now(p_tenant,p_world)+runtime.nexloop_outreach_approval_window());
 end if;
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('outreach-send:'||v_id,'nexloop.outreach.send.accepted',p_tenant,p_principal,'outreach',p_intent,
   jsonb_build_object('intent_id',v_id,'purpose',p_purpose,'origin_ref',p_origin,'initiator',p_initiator),clock_timestamp()) on conflict do nothing;
 return jsonb_build_object('intent_id',v_id,'state',case when v_approval then 'pending_approval' else 'accepted' end,'replayed',false);
end $function$;
