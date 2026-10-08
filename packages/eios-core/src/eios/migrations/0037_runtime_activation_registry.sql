-- API enrolls an actual issued Run before an independently leased Worker can
-- activate it. Opaque references never authenticate or grant another identity.
create table authz.nexloop_runtime_run_bindings (
 run_id uuid primary key references authz.nexloop_run_credentials(run_id),
 tenant_id text not null,world text not null,queue text not null,task_id text not null,
 run_digest text not null references authz.nexloop_run_credentials(token_digest),
 command_digest text not null check(command_digest ~ '^[a-f0-9]{64}$'),
 input_digest text not null check(input_digest ~ '^[a-f0-9]{64}$'),
 owner_epoch bigint not null check(owner_epoch>=1),enrolled_by text not null,
 created_at timestamptz not null,
 unique(tenant_id,world,task_id),
 foreign key(tenant_id,task_id) references runtime.jobs(tenant_id,job_id)
);
create table authz.nexloop_runtime_activations (
 activation_ref text primary key check(activation_ref ~ '^activation_[a-f0-9-]{36}$'),
 run_id uuid not null references authz.nexloop_runtime_run_bindings(run_id),
 tenant_id text not null,world text not null,queue text not null,task_id text not null,
 fence bigint not null check(fence>=1),lease_credential text not null,owner_epoch bigint not null check(owner_epoch>=1),
 command_digest text not null,input_digest text not null,created_at timestamptz not null,
 unique(tenant_id,world,task_id,fence)
);
alter table authz.nexloop_runtime_run_bindings owner to nexloop_owner;
alter table authz.nexloop_runtime_activations owner to nexloop_owner;
revoke all on authz.nexloop_runtime_run_bindings,authz.nexloop_runtime_activations from public,nexloop_api,nexloop_scheduler,nexloop_domain_worker,nexloop_action_worker;

-- An owned-task hint only. Complete queue and Run permission proofs remain
-- mandatory in the signed command; no Run identity or private digest is here.
create function authz.nexloop_runtime_activation_hint(p_digest text,p_world text,p_ref text)
 returns text language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare identity jsonb;q text;
begin
 if session_user not in ('nexloop_scheduler','nexloop_domain_worker') then raise exception 'activation denied' using errcode='42501';end if;
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
alter function authz.nexloop_runtime_activation_hint(text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_runtime_activation_hint(text,text,text) from public;
grant execute on function authz.nexloop_runtime_activation_hint(text,text,text) to nexloop_scheduler,nexloop_domain_worker;

create function authz.nexloop_runtime_activation_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on set timezone='UTC' as $$
declare c jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;k bytea;identity jsonb;queue_binding jsonb;run_identity jsonb;
 q text:=p->>'queue';verb text:=p->>'verb';target text;tenant text;command jsonb;cmd_digest text;input_hash text;owner bigint;
 j runtime.jobs%rowtype;r authz.nexloop_run_credentials%rowtype;b authz.nexloop_runtime_run_bindings%rowtype;
 a authz.nexloop_runtime_activations%rowtype;proof jsonb;seen text[]:=array[]::text[];ts timestamptz;
begin
 if session_user not in ('nexloop_api','nexloop_scheduler','nexloop_domain_worker') or octet_length(p_text)>1048576 or octet_length(p_payload)>1048576
 or q is null or q !~ '^[A-Za-z][A-Za-z0-9_-]{0,63}$'
 or (verb='register' and session_user<>'nexloop_api')
 or (verb in ('create','resolve','authorize') and session_user not in ('nexloop_scheduler','nexloop_domain_worker')) then
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
alter function authz.nexloop_runtime_activation_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_runtime_activation_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_runtime_activation_command(text,text,text,text,text) to nexloop_api,nexloop_scheduler,nexloop_domain_worker;
