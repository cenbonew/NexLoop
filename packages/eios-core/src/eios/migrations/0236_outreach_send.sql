-- Proactive outreach, slice 4: the governed outreach send and its dispatch gates (docs/implementation/outreach-impl-plan.md;
-- design §3.1/§3.2, owner decisions P1/P3/P8; contract packages/contracts/outreach-send.schema.json). The migration number lives
-- only in the file name and the catalog (renumbered at integration); nothing below repeats it.
-- The send follows the governed effect pattern of the incentive issue (own intent ledger, acceptance with a control snapshot,
-- an executor port that admits each intent at dispatch, a loopback provider only): nothing here opens a real channel.
-- 1. runtime.nexloop_outreach_sends: one row per business intent (accepted -> dispatching -> fulfilled | failed | unknown, or
--    refused before any provider request). origin_kind is 'proactive' (P1: a reply keeps its row in runtime.nexloop_outbound_messages
--    with its trigger message; a proactive send has none and carries origin_ref, purpose and purpose_version instead). The business
--    intent id is outreach:sha256(tenant, consumer, purpose, origin_ref, day in the tenant time zone): a retry, a restart or a second
--    request on the same day returns the stored intent and never makes a second one. What was asked for never changes.
-- 2. Governed owner Action outreach.send / send_outreach (human): consumer, a listed purpose (topic, version and frequency class
--    frozen from the current list), origin_ref, body within the purpose's max_chars, channel native.webchat, optional conversation of
--    that consumer. Acceptance sends nothing and reserves nothing.
-- 3. Executor port authz.nexloop_outreach_command (nexloop-outreach-v1; EXECUTE on nexloop.outreach.execute:1, domain worker services
--    only): due / admit / observe / status. admit rechecks everything at dispatch under the intent's row lock and a per-consumer
--    lock, in this order: consumer not erasing, the contact restriction first (ADR-023: no exception), the requester still an active
--    member allowed to send, the owner controls on the stored snapshot (NXC01/02/04), then the gates of
--    runtime.nexloop_outreach_gates at dispatch time: purpose (listed, enabled, same topic; approval refused until slice 6), marketing
--    eligibility, test persona, tenant quiet hours, tenant frequency with the marketing allowance, consumer preferences, the three
--    real dispatch layers (plus a switch change after acceptance), the model budget. A refusal is terminal and recorded with its code
--    and the gate results; the provider receives nothing. An unknown provider answer is queried by intent id, never sent again.
-- 4. Frequency counting (P3/O2) and the consumer frequency preference count admitted outreach sends; a topic preference matches by
--    prefix (refusing 'marketing' refuses every 'marketing.*' topic). Bodies copied from their latest definitions, changes marked.
-- 5. The real dispatch switch of eios:action:nexloop.outreach.send:1 is never opened here: no row is closed.

