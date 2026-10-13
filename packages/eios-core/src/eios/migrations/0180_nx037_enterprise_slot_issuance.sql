-- ADR-030 (NX-037 gap B slice 2): the enterprise slot of a Message Run. The owner enrolls it explicitly with the relay
-- enrollment (three nullable columns, all or none): the consumer's enterprise EffectControl (its own budget), the
-- enterprise offering and the consumer's binding to it. Only the governed enrollment command writes them (owner of both
-- controls, intent id, append-only enrollment rows as the audit); no runtime role can write the table.
-- runtime.nexloop_relay_profile adds an 'enterprise' block only when the columns are set and currently valid, or
-- 'enterprise_skipped' with a reason when set but no longer valid (the relay then issues the reply slot only and logs it);
-- with the columns empty the profile is byte-identical to before. Message Run issuance accepts allowed_actions of exactly
-- the reply Action, or the reply and the enterprise Action; the latter needs a second Source proof, the enrolled and
-- currently valid enterprise slot, and issues the Run credential for both. The reply-only issuance is unchanged.
-- Bodies are copied from their current definitions except the marked substitutions; published migrations unchanged.

alter table control.nexloop_relay_enrollments add column enterprise_control_id text check(enterprise_control_id~'^[a-f0-9]{64}$'),
 add column enterprise_offering_id text check(enterprise_offering_id~'^[a-f0-9]{64}$'),
 add column enterprise_binding_id text check(enterprise_binding_id~'^[a-f0-9]{64}$'),
 add constraint relay_enrollment_enterprise_all_or_none check((enterprise_control_id is null)=(enterprise_offering_id is null)
  and (enterprise_offering_id is null)=(enterprise_binding_id is null));

-- The enterprise slot's current validity (shared by the profile and the issuance check): null when valid, else a reason.
create function control.nexloop_relay_enterprise_problem(p_tenant text,p_world text,p_consumer text,e control.nexloop_relay_enrollments)
returns text language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v_control ontology.objects;v_ctl control.nexloop_effect_control_ledger;
 v_binding ontology.objects;v_offering ontology.objects;v text;
begin
 if e.enterprise_control_id is null then return 'not_enrolled';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into v_control from ontology.objects where tenant_id=p_tenant and world=p_world and object_id=e.enterprise_control_id and type_name='EffectControl';
 select * into v_ctl from control.nexloop_effect_control_ledger where tenant_id=p_tenant and world=p_world and control_id=e.enterprise_control_id;
 select * into v_binding from ontology.objects where tenant_id=p_tenant and world=p_world and object_id=e.enterprise_binding_id and type_name='ConsumerServiceOffering';
 select * into v_offering from ontology.objects where tenant_id=p_tenant and world=p_world and object_id=e.enterprise_offering_id and type_name='ServiceOffering';
 v:=case
  when v_control.object_id is null or v_ctl.control_id is null or v_control.properties->>'consumer_id' is distinct from p_consumer
   or v_ctl.consumer_id is distinct from p_consumer then 'control_mismatch'
  when v_control.properties->'allow_effect' is distinct from 'true'::jsonb or jsonb_typeof(v_control.properties->'valid_until') is distinct from 'string'
   or (v_control.properties->>'valid_until')::timestamptz<=clock_timestamp() or v_ctl.control_revision<>v_control.nexloop_revision
   or v_ctl.valid_until<=clock_timestamp() then 'control_invalid'
  when v_offering.object_id is null or v_offering.properties->>'delivery_action' is distinct from 'nexloop.enterprise.deliver:1'
   or v_offering.properties->'active' is distinct from 'true'::jsonb or jsonb_typeof(v_offering.properties->'valid_until') is distinct from 'string'
   or (v_offering.properties->>'valid_until')::timestamptz<=clock_timestamp() then 'offering_invalid'
  when v_binding.object_id is null or v_binding.properties->>'consumer_id' is distinct from p_consumer
   or v_binding.properties->>'offering_id' is distinct from e.enterprise_offering_id or v_binding.properties->'active' is distinct from 'true'::jsonb then 'binding_invalid'
  else null end;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
alter function control.nexloop_relay_enterprise_problem(text,text,text,control.nexloop_relay_enrollments) owner to nexloop_owner;
revoke all on function control.nexloop_relay_enterprise_problem(text,text,text,control.nexloop_relay_enrollments) from public;


