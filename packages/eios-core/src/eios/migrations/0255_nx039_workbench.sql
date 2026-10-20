-- NX-039 V6 workbench (design §11 V6; owner decisions D39-7, D39-8, D39-10, D39-11, D39-14). The migration number lives only in the
-- file name and the catalog (renumbered at integration); nothing below repeats it.
-- 1. Governed owner Action semantic_release.stop / set_evolution_stop: stop (or resume) evolution for one dimension of the tenant,
--    with a reason (§8.5). It writes the existing control.nexloop_evo_dimension_stops row (V1), which evaluation opening and V4
--    publication already check (dimension_stopped); nothing else. Like publish and rollback it is the release scope's Action
--    (D39-8: owner by default, never a service or an Agent), so the same role check gives the three buttons.
-- 2. Workbench observe verb semantic_review (owner only, as semantic_releases): for each candidate with a report, the latest report's
--    conclusion, each frozen gate's result (gate and pass/fail only - no evidence ids), the L0/L1 scores (counts and rates), the
--    frozen non-inferiority rule, the change class classify_change wrote (D39-7: shown, never set here), the report digest, and
--    the case count with a digest of the case set (D39-11: no Run or Consumer id); the dimension stops; the 7-day release watch
--    (V5 runtime.nexloop_evo_release_watch, D39-10: a warning and a rollback suggestion, never an automatic rollback).
-- Nothing here reads a message, a Claim value or an alias text.

-- 1. The stop switch ---------------------------------------------------------------------------------------------------------------
create function control.nexloop_governed_set_evolution_stop(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_dim text:=body->>'dimension';v_stop boolean;cur control.nexloop_evo_dimension_stops;v_rev bigint;
begin
 if jsonb_typeof(body) is distinct from 'object' or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','dimension','stopped','reason'))
  or v_dim not in ('semantic_content','semantic_tool','ontology_schema') or v_dim is null or jsonb_typeof(body->'stopped') is distinct from 'boolean'
  or jsonb_typeof(body->'reason') is distinct from 'string' or char_length(btrim(body->>'reason')) not between 1 and 500 then
  raise exception 'evolution stop: invalid' using errcode='22023';end if;
 v_stop:=(body->>'stopped')::boolean;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into cur from control.nexloop_evo_dimension_stops where tenant_id=p_tenant and world=p_world and dimension=v_dim for update;
 if found and cur.stopped=v_stop then
  return jsonb_build_object('dimension',v_dim,'stopped',v_stop,'revision',cur.revision,'changed',false);end if;
 v_rev:=coalesce(cur.revision,0)+1;
 insert into control.nexloop_evo_dimension_stops(tenant_id,world,dimension,stopped,revision,changed_by) values(p_tenant,p_world,v_dim,v_stop,v_rev,p_principal)
  on conflict(tenant_id,world,dimension) do update set stopped=excluded.stopped,revision=excluded.revision,changed_by=excluded.changed_by,changed_at=clock_timestamp();
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('evolution-stop:'||v_dim||':'||v_rev,'nexloop.semantic_release.'||case when v_stop then 'stopped' else 'resumed' end,p_tenant,p_principal,'evolution',p_intent,
   jsonb_build_object('dimension',v_dim,'stopped',v_stop,'revision',v_rev,'reason',btrim(body->>'reason')),clock_timestamp());
 return jsonb_build_object('dimension',v_dim,'stopped',v_stop,'revision',v_rev,'changed',true);
end $$;
alter function control.nexloop_governed_set_evolution_stop(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_governed_set_evolution_stop(text,text,text,text,text,jsonb) from public;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('semantic_release.stop','set_evolution_stop','human','control.nexloop_governed_set_evolution_stop(text,text,text,text,text,jsonb)','NX-039');

-- 2. The review read ----------------------------------------------------------------------------------------------------------------
-- One report as the review queue shows it: numbers, codes and digests only.
create function runtime.nexloop_evo_review_summary(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_items jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select coalesce(jsonb_agg(jsonb_build_object(
   'candidate_id',x.candidate_id,'status',x.status,'end_reason',x.end_reason,'dimension',x.dimension,'change_kind',p.body->>'change_kind',
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

alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) rename to nexloop_workbench_observe_read_before_semantic_review;
revoke all on function authz.nexloop_workbench_observe_read_before_semantic_review(text,text,text,text,text) from public,nexloop_api;
create function authz.nexloop_workbench_observe_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;w jsonb;cfg control.nexloop_workbench_configurations;v_tenant text;
 v_action text:='eios:action:nexloop.workbench.read:1';
begin
 if c->>'verb' is distinct from 'semantic_review' then
  return authz.nexloop_workbench_observe_read_before_semantic_review(p_digest,p_world,p_text,p_signature,p_payload);end if;
 -- The 0133 verification, for this verb (as the earlier verbs).
 if octet_length(p_text)>1048576 or octet_length(p_payload)>4096
  or a->>'protocol' is distinct from 'nexloop-workbench-observe-v1' or a->>'operation' is distinct from 'execute'
  or a->>'action_resource' is distinct from v_action or a->>'resource_id' is distinct from v_action
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'workbench observe unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-workbench-observe-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'workbench observe unavailable' using errcode='42501';end if;
 -- Owner only, as semantic_releases (D39-8).
 w:=authz.nexloop_workbench_member(p_digest,p_world);v_tenant:=w->>'tenant_id';cfg:=control.nexloop_workbench_current(v_tenant);
 if a->>'tenant_id' is distinct from v_tenant or a->>'principal_id' is distinct from w->>'principal_id' or not (cfg.roles->(w->>'role') ? v_action)
  or w->>'role'<>'owner' then
  raise exception 'workbench observe forbidden' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 if (select count(*) from jsonb_object_keys(c))<>1 then raise exception 'workbench observe invalid' using errcode='22023';end if;
 return runtime.nexloop_evo_review_summary(v_tenant,p_world);
end $$;
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_observe_read(text,text,text,text,text) from public;
grant execute on function authz.nexloop_workbench_observe_read(text,text,text,text,text) to nexloop_api;
