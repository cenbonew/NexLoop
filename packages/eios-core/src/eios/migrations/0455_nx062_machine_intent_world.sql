-- Bind intent recovery to the authenticated world while allowing same-world key rotation.

create or replace function authz.nexloop_machine_prepare(p_prefix text,p_digest text,p_capability text,p_intent text,p_params jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;v_tenant text;v_client text;v_world text;w control.nexloop_machine_write_capabilities;v_types text[];v_params jsonb;
 v_pd text;v_exp timestamptz;i runtime.nexloop_machine_intents;cap control.nexloop_machine_capabilities;
begin
 ident:=authz.nexloop_machine_identity_snapshot(p_prefix,p_digest);
 v_tenant:=ident->'binding'->>'tenant_id';v_client:=ident->>'client_id';v_world:=ident->>'world';
 if coalesce(p_intent,'')!~'^[A-Za-z0-9._:-]{8,128}$' or coalesce(p_capability,'')!~'^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$'
  or jsonb_typeof(p_params) is distinct from 'object' then raise exception 'machine write: invalid' using errcode='22023';end if;
 -- 2.2-2.4: the key's scope, the allowlist (a current write), the client's grant.
 if not (p_capability=any(array(select jsonb_array_elements_text(ident->'scope')))) then raise exception 'machine write: not_permitted' using errcode='42501';end if;
 select * into cap from control.nexloop_machine_capabilities where capability=p_capability order by revision desc limit 1;
 select * into w from control.nexloop_machine_write_capabilities where capability=p_capability;
 v_types:=authz.nexloop_machine_grant_types(v_tenant,v_client,p_capability);
 if cap.capability is null or cap.state<>'active' or cap.kind<>'write' or w.capability is null or v_types is null
  or control.nexloop_machine_capability_forbidden(p_capability) then raise exception 'machine write: not_permitted' using errcode='42501';end if;
 -- 2.5: an effect capability needs an approval policy.
 if cap.effect and (control.nexloop_approval_policy(p_capability)).capability is null then
  raise exception 'machine write: approval_policy_required' using errcode='42501';end if;
 -- The parameters: only the registered keys; a type the client holds; a visible object.
 if exists(select 1 from jsonb_object_keys(p_params) x where not (x=any(w.param_keys))) then raise exception 'machine write: invalid' using errcode='22023';end if;
 if p_params ? 'type_name' and (jsonb_typeof(p_params->'type_name')<>'string' or not (p_params->>'type_name'=any(v_types))
   or not (p_params->>'type_name'=any(w.object_types))) then raise exception 'machine write: not_found' using errcode='P0002';end if;
 if p_params ? 'object_id' and (not (p_params ? 'type_name') or coalesce(p_params->>'object_id','')!~'^[a-f0-9]{64}$'
   or not authz.nexloop_machine_object_visible(v_tenant,v_world,p_params->>'type_name',p_params->>'object_id')) then
  raise exception 'machine write: not_found' using errcode='P0002';end if;
 v_params:=runtime.nexloop_canonical_json(p_params)::jsonb;
 perform 1 from authz.nexloop_machine_clients c where c.tenant_id=v_tenant and c.client_id=v_client for update;  -- one prepare at a time
 select * into i from runtime.nexloop_machine_intents where tenant_id=v_tenant and client_id=v_client and world=v_world and intent_id=p_intent for update;
 if found then
  if i.phase<>'prepare' then raise exception 'machine write: intent_committed' using errcode='NX409';end if;
  if i.expires_at>clock_timestamp() then
   if i.capability=p_capability and i.world=v_world and i.params_digest=runtime.nexloop_machine_params_digest(v_params) then
    return jsonb_build_object('intent_id',i.intent_id,'prepare_digest',i.prepare_digest,'expires_at',to_jsonb(i.expires_at),'normalized',v_params,'replayed',true);
   end if;
   raise exception 'machine write: intent_conflict' using errcode='NX409';
  end if;
 end if;
 v_exp:=date_trunc('milliseconds',clock_timestamp())+interval '10 minutes';
 v_pd:=runtime.nexloop_machine_prepare_digest(v_tenant,v_client,v_world,p_capability,p_intent,v_params,v_exp);
 insert into runtime.nexloop_machine_intents(tenant_id,client_id,intent_id,capability,world,params_digest,prepare_digest,expires_at,phase,key_prefix)
  values(v_tenant,v_client,p_intent,p_capability,v_world,runtime.nexloop_machine_params_digest(v_params),v_pd,v_exp,'prepare',ident->>'key_prefix')
  on conflict(tenant_id,client_id,intent_id) do update set capability=excluded.capability,params_digest=excluded.params_digest,
   prepare_digest=excluded.prepare_digest,expires_at=excluded.expires_at,key_prefix=excluded.key_prefix
  where runtime.nexloop_machine_intents.world=excluded.world
  returning * into i;
 if not found then raise exception 'machine write: intent_conflict' using errcode='NX409';end if;
 perform runtime.nexloop_machine_intent_audit(i,ident->'binding'->>'subject_principal_id','prepare');
 return jsonb_build_object('intent_id',i.intent_id,'prepare_digest',i.prepare_digest,'expires_at',to_jsonb(i.expires_at),'normalized',v_params,'replayed',false);
end $$;

create or replace function authz.nexloop_machine_commit(p_prefix text,p_digest text,p_capability text,p_intent text,p_params jsonb,p_prepare_digest text,
 p_text text,p_signature text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;v_tenant text;v_client text;i runtime.nexloop_machine_intents;cap control.nexloop_governed_capabilities;v_payload text;v jsonb;
begin
 ident:=authz.nexloop_machine_identity_snapshot(p_prefix,p_digest);
 v_tenant:=ident->'binding'->>'tenant_id';v_client:=ident->>'client_id';
 if coalesce(p_intent,'')!~'^[A-Za-z0-9._:-]{8,128}$' or coalesce(p_prepare_digest,'')!~'^[0-9a-f]{64}$' or jsonb_typeof(p_params) is distinct from 'object' then
  raise exception 'machine write: invalid' using errcode='22023';end if;
 select * into i from runtime.nexloop_machine_intents where tenant_id=v_tenant and client_id=v_client and world=ident->>'world' and intent_id=p_intent for update;
 if not found then raise exception 'machine write: intent_not_found' using errcode='P0002';end if;
 -- R4: a finished intent answers first (its first result for the same digest), before any expiry.
 if i.phase in ('committed','refused') then
  if i.prepare_digest=p_prepare_digest and i.capability=p_capability
   and i.params_digest=runtime.nexloop_machine_params_digest(runtime.nexloop_canonical_json(p_params)::jsonb) then
   return runtime.nexloop_machine_intent_view(i)||jsonb_build_object('replayed',true);end if;
  raise exception 'machine write: digest_mismatch' using errcode='NX409';
 end if;
 if i.capability is distinct from p_capability or i.prepare_digest is distinct from p_prepare_digest then
  raise exception 'machine write: digest_mismatch' using errcode='NX409';end if;
 if i.phase='prepare' and i.expires_at<=clock_timestamp() then raise exception 'machine write: prepare_expired' using errcode='NX410';end if;
 select * into cap from control.nexloop_governed_capabilities where capability=p_capability;
 if cap.capability is null then raise exception 'machine write: not_permitted' using errcode='42501';end if;
 v_payload:=runtime.nexloop_canonical_json(p_params||jsonb_build_object('operation',cap.operation,
  'request_id',runtime.nexloop_machine_business_intent(v_tenant,v_client,p_intent)));
 begin
  v:=authz.nexloop_goal_governed_action('nxm:'||p_prefix||':'||p_digest,ident->>'world',p_text,p_signature,v_payload);
 exception when insufficient_privilege then
  -- L6 Y1: only a refusal by authority now ends the intent - the machine authority assertion denying (revoked, out of scope, retired,
  -- inactive) or a human capability. An expired or rotated signature, a changed Action definition or facts read before a change are
  -- no decision: re-raised, the intent keeps its phase and the same intent may be committed again with a new command.
  if sqlerrm not in ('machine action authority denied','human goal authority required') then raise;end if;
  perform set_config('eios.tenant_id',v_tenant,true);
  update runtime.nexloop_machine_intents set phase='refused',result_code='not_permitted',committed_at=clock_timestamp()
   where tenant_id=i.tenant_id and client_id=i.client_id and intent_id=i.intent_id returning * into i;
  perform runtime.nexloop_machine_intent_audit(i,ident->'binding'->>'subject_principal_id','refused');
  return runtime.nexloop_machine_intent_view(i)||jsonb_build_object('replayed',false);
 end;
 return v;
end $$;

create or replace function authz.nexloop_machine_intent(p_prefix text,p_digest text,p_intent text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;i runtime.nexloop_machine_intents;v_status text;
begin
 ident:=authz.nexloop_machine_identity_snapshot(p_prefix,p_digest);
 select * into i from runtime.nexloop_machine_intents where tenant_id=ident->'binding'->>'tenant_id' and client_id=ident->>'client_id' and world=ident->>'world' and intent_id=p_intent;
 if not found then raise exception 'machine write: intent_not_found' using errcode='P0002';end if;
 if i.approval_id is not null then
  perform set_config('eios.tenant_id',i.tenant_id,true);
  select q.status into v_status from control.nexloop_approval_requests q where q.tenant_id=i.tenant_id and q.world=i.world and q.approval_id=i.approval_id;
 end if;
 return runtime.nexloop_machine_intent_view(i)||jsonb_build_object('approval_status',v_status);
end $$;
