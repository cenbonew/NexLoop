-- Proactive outreach, slice 6: optional approval (docs/implementation/outreach-impl-plan.md slice 6; design P5; the purpose list's
-- requires_approval). The migration number lives only in the file name and the catalog (renumbered at integration); nothing below
-- repeats it.
-- 1. A send for a purpose with requires_approval=true is accepted as `pending_approval` with an approval row whose window is
--    runtime.nexloop_outreach_approval_window() (24 hours, one place to change). Nothing is admitted while pending.
-- 2. Governed Action outreach.approve / decide_outreach_approval (human): approve or reject one pending send, with a reason. The
--    approver must hold the Action in their current workbench role (granted to the owner role), must not be the requester, and the
--    window must still be open. Approve moves the send to `accepted`; reject ends it `refused/approval_rejected`; an approval past
--    its window ends it `refused/approval_expired` (at the decision or when the executor lists due work). Rejected and expired are
--    terminal; a decision is recorded once.
-- 3. Approval is not an exemption: the executor admits an approved send through every dispatch gate as before (the contact
--    restriction first), and the purpose gate passes for an approved intent only. The real dispatch switch stays closed.
-- 4. Workbench observe verb outreach_approvals (owner only): pending approvals with intent id, purpose, request time, expiry and
--    requester; never the body.

-- 1. States and the approval record ------------------------------------------------------------------------------------------------
alter table runtime.nexloop_outreach_sends drop constraint nexloop_outreach_sends_state_check;
alter table runtime.nexloop_outreach_sends add constraint nexloop_outreach_sends_state_check
 check(state in ('pending_approval','accepted','dispatching','unknown','fulfilled','failed','refused'));

create table runtime.nexloop_outreach_approvals (
 tenant_id text not null,world text not null,intent_id text not null,requested_by text not null,
 requested_at timestamptz not null default clock_timestamp(),expires_at timestamptz not null,
 decision text check(decision in ('approved','rejected','expired')),decided_by text,decided_at timestamptz,
 reason text check(char_length(reason) between 1 and 500),decision_intent text,
 primary key(tenant_id,world,intent_id),foreign key(tenant_id,world,intent_id) references runtime.nexloop_outreach_sends(tenant_id,world,intent_id),
 check(expires_at>requested_at),check((decision is null)=(decided_at is null)),
 check(decision is null or decision='expired' or (decided_by is not null and reason is not null and decision_intent is not null)),
 check(decided_by is null or decided_by<>requested_by)
);
alter table runtime.nexloop_outreach_approvals owner to nexloop_owner;
alter table runtime.nexloop_outreach_approvals enable row level security;
alter table runtime.nexloop_outreach_approvals force row level security;
create policy tenant_boundary on runtime.nexloop_outreach_approvals to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_outreach_approvals from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;
-- A decision is recorded once; the request side never changes; rows are never deleted.
create function runtime.nexloop_outreach_approval_guard() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if tg_op='DELETE' then raise exception 'outreach approvals are never deleted' using errcode='42501';end if;
 if old.decision is not null or (new.tenant_id,new.world,new.intent_id,new.requested_by,new.requested_at,new.expires_at)
   is distinct from (old.tenant_id,old.world,old.intent_id,old.requested_by,old.requested_at,old.expires_at) then
  raise exception 'outreach approval is immutable' using errcode='42501';end if;
 return new;
end $$;
alter function runtime.nexloop_outreach_approval_guard() owner to nexloop_owner;
revoke all on function runtime.nexloop_outreach_approval_guard() from public;
create trigger outreach_approval_guard before update or delete on runtime.nexloop_outreach_approvals for each row execute function runtime.nexloop_outreach_approval_guard();

create function runtime.nexloop_outreach_approval_window() returns interval language sql immutable set search_path=pg_catalog,pg_temp as $$ select interval '24 hours' $$;
alter function runtime.nexloop_outreach_approval_window() owner to nexloop_owner;
revoke all on function runtime.nexloop_outreach_approval_window() from public;

