-- Proactive outreach, slice 5: the plan reevaluation Run may ask for a proactive message (docs/implementation/outreach-impl-plan.md;
-- design §3.1/§3.3). The migration number lives only in the file name and the catalog; nothing below repeats it.
-- 1. runtime.nexloop_outreach_sends gains the Agent initiator: initiator 'agent' carries its run_id (principal run:<id>); the
--    human owner's rows are unchanged.
-- 2. runtime.nexloop_outreach_accept: the acceptance of the owner's send, moved unchanged out of the governed handler (which now
--    validates and calls it) so that both entries freeze the request and derive the business intent id the same way.
-- 3. Run entry authz.nexloop_run_outreach_command (nexloop-run-outreach-v1; EXECUTE on nexloop.outreach.run_submit:1, held by the
--    runtime worker): the Run's live activation under the caller's own task lease and the exact command (as the incentive Run
--    entry), only a plan reevaluation Run (never a reply fallback Run: ADR-023 §2.7), the Run credential naming
--    nexloop.outreach.send:1, the Role execution ceiling holding it, the Run's own Consumer. The origin is the plan
--    (plan:<plan_id>), never chosen by the model; the Run gives only the purpose and the body. Accepted is not sent: it goes
--    through the same executor admission and every gate of slice 4 at dispatch.
-- 4. Executor admission (latest body copied, change marked "Outreach slice 5"): an Agent request stands only while the Run's Role
--    ceiling still holds the Action (the human requester check is unchanged).
-- 5. Context (contract: context-pack-v6 optional section outreach): a plan reevaluation Run whose Role ceiling holds the send gets
--    the outreach section (enabled purposes of the current list, marketing state) and, as the message Run since 0227, the operator
--    persona section with the tenant's AI disclosure configuration; the Role bind re-derives both and refuses a pack that differs.

alter table runtime.nexloop_outreach_sends drop constraint nexloop_outreach_sends_initiator_check,
 add constraint nexloop_outreach_sends_initiator_check check(initiator in ('human','agent'));
alter table runtime.nexloop_outreach_sends add column run_id uuid,
 add constraint nexloop_outreach_sends_run_check check((initiator='agent')=(run_id is not null) and (initiator<>'agent' or principal_id='run:'||run_id::text));

-- 2. Shared acceptance -----------------------------------------------------------------------------------------------------------
create function runtime.nexloop_outreach_accept(p_tenant text,p_world text,p_consumer text,p_purpose text,p_origin text,p_body text,p_conversation text,
  p_initiator text,p_principal text,p_intent text,p_run uuid) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_purpose jsonb;v_id text;v_digest text;v_head bigint;old runtime.nexloop_outreach_sends;v_day text;
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
 v_day:=to_char(clock_timestamp() at time zone (runtime.nexloop_outreach_settings(p_tenant)->>'time_zone'),'YYYY-MM-DD');
 v_id:='outreach:'||encode(sha256(convert_to(jsonb_build_array(p_tenant,p_consumer,p_purpose,p_origin,v_day)::text,'UTF8')),'hex');
 v_digest:=encode(sha256(convert_to(p_body,'UTF8')),'hex');
 select * into old from runtime.nexloop_outreach_sends where tenant_id=p_tenant and world=p_world and intent_id=v_id;
 if found then
  if old.body_digest is distinct from v_digest or old.conversation_id is distinct from p_conversation then
   raise exception 'outreach send: intent_exists' using errcode='22023';end if;
  return jsonb_build_object('intent_id',old.intent_id,'state',old.state,'replayed',true);
 end if;
 select revision into v_head from control.nexloop_control_heads where tenant_id=p_tenant and world=p_world;
 insert into runtime.nexloop_outreach_sends(tenant_id,world,intent_id,consumer_id,purpose_id,purpose_version,topic,frequency_class,channel,origin_ref,conversation_id,
   body,body_digest,initiator,principal_id,request_intent,control_snapshot,run_id)
  values(p_tenant,p_world,v_id,p_consumer,p_purpose,(v_purpose->>'list_version')::integer,v_purpose->'definition'->>'topic',
   v_purpose->'definition'->>'frequency_class','native.webchat',p_origin,p_conversation,p_body,v_digest,p_initiator,p_principal,p_intent,
   jsonb_build_object('control_revision',coalesce(v_head,0),
    'scopes',jsonb_build_array(jsonb_build_object('kind','consumer','ref',p_consumer),jsonb_build_object('kind','consumer','ref','consumer:'||p_consumer),
      jsonb_build_object('kind','action_type','ref','nexloop.outreach.send')),'goals','[]'::jsonb,'objects','[]'::jsonb,'budgets','[]'::jsonb),p_run);
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('outreach-send:'||v_id,'nexloop.outreach.send.accepted',p_tenant,p_principal,'outreach',p_intent,
   jsonb_build_object('intent_id',v_id,'purpose',p_purpose,'origin_ref',p_origin,'initiator',p_initiator),clock_timestamp()) on conflict do nothing;
 return jsonb_build_object('intent_id',v_id,'state','accepted','replayed',false);
