-- Enterprise world model W4 "data assets" counts (docs/design/workbench-world-model-design.md §5 W4; owner 2026-10-19; temporary
-- number 0243). The M12 metrics snapshot (latest wrapper 0216) gains one block, `data_assets`, read by the workbench metrics read and
-- otherwise unused: counts, profile ids, dimensions and times only, never a Claim value, quote, recall body or embedding.
--   claims           total, by epistemic kind, by resolution state (tenant and world);
--   extraction_runs  count and the last run time;
--   extraction_jobs  the claim-extraction queue's jobs by status (the same rows as the snapshot's queues block);
--   recall           entries by index kind (definition entries are tenant-wide), embedding profiles with their vector counts,
--                    and the recorded backfills (runs, last run, last queued);
--   embedding_budget unavailable (no budget or cost kind records embedding spend: budget kinds are model / incentive, cost kinds
--                    model / channel / discount / service / labour), never reported as 0.
-- The existing blocks (candidates 0216, backup 0146) are reused, not repeated. Nothing is written.
alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_data_assets_v0243;
revoke all on function runtime.nexloop_metrics_snapshot_before_data_assets_v0243(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v jsonb:=runtime.nexloop_metrics_snapshot_before_data_assets_v0243(p_tenant,p_world);d jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select jsonb_build_object(
  'claims',jsonb_build_object(
    'total',(select count(*) from ontology.nexloop_claims c where c.tenant_id=p_tenant and c.world=p_world),
    'by_epistemic_kind',coalesce((select jsonb_object_agg(k,n) from (select c.epistemic_kind k,count(*) n from ontology.nexloop_claims c
       where c.tenant_id=p_tenant and c.world=p_world group by 1) x),'{}'::jsonb),
    'by_resolution_state',coalesce((select jsonb_object_agg(k,n) from (select c.resolution_state k,count(*) n from ontology.nexloop_claims c
       where c.tenant_id=p_tenant and c.world=p_world group by 1) x),'{}'::jsonb)),
  'extraction_runs',(select jsonb_build_object('total',count(*),'last_at',max(r.recorded_at)) from ontology.nexloop_extraction_runs r
     where r.tenant_id=p_tenant and r.world=p_world),
  'extraction_jobs',coalesce((select jsonb_object_agg(s,n) from (select j.status s,count(*) n from runtime.jobs j
     where j.tenant_id=p_tenant and j.world=p_world and j.queue='claim-extraction' group by 1) x),'{}'::jsonb),
  'recall',jsonb_build_object(
    'entries',coalesce((select jsonb_object_agg(k,n) from (select e.index_kind k,count(*) n from ontology.nexloop_recall_entries e
       where e.tenant_id=p_tenant and (e.world=p_world or e.world is null) group by 1) x),'{}'::jsonb),
    'profiles',coalesce((select jsonb_agg(jsonb_build_object('profile_id',p.profile_id,'dimension',p.dimension,'created_at',p.created_at,
       'vectors',(select count(*) from ontology.nexloop_recall_embeddings m join ontology.nexloop_recall_entries e on e.entry_id=m.entry_id
         where m.tenant_id=p_tenant and m.profile_id=p.profile_id and (e.world=p_world or e.world is null))) order by p.profile_id)
       from ontology.nexloop_embedding_profiles p where p.tenant_id=p_tenant),'[]'::jsonb),
    'backfills',coalesce((select jsonb_agg(jsonb_build_object('profile_id',b.profile_id,'runs',b.runs,'first_at',b.first_at,'last_at',b.last_at,
       'last_queued',b.last_queued,'last_definition_types',b.last_definition_types) order by b.profile_id)
       from ontology.nexloop_recall_backfills b where b.tenant_id=p_tenant and b.world=p_world),'[]'::jsonb)),
  'embedding_budget',jsonb_build_object('status','unavailable','reason','not_recorded')) into d;
 return v||jsonb_build_object('data_assets',d);
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;