-- 1. Ledger ---------------------------------------------------------------------------------------------------------------------
create table runtime.nexloop_outreach_sends (
 tenant_id text not null,world text not null,intent_id text not null check(intent_id~'^outreach:[0-9a-f]{64}$'),
 consumer_id text not null check(consumer_id~'^[a-f0-9]{64}$' or consumer_id~'^pseudonym:[0-9a-f]{32}$'),
 purpose_id text not null check(purpose_id~'^[a-z][a-z0-9_.]{0,63}$'),purpose_version integer not null check(purpose_version>=1),
 topic text not null check(topic~'^[a-z][a-z0-9_.]{0,63}$'),frequency_class text not null check(frequency_class in ('service','marketing')),
 channel text not null check(channel='native.webchat'),origin_kind text not null default 'proactive' check(origin_kind='proactive'),
 origin_ref text not null check(origin_ref~'^(commitment|intent|plan|owner):[A-Za-z0-9._:-]{1,190}$'),conversation_id text check(conversation_id~'^[a-f0-9]{64}$'),
 body text not null check(char_length(body)<=2000),body_digest text not null check(body_digest~'^[0-9a-f]{64}$'),
 initiator text not null check(initiator='human'),principal_id text not null,request_intent text not null,
 control_snapshot jsonb not null check(jsonb_typeof(control_snapshot)='object'),
 state text not null default 'accepted' check(state in ('accepted','dispatching','unknown','fulfilled','failed','refused')),
 refusal_code text check(refusal_code~'^[a-z_]{1,64}$'),refusal_gates jsonb,fence bigint not null default 0 check(fence>=0),lease_until timestamptz,
 provider_reference text check(char_length(provider_reference) between 1 and 200),
 created_at timestamptz not null default clock_timestamp(),admitted_at timestamptz,finished_at timestamptz,updated_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,intent_id),foreign key(tenant_id) references control.nexloop_tenants(tenant_id),
 check((state='refused')=(refusal_code is not null)),check((state in ('fulfilled','failed','refused'))=(finished_at is not null))
);
create index nexloop_outreach_sends_due on runtime.nexloop_outreach_sends(tenant_id,world,state,updated_at) where state in ('accepted','dispatching','unknown');
create index nexloop_outreach_sends_consumer on runtime.nexloop_outreach_sends(tenant_id,world,consumer_id,admitted_at);
alter table runtime.nexloop_outreach_sends owner to nexloop_owner;
alter table runtime.nexloop_outreach_sends enable row level security;
alter table runtime.nexloop_outreach_sends force row level security;
create policy tenant_boundary on runtime.nexloop_outreach_sends to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_outreach_sends from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;
-- What was asked for never changes; only the execution columns move, and a terminal intent never moves again.
create function runtime.nexloop_outreach_send_guard() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
declare v_moving text[]:=array['state','refusal_code','refusal_gates','fence','lease_until','provider_reference','admitted_at','finished_at','updated_at'];
begin
 if tg_op='DELETE' then raise exception 'outreach intents are never deleted' using errcode='42501';end if;
 if (to_jsonb(new)-v_moving) is distinct from (to_jsonb(old)-v_moving)
  or (old.state in ('fulfilled','failed','refused') and (new.state,new.fence) is distinct from (old.state,old.fence)) then
  raise exception 'outreach intent is immutable' using errcode='42501';end if;
 return new;
end $$;
alter function runtime.nexloop_outreach_send_guard() owner to nexloop_owner;
revoke all on function runtime.nexloop_outreach_send_guard() from public;
create trigger outreach_send_guard before update or delete on runtime.nexloop_outreach_sends for each row execute function runtime.nexloop_outreach_send_guard();

-- 4. Counting and preferences (bodies copied, changes marked "Outreach slice 4") -----------------------------------------------------
create or replace function runtime.nexloop_outreach_contacts_since(p_tenant text,p_world text,p_consumer text,p_since timestamptz) returns bigint
 language sql stable set search_path=pg_catalog,pg_temp as $$
 select (select count(*) from runtime.nexloop_effect_intents i where i.tenant_id=p_tenant and i.world=p_world and i.consumer_id=p_consumer and i.created_at>p_since
  and i.state in ('dispatching','unknown','fulfilled','observed_fulfilled','confirmed')
  and not exists(select 1 from runtime.nexloop_outbound_messages o where o.tenant_id=i.tenant_id and o.world=i.world and o.intent_id=i.intent_id and o.trigger_message_id is not null)
  and coalesce((control.nexloop_intent_effect_category(p_tenant,p_world,i.intent_id)->>'category')='customer_contact'
   or (control.nexloop_intent_effect_category(p_tenant,p_world,i.intent_id)->>'notification')::boolean,true))
 -- Outreach slice 4: every admitted outreach send reaches the customer.
 +(select count(*) from runtime.nexloop_outreach_sends s where s.tenant_id=p_tenant and s.world=p_world and s.consumer_id=p_consumer
   and s.admitted_at>p_since and s.state in ('dispatching','unknown','fulfilled'))
$$;

create or replace function runtime.nexloop_contact_preference_refusal(p_tenant text,p_world text,p_consumer text,p_channel text,p_topic text,p_intent uuid,p_at timestamptz)
 returns text language plpgsql stable set search_path=pg_catalog,pg_temp as $$
