-- NX-054 P1 (ADR-038 adopted 2026-10-22; design docs/design/relation-types-design.md §3, §7 P1): formal relation types are
-- defined and published; nothing derives, backfills or reads them yet (P2-P4). Temporary number; the dispatcher renumbers.
--
-- 1. control.nexloop_relation_type_profiles: per relation type version the NexLoop profile the EIOS definition does not carry
--    (the definition in ontology.relation_type_versions stays exactly RelationTypeDefinition, which the 0016 link compares):
--    source_property (the reference property P2 derives from) and consumer_ownership ('target': the source object belongs to
--    the target Consumer; 'transitive': to the Consumer the target object belongs to).
--    control.nexloop_relation_catalogs: each published catalog version with its longest ownership chain (C-1).
-- 2. control.nexloop_publish_relation_types(tenant, manifest): trusted configuration only (nexloop_configurator), the way
--    alert rules are (0131). It checks every definition (exact RelationTypeDefinition keys, existing endpoint types, no
--    properties), that each source type has at most one ownership relation, that every ownership chain reaches Consumer without
--    a cycle, and that the declared longest chain equals the computed one; a replay of identical definitions is accepted, a
--    different definition of a published version is refused (versions are immutable).
-- 3. ontology.nexloop_relation_world_guard: both endpoints of a relation exist in its tenant and world (the end-point foreign keys
--    of 0001 have no world column).
-- 4. Write privileges: nothing but the owner writes ontology.relations or ontology.relation_type_versions; the conditional 0001
--    grant to nex_eios_app (if that role exists) is revoked.
-- 5. The governed unlink entry (the 0016 link pattern): authz.nexloop_unlink_relation_action, capability
--    ontology.relation.unlink, relation authority with operation delete (authz.nexloop_assert_relation_delete_authority, the
--    0034 create check with operation delete), both endpoints' edit proofs, Action claim idempotency; a relation already absent
--    is a successful no-op.
-- 6. runtime.nexloop_relation_object_consumers(tenant, world, type, id): the Consumers an object belongs to through ownership
--    relations, walking at most the published longest chain. Not called by any reader yet (P4 switches the read guard).
-- Published migrations are not edited.

create table control.nexloop_relation_catalogs (
 tenant_id text not null references control.nexloop_tenants(tenant_id),version integer not null check(version>=1),
 longest_ownership_chain integer not null check(longest_ownership_chain between 1 and 8),
 relation_names text[] not null,manifest_digest text not null check(manifest_digest~'^[0-9a-f]{64}$'),
 published_by text not null,published_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,version));
create table control.nexloop_relation_type_profiles (
 tenant_id text not null references control.nexloop_tenants(tenant_id),relation_name text not null,version integer not null,
 source_property text check(source_property is null or source_property~'^[A-Za-z][A-Za-z0-9_]{0,63}$'),
 consumer_ownership text check(consumer_ownership in ('target','transitive')),
 primary key(tenant_id,relation_name,version),
 foreign key(tenant_id,relation_name,version) references ontology.relation_type_versions(tenant_id,relation_name,version));
do $$declare t text;begin
 foreach t in array array['control.nexloop_relation_catalogs','control.nexloop_relation_type_profiles'] loop
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy tenant_boundary on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on %s from public',t);
  execute format('create trigger relation_catalog_immutable before update on %s for each row execute function authz.nexloop_schema_version_immutable()',t);
 end loop;
end $$;

create function control.nexloop_publish_relation_types(p_tenant text,p_manifest jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);r jsonb;d jsonb;existing jsonb;own record;cur record;n integer:=0;v_len integer;
 v_longest integer:=0;v_names text[]:='{}';v_digest text;v_replay boolean:=true;v_catalog control.nexloop_relation_catalogs;
