-- Independent narrow adapter for frozen EIOS AuthorizationFactsResolver.
-- Preserves 0313 service/API-key identity binding, no browser-session impersonation.
grant usage on schema control to nexloop_owner;
create schema authz authorization nexloop_owner;
revoke all on schema authz from public;
grant usage on schema authz to nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler;

-- Authentication realm registry; server-derived active tenant and monotonic epoch.
create table control.nexloop_tenants (
 tenant_id text primary key,
 status text not null check(status in ('active','suspended')),
 authority_revision bigint not null default 1 check(authority_revision>=1)
);
alter table control.nexloop_tenants owner to nexloop_owner;
revoke all on control.nexloop_tenants from public;
create function authz.nexloop_tenant_revision_guard() returns trigger language plpgsql
 set search_path=pg_catalog as $$
begin new.authority_revision:=old.authority_revision+1;return new;end $$;
alter function authz.nexloop_tenant_revision_guard() owner to nexloop_owner;
revoke all on function authz.nexloop_tenant_revision_guard() from public;
create trigger tenant_revision_guard before update on control.nexloop_tenants
 for each row execute function authz.nexloop_tenant_revision_guard();

-- Global authentication directory: no business data; only owner definers may read.
create table authz.nexloop_service_credentials (
 token_digest text primary key check(token_digest ~ '^[a-f0-9]{64}$'),
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 credential_id text not null unique,
 binding jsonb not null check(jsonb_typeof(binding)='object'),
 worlds text[] not null check(cardinality(worlds) between 1 and 16),
 audience text not null check(audience='nexloop-core'),
 status text not null check(status in ('active','revoked')),
 expires_at timestamptz not null,
 check(binding->>'tenant_id'=tenant_id),
 check(binding->>'credential_tenant_id'=tenant_id),
 check(binding->>'credential_id'=credential_id),
 check(binding->>'credential_kind'='api_key'),
 check(binding->>'subject_kind' in ('service','agent'))
);
alter table authz.nexloop_service_credentials owner to nexloop_owner;
revoke all on authz.nexloop_service_credentials from public;

-- EIOS typed authority records. Administrative authority publication only;
-- application/runtime roles have no raw SELECT/INSERT/UPDATE/DELETE privileges.
create table authz.nexloop_authority_facts (
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 fact_kind text not null check(fact_kind in ('subject','membership','actor','authentication','application','subject_authority','resource_graph','grants','scope','controls','policies','revision')),
 entity_key text[] not null check(cardinality(entity_key) between 1 and 3),
 payload jsonb not null check(jsonb_typeof(payload)='object'),
 primary key(tenant_id,fact_kind,entity_key),
 check(payload->>'tenant_id'=tenant_id)
);
alter table authz.nexloop_authority_facts owner to nexloop_owner;
alter table authz.nexloop_authority_facts enable row level security;
alter table authz.nexloop_authority_facts force row level security;
create policy nexloop_authority_tenant on authz.nexloop_authority_facts to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true))
 with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on authz.nexloop_authority_facts from public;

create function authz.nexloop_authority_epoch_guard() returns trigger language plpgsql security definer
 set search_path=pg_catalog as $$
begin
 update control.nexloop_tenants set authority_revision=authority_revision+1
  where tenant_id=case when tg_op='DELETE' then old.tenant_id else new.tenant_id end;
 return null;
end $$;
alter function authz.nexloop_authority_epoch_guard() owner to nexloop_owner;
revoke all on function authz.nexloop_authority_epoch_guard() from public;
create trigger credential_epoch_guard after insert or update or delete on authz.nexloop_service_credentials
 for each row execute function authz.nexloop_authority_epoch_guard();
create trigger authority_epoch_guard after insert or update or delete on authz.nexloop_authority_facts
 for each row execute function authz.nexloop_authority_epoch_guard();

create function authz.nexloop_service_identity(p_digest text,p_world text)
 returns jsonb language plpgsql stable security definer
 set search_path=pg_catalog set row_security=on as $$
declare v_row authz.nexloop_service_credentials%rowtype;
begin
 select * into v_row from authz.nexloop_service_credentials
  where token_digest=p_digest and status='active' and expires_at>clock_timestamp()
   and p_world=any(worlds) and audience='nexloop-core'
   and exists(select 1 from control.nexloop_tenants t where t.tenant_id=authz.nexloop_service_credentials.tenant_id and t.status='active');
 if not found then raise exception 'service authentication denied' using errcode='42501';end if;
 return jsonb_build_object('binding',v_row.binding,'expires_at',v_row.expires_at);
end $$;
alter function authz.nexloop_service_identity(text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_service_identity(text,text) from public;
grant execute on function authz.nexloop_service_identity(text,text) to nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler;

create function authz.nexloop_load_authority_fact(p_digest text,p_world text,p_kind text,p_key text[])
 returns jsonb language plpgsql stable security definer
 set search_path=pg_catalog set row_security=on as $$
declare v_binding jsonb;v_payload jsonb;v_principal text;v_valid boolean:=false;
begin
 v_binding:=authz.nexloop_service_identity(p_digest,p_world)->'binding';
 v_principal:=v_binding->>'subject_principal_id';
 case p_kind
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
alter function authz.nexloop_load_authority_fact(text,text,text,text[]) owner to nexloop_owner;
revoke all on function authz.nexloop_load_authority_fact(text,text,text,text[]) from public;
grant execute on function authz.nexloop_load_authority_fact(text,text,text,text[]) to nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler;
