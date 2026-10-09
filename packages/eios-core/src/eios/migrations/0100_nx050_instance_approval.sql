-- NX-050: governed human approval of object_instance candidates and reflow creation.
-- Approve (human review Action, same checks as 0082) records the approved identity only: the type's latest
-- Schema must already publish every identifying property, the type must have an active <Type>.create
-- Action bound to that version, and no object or earlier approval may carry the same identity. Nothing is
-- written to the formal store in the decision; no authority fact is written (fingerprint gate).
-- The service reflow then creates the object through <Type>.create (governed, granted by trusted
-- configuration), records it here, and releases the dependent Claims to the four-layer matcher (0099
-- rematch generation + claim-match feed). A later name-only match of the same identity reuses the approved
-- object instead of staging a second candidate. Property / vocabulary publications release their
-- non-consumer (entity) dependent Claims the same way.

create table ontology.nexloop_instance_approvals (
 tenant_id text not null,world text not null,candidate_id text not null,decision_id text not null,type_name text not null,
 dedupe_key text not null,identifying jsonb not null check(jsonb_typeof(identifying)='object'),create_action text not null,
 object_id text check(object_id is null or object_id~'^[0-9a-f]{64}$'),approved_at timestamptz not null default clock_timestamp(),created_at timestamptz,
 primary key(tenant_id,world,candidate_id),unique(tenant_id,world,type_name,dedupe_key),check((object_id is null)=(created_at is null)),
 foreign key(tenant_id,world,candidate_id) references ontology.nexloop_candidate_definitions,
 foreign key(tenant_id,world,decision_id) references ontology.nexloop_review_decisions
);
alter table ontology.nexloop_instance_approvals owner to nexloop_owner;
alter table ontology.nexloop_instance_approvals enable row level security;
alter table ontology.nexloop_instance_approvals force row level security;
create policy tenant_boundary on ontology.nexloop_instance_approvals to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on ontology.nexloop_instance_approvals from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;

create function ontology.nexloop_review_instance_type(p_candidate jsonb) returns text
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select substring(p_candidate->'proposed'->>'type_ref' from '^eios:object_type:([A-Za-z][A-Za-z0-9_]*)$')
$$;

-- Gates for an instance approval; the matching create Action resource is returned through p_action.
create function ontology.nexloop_review_instance_gates(p_tenant text,p_world text,x ontology.nexloop_candidate_definitions,out failures text[],out create_action text)
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
 if cardinality(failures)>0 then return;end if;
 -- One object per approved identity.
 if exists(select 1 from ontology.objects o where o.tenant_id=p_tenant and o.world=p_world and o.type_name=owner and o.properties@>ip) then
  failures:=array_append(failures,'instance_already_exists');end if;
 if exists(select 1 from ontology.nexloop_instance_approvals a where a.tenant_id=p_tenant and a.world=p_world and a.type_name=owner
   and (a.identifying=ip or a.dedupe_key=x.dedupe_key) and a.candidate_id<>x.candidate_id) then
  failures:=array_append(failures,'instance_already_approved');end if;
end $$;