begin
 if session_user<>'nexloop_configurator' then raise exception 'relation types are trusted configuration only' using errcode='42501';end if;
 if p_manifest->>'schema' is distinct from 'nexloop-relation-types/1' or jsonb_typeof(p_manifest->'version') is distinct from 'number'
  or jsonb_typeof(p_manifest->'longest_ownership_chain') is distinct from 'number'
  or jsonb_typeof(p_manifest->'relation_types') is distinct from 'array' or jsonb_array_length(p_manifest->'relation_types') not between 1 and 200
  or exists(select 1 from jsonb_object_keys(p_manifest) k where k not in ('schema','version','longest_ownership_chain','relation_types')) then
  raise exception 'relation types invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 create temporary table if not exists pg_temp.nexloop_relation_publish(relation_name text primary key,source_type text,target_type text,ownership text)
  on commit drop;
 truncate pg_temp.nexloop_relation_publish;
 for r in select value from jsonb_array_elements(p_manifest->'relation_types') loop
  d:=r->'definition';
  if jsonb_typeof(r) is distinct from 'object' or exists(select 1 from jsonb_object_keys(r) k where k not in ('definition','source_property','consumer_ownership'))
   or jsonb_typeof(d) is distinct from 'object'
   or (select array_agg(k order by k) from jsonb_object_keys(d) k) is distinct from array['cardinality','description','inverse_name','properties','relation_name','source_type','target_type','version']
   or coalesce(d->>'relation_name','')!~'^[a-z][a-z0-9_]{0,63}$' or d->>'cardinality' not in ('one_to_one','one_to_many','many_to_one','many_to_many')
   or d->'properties' is distinct from '[]'::jsonb or jsonb_typeof(d->'version') is distinct from 'number'
   or coalesce(r->>'consumer_ownership','target') not in ('target','transitive')
   or (r->>'consumer_ownership'='target' and d->>'target_type'<>'Consumer') or (r->>'consumer_ownership'='transitive' and d->>'target_type'='Consumer')
   or (r ? 'source_property' and r->>'source_property' is not null and r->>'source_property'!~'^[A-Za-z][A-Za-z0-9_]{0,63}$')
   or not exists(select 1 from ontology.object_type_versions v where v.tenant_id=p_tenant and v.type_name=d->>'source_type')
   or not exists(select 1 from ontology.object_type_versions v where v.tenant_id=p_tenant and v.type_name=d->>'target_type') then
   raise exception 'relation type invalid: %',coalesce(d->>'relation_name','?') using errcode='22023';end if;
  begin
   insert into pg_temp.nexloop_relation_publish values(d->>'relation_name',d->>'source_type',d->>'target_type',r->>'consumer_ownership');
  exception when unique_violation then raise exception 'relation type declared twice: %',d->>'relation_name' using errcode='22023';end;
 end loop;
 -- One ownership relation per source type; every ownership chain reaches Consumer without a cycle; the declared longest chain.
 if exists(select 1 from pg_temp.nexloop_relation_publish where ownership is not null group by source_type having count(*)>1) then
  raise exception 'relation types invalid: two ownership relations from one type' using errcode='22023';end if;
 for own in select * from pg_temp.nexloop_relation_publish where ownership is not null loop
  v_len:=1;cur:=own;
  while cur.ownership='transitive' loop
   select * into cur from pg_temp.nexloop_relation_publish p where p.source_type=cur.target_type and p.ownership is not null;
   if not found then raise exception 'ownership chain of % does not reach Consumer',own.relation_name using errcode='22023';end if;
   v_len:=v_len+1;
   if v_len>8 then raise exception 'ownership chain of % has a cycle',own.relation_name using errcode='22023';end if;
  end loop;
  v_longest:=greatest(v_longest,v_len);
 end loop;
 if v_longest=0 or (p_manifest->>'longest_ownership_chain')::integer<>v_longest then
  raise exception 'declared longest ownership chain % is not the computed %',p_manifest->>'longest_ownership_chain',v_longest using errcode='22023';end if;
 -- Immutable versions: identical definitions replay, a different one is refused.
 for r in select value from jsonb_array_elements(p_manifest->'relation_types') loop
  d:=r->'definition';
  select definition into existing from ontology.relation_type_versions where tenant_id=p_tenant and relation_name=d->>'relation_name' and version=(d->>'version')::integer;
  if found then
   if existing is distinct from d or not exists(select 1 from control.nexloop_relation_type_profiles p where p.tenant_id=p_tenant
      and p.relation_name=d->>'relation_name' and p.version=(d->>'version')::integer
      and p.source_property is not distinct from r->>'source_property' and p.consumer_ownership is not distinct from r->>'consumer_ownership') then
    raise exception 'relation type % version % is published with other content',d->>'relation_name',d->>'version' using errcode='40001';end if;
  else
   insert into ontology.relation_type_versions(tenant_id,relation_name,version,definition) values(p_tenant,d->>'relation_name',(d->>'version')::integer,d);
   insert into control.nexloop_relation_type_profiles(tenant_id,relation_name,version,source_property,consumer_ownership)
    values(p_tenant,d->>'relation_name',(d->>'version')::integer,r->>'source_property',r->>'consumer_ownership');
   v_replay:=false;n:=n+1;
  end if;
  v_names:=v_names||(d->>'relation_name');
 end loop;
 v_digest:=encode(sha256(convert_to(p_manifest::text,'UTF8')),'hex');
 select * into v_catalog from control.nexloop_relation_catalogs where tenant_id=p_tenant and version=(p_manifest->>'version')::integer;
 if found then
  if v_catalog.manifest_digest<>v_digest then raise exception 'relation catalog version % is published with other content',p_manifest->>'version' using errcode='40001';end if;
 else
  insert into control.nexloop_relation_catalogs(tenant_id,version,longest_ownership_chain,relation_names,manifest_digest,published_by)
   values(p_tenant,(p_manifest->>'version')::integer,v_longest,(select array_agg(x order by x) from unnest(v_names) x),v_digest,session_user);
  v_replay:=false;
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return jsonb_build_object('published',true,'replayed',v_replay,'new_relation_types',n,'relation_types',cardinality(v_names),'longest_ownership_chain',v_longest);
end $$;
alter function control.nexloop_publish_relation_types(text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_publish_relation_types(text,jsonb) from public;
grant execute on function control.nexloop_publish_relation_types(text,jsonb) to nexloop_configurator;

-- 3. World isolation on the table itself.
create function ontology.nexloop_relation_world_guard() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);ok boolean;
begin
 perform set_config('eios.tenant_id',new.tenant_id,true);  -- the objects' tenant policy
 ok:=exists(select 1 from ontology.objects o where o.tenant_id=new.tenant_id and o.world=new.world and o.type_name=new.source_type and o.object_id=new.source_object_id)
  and exists(select 1 from ontology.objects o where o.tenant_id=new.tenant_id and o.world=new.world and o.type_name=new.target_type and o.object_id=new.target_object_id);
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 if not ok then raise exception 'relation endpoint outside the relation''s world' using errcode='42501';end if;
 return new;
