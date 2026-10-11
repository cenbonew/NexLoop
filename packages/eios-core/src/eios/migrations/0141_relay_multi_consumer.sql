-- Relay multi-consumer (temporary number 0160; design docs/implementation/relay-multi-consumer-design.md, D1–D6 and C1–C7
-- approved by the dispatcher 2026-10-11; owner 2026-10-11 "同意，按你的方案修").
-- 1. control.nexloop_relay_enrollments: which relay principal serves which consumer with which EffectControl, offering and
--    binding (plus, optionally, the consumer's Role link and plan step for NX-024 loading). Append-only; written only by the
--    signed enrollment port (nexloop.relay.enroll / nexloop.relay.withdraw), whose caller must be the control's owner
--    principal (effect control ledger); the relay itself can never enroll or withdraw.
-- 2. claim_next / profile: new verbs of the message run issuance command (0049, wrapped by 0126), renamed and wrapped again
--    (0049 and 0126 unchanged). claim_next picks among the caller's actively enrolled consumers, not erasing (NX-029), not
--    taken over (NX-028), with no message of theirs in flight (one consumer at a time, D2), the one served least recently
--    (D3), and that consumer's oldest pending message by accepted_at; it returns the consumer's current profile (revisions
--    read now, rechecked at issue).
-- 3. issue: after the existing checks, a message claimed through claim_next is re-checked against the consumer's current
--    enrollment (still active, same revision, same relay principal, same EffectControl); any message's consumer must not be
--    erasing. A refusal rolls the issuance back.
-- 4. runtime.nexloop_metrics_snapshot: the latest body (0131; 0133 did not replace it) plus a relay section (NX-030).

-- 1. Tables ------------------------------------------------------------------------------------------------------------------
create table control.nexloop_relay_enrollments (
 tenant_id text not null,world text not null check(world='real'),
 consumer_id text not null check(consumer_id~'^[a-f0-9]{64}$'),
 enrollment_revision bigint not null check(enrollment_revision>0),
 state text not null check(state in ('active','withdrawn')),
 relay_principal text not null check(length(relay_principal) between 1 and 512),
 control_id text check(control_id~'^[a-f0-9]{64}$'),offering_id text check(offering_id~'^[a-f0-9]{64}$'),binding_id text check(binding_id~'^[a-f0-9]{64}$'),
 role_id text check(role_id~'^[a-f0-9]{64}$'),link_id text check(link_id~'^[a-f0-9]{64}$'),step_id text check(step_id~'^[a-f0-9]{64}$'),
 enrolled_by text not null,intent_id text not null check(intent_id~'^[A-Za-z0-9._:-]{8,160}$'),
 recorded_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,consumer_id,enrollment_revision),unique(tenant_id,world,intent_id),
 check((state='active')=(control_id is not null and offering_id is not null and binding_id is not null)),
 check((role_id is null)=(link_id is null) and (role_id is null)=(step_id is null))
);
create table runtime.nexloop_relay_consumer_cursors (
 tenant_id text not null,world text not null,consumer_id text not null,last_served_at timestamptz not null,
 primary key(tenant_id,world,consumer_id)
);
-- A message claimed by claim_next: the enrollment it was claimed under, re-checked at issue.
create table runtime.nexloop_relay_claims (
 tenant_id text not null,world text not null,message_id text not null,consumer_id text not null,
 enrollment_revision bigint not null,relay_principal text not null,fence bigint not null,claimed_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,message_id)
);
do $$ declare t text; begin
 foreach t in array array['control.nexloop_relay_enrollments','runtime.nexloop_relay_consumer_cursors','runtime.nexloop_relay_claims'] loop
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy relay_tenant on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on %s from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator',t);
 end loop; end $$;
create trigger relay_append_only before update or delete on control.nexloop_relay_enrollments for each row execute function control.nexloop_nx022_append_only();
create index nexloop_message_outbox_pending on runtime.nexloop_message_outbox(tenant_id,world,conversation_id) where status='pending';

-- 2. Current enrollment and the servable profile ---------------------------------------------------------------------------
create function control.nexloop_relay_enrollment_current(p_tenant text,p_world text,p_consumer text) returns control.nexloop_relay_enrollments
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);e control.nexloop_relay_enrollments;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into e from control.nexloop_relay_enrollments where tenant_id=p_tenant and world=p_world and consumer_id=p_consumer order by enrollment_revision desc limit 1;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return e;
end $$;
alter function control.nexloop_relay_enrollment_current(text,text,text) owner to nexloop_owner;
revoke all on function control.nexloop_relay_enrollment_current(text,text,text) from public;

-- The consumer's profile as it can be served now: null unless the current enrollment is active (for p_relay when given) and
-- its EffectControl (object and ledger), binding and offering are current and valid. Revisions are read now.
create function runtime.nexloop_relay_profile(p_tenant text,p_world text,p_consumer text,p_relay text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);e control.nexloop_relay_enrollments;v_consumer ontology.objects;v_control ontology.objects;
 v_binding ontology.objects;v_offering ontology.objects;v_ctl control.nexloop_effect_control_ledger;v jsonb;
