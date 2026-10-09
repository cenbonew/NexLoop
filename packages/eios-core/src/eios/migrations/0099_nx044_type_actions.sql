-- NX-044 closing: approving a new object type publishes, in the same transaction, the type's governed
-- <Type>.create:1 and <Type>.edit:1 Action definitions next to Schema v1, so Claims waiting for the type
-- can be applied once trusted configuration grants them (ADR-020 §3: still no authority fact is written;
-- the 0082 fingerprint gate keeps enforcing that).
-- Both definitions reuse a Capability snapshot the tenant already publishes for
-- ontology.object.create / ontology.object.edit (no new capability, scope or risk) and must equal the
-- canonical low-risk, no-approval, policy-free shape exactly; only contract_digest, created_at and the
-- bound schema digest come from the reviewer's backend (EIOS models + schema_contract_digest).

create function ontology.nexloop_review_type_action_capabilities(p_tenant text) returns jsonb
 language sql stable security definer set search_path=pg_catalog set row_security=on as $$
 select coalesce(jsonb_object_agg(n,cap),'{}'::jsonb) from (
  select distinct on (a.capability->>'capability_name') a.capability->>'capability_name' n,a.capability cap
  from control.nexloop_action_definitions a where a.tenant_id=p_tenant and a.world='real' and a.active
   and a.capability->>'capability_name' in ('ontology.object.create','ontology.object.edit')
   and a.definition->'capability_binding'->>'capability_name'=a.capability->>'capability_name'
  order by a.capability->>'capability_name',a.resource_id) s
$$;

create function ontology.nexloop_review_type_action_expected(p_tenant text,p_type text,p_suffix text,p_cap jsonb,p_digest text) returns jsonb
 language sql immutable set search_path=pg_catalog as $$
 select jsonb_build_object('tenant_id',p_tenant,'definition_type','action','stable_name',p_type||'.'||p_suffix,'version',1,'status','published',
  'required_scopes',p_cap->'required_scopes','required_markings','[]'::jsonb,'created_by','nexloop-review-publication','previous_version',null,
  'source_lineage','[]'::jsonb,
  'capability_binding',jsonb_build_object('capability_name',p_cap->>'capability_name','capability_version',p_cap->>'capability_version','schema_hash',p_cap->>'schema_hash'),
  'object_types',jsonb_build_array(r.ref),'preconditions','[]'::jsonb,
  'governance',jsonb_build_object('change_scope',jsonb_build_object('object_types',jsonb_build_array(r.ref),'properties','[]'::jsonb,'target_systems','["postgres"]'::jsonb),
   'risk_level','low','policy_refs','[]'::jsonb,'approval_mode','none','idempotency',jsonb_build_object('required',true,'key_fields','["request_id"]'::jsonb),
   'compensation_mode','none','receipt_required',true),
  'receipt_schema','{"type":"object"}'::jsonb,'input_schema','{}'::jsonb,'parameters','[]'::jsonb)
 from (select jsonb_build_object('tenant_id',p_tenant,'schema_type','object_type','stable_name',p_type,'version',1,'schema_digest',p_digest) ref) r
$$;

