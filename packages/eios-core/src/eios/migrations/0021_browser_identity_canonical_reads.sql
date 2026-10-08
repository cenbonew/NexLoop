-- Dedicated identity operator: no business schemas or application credentials.
create role nexloop_identity login nosuperuser nocreatedb nocreaterole noinherit nobypassrls;
grant usage on schema control to nexloop_identity;
grant select on control.schema_migrations to nexloop_identity;
create table control.nexloop_browser_applications (
 tenant_id text not null,application_id text not null,revision integer not null check(revision>0),active boolean not null,
 primary key(tenant_id,application_id)
);
create table control.nexloop_browser_subjects (
 subject_id text primary key,payload jsonb not null,
 check(payload->>'subject_id'=subject_id),check(payload->>'kind' in ('human','service','agent','system')),
 check(payload->>'status' in ('active','disabled')),check((payload->>'revision')::integer>0)
);
create table control.nexloop_browser_memberships (
 tenant_id text not null,principal_id text not null,subject_id text not null references control.nexloop_browser_subjects,
 payload jsonb not null,primary key(tenant_id,principal_id),
 check(payload->>'tenant_id'=tenant_id and payload->>'principal_id'=principal_id and payload->>'subject_id'=subject_id),
 check(payload->>'status' in ('invited','active','suspended','revoked')),check((payload->>'revision')::integer>0)
);
create table control.nexloop_browser_accounts (
 tenant_id text not null,local_account_id text not null,subject_id text not null references control.nexloop_browser_subjects,
 username text not null,password_hash text,password_history text[] not null default '{}',payload jsonb not null,
 primary key(tenant_id,local_account_id),unique(tenant_id,username),
 check(payload->>'tenant_id'=tenant_id and payload->>'local_account_id'=local_account_id and payload->>'subject_id'=subject_id and payload->>'username'=username),
 check(not(payload ? 'password_hash') and not(payload ? 'password_history')),
 check(payload->>'status' in ('invited','active','disabled')),check((payload->>'revision')::integer>0),check((payload->>'session_epoch')::integer>0)
);
alter table control.nexloop_browser_applications owner to nexloop_owner;
alter table control.nexloop_browser_subjects owner to nexloop_owner;
alter table control.nexloop_browser_memberships owner to nexloop_owner;
alter table control.nexloop_browser_accounts owner to nexloop_owner;
alter table control.nexloop_browser_applications enable row level security;
alter table control.nexloop_browser_applications force row level security;
alter table control.nexloop_browser_subjects enable row level security;
alter table control.nexloop_browser_subjects force row level security;
alter table control.nexloop_browser_memberships enable row level security;
alter table control.nexloop_browser_memberships force row level security;
alter table control.nexloop_browser_accounts enable row level security;
alter table control.nexloop_browser_accounts force row level security;
create policy browser_application_tenant on control.nexloop_browser_applications to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
create policy browser_membership_tenant on control.nexloop_browser_memberships to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
create policy browser_account_tenant on control.nexloop_browser_accounts to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
create policy browser_subject_tenant on control.nexloop_browser_subjects to nexloop_owner using(
 exists(select 1 from control.nexloop_browser_memberships m where m.subject_id=nexloop_browser_subjects.subject_id)
 or exists(select 1 from control.nexloop_browser_accounts a where a.subject_id=nexloop_browser_subjects.subject_id)
);
revoke all on control.nexloop_browser_applications,control.nexloop_browser_subjects,control.nexloop_browser_memberships,control.nexloop_browser_accounts from public;
create function control.nexloop_read_browser_identity(p_tenant text,p_application text,p_operator text,p_kind text,p_identifier text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare result jsonb;
begin
 if session_user<>'nexloop_identity' or p_operator is distinct from session_user
  or p_tenant is null or length(p_tenant) not between 1 and 320 or p_application is null or length(p_application) not between 1 and 320
  or p_identifier is null or length(p_identifier) not between 1 and 320 then
  raise exception 'identity unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform 1 from control.nexloop_browser_applications a where a.tenant_id=p_tenant and a.application_id=p_application and a.active;
 if not found then raise exception 'identity unavailable' using errcode='42501';end if;
 if p_kind='subject' then
  select s.payload into result from control.nexloop_browser_subjects s where s.subject_id=p_identifier;
 elsif p_kind='membership' then
  select m.payload into result from control.nexloop_browser_memberships m where m.tenant_id=p_tenant and m.principal_id=p_identifier;
 elsif p_kind in ('account','account_by_username') then
  select a.payload || jsonb_build_object('password_hash',a.password_hash,'password_history',a.password_history) into result
   from control.nexloop_browser_accounts a where a.tenant_id=p_tenant
    and ((p_kind='account' and a.local_account_id=p_identifier) or (p_kind='account_by_username' and a.username=p_identifier));
 else raise exception 'identity unavailable' using errcode='42501';end if;
 return result;
end $$;
alter function control.nexloop_read_browser_identity(text,text,text,text,text) owner to nexloop_owner;
revoke all on function control.nexloop_read_browser_identity(text,text,text,text,text) from public;
grant execute on function control.nexloop_read_browser_identity(text,text,text,text,text) to nexloop_identity;
