-- CANDIDATE DRAFT ONLY. Root registers after the frozen 0046 test terminal.
-- Message routing is technical admission; Source Run remains the actual agent.
-- UUIDv5 canonical event identities require private owner-only SHA1 computation.
grant execute on function extensions.digest(bytea,text) to nexloop_owner;
create table runtime.nexloop_message_routes (
 tenant_id text not null,world text not null,message_id text not null,
 run_id uuid not null unique references authz.nexloop_run_credentials(run_id),run_digest text not null,
 queue text not null,event_id uuid not null unique,event_envelope jsonb not null,
 command jsonb not null,command_digest text not null,input_digest text not null,
 bound_by text not null,context_id uuid not null references control.nexloop_effect_contexts(context_id),
 fence bigint not null default 0,lease_credential text,lease_until timestamptz,task_id text,
 primary key(tenant_id,world,message_id),check(fence>=0),check(world='real'),
 check(command_digest~'^[a-f0-9]{64}$' and input_digest~'^[a-f0-9]{64}$')
);
alter table runtime.nexloop_message_routes owner to nexloop_owner;
alter table runtime.nexloop_message_routes enable row level security;
alter table runtime.nexloop_message_routes force row level security;
create policy message_route_tenant on runtime.nexloop_message_routes to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_message_routes from public,nexloop_api,nexloop_scheduler,nexloop_domain_worker,nexloop_action_worker,nexloop_identity;

-- Private helper validates actual current Source Run and its pre-bound formal
-- Consumer/Goal/Step/control, not caller-provided reference text alone.
create function authz.nexloop_assert_message_source(p_digest text,p_world text,p_tenant text,p_consumer text,p_command jsonb,p_proofs jsonb)
 returns uuid language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare identity jsonb;r authz.nexloop_run_credentials;link control.nexloop_effect_run_contexts;
 ctx control.nexloop_effect_contexts;plan control.nexloop_effect_plan_bindings;
 ctl control.nexloop_effect_control_ledger;proof jsonb;o ontology.objects;matched integer:=0;