end $$;
alter function runtime.nexloop_outreach_accept(text,text,text,text,text,text,text,text,text,text,uuid) owner to nexloop_owner;
revoke all on function runtime.nexloop_outreach_accept(text,text,text,text,text,text,text,text,text,text,uuid) from public;

create or replace function control.nexloop_governed_outreach_send(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
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
 -- Outreach slice 5: the shared acceptance (body moved unchanged into runtime.nexloop_outreach_accept).
 return runtime.nexloop_outreach_accept(p_tenant,p_world,v_consumer,body->>'purpose',body->>'origin_ref',body->>'body',body->>'conversation_id','human',p_principal,p_intent,null);
end $$;

-- 3. Run entry --------------------------------------------------------------------------------------------------------------------
create function authz.nexloop_run_outreach_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-run-outreach-v1','eios:action:nexloop.outreach.run_submit:1',
  array['nexloop_scheduler','nexloop_domain_worker']);
 c jsonb:=p_payload::jsonb;p jsonb:=c->'parameters';act authz.nexloop_runtime_activations%rowtype;j runtime.jobs%rowtype;b authz.nexloop_runtime_run_bindings%rowtype;
 v_command jsonb;v_consumer text;v_plan uuid;rb authz.nexloop_role_run_bindings%rowtype;ceiling jsonb;
begin
 select * into act from authz.nexloop_runtime_activations where activation_ref=c->>'activation_ref' and tenant_id=t and world=p_world;
 if not found then raise exception 'outreach activation unavailable' using errcode='42501';end if;
 select * into j from runtime.jobs where tenant_id=t and job_id=act.task_id for share;
 if not found or j.status<>'running' or j.lease_until<=clock_timestamp() or j.lease_credential is distinct from p_digest
  or act.lease_credential is distinct from p_digest or act.fence is distinct from j.fencing_token then
  raise exception 'outreach lease unavailable' using errcode='42501';end if;
 select * into b from authz.nexloop_runtime_run_bindings where run_id=act.run_id;
 v_command:=(c->>'command_text')::jsonb;
 if b.command_digest is distinct from encode(sha256(convert_to(c->>'command_text','UTF8')),'hex') or v_command->>'run_id' is distinct from act.run_id::text then
  raise exception 'outreach command mismatch' using errcode='42501';end if;
 if jsonb_typeof(p) is distinct from 'object' or exists(select 1 from jsonb_object_keys(p) k where k not in ('purpose','body'))
  or coalesce(p->>'purpose','')!~'^[a-z][a-z0-9_.]{0,63}$' or jsonb_typeof(p->'body') is distinct from 'string' or char_length(p->>'body') not between 1 and 2000 then
  raise exception 'outreach send: invalid' using errcode='22023';end if;
 -- ADR-023 §2.7: a reply fallback Run has exactly one tool, the bound reply.
 if authz.nexloop_is_fallback_run(act.run_id) then raise exception 'outreach outside a plan reevaluation Run' using errcode='42501';end if;
 perform set_config('eios.tenant_id',t,true);
 select plan_id into v_plan from runtime.nexloop_plan_runs where tenant_id=t and world=p_world and run_id=act.run_id;
 if v_plan is null then raise exception 'outreach outside a plan reevaluation Run' using errcode='42501';end if;
 v_consumer:=substring(coalesce(v_command->>'consumer_ref','') from '^consumer:([a-f0-9]{64})$');
 if v_consumer is null then raise exception 'outreach needs the Run''s Consumer' using errcode='42501';end if;
 if not exists(select 1 from authz.nexloop_run_credentials r where r.run_id=act.run_id and r.status='active'
   and 'eios:action:nexloop.outreach.send:1'=any(r.allowed_resources)) then raise exception 'outreach outside the Run' using errcode='42501';end if;
 perform set_config('eios.tenant_id',t,true);
 select * into rb from authz.nexloop_role_run_bindings where run_id=act.run_id;
 if found then
  select o.properties into ceiling from ontology.objects d join ontology.objects o on o.tenant_id=d.tenant_id and o.world=d.world
    and o.type_name='RoleExecutionCeiling' and o.object_id=d.properties->>'ceiling_ref'
   where d.tenant_id=t and d.world=p_world and d.type_name='RoleDefinition' and d.object_id=rb.role_id;
  if ceiling is null or not coalesce(ceiling->'action_resources','[]'::jsonb) ? 'eios:action:nexloop.outreach.send:1'
   or rb.consumer_id is distinct from v_consumer then raise exception 'outreach outside the Role ceiling' using errcode='42501';end if;
 end if;
 return runtime.nexloop_outreach_accept(t,p_world,v_consumer,p->>'purpose','plan:'||v_plan::text,p->>'body',null,'agent','run:'||act.run_id::text,
  'run:'||act.run_id::text,act.run_id)||jsonb_build_object('run_id',act.run_id,'origin_ref','plan:'||v_plan::text);
