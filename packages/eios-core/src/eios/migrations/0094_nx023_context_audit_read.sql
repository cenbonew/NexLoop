-- NX-023-C: governed human read of a model-call Manifest (owner decision 2026-10-09).
-- nexloop.context.audit:1 is granted to humans only (owner / audit role) through trusted
-- configuration; SQL refuses every service principal, Agent and Run credential before it
-- even evaluates a grant. The owner-only projection runtime.nexloop_context_manifest is
-- unchanged; this definer only gates who may receive it.
create function authz.nexloop_context_manifest_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;ident jsonb;v_tenant text;d jsonb;v_result jsonb;
begin
 if session_user<>'nexloop_api' or p_world is distinct from 'real' or octet_length(p_text)>1048576 or octet_length(p_payload)>1024
  or a->>'protocol' is distinct from 'nexloop-context-audit-v1' or a->>'resource_id' is distinct from 'eios:action:nexloop.context.audit:1'
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds'
  or (select array_agg(x order by x) from jsonb_object_keys(c) x) is distinct from array['call_sequence','run_id']
  or c->>'run_id'!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' or jsonb_typeof(c->'call_sequence') is distinct from 'number'
  or c->>'call_sequence'!~'^[1-9][0-9]{0,4}$' then raise exception 'context audit unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-context-audit-v1:'||p_text,'UTF8'),k,'sha256'),'hex') then
  raise exception 'context audit unavailable' using errcode='42501';end if;
 -- Identity first: no service, Agent or Run credential reaches the grant check at all.
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ident->'run_context' is distinct from 'null'::jsonb or ident->'binding'->>'subject_kind' is distinct from 'human' then
  raise exception 'human context audit authority required' using errcode='42501';end if;
 v_tenant:=ident->'binding'->>'tenant_id';
 if a->>'tenant_id' is distinct from v_tenant or a->>'principal_id' is distinct from ident->'binding'->>'subject_principal_id' then
  raise exception 'context audit identity binding mismatch' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 select definition into d from control.nexloop_action_definitions where tenant_id=v_tenant and world=p_world and resource_id=a->>'resource_id' and active;
 if d is null or d is distinct from a->'definition' or d->'capability_binding'->>'capability_name' is distinct from 'context.audit'
  or d->'governance'->>'approval_mode' is distinct from 'none' or jsonb_array_length(d->'governance'->'policy_refs')<>0 then
  raise exception 'context audit Action contract unavailable' using errcode='42501';end if;
 -- Tenant RLS bounds the projection to the caller's own tenant.
 perform set_config('eios.tenant_id',v_tenant,true);
 v_result:=runtime.nexloop_context_manifest((c->>'run_id')::uuid,(c->>'call_sequence')::integer);
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 if (a->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'context audit expired' using errcode='42501';end if;
 return v_result;
end $$;
alter function authz.nexloop_context_manifest_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_context_manifest_read(text,text,text,text,text) from public,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;
grant execute on function authz.nexloop_context_manifest_read(text,text,text,text,text) to nexloop_api;

-- Manifest determinism fix: ontology_schema_revision is the schema as of the request, not as of
-- the read (a later type publication must not change an earlier call's Manifest). Still owner-only.
create or replace function runtime.nexloop_context_manifest(p_run uuid,p_call integer) returns jsonb language sql stable set search_path=pg_catalog as $$
 select jsonb_build_object('schema_version','1.0','context_id',r.context_id,'tenant_id',r.tenant_id,'world_id',r.world,'mode','real','run_id',r.run_id,
  'call_sequence',r.call_sequence,'goal_version_ref',b.pack_text::jsonb->'goal'->'goal_version_refs'->>0,
  'policy_revision','sha256:'||runtime.nexloop_context_hash(b.pack_text::jsonb->'current_constraints'),
  'ontology_schema_revision','sha256:'||runtime.nexloop_context_hash(coalesce((select jsonb_object_agg(v.type_name,v.version) from (select type_name,max(version) version
    from ontology.object_type_versions where tenant_id=r.tenant_id and created_at<=r.requested_at group by type_name) v),'{}'::jsonb)),
  'semantic_snapshot_ref','semantic:none','context_strategy_version',c.strategy_ref,'model_provider',r.model_provider,'model_id',r.model_id,'embedding_profile_ref',null,
  'sources',(select jsonb_agg(jsonb_build_object('ref',s.ref,'revision',s.revision,'content_hash',s.content_hash,'evidence_kind',s.evidence_kind,'access_decision_ref',s.access_decision_ref) order by s.ordinal)
    from runtime.nexloop_context_sources s where s.tenant_id=r.tenant_id and s.world=r.world and s.context_id=r.context_id and s.status='included'),
  'prompt_artifact_ref','prompt:'||r.prompt_artifact_id,'request_digest',r.request_digest,'input_token_budget',r.input_token_budget,'output_token_budget',r.output_token_budget,
  'redaction_policy_ref','redaction:none-v1','created_at',to_char(r.requested_at at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))
 from runtime.nexloop_model_requests r join runtime.nexloop_context_packs c on c.tenant_id=r.tenant_id and c.world=r.world and c.context_id=r.context_id
  cross join lateral runtime.nexloop_context_v6_binding(r.tenant_id,r.world,r.run_id) b
 where r.run_id=p_run and r.call_sequence=p_call $$;
