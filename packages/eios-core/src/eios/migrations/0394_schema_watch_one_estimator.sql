-- S87 (S86 (a), owner 2026-11-01; temporary number, the dispatcher numbers it at batch time): the Context estimator v2 counts
-- the item envelopes and the fixed sections, so budget_report.used of a v2 pack is several times a v1 pack's for the same content.
-- The Schema publication watch (0257) compares the average used of the week before a publication with the packs after it; a watch
-- spanning the estimator change would read the change as a Context volume over 105% and suggest hiding the Schema. It now compares
-- packs of the newest pack's estimator only; with fewer than 20 such packs on a side it makes no Context claim (counts shown, as
-- before). Body otherwise 0257's, verbatim.
create or replace function runtime.nexloop_schema_watch(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare w record;eb record;ea record;cb record;ca record;items jsonb:='[]'::jsonb;n_active integer:=0;n_reg integer:=0;v_ext boolean;v_ctx boolean;v_est text;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 for w in select publication_id,type_name,published_at,watch_until from runtime.nexloop_schema_watches
   where tenant_id=p_tenant and world=p_world and watch_until>clock_timestamp() order by published_at loop
  n_active:=n_active+1;
  select count(*) n,avg(case when status in ('failed','dead_lettered') then 1.0 else 0.0 end) rate into eb from runtime.jobs
   where tenant_id=p_tenant and world=p_world and queue='claim-extraction' and status in ('succeeded','failed','dead_lettered')
    and updated_at>=w.published_at-interval '7 days' and updated_at<w.published_at;
  select count(*) n,avg(case when status in ('failed','dead_lettered') then 1.0 else 0.0 end) rate into ea from runtime.jobs
   where tenant_id=p_tenant and world=p_world and queue='claim-extraction' and status in ('succeeded','failed','dead_lettered') and updated_at>=w.published_at;
  -- S87: compare packs of one estimator only (the newest one's); an estimator change is not a Context volume change.
  select budget_report->>'estimator' into v_est from runtime.nexloop_context_packs where tenant_id=p_tenant and world=p_world order by created_at desc limit 1;
  select count(*) n,avg((budget_report->>'used')::numeric) used into cb from runtime.nexloop_context_packs
   where tenant_id=p_tenant and world=p_world and created_at>=w.published_at-interval '7 days' and created_at<w.published_at and jsonb_typeof(budget_report->'used')='number' and budget_report->>'estimator' is not distinct from v_est;
  select count(*) n,avg((budget_report->>'used')::numeric) used into ca from runtime.nexloop_context_packs
   where tenant_id=p_tenant and world=p_world and created_at>=w.published_at and jsonb_typeof(budget_report->'used')='number' and budget_report->>'estimator' is not distinct from v_est;
  v_ext:=eb.n>=20 and ea.n>=20 and ea.rate-eb.rate>0.02;
  v_ctx:=cb.n>=20 and ca.n>=20 and cb.used>0 and ca.used>cb.used*1.05;
  if v_ext or v_ctx then n_reg:=n_reg+1;end if;
  items:=items||jsonb_build_array(jsonb_build_object('publication_id',w.publication_id,'type_name',w.type_name,'published_at',w.published_at,'watch_until',w.watch_until,
   'extraction_tasks_before',eb.n,'extraction_tasks_after',ea.n,'extraction_failure_rate_before',round(coalesce(eb.rate,0),4),'extraction_failure_rate_after',round(coalesce(ea.rate,0),4),
   'context_packs_before',cb.n,'context_packs_after',ca.n,'context_used_before',round(coalesce(cb.used,0),1),'context_used_after',round(coalesce(ca.used,0),1),
   'extraction_regressing',v_ext,'context_over_limit',v_ctx,'regressing',v_ext or v_ctx,'suggestion',case when v_ext or v_ctx then 'hide' end));
 end loop;
 return jsonb_build_object('active',n_active,'regressing',n_reg,'publications',items);
end $$;
alter function runtime.nexloop_schema_watch(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_schema_watch(text,text) from public;
