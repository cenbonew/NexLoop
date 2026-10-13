-- ADR-031 (owner 2026-10-13): within one authz.nexloop_read_object call, derive the relay Source read of the object once.
-- The relay Source has no configured grants, so each property proof of a catalog read re-ran the whole relay-source-v1
-- derivation (rule, enrollment scan, binding, object) although only its property part differs (L4 profile: ~370
-- derivations per message). Now: the object derivation once per call, the property part per property
-- (authz.nexloop_relay_source_property_basis), every other check of authz.nexloop_assert_relay_source_read unchanged per
-- property, and one more full object derivation at the end of the call that must equal the first. Not across calls,
-- objects or tenants. read_object keeps its name, owner and grants (create or replace); the previous body is kept as
-- authz.nexloop_read_object_before_derivation_once_v0420 (owner only) for the equivalence test and for rollback.
CREATE FUNCTION authz.nexloop_read_object_before_derivation_once_v0420(p_digest text, p_world text, p_text text, p_signature text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare a jsonb:=p_text::jsonb;k bytea;r ontology.objects%rowtype;field text;proof jsonb;values jsonb:='{}'::jsonb;target text;
begin
 if octet_length(p_text)>1048576 or jsonb_typeof(a->'fields') is distinct from 'array' or jsonb_array_length(a->'fields')>64 then
  raise exception 'invalid object read' using errcode='22023';end if;
 target:='eios:object:'||(a->>'type_name')||'/'||(a->>'object_id');
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or a->>'protocol' is distinct from 'nexloop-object-read-v1'
  or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-object-read-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'resource_id' is distinct from target or a->>'target_resource' is distinct from target
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'object read authority rejected' using errcode='42501';end if;
 perform authz.nexloop_assert_read_authority(p_digest,p_world,a);
 select * into r from ontology.objects where tenant_id=a->>'tenant_id' and world=p_world and type_name=a->>'type_name' and object_id=a->>'object_id' for share;
 if not found then raise exception 'object unavailable' using errcode='42501';end if;
 for field in select value from jsonb_array_elements_text(a->'fields') loop
  select value into proof from jsonb_array_elements(a->'property_authorities') where value->>'target_resource'='eios:property:'||r.type_name||'/'||r.object_id||'/'||field;
  if not found or not (r.properties ? field) then raise exception 'property unavailable' using errcode='42501';end if;
  perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
  values:=values||jsonb_build_object(field,r.properties->field);
 end loop;
 perform authz.nexloop_assert_read_authority(p_digest,p_world,a);
 return jsonb_build_object('object_id',r.object_id,'type_name',r.type_name,'world',r.world,'schema_version',r.schema_version,'properties',values,'revision',r.nexloop_revision);
end $function$;

CREATE FUNCTION authz.nexloop_relay_source_property_basis(p_tenant text, p_world text, p_object_resource text, p_object_basis jsonb, p_resource text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
-- ADR-031: the relay-source-v1 derivation of one property, given the derivation of its object in the same call. Exactly the
-- property-specific part of authz.nexloop_relay_source_derivation (0142, wrapped 0182): the fixed field lists, the property in
-- the object's schema version, the owner-restricted group exclusion and the restriction hash; the object part (rule,
-- enrollment, binding naming this Source, object present) is p_object_basis, the derivation of p_object_resource; the property
-- must belong to exactly that object.
declare m text[];t text;oid text;prop text;restriction jsonb;v_object ontology.objects;def jsonb;grp text;
begin
 if p_object_basis is null or p_world is distinct from 'real' then return null;end if;
 m:=regexp_match(p_resource,'^eios:property:(ServiceOffering|ConsumerServiceOffering)/([a-f0-9]{64})/([a-z_]{1,64})$');
 if m is null then return null;end if;
 t:=m[1];oid:=m[2];prop:=m[3];
 if p_object_basis->>'type_name' is distinct from t or p_object_resource is distinct from 'eios:object:'||t||'/'||oid then return null;end if;
 if (t='ServiceOffering' and prop not in ('active','allowed_discounts','allowed_guarantees','content_kind','currency','delivery_action','eligibility',
      'evidence_kind','price_amount','service_code','title','valid_until'))
  or (t='ConsumerServiceOffering' and prop not in ('active','consumer_id','offering_id','offering_revision','source_principal')) then return null;end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into v_object from ontology.objects where tenant_id=p_tenant and world=p_world and object_id=oid and type_name=t for share;
 if v_object.object_id is null then return null;end if;
 select definition into def from ontology.object_type_versions where tenant_id=p_tenant and type_name=t and version=v_object.schema_version for share;
 if def is null or not exists(select 1 from jsonb_array_elements(def->'properties') d where d->>'property_name'=prop) then return null;end if;
 grp:=ontology.nexloop_property_group_of(def,prop);
 select payload into restriction from authz.nexloop_authority_facts where tenant_id=p_tenant and fact_kind='property_group_restriction' and entity_key=array[t] for share;
 if grp is not null and restriction->'restricted_groups' ? grp then return null;end if;
 return p_object_basis||jsonb_build_object('restriction_hash',case when restriction is null then null else encode(sha256(convert_to(restriction::text,'UTF8')),'hex') end);
end $function$;

CREATE FUNCTION authz.nexloop_assert_relay_source_property_read(p_digest text, p_world text, p_claims jsonb, p_object_resource text, p_object_basis jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare ident jsonb;tenant text;principal text;basis jsonb:=p_claims->'derivation_basis';b jsonb;
begin
 perform runtime.nexloop_assert_not_erasing(p_world,p_claims);  -- as authz.nexloop_assert_read_authority does first
 perform authz.nexloop_lock_credential(p_digest,p_world,p_claims->>'resource_id');
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);  -- raises for a revoked or expired credential
 tenant:=ident->'binding'->>'tenant_id';principal:=ident->'binding'->>'subject_principal_id';
 if p_claims->>'derivation' is distinct from 'relay-source-v1' or jsonb_typeof(basis) is distinct from 'object'
  or p_claims->>'tenant_id' is distinct from tenant or p_claims->>'principal_id' is distinct from principal
  or p_claims->>'credential_id' is distinct from ident->'binding'->>'credential_id'
  or p_claims->>'world' is distinct from p_world or p_world is distinct from 'real'
  or p_claims->>'directory_hash' is distinct from ident->>'directory_hash'
  or p_claims->>'resource_id' is distinct from p_claims->>'target_resource' or p_claims->>'operation' is distinct from 'read'
  or p_claims->>'expires_at' is null or (p_claims->>'expires_at')::timestamptz<=clock_timestamp()
  or p_claims->'facts' is distinct from '[]'::jsonb
  or exists(select 1 from authz.nexloop_browser_token_realms br where br.token_digest=p_digest)
  or ident->'binding'->>'subject_kind' is distinct from 'service' or (ident->'run_context' is not null and ident->'run_context'<>'null'::jsonb) then
  raise exception 'derived relay source read invalid' using errcode='42501';end if;
 perform set_config('eios.tenant_id',tenant,true);
 if exists(select 1 from authz.nexloop_authority_facts where tenant_id=tenant and fact_kind='grants' and entity_key=array[principal,p_claims->>'resource_id']) then
  raise exception 'derived relay source read superseded' using errcode='42501';end if;
 -- ADR-031: the object part comes from this call's single object derivation; the property part is checked here.
 b:=authz.nexloop_relay_source_property_basis(tenant,p_world,p_object_resource,p_object_basis,p_claims->>'resource_id');
 if b is null then raise exception 'derived relay source read unavailable' using errcode='42501';end if;
 if b->>'rule_hash' is distinct from basis->>'rule_hash' or b->>'consumer_id' is distinct from basis->>'consumer_id'
  or b->'enrollment_revision' is distinct from basis->'enrollment_revision' or b->>'type_name' is distinct from basis->>'type_name'
  or b->'restriction_hash' is distinct from basis->'restriction_hash' then raise exception 'derived relay source read changed' using errcode='42501';end if;
 if (p_claims->>'expires_at')::timestamptz>(b->>'rule_valid_until')::timestamptz then raise exception 'derived relay source read expired' using errcode='42501';end if;
 return ident->'binding';
end $function$;

CREATE OR REPLACE FUNCTION authz.nexloop_read_object(p_digest text, p_world text, p_text text, p_signature text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare a jsonb:=p_text::jsonb;k bytea;r ontology.objects%rowtype;field text;proof jsonb;values jsonb:='{}'::jsonb;target text;obj_basis jsonb;
begin
 if octet_length(p_text)>1048576 or jsonb_typeof(a->'fields') is distinct from 'array' or jsonb_array_length(a->'fields')>64 then
  raise exception 'invalid object read' using errcode='22023';end if;
 target:='eios:object:'||(a->>'type_name')||'/'||(a->>'object_id');
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or a->>'protocol' is distinct from 'nexloop-object-read-v1'
  or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-object-read-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'resource_id' is distinct from target or a->>'target_resource' is distinct from target
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'object read authority rejected' using errcode='42501';end if;
 perform authz.nexloop_assert_read_authority(p_digest,p_world,a);
 select * into r from ontology.objects where tenant_id=a->>'tenant_id' and world=p_world and type_name=a->>'type_name' and object_id=a->>'object_id' for share;
 if not found then raise exception 'object unavailable' using errcode='42501';end if;
 for field in select value from jsonb_array_elements_text(a->'fields') loop
  select value into proof from jsonb_array_elements(a->'property_authorities') where value->>'target_resource'='eios:property:'||r.type_name||'/'||r.object_id||'/'||field;
  if not found or not (r.properties ? field) then raise exception 'property unavailable' using errcode='42501';end if;
  if proof->>'derivation'='relay-source-v1' then
   -- ADR-031: one relay Source derivation of this object per call; each property keeps its own checks.
   if obj_basis is null then
    obj_basis:=authz.nexloop_relay_source_derivation(a->>'tenant_id',a->>'principal_id',p_world,target,'read');
    if obj_basis is null then raise exception 'derived relay source read unavailable' using errcode='42501';end if;
   end if;
   perform authz.nexloop_assert_relay_source_property_read(p_digest,p_world,proof,target,obj_basis);
  else
   perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
  end if;
  values:=values||jsonb_build_object(field,r.properties->field);
 end loop;
 -- Anything committed during the call that the per-property derivations would have seen is seen here.
 if obj_basis is not null and authz.nexloop_relay_source_derivation(a->>'tenant_id',a->>'principal_id',p_world,target,'read') is distinct from obj_basis then
  raise exception 'derived relay source read changed' using errcode='42501';end if;
 perform authz.nexloop_assert_read_authority(p_digest,p_world,a);
 return jsonb_build_object('object_id',r.object_id,'type_name',r.type_name,'world',r.world,'schema_version',r.schema_version,'properties',values,'revision',r.nexloop_revision);
end $function$;

alter function authz.nexloop_read_object_before_derivation_once_v0420(text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_read_object_before_derivation_once_v0420(text,text,text,text) from public;
alter function authz.nexloop_relay_source_property_basis(text,text,text,jsonb,text) owner to nexloop_owner;
revoke all on function authz.nexloop_relay_source_property_basis(text,text,text,jsonb,text) from public;
alter function authz.nexloop_assert_relay_source_property_read(text,text,jsonb,text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_assert_relay_source_property_read(text,text,jsonb,text,jsonb) from public;
