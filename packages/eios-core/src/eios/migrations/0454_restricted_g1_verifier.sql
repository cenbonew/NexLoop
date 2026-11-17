-- G1 maintenance only. No credential is provisioned and no application READ is widened.
create role nexloop_g1_verifier nologin nosuperuser nocreatedb nocreaterole noinherit noreplication nobypassrls connection limit 1;
alter role nexloop_g1_verifier set default_transaction_read_only=on;
create schema nexloop_maintenance authorization nexloop_owner;
revoke all on schema nexloop_maintenance from public;
grant usage on schema nexloop_maintenance to nexloop_g1_verifier;

create function nexloop_maintenance.nexloop_g1_verify() returns jsonb
 language plpgsql stable security definer
 set search_path=pg_catalog,pg_temp set row_security=on set "eios.tenant_id"='' as $$
declare owner_oid oid; verifier_oid oid;t record;expected bigint;scanned bigint:=0;suspended bigint:=0;
 active_count bigint:=0;positive bigint;pol pg_policy;
begin
 if session_user<>'nexloop_g1_verifier' or current_user<>'nexloop_owner'
  or current_setting('transaction_read_only')<>'on'
  or current_setting('transaction_isolation') not in ('repeatable read','serializable') then
  raise exception 'G1 restricted read-only snapshot required' using errcode='42501';
 end if;
 select oid into owner_oid from pg_roles where rolname='nexloop_owner' and not rolcanlogin and not rolsuper and not rolbypassrls;
 select oid into verifier_oid from pg_roles where rolname='nexloop_g1_verifier' and rolcanlogin and not rolsuper
  and not rolbypassrls and not rolcreaterole and not rolcreatedb and not rolreplication and not rolinherit;
 if owner_oid is null or verifier_oid is null
  or exists(select 1 from pg_auth_members where member=verifier_oid or roleid=verifier_oid)
  or exists(select 1 from pg_namespace where nspname in ('authz','control','runtime','ontology')
     and has_schema_privilege(verifier_oid,oid,'USAGE'))
  or has_schema_privilege(verifier_oid,'nexloop_maintenance','CREATE')
  or exists(select 1 from pg_class c join pg_namespace n on n.oid=c.relnamespace
     where n.nspname in ('authz','control','runtime','ontology') and c.relkind in ('r','p','v','m','f')
      and has_table_privilege(verifier_oid,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'))
  or not exists(select 1 from pg_class where oid='control.nexloop_tenants'::regclass
     and relowner=owner_oid and not relrowsecurity and not relforcerowsecurity)
  or not exists(select 1 from pg_class where oid='control.nexloop_effect_action_admissions'::regclass
     and relowner=owner_oid and relrowsecurity and relforcerowsecurity)
  or (select count(*) from pg_policy where polrelid='control.nexloop_effect_action_admissions'::regclass)<>1 then
  raise exception 'G1 authority or coverage structure invalid' using errcode='42501';
 end if;
 select * into pol from pg_policy where polrelid='control.nexloop_effect_action_admissions'::regclass;
 if pol.polname<>'effect_action_admissions_tenant' or pol.polcmd<>'*' or not pol.polpermissive
  or pol.polroles<>array[owner_oid]
  or pg_get_expr(pol.polqual,pol.polrelid) is distinct from '(tenant_id = current_setting(''eios.tenant_id''::text, true))'
  or pg_get_expr(pol.polwithcheck,pol.polrelid) is distinct from '(tenant_id = current_setting(''eios.tenant_id''::text, true))' then
  raise exception 'G1 tenant policy invalid' using errcode='42501';
 end if;
 select count(*) into positive from control.nexloop_effect_actions;
 select count(*) into expected from control.nexloop_tenants;
 if positive=0 or expected=0 then
  raise exception 'G1 trusted coverage or positive control empty' using errcode='55000';
 end if;
 for t in select tenant_id,status from control.nexloop_tenants order by tenant_id loop
  perform set_config('eios.tenant_id',t.tenant_id,true);
  active_count:=active_count+(select count(*) from control.nexloop_effect_action_admissions where retired_at is null);
  scanned:=scanned+1;
  if t.status='suspended' then suspended:=suspended+1;end if;
 end loop;
 if scanned<>expected then raise exception 'G1 incomplete coverage' using errcode='55000';end if;
 return jsonb_build_object('status',case when active_count=0 then 'clear' else 'blocked_nonzero' end,
  'tenants_checked',scanned,'suspended_checked',suspended,'effect_actions',positive,'active_admissions',active_count);
end $$;
alter function nexloop_maintenance.nexloop_g1_verify() owner to nexloop_owner;
revoke all on function nexloop_maintenance.nexloop_g1_verify() from public;
grant execute on function nexloop_maintenance.nexloop_g1_verify() to nexloop_g1_verifier;
comment on function nexloop_maintenance.nexloop_g1_verify() is 'Restricted G1 aggregate only; all registered tenants including suspended; no admission or business effect; repeatable read/read only required.';