begin
 identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if identity->'run_context' is null or identity->'run_context'='null'::jsonb
  or identity->'binding'->>'tenant_id' is distinct from p_tenant then raise exception 'message runtime unavailable' using errcode='42501';end if;
 select * into r from authz.nexloop_run_credentials where token_digest=p_digest for share;
 if not found or r.run_id is distinct from (p_command->>'run_id')::uuid or r.expires_at<=clock_timestamp()
  or p_command->>'tenant_id' is distinct from p_tenant or p_command->>'world_id' is distinct from p_world
  or p_command->>'mode' is distinct from 'real' or p_command->>'consumer_ref' is distinct from 'consumer:'||p_consumer
  or (p_command->>'not_after')::timestamptz>r.expires_at or (p_command->>'not_after')::timestamptz<=clock_timestamp()
  or jsonb_typeof(p_proofs) is distinct from 'array' or jsonb_array_length(p_proofs) not between 1 and 32 then
  raise exception 'message runtime unavailable' using errcode='42501';end if;
 for proof in select value from jsonb_array_elements(p_proofs) loop
  perform authz.nexloop_assert_action_authority(p_digest,p_world,proof);
  if proof->>'tenant_id' is distinct from p_tenant then raise exception 'message runtime unavailable' using errcode='42501';end if;
 end loop;
 -- Exact actual Run allowlist coverage; no missing source Action proof.
 if exists(select 1 from jsonb_array_elements_text(identity->'run_context'->'allowed_resources') x
   where not exists(select 1 from jsonb_array_elements(p_proofs) y where y->>'action_resource'=x)) then
  raise exception 'message runtime unavailable' using errcode='42501';end if;
 select * into link from control.nexloop_effect_run_contexts where run_id=r.run_id for share;
 if not found or link.valid_until<=clock_timestamp() then raise exception 'message runtime unavailable' using errcode='42501';end if;
 select * into ctx from control.nexloop_effect_contexts where context_id=link.context_id and tenant_id=p_tenant and world=p_world for share;
 if not found or ctx.consumer_id is distinct from p_consumer or not ctx.allow_effect or ctx.valid_until<=clock_timestamp()
  or ctx.goal_version_ref is distinct from p_command->>'goal_version_ref' then raise exception 'message runtime unavailable' using errcode='42501';end if;
 select * into plan from control.nexloop_effect_plan_bindings where context_id=ctx.context_id and tenant_id=p_tenant and world=p_world for share;
 if not found or not(plan.submitter_principals ? (identity->'binding'->>'subject_principal_id')) then
  raise exception 'message runtime unavailable' using errcode='42501';end if;
 select * into ctl from control.nexloop_effect_control_ledger where control_id=plan.control_id and tenant_id=p_tenant and world=p_world for share;
 if not found or ctl.consumer_id is distinct from p_consumer or ctl.control_revision<>plan.control_revision
  or ctl.valid_until<=clock_timestamp() then raise exception 'message runtime unavailable' using errcode='42501';end if;
 for o in select * from ontology.objects where tenant_id=p_tenant and world=p_world
  and object_id in(p_consumer,plan.goal_id,plan.step_id,plan.control_id) order by object_id for share loop
  if o.object_id=p_consumer and o.type_name='Consumer' and o.nexloop_revision=ctx.consumer_revision then matched:=matched+1;
  elsif o.object_id=plan.goal_id and o.type_name='Goal' and o.nexloop_revision=plan.goal_revision
   and o.properties->>'consumer_id'=p_consumer and o.properties->>'state'='active'
   and (o.properties->>'valid_until')::timestamptz>clock_timestamp() then matched:=matched+1;
  elsif o.object_id=plan.step_id and o.type_name='PlanStep' and o.nexloop_revision=plan.step_revision
   and o.properties->>'consumer_id'=p_consumer and o.properties->>'goal_id'=plan.goal_id
   and o.properties->>'control_id'=plan.control_id and o.properties->>'state'='ready'
   and o.properties->'submitter_principals'=plan.submitter_principals then matched:=matched+1;
  elsif o.object_id=plan.control_id and o.type_name='EffectControl' and o.nexloop_revision=plan.control_revision
   and o.properties->>'consumer_id'=p_consumer and o.properties->'allow_effect'='true'::jsonb
   and (o.properties->>'valid_until')::timestamptz>clock_timestamp() then matched:=matched+1;
  else raise exception 'message runtime unavailable' using errcode='42501';end if;
 end loop;
 if matched<>4 or r.expires_at<=clock_timestamp() or link.valid_until<=clock_timestamp() or ctx.valid_until<=clock_timestamp()
  or ctl.valid_until<=clock_timestamp() or (p_command->>'not_after')::timestamptz<=clock_timestamp() then
  raise exception 'message runtime unavailable' using errcode='42501';end if;
 for proof in select value from jsonb_array_elements(p_proofs) loop perform authz.nexloop_assert_action_authority(p_digest,p_world,proof);end loop;
 return ctx.context_id;