declare x jsonb;v_local time:=(p_at at time zone (runtime.nexloop_outreach_settings(p_tenant)->>'time_zone'))::time;v_from time;v_to time;v_count bigint;v_only text[];
begin
 for x in select value from jsonb_array_elements(runtime.nexloop_contact_preferences(p_tenant,p_world,p_consumer)) where not (value->>'dismissed')::boolean loop
  if x->>'dimension'='quiet_hours' and x->>'polarity'='affirmed' then
   v_from:=split_part(x->>'value','-',1)::time;v_to:=split_part(x->>'value','-',2)::time;
   if (v_from<=v_to and v_local>=v_from and v_local<v_to) or (v_from>v_to and (v_local>=v_from or v_local<v_to)) then return 'preference_quiet_hours';end if;
  elsif x->>'dimension'='frequency' and x->>'polarity'='affirmed' then
   select count(*) into v_count from runtime.nexloop_effect_intents i where i.tenant_id=p_tenant and i.world=p_world and i.consumer_id=p_consumer
    and i.intent_id<>p_intent and i.created_at>p_at-interval '7 days'
    and i.state in ('dispatching','unknown','fulfilled','observed_fulfilled','confirmed')
    and not exists(select 1 from runtime.nexloop_outbound_messages o where o.tenant_id=i.tenant_id and o.world=i.world and o.intent_id=i.intent_id and o.trigger_message_id is not null)
    and coalesce((control.nexloop_intent_effect_category(p_tenant,p_world,i.intent_id)->>'category')='customer_contact'
      or (control.nexloop_intent_effect_category(p_tenant,p_world,i.intent_id)->>'notification')::boolean,false);
   -- Outreach slice 4: admitted outreach sends count too.
   v_count:=v_count+(select count(*) from runtime.nexloop_outreach_sends s where s.tenant_id=p_tenant and s.world=p_world and s.consumer_id=p_consumer
    and s.admitted_at>p_at-interval '7 days' and s.state in ('dispatching','unknown','fulfilled'));
   if v_count>=floor((x->>'value')::numeric) then return 'preference_frequency';end if;
  elsif x->>'dimension'='channel' then
   if x->>'polarity'='negated' and p_channel is not null and lower(p_channel)=lower(x->>'value') then return 'preference_channel';end if;
   if x->>'polarity'='affirmed' then v_only:=array_append(v_only,lower(x->>'value'));end if;
  -- Outreach slice 4: a refused topic also refuses every topic below it (prefix match on the dot boundary).
  elsif x->>'dimension'='topic' and x->>'polarity'='negated' and p_topic is not null
   and (lower(p_topic)=lower(x->>'value') or starts_with(lower(p_topic),lower(x->>'value')||'.')) then
   return 'preference_topic';
  end if;
 end loop;
 if v_only is not null and (p_channel is null or not lower(p_channel)=any(v_only)) then return 'preference_channel';end if;
 return null;
end $$;

-- 3. The gates, shared by the dry run and the dispatch (latest body copied; changes marked "Outreach slice 4"). -----------------
-- Outreach slice 4: the contact restriction comes first (ADR-023), then the pause; marketing eligibility (gate 4) and the
-- marketing allowance are wired; the preference check reads the purpose's topic; the budget gate reads the tenant model budget.
create or replace function runtime.nexloop_outreach_gates(p_tenant text,p_world text,p_consumer text,p_purpose text,p_at timestamptz) returns jsonb
 language plpgsql stable set search_path=pg_catalog,pg_temp as $$
declare d jsonb:=runtime.nexloop_outreach_settings(p_tenant);g jsonb:='[]'::jsonb;v_local time:=(p_at at time zone (d->>'time_zone'))::time;
 v_from time:=split_part(d->>'quiet_hours','-',1)::time;v_to time:=split_part(d->>'quiet_hours','-',2)::time;v_n24 bigint;v_n7 bigint;v_code text;v_layer text;v_purpose jsonb;
 v_marketing boolean;v_m7 bigint;lim control.nexloop_budget_limits;v_used numeric;
