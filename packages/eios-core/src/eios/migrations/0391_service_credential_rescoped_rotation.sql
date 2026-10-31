-- S79 (dispatcher 2026-11-01; temporary number 0391): a rotation may change a service credential's requested scopes, and a superseded
-- token can be closed without the rotation's files. R65: service-grants v24 gave catalog_maintainer two object_type READ grants, so
-- compile_principal derives requested_scopes [action.execute, object_type.read], while 0384's rotation copies the stored binding
-- ([action.execute]); register then stops on credential_rotation_required and no governed path changed a credential's scopes.
--   1. control.nexloop_rotate_service_credential_scopes: 0384's rotation (same caller, signing key, request replay, current token the
--      only active one, expiry rules) whose request also names requested_scopes (1 to 64 distinct canonical scopes, the EIOS
--      _CANONICAL_SCOPE form). The new token's binding is the current binding with those scopes and nothing else changed; subject,
--      principal, application, credential and revisions stay. The authentication fact (keyed by credential_id, shared by both tokens)
--      takes the new expiry and the new scopes: the caller passes it sealed and it must equal the stored fact but for expires_at,
--      snapshot_digest and requested_scopes, whose set equals the request's. EIOS compares every binding field with that fact, so
--      from this call on the previous token still authenticates but its decisions are stale (AUTHENTICATION_STALE) until it is
--      revoked: a scope change is not a seamless overlap, switch right after it (docs/release/run-credential-scope-rotation.md).
--   2. The row guard (0384) also accepts the second token from inside this entry (the call stack, never a GUC).
--   3. control.nexloop_revoke_superseded_service_credential: {request_id, credential_id, keep_digest, expires_at} revokes the one
--      active token of the credential with exactly that expiry, when keep_digest names another active token of it that expires
--      later (the token the caller's tree holds). For a rotation whose files are gone (R65: two active tokens, finish already run).
--      Idempotent by request_id; the journal entry names the revoked token (0384 trigger).
-- Every write goes through these definer functions; no role gains a table privilege.

-- 1. -------------------------------------------------------------------------------------------------------------------------------
create function control.nexloop_rotate_service_credential_scopes(p_tenant text,p_request jsonb,p_new_digest text,p_fact jsonb,p_key_id text,p_key bytea) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare v_text text;v_digest text;v_fingerprint text;v_prior jsonb;v_request uuid;v_until timestamptz;cur authz.nexloop_service_credentials;
 v_revision bigint;v_result jsonb;v_fact jsonb;v_scopes jsonb;v_binding jsonb;
begin
 perform control.nexloop_credential_configurator_check(p_tenant,p_key_id,p_key);
 if jsonb_typeof(p_request) is distinct from 'object' or (select count(*) from jsonb_object_keys(p_request))<>5
  or exists(select 1 from jsonb_object_keys(p_request) k where k not in ('request_id','credential_id','current_digest','expires_at','requested_scopes'))
  or coalesce(p_request->>'request_id','')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
  or coalesce(p_request->>'credential_id','')!~'^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$'
  or coalesce(p_request->>'current_digest','')!~'^[a-f0-9]{64}$' or coalesce(p_new_digest,'')!~'^[a-f0-9]{64}$'
  or p_new_digest=p_request->>'current_digest'
  or coalesce(p_request->>'expires_at','')!~'^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+(Z|[+-][0-9]{2}:[0-9]{2})$'
  or jsonb_typeof(p_request->'requested_scopes') is distinct from 'array'
  or jsonb_array_length(p_request->'requested_scopes') not between 1 and 64
  or exists(select 1 from jsonb_array_elements(p_request->'requested_scopes') s where jsonb_typeof(s)<>'string'
   or octet_length(s#>>'{}')>128 or s#>>'{}'!~'^[a-z][a-z0-9]*([._:-][a-z0-9]+){0,15}$')
  or (select count(distinct s) from jsonb_array_elements_text(p_request->'requested_scopes') s)<>jsonb_array_length(p_request->'requested_scopes') then
  raise exception 'credential rotation rejected' using errcode='22023';end if;
 v_request:=(p_request->>'request_id')::uuid;v_until:=(p_request->>'expires_at')::timestamptz;
 select jsonb_agg(s order by s) into v_scopes from jsonb_array_elements_text(p_request->'requested_scopes') s;
 v_text:=p_request::text;v_digest:=encode(sha256(convert_to(v_text,'UTF8')),'hex');
 v_fingerprint:=encode(sha256(p_key||convert_to(p_new_digest,'UTF8')),'hex');
 perform pg_advisory_xact_lock(hashtextextended('nexloop-configure:'||p_tenant,0));
 v_prior:=control.nexloop_credential_request_prior(p_tenant,v_request,v_digest,v_fingerprint);
 if v_prior is not null then return v_prior;end if;
 perform pg_advisory_xact_lock(hashtextextended('nexloop-service-credential:'||(p_request->>'credential_id'),0));
 select payload into v_fact from authz.nexloop_authority_facts where tenant_id=p_tenant and fact_kind='authentication'
  and entity_key=array[p_request->>'credential_id'];
 select * into cur from authz.nexloop_service_credentials where token_digest=p_request->>'current_digest' and tenant_id=p_tenant
  and credential_id=p_request->>'credential_id' and status='active' and binding->>'subject_kind'='service';
 if not found or exists(select 1 from authz.nexloop_service_credentials where credential_id=cur.credential_id and status='active' and token_digest<>cur.token_digest)
  or exists(select 1 from authz.nexloop_service_credentials where token_digest=p_new_digest)
  or v_until<=cur.expires_at or v_until<clock_timestamp()+interval '1 hour' or v_until>clock_timestamp()+interval '400 days'
  or v_fact is null or jsonb_typeof(p_fact) is distinct from 'object'
  or (p_fact-'expires_at'-'snapshot_digest'-'requested_scopes') is distinct from (v_fact-'expires_at'-'snapshot_digest'-'requested_scopes')
  or jsonb_typeof(p_fact->'requested_scopes') is distinct from 'array'
  or (select jsonb_agg(s order by s) from jsonb_array_elements_text(p_fact->'requested_scopes') s) is distinct from v_scopes
  or p_fact->>'status' is distinct from 'active' or (p_fact->>'expires_at')::timestamptz is distinct from v_until
  or coalesce(p_fact->>'snapshot_digest','')!~'^[a-f0-9]{64}$' or jsonb_typeof(cur.binding->'requested_scopes') is distinct from 'array' then
  raise exception 'credential rotation refused' using errcode='42501';end if;
 -- Equal scopes (in any order) keep the stored binding byte for byte, as 0384 does.
 v_binding:=case when (select jsonb_agg(s order by s) from jsonb_array_elements_text(cur.binding->'requested_scopes') s)=v_scopes then cur.binding
  else jsonb_set(cur.binding,'{requested_scopes}',v_scopes) end;
 insert into authz.nexloop_service_credentials(token_digest,tenant_id,credential_id,binding,worlds,audience,status,expires_at,agent_invocation,sim_use)
  values(p_new_digest,p_tenant,cur.credential_id,v_binding,cur.worlds,cur.audience,'active',v_until,cur.agent_invocation,cur.sim_use);
 update authz.nexloop_authority_facts set payload=p_fact where tenant_id=p_tenant and fact_kind='authentication' and entity_key=array[cur.credential_id];
 v_result:=jsonb_build_object('rotated',true,'credential_id',cur.credential_id,'expires_at',v_until,'previous_expires_at',cur.expires_at,'active_tokens',2,
  'scopes_added',(select count(*) from jsonb_array_elements(v_scopes) s where not cur.binding->'requested_scopes' @> jsonb_build_array(s)),
  'scopes_removed',(select count(*) from jsonb_array_elements(cur.binding->'requested_scopes') s where not v_scopes @> jsonb_build_array(s)),
  'replayed',false);
 v_revision:=control.nexloop_credential_request_record(p_tenant,v_request,v_digest,v_fingerprint,
  jsonb_build_object('kind','service_credential_scope_rotation','credential_id',cur.credential_id,'expires_at',v_until,'requested_scopes',v_scopes,'result',v_result));
 return v_result||jsonb_build_object('authority_revision',v_revision);
end $$;
alter function control.nexloop_rotate_service_credential_scopes(text,jsonb,text,jsonb,text,bytea) owner to nexloop_owner;
revoke all on function control.nexloop_rotate_service_credential_scopes(text,jsonb,text,jsonb,text,bytea) from public;
grant execute on function control.nexloop_rotate_service_credential_scopes(text,jsonb,text,jsonb,text,bytea) to nexloop_configurator;

-- 2. -------------------------------------------------------------------------------------------------------------------------------
-- As 0384's, with the scope rotation entry on the call stack accepted too.
create or replace function authz.nexloop_service_credential_rows_guard() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
declare v_stack text;
begin
 perform pg_advisory_xact_lock(hashtextextended('nexloop-service-credential:'||new.credential_id,0));
 get diagnostics v_stack=pg_context;
 if tg_op='UPDATE' and new.credential_id is distinct from old.credential_id then
  raise exception 'service credential id is immutable' using errcode='42501';end if;
 if tg_op='INSERT' and exists(select 1 from authz.nexloop_service_credentials c where c.credential_id=new.credential_id and c.token_digest<>new.token_digest)
  and position('function control.nexloop_rotate_service_credential(' in v_stack)=0
  and position('function control.nexloop_rotate_service_credential_scopes(' in v_stack)=0 then
  -- A second token for an id is inserted only from inside the rotation entry (the server-side call stack, L6 S68), never on a
  -- caller-settable GUC: the trusted configuration (0050) or any other path keeps the old unique behaviour.
  raise exception 'duplicate key value violates unique constraint "nexloop_service_credentials_credential_id_key"' using errcode='23505';end if;
 if new.status='active' and (select count(*) from authz.nexloop_service_credentials c
   where c.credential_id=new.credential_id and c.status='active' and c.token_digest<>new.token_digest)>=2 then
  raise exception 'a service credential holds at most two active tokens' using errcode='23505';end if;
 return new;
end $$;

-- 3. -------------------------------------------------------------------------------------------------------------------------------
create function control.nexloop_revoke_superseded_service_credential(p_tenant text,p_request jsonb,p_key_id text,p_key bytea) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare v_text text;v_digest text;v_fingerprint text;v_prior jsonb;v_request uuid;v_credential text;v_until timestamptz;keep authz.nexloop_service_credentials;
 tgt authz.nexloop_service_credentials;n integer;v_left integer;v_revision bigint;v_result jsonb;
begin
 perform control.nexloop_credential_configurator_check(p_tenant,p_key_id,p_key);
 if jsonb_typeof(p_request) is distinct from 'object' or (select count(*) from jsonb_object_keys(p_request))<>4
  or exists(select 1 from jsonb_object_keys(p_request) k where k not in ('request_id','credential_id','keep_digest','expires_at'))
  or coalesce(p_request->>'request_id','')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
  or coalesce(p_request->>'credential_id','')!~'^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$'
  or coalesce(p_request->>'keep_digest','')!~'^[a-f0-9]{64}$'
  or coalesce(p_request->>'expires_at','')!~'^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+(Z|[+-][0-9]{2}:[0-9]{2})$' then
  raise exception 'credential revocation rejected' using errcode='22023';end if;
 v_request:=(p_request->>'request_id')::uuid;v_credential:=p_request->>'credential_id';v_until:=(p_request->>'expires_at')::timestamptz;
 v_text:=p_request::text;v_digest:=encode(sha256(convert_to(v_text,'UTF8')),'hex');
 v_fingerprint:=encode(sha256(p_key||convert_to((p_request->>'keep_digest')||'/'||(p_request->>'expires_at'),'UTF8')),'hex');
 perform pg_advisory_xact_lock(hashtextextended('nexloop-configure:'||p_tenant,0));
 v_prior:=control.nexloop_credential_request_prior(p_tenant,v_request,v_digest,v_fingerprint);
 if v_prior is not null then return v_prior;end if;
 perform pg_advisory_xact_lock(hashtextextended('nexloop-service-credential:'||v_credential,0));
 select * into keep from authz.nexloop_service_credentials where token_digest=p_request->>'keep_digest' and tenant_id=p_tenant
  and credential_id=v_credential and status='active' and binding->>'subject_kind'='service';
 if not found then raise exception 'credential revocation refused' using errcode='42501';end if;
 select count(*) into n from authz.nexloop_service_credentials where tenant_id=p_tenant and credential_id=v_credential and status='active'
  and token_digest<>keep.token_digest and expires_at=v_until;
 select * into tgt from authz.nexloop_service_credentials where tenant_id=p_tenant and credential_id=v_credential and status='active'
  and token_digest<>keep.token_digest and expires_at=v_until;
 if n<>1 or tgt.expires_at>=keep.expires_at then
  raise exception 'credential revocation refused' using errcode='42501';end if;
 update authz.nexloop_service_credentials set status='revoked' where token_digest=tgt.token_digest;
 select count(*) into v_left from authz.nexloop_service_credentials where credential_id=v_credential and status='active';
 v_result:=jsonb_build_object('revoked',1,'credential_id',v_credential,'active_tokens',v_left,'retired',false,'superseded',true,'replayed',false);
 v_revision:=control.nexloop_credential_request_record(p_tenant,v_request,v_digest,v_fingerprint,
  jsonb_build_object('kind','service_credential_revocation','credential_id',v_credential,'result',v_result));
 return v_result||jsonb_build_object('authority_revision',v_revision);
end $$;
alter function control.nexloop_revoke_superseded_service_credential(text,jsonb,text,bytea) owner to nexloop_owner;
revoke all on function control.nexloop_revoke_superseded_service_credential(text,jsonb,text,bytea) from public;
grant execute on function control.nexloop_revoke_superseded_service_credential(text,jsonb,text,bytea) to nexloop_configurator;