end $$;
alter function authz.nexloop_run_outreach_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_run_outreach_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_run_outreach_command(text,text,text,text,text) to nexloop_scheduler,nexloop_domain_worker;

-- 4. Executor admission (latest body copied; change marked "Outreach slice 5") ------------------------------------------------
create or replace function authz.nexloop_outreach_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
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
   if i.initiator='human' and (not exists(select 1 from control.nexloop_browser_memberships m where m.tenant_id=t and m.principal_id=i.principal_id and m.payload->>'status'='active')
    or (cfg.tenant_id is not null and not coalesce(cfg.roles->(cfg.members->>i.principal_id) ? v_resource,false))) then
    raise exception 'requester' using errcode='42501',hint='requester_unauthorized';end if;
   -- Outreach slice 5: a reevaluation Run's request stands only while its Role's execution ceiling still holds the Action.
   if i.initiator='agent' and exists(select 1 from authz.nexloop_role_run_bindings rb where rb.run_id=i.run_id) and not exists(
     select 1 from authz.nexloop_role_run_bindings rb join ontology.objects d on d.tenant_id=t and d.world=p_world and d.type_name='RoleDefinition' and d.object_id=rb.role_id
      join ontology.objects o on o.tenant_id=d.tenant_id and o.world=d.world and o.type_name='RoleExecutionCeiling' and o.object_id=d.properties->>'ceiling_ref'
     where rb.run_id=i.run_id and coalesce(o.properties->'action_resources','[]'::jsonb) ? v_resource) then
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

-- 5. Context: the outreach section of a plan reevaluation Run and the persona section on the Role path -----------------------------
-- Present only when the Run's Role execution ceiling holds nexloop.outreach.send:1 (and the Run is a plan reevaluation Run): the
-- current list version, the purposes enabled for the tenant, the marketing state. Deterministic: no clock, no gate results.
create function runtime.nexloop_outreach_run_section(p_tenant text,p_world text,p_consumer text,p_run uuid) returns jsonb
 language plpgsql stable set search_path=pg_catalog,pg_temp as $$
declare l control.nexloop_outreach_purpose_lists;s jsonb:=runtime.nexloop_outreach_settings(p_tenant);
begin
 if not exists(select 1 from runtime.nexloop_plan_runs pr where pr.tenant_id=p_tenant and pr.world=p_world and pr.run_id=p_run)
  or not exists(select 1 from authz.nexloop_role_run_bindings rb join ontology.objects d on d.tenant_id=p_tenant and d.world=p_world and d.type_name='RoleDefinition'
     and d.object_id=rb.role_id join ontology.objects o on o.tenant_id=d.tenant_id and o.world=d.world and o.type_name='RoleExecutionCeiling' and o.object_id=d.properties->>'ceiling_ref'
    where rb.run_id=p_run and rb.consumer_id=p_consumer and coalesce(o.properties->'action_resources','[]'::jsonb) ? 'eios:action:nexloop.outreach.send:1') then
  return null;end if;
 select * into l from control.nexloop_outreach_purpose_lists order by version desc limit 1;
 return jsonb_build_object('schema','nexloop-outreach-context/1','list_version',l.version,
  'purposes',coalesce((select jsonb_agg(jsonb_build_object('purpose_id',d->>'purpose_id','kind',d->>'kind','topic',d->>'topic','frequency_class',d->>'frequency_class',
     'requires_approval',(d->>'requires_approval')::boolean,'content_rules',d->'content_rules') order by n)
    from jsonb_array_elements(l.definition->'purposes') with ordinality x(d,n)
    where (runtime.nexloop_outreach_purpose(p_tenant,d->>'purpose_id')->>'enabled')::boolean),'[]'::jsonb),
  'marketing',jsonb_build_object('enabled',(s->'marketing'->>'enabled')::boolean,'consent_mode',s->'marketing'->>'consent_mode'));
