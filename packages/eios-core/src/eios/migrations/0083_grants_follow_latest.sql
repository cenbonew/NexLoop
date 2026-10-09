-- ADR-020 §3 follow-latest grants: read-only lineage of published Action versions
-- for the trusted configuration identity. service_grants uses it to extend a
-- manifest grant declared follow_latest_version to successor versions that a human
-- review decision (NX-044) published as a pure schema re-binding. No write here.
create function control.nexloop_service_grant_action_lineage(p_tenant text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
begin
 if session_user<>'nexloop_configurator' then raise exception 'trusted configuration only' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 return coalesce((select jsonb_agg(jsonb_build_object('resource_id',a.resource_id,'stable_name',a.definition->>'stable_name',
   'version',(a.definition->>'version')::integer,'world',a.world,'active',a.active,'definition',a.definition,'capability',a.capability,
   'review_decision_id',(select d.decision_id from ontology.nexloop_review_decisions d where d.tenant_id=a.tenant_id and d.outcome='published'
     and d.publication->'published_refs' ? a.resource_id order by d.decided_at limit 1))
   order by a.definition->>'stable_name',(a.definition->>'version')::integer)
  from control.nexloop_action_definitions a where a.tenant_id=p_tenant),'[]'::jsonb);
end $$;
alter function control.nexloop_service_grant_action_lineage(text) owner to nexloop_owner;
revoke all on function control.nexloop_service_grant_action_lineage(text) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;
grant execute on function control.nexloop_service_grant_action_lineage(text) to nexloop_configurator;
