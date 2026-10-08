-- Authenticated Agent invocation and complete live release ceilings.
-- New independent lineage only; never modify published migrations or production.
alter table authz.nexloop_service_credentials add column agent_invocation jsonb;
alter table authz.nexloop_service_credentials add constraint agent_invocation_binding check (
 (binding->>'subject_kind'='service' and agent_invocation is null)
 or (binding->>'subject_kind'='agent' and agent_invocation is not null
  and jsonb_typeof(agent_invocation)='object'
  and agent_invocation->>'tenant_id' is not distinct from tenant_id
  and agent_invocation->>'actor_principal_id' is not distinct from binding->>'subject_principal_id'
  and agent_invocation->>'agent_id' is not distinct from binding->>'subject_id')
);
alter table authz.nexloop_authority_facts drop constraint nexloop_authority_facts_fact_kind_check;
alter table authz.nexloop_authority_facts add constraint nexloop_authority_facts_fact_kind_check
 check(fact_kind in ('subject','membership','actor','authentication','application','subject_authority','resource_graph','grants','scope','controls','policies','revision','agent','agent_release','agent_application'));

create or replace function authz.nexloop_service_identity(p_digest text,p_world text)
 returns jsonb language plpgsql stable security definer
 set search_path=pg_catalog set row_security=on as $$
declare v_row authz.nexloop_service_credentials%rowtype;
begin
 select * into v_row from authz.nexloop_service_credentials
  where token_digest=p_digest and status='active' and expires_at>clock_timestamp()
   and p_world=any(worlds) and audience='nexloop-core'
   and exists(select 1 from control.nexloop_tenants t where t.tenant_id=authz.nexloop_service_credentials.tenant_id and t.status='active');
 if not found then raise exception 'service authentication denied' using errcode='42501';end if;
 return jsonb_build_object('binding',v_row.binding,'expires_at',v_row.expires_at,'agent_invocation',v_row.agent_invocation);
end $$;


-- Embedded parent frames are evidence, never an independent authority store.
-- Read-only resolution checks current rows; dispatch locks the same dependencies.
create function authz.nexloop_assert_agent_parents(p_identity jsonb,p_lock boolean)
 returns void language plpgsql volatile security definer
 set search_path=pg_catalog set row_security=on as $$
declare v_tenant text;v_root jsonb;v_frame jsonb;v_release jsonb;v_agent jsonb;v_app jsonb;v_live jsonb;
begin
 if p_identity->'binding'->>'subject_kind'<>'agent' then return;end if;
 v_tenant:=p_identity->'binding'->>'tenant_id';
 perform set_config('eios.tenant_id',v_tenant,true);
 select payload into v_root from authz.nexloop_authority_facts
  where tenant_id=v_tenant and fact_kind='agent_release'
   and entity_key=array[p_identity->'agent_invocation'->>'release_id'];
 if not found then raise exception 'agent release unavailable' using errcode='42501';end if;
 if jsonb_typeof(v_root->'parent_chain') is distinct from 'array'
  or jsonb_array_length(v_root->'parent_chain')>32 then
  raise exception 'invalid parent authority chain' using errcode='42501';end if;
 if p_lock then
  perform payload from authz.nexloop_authority_facts
   where tenant_id=v_tenant and (
    (fact_kind='agent_release' and (entity_key=array[p_identity->'agent_invocation'->>'release_id']
      or entity_key in(select array[value->>'release_id'] from jsonb_array_elements(v_root->'parent_chain'))))
    or (fact_kind='agent' and entity_key in(select array[value->>'agent_id'] from jsonb_array_elements(v_root->'parent_chain')))
    or (fact_kind='agent_application' and entity_key in(select array[value->>'application_id',value->>'application_version'] from jsonb_array_elements(v_root->'parent_chain')))
   ) order by fact_kind,entity_key for share;
 end if;
 for v_frame in select value from jsonb_array_elements(v_root->'parent_chain') loop
  select payload into v_release from authz.nexloop_authority_facts
   where tenant_id=v_tenant and fact_kind='agent_release' and entity_key=array[v_frame->>'release_id'];
  if not found or v_release->'chain_complete' is distinct from 'true'::jsonb then
   raise exception 'live parent release unavailable' using errcode='42501';end if;
  select payload into v_agent from authz.nexloop_authority_facts
   where tenant_id=v_tenant and fact_kind='agent' and entity_key=array[v_release->>'agent_id'];
  if not found or v_agent->>'application_id' is distinct from v_release->>'application_id' then
   raise exception 'live parent agent unavailable' using errcode='42501';end if;
  select payload into v_app from authz.nexloop_authority_facts
   where tenant_id=v_tenant and fact_kind='agent_application'
    and entity_key=array[v_release->>'application_id',v_release->>'application_version'];
  if not found then raise exception 'live parent application unavailable' using errcode='42501';end if;
  v_live:=(v_release-array['repository_witness','snapshot_digest','parent_chain','chain_complete'])
   ||jsonb_build_object('agent_revision',v_agent->'revision','agent_status',v_agent->'status',
    'application',v_app-array['repository_witness','snapshot_digest']);
  if v_live is distinct from v_frame then
   raise exception 'parent release authority snapshot stale' using errcode='42501';end if;
 end loop;