end $$;
alter function runtime.nexloop_outreach_run_section(text,text,text,uuid) owner to nexloop_owner;
revoke all on function runtime.nexloop_outreach_run_section(text,text,text,uuid) from public;

-- The Role producer reads it as the persona (0227 authz.nexloop_context_operator_persona): service identity, tenant from it.
create function authz.nexloop_context_outreach(p_digest text,p_world text,p_consumer text,p_run uuid) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;v_tenant text;
begin
 if session_user<>'nexloop_api' or p_world is distinct from 'real' or coalesce(p_consumer,'')!~'^[a-f0-9]{64}$' or p_run is null then
  raise exception 'outreach context unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ident->'binding'->>'subject_kind' is distinct from 'service' then raise exception 'outreach context unavailable' using errcode='42501';end if;
 v_tenant:=ident->'binding'->>'tenant_id';perform set_config('eios.tenant_id',v_tenant,true);
 return runtime.nexloop_outreach_run_section(v_tenant,p_world,p_consumer,p_run);
end $$;
alter function authz.nexloop_context_outreach(text,text,text,uuid) owner to nexloop_owner;
revoke all on function authz.nexloop_context_outreach(text,text,text,uuid) from public;
grant execute on function authz.nexloop_context_outreach(text,text,text,uuid) to nexloop_api;

-- The Role Run bind (0105 body copied; changes marked "Outreach slice 5").
create or replace function authz.nexloop_role_context_v6_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;p jsonb;k bytea;ident jsonb;tenant text;principal text;ca jsonb;cp jsonb;snapshot jsonb;pack jsonb;
 b runtime.nexloop_role_context_artifacts;artifact runtime.nexloop_local_artifacts;proof jsonb;key text;consumer text;fresh boolean;namespace text;
 context_uuid uuid;run uuid;binding jsonb;core jsonb;opersona jsonb;outreach jsonb;