begin
 g:=g||jsonb_build_object('gate','contact_restricted','result',case when exists(select 1 from control.nexloop_contact_restrictions r where r.tenant_id=p_tenant
   and r.world=p_world and r.consumer_id=p_consumer and r.active) then 'refuse' else 'pass' end);
 g:=g||jsonb_build_object('gate','control_paused','result',case when exists(select 1 from control.nexloop_control_scopes s where s.tenant_id=p_tenant and s.world=p_world and s.paused
   and ((s.scope_kind='tenant' and s.scope_ref='*') or (s.scope_kind='consumer' and s.scope_ref in (p_consumer,'consumer:'||p_consumer)))) then 'refuse' else 'pass' end);
 -- Outreach slice 3: the purpose must be in the current purpose list and enabled for the tenant; approval is slice 6.
 v_purpose:=runtime.nexloop_outreach_purpose(p_tenant,p_purpose);
 g:=g||jsonb_build_object('gate','purpose','result',case when not (v_purpose->>'listed')::boolean or not (v_purpose->>'enabled')::boolean then 'refuse'
   when (v_purpose->'definition'->>'requires_approval')::boolean then 'not_wired' else 'pass' end,
  'detail',case when not (v_purpose->>'listed')::boolean then 'purpose_not_allowed' when not (v_purpose->>'enabled')::boolean then 'purpose_disabled'
   when (v_purpose->'definition'->>'requires_approval')::boolean then 'approval: slice 6' else 'list v'||(v_purpose->>'list_version') end);
 -- Outreach slice 4, gate 4: marketing needs the tenant's marketing switch; opt-in (tenant or purpose) needs a consent record, and
 -- consent records arrive with slice 7, so it refuses until then. Opt-out refusals are gates 1 (restriction) and 8 (topic).
 v_marketing:=coalesce(v_purpose->'definition'->>'frequency_class','service')='marketing';
 g:=g||jsonb_build_object('gate','marketing','result',case when not v_marketing then 'pass' when not (d->'marketing'->>'enabled')::boolean then 'refuse'
   when d->'marketing'->>'consent_mode'='opt_in' or coalesce((v_purpose->'definition'->>'requires_consent')::boolean,false) then 'refuse' else 'pass' end,
  'detail',case when not v_marketing then 'not_marketing' when not (d->'marketing'->>'enabled')::boolean then 'marketing_disabled'
   when d->'marketing'->>'consent_mode'='opt_in' or coalesce((v_purpose->'definition'->>'requires_consent')::boolean,false) then 'marketing_consent_missing'
   else 'opt_out' end);
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
 g:=g||jsonb_build_object('gate','budget','result',case when lim.tenant_id is null or p_at<lim.period_start or p_at>=lim.period_end or v_used>=lim.limit_amount
   then 'refuse' else 'pass' end,'detail',case when lim.tenant_id is null then 'budget_unconfigured'
   when p_at<lim.period_start or p_at>=lim.period_end or v_used>=lim.limit_amount then 'budget_exhausted' else 'model' end);
 return g;
end $$;