-- 2. Expiry: a pending send past its window ends refused/approval_expired (terminal). -----------------------------------------------
create function runtime.nexloop_outreach_expire_approvals(p_tenant text,p_world text) returns integer
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare n integer:=0;r record;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 for r in select a.intent_id from runtime.nexloop_outreach_approvals a join runtime.nexloop_outreach_sends s
   on s.tenant_id=a.tenant_id and s.world=a.world and s.intent_id=a.intent_id
  where a.tenant_id=p_tenant and a.world=p_world and a.decision is null and a.expires_at<=clock_timestamp() and s.state='pending_approval'
  order by a.expires_at limit 200 for update of a,s skip locked loop
  update runtime.nexloop_outreach_approvals set decision='expired',decided_at=clock_timestamp() where tenant_id=p_tenant and world=p_world and intent_id=r.intent_id;
  update runtime.nexloop_outreach_sends set state='refused',refusal_code='approval_expired',finished_at=clock_timestamp(),updated_at=clock_timestamp()
   where tenant_id=p_tenant and world=p_world and intent_id=r.intent_id;
  insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
   values('outreach-approval-expired:'||r.intent_id,'nexloop.outreach.approval.expired',p_tenant,'service:outreach','outreach',r.intent_id,
    jsonb_build_object('intent_id',r.intent_id),clock_timestamp()) on conflict do nothing;
  n:=n+1;
 end loop;
 return n;
end $$;
alter function runtime.nexloop_outreach_expire_approvals(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_outreach_expire_approvals(text,text) from public;

-- 3. The gates (latest body copied; changes marked "Outreach slice 6") ------------------------------------------------------------
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
 -- Outreach slice 3: the purpose must be in the current purpose list and enabled for the tenant. Outreach slice 6: a purpose that
 -- requires approval refuses with approval_required; the dispatch lets it pass only for an intent whose approval was granted.
 v_purpose:=runtime.nexloop_outreach_purpose(p_tenant,p_purpose);
 g:=g||jsonb_build_object('gate','purpose','result',case when not (v_purpose->>'listed')::boolean or not (v_purpose->>'enabled')::boolean then 'refuse'
   when (v_purpose->'definition'->>'requires_approval')::boolean then 'refuse' else 'pass' end,  -- Outreach slice 6: refused until approved (admit)
  'detail',case when not (v_purpose->>'listed')::boolean then 'purpose_not_allowed' when not (v_purpose->>'enabled')::boolean then 'purpose_disabled'
   when (v_purpose->'definition'->>'requires_approval')::boolean then 'approval_required' else 'list v'||(v_purpose->>'list_version') end);
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

-- 4. Acceptance (integration of slices 5 and 6: the shared acceptance of slice 5, runtime.nexloop_outreach_accept, copied with the
--    approval of slice 6 marked "Outreach slice 6"; the governed handler of slice 5 already delegates to it, so both the owner's
--    send and a reevaluation Run's request wait for approval) -----------------------------------------------------------------------
create or replace function runtime.nexloop_outreach_accept(p_tenant text,p_world text,p_consumer text,p_purpose text,p_origin text,p_body text,p_conversation text,
  p_initiator text,p_principal text,p_intent text,p_run uuid) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_purpose jsonb;v_id text;v_digest text;v_head bigint;old runtime.nexloop_outreach_sends;v_day text;
 v_approval boolean;  -- Outreach slice 6
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
   body,body_digest,initiator,principal_id,request_intent,control_snapshot,run_id,state)
  values(p_tenant,p_world,v_id,p_consumer,p_purpose,(v_purpose->>'list_version')::integer,v_purpose->'definition'->>'topic',
   v_purpose->'definition'->>'frequency_class','native.webchat',p_origin,p_conversation,p_body,v_digest,p_initiator,p_principal,p_intent,
   jsonb_build_object('control_revision',coalesce(v_head,0),
    'scopes',jsonb_build_array(jsonb_build_object('kind','consumer','ref',p_consumer),jsonb_build_object('kind','consumer','ref','consumer:'||p_consumer),
      jsonb_build_object('kind','action_type','ref','nexloop.outreach.send')),'goals','[]'::jsonb,'objects','[]'::jsonb,'budgets','[]'::jsonb),p_run,
   case when v_approval then 'pending_approval' else 'accepted' end);
 if v_approval then  -- Outreach slice 6: the approval window starts at acceptance
  insert into runtime.nexloop_outreach_approvals(tenant_id,world,intent_id,requested_by,expires_at)
   values(p_tenant,p_world,v_id,p_principal,clock_timestamp()+runtime.nexloop_outreach_approval_window());
 end if;
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('outreach-send:'||v_id,'nexloop.outreach.send.accepted',p_tenant,p_principal,'outreach',p_intent,
   jsonb_build_object('intent_id',v_id,'purpose',p_purpose,'origin_ref',p_origin,'initiator',p_initiator),clock_timestamp()) on conflict do nothing;
 return jsonb_build_object('intent_id',v_id,'state',case when v_approval then 'pending_approval' else 'accepted' end,'replayed',false);
end $$;

-- 5. Executor port (slice 5 body copied - its agent requester rule kept; changes marked "Outreach slice 6") ---------------------------------------------------------
create or replace function authz.nexloop_outreach_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-outreach-v1',
  'eios:action:nexloop.outreach.execute:1',array['nexloop_domain_worker']);c jsonb:=p_payload::jsonb;v jsonb;i runtime.nexloop_outreach_sends;
 v_limit integer;v_code text;v_state text;g jsonb;x jsonb;cfg control.nexloop_workbench_configurations;v_purpose jsonb;v_text text;
 v_resource text:='eios:action:nexloop.outreach.send:1';v_approved boolean;  -- Outreach slice 6