end $$;
alter function ontology.nexloop_relation_world_guard() owner to nexloop_owner;
revoke all on function ontology.nexloop_relation_world_guard() from public;
create trigger nexloop_relation_world_guard before insert or update on ontology.relations for each row execute function ontology.nexloop_relation_world_guard();

-- 4. Only the owner writes relations and relation types.
do $$begin
 if exists(select 1 from pg_roles where rolname='nex_eios_app') then
  execute 'revoke insert,update,delete on ontology.relations,ontology.relation_type_versions from nex_eios_app';
 end if;
end $$;

-- 5. The governed unlink entry.
create function authz.nexloop_assert_relation_delete_authority(p_digest text,p_world text,p_claims jsonb)
 returns jsonb language plpgsql security definer
 set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_identity jsonb;v_entry jsonb;v_key text[];v_payload jsonb;v_snapshot jsonb;
begin
 -- Lock the credential through this short transaction, serializing revocation.
 perform authz.nexloop_lock_credential(p_digest,p_world,p_claims->>'resource_id');
 v_identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if p_claims->>'tenant_id' is distinct from v_identity->'binding'->>'tenant_id'
  or p_claims->>'credential_id' is distinct from v_identity->'binding'->>'credential_id'
  or p_claims->>'principal_id' is distinct from v_identity->'binding'->>'subject_principal_id'
  or p_claims->>'world' is distinct from p_world
  or p_claims->>'directory_hash' is distinct from v_identity->>'directory_hash'
  or p_claims->>'resource_id' is distinct from p_claims->>'target_resource'
  or p_claims->>'operation' is distinct from 'delete'
  or (p_claims->>'expires_at')::timestamptz <= clock_timestamp()
  or jsonb_typeof(p_claims->'facts') is distinct from 'array'
  or authz.nexloop_fact_coverage(v_identity,p_claims) is distinct from true
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
 perform authz.nexloop_assert_agent_parents(v_identity,true);
 perform 1 from control.nexloop_tenants where tenant_id=p_claims->>'tenant_id' for share;
 v_identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if v_identity->>'directory_hash' is distinct from p_claims->>'directory_hash'
  or (p_claims->>'expires_at')::timestamptz<=clock_timestamp() then
  raise exception 'read authority epoch changed' using errcode='42501';
 end if;
 return v_identity->'binding';