end $$;
alter function authz.nexloop_assert_message_source(text,text,text,text,jsonb,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_assert_message_source(text,text,text,text,jsonb,jsonb) from public,nexloop_api,nexloop_scheduler,nexloop_domain_worker,nexloop_action_worker,nexloop_identity;

create function authz.nexloop_message_runtime_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;tenant text:=a->>'tenant_id';verb text:=c->>'verb';
 principal text:=a->>'principal_id';route runtime.nexloop_message_routes;outbox runtime.nexloop_message_outbox;
 conv runtime.nexloop_conversations;command jsonb;event jsonb;context_uuid uuid;result jsonb;
 def control.nexloop_action_definitions;binding authz.nexloop_runtime_run_bindings;job runtime.jobs;
 lease_seconds integer;event_uuid uuid;event_hash bytea;event_hex text;
begin
 if session_user<>'nexloop_api' or p_world is distinct from 'real' or octet_length(p_text)>1048576 or octet_length(p_payload)>262144
  or a->>'protocol' is distinct from 'nexloop-message-runtime-v1' or a->>'operation' is distinct from 'execute'
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or verb is null or verb not in ('message','bind','claim','authorize','ack','read') then
  raise exception 'message runtime unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-message-runtime-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'message runtime unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 perform set_config('eios.tenant_id',tenant,true);
 select * into def from control.nexloop_action_definitions where tenant_id=tenant and world=p_world and resource_id=a->>'action_resource' and active for share;
 if not found or def.definition is distinct from a->'definition' or def.capability is distinct from a->'capability'
  or def.definition->'governance'->>'approval_mode' is distinct from 'none'
  or def.definition->'governance'->>'risk_level' is distinct from 'low'
  or jsonb_array_length(def.definition->'governance'->'policy_refs')<>0 then raise exception 'message runtime unavailable' using errcode='42501';end if;
 if verb='read' then
  if a->>'action_resource' is distinct from 'eios:action:nexloop.conversation.read:1'
   or not exists(select 1 from control.nexloop_browser_sessions where encode(session_token_digest,'hex')=p_digest) then
   raise exception 'message runtime unavailable' using errcode='42501';end if;
 else
  if a->>'action_resource' is distinct from 'eios:action:nexloop.conversation.route:1'
   or not exists(select 1 from authz.nexloop_service_credentials svc where svc.token_digest=p_digest and svc.tenant_id=tenant
    and svc.binding->>'subject_kind'='service' and svc.status='active') then raise exception 'message runtime unavailable' using errcode='42501';end if;
 end if;
 if verb='claim' then
  if jsonb_typeof(c->'lease_seconds') is distinct from 'number' or c->>'lease_seconds'!~'^[0-9]{1,3}$' then
   raise exception 'message runtime unavailable' using errcode='42501';end if;
  lease_seconds=(c->>'lease_seconds')::integer;
  if lease_seconds not between 3 and 300 then raise exception 'message runtime unavailable' using errcode='42501';end if;
  select r.* into route from runtime.nexloop_message_routes r join runtime.nexloop_message_outbox b
   on b.tenant_id=r.tenant_id and b.world=r.world and b.message_id=r.message_id
   where r.tenant_id=tenant and r.world=p_world and b.status='pending' and r.bound_by=principal
    and (r.lease_until is null or r.lease_until<=clock_timestamp()) order by b.conversation_id,b.sequence for update of r skip locked limit 1;
  if not found then perform authz.nexloop_assert_action_authority(p_digest,p_world,a);return null;end if;
  update runtime.nexloop_message_routes set fence=fence+1,lease_credential=p_digest,lease_until=clock_timestamp()+make_interval(secs=>lease_seconds)
   where tenant_id=tenant and world=p_world and message_id=route.message_id returning * into route;
 else
  if verb='bind' then perform pg_advisory_xact_lock(hashtextextended(tenant||':'||p_world||':message-route:'||(c->>'message_id'),0));end if;
  if c->>'message_id' is null or c->>'message_id'!~'^[a-f0-9]{64}$' then raise exception 'message runtime unavailable' using errcode='42501';end if;
  select * into route from runtime.nexloop_message_routes where tenant_id=tenant and world=p_world and message_id=c->>'message_id' for update;
 end if;
 select * into outbox from runtime.nexloop_message_outbox where tenant_id=tenant and world=p_world
  and message_id=case when verb='claim' then route.message_id else c->>'message_id' end for update;
 if not found then raise exception 'message runtime unavailable' using errcode='42501';end if;
 select * into conv from runtime.nexloop_conversations where tenant_id=tenant and world=p_world and conversation_id=outbox.conversation_id for share;
 if not found or not exists(select 1 from ontology.objects where tenant_id=tenant and world=p_world and type_name='Conversation'
  and object_id=conv.conversation_id and properties=jsonb_build_object('consumer_id',conv.consumer_id,'owner_principal',conv.principal_id)) then
  raise exception 'message runtime unavailable' using errcode='42501';end if;
 if verb='read' then
  if not exists(select 1 from ontology.objects where tenant_id=tenant and world=p_world and type_name='Consumer' and object_id=conv.consumer_id)
   or conv.principal_id is distinct from principal or not exists(select 1 from control.nexloop_consumer_owners o join ontology.objects f
    on f.tenant_id=o.tenant_id and f.world=o.world and f.object_id=o.ownership_id and f.type_name='ConsumerOwnership'
    and f.properties=jsonb_build_object('consumer_id',o.consumer_id,'principal_id',o.principal_id)
    where o.tenant_id=tenant and o.world=p_world and o.principal_id=principal and o.consumer_id=conv.consumer_id) then
   raise exception 'message runtime unavailable' using errcode='42501';end if;
  perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
  if route.task_id is null or outbox.status<>'delivered' then return null;end if;
  return jsonb_build_object('message_id',route.message_id,'run_id',route.run_id,'request_id',route.command->>'request_id','task_id',route.task_id,'status','queued');
 end if;
 if route.message_id is not null and route.bound_by is distinct from principal then raise exception 'message runtime unavailable' using errcode='42501';end if;
 if verb='message' then
  perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
  return jsonb_build_object('record',outbox.record,'source_event_id',outbox.event_id);
 elsif verb='bind' then
  if jsonb_typeof(c->'command_text') is distinct from 'string' or jsonb_typeof(c->'event_text') is distinct from 'string'
   or c->>'command_digest' is distinct from encode(sha256(convert_to(c->>'command_text','UTF8')),'hex')
   or c->>'input_digest' is distinct from encode(sha256(convert_to(outbox.record->>'body','UTF8')),'hex')
   or c->>'queue' is null or c->>'queue'!~'^[A-Za-z][A-Za-z0-9_-]{0,63}$' then raise exception 'message runtime unavailable' using errcode='42501';end if;
  command=(c->>'command_text')::jsonb;event=(c->>'event_text')::jsonb;event_uuid=(event->>'event_id')::uuid;
  if jsonb_typeof(c->'event_name_text') is distinct from 'string'
   or (c->>'event_name_text')::jsonb is distinct from jsonb_build_array(tenant,p_world,'webchat',outbox.event_id) then
   raise exception 'message runtime unavailable' using errcode='42501';end if;
  event_hash=extensions.digest(uuid_send('e0963cdf-ea02-587c-a8bc-c04f7c2c4029'::uuid)||convert_to(c->>'event_name_text','UTF8'),'sha1');
  event_hex=encode(event_hash,'hex');
  if event_uuid is distinct from (substr(event_hex,1,8)||'-'||substr(event_hex,9,4)||'-5'||substr(event_hex,14,3)||'-'
    ||to_hex(((get_byte(event_hash,8)>>4)&3)|8)||substr(event_hex,18,3)||'-'||substr(event_hex,21,12))::uuid then
   raise exception 'message runtime unavailable' using errcode='42501';end if;
  if event is distinct from jsonb_build_object('schema_version','1.0','event_id',event_uuid::text,'tenant_id',tenant,'world_id',p_world,
   'mode','real','event_type','message.accepted','source','webchat','source_event_id',outbox.event_id,
   'occurred_at',outbox.record->>'accepted_at','recorded_at',outbox.record->>'accepted_at','subject_ref','message:'||outbox.message_id,
   'correlation_id',command->>'run_id','causation_id',null,'payload',jsonb_build_object('message_ref','message:'||outbox.message_id,
    'conversation_ref','conversation:'||outbox.conversation_id)) then raise exception 'message runtime unavailable' using errcode='42501';end if;
  if event->>'schema_version' is distinct from '1.0' or event->>'tenant_id' is distinct from tenant
   or event->>'world_id' is distinct from p_world or event->>'mode' is distinct from 'real'
   or event->>'source' is distinct from 'webchat' or event->>'source_event_id' is distinct from outbox.event_id
   or event->>'event_type' is distinct from 'message.accepted' or event->>'subject_ref' is distinct from 'message:'||outbox.message_id
   or event->>'correlation_id' is distinct from command->>'run_id' or event->'causation_id' is distinct from 'null'::jsonb
   or event->>'occurred_at' is distinct from outbox.record->>'accepted_at' or event->>'recorded_at' is distinct from outbox.record->>'accepted_at'
   or event->'payload' is distinct from jsonb_build_object('message_ref','message:'||outbox.message_id,'conversation_ref','conversation:'||outbox.conversation_id)
   or command->>'trigger_event_id' is distinct from event->>'event_id' then raise exception 'message runtime unavailable' using errcode='42501';end if;
  context_uuid=authz.nexloop_assert_message_source(c->>'run_digest',p_world,tenant,conv.consumer_id,command,c->'run_proofs');
  if route.message_id is not null then
   if (route.run_digest,route.command_digest,route.input_digest,route.queue,route.event_envelope)
    is distinct from (c->>'run_digest',c->>'command_digest',c->>'input_digest',c->>'queue',event) then
    raise exception 'message_runtime_conflict' using errcode='P0001';end if;
  else
   if outbox.status<>'pending' then raise exception 'message runtime unavailable' using errcode='42501';end if;
   insert into runtime.nexloop_message_routes(tenant_id,world,message_id,run_id,run_digest,queue,event_id,event_envelope,command,command_digest,input_digest,bound_by,context_id)
    values(tenant,p_world,outbox.message_id,(command->>'run_id')::uuid,c->>'run_digest',c->>'queue',event_uuid,event,command,c->>'command_digest',c->>'input_digest',principal,context_uuid) returning * into route;
  end if;
  perform authz.nexloop_assert_message_source(route.run_digest,p_world,tenant,conv.consumer_id,route.command,c->'run_proofs');
  perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
  return jsonb_build_object('message_id',route.message_id,'run_id',route.run_id,'event_id',route.event_id,'bound',true);
 end if;
 if route.message_id is null or route.lease_credential is distinct from p_digest or route.lease_until is null or route.lease_until<=clock_timestamp()
  or (verb<>'claim' and (jsonb_typeof(c->'fence') is distinct from 'number' or c->>'fence'!~'^[1-9][0-9]{0,18}$'
   or route.fence::text is distinct from c->>'fence')) then raise exception 'message runtime unavailable' using errcode='42501';end if;
 if verb='claim' then
  perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
  return jsonb_build_object('message_id',route.message_id,'event_id',route.event_id,'fence',route.fence,'queue',route.queue,
   'command',route.command,'input',outbox.record->>'body','_run_digest',route.run_digest,'event_envelope',route.event_envelope);
 elsif verb='authorize' then
  perform authz.nexloop_assert_message_source(route.run_digest,p_world,tenant,conv.consumer_id,route.command,c->'run_proofs');
  perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
  if route.lease_until<=clock_timestamp() then raise exception 'message runtime unavailable' using errcode='42501';end if;
  return jsonb_build_object('authorized',true);
 elsif verb='ack' then
  if c->'allow_missing' not in ('true'::jsonb,'false'::jsonb) or c->'allow_missing' is null then raise exception 'message runtime unavailable' using errcode='42501';end if;
  select b.* into binding from authz.nexloop_runtime_run_bindings b join runtime.nexloop_inbox i
   on i.tenant_id=b.tenant_id and i.world=b.world and i.job_id=b.task_id and i.source_id='webchat' and i.event_id=route.event_id::text
   where b.run_id=route.run_id and b.tenant_id=tenant and b.world=p_world for share of b,i;
  if not found then
   if c->'allow_missing'='true'::jsonb then
    perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
    if route.lease_until<=clock_timestamp() then raise exception 'message runtime unavailable' using errcode='42501';end if;
    return null;end if;
   raise exception 'message runtime unavailable' using errcode='42501';end if;
  select * into job from runtime.jobs where tenant_id=tenant and job_id=binding.task_id for share;
  if not found or binding.run_digest is distinct from route.run_digest or binding.command_digest is distinct from route.command_digest
   or binding.input_digest is distinct from route.input_digest or binding.queue is distinct from route.queue
   or binding.owner_epoch is distinct from (route.command->>'runtime_owner_epoch')::bigint
   or job.normalized_input is distinct from jsonb_build_object('run_command',route.command,'input',outbox.record->>'body') then
   raise exception 'message runtime unavailable' using errcode='42501';end if;
  update runtime.nexloop_message_routes set task_id=binding.task_id where tenant_id=tenant and world=p_world and message_id=route.message_id;
  update runtime.nexloop_message_outbox set status='delivered' where tenant_id=tenant and world=p_world and message_id=route.message_id;
  perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
  if route.lease_until<=clock_timestamp() then raise exception 'message runtime unavailable' using errcode='42501';end if;
  return jsonb_build_object('message_id',route.message_id,'run_id',route.run_id,'request_id',route.command->>'request_id','task_id',binding.task_id,'status','queued');
 end if;
 raise exception 'message runtime unavailable' using errcode='42501';
end $$;
alter function authz.nexloop_message_runtime_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_message_runtime_command(text,text,text,text,text) from public,nexloop_scheduler,nexloop_domain_worker,nexloop_action_worker,nexloop_identity;
grant execute on function authz.nexloop_message_runtime_command(text,text,text,text,text) to nexloop_api;