CREATE OR REPLACE FUNCTION authz.nexloop_relay_enrollment_command(p_digest text, p_world text, p_text text, p_signature text, p_payload text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare c jsonb:=p_payload::jsonb;a jsonb:=p_text::jsonb;v_verb text:=c->>'verb';v_consumer text:=c->>'consumer_id';v_principal text:=a->>'principal_id';
 v_action text;v_tenant text;e control.nexloop_relay_enrollments;v_prior control.nexloop_relay_enrollments;v_ctl control.nexloop_effect_control_ledger;
 v_control ontology.objects;v_binding ontology.objects;v_offering ontology.objects;v_link ontology.objects;v_step ontology.objects;v_role ontology.objects;
 v_row control.nexloop_relay_enrollments;v_ent jsonb:=c->'enterprise';v_ectl control.nexloop_effect_control_ledger;
 v_econtrol ontology.objects;v_ebinding ontology.objects;v_eoffering ontology.objects;
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
   'control_id',e.control_id,'offering_id',e.offering_id,'binding_id',e.binding_id,'role_id',e.role_id,'link_id',e.link_id,'step_id',e.step_id)
   ||case when e.enterprise_control_id is null then '{}'::jsonb else jsonb_build_object('enterprise',jsonb_build_object('control_id',e.enterprise_control_id,
    'offering_id',e.enterprise_offering_id,'binding_id',e.enterprise_binding_id)) end;
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
 -- ADR-030: the optional enterprise slot, all three ids or none.
 if v_ent is not null and (jsonb_typeof(v_ent) is distinct from 'object' or (select count(*) from jsonb_object_keys(v_ent))<>3
  or v_ent->>'control_id' is null or v_ent->>'control_id'!~'^[a-f0-9]{64}$' or v_ent->>'offering_id' is null or v_ent->>'offering_id'!~'^[a-f0-9]{64}$'
  or v_ent->>'binding_id' is null or v_ent->>'binding_id'!~'^[a-f0-9]{64}$' or v_ent->>'control_id'=c->>'control_id') then
  raise exception 'relay enrollment invalid' using errcode='22023';end if;
 if v_prior.intent_id is not null then
  if v_prior.consumer_id is distinct from v_consumer or v_prior.state<>'active' or v_prior.control_id is distinct from c->>'control_id' or v_prior.offering_id is distinct from c->>'offering_id'
   or v_prior.binding_id is distinct from c->>'binding_id' or v_prior.relay_principal is distinct from c->>'relay_principal' or v_prior.role_id is distinct from c->>'role_id'
   or v_prior.link_id is distinct from c->>'link_id' or v_prior.step_id is distinct from c->>'step_id'
   or v_prior.enterprise_control_id is distinct from v_ent->>'control_id' or v_prior.enterprise_offering_id is distinct from v_ent->>'offering_id'
   or v_prior.enterprise_binding_id is distinct from v_ent->>'binding_id' then raise exception 'relay enrollment intent conflict' using errcode='22023';end if;
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
 if v_ent is not null then
  -- The enterprise slot: same tenant/world, the caller owns its control, the control and binding are this consumer's,
  -- the offering delivers through the enterprise Action, and all are currently valid.
  select * into v_ectl from control.nexloop_effect_control_ledger where tenant_id=v_tenant and world=p_world and control_id=v_ent->>'control_id';
  select * into v_econtrol from ontology.objects where tenant_id=v_tenant and world=p_world and object_id=v_ent->>'control_id' and type_name='EffectControl';
  select * into v_ebinding from ontology.objects where tenant_id=v_tenant and world=p_world and object_id=v_ent->>'binding_id' and type_name='ConsumerServiceOffering';
  select * into v_eoffering from ontology.objects where tenant_id=v_tenant and world=p_world and object_id=v_ent->>'offering_id' and type_name='ServiceOffering';
  if v_ectl.control_id is null or v_ectl.owner_principal is distinct from v_principal or v_ectl.consumer_id is distinct from v_consumer then
   raise exception 'relay enrollment forbidden' using errcode='42501';end if;
  if v_econtrol.object_id is null or v_econtrol.properties->>'consumer_id' is distinct from v_consumer or v_econtrol.properties->'allow_effect' is distinct from 'true'::jsonb
   or v_ebinding.object_id is null or v_ebinding.properties->>'consumer_id' is distinct from v_consumer or v_ebinding.properties->>'offering_id' is distinct from v_ent->>'offering_id'
   or v_ebinding.properties->'active' is distinct from 'true'::jsonb or v_eoffering.object_id is null or v_eoffering.properties->'active' is distinct from 'true'::jsonb
   or v_eoffering.properties->>'delivery_action' is distinct from 'nexloop.enterprise.deliver:1' then
   raise exception 'relay enrollment invalid' using errcode='22023';end if;
 end if;
 -- The same active enrollment again is a replay, not a new revision.
 if e.state='active' and e.control_id=c->>'control_id' and e.offering_id=c->>'offering_id' and e.binding_id=c->>'binding_id' and e.relay_principal=c->>'relay_principal'
  and e.role_id is not distinct from c->>'role_id' and e.link_id is not distinct from c->>'link_id' and e.step_id is not distinct from c->>'step_id'
  and e.enterprise_control_id is not distinct from v_ent->>'control_id' and e.enterprise_offering_id is not distinct from v_ent->>'offering_id'
  and e.enterprise_binding_id is not distinct from v_ent->>'binding_id' then
  return jsonb_build_object('consumer_id',v_consumer,'state','active','enrollment_revision',e.enrollment_revision,'replay',true);
 end if;
 insert into control.nexloop_relay_enrollments(tenant_id,world,consumer_id,enrollment_revision,state,relay_principal,control_id,offering_id,binding_id,role_id,link_id,step_id,enrolled_by,intent_id,
   enterprise_control_id,enterprise_offering_id,enterprise_binding_id)
  values(v_tenant,p_world,v_consumer,coalesce(e.enrollment_revision,0)+1,'active',c->>'relay_principal',c->>'control_id',c->>'offering_id',c->>'binding_id',
   c->>'role_id',c->>'link_id',c->>'step_id',v_principal,c->>'intent_id',v_ent->>'control_id',v_ent->>'offering_id',v_ent->>'binding_id') returning * into v_row;
 return jsonb_build_object('consumer_id',v_consumer,'state','active','enrollment_revision',v_row.enrollment_revision,'replay',false);