end $$;
alter function authz.nexloop_assert_relation_delete_authority(text,text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_assert_relation_delete_authority(text,text,jsonb) from public;

create function authz.nexloop_unlink_relation_action(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;body jsonb:=p_payload::jsonb;k bytea;stored runtime.nexloop_action_claims%rowtype;
 permit jsonb:=a->'permit';v_claim jsonb:=permit->'claim';ref jsonb:=v_claim->'binding'->'action_reference';d jsonb;s jsonb;v_id text;v_outcome jsonb;
 b control.nexloop_relation_action_bindings%rowtype;endpoint jsonb;proof jsonb;v_deleted integer;v_type text;
begin
 if octet_length(p_text)>1048576 or octet_length(p_payload)>262144 then raise exception 'instance command too large' using errcode='22023';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-relation-unlink-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'protocol' is distinct from 'nexloop-relation-unlink-v1'
  or a->>'resource_id' is distinct from 'eios:action:'||(ref->>'stable_name')||':'||(ref->>'version')
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds'
  or v_claim->'binding'->>'request_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'instance permit rejected' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 select definition into d from control.nexloop_action_definitions where tenant_id=a->>'tenant_id' and world=p_world and resource_id=a->>'resource_id' and active;
 if not found or d is distinct from a->'definition' or d->'governance'->>'approval_mode'<>'none'
  or d->'governance'->>'risk_level'<>'low' or jsonb_array_length(d->'governance'->'policy_refs')<>0
  or d->'capability_binding'->>'capability_name' is distinct from 'ontology.relation.unlink'
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
 perform authz.nexloop_assert_relation_delete_authority(p_digest,p_world,a->'relation_authority');
 perform pg_advisory_xact_lock(hashtextextended(jsonb_build_array(b.tenant_id,p_world,b.relation_name)::text,0));
 for endpoint in select distinct value from jsonb_array_elements(jsonb_build_array(
  jsonb_build_object('type',s->>'source_type','id',body->>'source_id'),
  jsonb_build_object('type',s->>'target_type','id',body->>'target_id'))) order by value loop
  select value into proof from jsonb_array_elements(a->'endpoint_authorities') where value->>'target_resource'='eios:object:'||(endpoint->>'type')||'/'||(endpoint->>'id');
  if not found then raise exception 'endpoint proof missing' using errcode='42501';end if;
  perform authz.nexloop_assert_edit_authority(p_digest,p_world,proof);
 end loop;
 v_id:=encode(sha256(convert_to(jsonb_build_array(b.tenant_id,p_world,b.relation_name,s->>'source_type',body->>'source_id',s->>'target_type',body->>'target_id')::text,'UTF8')),'hex');
 delete from ontology.relations where relation_id=v_id and tenant_id=b.tenant_id and world=p_world;
 get diagnostics v_deleted=row_count;
 v_outcome:=jsonb_build_object('outcome_id',v_id,'outcome_revision',1,'status','succeeded','outcome_digest',v_claim->'binding'->>'request_digest','finalized_at',clock_timestamp());
 update runtime.nexloop_action_claims set claim=stored.claim||jsonb_build_object('state','terminal','terminal_outcome',v_outcome)
  where tenant_id=stored.tenant_id and world=p_world and action_name=stored.action_name and intent_id=stored.intent_id;
 return jsonb_build_object('relation_id',v_id,'relation_name',b.relation_name,'world',p_world,'deleted',v_deleted=1);
end $$;
alter function authz.nexloop_unlink_relation_action(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_unlink_relation_action(text,text,text,text,text) from public;
grant execute on function authz.nexloop_unlink_relation_action(text,text,text,text,text) to nexloop_api,nexloop_domain_worker,nexloop_action_worker;

-- 6. Ownership through relations (P4 switches the readers; P1 only defines and tests it).
create function runtime.nexloop_relation_object_consumers(p_tenant text,p_world text,p_type text,p_object text) returns text[]
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v_limit integer;out text[];
begin
 if p_type='Consumer' then return array[p_object];end if;
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 select c.longest_ownership_chain into v_limit from control.nexloop_relation_catalogs c where c.tenant_id=p_tenant order by c.version desc limit 1;
 with recursive walk(type_name,object_id,depth) as (
   select p_type,p_object,0
   union all
   select r.target_type,r.target_object_id,w.depth+1 from walk w
    join ontology.relations r on r.tenant_id=p_tenant and r.world=p_world and r.source_type=w.type_name and r.source_object_id=w.object_id
    join control.nexloop_relation_type_profiles p on p.tenant_id=r.tenant_id and p.relation_name=r.relation_name and p.version=r.schema_version
     and p.consumer_ownership is not null
    where w.depth<coalesce(v_limit,0) and w.type_name<>'Consumer')
 select coalesce(array_agg(distinct object_id order by object_id),'{}') into out from walk where type_name='Consumer' and depth>0;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return out;
end $$;
alter function runtime.nexloop_relation_object_consumers(text,text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_relation_object_consumers(text,text,text,text) from public;
