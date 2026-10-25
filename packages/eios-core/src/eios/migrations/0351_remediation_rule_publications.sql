-- NX-057 G1 (dispatcher 2026-10-25; docs/release/run-nx057-remediation-stage.md §0.4): the remediation rules are published from the
-- file's exact text, and each publication records the file's sha256 and the revision the operator expected, like the approval policies
-- (0330). The migration number is temporary; it lives only in the file name and the catalog.
--
-- * control.nexloop_remediation_rule_publications: one row per (tenant, world, manifest_version) published through this entry, append-only,
--   tenant FK; it references the rules row it published.
-- * control.nexloop_publish_remediation_rules(tenant, world, text, sha256, expected_revision): configurator only. The sha256 must be that
--   of the text. The same version with the same sha256 again is a replay (nothing written); the same version with another text is
--   refused (40001). Otherwise the expected revision must equal the tenant's current rules version (0 when none: 40001 otherwise), and
--   the text goes to control.nexloop_configure_remediation_rules (0338), which keeps every check (shape, declared effect source, versions
--   only increase). One publication at a time per tenant and world (advisory lock).
-- * The configurator no longer executes control.nexloop_configure_remediation_rules itself: this entry calls it as its owner, the
--   session user is still the configurator, so its own check holds. Every publication from now on is recorded.

create table control.nexloop_remediation_rule_publications (
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null,
 manifest_version integer not null check(manifest_version between 1 and 999999999),
 manifest_sha256 text not null check(manifest_sha256~'^[0-9a-f]{64}$'),
 expected_revision integer not null check(expected_revision>=0),
 published_by text not null default session_user,published_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,manifest_version),
 foreign key(tenant_id,world,manifest_version) references control.nexloop_remediation_rules(tenant_id,world,manifest_version),
 check(expected_revision<manifest_version)
);
alter table control.nexloop_remediation_rule_publications owner to nexloop_owner;
alter table control.nexloop_remediation_rule_publications enable row level security;
alter table control.nexloop_remediation_rule_publications force row level security;
create policy remediation_rule_publications_tenant on control.nexloop_remediation_rule_publications to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_remediation_rule_publications from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,
 nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_stand_in_worker;
create trigger nx057_remediation_rule_publications_append_only before update or delete on control.nexloop_remediation_rule_publications
 for each row execute function control.nexloop_nx022_append_only();

create function control.nexloop_publish_remediation_rules(p_tenant text,p_world text,p_text text,p_digest text,p_expected integer) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v_manifest jsonb;v integer;cur integer;pub control.nexloop_remediation_rule_publications;r jsonb;
begin
 if session_user<>'nexloop_configurator' then raise exception 'remediation rules are trusted configuration only' using errcode='42501';end if;
 if p_tenant is null or p_world is null or p_text is null or octet_length(p_text)>1048576
  or p_digest is distinct from encode(sha256(convert_to(p_text,'UTF8')),'hex') or p_expected is null or p_expected<0 then
  raise exception 'remediation rules: invalid' using errcode='22023';end if;
 begin
  v_manifest:=p_text::jsonb;
 exception when others then
  raise exception 'remediation rules: invalid' using errcode='22023';
 end;
 if jsonb_typeof(v_manifest->'manifest_version') is distinct from 'number' or coalesce(v_manifest->>'manifest_version','')!~'^[1-9][0-9]{0,8}$' then
  raise exception 'remediation rules: invalid' using errcode='22023';end if;
 v:=(v_manifest->>'manifest_version')::integer;
 perform pg_advisory_xact_lock(hashtextextended('nexloop-remediation-rules:'||p_tenant||':'||p_world,0));
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into pub from control.nexloop_remediation_rule_publications q where q.tenant_id=p_tenant and q.world=p_world and q.manifest_version=v;
 if found then
  perform set_config('eios.tenant_id',coalesce(prior,''),true);
  if pub.manifest_sha256<>p_digest then
   raise exception 'remediation rules: version % published with another text',v using errcode='40001';end if;
  return jsonb_build_object('configured',true,'manifest_version',v,'manifest_sha256',p_digest,'expected_revision',pub.expected_revision,'replay',true);
 end if;
 select coalesce(max(q.manifest_version),0) into cur from control.nexloop_remediation_rules q where q.tenant_id=p_tenant and q.world=p_world;
 if p_expected<>cur then
  perform set_config('eios.tenant_id',coalesce(prior,''),true);
  raise exception 'remediation rules: expected revision %, current %',p_expected,cur using errcode='40001';end if;
 r:=control.nexloop_configure_remediation_rules(p_tenant,p_world,v_manifest);
 -- A version configured before this entry existed (no publication row) replays there: nothing to record, and no recorded expected
 -- revision to report (null, L6 S-1/S-2).
 if (r->>'replay')::boolean then
  perform set_config('eios.tenant_id',coalesce(prior,''),true);
  return r||jsonb_build_object('manifest_sha256',p_digest,'expected_revision',null);
 end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 insert into control.nexloop_remediation_rule_publications(tenant_id,world,manifest_version,manifest_sha256,expected_revision)
  values(p_tenant,p_world,v,p_digest,p_expected);
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return r||jsonb_build_object('manifest_sha256',p_digest,'expected_revision',p_expected);
end $$;
alter function control.nexloop_publish_remediation_rules(text,text,text,text,integer) owner to nexloop_owner;
revoke all on function control.nexloop_publish_remediation_rules(text,text,text,text,integer) from public;
grant execute on function control.nexloop_publish_remediation_rules(text,text,text,text,integer) to nexloop_configurator;
revoke execute on function control.nexloop_configure_remediation_rules(text,text,jsonb) from nexloop_configurator;
