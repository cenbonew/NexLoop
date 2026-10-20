-- NX-040 F2, migration C: the ontology_schema window for proposal input only (docs/implementation/NX-040-design.md §3 step 1, §9;
-- D21, owner 2026-10-20). The migration number lives only in the file name and the catalog; nothing below repeats it.
-- 1. NX-038 E4 candidates may carry the dimension ontology_schema (the check is widened from semantic_content alone).
-- 2. authz.nexloop_evo_candidate_propose (0235 body copied; changes marked "NX-040 D21"): an ontology_schema candidate is accepted as
--    the input of a Schema proposal, narrowly: its scope names only eios:object_type: / eios:property: refs and its patch is one §4.1
--    whitelist change kind (add_optional_property, add_vocabulary_value, add_object_type), ids and hashes only as before.
-- 3. Not opened here: evaluation. NX-039's open dimension list (control.nexloop_evo_open_dimensions) still holds semantic_content
--    only, so opening an evaluation of an ontology_schema candidate is refused dimension_closed until F4 delivers G9-G13 and a
--    migration adds the dimension there. semantic_tool stays closed everywhere.

alter table runtime.nexloop_evo_candidates drop constraint nexloop_evo_candidates_dimension_check;
alter table runtime.nexloop_evo_candidates add constraint nexloop_evo_candidates_dimension_check check(dimension in ('semantic_content','ontology_schema'));

