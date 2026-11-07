-- The workbench review queue shows each candidate's category (retrieval_miss, ambiguity_unresolved and, since 0419,
-- owner_directed_schema): runtime.nexloop_evo_review_summary as 0255 last defined it, copied with only 'category' added to each
-- candidate object. Every other field, the definer, search_path, row security, owner and grants are unchanged. Temporary number; the
-- dispatcher renumbers.
create or replace function runtime.nexloop_evo_review_summary(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_items jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select coalesce(jsonb_agg(jsonb_build_object(
   'candidate_id',x.candidate_id,'category',x.category,'status',x.status,'end_reason',x.end_reason,'dimension',x.dimension,'change_kind',p.body->>'change_kind',
   'report_digest',r.report_digest,'level',r.level,'conclusion',r.conclusion,'reason',r.report->>'reason','change_class',r.report->>'change_class',
   'hard_gates_version',coalesce(r.report->>'hard_gates_version',b.bundle->>'hard_gates_version'),
   'gates',(select coalesce(jsonb_agg(jsonb_build_object('gate',g->>'gate','result',g->>'result') order by ord),'[]'::jsonb)
     from jsonb_array_elements(coalesce(r.report->'hard_gates','[]'::jsonb)) with ordinality t(g,ord)),
   'scores',jsonb_build_object(
     'design',jsonb_build_object('cases',r.report->'scores'->'design'->'cases','parent',r.report->'scores'->'design'->'parent','candidate',r.report->'scores'->'design'->'candidate'),
     'validation',jsonb_build_object('cases',r.report->'scores'->'validation'->'cases','parent',r.report->'scores'->'validation'->'parent','candidate',r.report->'scores'->'validation'->'candidate'),
     'ambiguity_rate',r.report->'scores'->'metrics'->'ambiguity_rate','regressions',r.report->'scores'->'metrics'->'regressions',
     'missing_cases',r.report->'scores'->'metrics'->'missing_cases'),
   'non_inferiority',b.bundle->'non_inferiority',
   'cases',cardinality(x.design_case_ids)+cardinality(x.validation_case_ids),
   'cases_digest',encode(sha256(convert_to(array_to_string(array(select c from unnest(x.design_case_ids||x.validation_case_ids) c order by c),','),'UTF8')),'hex'),
   'published_version',(select s.version from ontology.nexloop_semantic_releases s where s.tenant_id=x.tenant_id and s.world=x.world and s.candidate_id=x.candidate_id
     and s.reason='publish' order by s.recorded_at desc limit 1),
   'recorded_at',r.recorded_at) order by r.recorded_at desc),'[]'::jsonb) into v_items
 from (select * from runtime.nexloop_evo_candidates c where c.tenant_id=p_tenant and c.world=p_world
   and exists(select 1 from runtime.nexloop_evo_reports rr where rr.tenant_id=c.tenant_id and rr.world=c.world and rr.candidate_id=c.candidate_id)) x
 join lateral (select * from runtime.nexloop_evo_reports rr where rr.tenant_id=x.tenant_id and rr.world=x.world and rr.candidate_id=x.candidate_id
   order by rr.recorded_at desc limit 1) r on true
 left join runtime.nexloop_evo_bundles b on b.tenant_id=x.tenant_id and b.world=x.world and b.candidate_id=x.candidate_id
 left join runtime.nexloop_evo_patches p on p.tenant_id=x.tenant_id and p.world=x.world and p.patch_id=x.patch_id
 where r.recorded_at>clock_timestamp()-interval '365 days';
 return jsonb_build_object('candidates',(select coalesce(jsonb_agg(e),'[]'::jsonb) from (select e from jsonb_array_elements(v_items) e limit 50) z),
  'stops',coalesce((select jsonb_agg(jsonb_build_object('dimension',d.dimension,'open',true,'stopped',coalesce(s.stopped,false),'revision',coalesce(s.revision,0),
     'changed_at',s.changed_at) order by d.dimension)
    from control.nexloop_evo_open_dimensions d left join control.nexloop_evo_dimension_stops s on s.tenant_id=p_tenant and s.world=p_world and s.dimension=d.dimension),'[]'::jsonb),
  'watch',runtime.nexloop_evo_release_watch(p_tenant,p_world));
end $$;
alter function runtime.nexloop_evo_review_summary(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_evo_review_summary(text,text) from public;