-- Review decision: instance approvals are handled here; everything else is the 0082 path unchanged.
alter function authz.nexloop_review_decide(text,text,text,text,text) rename to nexloop_review_decide_v0082;
revoke all on function authz.nexloop_review_decide_v0082(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator;
create function authz.nexloop_review_decide(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;ident jsonb;t text;principal text;x ontology.nexloop_candidate_definitions%rowtype;
 prior ontology.nexloop_review_decisions%rowtype;outcome text;reflow text:='none';gates text[];action text;pub jsonb;authority jsonb;fp_before text;
 owner text;v integer;rev bigint;now_ts timestamptz:=clock_timestamp();record jsonb;effects jsonb;
begin
 -- Route on the claimed tenant only; the instance path below re-verifies everything before acting.
 perform set_config('eios.tenant_id',coalesce(a->>'tenant_id',''),true);
 if c->>'decision' is distinct from 'approve' or not exists(select 1 from ontology.nexloop_candidate_definitions d where d.tenant_id=a->>'tenant_id'
   and d.world=p_world and d.candidate_id=c->>'candidate_id' and d.kind='object_instance') then
  return authz.nexloop_review_decide_v0082(p_digest,p_world,p_text,p_signature,p_payload);
 end if;
 if session_user<>'nexloop_api' or p_world is null or octet_length(p_text)>1048576 or octet_length(p_payload)>2097152
  or a->>'protocol' is distinct from 'nexloop-review-decision-v1' or a->>'operation' is distinct from 'execute'
  or a->>'action_resource' is distinct from 'eios:action:ontology.schema.review:1'
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'review decision unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-review-decision-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'review decision unavailable' using errcode='42501';end if;
 if not exists(select 1 from authz.nexloop_browser_token_realms where token_digest=p_digest) then
  raise exception 'review decisions require a human session' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ident->'binding'->>'subject_kind' is distinct from 'human' or ident->'binding'->>'tenant_id' is distinct from a->>'tenant_id'
  or ident->'binding'->>'subject_principal_id' is distinct from a->>'principal_id' then
  raise exception 'review decisions require a human session' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 t:=a->>'tenant_id';principal:=a->>'principal_id';
 perform set_config('eios.tenant_id',t,true);
 if c->>'decision_id'!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' or c->>'decision_id' is null
  or jsonb_typeof(c->'expected_revision') is distinct from 'number' or char_length(coalesce(c->>'rationale',''))not between 1 and 2000 or c ? 'merge_target_ref' then
  raise exception 'review decision invalid' using errcode='22023';end if;
 select * into prior from ontology.nexloop_review_decisions where tenant_id=t and world=p_world and decision_id=c->>'decision_id';
 if found then
  if prior.candidate_id<>c->>'candidate_id' or prior.decision<>c->>'decision' or prior.reviewer_principal<>principal then
   raise exception 'review decision id reused' using errcode='40001';end if;
  return jsonb_build_object('replay',true,'decision_id',prior.decision_id,'outcome',prior.outcome,'record',prior.record,'publication',prior.publication,'reflow_status',prior.reflow_status);
 end if;
 select * into x from ontology.nexloop_candidate_definitions where tenant_id=t and world=p_world and candidate_id=c->>'candidate_id' for update;
 if not found then raise exception 'candidate unavailable' using errcode='42501';end if;
 if x.status<>'pending_review' or x.revision<>(c->>'expected_revision')::bigint then
  raise exception 'candidate changed; reload the review item' using errcode='40001';end if;
 authority:=jsonb_build_object('facts',a->'facts','directory_hash',a->>'directory_hash','credential_id',a->>'credential_id');
 owner:=ontology.nexloop_review_instance_type(x.candidate);
 select max(version) into v from ontology.object_type_versions where tenant_id=t and type_name=owner;
 select g.failures,g.create_action into gates,action from ontology.nexloop_review_instance_gates(t,p_world,x) g;
 if cardinality(gates)>0 then
  update ontology.nexloop_candidate_definitions set status_reason=left('publication_failed: '||array_to_string(gates,', '),500),updated_at=now_ts
   where tenant_id=t and world=p_world and candidate_id=x.candidate_id;
  outcome:='publication_failed';
  pub:=jsonb_build_object('schema_revision_before',coalesce(owner,'unknown')||'@'||coalesce(v::text,'none'),'gate_failures',to_jsonb(gates),'applied_claim_count',0);
 else
  fp_before:=authz.nexloop_tenant_authority_fingerprint(t);
  rev:=ontology.nexloop_candidate_transition(t,p_world,x.candidate_id,x.revision,'pending_review','published','human','human:'||principal,left(c->>'rationale',500),
   jsonb_build_object('decision_id',c->>'decision_id'));
  select coalesce(jsonb_agg(substr(d,7) order by d),'[]'::jsonb) into effects from unnest(x.dependent_claims) d where d like 'claim:%';
  pub:=jsonb_build_object('schema_revision_before',owner||'@'||v,'schema_revision_after',owner||'@'||v,
   'published_refs',jsonb_build_array('candidate:'||x.candidate_id,action),'applied_claim_count',0,'gate_failures','[]'::jsonb,
   'instance',jsonb_build_object('type_name',owner,'properties',x.candidate->'proposed'->'identifying_properties','create_action',action),'waiting_claims',effects);
  outcome:='published';reflow:='pending';
 end if;
 record:=jsonb_build_object('schema_version','1.0','decision_id',c->>'decision_id','tenant_id',t,'world_id',p_world,'mode',case when p_world='real' then 'real' else 'test' end,
  'candidate_ref','candidate:'||x.candidate_id,'reviewer_ref','human:'||principal,'decision','approve','rationale',c->>'rationale',
  'expected_candidate_status','pending_review','decided_at',to_char(now_ts at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
  'publication',pub-'instance'-'waiting_claims');
 insert into ontology.nexloop_review_decisions values(t,p_world,c->>'decision_id',x.candidate_id,'approve',outcome,'human:'||principal,principal,
  (c->>'expected_revision')::bigint,c->>'rationale',null,record,pub,authority,reflow,null,now_ts,null);
 if outcome='published' then
  insert into ontology.nexloop_instance_approvals(tenant_id,world,candidate_id,decision_id,type_name,dedupe_key,identifying,create_action)
   values(t,p_world,x.candidate_id,c->>'decision_id',owner,x.dedupe_key,x.candidate->'proposed'->'identifying_properties',action);
  if authz.nexloop_tenant_authority_fingerprint(t) is distinct from fp_before then
   raise exception 'publication must not change authority' using errcode='42501';end if;
 end if;
 return jsonb_build_object('replay',false,'decision_id',c->>'decision_id','outcome',outcome,'record',record,'publication',pub,'reflow_status',reflow);
end $$;

-- 0070/0099 read verbs unchanged, plus 'approved_instance': the object created for an approved name-only identity.
create or replace function authz.nexloop_read_claim_matching(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=authz.nexloop_assert_claim_match_authority(p_digest,p_world,p_text,p_signature,p_payload);c jsonb:=p_payload::jsonb;t text:=a->>'tenant_id';
begin
 if c->>'verb'='proposal' then
  return (select to_jsonb(p)-'tenant_id' from ontology.nexloop_mutation_proposals p where p.tenant_id=t and p.world=p_world and p.proposal_id=c->>'proposal_id');
 elsif c->>'verb'='latest_applied' then
  -- Newest applied evidence for one formal property (late-evidence rule, docs/04 §7.3).
  return (select jsonb_build_object('proposal_id',p.proposal_id,'claim_id',p.claim_id,'effective_at',p.effective_at)
   from ontology.nexloop_mutation_proposals p where p.tenant_id=t and p.world=p_world and p.target_ref=c->>'target_ref'
    and p.property_name=c->>'property_name' and p.status='applied' order by p.effective_at desc,p.updated_at desc limit 1);
 elsif c->>'verb'='candidates' then
  return coalesce((select jsonb_agg(to_jsonb(x)-'tenant_id' order by x.created_at,x.candidate_id) from ontology.nexloop_candidate_definitions x
   where x.tenant_id=t and x.world=p_world),'[]'::jsonb);
 elsif c->>'verb'='match' then
  return (select to_jsonb(m)-'tenant_id' from ontology.nexloop_claim_matches m where m.tenant_id=t and m.world=p_world and m.claim_id=c->>'claim_id'
   and m.matcher_version=c->>'matcher_version');
 elsif c->>'verb'='rematch' then
  if jsonb_typeof(c->'claim_ids') is distinct from 'array' or jsonb_array_length(c->'claim_ids')>1024 then raise exception 'claim match read verb invalid' using errcode='22023';end if;
  return coalesce((select jsonb_object_agg(r.claim_id,r.generation) from ontology.nexloop_claim_rematch r where r.tenant_id=t and r.world=p_world
   and r.claim_id in (select jsonb_array_elements_text(c->'claim_ids'))),'{}'::jsonb);
 elsif c->>'verb'='approved_instance' then
  return (select jsonb_build_object('candidate_id',i.candidate_id,'object_id',i.object_id,'identifying',i.identifying) from ontology.nexloop_instance_approvals i
   where i.tenant_id=t and i.world=p_world and i.type_name=c->>'type_name' and i.dedupe_key=c->>'dedupe_key');
 end if;
 raise exception 'claim match read verb invalid' using errcode='22023';
end $$;

-- 0099 reflow verbs unchanged (release_type kept), plus a general 'release', and 'instance_created'.
create or replace function authz.nexloop_review_reflow(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=authz.nexloop_assert_claim_match_authority(p_digest,p_world,p_text,p_signature,p_payload);c jsonb:=p_payload::jsonb;t text:=a->>'tenant_id';
 v_dec ontology.nexloop_review_decisions%rowtype;x ontology.nexloop_candidate_definitions%rowtype;ap ontology.nexloop_instance_approvals%rowtype;
 fp text;claims text[];eligible text[];conv text;open_count integer;grant_wait integer;status text;report jsonb;v_cause text;o ontology.objects%rowtype;
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

do $grants$
declare f text;
begin
 foreach f in array array['ontology.nexloop_review_instance_type(jsonb)','ontology.nexloop_review_instance_gates(text,text,ontology.nexloop_candidate_definitions)',
  'authz.nexloop_review_decide(text,text,text,text,text)','authz.nexloop_read_claim_matching(text,text,text,text,text)','authz.nexloop_review_reflow(text,text,text,text,text)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';
  execute 'revoke all on function '||f||' from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator';
 end loop;
end $grants$;
grant execute on function authz.nexloop_review_decide(text,text,text,text,text) to nexloop_api;
grant execute on function authz.nexloop_read_claim_matching(text,text,text,text,text),authz.nexloop_review_reflow(text,text,text,text,text) to nexloop_domain_worker;