begin
 if session_user<>'nexloop_api' or p_world is distinct from 'real' or octet_length(p_text)>1048576 or octet_length(p_payload)>1048576
  or a->>'protocol' is distinct from 'nexloop-context-v6-v1' or a->>'resource_id' is distinct from 'eios:action:nexloop.context.assemble:1'
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds' then
  raise exception 'context v6 unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-context-v6-v1:'||p_text,'UTF8'),k,'sha256'),'hex') then
  raise exception 'context v6 unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_context_assemble(p_digest,p_world,a,p_payload);
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ident->'run_context' is distinct from 'null'::jsonb or ident->'binding'->>'subject_kind' is distinct from 'service' then raise exception 'context v6 unavailable' using errcode='42501';end if;
 tenant:=ident->'binding'->>'tenant_id';principal:=ident->'binding'->>'subject_principal_id';perform set_config('eios.tenant_id',tenant,true);
 p:=p_payload::jsonb;
 if p->>'verb' is distinct from 'bind' or jsonb_typeof(p->'core') is distinct from 'object' or p->>'pack_text' is null or octet_length(p->>'pack_text')>65536 then
  raise exception 'context v6 unavailable' using errcode='42501';end if;
 ca:=(p->'core'->>'text')::jsonb;cp:=(p->'core'->>'payload')::jsonb;
 if cp->>'verb' is distinct from 'snapshot' then raise exception 'context v6 unavailable' using errcode='42501';end if;
 snapshot:=authz.nexloop_role_context_command(p_digest,p_world,p->'core'->>'text',p->'core'->>'signature',p->'core'->>'payload');
 pack:=(p->>'pack_text')::jsonb;
 if p->>'pack_digest' is distinct from encode(sha256(convert_to(p->>'pack_text','UTF8')),'hex') or runtime.nexloop_canonical_json(pack) is distinct from p->>'pack_text'
  or pack->>'schema_version' is distinct from 'nexloop.context-pack.v6' or (select count(*) from jsonb_object_keys(pack))<>18+(case when pack ? 'operator_persona' then 1 else 0 end)+(case when pack ? 'outreach' then 1 else 0 end)
  or not pack ?& array['strategy_ref','bindings','role','current_event','user_statement','goal','formal_facts','current_constraints','supply','constraints','consumer_state',
   'open_work','evidence','semantics','experience','budget_report','insufficient'] then raise exception 'context v6 pack invalid' using errcode='42501';end if;
 foreach key in array array['bindings','formal_facts','current_constraints','supply'] loop
  if pack->key is distinct from snapshot->key then raise exception 'context v6 core mismatch' using errcode='42501';end if;
 end loop;
 if pack->'role' is distinct from jsonb_build_object('role_binding',snapshot->'role_binding','role_policy',coalesce(snapshot->'role_policy','null'::jsonb)) then
  raise exception 'context v6 Role section mismatch' using errcode='42501';end if;
 if pack->'current_event' is distinct from snapshot->'trigger_statement' or pack->'user_statement' is distinct from 'null'::jsonb then
  raise exception 'context v6 current event mismatch' using errcode='42501';end if;
 run:=(pack->'bindings'->>'run_id')::uuid;context_uuid:=runtime.nexloop_role_pack_context_id(run);consumer:=snapshot->'role_binding'->'binding'->>'consumer_id';
 if consumer is null or not exists(select 1 from jsonb_array_elements(pack->'formal_facts') f where f->>'type'='Consumer' and f->>'id'=consumer) then
  raise exception 'context v6 consumer mismatch' using errcode='42501';end if;
 binding:=authz.nexloop_context_command_binding(cp->'command');
 select * into b from runtime.nexloop_role_context_artifacts where run_id=run for update;
 fresh:=not found;
 if not fresh and (b.pack_text is distinct from p->>'pack_text' or b.command_binding is distinct from binding or b.tenant_id is distinct from tenant) then
  raise exception 'role Artifact conflict' using errcode='23505';end if;
 if fresh then
  -- Role trigger is the current event; Role binding and policy are governed policy provenance.
  select jsonb_build_array(
    jsonb_build_object('section','current_event','ref',pack->'current_event'->>'provenance','revision','1','hash',runtime.nexloop_context_hash(pack->'current_event'),
     'kind','current_message','decision',runtime.nexloop_context_hash(ca->'role_envelope')),
    jsonb_build_object('section','role','ref',snapshot->'role_binding'->'binding'->>'role_ref','revision',snapshot->'role_binding'->'binding'->>'role_revision',
     'hash',runtime.nexloop_context_hash(pack->'role'),'kind','policy','decision',runtime.nexloop_context_hash(ca->'role_envelope')),
    jsonb_build_object('section','constraints','ref','eios:action:nexloop.service.request:1','revision',runtime.nexloop_context_hash(pack->'current_constraints'),
     'hash',runtime.nexloop_context_hash(pack->'current_constraints'),'kind','policy','decision',runtime.nexloop_context_hash(ca)),
    jsonb_build_object('section','constraints','ref','eios:object:'||(pack->'supply'->>'offering_id'),'revision',pack->'supply'->>'offering_revision',
     'hash',runtime.nexloop_context_hash(pack->'supply'),'kind','formal_object','decision',runtime.nexloop_context_hash(ca->'catalog_envelope')))
   ||coalesce((select jsonb_agg(jsonb_build_object('section','constraints','ref','eios:object:'||(f->>'type')||'/'||(f->>'id'),'revision',f->>'revision',
     'hash',runtime.nexloop_context_hash(f),'kind','formal_object','decision',runtime.nexloop_context_hash(ca->'formal_reads'->(f->>'type')))) from jsonb_array_elements(pack->'formal_facts') f),'[]'::jsonb)
   into core;
  -- Outreach slice 5: the operator persona section (as the message Run, 0227) and the outreach section are exactly the re-derived
  -- ones, present only when they exist; both are cited as role sources.
  opersona:=runtime.nexloop_operator_persona_section(tenant,p_world,consumer);
  if (pack ? 'operator_persona') is distinct from (opersona is not null) or (opersona is not null and pack->'operator_persona' is distinct from opersona) then
   raise exception 'context v6 operator persona mismatch' using errcode='42501';end if;
  if opersona is not null then
   core:=core||jsonb_build_array(jsonb_build_object('section','role','ref',opersona->>'persona_ref','revision',opersona->>'version',
    'hash',runtime.nexloop_context_hash(opersona),'kind','policy','decision',runtime.nexloop_context_hash(ca)));
   perform runtime.nexloop_operator_persona_settle(tenant,p_world,consumer,principal,'context:'||context_uuid::text,null);
  end if;
  outreach:=runtime.nexloop_outreach_run_section(tenant,p_world,consumer,run);
  if (pack ? 'outreach') is distinct from (outreach is not null) or (outreach is not null and pack->'outreach' is distinct from outreach) then
   raise exception 'context v6 outreach mismatch' using errcode='42501';end if;
  if outreach is not null then
   core:=core||jsonb_build_array(jsonb_build_object('section','role','ref','outreach-purposes@'||(outreach->>'list_version'),'revision',outreach->>'list_version',
    'hash',runtime.nexloop_context_hash(outreach),'kind','policy','decision',runtime.nexloop_context_hash(ca)));
  end if;
  perform authz.nexloop_context_v6_sections(p_digest,p_world,tenant,principal,a,p,pack,
   array(select cv.conversation_id from runtime.nexloop_conversations cv where cv.tenant_id=tenant and cv.world=p_world and cv.consumer_id=consumer),consumer,run,context_uuid,core);
 elsif not exists(select 1 from runtime.nexloop_context_packs c where c.tenant_id=tenant and c.world=p_world and c.context_id=context_uuid and c.run_id=run and c.pack_digest=p->>'pack_digest') then
  raise exception 'role Artifact conflict' using errcode='23505';
 end if;
 -- Artifact: same rules as the frozen Role bind, against the v6 bytes.
 if jsonb_typeof(ca->'artifact_proofs') is distinct from 'array' or jsonb_array_length(ca->'artifact_proofs')<>2
  or (select jsonb_agg(v->>'operation' order by v->>'operation') from jsonb_array_elements(ca->'artifact_proofs') t(v)) is distinct from '["create","read"]'::jsonb then
  raise exception 'Artifact CREATE/READ required' using errcode='42501';end if;
 for proof in select value from jsonb_array_elements(ca->'artifact_proofs') loop
  if proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'Artifact proof expired' using errcode='42501';end if;
  perform authz.nexloop_assert_artifact_authority(p_digest,p_world,proof);
 end loop;
 namespace:=encode(sha256(convert_to(tenant,'UTF8')||decode('00','hex')||convert_to(p_world,'UTF8')),'hex');
 select * into artifact from runtime.nexloop_local_artifacts where tenant_id=tenant and world=p_world and artifact_id=pack->'bindings'->>'artifact_id' for share;
 if not found or artifact.artifact_id is distinct from p->>'artifact_id' or artifact.status is distinct from 'available' or artifact.principal_id is distinct from principal
  or artifact.sha256 is distinct from p->>'pack_digest' or artifact.size_bytes is distinct from octet_length(p->>'pack_text')
  or artifact.media_type is distinct from 'application/vnd.nexloop.context+json' or artifact.retention_until is null or artifact.retention_until<=clock_timestamp()
  or artifact.object_key is distinct from namespace||'/'||artifact.artifact_id then raise exception 'role Artifact unavailable' using errcode='42501';end if;
 if fresh then
  insert into runtime.nexloop_role_context_artifacts values(run,tenant,p_world,principal,p_digest,(pack->'bindings'->>'context_id')::uuid,artifact.artifact_id,namespace,p->>'pack_text',p->>'pack_digest',binding,clock_timestamp());
 end if;
 -- Tail: every authority used above is still current at return.
 for proof in select value from jsonb_array_elements(a->'read_proofs') loop
  if (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'context read proof expired' using errcode='42501';end if;
  perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
 end loop;
 for proof in select value from jsonb_array_elements(ca->'artifact_proofs') loop perform authz.nexloop_assert_artifact_authority(p_digest,p_world,proof);end loop;
 perform authz.nexloop_assert_context_assemble(p_digest,p_world,a,p_payload);
 return jsonb_build_object('context_id',context_uuid,'run_id',run,'pack_digest',p->>'pack_digest','strategy_ref',pack->>'strategy_ref','bound',true);
end $$;
