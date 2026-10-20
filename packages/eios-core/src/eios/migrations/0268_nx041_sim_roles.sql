-- NX-041 S6c (design D3; L6 nx041-s6-review 1.2; plan build/nx041-s6c-plan.md, option b): the simulation process group's own
-- database roles. The migration number lives only in the file name and the catalog; nothing below repeats it.
-- 1. Three login roles without any privilege or membership: nexloop_sim_worker (the simulation runtime worker's dispatcher),
--    nexloop_sim_guard (its guard child processes) and nexloop_sim_api (event intake and Run credentials of a simulation world).
--    None of them is a member of a real role, so none inherits a real grant; the simulation secret directory holds no real password.
-- 2. Each role executes exactly the functions listed below (the initial list of the plan: the Run chain's common entries and the
--    functions marked needed; everything else is not granted). tests/test_nx041_sim_roles_pg.py fixes the list: a new grant to
--    a simulation role fails that test until the list is changed on purpose. The list is to be corrected by a track_functions
--    trace once D10 (the three credentials per world) is decided.
-- 3. The eight functions that check the caller's role by name (directly or in the inner version an entry wraps) read
--    authz.nexloop_app_role(p_world) instead of session_user: a simulation role counts as its real counterpart (nexloop_sim_api as
--    nexloop_api, nexloop_sim_worker and nexloop_sim_guard as nexloop_domain_worker) only for a sim:/shadow: world (or for a call
--    without a world: the numeric process samples); for real, test or any other world it counts as no application role at all, so
--    every check refuses. A real role's result is session_user exactly as before. Each body is otherwise copied unchanged.

do $$declare r text;begin
 foreach r in array array['nexloop_sim_worker','nexloop_sim_guard','nexloop_sim_api'] loop
  if not exists(select 1 from pg_roles where rolname=r) then
   execute format('create role %I login nosuperuser nocreatedb nocreaterole noinherit noreplication nobypassrls',r);
  end if;
  execute format('grant usage on schema authz to %I',r);
 end loop;
end $$;

create function authz.nexloop_app_role(p_world text) returns text language sql stable set search_path=pg_catalog,pg_temp as $$
 select case
  when session_user::text not in ('nexloop_sim_worker','nexloop_sim_guard','nexloop_sim_api') then session_user::text
  when p_world is not null and coalesce(runtime.nexloop_world_kind(p_world),'') not in ('simulation','shadow') then 'nexloop_sim_outside_its_world'
  when session_user::text='nexloop_sim_api' then 'nexloop_api'
  else 'nexloop_domain_worker' end
$$;
alter function authz.nexloop_app_role(text) owner to nexloop_owner;
revoke all on function authz.nexloop_app_role(text) from public;

-- 2. The grants (initial list) --------------------------------------------------------------------------------------------------
do $$declare g record;begin
 for g in select * from (values
   ('nexloop_sim_worker','authz.nexloop_queue_command(text,text,text,text,text)'),
   ('nexloop_sim_worker','authz.nexloop_runtime_activation_command(text,text,text,text,text)'),
   ('nexloop_sim_worker','authz.nexloop_runtime_activation_hint(text,text,text)'),
   ('nexloop_sim_worker','authz.nexloop_run_tool_timing(text,text,uuid)'),
   ('nexloop_sim_guard','authz.nexloop_runtime_activation_command(text,text,text,text,text)'),
   ('nexloop_sim_guard','authz.nexloop_runtime_activation_hint(text,text,text)'),
   ('nexloop_sim_guard','authz.nexloop_record_run_tool_timing(text,text,uuid,numeric)'),
   ('nexloop_sim_guard','authz.nexloop_sim_effect_submit(text,text,jsonb)'),
   ('nexloop_sim_guard','authz.nexloop_sim_effect_query(text,text,uuid)'),
   ('nexloop_sim_api','authz.nexloop_queue_command(text,text,text,text,text)'),
   ('nexloop_sim_api','authz.nexloop_runtime_activation_command(text,text,text,text,text)'),
   ('nexloop_sim_api','authz.nexloop_issue_run_credential(text,text,text,text)')) v(r,f)
  union all
  select r,f from unnest(array['nexloop_sim_worker','nexloop_sim_guard','nexloop_sim_api']) r
   cross join unnest(array['authz.nexloop_service_identity_snapshot(text,text)','authz.nexloop_load_authority_facts(text,text,jsonb)',
     'authz.nexloop_load_authority_fact_snapshot(text,text,text,text[])','authz.nexloop_verify_backend_signer(text,text,text)',
     'authz.nexloop_record_process_sample(text,text,text,timestamptz,integer,jsonb)']) f
 loop
  execute format('grant execute on function %s to %I',g.f,g.r);
 end loop;
end $$;

-- 3. The role checks ----------------------------------------------------------------------------------------------------------
-- (0037's two functions also gain pg_temp last in their search_path, as every redefined security definer function must.)
-- authz.nexloop_queue_command_before_refusals_v0150 (0209_queue_dead_letter_cause.sql): the role checks read authz.nexloop_app_role(p_world); the body is otherwise unchanged.
create or replace function authz.nexloop_queue_command_before_refusals_v0150(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare c jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;k bytea;identity jsonb;binding jsonb;
 tenant text;actor text;q text:=p->>'queue';verb text:=p->>'verb';target text;ts timestamptz;
 j runtime.jobs%rowtype;o runtime.nexloop_outbox%rowtype;i runtime.nexloop_inbox%rowtype;
 jid text;iid text;digest text;seconds integer;original_lease timestamptz;sequence_number integer;
begin
 if octet_length(p_text)>1048576 or octet_length(p_payload)>262144 or q is null or q !~ '^[A-Za-z][A-Za-z0-9_-]{0,63}$'
  or authz.nexloop_app_role(p_world) not in ('nexloop_api','nexloop_scheduler','nexloop_domain_worker') then
  raise exception 'queue authority denied' using errcode='42501';end if;
 if (verb='accept' and authz.nexloop_app_role(p_world)<>'nexloop_api') or (verb in ('outbox_claim','outbox_ack') and authz.nexloop_app_role(p_world)<>'nexloop_scheduler')
  or (verb in ('claim','finish','renew','assert_lease') and authz.nexloop_app_role(p_world) not in ('nexloop_scheduler','nexloop_domain_worker')) then
  raise exception 'queue role denied' using errcode='42501';end if;
 target:='eios:action:NexLoop.queue.'||q||':1';
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=c->>'key_id' and active;
 if not found or c->>'protocol' is distinct from 'nexloop-queue-command-v1' or c->>'resource_id' is distinct from target
  or c->>'expires_at' is null or (c->>'expires_at')::timestamptz<=clock_timestamp()
  or (c->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds'
  or c->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-queue-command-v1:'||p_text,'UTF8'),k,'sha256'),'hex') then
  raise exception 'queue signature denied' using errcode='42501';end if;
 perform authz.nexloop_lock_credential(p_digest,p_world,target);
 identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if identity->'run_context' is distinct from 'null'::jsonb then raise exception 'queue Run denied' using errcode='42501';end if;
 binding:=authz.nexloop_assert_action_authority(p_digest,p_world,c);
 tenant:=binding->>'tenant_id';actor:=binding->>'subject_principal_id';ts:=clock_timestamp();
 if verb='accept' then
  if p->>'source_id' is null or length(p->>'source_id') not between 1 and 255
   or p->>'event_id' is null or length(p->>'event_id') not between 1 and 255
   or jsonb_typeof(p->'payload') is distinct from 'object' or p->>'max_attempts' is null or (p->>'max_attempts')::integer not between 1 and 10 then
   raise exception 'queue input invalid';end if;
  digest:=encode(sha256(convert_to(p_payload,'UTF8')),'hex');
  -- Serialize the exact inbox key before creating ledger rows; collisions only
  -- cause conservative serialization, never alias the persisted key.
  perform pg_advisory_xact_lock(hashtextextended(jsonb_build_array(tenant,p_world,p->>'source_id',p->>'event_id')::text,0));
  perform authz.nexloop_assert_action_authority(p_digest,p_world,c);ts:=clock_timestamp();
  select * into i from runtime.nexloop_inbox where tenant_id=tenant and world=p_world and source_id=p->>'source_id' and event_id=p->>'event_id';
  if found then
   if i.payload_digest is distinct from digest then raise exception 'queue_payload_conflict';end if;
   select * into j from runtime.jobs where tenant_id=tenant and job_id=i.job_id;
   perform authz.nexloop_assert_action_authority(p_digest,p_world,c);ts:=clock_timestamp();
  return jsonb_build_object('accepted',true,'created',false,'task_id',j.job_id,'status',j.status);
  end if;
  jid:='task_'||pg_catalog.gen_random_uuid()::text;iid:='inv_'||pg_catalog.gen_random_uuid()::text;
  insert into runtime.invocations(invocation_id,tenant_id,capability_name,capability_version,actor_id,trace_id,request_id,correlation_id,input_digest,status,job_id,created_at,updated_at)
   values(iid,tenant,'NexLoop.event',q,actor,jid,p->>'event_id',jid,digest,'accepted',jid,ts,ts);
  insert into runtime.jobs(job_id,invocation_id,capability_name,capability_version,capability_type,execution_mode,tenant_id,actor_id,trace_id,request_id,
   normalized_input,plan_snapshot,status,created_at,updated_at,world,queue,available_at,max_attempts)
   values(jid,iid,'NexLoop.event',q,'workflow','async',tenant,actor,jid,p->>'event_id',p->'payload','{}','pending',ts,ts,p_world,q,ts,(p->>'max_attempts')::integer);
  insert into runtime.nexloop_inbox values(tenant,p_world,p->>'source_id',p->>'event_id',digest,jid,ts);
  insert into runtime.nexloop_outbox(tenant_id,world,queue,outbox_id,job_id,kind,status,created_at)
   values(tenant,p_world,q,'out_'||pg_catalog.gen_random_uuid()::text,jid,'task.accepted','pending',ts);
  insert into runtime.job_events(tenant_id,job_id,sequence,status,message,created_at) values(tenant,jid,1,'pending','event persisted',ts);
  perform authz.nexloop_assert_action_authority(p_digest,p_world,c);ts:=clock_timestamp();
  return jsonb_build_object('accepted',true,'created',true,'task_id',jid,'status','pending');
 elsif verb in ('claim','outbox_claim','renew') then
  seconds:=(p->>'lease_seconds')::integer;
  if seconds is null or seconds not between 1 and 300 then raise exception 'queue lease invalid';end if;
 end if;
 if verb='claim' then
  -- Reclaim exhausted tasks without granting any new Action execution.
  for j in select * from runtime.jobs where tenant_id=tenant and world=p_world and queue=q and status='running'
   and lease_until<=ts and attempts>=max_attempts order by job_id for update skip locked limit 100 loop
   -- 0209: the dead letter names its cause (the holder never finished before its lease ran out); codes only.
   update runtime.jobs set status='dead_lettered',updated_at=ts,lease_until=null,
    result=coalesce(result,jsonb_build_object('scope','runtime_only','business_action_success',false,'code','runtime_lease_exhausted'))
    where tenant_id=tenant and job_id=j.job_id;
   update runtime.invocations set status='dead_lettered',updated_at=ts where tenant_id=tenant and invocation_id=j.invocation_id;
   select coalesce(max(sequence),0)+1 into sequence_number from runtime.job_events where tenant_id=tenant and job_id=j.job_id;
   insert into runtime.job_events(tenant_id,job_id,sequence,status,message,created_at) values(tenant,j.job_id,sequence_number,'dead_lettered','lease attempts exhausted',ts);
  end loop;
  select * into j from runtime.jobs where tenant_id=tenant and world=p_world and queue=q and attempts<max_attempts
   and ((status in ('pending','retry_wait') and available_at<=ts) or (status='running' and lease_until<=ts))
   order by available_at,job_id for update skip locked limit 1;
  if not found then perform authz.nexloop_assert_action_authority(p_digest,p_world,c);ts:=clock_timestamp();
  return null;end if;
  perform authz.nexloop_assert_action_authority(p_digest,p_world,c);ts:=clock_timestamp();
  update runtime.jobs set status='running',attempts=attempts+1,fencing_token=fencing_token+1,lease_until=ts+make_interval(secs=>seconds),
   lease_credential=p_digest,updated_at=ts where tenant_id=tenant and job_id=j.job_id returning * into j;
  update runtime.invocations set status='running',updated_at=ts where tenant_id=tenant and invocation_id=j.invocation_id;
  select coalesce(max(sequence),0)+1 into sequence_number from runtime.job_events where tenant_id=tenant and job_id=j.job_id;
  insert into runtime.job_events(tenant_id,job_id,sequence,status,message,data,created_at) values(tenant,j.job_id,sequence_number,'running','task lease claimed',jsonb_build_object('fence',j.fencing_token),ts);
  perform authz.nexloop_assert_action_authority(p_digest,p_world,c);ts:=clock_timestamp();
  if j.lease_until<=clock_timestamp() then raise exception 'queue_stale_lease';end if;
  return jsonb_build_object('task_id',j.job_id,'fence',j.fencing_token,'lease_until',j.lease_until,'attempts',j.attempts,'payload',j.normalized_input,'status',j.status);
 elsif verb in ('finish','renew') then
  select * into j from runtime.jobs where tenant_id=tenant and world=p_world and queue=q and job_id=p->>'task_id' for update;
  if found and verb='finish' and j.status in ('succeeded','failed','retry_wait','dead_lettered')
   and j.completion_digest is not null and j.lease_credential=p_digest and j.fencing_token=(p->>'fence')::bigint then
   if j.completion_digest is distinct from c->>'parameters_digest' then raise exception 'queue_payload_conflict';end if;
   perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
   return jsonb_build_object('task_id',j.job_id,'fence',j.fencing_token,'status',j.status);
  end if;
  if not found or j.status<>'running' or j.lease_until<=clock_timestamp() or j.lease_credential is distinct from p_digest
   or j.fencing_token is distinct from (p->>'fence')::bigint then raise exception 'queue_stale_lease';end if;
  perform authz.nexloop_assert_action_authority(p_digest,p_world,c);ts:=clock_timestamp();
  original_lease:=j.lease_until;
  if verb='renew' then
   update runtime.jobs set lease_until=clock_timestamp()+make_interval(secs=>seconds),updated_at=clock_timestamp() where tenant_id=tenant and job_id=j.job_id returning * into j;
   perform authz.nexloop_assert_action_authority(p_digest,p_world,c);ts:=clock_timestamp();
  if original_lease<=clock_timestamp() or j.lease_until<=clock_timestamp() then raise exception 'queue_stale_lease';end if;
   return jsonb_build_object('task_id',j.job_id,'fence',j.fencing_token,'lease_until',j.lease_until);
  end if;
  if p->>'status' is null or p->>'status' not in ('succeeded','failed','retry_wait') or p->>'retry_seconds' is null or (p->>'retry_seconds')::integer not between 0 and 3600 then raise exception 'queue result invalid';end if;
  if p->>'status'='retry_wait' and j.attempts>=j.max_attempts then p:=jsonb_set(p,'{status}','"dead_lettered"');end if;
  update runtime.jobs set status=p->>'status',result=p->'result',completion_digest=c->>'parameters_digest',lease_until=null,updated_at=clock_timestamp(),
   available_at=clock_timestamp()+make_interval(secs=>(p->>'retry_seconds')::integer) where tenant_id=tenant and job_id=j.job_id returning * into j;
  update runtime.invocations set status=j.status,result=j.result,updated_at=j.updated_at where tenant_id=tenant and invocation_id=j.invocation_id;
  select coalesce(max(sequence),0)+1 into sequence_number from runtime.job_events where tenant_id=tenant and job_id=j.job_id;
  insert into runtime.job_events(tenant_id,job_id,sequence,status,message,created_at) values(tenant,j.job_id,sequence_number,j.status,'task lease finished',j.updated_at);
  perform authz.nexloop_assert_action_authority(p_digest,p_world,c);ts:=clock_timestamp();
  if original_lease<=clock_timestamp() then raise exception 'queue_stale_lease';end if;
  return jsonb_build_object('task_id',j.job_id,'fence',j.fencing_token,'status',j.status);
 elsif verb='assert_lease' then
  select * into j from runtime.jobs where tenant_id=tenant and world=p_world and queue=q and job_id=p->>'task_id' for share;
  if not found or j.status<>'running' or j.lease_until<=clock_timestamp() or j.lease_credential is distinct from p_digest
   or j.fencing_token is distinct from (p->>'fence')::bigint then raise exception 'queue_stale_lease';end if;
  perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
  if j.lease_until<=clock_timestamp() then raise exception 'queue_stale_lease';end if;
  return jsonb_build_object('task_id',j.job_id,'fence',j.fencing_token,'lease_until',j.lease_until,'payload',j.normalized_input,'world',j.world);
 elsif verb='inspect' then
  select * into j from runtime.jobs where tenant_id=tenant and world=p_world and queue=q and job_id=p->>'task_id';
  if not found then return null;end if;
  perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
  return jsonb_build_object('task_id',j.job_id,'status',j.status,'fence',j.fencing_token,'attempts',j.attempts,'result',j.result);
 elsif verb='outbox_claim' then
  select * into o from runtime.nexloop_outbox where tenant_id=tenant and world=p_world and queue=q
   and (status='pending' or (status='leased' and lease_until<=ts)) order by created_at,outbox_id for update skip locked limit 1;
  if not found then perform authz.nexloop_assert_action_authority(p_digest,p_world,c);ts:=clock_timestamp();
  return null;end if;
  perform authz.nexloop_assert_action_authority(p_digest,p_world,c);ts:=clock_timestamp();
  update runtime.nexloop_outbox set status='leased',fence=fence+1,lease_until=ts+make_interval(secs=>seconds),lease_credential=p_digest
   where tenant_id=tenant and outbox_id=o.outbox_id returning * into o;
  perform authz.nexloop_assert_action_authority(p_digest,p_world,c);ts:=clock_timestamp();
  if o.lease_until<=clock_timestamp() then raise exception 'queue_stale_lease';end if;
  return jsonb_build_object('outbox_id',o.outbox_id,'task_id',o.job_id,'fence',o.fence,'kind',o.kind,'lease_until',o.lease_until);
 elsif verb='outbox_ack' then
  select * into o from runtime.nexloop_outbox where tenant_id=tenant and world=p_world and queue=q and outbox_id=p->>'outbox_id' for update;
  if found and o.status='delivered' and o.lease_credential=p_digest and o.fence=(p->>'fence')::bigint then
   perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
   return jsonb_build_object('outbox_id',o.outbox_id,'delivered',true);
  end if;
  if not found or o.status<>'leased' or o.lease_until<=clock_timestamp() or o.lease_credential is distinct from p_digest
   or o.fence is distinct from (p->>'fence')::bigint then raise exception 'queue_stale_lease';end if;
  perform authz.nexloop_assert_action_authority(p_digest,p_world,c);ts:=clock_timestamp();
  update runtime.nexloop_outbox set status='delivered',lease_until=null where tenant_id=tenant and outbox_id=o.outbox_id;
  perform authz.nexloop_assert_action_authority(p_digest,p_world,c);ts:=clock_timestamp();
  if o.lease_until<=clock_timestamp() then raise exception 'queue_stale_lease';end if;
  return jsonb_build_object('outbox_id',o.outbox_id,'delivered',true);
 end if;
 raise exception 'queue verb invalid';
end $$;

-- authz.nexloop_runtime_activation_command_v0037 (0037_runtime_activation_registry.sql): the role checks read authz.nexloop_app_role(p_world); the body is otherwise unchanged.
create or replace function authz.nexloop_runtime_activation_command_v0037(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare c jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;k bytea;identity jsonb;queue_binding jsonb;run_identity jsonb;
 q text:=p->>'queue';verb text:=p->>'verb';target text;tenant text;command jsonb;cmd_digest text;input_hash text;owner bigint;
 j runtime.jobs%rowtype;r authz.nexloop_run_credentials%rowtype;b authz.nexloop_runtime_run_bindings%rowtype;
 a authz.nexloop_runtime_activations%rowtype;proof jsonb;seen text[]:=array[]::text[];ts timestamptz;
begin
 if authz.nexloop_app_role(p_world) not in ('nexloop_api','nexloop_scheduler','nexloop_domain_worker') or octet_length(p_text)>1048576 or octet_length(p_payload)>1048576
 or q is null or q !~ '^[A-Za-z][A-Za-z0-9_-]{0,63}$'
 or (verb='register' and authz.nexloop_app_role(p_world)<>'nexloop_api')
 or (verb in ('create','resolve','authorize') and authz.nexloop_app_role(p_world) not in ('nexloop_scheduler','nexloop_domain_worker')) then
  raise exception 'activation denied' using errcode='42501';end if;
 target:='eios:action:NexLoop.queue.'||q||':1';
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=c->>'key_id' and active;
 if not found or c->>'protocol' is distinct from 'nexloop-runtime-activation-v1' or c->>'resource_id' is distinct from target
 or c->>'expires_at' is null or (c->>'expires_at')::timestamptz<=clock_timestamp()
 or (c->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds'
 or c->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
 or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-runtime-activation-v1:'||p_text,'UTF8'),k,'sha256'),'hex') then
  raise exception 'activation denied' using errcode='42501';end if;
 perform authz.nexloop_lock_credential(p_digest,p_world,target);
 identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if identity->'run_context' is distinct from 'null'::jsonb then raise exception 'activation denied' using errcode='42501';end if;
 queue_binding:=authz.nexloop_assert_action_authority(p_digest,p_world,c);tenant:=queue_binding->>'tenant_id';
 command:=(p->>'command_text')::jsonb;cmd_digest:=encode(sha256(convert_to(p->>'command_text','UTF8')),'hex');input_hash:=p->>'input_digest';
 if jsonb_typeof(command) is distinct from 'object' or p->>'command_digest' is distinct from cmd_digest
 or command->>'tenant_id' is distinct from tenant or command->>'world_id' is distinct from p_world
 or command->>'not_after' is null or (command->>'not_after')::timestamptz<=clock_timestamp() then
  raise exception 'activation command denied' using errcode='42501';end if;
 owner:=(command->>'runtime_owner_epoch')::bigint;
 if owner is null or owner<1 then raise exception 'activation command denied' using errcode='42501';end if;
 if verb in ('register','create') then
  if input_hash is null or input_hash !~ '^[a-f0-9]{64}$' then raise exception 'activation input denied' using errcode='42501';end if;
  select * into j from runtime.jobs where tenant_id=tenant and job_id=p->>'task_id' and world=p_world and queue=q for share;
 else
  select * into a from authz.nexloop_runtime_activations where activation_ref=p->>'activation_ref' and tenant_id=tenant and world=p_world and queue=q for share;
  if not found then raise exception 'activation denied' using errcode='42501';end if;
  select * into j from runtime.jobs where tenant_id=tenant and job_id=a.task_id and world=p_world and queue=q for share;
 end if;
 if not found or j.normalized_input->'run_command' is distinct from command then raise exception 'activation task denied' using errcode='42501';end if;
 if verb='register' then
  select * into r from authz.nexloop_run_credentials where token_digest=p->>'run_digest' and world=p_world for share;
  if not found then raise exception 'activation Run denied' using errcode='42501';end if;
 else
  select * into b from authz.nexloop_runtime_run_bindings where run_id=(command->>'run_id')::uuid and tenant_id=tenant and world=p_world and queue=q and task_id=j.job_id for share;
  if not found or b.command_digest is distinct from cmd_digest or b.owner_epoch is distinct from owner
   or (input_hash is not null and b.input_digest is distinct from input_hash) then raise exception 'activation binding denied' using errcode='42501';end if;
  select * into r from authz.nexloop_run_credentials where token_digest=b.run_digest and world=p_world for share;
 end if;
 if not found or r.run_id::text is distinct from command->>'run_id' or r.status<>'active' or r.expires_at<=clock_timestamp()
  or (command->>'not_after')::timestamptz>r.expires_at then raise exception 'activation Run denied' using errcode='42501';end if;
 run_identity:=authz.nexloop_service_identity_snapshot(r.token_digest,p_world);
 if run_identity->'binding'->>'tenant_id' is distinct from tenant or run_identity->'run_context'->>'run_id' is distinct from r.run_id::text then
  raise exception 'activation identity denied' using errcode='42501';end if;
 if verb in ('register','authorize') then
  if jsonb_typeof(p->'run_proofs') is distinct from 'array' or jsonb_array_length(p->'run_proofs') is distinct from cardinality(r.allowed_resources) then
   raise exception 'activation proofs denied' using errcode='42501';end if;
  for proof in select value from jsonb_array_elements(p->'run_proofs') order by value->>'resource_id' loop
   if proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp() or (proof->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds'
    or proof->>'resource_id' is null or not (proof->>'resource_id'=any(r.allowed_resources)) or proof->>'resource_id'=any(seen) then
    raise exception 'activation proofs denied' using errcode='42501';end if;
   seen:=array_append(seen,proof->>'resource_id');
   perform authz.nexloop_lock_credential(r.token_digest,p_world,proof->>'resource_id');
   perform authz.nexloop_assert_action_authority(r.token_digest,p_world,proof);
  end loop;
 end if;
 if verb='register' then
  if j.status not in ('pending','retry_wait','running') then raise exception 'activation task denied' using errcode='42501';end if;
  perform pg_advisory_xact_lock(hashtextextended(jsonb_build_array(tenant,p_world,j.job_id,'runtime-enrollment')::text,0));
  insert into authz.nexloop_runtime_run_bindings values(r.run_id,tenant,p_world,q,j.job_id,r.token_digest,cmd_digest,input_hash,owner,p_digest,clock_timestamp())
   on conflict do nothing;
  select * into b from authz.nexloop_runtime_run_bindings where run_id=r.run_id;
  if not found or b.tenant_id is distinct from tenant or b.world is distinct from p_world or b.queue is distinct from q or b.task_id is distinct from j.job_id
   or b.run_digest is distinct from r.token_digest or b.command_digest is distinct from cmd_digest or b.input_digest is distinct from input_hash or b.owner_epoch is distinct from owner then
   raise exception 'activation enrollment conflict' using errcode='42501';end if;
  perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
  perform authz.nexloop_service_identity_snapshot(r.token_digest,p_world);
  for proof in select value from jsonb_array_elements(p->'run_proofs') order by value->>'resource_id' loop
   if proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'activation proof expired' using errcode='42501';end if;
   perform authz.nexloop_assert_action_authority(r.token_digest,p_world,proof);
  end loop;
  if (command->>'not_after')::timestamptz<=clock_timestamp() then raise exception 'activation expired' using errcode='42501';end if;
  return jsonb_build_object('registered',true,'run_id',r.run_id,'task_id',j.job_id,'command_digest',cmd_digest,'input_digest',input_hash,'owner_epoch',owner);
 end if;
 if j.status<>'running' or j.lease_until<=clock_timestamp() or j.lease_credential is distinct from p_digest
 or (verb='create' and (j.fencing_token is distinct from (p->>'fence')::bigint or r.run_id::text is distinct from p->>'run_id' or owner is distinct from (p->>'owner_epoch')::bigint))
 or (verb in ('resolve','authorize') and (a.fence is distinct from j.fencing_token or a.lease_credential is distinct from p_digest
   or a.run_id is distinct from r.run_id or a.owner_epoch is distinct from owner or a.command_digest is distinct from cmd_digest or a.input_digest is distinct from b.input_digest)) then
  raise exception 'activation lease denied' using errcode='42501';end if;
 if verb='create' then
  insert into authz.nexloop_runtime_activations values('activation_'||pg_catalog.gen_random_uuid()::text,r.run_id,tenant,p_world,q,j.job_id,j.fencing_token,p_digest,owner,cmd_digest,input_hash,clock_timestamp()) on conflict do nothing;
  select * into a from authz.nexloop_runtime_activations where tenant_id=tenant and world=p_world and task_id=j.job_id and fence=j.fencing_token;
  if a.run_id is distinct from r.run_id or a.command_digest is distinct from cmd_digest or a.input_digest is distinct from input_hash
   or a.owner_epoch is distinct from owner or a.lease_credential is distinct from p_digest then raise exception 'activation conflict' using errcode='42501';end if;
 elsif verb in ('resolve','authorize') then
  if p->>'operation' is null or p->>'operation' not in ('start','resume','inspect','cancel','model','tool')
   or (p->>'operation' in ('start','resume') and input_hash is null) then raise exception 'activation operation denied' using errcode='42501';end if;
 else raise exception 'activation verb denied' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 perform authz.nexloop_service_identity_snapshot(r.token_digest,p_world);
 if verb='authorize' then
  for proof in select value from jsonb_array_elements(p->'run_proofs') order by value->>'resource_id' loop
   if proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'activation proof expired' using errcode='42501';end if;
   perform authz.nexloop_assert_action_authority(r.token_digest,p_world,proof);
  end loop;
 end if;
 if j.lease_until<=clock_timestamp() or (command->>'not_after')::timestamptz<=clock_timestamp() then raise exception 'activation expired' using errcode='42501';end if;
 if verb='create' then
  return jsonb_build_object('activation_ref',a.activation_ref,'run_id',r.run_id,'task_id',j.job_id,'fence',j.fencing_token,'owner_epoch',owner,'command_digest',cmd_digest,'input_digest',input_hash);
 end if;
 -- Private digest is consumed only by trusted backend to construct the same
 -- EIOS Run session. The public Python port strips it before Host transport.
 return jsonb_build_object('authorized',true,'activation_ref',a.activation_ref,'run_id',r.run_id,'task_id',j.job_id,'fence',j.fencing_token,
  'owner_epoch',owner,'command_digest',cmd_digest,'input_digest',b.input_digest,'_run_digest',r.token_digest);
end $$;

-- authz.nexloop_runtime_activation_hint (0037_runtime_activation_registry.sql): the role checks read authz.nexloop_app_role(p_world); the body is otherwise unchanged.
create or replace function authz.nexloop_runtime_activation_hint(p_digest text,p_world text,p_ref text)
 returns text language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare identity jsonb;q text;
begin
 if authz.nexloop_app_role(p_world) not in ('nexloop_scheduler','nexloop_domain_worker') then raise exception 'activation denied' using errcode='42501';end if;
 identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if identity->'run_context' is distinct from 'null'::jsonb then raise exception 'activation denied' using errcode='42501';end if;
 perform set_config('eios.tenant_id',identity->'binding'->>'tenant_id',true);
 select a.queue into q from authz.nexloop_runtime_activations a join runtime.jobs j on j.tenant_id=a.tenant_id and j.job_id=a.task_id
 where a.activation_ref=p_ref and a.tenant_id=identity->'binding'->>'tenant_id' and a.world=p_world
 and j.world=p_world and j.queue=a.queue and j.status='running' and j.lease_until>clock_timestamp()
 and a.fence=j.fencing_token and a.lease_credential=p_digest and j.lease_credential=p_digest;
 if not found then raise exception 'activation denied' using errcode='42501';end if;
 return q;
end $$;

-- authz.nexloop_run_tool_timing (0133_nx030_ui.sql): the role checks read authz.nexloop_app_role(p_world); the body is otherwise unchanged.
create or replace function authz.nexloop_run_tool_timing(p_digest text,p_world text,p_run uuid) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text;t runtime.nexloop_run_tool_timings;
begin
 if authz.nexloop_app_role(p_world) not in ('nexloop_domain_worker','nexloop_scheduler') then raise exception 'tool timing unavailable' using errcode='42501';end if;
 v_tenant:=authz.nexloop_service_identity_snapshot(p_digest,p_world)->'binding'->>'tenant_id';
 perform set_config('eios.tenant_id',v_tenant,true);
 select * into t from runtime.nexloop_run_tool_timings where tenant_id=v_tenant and world=p_world and run_id=p_run;
 return jsonb_build_object('calls',coalesce(t.calls,0),'total_ms',coalesce(round(t.total_ms,1),0),'max_ms',coalesce(round(t.max_ms,1),0));
end $$;

-- authz.nexloop_record_run_tool_timing (0133_nx030_ui.sql): the role checks read authz.nexloop_app_role(p_world); the body is otherwise unchanged.
create or replace function authz.nexloop_record_run_tool_timing(p_digest text,p_world text,p_run uuid,p_elapsed_ms numeric) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text;
begin
 if authz.nexloop_app_role(p_world) not in ('nexloop_domain_worker','nexloop_scheduler') or p_elapsed_ms is null or p_elapsed_ms<0 or p_elapsed_ms>600000 then
  raise exception 'tool timing unavailable' using errcode='42501';end if;
 v_tenant:=authz.nexloop_service_identity_snapshot(p_digest,p_world)->'binding'->>'tenant_id';
 if not exists(select 1 from authz.nexloop_run_credentials r join authz.nexloop_service_credentials s on s.token_digest=r.source_digest
   where r.run_id=p_run and r.world=p_world and s.tenant_id=v_tenant) then raise exception 'tool timing unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 insert into runtime.nexloop_run_tool_timings(tenant_id,world,run_id,calls,total_ms,max_ms) values(v_tenant,p_world,p_run,1,p_elapsed_ms,p_elapsed_ms)
  on conflict(tenant_id,world,run_id) do update set calls=runtime.nexloop_run_tool_timings.calls+1,total_ms=runtime.nexloop_run_tool_timings.total_ms+excluded.total_ms,
   max_ms=greatest(runtime.nexloop_run_tool_timings.max_ms,excluded.max_ms),updated_at=clock_timestamp();
end $$;

-- authz.nexloop_record_process_sample (0188_relay_step_samples.sql): the role checks read authz.nexloop_app_role(null); the body is otherwise unchanged.
create or replace function authz.nexloop_record_process_sample(p_kind text,p_service text,p_instance text,p_window_start timestamptz,p_window_seconds integer,p_sample jsonb)
 returns void language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
declare allowed text[];k text;
begin
 allowed:=case p_kind
  when 'guard' then array['calls','timeouts','errors','p50_ms','p95_ms','max_ms']
  when 'guard_op' then array['calls','timeouts','errors','p50_ms','p95_ms','max_ms']
  when 'relay_step' then array['calls','timeouts','errors','p50_ms','p95_ms','max_ms']
  when 'host' then array['active_runs','waiting','max_active_runs','admission_timeouts','busy_refusals','runs_started','available']
  when 'pool' then array['pool_size','pool_available','requests_waiting','requests_num','requests_errors','requests_wait_ms','usage_ms','backend_busy','max_size']
  end;
 if allowed is null or authz.nexloop_app_role(null) not in ('nexloop_api','nexloop_domain_worker','nexloop_action_worker','nexloop_scheduler')
  or jsonb_typeof(p_sample) is distinct from 'object' or p_window_seconds not between 1 and 3600
  or p_window_start>clock_timestamp()+interval '1 minute' or p_window_start<clock_timestamp()-interval '1 day'
  or (p_kind='guard_op' and p_service not in ('start','resume','inspect','cancel','model','model_result','tool','effect_submit','effect_find','outcome','incentive','other'))
  or (p_kind='relay_step' and p_service not in ('backlog','claim','recover','lookup','goal_create','step_create','enterprise_step_create','assignment_create',
   'vault_prepare','source_proofs','issue','effect_bind','context_prepare','message_bind','own_route','route_authorize','runtime_accept','ack','release',
   'handoff','total','authenticate')) then
  raise exception 'process sample invalid' using errcode='22023';end if;
 for k in select jsonb_object_keys(p_sample) loop
  if not (k=any(allowed) or (p_kind in ('guard','guard_op','relay_step') and k ~ '^h_(0|[1-9][0-9]?|10[0-9]|11[0-6])$' and jsonb_typeof(p_sample->k)='number'))
   or jsonb_typeof(p_sample->k) not in ('number','boolean') then raise exception 'process sample invalid' using errcode='22023';end if;
 end loop;
 insert into runtime.nexloop_process_samples(kind,service,instance,window_start,window_seconds,sample) values(p_kind,p_service,p_instance,p_window_start,p_window_seconds,p_sample);
end $$;

-- authz.nexloop_sim_effect_submit (0262_nx041_stand_ins.sql, as of L6's review fixes 2 with N4 and the outcome seed; re-copied at
-- integration): the role checks read authz.nexloop_app_role(p_world); the body is otherwise unchanged.
create or replace function authz.nexloop_sim_effect_submit(p_digest text,p_world text,p_payload jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text;s control.nexloop_stand_ins;x runtime.nexloop_sim_effect_intents;v_principal text;v_seed text;
begin
 if authz.nexloop_app_role(p_world) not in ('nexloop_api','nexloop_domain_worker') then raise exception 'stand-in: unavailable' using errcode='42501';end if;
 v_tenant:=authz.nexloop_sim_effect_tenant(p_digest,p_world);
 if jsonb_typeof(p_payload) is distinct from 'object' or exists(select 1 from jsonb_object_keys(p_payload) k where k not in
   ('intent_id','action_name','action_version','provider_kind','business_digest','request_digest','origin'))
  or coalesce(p_payload->>'intent_id','')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
  or coalesce(p_payload->>'business_digest','')!~'^[0-9a-f]{64}$' or coalesce(p_payload->>'request_digest','')!~'^[0-9a-f]{64}$'
  or coalesce(p_payload->>'origin','') not in ('twin','replay','injected') or jsonb_typeof(p_payload->'action_version') is distinct from 'number' then
  raise exception 'stand-in: invalid' using errcode='22023';end if;
 -- D11: no stand-in, no effect in a simulation world.
 select * into s from control.nexloop_stand_ins where action_name=p_payload->>'action_name' and action_version=(p_payload->>'action_version')::integer
  and provider_kind=p_payload->>'provider_kind';
 if not found then raise exception 'stand-in: unavailable' using errcode='42501';end if;
 v_principal:=authz.nexloop_service_identity(p_digest,p_world)->'binding'->>'principal_id';
 -- One open intent per business intent in a world (review N4): an intent is open until its receipt is fulfilled or failed (unknown
 -- is not terminal: query first). A second intent id for the same business digest while one is open is refused, so a submitter
 -- cannot resubmit under new intent ids and keep only the fulfilled receipt. Serialized per business digest.
 perform pg_advisory_xact_lock(hashtextextended('nx041-stand-in:'||v_tenant||':'||p_world||':'||(p_payload->>'business_digest'),0));
 if exists(select 1 from runtime.nexloop_sim_effect_intents i where i.tenant_id=v_tenant and i.world=p_world and i.business_digest=p_payload->>'business_digest'
     and i.intent_id<>(p_payload->>'intent_id')::uuid
     and not exists(select 1 from runtime.nexloop_sim_effect_receipts r where r.tenant_id=i.tenant_id and r.world=i.world and r.intent_id=i.intent_id
       and r.status in ('fulfilled','failed'))) then
  raise exception 'stand-in: business_intent_open' using errcode='22023';end if;
 -- S7 decision A: with a frozen outcome seed the salt is sha256(seed | intent id), reproducible in a replay world; without a
 -- configuration it stays a fresh random salt.
 select c.outcome_seed into v_seed from control.nexloop_stand_in_configs c where c.world=p_world and c.tenant_id=v_tenant;
 insert into runtime.nexloop_sim_effect_intents(tenant_id,world,intent_id,stand_in_id,business_digest,request_digest,origin,sim_clock,outcome_salt,outcome_seeded,submitted_by)
  -- The simulated clock is the world's (S5), the salt the server's: the submitter can predict neither, so it cannot pick an outcome
  -- by choosing its intent id or a clock (review 4.1/4.2). Both stay with the intent, so a repeat gives the same receipt.
  values(v_tenant,p_world,(p_payload->>'intent_id')::uuid,s.stand_in_id,p_payload->>'business_digest',p_payload->>'request_digest',p_payload->>'origin',
   runtime.nexloop_world_now(v_tenant,p_world),
   case when v_seed is null then encode(sha256(convert_to(gen_random_uuid()::text||gen_random_uuid()::text||clock_timestamp()::text,'UTF8')),'hex')
    else encode(sha256(convert_to(v_seed||'|'||lower(p_payload->>'intent_id'),'UTF8')),'hex') end,v_seed is not null,
   coalesce(v_principal,'service'))
  on conflict(tenant_id,world,intent_id) do nothing;
 select * into x from runtime.nexloop_sim_effect_intents where tenant_id=v_tenant and world=p_world and intent_id=(p_payload->>'intent_id')::uuid;
 if x.stand_in_id<>s.stand_in_id or x.business_digest<>p_payload->>'business_digest' or x.request_digest<>p_payload->>'request_digest' then
  raise exception 'stand-in: intent_conflict' using errcode='22023';end if;
 return jsonb_build_object('intent_id',x.intent_id,'world',p_world,'stand_in_id',s.stand_in_id,'stand_in_version',s.stand_in_version,'state','accepted','simulated',true);
end $$;

-- authz.nexloop_sim_effect_query (0262_nx041_stand_ins.sql): the role checks read authz.nexloop_app_role(p_world); the body is otherwise unchanged.
create or replace function authz.nexloop_sim_effect_query(p_digest text,p_world text,p_intent uuid) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text;r runtime.nexloop_sim_effect_receipts;
begin
 if authz.nexloop_app_role(p_world) not in ('nexloop_api','nexloop_domain_worker','nexloop_stand_in_worker') then raise exception 'stand-in: unavailable' using errcode='42501';end if;
 v_tenant:=authz.nexloop_sim_effect_tenant(p_digest,p_world);
 select * into r from runtime.nexloop_sim_effect_receipts where tenant_id=v_tenant and world=p_world and intent_id=p_intent;
 if not found then return null;end if;
 return authz.nexloop_sim_effect_receipt(r,false);
end $$;
