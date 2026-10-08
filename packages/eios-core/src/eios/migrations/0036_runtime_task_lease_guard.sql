-- Runtime checks the durable task/Run scheduling lease; no Action execution grant.
create or replace function authz.nexloop_queue_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on set timezone='UTC' as $$
declare c jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;k bytea;identity jsonb;binding jsonb;
 tenant text;actor text;q text:=p->>'queue';verb text:=p->>'verb';target text;ts timestamptz;
 j runtime.jobs%rowtype;o runtime.nexloop_outbox%rowtype;i runtime.nexloop_inbox%rowtype;
 jid text;iid text;digest text;seconds integer;original_lease timestamptz;sequence_number integer;
begin
 if octet_length(p_text)>1048576 or octet_length(p_payload)>262144 or q is null or q !~ '^[A-Za-z][A-Za-z0-9_-]{0,63}$'
  or session_user not in ('nexloop_api','nexloop_scheduler','nexloop_domain_worker') then
  raise exception 'queue authority denied' using errcode='42501';end if;
 if (verb='accept' and session_user<>'nexloop_api') or (verb in ('outbox_claim','outbox_ack') and session_user<>'nexloop_scheduler')
  or (verb in ('claim','finish','renew','assert_lease') and session_user not in ('nexloop_scheduler','nexloop_domain_worker')) then
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
   update runtime.jobs set status='dead_lettered',updated_at=ts,lease_until=null where tenant_id=tenant and job_id=j.job_id;
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
alter function authz.nexloop_queue_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_queue_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_queue_command(text,text,text,text,text) to nexloop_api,nexloop_scheduler,nexloop_domain_worker;
