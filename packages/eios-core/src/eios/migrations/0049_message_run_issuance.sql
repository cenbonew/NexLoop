-- Candidate only: Root must review/register, never modify prior checksums.
create table runtime.nexloop_message_relay_leases (
 tenant_id text not null,world text not null check(world='real'),message_id text not null,
 consumer_id text not null,principal_id text not null,credential_digest text not null,
 fence bigint not null check(fence>0),lease_until timestamptz not null,
 primary key(tenant_id,world,message_id)
);
create table authz.nexloop_message_run_issuances (
 tenant_id text not null,world text not null check(world='real'),message_id text not null,
 run_id uuid not null unique references authz.nexloop_run_credentials(run_id),
 request_id text not null,issuance_nonce text not null check(issuance_nonce~'^[0-9a-f]{64}$'),
 assignment_id text not null,assignment_revision bigint not null check(assignment_revision>0),
 assignment_digest text not null check(assignment_digest~'^[0-9a-f]{64}$'),
 issuer_principal text not null,run_digest text not null unique references authz.nexloop_run_credentials(token_digest),
 issued_at timestamptz not null,expires_at timestamptz not null,
 primary key(tenant_id,world,message_id),check(expires_at>issued_at and expires_at<=issued_at+interval '300 seconds')
);
alter table runtime.nexloop_message_relay_leases owner to nexloop_owner;
alter table authz.nexloop_message_run_issuances owner to nexloop_owner;
alter table runtime.nexloop_message_relay_leases enable row level security;
alter table runtime.nexloop_message_relay_leases force row level security;
alter table authz.nexloop_message_run_issuances enable row level security;
alter table authz.nexloop_message_run_issuances force row level security;
create policy message_relay_tenant on runtime.nexloop_message_relay_leases to nexloop_owner using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
create policy message_issuance_tenant on authz.nexloop_message_run_issuances to nexloop_owner using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_message_relay_leases,authz.nexloop_message_run_issuances from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;

create function authz.nexloop_message_run_issuance_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on set timezone='UTC' as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;v_verb text:=c->>'verb';v_tenant text:=a->>'tenant_id';
 v_principal text:=a->>'principal_id';v_key bytea;v_def control.nexloop_action_definitions;v_source_def control.nexloop_action_definitions;v_name text;
 v_lease runtime.nexloop_message_relay_leases;v_out runtime.nexloop_message_outbox;v_conv runtime.nexloop_conversations;
 v_route runtime.nexloop_message_routes;v_issued authz.nexloop_message_run_issuances;v_root jsonb;v_proof jsonb;
 v_assignment ontology.objects;v_goal ontology.objects;v_step ontology.objects;v_control ontology.objects;v_consumer ontology.objects;
 v_props jsonb;v_ctl control.nexloop_effect_control_ledger;v_seconds int;v_ts timestamptz;v_expiry timestamptz;
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
  or jsonb_typeof(c->'source_proofs') is distinct from 'array' or jsonb_array_length(c->'source_proofs')<>1 then raise exception 'message relay unavailable' using errcode='42501';end if;
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
 or v_props->'allowed_actions' is distinct from '["eios:action:nexloop.service.request:1"]'::jsonb or (v_props->>'valid_until')::timestamptz<=clock_timestamp() then raise exception 'message relay unavailable' using errcode='42501';end if;
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
 if v_issued.message_id is not null then
  if v_issued.run_id is distinct from (c->>'run_id')::uuid or v_issued.run_digest is distinct from c->>'token_digest' or v_issued.request_id is distinct from c->>'request_id'
   or v_issued.issuance_nonce is distinct from c->>'issuance_nonce' or v_issued.assignment_id is distinct from v_assignment.object_id or v_issued.assignment_digest is distinct from c->>'assignment_digest'
   or v_issued.issuer_principal is distinct from v_principal
   or not exists(select 1 from authz.nexloop_run_credentials r where r.run_id=v_issued.run_id and r.source_digest=c->>'source_digest' and r.source_directory_hash=v_root->>'directory_hash' and r.allowed_resources=array['eios:action:nexloop.service.request:1']) then raise exception 'message relay conflict' using errcode='23505';end if;
 else
  v_ts=clock_timestamp();v_expiry=least((v_root->>'expires_at')::timestamptz,v_ts+make_interval(secs=>v_seconds),(v_props->>'valid_until')::timestamptz,(v_goal.properties->>'valid_until')::timestamptz,v_ctl.valid_until);
  insert into authz.nexloop_run_credentials values(c->>'token_digest',(c->>'run_id')::uuid,c->>'source_digest',v_root->>'directory_hash',p_world,'nexloop-agent-host',array['eios:action:nexloop.service.request:1'],'active',v_ts,v_expiry);
  insert into authz.nexloop_message_run_issuances values(v_tenant,p_world,v_out.message_id,(c->>'run_id')::uuid,c->>'request_id',c->>'issuance_nonce',v_assignment.object_id,v_assignment.nexloop_revision,c->>'assignment_digest',v_principal,c->>'token_digest',v_ts,v_expiry) returning * into v_issued;
 end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 perform authz.nexloop_assert_action_authority(c->>'source_digest',p_world,v_proof);
 if v_lease.lease_until<=clock_timestamp() or v_ctl.valid_until<=clock_timestamp() or (v_props->>'valid_until')::timestamptz<=clock_timestamp() or (v_goal.properties->>'valid_until')::timestamptz<=clock_timestamp() then raise exception 'message relay unavailable' using errcode='42501';end if;
 return jsonb_build_object('run_id',v_issued.run_id,'request_id',v_issued.request_id,'expires_at',v_issued.expires_at,
  'state',case when v_issued.expires_at<=clock_timestamp() then 'requires_governed_replan' else 'issued' end);
end $$;
alter function authz.nexloop_message_run_issuance_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_message_run_issuance_command(text,text,text,text,text) from public,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;
grant execute on function authz.nexloop_message_run_issuance_command(text,text,text,text,text) to nexloop_api;
