-- Exact immutable Relation schema and Action binding; no domain pack.
create trigger relation_schema_version_immutable before update on ontology.relation_type_versions
 for each row execute function authz.nexloop_schema_version_immutable();
create trigger relation_schema_authority_revision after insert or update or delete on ontology.relation_type_versions
 for each row execute function authz.nexloop_authority_epoch_guard();
create table control.nexloop_relation_action_bindings (
 tenant_id text not null,world text not null,resource_id text not null,action_contract_digest text not null check(action_contract_digest ~ '^[a-f0-9]{64}$'),
 relation_name text not null,relation_version integer not null,schema_digest text not null check(schema_digest ~ '^[a-f0-9]{64}$'),
 primary key(tenant_id,world,resource_id),
 foreign key(tenant_id,world,resource_id) references control.nexloop_action_definitions(tenant_id,world,resource_id),
 foreign key(tenant_id,relation_name,relation_version) references ontology.relation_type_versions(tenant_id,relation_name,version)
);
alter table control.nexloop_relation_action_bindings owner to nexloop_owner;
alter table control.nexloop_relation_action_bindings enable row level security;
alter table control.nexloop_relation_action_bindings force row level security;
create policy relation_action_binding_tenant on control.nexloop_relation_action_bindings to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_relation_action_bindings from public;
create trigger relation_action_binding_immutable before update on control.nexloop_relation_action_bindings
 for each row execute function authz.nexloop_schema_version_immutable();
create trigger relation_action_binding_authority_revision after insert or update or delete on control.nexloop_relation_action_bindings
 for each row execute function authz.nexloop_authority_epoch_guard();
alter table ontology.relations add column world text not null default 'real';
create unique index nexloop_relation_pair on ontology.relations(tenant_id,world,relation_name,source_type,source_object_id,target_type,target_object_id);

create or replace function authz.nexloop_read_action_bundle(p_digest text,p_world text,p_text text,p_signature text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare bundle jsonb;r jsonb;payload jsonb;schemas jsonb:='[]'::jsonb;b control.nexloop_relation_action_bindings%rowtype;v_relation jsonb;
begin
 bundle:=authz.nexloop_read_action_definition(p_digest,p_world,p_text,p_signature);
 for r in select distinct value from jsonb_array_elements((bundle->'definition'->'object_types')||coalesce(bundle->'definition'->'governance'->'change_scope'->'object_types','[]'::jsonb)) loop
  if r->>'tenant_id' is distinct from p_text::jsonb->>'tenant_id' or r->>'schema_type' is distinct from 'object_type' then
   raise exception 'unsupported Action schema dependency' using errcode='42501';end if;
  select definition into payload from ontology.object_type_versions where tenant_id=r->>'tenant_id' and type_name=r->>'stable_name' and version=(r->>'version')::integer;
  if not found then raise exception 'Action schema dependency missing' using errcode='42501';end if;
  schemas:=schemas||jsonb_build_array(jsonb_build_object('reference',r,'definition',payload));
 end loop;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,p_text::jsonb);
 select * into b from control.nexloop_relation_action_bindings where tenant_id=p_text::jsonb->>'tenant_id'
  and world=p_world and resource_id=p_text::jsonb->>'resource_id';
 if found then
  select definition into v_relation from ontology.relation_type_versions where tenant_id=b.tenant_id and relation_name=b.relation_name and version=b.relation_version;
  if not found then raise exception 'Relation schema dependency missing' using errcode='42501';end if;
  return bundle||jsonb_build_object('schemas',schemas,'relation_binding',to_jsonb(b),'relation_schema',v_relation);
 end if;
 return bundle||jsonb_build_object('schemas',schemas);