end $function$
;

CREATE OR REPLACE FUNCTION runtime.nexloop_relay_profile(p_tenant text, p_world text, p_consumer text, p_relay text)
 RETURNS jsonb
 LANGUAGE plpgsql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
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
  if e.enterprise_control_id is not null then
   if control.nexloop_relay_enterprise_problem(p_tenant,p_world,p_consumer,e) is null then
    perform set_config('eios.tenant_id',p_tenant,true);
    v:=v||jsonb_build_object('enterprise',jsonb_build_object('control_id',e.enterprise_control_id,
     'control_revision',(select o.nexloop_revision from ontology.objects o where o.tenant_id=p_tenant and o.world=p_world and o.object_id=e.enterprise_control_id),
     'offering_id',e.enterprise_offering_id,'binding_id',e.enterprise_binding_id));
   else
    v:=v||jsonb_build_object('enterprise_skipped',control.nexloop_relay_enterprise_problem(p_tenant,p_world,p_consumer,e));
   end if;
  end if;
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $function$
;

CREATE OR REPLACE FUNCTION authz.nexloop_message_run_issuance_command_before_takeover_v0049(p_digest text, p_world text, p_text text, p_signature text, p_payload text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
 SET "TimeZone" TO 'UTC'
AS $function$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;v_verb text:=c->>'verb';v_tenant text:=a->>'tenant_id';
 v_principal text:=a->>'principal_id';v_key bytea;v_def control.nexloop_action_definitions;v_source_def control.nexloop_action_definitions;v_name text;
 v_lease runtime.nexloop_message_relay_leases;v_out runtime.nexloop_message_outbox;v_conv runtime.nexloop_conversations;
 v_route runtime.nexloop_message_routes;v_issued authz.nexloop_message_run_issuances;v_root jsonb;v_proof jsonb;
 v_assignment ontology.objects;v_goal ontology.objects;v_step ontology.objects;v_control ontology.objects;v_consumer ontology.objects;
 v_props jsonb;v_ctl control.nexloop_effect_control_ledger;v_proof2 jsonb;v_resources text[];v_seconds int;v_ts timestamptz;v_expiry timestamptz;
begin
 if v_tenant is null or v_principal is null or session_user<>'nexloop_api' or p_world is distinct from 'real' or octet_length(p_text)>1048576 or octet_length(p_payload)>262144
 or a->>'protocol' is distinct from 'nexloop-message-run-issuance-v1' or a->>'action_resource' is distinct from 'eios:action:nexloop.conversation.route:1'
 or a->>'operation' is distinct from 'execute' or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
 or v_verb is null or v_verb not in('claim','issue','own_route','release','lookup') then raise exception 'message relay unavailable' using errcode='42501';end if;
 select key_material into v_key from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-message-run-issuance-v1:'||p_text,'UTF8'),v_key,'sha256'),'hex')
 or a->>'expires_at' is null or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then raise exception 'message relay unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 perform set_config('eios.tenant_id',v_tenant,true);
 if not exists(select 1 from authz.nexloop_service_credentials s where s.token_digest=p_digest and s.tenant_id=v_tenant and s.status='active' and s.binding->>'subject_kind'='service') then raise exception 'message relay unavailable' using errcode='42501';end if;
 select * into v_def from control.nexloop_action_definitions where tenant_id=v_tenant and world=p_world and resource_id=a->>'action_resource' and active for share;
 if not found or v_def.definition is distinct from a->'definition' or v_def.capability is distinct from a->'capability'
 or v_def.definition->'governance'->>'approval_mode' is distinct from 'none'
 or v_def.definition->'governance'->>'risk_level' is distinct from 'low'
 or jsonb_array_length(v_def.definition->'governance'->'policy_refs')<>0 then raise exception 'message relay unavailable' using errcode='42501';end if;
 if v_verb='claim' then
  if jsonb_typeof(c->'lease_seconds') is distinct from 'number' or c->>'lease_seconds'!~'^[0-9]{1,3}$'
   or c->>'consumer_id' is null or c->>'consumer_id'!~'^[a-f0-9]{64}$' then raise exception 'message relay unavailable' using errcode='42501';end if;
  v_seconds=(c->>'lease_seconds')::int;
  if v_seconds not between 3 and 300 then raise exception 'message relay unavailable' using errcode='42501';end if;
  -- Actual registered owner, canonical formal Consumer and Message/Conversation.
  select b.* into v_out from runtime.nexloop_message_outbox b join runtime.nexloop_conversations cv
   on cv.tenant_id=b.tenant_id and cv.world=b.world and cv.conversation_id=b.conversation_id
   join control.nexloop_consumer_owners ow on ow.tenant_id=cv.tenant_id and ow.world=cv.world and ow.principal_id=cv.principal_id and ow.consumer_id=cv.consumer_id
   join ontology.objects own on own.tenant_id=ow.tenant_id and own.world=ow.world and own.object_id=ow.ownership_id and own.type_name='ConsumerOwnership'
    and own.properties=jsonb_build_object('consumer_id',ow.consumer_id,'principal_id',ow.principal_id)
   join ontology.objects co on co.tenant_id=cv.tenant_id and co.world=cv.world and co.object_id=cv.consumer_id and co.type_name='Consumer'
   join ontology.objects formal on formal.tenant_id=cv.tenant_id and formal.world=cv.world and formal.object_id=cv.conversation_id and formal.type_name='Conversation'
    and formal.properties=jsonb_build_object('consumer_id',cv.consumer_id,'owner_principal',cv.principal_id)
   where b.tenant_id=v_tenant and b.world=p_world and b.status='pending' and cv.consumer_id=c->>'consumer_id'
    and not exists(select 1 from runtime.nexloop_message_relay_leases l where l.tenant_id=v_tenant and l.world=p_world and l.message_id=b.message_id and l.lease_until>clock_timestamp())
   order by b.conversation_id,b.sequence limit 1;
  if not found then perform authz.nexloop_assert_action_authority(p_digest,p_world,a);return null;end if;
  if not pg_try_advisory_xact_lock(hashtextextended(v_tenant||':'||p_world||':message-route:'||v_out.message_id,0)) then return null;end if;
  select * into v_route from runtime.nexloop_message_routes where tenant_id=v_tenant and world=p_world and message_id=v_out.message_id for update;
  select * into v_out from runtime.nexloop_message_outbox where tenant_id=v_tenant and world=p_world and message_id=v_out.message_id for update;
  if not found or v_out.status is distinct from 'pending' then perform authz.nexloop_assert_action_authority(p_digest,p_world,a);return null;end if;
  perform 1 from runtime.nexloop_message_relay_leases l where l.tenant_id=v_tenant and l.world=p_world and l.message_id=v_out.message_id and l.lease_until>clock_timestamp();
  if found then perform authz.nexloop_assert_action_authority(p_digest,p_world,a);return null;end if;
  insert into runtime.nexloop_message_relay_leases values(v_tenant,p_world,v_out.message_id,c->>'consumer_id',v_principal,p_digest,1,clock_timestamp()+make_interval(secs=>v_seconds))
   on conflict(tenant_id,world,message_id) do update set principal_id=excluded.principal_id,credential_digest=excluded.credential_digest,
    fence=runtime.nexloop_message_relay_leases.fence+1,lease_until=excluded.lease_until returning * into v_lease;
 else
  if c->>'message_id' is null or c->>'message_id'!~'^[a-f0-9]{64}$' then raise exception 'message relay unavailable' using errcode='42501';end if;
  perform pg_advisory_xact_lock(hashtextextended(v_tenant||':'||p_world||':message-route:'||(c->>'message_id'),0));
  select * into v_route from runtime.nexloop_message_routes where tenant_id=v_tenant and world=p_world and message_id=c->>'message_id' for update;
  select * into v_out from runtime.nexloop_message_outbox where tenant_id=v_tenant and world=p_world and message_id=c->>'message_id' for update;
  if not found then raise exception 'message relay unavailable' using errcode='42501';end if;
  select * into v_lease from runtime.nexloop_message_relay_leases where tenant_id=v_tenant and world=p_world and message_id=c->>'message_id' for update;
  if not found or v_lease.principal_id is distinct from v_principal or v_lease.credential_digest is distinct from p_digest
   or jsonb_typeof(c->'fence') is distinct from 'number' or c->>'fence'!~'^[0-9]{1,18}$' or v_lease.fence<>(c->>'fence')::bigint or v_lease.lease_until<=clock_timestamp() then raise exception 'message relay unavailable' using errcode='42501';end if;
  select * into v_out from runtime.nexloop_message_outbox where tenant_id=v_tenant and world=p_world and message_id=v_lease.message_id for update;
  if not found then raise exception 'message relay unavailable' using errcode='42501';end if;
 end if;
 select * into v_conv from runtime.nexloop_conversations where tenant_id=v_tenant and world=p_world and conversation_id=v_out.conversation_id for share;
 if not found or v_conv.consumer_id is distinct from v_lease.consumer_id then raise exception 'message relay unavailable' using errcode='42501';end if;
 -- Canonical formal Message must correspond exactly to committed outbox data.
 if not exists(select 1 from ontology.objects mo where mo.tenant_id=v_tenant and mo.world=p_world and mo.object_id=v_out.message_id and mo.type_name='Message'
  and mo.properties=jsonb_build_object('conversation_id',v_out.conversation_id,'sequence',v_out.sequence,'actor',v_out.record->>'actor','body',v_out.record->>'body','accepted_at',v_out.record->>'accepted_at')) then raise exception 'message relay unavailable' using errcode='42501';end if;
 select * into v_route from runtime.nexloop_message_routes where tenant_id=v_tenant and world=p_world and message_id=v_out.message_id for update;
 if found and v_route.bound_by is distinct from v_principal then raise exception 'message relay unavailable' using errcode='42501';end if;
 if v_verb in('claim','own_route') then
  if v_route.message_id is not null then
   if v_route.lease_until>clock_timestamp() then
    if v_verb is distinct from 'own_route' or v_route.lease_credential is distinct from p_digest or v_route.lease_until is distinct from v_lease.lease_until then
     raise exception 'message relay unavailable' using errcode='42501';end if;
   else
   update runtime.nexloop_message_routes set fence=fence+1,lease_credential=p_digest,lease_until=v_lease.lease_until
    where tenant_id=v_tenant and world=p_world and message_id=v_out.message_id returning * into v_route;
   end if;
  elsif v_verb='own_route' then raise exception 'message relay unavailable' using errcode='42501';end if;
  perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
  if v_lease.lease_until<=clock_timestamp() then raise exception 'message relay unavailable' using errcode='42501';end if;
  return jsonb_build_object('message_id',v_out.message_id,'tenant_id',v_tenant,'consumer_id',v_conv.consumer_id,'fence',v_lease.fence,
   'route_fence',v_route.fence,'source_event_id',v_out.event_id,'body',v_out.record->>'body');
 elsif v_verb='release' then
  if v_out.status is distinct from 'delivered' or v_route.task_id is null then raise exception 'message relay unavailable' using errcode='42501';end if;
  update runtime.nexloop_message_relay_leases set lease_until=clock_timestamp() where tenant_id=v_tenant and world=p_world and message_id=v_out.message_id;
  perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
  return jsonb_build_object('status','queued');
 end if;
 select * into v_issued from authz.nexloop_message_run_issuances where tenant_id=v_tenant and world=p_world and message_id=v_out.message_id for update;
 if v_verb='lookup' then
  perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
  if v_issued.message_id is null then return null;end if;
  return jsonb_build_object('run_id',v_issued.run_id,'request_id',v_issued.request_id,'expires_at',v_issued.expires_at,
   'state',case when v_issued.expires_at<=clock_timestamp() then 'requires_governed_replan' else 'issued' end);
 end if;
 if jsonb_typeof(c->'run_id') is distinct from 'string' or c->>'run_id'!~'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$'
  or jsonb_typeof(c->'source_digest') is distinct from 'string' or c->>'source_digest'!~'^[a-f0-9]{64}$'
  or jsonb_typeof(c->'request_id') is distinct from 'string' or c->>'assignment_id' is null or c->>'assignment_id'!~'^[a-f0-9]{64}$' or jsonb_typeof(c->'assignment_revision') is distinct from 'number' or c->>'assignment_revision' is distinct from '1'
  or c->>'assignment_digest' is null or c->>'assignment_digest'!~'^[a-f0-9]{64}$' or c->>'token_digest' is null or c->>'token_digest'!~'^[a-f0-9]{64}$'
  or c->>'issuance_nonce' is null or c->>'issuance_nonce'!~'^[a-f0-9]{64}$' or jsonb_typeof(c->'ttl_seconds') is distinct from 'number' or c->>'ttl_seconds'!~'^[0-9]{1,3}$'
  or jsonb_typeof(c->'source_proofs') is distinct from 'array' or jsonb_array_length(c->'source_proofs') not in (1,2) then raise exception 'message relay unavailable' using errcode='42501';end if;
 v_seconds=(c->>'ttl_seconds')::int;
 if v_seconds not between 1 and 300 then raise exception 'message relay unavailable' using errcode='42501';end if;
 select * into v_assignment from ontology.objects where tenant_id=v_tenant and world=p_world and object_id=c->>'assignment_id' and type_name='MessageAssignment' for share;
 if not found or v_assignment.nexloop_revision<>(c->>'assignment_revision')::bigint then raise exception 'message relay unavailable' using errcode='42501';end if;
 v_props=v_assignment.properties;
 if jsonb_typeof(v_props) is distinct from 'object' or (select count(*) from jsonb_object_keys(v_props))<>15
  or not(v_props ?& array['message_id','consumer_id','goal_id','goal_revision','step_id','step_revision','control_id','control_revision','consumer_revision','source_principal','executor_principal','recipe_digest','allowed_actions','state','valid_until']) then raise exception 'message relay unavailable' using errcode='42501';end if;
 for v_name in select unnest(array['message_id','consumer_id','goal_id','step_id','control_id','source_principal','executor_principal','recipe_digest','state','valid_until']) loop
  if jsonb_typeof(v_props->v_name) is distinct from 'string' or length(v_props->>v_name) not between 1 and 512 then raise exception 'message relay unavailable' using errcode='42501';end if;
 end loop;
 for v_name in select unnest(array['goal_revision','step_revision','control_revision','consumer_revision']) loop
  if jsonb_typeof(v_props->v_name) is distinct from 'number' or v_props->>v_name!~'^[1-9][0-9]{0,17}$' then raise exception 'message relay unavailable' using errcode='42501';end if;
 end loop;
 for v_name in select unnest(array['message_id','consumer_id','goal_id','step_id','control_id','recipe_digest']) loop
  if v_props->>v_name!~'^[a-f0-9]{64}$' then raise exception 'message relay unavailable' using errcode='42501';end if;
 end loop;
 if jsonb_typeof(c->'assignment_text') is distinct from 'string' or (c->>'assignment_text')::jsonb is distinct from v_props
  or c->>'assignment_digest' is distinct from encode(sha256(convert_to(c->>'assignment_text','UTF8')),'hex')
  or c->>'request_id' is distinct from 'message-'||encode(sha256(convert_to('["'||v_tenant||'","real","'||v_out.message_id||'"]','UTF8')),'hex') then raise exception 'message relay unavailable' using errcode='42501';end if;
 if v_props->>'message_id' is distinct from v_out.message_id or v_props->>'consumer_id' is distinct from v_conv.consumer_id or v_props->>'state' is distinct from 'active'
 or v_props->'allowed_actions' not in ('["eios:action:nexloop.service.request:1"]'::jsonb,'["eios:action:nexloop.service.request:1","eios:action:nexloop.enterprise.deliver:1"]'::jsonb)
 or jsonb_array_length(c->'source_proofs')<>jsonb_array_length(v_props->'allowed_actions') or (v_props->>'valid_until')::timestamptz<=clock_timestamp() then raise exception 'message relay unavailable' using errcode='42501';end if;
 select * into v_consumer from ontology.objects where tenant_id=v_tenant and world=p_world and object_id=v_conv.consumer_id and type_name='Consumer' for share;
 if not found or v_consumer.nexloop_revision<>(v_props->>'consumer_revision')::bigint then raise exception 'message relay unavailable' using errcode='42501';end if;
 select * into v_goal from ontology.objects where tenant_id=v_tenant and world=p_world and object_id=v_props->>'goal_id' and type_name='Goal' for share;
 if not found or jsonb_typeof(v_goal.properties->'valid_until') is distinct from 'string' or jsonb_typeof(v_goal.properties->'consumer_id') is distinct from 'string' or v_goal.nexloop_revision<>(v_props->>'goal_revision')::bigint or v_goal.properties->>'consumer_id' is distinct from v_conv.consumer_id or v_goal.properties->>'state' is distinct from 'active' or (v_goal.properties->>'valid_until')::timestamptz<=clock_timestamp() then raise exception 'message relay unavailable' using errcode='42501';end if;
 select * into v_step from ontology.objects where tenant_id=v_tenant and world=p_world and object_id=v_props->>'step_id' and type_name='PlanStep' for share;
 if not found or v_step.nexloop_revision<>(v_props->>'step_revision')::bigint or v_step.properties->>'consumer_id' is distinct from v_conv.consumer_id
 or v_step.properties->>'goal_id' is distinct from v_goal.object_id or v_step.properties->>'control_id' is distinct from v_props->>'control_id'
 or v_step.properties->>'state' is distinct from 'ready' or v_step.properties->>'action_name' is distinct from 'nexloop.service.request'
 or v_step.properties->'submitter_principals' is distinct from jsonb_build_array(v_props->>'source_principal') then raise exception 'message relay unavailable' using errcode='42501';end if;
 select * into v_control from ontology.objects where tenant_id=v_tenant and world=p_world and object_id=v_props->>'control_id' and type_name='EffectControl' for share;
 if not found or jsonb_typeof(v_control.properties->'valid_until') is distinct from 'string' or jsonb_typeof(v_control.properties->'executor_principal') is distinct from 'string' or v_control.nexloop_revision<>(v_props->>'control_revision')::bigint or v_control.properties->>'consumer_id' is distinct from v_conv.consumer_id or v_control.properties->'allow_effect' is distinct from 'true'::jsonb
 or v_control.properties->>'executor_principal' is distinct from v_props->>'executor_principal' or (v_control.properties->>'valid_until')::timestamptz<=clock_timestamp() then raise exception 'message relay unavailable' using errcode='42501';end if;
 select * into v_ctl from control.nexloop_effect_control_ledger where tenant_id=v_tenant and world=p_world and control_id=v_control.object_id for share;
 if not found or v_ctl.valid_until is null or v_ctl.control_revision is null or v_ctl.consumer_id is distinct from v_conv.consumer_id or v_ctl.control_revision<>v_control.nexloop_revision or v_ctl.executor_principal is distinct from v_props->>'executor_principal' or v_ctl.valid_until<=clock_timestamp() then raise exception 'message relay unavailable' using errcode='42501';end if;
 perform 1 from authz.nexloop_service_credentials where token_digest=c->>'source_digest' for share;
 if not found then raise exception 'message relay unavailable' using errcode='42501';end if;
 v_root=authz.nexloop_root_identity_snapshot(c->>'source_digest',p_world);
 if jsonb_typeof(v_root->'expires_at') is distinct from 'string' or v_root->>'directory_hash' is null or v_root->'binding'->>'tenant_id' is distinct from v_tenant or v_root->'binding'->>'subject_principal_id' is distinct from v_props->>'source_principal' then raise exception 'message relay unavailable' using errcode='42501';end if;
 select * into v_source_def from control.nexloop_action_definitions where tenant_id=v_tenant and world=p_world and resource_id='eios:action:nexloop.service.request:1' and active for share;
 if not found or v_source_def.definition is distinct from c->'source_definition' or v_source_def.capability is distinct from c->'source_capability' then raise exception 'message relay unavailable' using errcode='42501';end if;
 v_proof=c->'source_proofs'->0;
 if v_proof->>'resource_id' is distinct from 'eios:action:nexloop.service.request:1' then raise exception 'message relay unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(c->>'source_digest',p_world,v_proof);
 -- ADR-030: the enterprise slot needs the Source's own proof for the enterprise Action.
 select array_agg(x order by o) into v_resources from jsonb_array_elements_text(v_props->'allowed_actions') with ordinality t(x,o);
 if jsonb_array_length(v_props->'allowed_actions')=2 then
  v_proof2=c->'source_proofs'->1;
  if v_proof2->>'resource_id' is distinct from 'eios:action:nexloop.enterprise.deliver:1' then raise exception 'message relay unavailable' using errcode='42501';end if;
  perform authz.nexloop_assert_action_authority(c->>'source_digest',p_world,v_proof2);
 end if;
 if v_issued.message_id is not null then
  if v_issued.run_id is distinct from (c->>'run_id')::uuid or v_issued.run_digest is distinct from c->>'token_digest' or v_issued.request_id is distinct from c->>'request_id'
   or v_issued.issuance_nonce is distinct from c->>'issuance_nonce' or v_issued.assignment_id is distinct from v_assignment.object_id or v_issued.assignment_digest is distinct from c->>'assignment_digest'
   or v_issued.issuer_principal is distinct from v_principal
   or not exists(select 1 from authz.nexloop_run_credentials r where r.run_id=v_issued.run_id and r.source_digest=c->>'source_digest' and r.source_directory_hash=v_root->>'directory_hash' and r.allowed_resources=v_resources) then raise exception 'message relay conflict' using errcode='23505';end if;
 else
  v_ts=clock_timestamp();v_expiry=least((v_root->>'expires_at')::timestamptz,v_ts+make_interval(secs=>v_seconds),(v_props->>'valid_until')::timestamptz,(v_goal.properties->>'valid_until')::timestamptz,v_ctl.valid_until);
  insert into authz.nexloop_run_credentials values(c->>'token_digest',(c->>'run_id')::uuid,c->>'source_digest',v_root->>'directory_hash',p_world,'nexloop-agent-host',v_resources,'active',v_ts,v_expiry);
  insert into authz.nexloop_message_run_issuances values(v_tenant,p_world,v_out.message_id,(c->>'run_id')::uuid,c->>'request_id',c->>'issuance_nonce',v_assignment.object_id,v_assignment.nexloop_revision,c->>'assignment_digest',v_principal,c->>'token_digest',v_ts,v_expiry) returning * into v_issued;
 end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 perform authz.nexloop_assert_action_authority(c->>'source_digest',p_world,v_proof);
 if v_proof2 is not null then perform authz.nexloop_assert_action_authority(c->>'source_digest',p_world,v_proof2);end if;
 if v_lease.lease_until<=clock_timestamp() or v_ctl.valid_until<=clock_timestamp() or (v_props->>'valid_until')::timestamptz<=clock_timestamp() or (v_goal.properties->>'valid_until')::timestamptz<=clock_timestamp() then raise exception 'message relay unavailable' using errcode='42501';end if;
 return jsonb_build_object('run_id',v_issued.run_id,'request_id',v_issued.request_id,'expires_at',v_issued.expires_at,
  'state',case when v_issued.expires_at<=clock_timestamp() then 'requires_governed_replan' else 'issued' end);
end $function$
;

CREATE OR REPLACE FUNCTION authz.nexloop_message_run_issuance_command_before_relay_handoff_v0190(p_digest text, p_world text, p_text text, p_signature text, p_payload text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
 SET "TimeZone" TO 'UTC'
AS $function$
declare c jsonb:=p_payload::jsonb;v_verb text:=c->>'verb';v_tenant text:=p_text::jsonb->>'tenant_id';v_principal text:=p_text::jsonb->>'principal_id';
 v_result jsonb;v_consumer text;v_claim runtime.nexloop_relay_claims;e control.nexloop_relay_enrollments;
begin
 if v_verb in ('claim_next','profile') then return authz.nexloop_relay_multi_command(p_digest,p_world,p_text,p_signature,p_payload);end if;
 v_result:=authz.nexloop_message_run_issuance_command_before_relay_multi_v0160(p_digest,p_world,p_text,p_signature,p_payload);
 if v_verb='issue' and (c->>'assignment_text')::jsonb->'allowed_actions' @> '["eios:action:nexloop.enterprise.deliver:1"]'::jsonb then
  perform set_config('eios.tenant_id',v_tenant,true);
  select * into v_claim from runtime.nexloop_relay_claims where tenant_id=v_tenant and world=p_world and message_id=c->>'message_id';
  if not found then raise exception 'enterprise slot unavailable' using errcode='42501';end if;
  e:=control.nexloop_relay_enrollment_current(v_tenant,p_world,v_claim.consumer_id);
  if control.nexloop_relay_enterprise_problem(v_tenant,p_world,v_claim.consumer_id,e) is not null then
   raise exception 'enterprise slot unavailable' using errcode='42501';end if;
 end if;
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
end $function$
;
