-- NX-050 follow-up (dispatcher ruling 2026-10-10): the reflow creates an approved instance with the type's
-- CURRENT latest published <Type>.create, after SQL re-checks that (1) the approved identifying properties
-- still exist in the latest Schema with a compatible value (type / closed vocabulary) and every required
-- property is covered, and (2) that create Action is still in the canonical shape of 0101 (low risk, no
-- approval, no policy, bound to exactly the latest version, an ontology.object.create capability). If either
-- fails, the candidate goes back to pending_review with the reasons (audited event, approval released), so
-- it is never stuck waiting; a human decides again.

-- Identity compatibility with the type's latest Schema (shared by the approval gate and the reflow plan).
create function ontology.nexloop_instance_identity_issues(p_tenant text,p_type text,p_identifying jsonb) returns text[]
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare def jsonb;r text[]:='{}';k text;val jsonb;prop jsonb;vt text;
begin
 select d.definition into def from ontology.object_type_versions d where d.tenant_id=p_tenant and d.type_name=p_type order by d.version desc limit 1;
 if def is null then return array['instance_type_unavailable'];end if;
 for k,val in select key,value from jsonb_each(p_identifying) loop
  select e into prop from jsonb_array_elements(def->'properties') e where e->>'property_name'=k;
  if prop is null then r:=array_append(r,'identifying_property_removed:'||k);continue;end if;
  vt:=prop->>'value_type';
  if not ((vt='string' and jsonb_typeof(val)='string') or (vt in ('number','decimal') and jsonb_typeof(val)='number')
     or (vt='integer' and jsonb_typeof(val)='number' and (val#>>'{}')~'^-?[0-9]+$') or (vt='boolean' and jsonb_typeof(val)='boolean'))
   or (jsonb_typeof(prop->'type_descriptor'->'enum')='array' and jsonb_array_length(prop->'type_descriptor'->'enum')>0 and not (prop->'type_descriptor'->'enum')@>jsonb_build_array(val)) then
   r:=array_append(r,'identifying_property_incompatible:'||k);end if;
 end loop;
 for prop in select e from jsonb_array_elements(def->'properties') e where coalesce((e->>'required')::boolean,false) loop
  if not p_identifying ? (prop->>'property_name') then r:=array_append(r,'required_property_not_identified:'||(prop->>'property_name'));end if;
 end loop;
 return r;
end $$;

-- Canonical shape of a type's latest create Action (0101 shape, bound to exactly the given latest version).
create function ontology.nexloop_type_create_action_canonical(p_tenant text,p_type text,p_version integer,p_resource text) returns boolean
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a control.nexloop_action_definitions%rowtype;ref jsonb;cap jsonb;expected jsonb;
 strip text[]:=array['contract_digest','created_at','version','previous_version','object_types','governance'];
begin
 select * into a from control.nexloop_action_definitions where tenant_id=p_tenant and world='real' and resource_id=p_resource and active;
 if not found then return false;end if;
 ref:=a.definition->'object_types'->0;cap:=a.capability;
 expected:=ontology.nexloop_review_type_action_expected(p_tenant,p_type,'create',cap,ref->>'schema_digest');
 return not (cap->>'capability_name' is distinct from 'ontology.object.create' or cap->>'kind' is distinct from 'atomic' or cap->'has_side_effects' is distinct from 'true'::jsonb
   or jsonb_array_length(a.definition->'object_types')<>1 or ref-'schema_digest'-'version' is distinct from (expected->'object_types'->0)-'schema_digest'-'version'
   or (ref->>'version')::integer<>p_version or coalesce(ref->>'schema_digest','')!~'^[0-9a-f]{64}$'
   or a.definition->>'stable_name' is distinct from p_type||'.create'
   or a.definition->'governance'->'change_scope'->'object_types' is distinct from a.definition->'object_types'
   or (a.definition->'governance')-'change_scope' is distinct from (expected->'governance')-'change_scope'
   or (a.definition->'governance'->'change_scope')-'object_types' is distinct from (expected->'governance'->'change_scope')-'object_types'
   or (a.definition-strip) is distinct from (expected-strip));
end $$;

create function ontology.nexloop_instance_create_plan(p_tenant text,ap ontology.nexloop_instance_approvals) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v integer;r text[];resource text;
begin
 select max(d.version) into v from ontology.object_type_versions d where d.tenant_id=p_tenant and d.type_name=ap.type_name;
 if v is null then return jsonb_build_object('reasons',jsonb_build_array('instance_type_unavailable'));end if;
 r:=ontology.nexloop_instance_identity_issues(p_tenant,ap.type_name,ap.identifying);
 select d.resource_id into resource from control.nexloop_action_definitions d where d.tenant_id=p_tenant and d.world='real' and d.active and d.definition->>'stable_name'=ap.type_name||'.create'
  and exists(select 1 from jsonb_array_elements(d.definition->'object_types') o where o->>'stable_name'=ap.type_name and (o->>'version')::integer=v)
  order by (d.definition->>'version')::integer desc limit 1;
 if resource is null then r:=array_append(r,'type_create_action_unavailable');
 elsif not ontology.nexloop_type_create_action_canonical(p_tenant,ap.type_name,v,resource) then r:=array_append(r,'latest_create_action_not_canonical');end if;
 return jsonb_build_object('reasons',to_jsonb(r),'create_action',resource,'type_name',ap.type_name,'properties',ap.identifying,'schema_version',v);
end $$;

-- 0103 reflow verbs unchanged, plus 'instance_plan'.
create or replace function authz.nexloop_review_reflow(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=authz.nexloop_assert_claim_match_authority(p_digest,p_world,p_text,p_signature,p_payload);c jsonb:=p_payload::jsonb;t text:=a->>'tenant_id';
 v_dec ontology.nexloop_review_decisions%rowtype;x ontology.nexloop_candidate_definitions%rowtype;ap ontology.nexloop_instance_approvals%rowtype;
 fp text;claims text[];eligible text[];conv text;open_count integer;grant_wait integer;status text;report jsonb;v_cause text;v_reason text;o ontology.objects%rowtype;
begin
 if c->>'verb'='pending' then
  return coalesce((select jsonb_agg(jsonb_build_object('decision_id',d.decision_id,'candidate_id',d.candidate_id,'outcome',d.outcome,'publication',d.publication,
    'reflow_status',d.reflow_status,'kind',k.kind,'instance_object_id',i.object_id) order by d.decided_at,d.decision_id) from ontology.nexloop_review_decisions d
    join ontology.nexloop_candidate_definitions k on k.tenant_id=d.tenant_id and k.world=d.world and k.candidate_id=d.candidate_id
    left join ontology.nexloop_instance_approvals i on i.tenant_id=d.tenant_id and i.world=d.world and i.candidate_id=d.candidate_id
   where d.tenant_id=t and d.world=p_world and d.reflow_status in ('pending','waiting')),'[]'::jsonb);
 elsif c->>'verb'='record' then
  if c->>'status' not in ('done','waiting') or jsonb_typeof(c->'report') is distinct from 'object' then raise exception 'reflow record invalid' using errcode='22023';end if;
  update ontology.nexloop_review_decisions set reflow_status=c->>'status',reflow_report=c->'report',reflowed_at=clock_timestamp(),
   publication=case when publication is null then null else jsonb_set(publication,'{applied_claim_count}',to_jsonb(coalesce((c->'report'->>'applied')::integer,0))) end,
   record=case when record ? 'publication' then jsonb_set(record,'{publication,applied_claim_count}',to_jsonb(coalesce((c->'report'->>'applied')::integer,0))) else record end
   where tenant_id=t and world=p_world and decision_id=c->>'decision_id' and reflow_status in ('pending','waiting');
  if not found then raise exception 'reflow decision unavailable' using errcode='42501';end if;
  return jsonb_build_object('decision_id',c->>'decision_id','reflow_status',c->>'status');
 elsif c->>'verb'='instance_plan' then
  select * into v_dec from ontology.nexloop_review_decisions where tenant_id=t and world=p_world and decision_id=c->>'decision_id'
   and outcome='published' and reflow_status in ('pending','waiting') for update;
  if not found then raise exception 'reflow decision unavailable' using errcode='42501';end if;
  select * into ap from ontology.nexloop_instance_approvals where tenant_id=t and world=p_world and candidate_id=v_dec.candidate_id for update;
  if not found or ap.object_id is not null then raise exception 'reflow decision unavailable' using errcode='42501';end if;
  report:=ontology.nexloop_instance_create_plan(t,ap);
  if jsonb_array_length(report->'reasons')=0 then return report;end if;
  -- Not creatable as approved any more: back to human review with the reasons (never stuck in waiting).
  select * into x from ontology.nexloop_candidate_definitions where tenant_id=t and world=p_world and candidate_id=ap.candidate_id for update;
  v_reason:=left('returned_to_review: '||(select string_agg(value,', ') from jsonb_array_elements_text(report->'reasons')),500);
  update ontology.nexloop_candidate_definitions set status='pending_review',revision=revision+1,status_reason=v_reason,updated_at=clock_timestamp()
   where tenant_id=t and world=p_world and candidate_id=x.candidate_id and ontology.nexloop_candidate_definitions.status='published';
  if not found then raise exception 'candidate transition conflict' using errcode='40001';end if;
  insert into ontology.nexloop_candidate_events(tenant_id,world,candidate_id,from_status,to_status,actor_kind,actor_ref,reason,details)
   values(t,p_world,x.candidate_id,'published','pending_review','service','reflow:'||v_dec.decision_id,v_reason,
    jsonb_build_object('decision_id',v_dec.decision_id,'reasons',report->'reasons','approved_create_action',ap.create_action));
  delete from ontology.nexloop_instance_approvals where tenant_id=t and world=p_world and candidate_id=ap.candidate_id;
  update ontology.nexloop_review_decisions set reflow_status='done',reflowed_at=clock_timestamp(),
   reflow_report=jsonb_build_object('applied',0,'returned_to_review',true,'reasons',report->'reasons')
   where tenant_id=t and world=p_world and decision_id=v_dec.decision_id;
  return jsonb_build_object('returned_to_review',true,'reasons',report->'reasons','status','done');
 elsif c->>'verb'='instance_created' then
  select * into v_dec from ontology.nexloop_review_decisions where tenant_id=t and world=p_world and decision_id=c->>'decision_id'
   and outcome='published' and reflow_status in ('pending','waiting') for update;
  if not found then raise exception 'reflow decision unavailable' using errcode='42501';end if;
  select * into ap from ontology.nexloop_instance_approvals where tenant_id=t and world=p_world and candidate_id=v_dec.candidate_id for update;
  if not found then raise exception 'reflow decision unavailable' using errcode='42501';end if;
  select * into o from ontology.objects where tenant_id=t and world=p_world and type_name=ap.type_name and object_id=c->>'object_id';
  -- Only the governed object carrying exactly the approved identity can be bound.
  if not found or not (o.properties@>ap.identifying) or (ap.object_id is not null and ap.object_id<>o.object_id) then
   raise exception 'approved instance mismatch' using errcode='42501';end if;
  update ontology.nexloop_instance_approvals set object_id=o.object_id,created_at=coalesce(created_at,clock_timestamp())
   where tenant_id=t and world=p_world and candidate_id=ap.candidate_id;
  return jsonb_build_object('object_id',o.object_id,'type_name',ap.type_name);
 elsif c->>'verb' in ('release','release_type') then
  select * into v_dec from ontology.nexloop_review_decisions where tenant_id=t and world=p_world and decision_id=c->>'decision_id'
   and outcome='published' and reflow_status in ('pending','waiting') for update;
  if not found then raise exception 'reflow decision unavailable' using errcode='42501';end if;
  select * into x from ontology.nexloop_candidate_definitions where tenant_id=t and world=p_world and candidate_id=v_dec.candidate_id;
  if (c->>'verb'='release_type' and x.kind is distinct from 'object_type') or x.kind not in ('object_type','object_instance','property','vocabulary_value') then
   raise exception 'reflow decision unavailable' using errcode='42501';end if;
  if x.kind='object_instance' and not exists(select 1 from ontology.nexloop_instance_approvals i where i.tenant_id=t and i.world=p_world
    and i.candidate_id=x.candidate_id and i.object_id is not null) then
   raise exception 'approved instance not created yet' using errcode='42501';end if;
  v_cause:=case x.kind when 'object_type' then 'type:' when 'object_instance' then 'instance:' else 'definition:' end||x.candidate_id;
  fp:=authz.nexloop_tenant_authority_fingerprint(t);
  select coalesce(array_agg(substr(v,7) order by v),'{}') into claims from unnest(x.dependent_claims) v where v like 'claim:%';
  -- Property / vocabulary publications re-point consumer Claims deterministically (0082); only entity Claims are released here.
  if x.kind in ('property','vocabulary_value') then
   select coalesce(array_agg(cl.claim_id),'{}') into claims from ontology.nexloop_claims cl where cl.tenant_id=t and cl.world=p_world
    and cl.claim_id=any(claims) and cl.subject_kind<>'consumer';
  end if;
  -- First release per cause: Claims still waiting on this candidate. Again: a Claim the matcher left in
  -- needs_resolution under this cause, only after the authority changed.
  select coalesce(array_agg(cl.claim_id order by cl.claim_id),'{}') into eligible from ontology.nexloop_claims cl
   left join ontology.nexloop_claim_rematch r on r.tenant_id=cl.tenant_id and r.world=cl.world and r.claim_id=cl.claim_id
   where cl.tenant_id=t and cl.world=p_world and cl.claim_id=any(claims)
    and ((cl.resolution_state='awaiting_definition' and (r.claim_id is null or r.cause<>v_cause))
     or (r.cause=v_cause and cl.resolution_state='needs_resolution' and r.authority_fingerprint<>fp));
  if cardinality(eligible)>0 then
   update ontology.nexloop_claims set resolution_state='unresolved' where tenant_id=t and world=p_world and claim_id=any(eligible);
   insert into ontology.nexloop_claim_rematch as r(tenant_id,world,claim_id,generation,cause,authority_fingerprint)
    select t,p_world,e,1,v_cause,fp from unnest(eligible) e
   on conflict(tenant_id,world,claim_id) do update set generation=r.generation+1,cause=excluded.cause,authority_fingerprint=excluded.authority_fingerprint,released_at=clock_timestamp();
   for conv in select distinct cl.conversation_id from ontology.nexloop_claims cl where cl.tenant_id=t and cl.world=p_world and cl.claim_id=any(eligible) loop
    perform authz.nexloop_work_feed_touch(t,p_world,'claim-match',conv,jsonb_build_object('conversation_id',conv));
   end loop;
  end if;
  select count(*) filter(where cl.resolution_state='unresolved'),count(*) filter(where cl.resolution_state='needs_resolution')
   into open_count,grant_wait from ontology.nexloop_claims cl where cl.tenant_id=t and cl.world=p_world and cl.claim_id=any(claims);
  status:=case when open_count+grant_wait=0 then 'done' else 'waiting' end;
  report:=jsonb_build_object('applied',0,'released',to_jsonb(eligible),'claims',to_jsonb(claims),
   'reason',case when open_count>0 then 'awaiting_matching' when grant_wait>0 then 'awaiting_grants' else null end);
  -- Property / vocabulary decisions keep their 0082 reflow status (consumer Claims may still wait for grants).
  if x.kind in ('object_type','object_instance') then
   update ontology.nexloop_review_decisions set reflow_status=status,reflow_report=jsonb_strip_nulls(report),reflowed_at=clock_timestamp()
    where tenant_id=t and world=p_world and decision_id=v_dec.decision_id;
  end if;
  return jsonb_strip_nulls(report)||jsonb_build_object('status',status);
 end if;
 raise exception 'reflow verb invalid' using errcode='22023';
end $$;

-- 0103 instance gates plus the identity compatibility of 0104: an identity the latest Schema cannot hold (type,
-- closed vocabulary, uncovered required property) is refused at approval, so approve → reflow → return cannot loop.
create or replace function ontology.nexloop_review_instance_gates(p_tenant text,p_world text,x ontology.nexloop_candidate_definitions,out failures text[],out create_action text)
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare owner text:=ontology.nexloop_review_instance_type(x.candidate);ip jsonb:=x.candidate->'proposed'->'identifying_properties';v integer;def jsonb;k text;val jsonb;
begin
 failures:='{}';
 if p_world<>'real' then failures:=array['simulation_or_shadow_candidate_cannot_create_real_instance'];return;end if;
 if x.kind<>'object_instance' or owner is null then failures:=array['invalid_type_reference'];return;end if;
 select d.version,d.definition into v,def from ontology.object_type_versions d where d.tenant_id=p_tenant and d.type_name=owner order by d.version desc limit 1;
 if v is null then failures:=array['instance_type_unavailable'];return;end if;
 if jsonb_typeof(ip) is distinct from 'object' or (select count(*) from jsonb_object_keys(ip)) not between 1 and 16 then failures:=array['identifying_properties_invalid'];return;end if;
 for k,val in select key,value from jsonb_each(ip) loop
  if not exists(select 1 from jsonb_array_elements(def->'properties') e where e->>'property_name'=k) then
   failures:=array_append(failures,'identifying_property_not_published:'||k);
  elsif jsonb_typeof(val) not in ('string','number') or btrim(val#>>'{}')='' then failures:=array_append(failures,'identifying_value_invalid:'||k);end if;
 end loop;
 select a.resource_id into create_action from control.nexloop_action_definitions a where a.tenant_id=p_tenant and a.world='real' and a.active
  and a.definition->>'stable_name'=owner||'.create'
  and exists(select 1 from jsonb_array_elements(a.definition->'object_types') r where r->>'stable_name'=owner and (r->>'version')::integer=v)
  order by (a.definition->>'version')::integer desc limit 1;
 if create_action is null then failures:=array_append(failures,'type_create_action_unavailable');end if;
 failures:=failures||array(select issue from unnest(ontology.nexloop_instance_identity_issues(p_tenant,owner,ip)) issue
  where issue like 'identifying_property_incompatible:%' or issue like 'required_property_not_identified:%');
 if create_action is not null and not ontology.nexloop_type_create_action_canonical(p_tenant,owner,v,create_action) then
  failures:=array_append(failures,'latest_create_action_not_canonical');end if;
 if cardinality(failures)>0 then return;end if;
 -- One object per approved identity.
 if exists(select 1 from ontology.objects o where o.tenant_id=p_tenant and o.world=p_world and o.type_name=owner and o.properties@>ip) then
  failures:=array_append(failures,'instance_already_exists');end if;
 if exists(select 1 from ontology.nexloop_instance_approvals a where a.tenant_id=p_tenant and a.world=p_world and a.type_name=owner
   and (a.identifying=ip or a.dedupe_key=x.dedupe_key) and a.candidate_id<>x.candidate_id) then
  failures:=array_append(failures,'instance_already_approved');end if;
end $$;

do $grants$
declare f text;
begin
 foreach f in array array['ontology.nexloop_instance_identity_issues(text,text,jsonb)','ontology.nexloop_type_create_action_canonical(text,text,integer,text)','ontology.nexloop_instance_create_plan(text,ontology.nexloop_instance_approvals)',
  'ontology.nexloop_review_instance_gates(text,text,ontology.nexloop_candidate_definitions)','authz.nexloop_review_reflow(text,text,text,text,text)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';
  execute 'revoke all on function '||f||' from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator';
 end loop;
end $grants$;
grant execute on function authz.nexloop_review_reflow(text,text,text,text,text) to nexloop_domain_worker;