create or replace function authz.nexloop_evo_candidate_propose(p_digest text,p_body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text:=runtime.nexloop_evo_writer_tenant(p_digest);b jsonb:=p_body;c jsonb;p jsonb;d runtime.nexloop_evo_diagnoses;x runtime.nexloop_evo_candidates;
 v_design text[];v_validation text[];v_scope text[];v_targets text[];v_terms text[];v_patch_runs text[];v_sources text[];v_runs text[];v_patch uuid;
 v_cost numeric;
begin
 c:=b->'candidate';p:=b->'patch';
 if jsonb_typeof(b) is distinct from 'object' or (select array_agg(k order by k) from jsonb_object_keys(b) k)
   is distinct from array['candidate','case_groups','category','diagnosis_id','patch','regroups','source_consumer_ids']
  or jsonb_typeof(c) is distinct from 'object' or jsonb_typeof(p) is distinct from 'object' or jsonb_typeof(b->'case_groups') is distinct from 'object'
  or jsonb_typeof(b->'source_consumer_ids') is distinct from 'array' or jsonb_typeof(b->'regroups') is distinct from 'number'
  or coalesce(b->>'diagnosis_id','')!~'^[0-9a-f-]{36}$' or coalesce(c->>'candidate_id','')!~'^[0-9a-f-]{36}$'
  or c->>'schema_version' is distinct from '1.0' or c->'publication_permit_ref' is distinct from 'null'::jsonb or c->>'status' is distinct from 'proposed'
  or jsonb_typeof(c->'maximum_rounds') is distinct from 'number' or jsonb_typeof(c->'design_case_ids') is distinct from 'array'
  or jsonb_typeof(c->'validation_case_ids') is distinct from 'array' or jsonb_typeof(c->'allowed_change_scope') is distinct from 'array' then
  raise exception 'evolution candidate invalid' using errcode='22023';end if;
 -- NX-040 D21: ontology_schema opens as a proposal input only (evaluation of that dimension stays closed: NX-039's open dimension
 -- list, dimension_closed); every other dimension stays closed.
 if c->>'dimension' is null or c->>'dimension' not in ('semantic_content','ontology_schema') then
  raise exception 'evolution dimension % not open',c->>'dimension' using errcode='22023';end if;  -- D19
 if (c->>'maximum_rounds')::numeric not in (1,2,3) then raise exception 'evolution candidate round budget over 3' using errcode='22023';end if;  -- D20
 select * into d from runtime.nexloop_evo_diagnoses where tenant_id=v_tenant and world='real' and diagnosis_id=(b->>'diagnosis_id')::uuid for update;
 if not found then raise exception 'evolution diagnosis unknown' using errcode='22023';end if;
 select * into x from runtime.nexloop_evo_candidates where tenant_id=v_tenant and world='real' and candidate_id=(c->>'candidate_id')::uuid;
 if found then
  if x.diagnosis_id<>d.diagnosis_id then raise exception 'evolution candidate id reused' using errcode='22023';end if;
  return jsonb_build_object('proposed',true,'replayed',true,'candidate',runtime.nexloop_evo_candidate_wire(x));
 end if;
 if d.status<>'open' then raise exception 'evolution diagnosis %',d.status using errcode='22023';end if;
 if clock_timestamp()>=d.deadline then  -- §12.2: the diagnosis wall clock is spent
  perform runtime.nexloop_evo_diagnosis_incomplete(v_tenant,d.diagnosis_id,'budget_exhausted','status_changed',jsonb_build_object('tier','diagnosis_time'),'service:evolution');
  return jsonb_build_object('proposed',false,'reason','budget_exhausted','tier','diagnosis_time');
 end if;
 if c->>'tenant_id' is distinct from v_tenant or c->>'input_snapshot_digest' is distinct from d.input_snapshot_digest
  or c->>'parent_revision' is distinct from d.parent_revision or c->>'evaluator_version' is distinct from d.evaluator_version
  or c->>'model_profile' is distinct from d.model_profile then
  raise exception 'evolution candidate differs from its frozen diagnosis' using errcode='22023';end if;
 v_design:=array(select e#>>'{}' from jsonb_array_elements(c->'design_case_ids') e);
 v_validation:=array(select e#>>'{}' from jsonb_array_elements(c->'validation_case_ids') e);
 v_scope:=array(select e#>>'{}' from jsonb_array_elements(c->'allowed_change_scope') e);
 v_runs:=array(select r::text from unnest(d.run_ids) r);
 if cardinality(v_design) not between 1 and 256 or cardinality(v_validation) not between 1 and 256 or cardinality(v_scope) not between 1 and 256
  or (select count(distinct e) from unnest(v_design||v_validation) e)<>cardinality(v_design)+cardinality(v_validation)
  or not (v_design||v_validation)<@v_runs then
  raise exception 'evolution candidate cases are not the diagnosis Runs' using errcode='22023';end if;
 if exists(select 1 from unnest(v_scope) s where s!~'^(eios:object_type:|eios:property:|nexloop:vocabulary:)[^[:cntrl:][:space:]]+$' or char_length(s)>512) then
  raise exception 'evolution change scope not allowed' using errcode='42501';end if;  -- AT-024
 -- NX-040 D21: an ontology_schema candidate names Object Types and Properties only (no vocabulary-only ref, no Action, grant or tool).
 if c->>'dimension'='ontology_schema' and exists(select 1 from unnest(v_scope) s where s!~'^eios:(object_type|property):') then
  raise exception 'evolution change scope not allowed' using errcode='42501';end if;
 -- AT-048: every case has a cluster, and no cluster is on both sides.
 if (select array_agg(k order by k) from jsonb_object_keys(b->'case_groups') k) is distinct from (select array_agg(e order by e) from unnest(v_design||v_validation) e)
  or exists(select 1 from jsonb_each(b->'case_groups') g where jsonb_typeof(g.value)<>'string' or g.value#>>'{}'!~'^[A-Za-z0-9:._-]{1,190}$') then
  raise exception 'evolution case groups invalid' using errcode='22023';end if;
 if exists(select 1 from unnest(v_design) a join unnest(v_validation) v on (b->'case_groups'->>a)=(b->'case_groups'->>v)) then
  raise exception 'evolution validation leak: a cluster is on both sides' using errcode='22023';end if;
 -- The patch: ids, hashes and refs only; no free text, no Action, grant or tool.
 if (select array_agg(k order by k) from jsonb_object_keys(p) k) is distinct from array['change_kind','run_ids','target_refs','term_hashes']
  or p->>'change_kind' is null
  or p->>'change_kind' <> all(case when c->>'dimension'='ontology_schema' then array['add_optional_property','add_vocabulary_value','add_object_type']
    else array['alias','description'] end)  -- NX-040 §4.1 whitelist for ontology_schema
  or jsonb_typeof(p->'target_refs') is distinct from 'array' or jsonb_typeof(p->'term_hashes') is distinct from 'array' or jsonb_typeof(p->'run_ids') is distinct from 'array' then
  raise exception 'evolution patch invalid' using errcode='22023';end if;
 v_targets:=array(select e#>>'{}' from jsonb_array_elements(p->'target_refs') e);
 v_terms:=array(select e#>>'{}' from jsonb_array_elements(p->'term_hashes') e);
 v_patch_runs:=array(select e#>>'{}' from jsonb_array_elements(p->'run_ids') e);
 if cardinality(v_targets)<1 or not v_targets<@v_scope or cardinality(v_terms)>16 or array_to_string(v_terms,',')!~'^([0-9a-f]{64}(,[0-9a-f]{64})*)?$'
  or cardinality(v_patch_runs)<1 or not v_patch_runs<@v_runs then
  raise exception 'evolution patch invalid' using errcode='22023';end if;
 v_sources:=array(select distinct e#>>'{}' from jsonb_array_elements(b->'source_consumer_ids') e order by 1);
 if not v_sources<@d.consumer_ids then raise exception 'evolution patch source is not in the diagnosis' using errcode='22023';end if;
 if exists(select 1 from unnest(v_sources) s where runtime.nexloop_consumer_erasing(v_tenant,'real',s)) then
  raise exception 'evolution patch source erased' using errcode='22023';end if;
 v_patch:=gen_random_uuid();
 v_cost:=least(10,d.cost_limit);
 insert into runtime.nexloop_evo_patches(tenant_id,world,patch_id,candidate_id,body,body_digest)
  values(v_tenant,'real',v_patch,(c->>'candidate_id')::uuid,p,encode(sha256(convert_to(p::text,'UTF8')),'hex'));
 insert into runtime.nexloop_evo_patch_sources(tenant_id,world,patch_id,consumer_id) select v_tenant,'real',v_patch,s from unnest(v_sources) s;
 insert into runtime.nexloop_evo_candidates(tenant_id,world,candidate_id,diagnosis_id,category,dimension,parent_revision,candidate_revision,primary_hypothesis,patch_id,
   input_snapshot_digest,design_case_ids,validation_case_ids,case_groups,regroups,evaluator_version,model_profile,maximum_rounds,allowed_change_scope,status,cost_limit,unit)
  values(v_tenant,'real',(c->>'candidate_id')::uuid,d.diagnosis_id,b->>'category',c->>'dimension',d.parent_revision,c->>'candidate_revision',c->>'primary_hypothesis',v_patch,
   d.input_snapshot_digest,v_design,v_validation,b->'case_groups',(b->>'regroups')::integer,d.evaluator_version,d.model_profile,(c->>'maximum_rounds')::integer,v_scope,
   'proposed',v_cost,d.unit) returning * into x;
 perform runtime.nexloop_evo_event(v_tenant,d.diagnosis_id,x.candidate_id,'proposed',null,'proposed',null,
  jsonb_build_object('category',x.category,'design',cardinality(v_design),'validation',cardinality(v_validation),'regroups',x.regroups,'sources',cardinality(v_sources)),
  'service:evolution');
 return jsonb_build_object('proposed',true,'replayed',false,'candidate',runtime.nexloop_evo_candidate_wire(x));
end $$;