end $$;
alter function authz.nexloop_read_action_bundle(text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_read_action_bundle(text,text,text,text) from public;
grant execute on function authz.nexloop_read_action_bundle(text,text,text,text) to nexloop_api,nexloop_domain_worker,nexloop_action_worker;

-- Service object/property reads using actual frozen EIOS authority facts.
create function authz.nexloop_assert_relation_create_authority(p_digest text,p_world text,p_claims jsonb)
 returns jsonb language plpgsql security definer
 set search_path=pg_catalog set row_security=on as $$
declare v_identity jsonb;v_entry jsonb;v_key text[];v_payload jsonb;v_snapshot jsonb;
begin
 -- Lock the credential through this short transaction, serializing revocation.
 perform 1 from authz.nexloop_service_credentials where token_digest=p_digest for share;
 v_identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if p_claims->>'tenant_id' is distinct from v_identity->'binding'->>'tenant_id'
  or p_claims->>'credential_id' is distinct from v_identity->'binding'->>'credential_id'
  or p_claims->>'principal_id' is distinct from v_identity->'binding'->>'subject_principal_id'
  or p_claims->>'world' is distinct from p_world
  or p_claims->>'directory_hash' is distinct from v_identity->>'directory_hash'
  or p_claims->>'resource_id' is distinct from p_claims->>'target_resource'
  or p_claims->>'operation' is distinct from 'create'
  or (p_claims->>'expires_at')::timestamptz <= clock_timestamp()
  or jsonb_typeof(p_claims->'facts') is distinct from 'array'
  or jsonb_array_length(p_claims->'facts')<>12
 then raise exception 'read authority binding stale or invalid' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_claims->>'tenant_id',true);
 -- Fixed ordering prevents cross-request lock-order inversion.
 for v_entry in select value from jsonb_array_elements(p_claims->'facts') order by value->>'kind',(value->'key')::text loop
  select array_agg(value) into v_key from jsonb_array_elements_text(v_entry->'key');
  select payload into v_payload from authz.nexloop_authority_facts
   where tenant_id=p_claims->>'tenant_id' and fact_kind=v_entry->>'kind' and entity_key=v_key for share;
  if not found then raise exception 'read authority dependency missing' using errcode='42501';end if;
  v_snapshot:=authz.nexloop_load_authority_fact_snapshot(p_digest,p_world,v_entry->>'kind',v_key);
  if v_snapshot->>'record_hash' is distinct from v_entry->>'record_hash' then
   raise exception 'read authority revision changed' using errcode='42501';
  end if;
 end loop;
 -- Tenant changes and remove/restore cycles must not resurrect an old permit.
 -- Lock this realm row after fact locks (matching publisher lock ordering).
 perform 1 from control.nexloop_tenants where tenant_id=p_claims->>'tenant_id' for share;
 v_identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if v_identity->>'directory_hash' is distinct from p_claims->>'directory_hash'
  or (p_claims->>'expires_at')::timestamptz<=clock_timestamp() then
  raise exception 'read authority epoch changed' using errcode='42501';
 end if;
 return v_identity->'binding';