-- 2. Governed owner entry -------------------------------------------------------------------------------------------------------
create function control.nexloop_governed_outreach_send(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_purpose jsonb;v_id text;v_digest text;v_head bigint;old runtime.nexloop_outreach_sends;v_day text;v_consumer text:=body->>'consumer_id';
begin
 if jsonb_typeof(body) is distinct from 'object'
  or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','consumer_id','purpose','origin_ref','body','channel','conversation_id'))
  or coalesce(v_consumer,'')!~'^[a-f0-9]{64}$' or coalesce(body->>'purpose','')!~'^[a-z][a-z0-9_.]{0,63}$'
  or coalesce(body->>'origin_ref','')!~'^(commitment|intent|plan|owner):[A-Za-z0-9._:-]{1,190}$'
  or jsonb_typeof(body->'body') is distinct from 'string' or char_length(body->>'body') not between 1 and 2000
  or body->>'channel' is distinct from 'native.webchat'
  or (body ? 'conversation_id' and body->'conversation_id'<>'null'::jsonb and coalesce(body->>'conversation_id','')!~'^[a-f0-9]{64}$') then
  raise exception 'outreach send: invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 if not exists(select 1 from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='Consumer' and object_id=v_consumer)
  or runtime.nexloop_consumer_erasing(p_tenant,p_world,v_consumer) then raise exception 'outreach send: consumer_unavailable' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 if body->>'conversation_id' is not null and not exists(select 1 from runtime.nexloop_conversations cv where cv.tenant_id=p_tenant and cv.world=p_world
   and cv.conversation_id=body->>'conversation_id' and cv.consumer_id=v_consumer) then raise exception 'outreach send: conversation_unavailable' using errcode='22023';end if;
 -- Topic, version and frequency class are frozen from the current list; whether the purpose is enabled is a dispatch gate.
 v_purpose:=runtime.nexloop_outreach_purpose(p_tenant,body->>'purpose');
 if not (v_purpose->>'listed')::boolean then raise exception 'outreach send: purpose_not_allowed' using errcode='22023';end if;
 if char_length(body->>'body')>(v_purpose->'definition'->'content_rules'->>'max_chars')::integer then raise exception 'outreach send: body_too_long' using errcode='22023';end if;
 v_day:=to_char(clock_timestamp() at time zone (runtime.nexloop_outreach_settings(p_tenant)->>'time_zone'),'YYYY-MM-DD');
 v_id:='outreach:'||encode(sha256(convert_to(jsonb_build_array(p_tenant,v_consumer,body->>'purpose',body->>'origin_ref',v_day)::text,'UTF8')),'hex');
 v_digest:=encode(sha256(convert_to(body->>'body','UTF8')),'hex');
 select * into old from runtime.nexloop_outreach_sends where tenant_id=p_tenant and world=p_world and intent_id=v_id;
 if found then
  if old.body_digest is distinct from v_digest or old.conversation_id is distinct from body->>'conversation_id' then
   raise exception 'outreach send: intent_exists' using errcode='22023';end if;
  return jsonb_build_object('intent_id',old.intent_id,'state',old.state,'replayed',true);
 end if;
 select revision into v_head from control.nexloop_control_heads where tenant_id=p_tenant and world=p_world;
 insert into runtime.nexloop_outreach_sends(tenant_id,world,intent_id,consumer_id,purpose_id,purpose_version,topic,frequency_class,channel,origin_ref,conversation_id,
   body,body_digest,initiator,principal_id,request_intent,control_snapshot)
  values(p_tenant,p_world,v_id,v_consumer,body->>'purpose',(v_purpose->>'list_version')::integer,v_purpose->'definition'->>'topic',
   v_purpose->'definition'->>'frequency_class','native.webchat',body->>'origin_ref',body->>'conversation_id',body->>'body',v_digest,'human',p_principal,p_intent,
   jsonb_build_object('control_revision',coalesce(v_head,0),
    'scopes',jsonb_build_array(jsonb_build_object('kind','consumer','ref',v_consumer),jsonb_build_object('kind','consumer','ref','consumer:'||v_consumer),
      jsonb_build_object('kind','action_type','ref','nexloop.outreach.send')),'goals','[]'::jsonb,'objects','[]'::jsonb,'budgets','[]'::jsonb));
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('outreach-send:'||v_id,'nexloop.outreach.send.accepted',p_tenant,p_principal,'outreach',p_intent,
   jsonb_build_object('intent_id',v_id,'purpose',body->>'purpose','origin_ref',body->>'origin_ref'),clock_timestamp()) on conflict do nothing;
 return jsonb_build_object('intent_id',v_id,'state','accepted','replayed',false);
end $$;
alter function control.nexloop_governed_outreach_send(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_governed_outreach_send(text,text,text,text,text,jsonb) from public;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('outreach.send','send_outreach','human','control.nexloop_governed_outreach_send(text,text,text,text,text,jsonb)','NX-022');

-- 3. Executor port ---------------------------------------------------------------------------------------------------------------
create function authz.nexloop_outreach_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-outreach-v1',
  'eios:action:nexloop.outreach.execute:1',array['nexloop_domain_worker']);c jsonb:=p_payload::jsonb;v jsonb;i runtime.nexloop_outreach_sends;
 v_limit integer;v_code text;v_state text;g jsonb;x jsonb;cfg control.nexloop_workbench_configurations;v_purpose jsonb;v_text text;
 v_resource text:='eios:action:nexloop.outreach.send:1';
begin
 perform set_config('eios.tenant_id',t,true);
 v_limit:=coalesce((c->>'limit')::integer,50);
 if v_limit not between 1 and 200 then raise exception 'outreach command invalid' using errcode='22023';end if;
 if c->>'verb'='due' then
  select coalesce(jsonb_agg(jsonb_build_object('intent_id',x2.intent_id,'state',x2.state) order by x2.updated_at),'[]'::jsonb) into v
   from (select intent_id,state,updated_at from runtime.nexloop_outreach_sends where tenant_id=t and world=p_world
     and (state in ('accepted','unknown') or (state='dispatching' and lease_until<=clock_timestamp())) order by updated_at limit v_limit) x2;
  return jsonb_build_object('intents',v);
 end if;
 if coalesce(c->>'intent_id','')!~'^outreach:[0-9a-f]{64}$' then raise exception 'outreach command invalid' using errcode='22023';end if;
 select * into i from runtime.nexloop_outreach_sends where tenant_id=t and world=p_world and intent_id=c->>'intent_id' for update;
 if not found then raise exception 'outreach intent unavailable' using errcode='42501';end if;
 if c->>'verb'='admit' then
  -- Only an accepted intent is ever admitted; dispatching / unknown are queried, never sent again.
  if i.state<>'accepted' then return jsonb_build_object('intent_id',i.intent_id,'admitted',false,'state',i.state,'fence',i.fence);end if;
  -- One admission per consumer at a time: the frequency counts below cannot race.
  perform pg_advisory_xact_lock(hashtextextended('nexloop-outreach:'||t||':'||p_world||':'||i.consumer_id,0));
  begin
   if runtime.nexloop_consumer_erasing(t,p_world,i.consumer_id) then raise exception 'erasing' using errcode='42501',hint='consumer_erasing';end if;
   perform set_config('eios.tenant_id',t,true);
   -- ADR-023: the contact restriction first; a proactive send to a restricted customer has no exception (P6).
   if exists(select 1 from control.nexloop_contact_restrictions r where r.tenant_id=t and r.world=p_world and r.consumer_id=i.consumer_id and r.active) then
    raise exception 'restricted' using errcode='42501',hint='contact_restricted';end if;
   -- The requester's authority now, not at acceptance: an active member whose current workbench role still holds the Action.
   cfg:=control.nexloop_workbench_current(t);
   if not exists(select 1 from control.nexloop_browser_memberships m where m.tenant_id=t and m.principal_id=i.principal_id and m.payload->>'status'='active')
    or (cfg.tenant_id is not null and not coalesce(cfg.roles->(cfg.members->>i.principal_id) ? v_resource,false)) then
    raise exception 'requester' using errcode='42501',hint='requester_unauthorized';end if;
   perform set_config('eios.tenant_id',t,true);
   begin
    perform authz.nexloop_assert_dispatch_controls(p_digest,p_world,i.control_snapshot);
   exception when sqlstate 'NXC01' or sqlstate 'NXC02' or sqlstate 'NXC04' then
    raise exception 'control' using errcode='42501',hint=case sqlstate when 'NXC01' then 'control_paused' when 'NXC02' then 'control_revision_stale' else 'object_revision_stale' end;
   end;
   perform set_config('eios.tenant_id',t,true);
   v_purpose:=runtime.nexloop_outreach_purpose(t,i.purpose_id);
   if (v_purpose->>'listed')::boolean and v_purpose->'definition'->>'topic' is distinct from i.topic then
    raise exception 'purpose' using errcode='42501',hint='purpose_changed';end if;
   g:=runtime.nexloop_outreach_gates(t,p_world,i.consumer_id,i.purpose_id,clock_timestamp());
   for x in select value from jsonb_array_elements(g) loop
    if x->>'result'<>'pass' then
     raise exception 'gate' using errcode='42501',hint=case x->>'gate'
      when 'contact_restricted' then 'contact_restricted' when 'control_paused' then 'control_paused'
      when 'purpose' then case when x->>'result'='not_wired' then 'approval_required' else x->>'detail' end
      when 'marketing' then x->>'detail' when 'persona_excluded' then 'persona_excluded' when 'quiet_hours' then 'quiet_hours'
      when 'frequency' then 'frequency_capped' when 'consumer_preference' then x->>'detail' when 'real_dispatch' then 'real_dispatch_closed'
      when 'budget' then x->>'detail' else 'gate_refused' end;
    end if;
   end loop;
   -- A switch change after acceptance supersedes the request, as for every external effect.
   if exists(select 1 from control.nexloop_control_events e where e.tenant_id=t and e.world=p_world and e.event_kind='real_dispatch'
     and ((e.scope_kind='action_type' and e.scope_ref=v_resource) or e.scope_kind='tenant')
     and e.revision>coalesce((i.control_snapshot->>'control_revision')::bigint,-1)) then
    raise exception 'switch' using errcode='42501',hint='real_dispatch_closed';end if;
  exception when insufficient_privilege then
   get stacked diagnostics v_code=pg_exception_hint;
   if coalesce(v_code,'')='' then raise;end if;
  end;
  perform set_config('eios.tenant_id',t,true);
  if v_code is not null and v_code<>'' then
   update runtime.nexloop_outreach_sends set state='refused',refusal_code=v_code,refusal_gates=g,finished_at=clock_timestamp(),updated_at=clock_timestamp()
    where tenant_id=t and world=p_world and intent_id=i.intent_id;
   insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
    values('outreach-refused:'||i.intent_id,'nexloop.outreach.send.refused',t,'service:'||((p_text::jsonb)->>'principal_id'),'outreach',i.intent_id,
     jsonb_build_object('code',v_code,'gates',g),clock_timestamp()) on conflict do nothing;
   return jsonb_build_object('intent_id',i.intent_id,'admitted',false,'state','refused','code',v_code);
  end if;
  update runtime.nexloop_outreach_sends set state='dispatching',fence=fence+1,lease_until=clock_timestamp()+interval '30 seconds',
   admitted_at=clock_timestamp(),updated_at=clock_timestamp() where tenant_id=t and world=p_world and intent_id=i.intent_id returning * into i;
  -- A marketing message carries the tenant's unsubscribe notice, appended by the server (P8).
  v_text:=i.body||case when i.frequency_class='marketing' then E'\n'||(runtime.nexloop_outreach_settings(t)->'marketing'->>'unsubscribe_notice') else '' end;
  return jsonb_build_object('intent_id',i.intent_id,'admitted',true,'state',i.state,'fence',i.fence,
   'request',jsonb_build_object('intent_id',i.intent_id,'consumer_ref',i.consumer_id,'channel',i.channel,'topic',i.topic,'purpose',i.purpose_id,
    'conversation_id',i.conversation_id,'text',v_text));
 elsif c->>'verb'='observe' then
  if c->>'outcome' not in ('fulfilled','failed','unknown') or jsonb_typeof(c->'fence') is distinct from 'number'
   or (c ? 'provider_reference' and coalesce(c->>'provider_reference','')!~'^[A-Za-z0-9._:-]{1,200}$') then
   raise exception 'outreach command invalid' using errcode='22023';end if;
  if i.state not in ('dispatching','unknown') or i.fence<>(c->>'fence')::bigint then
   if i.state in ('fulfilled','failed') and i.state=c->>'outcome' then return jsonb_build_object('intent_id',i.intent_id,'state',i.state,'replayed',true);end if;
   raise exception 'outreach observation fenced' using errcode='40001';end if;
  v_state:=c->>'outcome';
  update runtime.nexloop_outreach_sends set state=v_state,provider_reference=coalesce(c->>'provider_reference',provider_reference),
   lease_until=null,finished_at=case when v_state in ('fulfilled','failed') then clock_timestamp() end,updated_at=clock_timestamp()
   where tenant_id=t and world=p_world and intent_id=i.intent_id;
  insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
   values('outreach-'||v_state||':'||i.intent_id||':'||i.fence,'nexloop.outreach.send.'||v_state,t,'service:'||((p_text::jsonb)->>'principal_id'),'outreach',
    i.intent_id,jsonb_build_object('fence',i.fence),clock_timestamp()) on conflict do nothing;
  return jsonb_build_object('intent_id',i.intent_id,'state',v_state,'fence',i.fence);
 elsif c->>'verb'='status' then
  return jsonb_build_object('intent_id',i.intent_id,'state',i.state,'refusal_code',i.refusal_code,'fence',i.fence,'purpose',i.purpose_id,
   'provider_reference',i.provider_reference);
 end if;
 raise exception 'outreach command invalid' using errcode='22023';
end $$;
alter function authz.nexloop_outreach_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_outreach_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_outreach_command(text,text,text,text,text) to nexloop_domain_worker;