end $$;
alter function authz.nexloop_assert_agent_parents(jsonb,boolean) owner to nexloop_owner;
revoke all on function authz.nexloop_assert_agent_parents(jsonb,boolean) from public;

create or replace function authz.nexloop_load_authority_fact(p_digest text,p_world text,p_kind text,p_key text[])
 returns jsonb language plpgsql stable security definer
 set search_path=pg_catalog set row_security=on as $$
declare v_identity jsonb;v_invocation jsonb;v_binding jsonb;v_payload jsonb;v_principal text;v_valid boolean:=false;
begin
 v_identity:=authz.nexloop_service_identity(p_digest,p_world);
 v_binding:=v_identity->'binding';v_invocation:=v_identity->'agent_invocation';
 v_principal:=v_binding->>'subject_principal_id';
 if p_kind='agent_release' then perform authz.nexloop_assert_agent_parents(v_identity,false);end if;
 case p_kind
 when 'agent' then v_valid:=v_binding->>'subject_kind'='agent' and p_key=array[v_invocation->>'agent_id'];
 when 'agent_release' then v_valid:=v_binding->>'subject_kind'='agent' and p_key=array[v_invocation->>'release_id'];
 when 'agent_application' then v_valid:=v_binding->>'subject_kind'='agent' and p_key=array[v_invocation->>'agent_application_id',v_invocation->>'agent_application_version'];
 when 'subject' then v_valid:=p_key=array[v_binding->>'subject_id'];
 when 'membership' then v_valid:=p_key=array[v_binding->>'subject_id',v_principal];
 when 'actor' then v_valid:=p_key=array[v_principal];
 when 'authentication' then v_valid:=p_key=array[v_binding->>'credential_id'];
 when 'application' then v_valid:=p_key=array[v_binding->>'caller_application_id',v_binding->>'caller_application_version'];
 when 'subject_authority' then v_valid:=p_key=array[v_principal];
 when 'grants' then v_valid:=cardinality(p_key)=2 and p_key[1]=v_principal;
 when 'scope','controls','policies' then v_valid:=cardinality(p_key)=3 and p_key[1]=v_principal;
 when 'resource_graph' then v_valid:=cardinality(p_key)=1;
 when 'revision' then v_valid:=p_key=array['catalog'];
 else v_valid:=false;
 end case;
 if v_valid is distinct from true then raise exception 'authority fact binding denied' using errcode='42501';end if;
 perform set_config('eios.tenant_id',v_binding->>'tenant_id',true);
 select payload into v_payload from authz.nexloop_authority_facts
  where tenant_id=v_binding->>'tenant_id' and fact_kind=p_kind and entity_key=p_key;
 return v_payload;
end $$;

create function authz.nexloop_fact_coverage(p_identity jsonb,p_claims jsonb)
 returns boolean language sql immutable security definer
 set search_path=pg_catalog as $$
 select jsonb_typeof(p_claims->'facts')='array'
 and (select array_agg(value->>'kind' order by value->>'kind') from jsonb_array_elements(p_claims->'facts')) =
 case when p_identity->'binding'->>'subject_kind'='agent' then
 array['actor','agent','agent_application','agent_release','application','authentication','controls','grants','membership','policies','resource_graph','revision','scope','subject','subject_authority']::text[]
 else array['actor','application','authentication','controls','grants','membership','policies','resource_graph','revision','scope','subject','subject_authority']::text[] end;