end $$;
alter function authz.nexloop_assert_relation_create_authority(text,text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_assert_relation_create_authority(text,text,jsonb) from public;


create function authz.nexloop_link_relation_action(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;body jsonb:=p_payload::jsonb;k bytea;stored runtime.nexloop_action_claims%rowtype;
 permit jsonb:=a->'permit';v_claim jsonb:=permit->'claim';ref jsonb:=v_claim->'binding'->'action_reference';d jsonb;s jsonb;v_id text;v_outcome jsonb;b control.nexloop_relation_action_bindings%rowtype;endpoint jsonb;proof jsonb;v_type text;v_endpoint text;v_existing ontology.relations%rowtype;
begin
 if octet_length(p_text)>1048576 or octet_length(p_payload)>262144 then raise exception 'instance command too large' using errcode='22023';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-relation-link-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'protocol' is distinct from 'nexloop-relation-link-v1'
  or a->>'resource_id' is distinct from 'eios:action:'||(ref->>'stable_name')||':'||(ref->>'version')
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds'
  or v_claim->'binding'->>'request_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'instance permit rejected' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 select definition into d from control.nexloop_action_definitions where tenant_id=a->>'tenant_id' and world=p_world and resource_id=a->>'resource_id' and active;
 if not found or d is distinct from a->'definition' or d->'governance'->>'approval_mode'<>'none'
  or d->'governance'->>'risk_level'<>'low' or jsonb_array_length(d->'governance'->'policy_refs')<>0
  or d->'capability_binding'->>'capability_name' not in ('ontology.relation.link')
  or not (d->'governance'->'change_scope'->'target_systems' @> '["postgres"]'::jsonb)
  or jsonb_array_length(d->'governance'->'change_scope'->'properties')<>0
 then raise exception 'instance Action contract unavailable' using errcode='42501';end if;
 select * into b from control.nexloop_relation_action_bindings where tenant_id=a->>'tenant_id' and world=p_world and resource_id=a->>'resource_id';
 if not found or to_jsonb(b) is distinct from a->'relation_binding' or b.relation_name is distinct from body->>'relation_name' or b.action_contract_digest is distinct from ref->>'contract_digest' then
  raise exception 'Relation binding unavailable' using errcode='42501';end if;
 select definition into s from ontology.relation_type_versions where tenant_id=b.tenant_id and relation_name=b.relation_name and version=b.relation_version;
 if not found or s is distinct from a->'schema' then raise exception 'Relation schema stale' using errcode='42501';end if;
 for v_type in select distinct value from jsonb_array_elements_text(jsonb_build_array(s->>'source_type',s->>'target_type')) loop
  if not exists(select 1 from jsonb_array_elements(d->'governance'->'change_scope'->'object_types') r where r->>'tenant_id'=a->>'tenant_id' and r->>'stable_name'=v_type) then
   raise exception 'Relation endpoint type outside Action scope' using errcode='42501';end if;
 end loop;
 select * into stored from runtime.nexloop_action_claims where tenant_id=a->>'tenant_id' and world=p_world
  and action_name=v_claim->'key'->>'action_stable_name' and intent_id=v_claim->'key'->>'idempotency_key' for update;
 if not found or stored.principal_id is distinct from a->>'principal_id' or stored.claim is distinct from v_claim
  or v_claim->>'state'<>'active' or (v_claim->>'lease_expires_at')::timestamptz<=clock_timestamp()
  or (permit->>'expires_at')::timestamptz<=clock_timestamp() then
  raise exception 'instance claim fenced' using errcode='40001';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 if body->>'source_id' !~ '^[a-f0-9]{64}$' or body->>'target_id' !~ '^[a-f0-9]{64}$'
  or a->'relation_authority'->>'target_resource' is distinct from 'eios:relation:'||b.relation_name then
  raise exception 'Relation target invalid' using errcode='42501';end if;
 perform authz.nexloop_assert_relation_create_authority(p_digest,p_world,a->'relation_authority');
 -- A single namespace lock serializes pair/cardinality decisions across processes.
 perform pg_advisory_xact_lock(hashtextextended(jsonb_build_array(b.tenant_id,p_world,b.relation_name)::text,0));
 for endpoint in select distinct value from jsonb_array_elements(jsonb_build_array(
  jsonb_build_object('type',s->>'source_type','id',body->>'source_id'),
  jsonb_build_object('type',s->>'target_type','id',body->>'target_id'))) order by value loop
  v_type:=endpoint->>'type';v_endpoint:=endpoint->>'id';
  select value into proof from jsonb_array_elements(a->'endpoint_authorities') where value->>'target_resource'='eios:object:'||v_type||'/'||v_endpoint;
  if not found then raise exception 'endpoint proof missing' using errcode='42501';end if;
  perform authz.nexloop_assert_edit_authority(p_digest,p_world,proof);
  perform 1 from ontology.objects o where o.tenant_id=a->>'tenant_id' and o.world=p_world and o.type_name=v_type and o.object_id=v_endpoint
   and exists(select 1 from jsonb_array_elements(d->'governance'->'change_scope'->'object_types') r where r->>'stable_name'=v_type and (r->>'version')::integer=o.schema_version) for share;
  if not found then raise exception 'Relation endpoint unavailable' using errcode='42501';end if;
 end loop;
 v_id:=encode(sha256(convert_to(jsonb_build_array(b.tenant_id,p_world,b.relation_name,s->>'source_type',body->>'source_id',s->>'target_type',body->>'target_id')::text,'UTF8')),'hex');
 select * into v_existing from ontology.relations where relation_id=v_id;
 if found then
  if v_existing.metadata is distinct from body->'metadata' or v_existing.schema_version<>b.relation_version then
   raise exception 'Relation pair payload conflict' using errcode='22000';end if;
 else
  if ((s->>'cardinality' in ('one_to_one','many_to_one')) and exists(select 1 from ontology.relations where tenant_id=b.tenant_id and world=p_world and relation_name=b.relation_name and source_type=s->>'source_type' and source_object_id=body->>'source_id'))
   or ((s->>'cardinality' in ('one_to_one','one_to_many')) and exists(select 1 from ontology.relations where tenant_id=b.tenant_id and world=p_world and relation_name=b.relation_name and target_type=s->>'target_type' and target_object_id=body->>'target_id')) then
   raise exception 'Relation cardinality conflict' using errcode='22000';end if;
  insert into ontology.relations(relation_id,tenant_id,world,relation_name,schema_version,source_type,source_object_id,target_type,target_object_id,metadata,created_at)
   values(v_id,b.tenant_id,p_world,b.relation_name,b.relation_version,s->>'source_type',body->>'source_id',s->>'target_type',body->>'target_id',body->'metadata',clock_timestamp());
 end if;
 v_outcome:=jsonb_build_object('outcome_id',v_id,'outcome_revision',1,'status','succeeded','outcome_digest',v_claim->'binding'->>'request_digest','finalized_at',clock_timestamp());
 update runtime.nexloop_action_claims set claim=stored.claim||jsonb_build_object('state','terminal','terminal_outcome',v_outcome)
  where tenant_id=stored.tenant_id and world=p_world and action_name=stored.action_name and intent_id=stored.intent_id;
 return jsonb_build_object('relation_id',v_id,'relation_name',b.relation_name,'world',p_world);
end $$;
alter function authz.nexloop_link_relation_action(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_link_relation_action(text,text,text,text,text) from public;
grant execute on function authz.nexloop_link_relation_action(text,text,text,text,text) to nexloop_api,nexloop_domain_worker,nexloop_action_worker;