begin
 e:=control.nexloop_relay_enrollment_current(p_tenant,p_world,p_consumer);
 if e.consumer_id is null or e.state<>'active' or (p_relay is not null and e.relay_principal is distinct from p_relay) then return null;end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into v_consumer from ontology.objects where tenant_id=p_tenant and world=p_world and object_id=p_consumer and type_name='Consumer';
 select * into v_control from ontology.objects where tenant_id=p_tenant and world=p_world and object_id=e.control_id and type_name='EffectControl';
 select * into v_ctl from control.nexloop_effect_control_ledger where tenant_id=p_tenant and world=p_world and control_id=e.control_id;
 select * into v_binding from ontology.objects where tenant_id=p_tenant and world=p_world and object_id=e.binding_id and type_name='ConsumerServiceOffering';
 select * into v_offering from ontology.objects where tenant_id=p_tenant and world=p_world and object_id=e.offering_id and type_name='ServiceOffering';
 if v_consumer.object_id is null or v_control.object_id is null or v_ctl.control_id is null or v_binding.object_id is null or v_offering.object_id is null
  or v_control.properties->>'consumer_id' is distinct from p_consumer or v_control.properties->'allow_effect' is distinct from 'true'::jsonb
  or jsonb_typeof(v_control.properties->'valid_until') is distinct from 'string' or (v_control.properties->>'valid_until')::timestamptz<=clock_timestamp()
  or v_ctl.consumer_id is distinct from p_consumer or v_ctl.control_revision<>v_control.nexloop_revision or v_ctl.valid_until<=clock_timestamp()
  or v_binding.properties->>'consumer_id' is distinct from p_consumer or v_binding.properties->>'offering_id' is distinct from e.offering_id
  or v_binding.properties->'active' is distinct from 'true'::jsonb or v_offering.properties->'active' is distinct from 'true'::jsonb then
  v:=null;
 else
  v:=jsonb_build_object('consumer_id',p_consumer,'control_id',e.control_id,'control_revision',v_control.nexloop_revision,'consumer_revision',v_consumer.nexloop_revision,
   'offering_id',e.offering_id,'binding_id',e.binding_id,'valid_until',v_control.properties->>'valid_until','enrollment_revision',e.enrollment_revision,
   'role_id',e.role_id,'link_id',e.link_id,'step_id',e.step_id);
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
alter function runtime.nexloop_relay_profile(text,text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_relay_profile(text,text,text,text) from public;

-- 3. Enrollment port (signed service port; caller = the EffectControl's owner principal) ------------------------------------
create function authz.nexloop_relay_enrollment_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare c jsonb:=p_payload::jsonb;a jsonb:=p_text::jsonb;v_verb text:=c->>'verb';v_consumer text:=c->>'consumer_id';v_principal text:=a->>'principal_id';
 v_action text;v_tenant text;e control.nexloop_relay_enrollments;v_prior control.nexloop_relay_enrollments;v_ctl control.nexloop_effect_control_ledger;
 v_control ontology.objects;v_binding ontology.objects;v_offering ontology.objects;v_link ontology.objects;v_step ontology.objects;v_role ontology.objects;
 v_row control.nexloop_relay_enrollments;
begin
 v_action:=case v_verb when 'enroll' then 'eios:action:nexloop.relay.enroll:1' when 'read' then 'eios:action:nexloop.relay.enroll:1'
  when 'withdraw' then 'eios:action:nexloop.relay.withdraw:1' end;
 if v_action is null or p_world is distinct from 'real' or v_consumer is null or v_consumer!~'^[a-f0-9]{64}$' then
  raise exception 'relay enrollment unavailable' using errcode='42501';end if;
 v_tenant:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-relay-enrollment-v1',v_action,array['nexloop_api']);
 perform pg_advisory_xact_lock(hashtextextended(v_tenant||':'||p_world||':relay-enrollment:'||v_consumer,0));
 e:=control.nexloop_relay_enrollment_current(v_tenant,p_world,v_consumer);
 perform set_config('eios.tenant_id',v_tenant,true);
 if v_verb='read' then
  if e.consumer_id is null then return null;end if;
  -- Only the owner of the enrolled (or last enrolled) control reads the enrollment.
  select * into v_ctl from control.nexloop_effect_control_ledger where tenant_id=v_tenant and world=p_world
   and control_id=coalesce(e.control_id,(select x.control_id from control.nexloop_relay_enrollments x where x.tenant_id=v_tenant and x.world=p_world
     and x.consumer_id=v_consumer and x.control_id is not null order by x.enrollment_revision desc limit 1));
  if v_ctl.owner_principal is distinct from v_principal then raise exception 'relay enrollment forbidden' using errcode='42501';end if;
  return jsonb_build_object('consumer_id',e.consumer_id,'state',e.state,'enrollment_revision',e.enrollment_revision,'relay_principal',e.relay_principal,
   'control_id',e.control_id,'offering_id',e.offering_id,'binding_id',e.binding_id,'role_id',e.role_id,'link_id',e.link_id,'step_id',e.step_id);
 end if;
 if c->>'intent_id' is null or c->>'intent_id'!~'^[A-Za-z0-9._:-]{8,160}$' then raise exception 'relay enrollment invalid' using errcode='22023';end if;
 select * into v_prior from control.nexloop_relay_enrollments where tenant_id=v_tenant and world=p_world and intent_id=c->>'intent_id';
 if v_verb='withdraw' then
  if v_prior.intent_id is not null then
   if v_prior.consumer_id is distinct from v_consumer or v_prior.state<>'withdrawn' then raise exception 'relay enrollment intent conflict' using errcode='22023';end if;
   return jsonb_build_object('consumer_id',v_consumer,'state','withdrawn','enrollment_revision',v_prior.enrollment_revision,'replay',true);
  end if;
  if e.consumer_id is null or e.state<>'active' then raise exception 'relay enrollment not active' using errcode='22023';end if;
  select * into v_ctl from control.nexloop_effect_control_ledger where tenant_id=v_tenant and world=p_world and control_id=e.control_id;
  if v_ctl.owner_principal is distinct from v_principal then raise exception 'relay enrollment forbidden' using errcode='42501';end if;
  insert into control.nexloop_relay_enrollments(tenant_id,world,consumer_id,enrollment_revision,state,relay_principal,enrolled_by,intent_id)
   values(v_tenant,p_world,v_consumer,e.enrollment_revision+1,'withdrawn',e.relay_principal,v_principal,c->>'intent_id') returning * into v_row;
  return jsonb_build_object('consumer_id',v_consumer,'state','withdrawn','enrollment_revision',v_row.enrollment_revision,'replay',false);
 end if;
 -- enroll
 if c->>'control_id' is null or c->>'control_id'!~'^[a-f0-9]{64}$' or c->>'offering_id' is null or c->>'offering_id'!~'^[a-f0-9]{64}$'
  or c->>'binding_id' is null or c->>'binding_id'!~'^[a-f0-9]{64}$' or jsonb_typeof(c->'relay_principal') is distinct from 'string'
  or length(c->>'relay_principal') not between 1 and 512
  or not ((c->'role_id' is null and c->'link_id' is null and c->'step_id' is null) or (c->>'role_id'~'^[a-f0-9]{64}$' and c->>'link_id'~'^[a-f0-9]{64}$' and c->>'step_id'~'^[a-f0-9]{64}$')) then
  raise exception 'relay enrollment invalid' using errcode='22023';end if;
 if v_prior.intent_id is not null then
  if v_prior.consumer_id is distinct from v_consumer or v_prior.state<>'active' or v_prior.control_id is distinct from c->>'control_id' or v_prior.offering_id is distinct from c->>'offering_id'
   or v_prior.binding_id is distinct from c->>'binding_id' or v_prior.relay_principal is distinct from c->>'relay_principal' or v_prior.role_id is distinct from c->>'role_id'
   or v_prior.link_id is distinct from c->>'link_id' or v_prior.step_id is distinct from c->>'step_id' then raise exception 'relay enrollment intent conflict' using errcode='22023';end if;
  return jsonb_build_object('consumer_id',v_consumer,'state','active','enrollment_revision',v_prior.enrollment_revision,'replay',true);
 end if;
 -- The caller owns this consumer's EffectControl (ledger owner) and is not the relay it authorizes.
 select * into v_ctl from control.nexloop_effect_control_ledger where tenant_id=v_tenant and world=p_world and control_id=c->>'control_id';
 if v_ctl.control_id is null or v_ctl.owner_principal is distinct from v_principal or v_ctl.consumer_id is distinct from v_consumer
  or c->>'relay_principal'=v_principal then raise exception 'relay enrollment forbidden' using errcode='42501';end if;
 if not exists(select 1 from authz.nexloop_service_credentials s where s.tenant_id=v_tenant and s.status='active' and s.binding->>'subject_kind'='service'
   and s.binding->>'subject_principal_id'=c->>'relay_principal') then raise exception 'relay enrollment invalid' using errcode='22023';end if;
 select * into v_control from ontology.objects where tenant_id=v_tenant and world=p_world and object_id=c->>'control_id' and type_name='EffectControl';
 select * into v_binding from ontology.objects where tenant_id=v_tenant and world=p_world and object_id=c->>'binding_id' and type_name='ConsumerServiceOffering';
 select * into v_offering from ontology.objects where tenant_id=v_tenant and world=p_world and object_id=c->>'offering_id' and type_name='ServiceOffering';
 if not exists(select 1 from ontology.objects where tenant_id=v_tenant and world=p_world and object_id=v_consumer and type_name='Consumer')
  or v_control.object_id is null or v_control.properties->>'consumer_id' is distinct from v_consumer or v_control.properties->'allow_effect' is distinct from 'true'::jsonb
  or v_binding.object_id is null or v_binding.properties->>'consumer_id' is distinct from v_consumer or v_binding.properties->>'offering_id' is distinct from c->>'offering_id'
  or v_binding.properties->'active' is distinct from 'true'::jsonb or v_offering.object_id is null or v_offering.properties->'active' is distinct from 'true'::jsonb then
  raise exception 'relay enrollment invalid' using errcode='22023';end if;
 if c->>'role_id' is not null then
  select * into v_role from ontology.objects where tenant_id=v_tenant and world=p_world and object_id=c->>'role_id' and type_name='RoleDefinition';
  select * into v_link from ontology.objects where tenant_id=v_tenant and world=p_world and object_id=c->>'link_id' and type_name='ConsumerRoleLink';
  select * into v_step from ontology.objects where tenant_id=v_tenant and world=p_world and object_id=c->>'step_id' and type_name='PlanStep';
  if v_role.object_id is null or v_link.properties->>'consumer_id' is distinct from v_consumer or v_link.properties->>'role_id' is distinct from c->>'role_id'
   or v_step.properties->>'consumer_id' is distinct from v_consumer or v_step.properties->>'control_id' is distinct from c->>'control_id' then
   raise exception 'relay enrollment invalid' using errcode='22023';end if;
 end if;
 -- The same active enrollment again is a replay, not a new revision.
 if e.state='active' and e.control_id=c->>'control_id' and e.offering_id=c->>'offering_id' and e.binding_id=c->>'binding_id' and e.relay_principal=c->>'relay_principal'
  and e.role_id is not distinct from c->>'role_id' and e.link_id is not distinct from c->>'link_id' and e.step_id is not distinct from c->>'step_id' then
  return jsonb_build_object('consumer_id',v_consumer,'state','active','enrollment_revision',e.enrollment_revision,'replay',true);
 end if;
 insert into control.nexloop_relay_enrollments(tenant_id,world,consumer_id,enrollment_revision,state,relay_principal,control_id,offering_id,binding_id,role_id,link_id,step_id,enrolled_by,intent_id)
  values(v_tenant,p_world,v_consumer,coalesce(e.enrollment_revision,0)+1,'active',c->>'relay_principal',c->>'control_id',c->>'offering_id',c->>'binding_id',
   c->>'role_id',c->>'link_id',c->>'step_id',v_principal,c->>'intent_id') returning * into v_row;
 return jsonb_build_object('consumer_id',v_consumer,'state','active','enrollment_revision',v_row.enrollment_revision,'replay',false);
end $$;
alter function authz.nexloop_relay_enrollment_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_relay_enrollment_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_relay_enrollment_command(text,text,text,text,text) to nexloop_api;

-- 4. claim_next / profile ---------------------------------------------------------------------------------------------------
create function authz.nexloop_relay_multi_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;v_verb text:=c->>'verb';v_tenant text:=a->>'tenant_id';v_principal text:=a->>'principal_id';
 v_key bytea;v_def control.nexloop_action_definitions;v_seconds int;v_cand record;v_profile jsonb;v_out runtime.nexloop_message_outbox;v_conv runtime.nexloop_conversations;
 v_route runtime.nexloop_message_routes;v_lease runtime.nexloop_message_relay_leases;
begin
 -- Same authentication as 0049: nexloop_api, signed route protocol, published low-risk route Action, active service credential.
 if v_tenant is null or v_principal is null or session_user<>'nexloop_api' or p_world is distinct from 'real' or octet_length(p_text)>1048576 or octet_length(p_payload)>262144
  or a->>'protocol' is distinct from 'nexloop-message-run-issuance-v1' or a->>'action_resource' is distinct from 'eios:action:nexloop.conversation.route:1'
  or a->>'operation' is distinct from 'execute' or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or v_verb is null or v_verb not in ('claim_next','profile') then raise exception 'message relay unavailable' using errcode='42501';end if;
 select key_material into v_key from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-message-run-issuance-v1:'||p_text,'UTF8'),v_key,'sha256'),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then raise exception 'message relay unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 perform set_config('eios.tenant_id',v_tenant,true);
 if not exists(select 1 from authz.nexloop_service_credentials s where s.token_digest=p_digest and s.tenant_id=v_tenant and s.status='active' and s.binding->>'subject_kind'='service') then raise exception 'message relay unavailable' using errcode='42501';end if;
 select * into v_def from control.nexloop_action_definitions where tenant_id=v_tenant and world=p_world and resource_id=a->>'action_resource' and active for share;
 if not found or v_def.definition is distinct from a->'definition' or v_def.capability is distinct from a->'capability'
  or v_def.definition->'governance'->>'approval_mode' is distinct from 'none' or v_def.definition->'governance'->>'risk_level' is distinct from 'low'
  or jsonb_array_length(v_def.definition->'governance'->'policy_refs')<>0 then raise exception 'message relay unavailable' using errcode='42501';end if;
 if v_verb='profile' then
  if c->>'consumer_id' is null or c->>'consumer_id'!~'^[a-f0-9]{64}$' then raise exception 'message relay unavailable' using errcode='42501';end if;
  if runtime.nexloop_consumer_erasing(v_tenant,p_world,c->>'consumer_id') then return null;end if;
  return runtime.nexloop_relay_profile(v_tenant,p_world,c->>'consumer_id',v_principal);
 end if;
 if jsonb_typeof(c->'lease_seconds') is distinct from 'number' or c->>'lease_seconds'!~'^[0-9]{1,3}$' then raise exception 'message relay unavailable' using errcode='42501';end if;
 v_seconds=(c->>'lease_seconds')::int;
 if v_seconds not between 3 and 300 then raise exception 'message relay unavailable' using errcode='42501';end if;
 for v_cand in
  select e.consumer_id,h.message_id,h.conversation_id,coalesce(cur.last_served_at,'-infinity'::timestamptz) served,h.accepted
  from (select distinct on (x.consumer_id) x.* from control.nexloop_relay_enrollments x where x.tenant_id=v_tenant and x.world=p_world
        order by x.consumer_id,x.enrollment_revision desc) e
  left join runtime.nexloop_relay_consumer_cursors cur on cur.tenant_id=v_tenant and cur.world=p_world and cur.consumer_id=e.consumer_id
  cross join lateral (select b.message_id,b.conversation_id,(b.record->>'accepted_at')::timestamptz accepted from runtime.nexloop_message_outbox b
     join runtime.nexloop_conversations cv on cv.tenant_id=b.tenant_id and cv.world=b.world and cv.conversation_id=b.conversation_id
     where b.tenant_id=v_tenant and b.world=p_world and b.status='pending' and cv.consumer_id=e.consumer_id
     order by (b.record->>'accepted_at')::timestamptz,b.conversation_id,b.sequence limit 1) h
  where e.state='active' and e.relay_principal=v_principal
  order by 4,5 limit 32
 loop
  -- One consumer at a time (D2): skip a consumer another relay is choosing now or that has a message in flight.
  if not pg_try_advisory_xact_lock(hashtextextended(v_tenant||':'||p_world||':relay-consumer:'||v_cand.consumer_id,0)) then continue;end if;
  if exists(select 1 from runtime.nexloop_message_relay_leases l join runtime.nexloop_message_outbox b on b.tenant_id=l.tenant_id and b.world=l.world and b.message_id=l.message_id
     where l.tenant_id=v_tenant and l.world=p_world and l.consumer_id=v_cand.consumer_id and l.lease_until>clock_timestamp() and b.status='pending') then continue;end if;
  if runtime.nexloop_consumer_erasing(v_tenant,p_world,v_cand.consumer_id)
   or control.nexloop_takeover_active(v_tenant,p_world,v_cand.consumer_id,v_cand.conversation_id) is not null then continue;end if;
  v_profile:=runtime.nexloop_relay_profile(v_tenant,p_world,v_cand.consumer_id,v_principal);
  if v_profile is null then continue;end if;
  -- Registered owner, canonical ConsumerOwnership, formal Consumer and Conversation (as 0049 claim).
  if not exists(select 1 from runtime.nexloop_conversations cv
    join control.nexloop_consumer_owners ow on ow.tenant_id=cv.tenant_id and ow.world=cv.world and ow.principal_id=cv.principal_id and ow.consumer_id=cv.consumer_id
    join ontology.objects own on own.tenant_id=ow.tenant_id and own.world=ow.world and own.object_id=ow.ownership_id and own.type_name='ConsumerOwnership'
     and own.properties=jsonb_build_object('consumer_id',ow.consumer_id,'principal_id',ow.principal_id)
    join ontology.objects co on co.tenant_id=cv.tenant_id and co.world=cv.world and co.object_id=cv.consumer_id and co.type_name='Consumer'
    join ontology.objects formal on formal.tenant_id=cv.tenant_id and formal.world=cv.world and formal.object_id=cv.conversation_id and formal.type_name='Conversation'
     and formal.properties=jsonb_build_object('consumer_id',cv.consumer_id,'owner_principal',cv.principal_id)
    where cv.tenant_id=v_tenant and cv.world=p_world and cv.conversation_id=v_cand.conversation_id and cv.consumer_id=v_cand.consumer_id) then continue;end if;
  if not pg_try_advisory_xact_lock(hashtextextended(v_tenant||':'||p_world||':message-route:'||v_cand.message_id,0)) then continue;end if;
  select * into v_route from runtime.nexloop_message_routes where tenant_id=v_tenant and world=p_world and message_id=v_cand.message_id for update;
  select * into v_out from runtime.nexloop_message_outbox where tenant_id=v_tenant and world=p_world and message_id=v_cand.message_id for update;
  if not found or v_out.status is distinct from 'pending' then continue;end if;
  if exists(select 1 from runtime.nexloop_message_relay_leases l where l.tenant_id=v_tenant and l.world=p_world and l.message_id=v_out.message_id and l.lease_until>clock_timestamp()) then continue;end if;
  select * into v_conv from runtime.nexloop_conversations where tenant_id=v_tenant and world=p_world and conversation_id=v_out.conversation_id for share;
  -- Canonical formal Message must correspond exactly to committed outbox data (as 0049).
  if not exists(select 1 from ontology.objects mo where mo.tenant_id=v_tenant and mo.world=p_world and mo.object_id=v_out.message_id and mo.type_name='Message'
   and mo.properties=jsonb_build_object('conversation_id',v_out.conversation_id,'sequence',v_out.sequence,'actor',v_out.record->>'actor','body',v_out.record->>'body','accepted_at',v_out.record->>'accepted_at')) then continue;end if;
  if v_route.message_id is not null and (v_route.bound_by is distinct from v_principal or v_route.lease_until>clock_timestamp()) then continue;end if;
  insert into runtime.nexloop_message_relay_leases values(v_tenant,p_world,v_out.message_id,v_cand.consumer_id,v_principal,p_digest,1,clock_timestamp()+make_interval(secs=>v_seconds))
   on conflict(tenant_id,world,message_id) do update set consumer_id=excluded.consumer_id,principal_id=excluded.principal_id,credential_digest=excluded.credential_digest,
    fence=runtime.nexloop_message_relay_leases.fence+1,lease_until=excluded.lease_until returning * into v_lease;
  if v_route.message_id is not null then
   update runtime.nexloop_message_routes set fence=fence+1,lease_credential=p_digest,lease_until=v_lease.lease_until
    where tenant_id=v_tenant and world=p_world and message_id=v_out.message_id returning * into v_route;
  end if;
  insert into runtime.nexloop_relay_consumer_cursors values(v_tenant,p_world,v_cand.consumer_id,clock_timestamp())
   on conflict(tenant_id,world,consumer_id) do update set last_served_at=excluded.last_served_at;
  insert into runtime.nexloop_relay_claims(tenant_id,world,message_id,consumer_id,enrollment_revision,relay_principal,fence)
   values(v_tenant,p_world,v_out.message_id,v_cand.consumer_id,(v_profile->>'enrollment_revision')::bigint,v_principal,v_lease.fence)
   on conflict(tenant_id,world,message_id) do update set consumer_id=excluded.consumer_id,enrollment_revision=excluded.enrollment_revision,
    relay_principal=excluded.relay_principal,fence=excluded.fence,claimed_at=clock_timestamp();
  perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
  return jsonb_build_object('message_id',v_out.message_id,'tenant_id',v_tenant,'consumer_id',v_conv.consumer_id,'fence',v_lease.fence,
   'route_fence',v_route.fence,'source_event_id',v_out.event_id,'body',v_out.record->>'body','profile',v_profile);
 end loop;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 return null;
end $$;
alter function authz.nexloop_relay_multi_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_relay_multi_command(text,text,text,text,text) from public;

-- 5. The issuance command, renamed and wrapped (0049 and 0126 unchanged) ----------------------------------------------------
alter function authz.nexloop_message_run_issuance_command(text,text,text,text,text) rename to nexloop_message_run_issuance_command_before_relay_multi_v0160;
revoke all on function authz.nexloop_message_run_issuance_command_before_relay_multi_v0160(text,text,text,text,text) from public,nexloop_api;
create function authz.nexloop_message_run_issuance_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare c jsonb:=p_payload::jsonb;v_verb text:=c->>'verb';v_tenant text:=p_text::jsonb->>'tenant_id';v_principal text:=p_text::jsonb->>'principal_id';
 v_result jsonb;v_consumer text;v_claim runtime.nexloop_relay_claims;e control.nexloop_relay_enrollments;
begin
 if v_verb in ('claim_next','profile') then return authz.nexloop_relay_multi_command(p_digest,p_world,p_text,p_signature,p_payload);end if;
 v_result:=authz.nexloop_message_run_issuance_command_before_relay_multi_v0160(p_digest,p_world,p_text,p_signature,p_payload);
 if v_verb='issue' then
  -- After the existing checks (authenticated by now); a refusal rolls the whole issuance back.
  perform set_config('eios.tenant_id',v_tenant,true);
  select cv.consumer_id into v_consumer from runtime.nexloop_message_outbox b join runtime.nexloop_conversations cv
   on cv.tenant_id=b.tenant_id and cv.world=b.world and cv.conversation_id=b.conversation_id where b.tenant_id=v_tenant and b.world=p_world and b.message_id=c->>'message_id';
  if runtime.nexloop_consumer_erasing(v_tenant,p_world,v_consumer) then raise exception 'consumer erasing: no message Run' using errcode='42501';end if;
  select * into v_claim from runtime.nexloop_relay_claims where tenant_id=v_tenant and world=p_world and message_id=c->>'message_id';
  if found then
   e:=control.nexloop_relay_enrollment_current(v_tenant,p_world,v_claim.consumer_id);
   perform set_config('eios.tenant_id',v_tenant,true);
   if e.state is distinct from 'active' or e.enrollment_revision is distinct from v_claim.enrollment_revision or e.relay_principal is distinct from v_principal
    or v_claim.consumer_id is distinct from v_consumer or (c->>'assignment_text')::jsonb->>'control_id' is distinct from e.control_id then
    raise exception 'relay enrollment changed: no message Run' using errcode='42501';end if;
  end if;
 end if;
 return v_result;
end $$;
alter function authz.nexloop_message_run_issuance_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_message_run_issuance_command(text,text,text,text,text) from public,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;
grant execute on function authz.nexloop_message_run_issuance_command(text,text,text,text,text) to nexloop_api;

-- 6. NX-030 metrics: relay section ------------------------------------------------------------------------------------------
create function runtime.nexloop_relay_metrics(p_tenant text,p_world text,p_now timestamptz) returns jsonb
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select jsonb_build_object('pending_messages',count(*),'consumers_pending',count(distinct cv.consumer_id),
   'oldest_pending_seconds',floor(extract(epoch from p_now-min((b.record->>'accepted_at')::timestamptz)))::bigint,
   'unenrolled_consumers_pending',count(distinct cv.consumer_id) filter (where e.state is distinct from 'active'))
 from runtime.nexloop_message_outbox b
 join runtime.nexloop_conversations cv on cv.tenant_id=b.tenant_id and cv.world=b.world and cv.conversation_id=b.conversation_id
 left join lateral (select x.state from control.nexloop_relay_enrollments x where x.tenant_id=b.tenant_id and x.world=b.world and x.consumer_id=cv.consumer_id
   order by x.enrollment_revision desc limit 1) e on true
 where b.tenant_id=p_tenant and b.world=p_world and b.status='pending' $$;
alter function runtime.nexloop_relay_metrics(text,text,timestamptz) owner to nexloop_owner;
revoke all on function runtime.nexloop_relay_metrics(text,text,timestamptz) from public;

-- Source of the body below: 0131_nx030_observability.sql (the only definition so far; 0133 did not replace it), copied verbatim.
create or replace function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v jsonb;now_at timestamptz:=clock_timestamp();guard jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 guard:=runtime.nexloop_process_section('guard');
 if guard->>'status'='ok' then
  guard:=guard||jsonb_build_object('timeout_rate',(select case when coalesce(sum((s.value->'sum'->>'calls')::numeric),0)=0 then 0
     else round(sum((s.value->'sum'->>'timeouts')::numeric)/sum((s.value->'sum'->>'calls')::numeric),4) end from jsonb_each(guard->'by_service') s),
   'p95_ms_max',(select max((s.value->'max'->>'p95_ms')::numeric) from jsonb_each(guard->'by_service') s));
 end if;
 v:=jsonb_build_object('tenant_id',p_tenant,'world',p_world,'computed_at',now_at,
  -- M01 dispatch refusals by code (D8 table).
  'refusals',jsonb_build_object(
   'last_5m',coalesce((select jsonb_object_agg(code,n) from (select code,count(*) n from runtime.nexloop_dispatch_refusals where tenant_id=p_tenant and world=p_world and recorded_at>now_at-interval '5 minutes' group by code) x),'{}'::jsonb),
   'last_hour',coalesce((select jsonb_object_agg(code,n) from (select code,count(*) n from runtime.nexloop_dispatch_refusals where tenant_id=p_tenant and world=p_world and recorded_at>now_at-interval '1 hour' group by code) x),'{}'::jsonb)),
  -- M02/M03 effect intents.
  'effects',jsonb_build_object(
   'by_state',coalesce((select jsonb_object_agg(state,n) from (select state,count(*) n from runtime.nexloop_effect_intents where tenant_id=p_tenant and world=p_world group by state) x),'{}'::jsonb),
   'unknown',(select jsonb_build_object('count',count(*),'oldest_age_seconds',floor(extract(epoch from now_at-min(created_at)))::bigint,
      'over_24h',count(*) filter (where created_at<now_at-interval '24 hours')) from runtime.nexloop_effect_intents where tenant_id=p_tenant and world=p_world and state='unknown')),
  -- M04 replies.
  'replies',jsonb_build_object(
   'pending',(select jsonb_build_object('count',count(*),'oldest_age_seconds',floor(extract(epoch from now_at-min(created_at)))::bigint)
     from runtime.nexloop_work_feed where tenant_id=p_tenant and world=p_world and feed='reply-due' and status='pending'),
   'escalations_last_hour',coalesce((select jsonb_object_agg(reason,n) from (select reason,count(*) n from control.nexloop_reply_escalations where tenant_id=p_tenant and world=p_world and escalated_at>now_at-interval '1 hour' group by reason) x),'{}'::jsonb),
   'escalations_total',(select count(*) from control.nexloop_reply_escalations where tenant_id=p_tenant and world=p_world)),
  -- M05 commitments.
  'commitments',jsonb_build_object(
   'exceptions',coalesce((select jsonb_object_agg(reason,n) from (select reason,count(*) n from runtime.nexloop_commitment_exceptions where tenant_id=p_tenant and world=p_world group by reason) x),'{}'::jsonb),
   'exceptions_last_hour',coalesce((select jsonb_object_agg(reason,n) from (select reason,count(*) n from runtime.nexloop_commitment_exceptions where tenant_id=p_tenant and world=p_world and raised_at>now_at-interval '1 hour' group by reason) x),'{}'::jsonb)),
  -- M06/M07 queues, work feeds and the extraction feed.
  'queues',coalesce((select jsonb_object_agg(queue,q) from (select queue,jsonb_build_object(
     'pending',count(*) filter (where status='pending'),'retry_wait',count(*) filter (where status='retry_wait'),'running',count(*) filter (where status='running'),
     'dead_lettered',count(*) filter (where status='dead_lettered'),'dead_lettered_last_hour',count(*) filter (where status='dead_lettered' and updated_at>now_at-interval '1 hour'),
     'oldest_pending_seconds',floor(extract(epoch from now_at-min(available_at) filter (where status in ('pending','retry_wait') and available_at<=now_at)))::bigint) q
    from runtime.jobs where tenant_id=p_tenant and world=p_world and queue<>'' group by queue) x),'{}'::jsonb),
  'feeds',coalesce((select jsonb_object_agg(feed,q) from (select feed,jsonb_build_object('pending',count(*) filter (where status='pending'),
     'dead_lettered',count(*) filter (where status='dead_lettered'),'dead_lettered_last_hour',count(*) filter (where status='dead_lettered' and changed_at>now_at-interval '1 hour'),
     'oldest_pending_seconds',floor(extract(epoch from now_at-min(changed_at) filter (where status='pending')))::bigint) q
    from runtime.nexloop_work_feed where tenant_id=p_tenant and world=p_world group by feed) x),'{}'::jsonb),
  'extraction',(select jsonb_build_object('pending_messages',count(*),'oldest_pending_seconds',floor(extract(epoch from now_at-min(created_at)))::bigint)
    from runtime.nexloop_claim_extraction_feed where tenant_id=p_tenant and world=p_world and task_id is null),
  -- M08–M10 process samples (deployment level, the last five minutes).
  'guard',guard,'host',runtime.nexloop_process_section('host'),'pool',runtime.nexloop_process_section('pool'),
  -- Database-wide backends (pg_stat_database is readable without extra privileges; per-role detail needs pg_read_all_stats).
  'connections',(select jsonb_build_object('total',coalesce(numbackends,0)) from pg_catalog.pg_stat_database where datname=current_database()),
  -- M11 cost (D6: data_mode separated; amounts as text).
  'cost',jsonb_build_object(
   'budgets',coalesce((select jsonb_object_agg(b.budget_kind,jsonb_build_object('limit',b.limit_amount::text,'unit',b.unit,
      'consumed',(select coalesce(sum(u.amount),0)::text from control.nexloop_budget_consumption u where u.tenant_id=b.tenant_id and u.world=b.world and u.budget_kind=b.budget_kind
        and u.recorded_at>=b.period_start and u.recorded_at<b.period_end),
      'ratio',case when b.limit_amount=0 then null else round((select coalesce(sum(u.amount),0) from control.nexloop_budget_consumption u where u.tenant_id=b.tenant_id and u.world=b.world
        and u.budget_kind=b.budget_kind and u.recorded_at>=b.period_start and u.recorded_at<b.period_end)/b.limit_amount,4) end))
     from control.nexloop_budget_limits b where b.tenant_id=p_tenant and b.world=p_world),'{}'::jsonb),
   'last_24h',coalesce((select jsonb_agg(c.v order by c.cost_kind,c.data_mode,c.currency) from (select cost_kind,data_mode,currency,
      jsonb_build_object('cost_kind',cost_kind,'data_mode',data_mode,'currency',currency,'amount',coalesce(sum(amount),0)::text,
       'entries',count(*),'unpriced',count(*) filter (where amount is null)) v
     from runtime.nexloop_cost_entries where tenant_id=p_tenant and world=p_world and occurred_at>now_at-interval '24 hours' group by cost_kind,data_mode,currency) c),'[]'::jsonb)),
  -- M12 backups belong to NX-035: unavailable until its manifest table exists, never zero.
  'backup',case when to_regclass('control.nexloop_backup_runs') is null then jsonb_build_object('status','unavailable') else jsonb_build_object('status','unavailable','reason','not_wired') end,
  -- M13 the evaluator itself.
  'evaluator',(select jsonb_build_object('last_evaluated_at',max(last_evaluated_at),'rules_version',max(rules_version),
     'stale',coalesce(max(last_evaluated_at)<now_at-interval '5 minutes',true)) from control.nexloop_alert_state where tenant_id=p_tenant and world=p_world));
 -- Relay multi-consumer (0160): the relay section, appended to the 0131 body unchanged above.
 v:=v||jsonb_build_object('relay',runtime.nexloop_relay_metrics(p_tenant,p_world,now_at));
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