$$;
alter function authz.nexloop_fact_coverage(jsonb,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_fact_coverage(jsonb,jsonb) from public;

create or replace function authz.nexloop_assert_artifact_authority(p_digest text,p_world text,p_claims jsonb)
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
  or p_claims->>'resource_id' is distinct from 'eios:artifact:local_'||p_world
  or p_claims->>'operation' not in ('create','read','delete')
  or (p_claims->>'expires_at')::timestamptz <= clock_timestamp()
  or jsonb_typeof(p_claims->'facts') is distinct from 'array'
  or authz.nexloop_fact_coverage(v_identity,p_claims) is distinct from true
 then raise exception 'artifact authority binding stale or invalid' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_claims->>'tenant_id',true);
 -- Fixed ordering prevents cross-request lock-order inversion.
 for v_entry in select value from jsonb_array_elements(p_claims->'facts') order by value->>'kind',(value->'key')::text loop
  select array_agg(value) into v_key from jsonb_array_elements_text(v_entry->'key');
  select payload into v_payload from authz.nexloop_authority_facts
   where tenant_id=p_claims->>'tenant_id' and fact_kind=v_entry->>'kind' and entity_key=v_key for share;
  if not found then raise exception 'artifact authority dependency missing' using errcode='42501';end if;
  v_snapshot:=authz.nexloop_load_authority_fact_snapshot(p_digest,p_world,v_entry->>'kind',v_key);
  if v_snapshot->>'record_hash' is distinct from v_entry->>'record_hash' then
   raise exception 'artifact authority revision changed' using errcode='42501';
  end if;
 end loop;
 -- Tenant changes and remove/restore cycles must not resurrect an old permit.
 -- Lock this realm row after fact locks (matching publisher lock ordering).
 perform authz.nexloop_assert_agent_parents(v_identity,true);
 perform 1 from control.nexloop_tenants where tenant_id=p_claims->>'tenant_id' for share;
 v_identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if v_identity->>'directory_hash' is distinct from p_claims->>'directory_hash' then
  raise exception 'artifact authority epoch changed' using errcode='42501';
 end if;
 return v_identity->'binding';
end $$;
create or replace function authz.nexloop_assert_action_authority(p_digest text,p_world text,p_claims jsonb)
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
  or p_claims->>'resource_id' is distinct from p_claims->>'action_resource'
  or p_claims->>'operation' is distinct from 'execute'
  or (p_claims->>'expires_at')::timestamptz <= clock_timestamp()
  or jsonb_typeof(p_claims->'facts') is distinct from 'array'
  or authz.nexloop_fact_coverage(v_identity,p_claims) is distinct from true
 then raise exception 'action authority binding stale or invalid' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_claims->>'tenant_id',true);
 -- Fixed ordering prevents cross-request lock-order inversion.
 for v_entry in select value from jsonb_array_elements(p_claims->'facts') order by value->>'kind',(value->'key')::text loop
  select array_agg(value) into v_key from jsonb_array_elements_text(v_entry->'key');
  select payload into v_payload from authz.nexloop_authority_facts
   where tenant_id=p_claims->>'tenant_id' and fact_kind=v_entry->>'kind' and entity_key=v_key for share;
  if not found then raise exception 'action authority dependency missing' using errcode='42501';end if;
  v_snapshot:=authz.nexloop_load_authority_fact_snapshot(p_digest,p_world,v_entry->>'kind',v_key);
  if v_snapshot->>'record_hash' is distinct from v_entry->>'record_hash' then
   raise exception 'action authority revision changed' using errcode='42501';
  end if;
 end loop;
 -- Tenant changes and remove/restore cycles must not resurrect an old permit.
 -- Lock this realm row after fact locks (matching publisher lock ordering).
 perform authz.nexloop_assert_agent_parents(v_identity,true);
 perform 1 from control.nexloop_tenants where tenant_id=p_claims->>'tenant_id' for share;
 v_identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if v_identity->>'directory_hash' is distinct from p_claims->>'directory_hash'
  or (p_claims->>'expires_at')::timestamptz<=clock_timestamp() then
  raise exception 'action authority epoch changed' using errcode='42501';
 end if;
 return v_identity->'binding';
end $$;
create or replace function authz.nexloop_assert_read_authority(p_digest text,p_world text,p_claims jsonb)
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
  or p_claims->>'operation' is distinct from 'read'
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
create or replace function authz.nexloop_assert_edit_authority(p_digest text,p_world text,p_claims jsonb)
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
  or p_claims->>'operation' is distinct from 'edit'
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
create or replace function authz.nexloop_assert_relation_create_authority(p_digest text,p_world text,p_claims jsonb)
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
create or replace function authz.nexloop_assert_orphan_authority(p_digest text,p_world text,p_claims jsonb)
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