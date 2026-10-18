-- Claim-matcher idle without a merge configuration (dispatcher 2026-10-18; temporary number, renumbered at merge). A tenant with
-- staged candidate definitions and no active merge configuration (deploy/ontology/merge-config.v1.json never applied through
-- nexloop_eios.merge_configuration --apply) made every claim-matcher tick fail with LookupError; the worker now reports the explicit
-- idle code merge_configuration_missing, and the M12 snapshot carries the 'candidates' block so alert-rules v5 can warn:
-- staged (count, world-scoped), merge_configuration_active (boolean, tenant-scoped) and staged_without_configuration (the staged
-- count while no configuration is active, else 0). Counts only, never candidate content.
alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_candidates_v0441;
revoke all on function runtime.nexloop_metrics_snapshot_before_candidates_v0441(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v jsonb:=runtime.nexloop_metrics_snapshot_before_candidates_v0441(p_tenant,p_world);v_staged bigint;v_active boolean;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select count(*) into v_staged from ontology.nexloop_candidate_definitions where tenant_id=p_tenant and world=p_world and status='staged';
 select exists(select 1 from ontology.nexloop_merge_configurations c where c.tenant_id=p_tenant and c.active) into v_active;
 return v||jsonb_build_object('candidates',jsonb_build_object('staged',v_staged,'merge_configuration_active',v_active,
  'staged_without_configuration',case when v_active then 0 else v_staged end));
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;
