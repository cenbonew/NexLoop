-- Signature denial causes (L6; stage 2026-10-20: "queue signature denied" stood for six different failures). The migration number
-- lives only in the file name and the catalog; nothing below repeats it.
-- The signed-command gates keep their errcode (42501) and their message text - clients still decide on the message - and now give
-- the failed condition as DETAIL, one of a fixed set: key_unknown, protocol, resource, expired, too_far_ahead, digest, signature
-- (other: a caller, world or size condition of the same gate). For too_far_ahead the HINT says by how much the proof's expires_at
-- exceeds the bound, rounded to 0.1 s (ahead_by_seconds=N.N); no other value, secret or parameter is ever put in DETAIL or HINT.
-- Gates covered: the durable queue (0209 inner, as copied by 0268), the runtime activation (0037 inner, as copied by 0268), the effect
-- execution (0044, named _v0052 since 0053) and the context v6 entry (latest body 0227, both its gates). Each body is otherwise unchanged
-- (registered in tests/test_function_copy_guard.py). The other gates with one message for several signature conditions are listed in
-- docs/implementation/signature-denial-causes.md.

-- The cause of a refused signed command: the first failing condition of those this gate checks (an argument given as NULL is a
-- condition this gate does not check). Pure classification of values the gate already holds; nothing is read.
create function authz.nexloop_signed_denial_cause(c jsonb,p_protocol text,p_resource text,p_payload text,p_window interval,p_key boolean,p_mac boolean)
 returns text language plpgsql stable set search_path=pg_catalog,pg_temp as $$
declare v_expires timestamptz;
begin
 if p_key is false then return 'key_unknown';end if;
 if p_protocol is not null and c->>'protocol' is distinct from p_protocol then return 'protocol';end if;
 if p_resource is not null and c->>'resource_id' is distinct from p_resource then return 'resource';end if;
 if p_window is not null then
  begin v_expires:=(c->>'expires_at')::timestamptz;exception when others then return 'expired';end;
  if v_expires is null or v_expires<=clock_timestamp() then return 'expired';end if;
  if v_expires>clock_timestamp()+p_window then return 'too_far_ahead';end if;
 end if;
 if p_payload is not null and c->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then return 'digest';end if;
 if p_mac is false then return 'signature';end if;
 return 'other';
end $$;
-- For too_far_ahead only: by how much expires_at exceeds the bound, in seconds rounded to 0.1 (else an empty hint).
create function authz.nexloop_signed_ahead_hint(c jsonb,p_window interval) returns text language plpgsql stable set search_path=pg_catalog,pg_temp as $$
declare v_expires timestamptz;
begin
 begin v_expires:=(c->>'expires_at')::timestamptz;exception when others then return '';end;
 if v_expires is null or v_expires<=clock_timestamp()+p_window then return '';end if;
 return 'ahead_by_seconds='||to_char(round(extract(epoch from v_expires-(clock_timestamp()+p_window))::numeric,1),'FM999999990.0');
end $$;
alter function authz.nexloop_signed_denial_cause(jsonb,text,text,text,interval,boolean,boolean) owner to nexloop_owner;
alter function authz.nexloop_signed_ahead_hint(jsonb,interval) owner to nexloop_owner;
revoke all on function authz.nexloop_signed_denial_cause(jsonb,text,text,text,interval,boolean,boolean) from public;
revoke all on function authz.nexloop_signed_ahead_hint(jsonb,interval) from public;

-- queue (0209 body via 0268): the denial raise carries its cause; the body is otherwise unchanged.
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
  raise exception 'queue signature denied' using errcode='42501',detail=authz.nexloop_signed_denial_cause(c,'nexloop-queue-command-v1',target,p_payload,interval '25 seconds',k is not null,p_signature is not distinct from encode(extensions.hmac(convert_to('nexloop-queue-command-v1:'||p_text,'UTF8'),k,'sha256'),'hex')),hint=authz.nexloop_signed_ahead_hint(c,interval '25 seconds');end if;
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