-- Basis: unchanged plus the tenant's create/edit capability snapshots for new-type Actions.
create or replace function authz.nexloop_read_review_publication_basis(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare t text:=authz.nexloop_assert_review_read(p_digest,p_world,p_text,p_signature,p_payload);c jsonb:=p_payload::jsonb;
 x ontology.nexloop_candidate_definitions%rowtype;owner text;v integer;s jsonb;
begin
 select * into x from ontology.nexloop_candidate_definitions where tenant_id=t and world=p_world and candidate_id=c->>'candidate_id' and status='pending_review';
 if not found then return null;end if;
 owner:=ontology.nexloop_review_owner_type(x.candidate,x.kind);
 select d.version,d.definition into v,s from ontology.object_type_versions d where d.tenant_id=t and d.type_name=owner order by d.version desc limit 1;
 return jsonb_build_object('candidate_id',x.candidate_id,'kind',x.kind,'revision',x.revision,'candidate',x.candidate,'owner_type',owner,
  'latest_version',v,'schema',s,
  'actions',coalesce((select jsonb_agg(jsonb_build_object('resource_id',a.resource_id,'definition',a.definition,'capability',a.capability) order by a.resource_id)
   from control.nexloop_action_definitions a where a.tenant_id=t and a.world='real' and a.active and v is not null
    and exists(select 1 from jsonb_array_elements(a.definition->'object_types') r where r->>'stable_name'=owner and (r->>'version')::integer=v)),'[]'::jsonb),
  'type_action_capabilities',case when x.kind='object_type' then ontology.nexloop_review_type_action_capabilities(t) else '{}'::jsonb end);
end $$;

-- Gates: 0082 unchanged for property / vocabulary_value; a new type must come with exactly its two Actions.
alter function ontology.nexloop_review_publication_gates(text,text,ontology.nexloop_candidate_definitions,jsonb) rename to nexloop_review_publication_gates_v0082;
revoke all on function ontology.nexloop_review_publication_gates_v0082(text,text,ontology.nexloop_candidate_definitions,jsonb) from public;
create function ontology.nexloop_review_publication_gates(p_tenant text,p_world text,x ontology.nexloop_candidate_definitions,p_pub jsonb)
 returns text[] language plpgsql stable security definer set search_path=pg_catalog set row_security=on as $$
declare f text[];owner text;caps jsonb;a jsonb;cap jsonb;suffix text;digest text;seen text[]:='{}';
begin
 if x.kind<>'object_type' then return ontology.nexloop_review_publication_gates_v0082(p_tenant,p_world,x,p_pub);end if;
 f:=ontology.nexloop_review_publication_gates_v0082(p_tenant,p_world,x,p_pub-'actions');
 if cardinality(f)>0 or (jsonb_typeof(p_pub->'builder_failures')='array' and jsonb_array_length(p_pub->'builder_failures')>0) then return f;end if;
 owner:=ontology.nexloop_review_owner_type(x.candidate,x.kind);caps:=ontology.nexloop_review_type_action_capabilities(p_tenant);
 foreach suffix in array array['create','edit'] loop
  if not caps ? ('ontology.object.'||suffix) then f:=array_append(f,'type_action_capability_unavailable:ontology.object.'||suffix);end if;
 end loop;
 if cardinality(f)>0 then return f;end if;
 if jsonb_typeof(p_pub->'actions') is distinct from 'array' or jsonb_array_length(p_pub->'actions')<>2 then return array['new_type_actions_must_be_create_and_edit'];end if;
 for a in select value from jsonb_array_elements(p_pub->'actions') loop
  suffix:=substring(a->'definition'->>'stable_name' from '^'||owner||'\.(create|edit)$');
  cap:=caps->('ontology.object.'||coalesce(suffix,''));
  if suffix is null or suffix=any(seen) then f:=array_append(f,'new_type_actions_must_be_create_and_edit');continue;end if;
  seen:=array_append(seen,suffix);
  digest:=a->'definition'->'object_types'->0->>'schema_digest';
  if coalesce(digest,'')!~'^[0-9a-f]{64}$' or jsonb_typeof(a->'definition'->'created_at') is distinct from 'string'
   or a->'capability' is distinct from cap
   or (a->'definition')-'contract_digest'-'created_at' is distinct from ontology.nexloop_review_type_action_expected(p_tenant,owner,suffix,cap,digest)
   or exists(select 1 from control.nexloop_action_definitions d where d.tenant_id=p_tenant and d.world='real' and d.resource_id='eios:action:'||owner||'.'||suffix||':1') then
   f:=array_append(f,'new_type_action_not_canonical:'||suffix);
  end if;
 end loop;
 if (p_pub->'actions'->0->'definition'->'object_types'->0->>'schema_digest') is distinct from (p_pub->'actions'->1->'definition'->'object_types'->0->>'schema_digest') then
  f:=array_append(f,'new_type_actions_bind_different_schemas');end if;
 return f;
end $$;

do $grants$
declare f text;
begin
 foreach f in array array['ontology.nexloop_review_type_action_capabilities(text)','ontology.nexloop_review_type_action_expected(text,text,text,jsonb,text)',
  'authz.nexloop_read_review_publication_basis(text,text,text,text,text)','ontology.nexloop_review_publication_gates(text,text,ontology.nexloop_candidate_definitions,jsonb)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';
  execute 'revoke all on function '||f||' from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator';
 end loop;
end $grants$;
grant execute on function authz.nexloop_read_review_publication_basis(text,text,text,text,text) to nexloop_api;
