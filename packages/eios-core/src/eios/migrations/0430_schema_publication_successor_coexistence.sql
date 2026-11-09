-- A Schema publication's successor Actions coexist with their predecessors; the workbench and follow-latest grants cope with that
-- (stage 2026-11-09, R73: publishing Consumer v2 through NX-040 F3 added a successor :2 for each of the 98 Actions bound to
-- Consumer while :1 stayed active, by design - every caller moves to :2 on its own. 0125 counted active definitions per human
-- capability and raised 'workbench capability published more than once', so nearly every workbench write failed; and 0083 only
-- knew NX-044 review decisions as the source of a successor, so follow_latest_version refused Consumer.edit:2). Temporary number;
-- the dispatcher renumbers. Nothing is granted, deactivated or copied: authorization facts and Action definitions are untouched.
--
-- 1. authz.nexloop_workbench_action_for (0125, renamed once and wrapped): when one active definition carries the capability the
--    wrapped body answers as before. When several do and they are versions of one Action (one stable_name), the answer is the
--    highest active version the caller holds a non-empty grant on - the grants name exact versions (workbench roles :1), so the
--    caller keeps executing the version it is granted, and moves to a successor only once its roles grant that successor. No
--    granted version: the highest active version with granted false (forbidden, as before for a missing grant). Several Actions
--    (stable_names) carrying one human capability stay refused as before. The governed entry (0124/0150) still checks the
--    caller's authority on the exact resource it executes: this function only chooses which granted version to name.
-- 2. control.nexloop_service_grant_action_lineage (0083, replaced, same signature and grants): each row also carries
--    schema_publication_id - the NX-040 F3 publication whose successor_actions name the resource, published by a human
--    ('human:' principal) with permission_digest_before = permission_digest_after (the publication's own proof that no permission
--    changed). search_path gains pg_temp last (definer check). service_grants.resolve_follow accepts either source; every other
--    re-binding check is unchanged.

-- 1. The workbench Action for a human capability ------------------------------------------------------------------------------
alter function authz.nexloop_workbench_action_for(text,text,text) rename to nexloop_workbench_action_for_single_version;
revoke all on function authz.nexloop_workbench_action_for_single_version(text,text,text) from public,nexloop_api;
create function authz.nexloop_workbench_action_for(p_digest text,p_world text,p_capability text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb:=authz.nexloop_service_identity_snapshot(p_digest,p_world);v_tenant text:=ident->'binding'->>'tenant_id';
 v_principal text:=ident->'binding'->>'subject_principal_id';n integer;v_names integer;v_name text;v_version integer;v_granted boolean;
begin
 select count(*),count(distinct d.definition->>'stable_name'),min(d.definition->>'stable_name') into n,v_names,v_name
  from control.nexloop_action_definitions d where d.tenant_id=v_tenant and d.world=p_world and d.active
   and d.definition->'capability_binding'->>'capability_name'=p_capability;
 -- One definition, none, several Actions, or a non-human session: the wrapped body answers or refuses exactly as before.
 if n<=1 or v_names>1 or ident->'binding'->>'subject_kind' is distinct from 'human' then
  return authz.nexloop_workbench_action_for_single_version(p_digest,p_world,p_capability);
 end if;
 if not exists(select 1 from control.nexloop_governed_capabilities g where g.capability=p_capability and g.subject_rule='human') then
  raise exception 'workbench capability unknown' using errcode='22023';end if;
 -- Versions of one Action: the highest version the caller is granted; else the highest version, not granted.
 select (d.definition->>'version')::integer,true into v_version,v_granted
  from control.nexloop_action_definitions d where d.tenant_id=v_tenant and d.world=p_world and d.active
   and d.definition->'capability_binding'->>'capability_name'=p_capability
   and exists(select 1 from authz.nexloop_authority_facts f where f.tenant_id=v_tenant and f.fact_kind='grants'
    and f.entity_key=array[v_principal,d.resource_id] and jsonb_array_length(coalesce(f.payload->'grants','[]'::jsonb))>0)
  order by (d.definition->>'version')::integer desc limit 1;
 if v_version is null then
  select max((d.definition->>'version')::integer),false into v_version,v_granted
   from control.nexloop_action_definitions d where d.tenant_id=v_tenant and d.world=p_world and d.active
    and d.definition->'capability_binding'->>'capability_name'=p_capability;
 end if;
 return jsonb_build_object('stable_name',v_name,'version',v_version,'granted',v_granted);
end $$;
alter function authz.nexloop_workbench_action_for(text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_action_for(text,text,text) from public;
grant execute on function authz.nexloop_workbench_action_for(text,text,text) to nexloop_api;

-- 2. Follow-latest lineage: a Schema publication is a source too ---------------------------------------------------------------
create or replace function control.nexloop_service_grant_action_lineage(p_tenant text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if session_user<>'nexloop_configurator' then raise exception 'trusted configuration only' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 return coalesce((select jsonb_agg(jsonb_build_object('resource_id',a.resource_id,'stable_name',a.definition->>'stable_name',
   'version',(a.definition->>'version')::integer,'world',a.world,'active',a.active,'definition',a.definition,'capability',a.capability,
   'review_decision_id',(select d.decision_id from ontology.nexloop_review_decisions d where d.tenant_id=a.tenant_id and d.outcome='published'
     and d.publication->'published_refs' ? a.resource_id order by d.decided_at limit 1),
   'schema_publication_id',(select p.publication_id from ontology.nexloop_schema_publications p where p.tenant_id=a.tenant_id and p.world=a.world
     and a.resource_id=any(p.successor_actions) and p.published_by like 'human:%' and p.permission_digest_before=p.permission_digest_after
     order by p.published_at limit 1))
   order by a.definition->>'stable_name',(a.definition->>'version')::integer)
  from control.nexloop_action_definitions a where a.tenant_id=p_tenant),'[]'::jsonb);
end $$;