-- activation (0037 body via 0268): the denial raise carries its cause; the body is otherwise unchanged.
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
  raise exception 'activation denied' using errcode='42501',detail=authz.nexloop_signed_denial_cause(c,'nexloop-runtime-activation-v1',target,p_payload,interval '25 seconds',k is not null,p_signature is not distinct from encode(extensions.hmac(convert_to('nexloop-runtime-activation-v1:'||p_text,'UTF8'),k,'sha256'),'hex')),hint=authz.nexloop_signed_ahead_hint(c,interval '25 seconds');end if;
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

-- effect execution (0044 body, now named _v0052): the denial raise carries its cause; search_path gains pg_temp last (as every redefined
-- security definer function); the body is otherwise unchanged.
create or replace function authz.nexloop_effect_execution_command_v0052(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare c jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;k bytea;v text:=p->>'verb';identity jsonb;
 tenant text;profile text;bound text;result jsonb;i runtime.nexloop_effect_intents;
 old_lease timestamptz;old_action_lease timestamptz;r authz.nexloop_run_credentials;technical_read boolean;
begin
 if session_user<>'nexloop_action_worker' or p_text is null or p_payload is null
  or octet_length(p_text)>1048576 or octet_length(p_payload)>524288
  or jsonb_typeof(c) is distinct from 'object' or jsonb_typeof(p) is distinct from 'object'
 then raise exception 'effect provider authority unavailable' using errcode='42501';end if;
 select sk.key_material into k from authz.nexloop_authority_signing_keys sk where sk.key_id=c->>'key_id' and sk.active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-effect-execution-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or c->>'protocol' is distinct from 'nexloop-effect-execution-v1'
  or c->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or c->>'expires_at' is null or (c->>'expires_at')::timestamptz<=clock_timestamp()
  or (c->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds'
 then raise exception 'effect provider authority unavailable' using errcode='42501',detail=authz.nexloop_signed_denial_cause(c,'nexloop-effect-execution-v1',null,p_payload,interval '25 seconds',k is not null,p_signature is not distinct from encode(extensions.hmac(convert_to('nexloop-effect-execution-v1:'||p_text,'UTF8'),k,'sha256'),'hex')),hint=authz.nexloop_signed_ahead_hint(c,interval '25 seconds');end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);tenant:=identity->'binding'->>'tenant_id';
 if v in ('admit','query','observe','finalize') then
  technical_read:=v='query' and p->'terminal_only'='true'::jsonb;
  profile:=p->>'provider_profile_digest';
  if not coalesce(technical_read,false) and (jsonb_typeof(p->'provider_profile_digest') is distinct from 'string' or profile !~ '^[a-f0-9]{64}$') then
   raise exception 'effect provider profile unavailable' using errcode='42501';end if;
  select ei.* into i from runtime.nexloop_effect_intents ei where ei.intent_id=(p->>'intent_id')::uuid and ei.tenant_id=tenant and ei.world=p_world;
  if not found then raise exception 'effect provider profile unavailable' using errcode='42501';end if;
  select pb.provider_profile_digest into bound from runtime.nexloop_effect_provider_bindings pb
   where pb.intent_id=i.intent_id and pb.tenant_id=tenant and pb.world=p_world;
  if v<>'admit' and (not found or not coalesce(technical_read,false) and bound is distinct from profile) or v='admit' and bound is not null then
   raise exception 'effect provider profile unavailable' using errcode='42501';end if;
  -- Read without new locks. The original command acquires and verifies the
  -- authoritative intent/outbox/attempt locks and ownership in its old order.
  select eo.lease_until into old_lease from runtime.nexloop_effect_outbox eo where eo.intent_id=i.intent_id and eo.tenant_id=tenant and eo.world=p_world;
  if v in ('admit','finalize') then
   select (ac.claim->>'lease_expires_at')::timestamptz into old_action_lease from runtime.nexloop_action_claims ac
    where ac.intent_id=i.intent_id::text and ac.tenant_id=tenant and ac.world=p_world and ac.action_name=i.action_name;
  end if;
 end if;
 result:=authz.nexloop_effect_execution_command_v0042(p_digest,p_world,p_text,p_signature,p_payload);
 if v='admit' then
  insert into runtime.nexloop_effect_provider_bindings values(i.intent_id,tenant,p_world,profile,clock_timestamp());
 end if;
 if v in ('admit','query','observe','finalize') then
  select pb.provider_profile_digest into bound from runtime.nexloop_effect_provider_bindings pb
   where pb.intent_id=i.intent_id and pb.tenant_id=tenant and pb.world=p_world;
  if not found or not coalesce(technical_read,false) and bound is distinct from profile then raise exception 'effect provider profile unavailable' using errcode='42501';end if;
  if not coalesce(technical_read,false) and (old_lease is null or old_lease<=clock_timestamp()) then
   raise exception 'effect provider lease expired' using errcode='42501';end if;
  if v in ('admit','finalize') then
   select rc.* into r from authz.nexloop_run_credentials rc where rc.run_id=i.origin_run_id;
   if not found or r.expires_at<=clock_timestamp() then raise exception 'effect provider source expired' using errcode='42501';end if;
   perform authz.nexloop_assert_action_authority(r.token_digest,p_world,p->'origin_proof');
   perform authz.nexloop_assert_effect_plan(i.context_id,tenant,p_world);
   -- finalize reserve may reacquire an expired Action lease. Its original
   -- function already checks the newly reserved lease; re-read its current row.
   select (ac.claim->>'lease_expires_at')::timestamptz into old_action_lease from runtime.nexloop_action_claims ac
    where ac.intent_id=i.intent_id::text and ac.tenant_id=tenant and ac.world=p_world and ac.action_name=i.action_name;
   if old_action_lease is null or old_action_lease<=clock_timestamp() then raise exception 'effect provider Action lease expired' using errcode='42501';end if;
  end if;
 end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 if (c->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'effect provider proof expired' using errcode='42501';end if;
 if v in ('admit','query','observe','finalize') and not coalesce(technical_read,false)
  and (old_lease is null or old_lease<=clock_timestamp()) then
  raise exception 'effect provider lease expired' using errcode='42501';end if;
 if v in ('admit','finalize') and (old_action_lease is null or old_action_lease<=clock_timestamp()
  or r.expires_at<=clock_timestamp() or (p->'origin_proof'->>'expires_at')::timestamptz<=clock_timestamp()) then
  raise exception 'effect provider final permit expired' using errcode='42501';end if;
 return result;
end $$;

-- context v6 (latest body, 0227 operator persona; both gates): the denial raises carry their cause; the body is otherwise unchanged.
CREATE OR REPLACE FUNCTION authz.nexloop_context_v6_command(p_digest text, p_world text, p_text text, p_signature text, p_payload text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare a jsonb:=p_text::jsonb;p jsonb;k bytea;ident jsonb;tenant text;principal text;ca jsonb;cp jsonb;snapshot jsonb;pack jsonb;
 b runtime.nexloop_context_artifact_bindings;artifact runtime.nexloop_local_artifacts;cl runtime.nexloop_action_claims;
 conv runtime.nexloop_conversations;consumer text;proof jsonb;key text;fresh boolean;namespace text;context_uuid uuid;run uuid;rel boolean;core jsonb;opersona jsonb;
begin
 if session_user<>'nexloop_api' or p_world is distinct from 'real' or octet_length(p_text)>1048576 or octet_length(p_payload)>1048576
  or a->>'protocol' is distinct from 'nexloop-context-v6-v1' or a->>'resource_id' is distinct from 'eios:action:nexloop.context.assemble:1'
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds' then
  raise exception 'context v6 unavailable' using errcode='42501',detail=authz.nexloop_signed_denial_cause(a,'nexloop-context-v6-v1','eios:action:nexloop.context.assemble:1',p_payload,interval '25 seconds',null,null),hint=authz.nexloop_signed_ahead_hint(a,interval '25 seconds');end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-context-v6-v1:'||p_text,'UTF8'),k,'sha256'),'hex') then
  raise exception 'context v6 unavailable' using errcode='42501',detail=authz.nexloop_signed_denial_cause(a,null,null,null,null,k is not null,p_signature is not distinct from encode(extensions.hmac(convert_to('nexloop-context-v6-v1:'||p_text,'UTF8'),k,'sha256'),'hex')),hint='';end if;
 perform authz.nexloop_assert_context_assemble(p_digest,p_world,a,p_payload);
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ident->'run_context' is distinct from 'null'::jsonb or ident->'binding'->>'subject_kind' is distinct from 'service' then raise exception 'context v6 unavailable' using errcode='42501';end if;
 tenant:=ident->'binding'->>'tenant_id';principal:=ident->'binding'->>'subject_principal_id';perform set_config('eios.tenant_id',tenant,true);
 p:=p_payload::jsonb;
 if p->>'verb' is distinct from 'bind' or jsonb_typeof(p->'core') is distinct from 'object' or jsonb_typeof(p->'omitted_items') is distinct from 'array'
  or jsonb_array_length(p->'omitted_items')>1024 or p->>'pack_text' is null or octet_length(p->>'pack_text')>65536 then raise exception 'context v6 unavailable' using errcode='42501';end if;
 ca:=(p->'core'->>'text')::jsonb;cp:=(p->'core'->>'payload')::jsonb;
 if cp->>'verb' is distinct from 'snapshot' then raise exception 'context v6 unavailable' using errcode='42501';end if;
 pack:=(p->>'pack_text')::jsonb;rel:=pack ? 'relationship_context';
 -- Core: the frozen v2 chain (or the v4 chain when the pack carries relationship assessments)
 -- re-derives and authorizes its sections under its own signed envelope.
 if rel then snapshot:=authz.nexloop_relationship_context_artifact_command(p_digest,p_world,p->'core'->>'text',p->'core'->>'signature',p->'core'->>'payload');
 else snapshot:=authz.nexloop_context_artifact_command(p_digest,p_world,p->'core'->>'text',p->'core'->>'signature',p->'core'->>'payload');end if;
 if p->>'pack_digest' is distinct from encode(sha256(convert_to(p->>'pack_text','UTF8')),'hex') or runtime.nexloop_canonical_json(pack) is distinct from p->>'pack_text'
  or pack->>'schema_version' is distinct from 'nexloop.context-pack.v6' or (select count(*) from jsonb_object_keys(pack))<>(case when rel then 19 else 18 end)+(case when pack ? 'effect_supplies' then 1 else 0 end)+(case when pack ? 'operator_persona' then 1 else 0 end)
  or not pack ?& array['strategy_ref','bindings','role','current_event','user_statement','goal','formal_facts','current_constraints','supply','constraints','consumer_state',
   'open_work','evidence','semantics','experience','budget_report','insufficient'] then raise exception 'context v6 pack invalid' using errcode='42501';end if;
 foreach key in array array['bindings','user_statement','formal_facts','current_constraints','supply','effect_supplies']||case when rel then array['relationship_context'] else array[]::text[] end loop
  if pack->key is distinct from snapshot->key then raise exception 'context v6 core mismatch' using errcode='42501';end if;
 end loop;
 if pack->'role' is distinct from 'null'::jsonb then raise exception 'context v6 Role section unsupported' using errcode='42501';end if;
 if pack->'current_event' is distinct from jsonb_build_object('kind','consumer_message','message_id',pack->'user_statement'->'message_id','provenance',pack->'user_statement'->'provenance') then
  raise exception 'context v6 current event mismatch' using errcode='42501';end if;
 context_uuid:=(pack->'bindings'->>'context_id')::uuid;run:=(pack->'bindings'->>'run_id')::uuid;
 select * into conv from runtime.nexloop_conversations where tenant_id=tenant and world=p_world and conversation_id=pack->'user_statement'->>'conversation_id' for share;
 select value->>'id' into consumer from jsonb_array_elements(pack->'formal_facts') where value->>'type'='Consumer';
 if not found or conv.consumer_id is distinct from consumer then raise exception 'context v6 consumer mismatch' using errcode='42501';end if;
 -- A bound pack replays unchanged: it was fully verified when bound (its read proofs have
 -- since expired); only the Artifact, the bind claim and the core above are rechecked.
 select * into b from runtime.nexloop_context_artifact_bindings where tenant_id=tenant and world=p_world and message_id=pack->'user_statement'->>'message_id' for update;
 fresh:=not found;
 if not fresh and (b.pack_text is distinct from p->>'pack_text' or b.run_id is distinct from run) then raise exception 'context artifact conflict' using errcode='23505';end if;
 if fresh then
  -- Core sources: what the frozen chain re-derived, cited under the envelope it verified;
  -- v4 relationship rows keep their v4 meaning (hypotheses are evidence only).
  core:=jsonb_build_array(
    jsonb_build_object('section','current_event','ref','eios:object:Message/'||(pack->'user_statement'->>'message_id'),'revision',pack->'user_statement'->>'sequence',
     'hash',runtime.nexloop_context_hash(pack->'user_statement'),'kind','current_message','decision',runtime.nexloop_context_hash(coalesce(ca->'message_read_envelope',ca->'trigger_message_envelope'))),
    jsonb_build_object('section','constraints','ref','eios:action:nexloop.service.request:1','revision',runtime.nexloop_context_hash(pack->'current_constraints'),
     'hash',runtime.nexloop_context_hash(pack->'current_constraints'),'kind','policy','decision',runtime.nexloop_context_hash(ca)),
    jsonb_build_object('section','constraints','ref','eios:object:'||(pack->'supply'->>'offering_id'),'revision',pack->'supply'->>'offering_revision',
     'hash',runtime.nexloop_context_hash(pack->'supply'),'kind','formal_object','decision',runtime.nexloop_context_hash(ca->'catalog_envelope')))
   ||coalesce((select jsonb_agg(jsonb_build_object('section','constraints','ref','eios:object:'||(f->>'type')||'/'||(f->>'id'),'revision',f->>'revision',
     'hash',runtime.nexloop_context_hash(f),'kind','formal_object','decision',runtime.nexloop_context_hash(ca))) from jsonb_array_elements(pack->'formal_facts') f),'[]'::jsonb)
   ||coalesce((select jsonb_agg(jsonb_build_object('section','constraints','ref','eios:object:'||(x->>'offering_id'),'revision',x->>'offering_revision',
     'hash',runtime.nexloop_context_hash(x),'kind','formal_object','decision',runtime.nexloop_context_hash(ca))) from jsonb_array_elements(coalesce(pack->'effect_supplies','[]'::jsonb)) x),'[]'::jsonb)
   ||case when rel then coalesce((select jsonb_agg(jsonb_build_object('section','evidence','ref',x->>'assessment_ref','revision',x->>'revision','hash',runtime.nexloop_context_hash(x),
     'kind',x->>'epistemic_kind','decision',runtime.nexloop_context_hash(ca->'relationship_envelopes')))
     from jsonb_array_elements((pack->'relationship_context'->'current_statements')||(pack->'relationship_context'->'evidence')) x),'[]'::jsonb) else '[]'::jsonb end;
  -- 0227: the operator persona section is exactly the re-derived one, present only when the tenant has a usable persona for this
  -- Consumer; it is cited as a role source (persona@version) and the Consumer's assignment is settled (an event on any change).
  opersona:=runtime.nexloop_operator_persona_section(tenant,p_world,consumer);
  if (pack ? 'operator_persona') is distinct from (opersona is not null) or (opersona is not null and pack->'operator_persona' is distinct from opersona) then
   raise exception 'context v6 operator persona mismatch' using errcode='42501';end if;
  if opersona is not null then
   core:=core||jsonb_build_array(jsonb_build_object('section','role','ref',opersona->>'persona_ref','revision',opersona->>'version',
    'hash',runtime.nexloop_context_hash(opersona),'kind','policy','decision',runtime.nexloop_context_hash(ca)));
   perform runtime.nexloop_operator_persona_settle(tenant,p_world,consumer,principal,'context:'||context_uuid::text,null);
  end if;
  perform authz.nexloop_context_v6_sections(p_digest,p_world,tenant,principal,a,p,pack,array[conv.conversation_id],consumer,run,context_uuid,core);
 end if;
 -- Artifact and governed bind claim: same rules as the frozen bind, against the v6 bytes.
 namespace:=encode(sha256(convert_to(tenant,'UTF8')||decode('00','hex')||convert_to(p_world,'UTF8')),'hex');
 select * into artifact from runtime.nexloop_local_artifacts where tenant_id=tenant and world=p_world and artifact_id=p->>'artifact_id' for share;
 if not found or artifact.artifact_id is distinct from pack->'bindings'->>'artifact_id' or artifact.status is distinct from 'available' or artifact.principal_id is distinct from principal
  or artifact.sha256 is distinct from p->>'pack_digest' or artifact.size_bytes is distinct from octet_length(p->>'pack_text')
  or artifact.media_type is distinct from 'application/vnd.nexloop.context+json' or artifact.retention_until<=clock_timestamp()
  or artifact.object_key is distinct from namespace||'/'||artifact.artifact_id then raise exception 'context artifact unavailable' using errcode='42501';end if;
 select * into cl from runtime.nexloop_action_claims where tenant_id=tenant and world=p_world and action_name='nexloop.context.bind' and intent_id=p->>'claim_id' for share;
 if not found or cl.principal_id is distinct from principal or cl.claim->'binding' is distinct from ca->'claim_binding' or cl.claim->>'state' not in ('active','terminal')
  or (cl.claim->>'state'='active' and (cl.claim->>'lease_expires_at')::timestamptz<=clock_timestamp())
  or (cl.claim->>'state'='terminal' and (fresh or cl.claim->'terminal_outcome'->>'status' is distinct from 'succeeded' or cl.claim->'terminal_outcome'->>'outcome_digest' is distinct from artifact.sha256)) then
  raise exception 'context artifact unavailable' using errcode='42501';end if;
 if fresh then
  insert into runtime.nexloop_context_artifact_bindings values(tenant,p_world,pack->'user_statement'->>'message_id',run,principal,p_digest,context_uuid,artifact.artifact_id,namespace,
   p->>'pack_text',p->>'pack_digest',authz.nexloop_context_command_binding(cp->'command'),clock_timestamp());
 elsif not exists(select 1 from runtime.nexloop_context_packs c where c.tenant_id=tenant and c.world=p_world and c.context_id=context_uuid and c.run_id=run and c.pack_digest=p->>'pack_digest') then
  raise exception 'context artifact conflict' using errcode='23505';
 end if;
 -- Tail: every authority used above is still current at return.
 for proof in select value from jsonb_array_elements(a->'read_proofs') loop
  if (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'context read proof expired' using errcode='42501';end if;
  perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
 end loop;
 perform authz.nexloop_assert_context_assemble(p_digest,p_world,a,p_payload);
 return jsonb_build_object('context_id',context_uuid,'run_id',run,'pack_digest',p->>'pack_digest','strategy_ref',pack->>'strategy_ref','bound',true);
end $function$;