begin
 perform set_config('eios.tenant_id',t,true);
 v_limit:=coalesce((c->>'limit')::integer,50);
 if v_limit not between 1 and 200 then raise exception 'outreach command invalid' using errcode='22023';end if;
 if c->>'verb'='due' then
  perform runtime.nexloop_outreach_expire_approvals(t,p_world);  -- Outreach slice 6: an approval past its window ends the intent
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
   -- Outreach slice 6: an approval lets the purpose gate pass for this intent only; every other gate is checked as before.
   v_approved:=exists(select 1 from runtime.nexloop_outreach_approvals ap where ap.tenant_id=t and ap.world=p_world and ap.intent_id=i.intent_id and ap.decision='approved');
   for x in select value from jsonb_array_elements(g) loop
    if x->>'result'<>'pass' and not (x->>'gate'='purpose' and x->>'detail'='approval_required' and v_approved) then
     raise exception 'gate' using errcode='42501',hint=case x->>'gate'
      when 'contact_restricted' then 'contact_restricted' when 'control_paused' then 'control_paused'
      when 'purpose' then x->>'detail'
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

-- 6. Governed approval Action ----------------------------------------------------------------------------------------------------------
create function control.nexloop_governed_outreach_approve(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare s runtime.nexloop_outreach_sends;a runtime.nexloop_outreach_approvals;cfg control.nexloop_workbench_configurations;
 v_resource text:='eios:action:nexloop.outreach.approve:1';v_decision text;v_intent text:=body->>'intent_id';
begin
 if jsonb_typeof(body) is distinct from 'object'
  or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','intent_id','decision','reason'))
  or coalesce(body->>'intent_id','')!~'^outreach:[0-9a-f]{64}$' or body->>'decision' not in ('approve','reject') or body->>'decision' is null
  or jsonb_typeof(body->'reason') is distinct from 'string' or char_length(body->>'reason') not between 1 and 500 then
  raise exception 'outreach approval: invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into s from runtime.nexloop_outreach_sends where tenant_id=p_tenant and world=p_world and intent_id=v_intent for update;
 select * into a from runtime.nexloop_outreach_approvals where tenant_id=p_tenant and world=p_world and intent_id=v_intent for update;
 if s.intent_id is null or a.intent_id is null then raise exception 'outreach approval: not_pending' using errcode='22023';end if;
 v_decision:=case body->>'decision' when 'approve' then 'approved' else 'rejected' end;
 -- The same decision by the same approver replays; any other decision on a decided approval is refused.
 if a.decision is not null then
  if a.decision=v_decision and a.decided_by=p_principal then return jsonb_build_object('intent_id',s.intent_id,'state',s.state,'decision',a.decision,'replayed',true);end if;
  raise exception 'outreach approval: not_pending' using errcode='22023';end if;
 -- Four eyes: the requester never decides their own request.
 if p_principal=s.principal_id or p_principal=a.requested_by then raise exception 'outreach approval: self_approval' using errcode='22023';end if;
 -- The approver's authority now: an active member whose current workbench role holds the Action.
 cfg:=control.nexloop_workbench_current(p_tenant);
 if not exists(select 1 from control.nexloop_browser_memberships m where m.tenant_id=p_tenant and m.principal_id=p_principal and m.payload->>'status'='active')
  or cfg.tenant_id is null or not coalesce(cfg.roles->(cfg.members->>p_principal) ? v_resource,false) then
  raise exception 'outreach approval: approver_unauthorized' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 if a.expires_at<=clock_timestamp() then
  -- Past its window: the send ends expired (terminal) and the decision is not taken.
  perform runtime.nexloop_outreach_expire_approvals(p_tenant,p_world);
  return jsonb_build_object('intent_id',s.intent_id,'state','refused','refusal_code','approval_expired','replayed',false);
 end if;
 update runtime.nexloop_outreach_approvals set decision=v_decision,decided_by=p_principal,decided_at=clock_timestamp(),reason=body->>'reason',decision_intent=p_intent
  where tenant_id=p_tenant and world=p_world and intent_id=s.intent_id;
 if v_decision='approved' then
  -- Approved is only accepted: the executor still admits it through every dispatch gate.
  update runtime.nexloop_outreach_sends set state='accepted',updated_at=clock_timestamp() where tenant_id=p_tenant and world=p_world and intent_id=s.intent_id;
 else
  update runtime.nexloop_outreach_sends set state='refused',refusal_code='approval_rejected',finished_at=clock_timestamp(),updated_at=clock_timestamp()
   where tenant_id=p_tenant and world=p_world and intent_id=s.intent_id;
 end if;
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('outreach-approval:'||s.intent_id,'nexloop.outreach.approval.'||v_decision,p_tenant,p_principal,'outreach',p_intent,
   jsonb_build_object('intent_id',s.intent_id,'decision',v_decision),clock_timestamp()) on conflict do nothing;
 return jsonb_build_object('intent_id',s.intent_id,'state',case when v_decision='approved' then 'accepted' else 'refused' end,'decision',v_decision,'replayed',false);
end $$;
alter function control.nexloop_governed_outreach_approve(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_governed_outreach_approve(text,text,text,text,text,jsonb) from public;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('outreach.approve','decide_outreach_approval','human','control.nexloop_governed_outreach_approve(text,text,text,text,text,jsonb)','NX-022');

-- 7. The owner's pending approvals (workbench observe; every other verb unchanged) ----------------------------------------------------
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) rename to nexloop_workbench_observe_read_before_outreach_approvals;
revoke all on function authz.nexloop_workbench_observe_read_before_outreach_approvals(text,text,text,text,text) from public,nexloop_api;
create function authz.nexloop_workbench_observe_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;w jsonb;cfg control.nexloop_workbench_configurations;v_tenant text;
 v_action text:='eios:action:nexloop.workbench.read:1';
begin
 if c->>'verb' is distinct from 'outreach_approvals' then
  return authz.nexloop_workbench_observe_read_before_outreach_approvals(p_digest,p_world,p_text,p_signature,p_payload);end if;
 -- The 0133 verification, for this verb (as the earlier verbs).
 if octet_length(p_text)>1048576 or octet_length(p_payload)>4096
  or a->>'protocol' is distinct from 'nexloop-workbench-observe-v1' or a->>'operation' is distinct from 'execute'
  or a->>'action_resource' is distinct from v_action or a->>'resource_id' is distinct from v_action
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'workbench observe unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-workbench-observe-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'workbench observe unavailable' using errcode='42501';end if;
 w:=authz.nexloop_workbench_member(p_digest,p_world);v_tenant:=w->>'tenant_id';cfg:=control.nexloop_workbench_current(v_tenant);
 if a->>'tenant_id' is distinct from v_tenant or a->>'principal_id' is distinct from w->>'principal_id' or not (cfg.roles->(w->>'role') ? v_action)
  or w->>'role'<>'owner' then
  raise exception 'workbench observe forbidden' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 if (select count(*) from jsonb_object_keys(c))<>1 then raise exception 'workbench observe invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 -- Ids, purpose, times and the requester only; never the body.
 return jsonb_build_object('window_seconds',extract(epoch from runtime.nexloop_outreach_approval_window())::integer,
  'pending',coalesce((select jsonb_agg(jsonb_build_object('intent_id',s.intent_id,'purpose_id',s.purpose_id,'requested_at',ap.requested_at,'expires_at',ap.expires_at,
    'requested_by',ap.requested_by) order by ap.requested_at,s.intent_id)
   from runtime.nexloop_outreach_approvals ap join runtime.nexloop_outreach_sends s on s.tenant_id=ap.tenant_id and s.world=ap.world and s.intent_id=ap.intent_id
   where ap.tenant_id=v_tenant and ap.world=p_world and ap.decision is null and s.state='pending_approval' and ap.expires_at>clock_timestamp()),'[]'::jsonb));
end $$;
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_observe_read(text,text,text,text,text) from public;
grant execute on function authz.nexloop_workbench_observe_read(text,text,text,text,text) to nexloop_api;
