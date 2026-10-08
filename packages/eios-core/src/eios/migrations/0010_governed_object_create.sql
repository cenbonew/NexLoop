-- Generic protected Action instance adapter: no raw application DML.
alter table ontology.objects add column world text not null default 'real';
create function authz.nexloop_create_object_action(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;body jsonb:=p_payload::jsonb;k bytea;stored runtime.nexloop_action_claims%rowtype;
 permit jsonb:=a->'permit';v_claim jsonb:=permit->'claim';ref jsonb:=v_claim->'binding'->'action_reference';d jsonb;s jsonb;v_id text;v_outcome jsonb;
begin
 if octet_length(p_text)>1048576 or octet_length(p_payload)>262144 then raise exception 'instance command too large' using errcode='22023';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-object-create-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'protocol' is distinct from 'nexloop-object-create-v1'
  or a->>'resource_id' is distinct from 'eios:action:'||(ref->>'stable_name')||':'||(ref->>'version')
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds'
  or v_claim->'binding'->>'request_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'instance permit rejected' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 select definition into d from control.nexloop_action_definitions where tenant_id=a->>'tenant_id' and world=p_world and resource_id=a->>'resource_id' and active;
 if not found or d is distinct from a->'definition' or d->'governance'->>'approval_mode'<>'none'
  or d->'governance'->>'risk_level'<>'low' or jsonb_array_length(d->'governance'->'policy_refs')<>0
  or d->'capability_binding'->>'capability_name' not in ('consumer.create','ontology.object.create')
  or not (d->'governance'->'change_scope'->'target_systems' @> '["postgres"]'::jsonb)
  or jsonb_array_length(d->'governance'->'change_scope'->'properties')<>0
 then raise exception 'instance Action contract unavailable' using errcode='42501';end if;
 if not exists(select 1 from jsonb_array_elements(d->'governance'->'change_scope'->'object_types') r
  where r->>'tenant_id'=a->>'tenant_id' and r->>'stable_name'=body->>'type_name' and (r->>'version')::integer=(a->'schema'->>'version')::integer) then
  raise exception 'instance type outside Action scope' using errcode='42501';end if;
 select definition into s from ontology.object_type_versions where tenant_id=a->>'tenant_id' and type_name=body->>'type_name' and version=(a->'schema'->>'version')::integer;
 if not found or s is distinct from a->'schema' or s->>'only_edit_via_actions'<>'true' then
  raise exception 'instance schema stale' using errcode='42501';end if;
 select * into stored from runtime.nexloop_action_claims where tenant_id=a->>'tenant_id' and world=p_world
  and action_name=v_claim->'key'->>'action_stable_name' and intent_id=v_claim->'key'->>'idempotency_key' for update;
 if not found or stored.principal_id is distinct from a->>'principal_id' or stored.claim is distinct from v_claim
  or v_claim->>'state'<>'active' or (v_claim->>'lease_expires_at')::timestamptz<=clock_timestamp()
  or (permit->>'expires_at')::timestamptz<=clock_timestamp() then
  raise exception 'instance claim fenced' using errcode='40001';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 v_id:=encode(sha256(convert_to(jsonb_build_array(a->>'tenant_id',p_world,body->>'type_name',stored.intent_id)::text,'UTF8')),'hex');
 insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
  values(a->>'tenant_id',p_world,body->>'type_name',v_id,(s->>'version')::integer,body->'properties','nexloop-action',stored.intent_id,clock_timestamp(),clock_timestamp());
 v_outcome:=jsonb_build_object('outcome_id',v_id,'outcome_revision',1,'status','succeeded','outcome_digest',v_claim->'binding'->>'request_digest','finalized_at',clock_timestamp());
 update runtime.nexloop_action_claims set claim=stored.claim||jsonb_build_object('state','terminal','terminal_outcome',v_outcome)
  where tenant_id=stored.tenant_id and world=p_world and action_name=stored.action_name and intent_id=stored.intent_id;
 return jsonb_build_object('object_id',v_id,'type_name',body->>'type_name','world',p_world);
end $$;
alter function authz.nexloop_create_object_action(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_create_object_action(text,text,text,text,text) from public;
grant execute on function authz.nexloop_create_object_action(text,text,text,text,text) to nexloop_api,nexloop_domain_worker,nexloop_action_worker;
