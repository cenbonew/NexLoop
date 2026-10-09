-- NX-044 closing: approving a new object type publishes, in the same transaction, the type's governed
-- <Type>.create:1 and <Type>.edit:1 Action definitions next to Schema v1, so Claims waiting for the type
-- can be applied once trusted configuration grants them (ADR-020 §3: still no authority fact is written;
-- the 0082 fingerprint gate keeps enforcing that).
-- Both definitions reuse a Capability snapshot the tenant already publishes for
-- ontology.object.create / ontology.object.edit (no new capability, scope or risk) and must equal the
-- canonical low-risk, no-approval, policy-free shape exactly; only contract_digest, created_at and the
-- bound schema digest come from the reviewer's backend (EIOS models + schema_contract_digest).

create function ontology.nexloop_review_type_action_capabilities(p_tenant text) returns jsonb
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select coalesce(jsonb_object_agg(n,cap),'{}'::jsonb) from (
  select distinct on (a.capability->>'capability_name') a.capability->>'capability_name' n,a.capability cap
  from control.nexloop_action_definitions a where a.tenant_id=p_tenant and a.world='real' and a.active
   and a.capability->>'capability_name' in ('ontology.object.create','ontology.object.edit')
   and a.definition->'capability_binding'->>'capability_name'=a.capability->>'capability_name'
  order by a.capability->>'capability_name',a.resource_id) s
$$;

create function ontology.nexloop_review_type_action_expected(p_tenant text,p_type text,p_suffix text,p_cap jsonb,p_digest text) returns jsonb
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select jsonb_build_object('tenant_id',p_tenant,'definition_type','action','stable_name',p_type||'.'||p_suffix,'version',1,'status','published',
  'required_scopes',p_cap->'required_scopes','required_markings','[]'::jsonb,'created_by','nexloop-review-publication','previous_version',null,
  'source_lineage','[]'::jsonb,
  'capability_binding',jsonb_build_object('capability_name',p_cap->>'capability_name','capability_version',p_cap->>'capability_version','schema_hash',p_cap->>'schema_hash'),
  'object_types',jsonb_build_array(r.ref),'preconditions','[]'::jsonb,
  'governance',jsonb_build_object('change_scope',jsonb_build_object('object_types',jsonb_build_array(r.ref),'properties','[]'::jsonb,'target_systems','["postgres"]'::jsonb),
   'risk_level','low','policy_refs','[]'::jsonb,'approval_mode','none','idempotency',jsonb_build_object('required',true,'key_fields','["request_id"]'::jsonb),
   'compensation_mode','none','receipt_required',true),
  'receipt_schema','{"type":"object"}'::jsonb,'input_schema','{}'::jsonb,'parameters','[]'::jsonb)
 from (select jsonb_build_object('tenant_id',p_tenant,'schema_type','object_type','stable_name',p_type,'version',1,'schema_digest',p_digest) ref) r
$$;

-- Basis: unchanged plus the tenant's create/edit capability snapshots for new-type Actions.
create or replace function authz.nexloop_read_review_publication_basis(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_assert_review_read(p_digest,p_world,p_text,p_signature,p_payload);c jsonb:=p_payload::jsonb;
 x ontology.nexloop_candidate_definitions%rowtype;owner text;v integer;s jsonb;
begin
 select * into x from ontology.nexloop_candidate_definitions where tenant_id=t and world=p_world and candidate_id=c->>'candidate_id' and status='pending_review';
 if not found then return null;end if;
 owner:=ontology.nexloop_review_owner_type(x.candidate,x.kind);
 select d.version,d.definition into v,s from ontology.object_type_versions d where d.tenant_id=t and d.type_name=owner order by d.version desc limit 1;
 return jsonb_build_object('candidate_id',x.candidate_id,'kind',x.kind,'revision',x.revision,'candidate',x.candidate,'owner_type',owner,
  'latest_version',v,'schema',s,
  'actions',coalesce((select jsonb_agg(jsonb_build_object('resource_id',a.resource_id,'definition',a.definition,'capability',a.capability) order by a.resource_id)
   from control.nexloop_action_definitions a where a.tenant_id=t and a.world='real' and a.active and v is not null
    and exists(select 1 from jsonb_array_elements(a.definition->'object_types') r where r->>'stable_name'=owner and (r->>'version')::integer=v)),'[]'::jsonb),
  'type_action_capabilities',case when x.kind='object_type' then ontology.nexloop_review_type_action_capabilities(t) else '{}'::jsonb end);
end $$;

-- Gates: 0082 unchanged for property / vocabulary_value; a new type must come with exactly its two Actions.
alter function ontology.nexloop_review_publication_gates(text,text,ontology.nexloop_candidate_definitions,jsonb) rename to nexloop_review_publication_gates_v0082;
revoke all on function ontology.nexloop_review_publication_gates_v0082(text,text,ontology.nexloop_candidate_definitions,jsonb) from public;
create function ontology.nexloop_review_publication_gates(p_tenant text,p_world text,x ontology.nexloop_candidate_definitions,p_pub jsonb)
 returns text[] language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare f text[];owner text;caps jsonb;a jsonb;cap jsonb;suffix text;digest text;seen text[]:='{}';
begin
 if x.kind<>'object_type' then return ontology.nexloop_review_publication_gates_v0082(p_tenant,p_world,x,p_pub);end if;
 f:=ontology.nexloop_review_publication_gates_v0082(p_tenant,p_world,x,p_pub-'actions');
 if cardinality(f)>0 or (jsonb_typeof(p_pub->'builder_failures')='array' and jsonb_array_length(p_pub->'builder_failures')>0) then return f;end if;
 owner:=ontology.nexloop_review_owner_type(x.candidate,x.kind);caps:=ontology.nexloop_review_type_action_capabilities(p_tenant);
 foreach suffix in array array['create','edit'] loop
  if not caps ? ('ontology.object.'||suffix) then f:=array_append(f,'type_action_capability_unavailable:ontology.object.'||suffix);end if;
 end loop;
 if cardinality(f)>0 then return f;end if;
 if jsonb_typeof(p_pub->'actions') is distinct from 'array' or jsonb_array_length(p_pub->'actions')<>2 then return array['new_type_actions_must_be_create_and_edit'];end if;
 for a in select value from jsonb_array_elements(p_pub->'actions') loop
  suffix:=substring(a->'definition'->>'stable_name' from '^'||owner||'\.(create|edit)$');
  cap:=caps->('ontology.object.'||coalesce(suffix,''));
  if suffix is null or suffix=any(seen) then f:=array_append(f,'new_type_actions_must_be_create_and_edit');continue;end if;
  seen:=array_append(seen,suffix);
  digest:=a->'definition'->'object_types'->0->>'schema_digest';
  if coalesce(digest,'')!~'^[0-9a-f]{64}$' or jsonb_typeof(a->'definition'->'created_at') is distinct from 'string'
   or a->'capability' is distinct from cap
   or (a->'definition')-'contract_digest'-'created_at' is distinct from ontology.nexloop_review_type_action_expected(p_tenant,owner,suffix,cap,digest)
   or exists(select 1 from control.nexloop_action_definitions d where d.tenant_id=p_tenant and d.world='real' and d.resource_id='eios:action:'||owner||'.'||suffix||':1') then
   f:=array_append(f,'new_type_action_not_canonical:'||suffix);
  end if;
 end loop;
 if (p_pub->'actions'->0->'definition'->'object_types'->0->>'schema_digest') is distinct from (p_pub->'actions'->1->'definition'->'object_types'->0->>'schema_digest') then
  f:=array_append(f,'new_type_actions_bind_different_schemas');end if;
 return f;
end $$;

do $grants$
declare f text;
begin
 foreach f in array array['ontology.nexloop_review_type_action_capabilities(text)','ontology.nexloop_review_type_action_expected(text,text,text,jsonb,text)',
  'authz.nexloop_read_review_publication_basis(text,text,text,text,text)','ontology.nexloop_review_publication_gates(text,text,ontology.nexloop_candidate_definitions,jsonb)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';
  execute 'revoke all on function '||f||' from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator';
 end loop;
end $grants$;
grant execute on function authz.nexloop_read_review_publication_basis(text,text,text,text,text) to nexloop_api;

-- Reflow of a published new type (approved design): its dependent Claims go back to unresolved and are
-- marked in the claim-match feed (0093), so the matcher runs the full four-layer chain again — no layer is
-- skipped (name-only instances and new properties still go to review). A per-Claim rematch generation
-- gives the re-match its own matcher version, so the earlier no_match is not simply replayed.
create table ontology.nexloop_claim_rematch (
 tenant_id text not null,world text not null,claim_id text not null,generation integer not null check(generation>0),
 cause text not null check(char_length(cause) between 1 and 200),authority_fingerprint text not null,released_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,claim_id),foreign key(tenant_id,world,claim_id) references ontology.nexloop_claims
);
alter table ontology.nexloop_claim_rematch owner to nexloop_owner;
alter table ontology.nexloop_claim_rematch enable row level security;
alter table ontology.nexloop_claim_rematch force row level security;
create policy tenant_boundary on ontology.nexloop_claim_rematch to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on ontology.nexloop_claim_rematch from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;

-- 0070 read verbs unchanged, plus 'rematch': current generation per Claim (absent = never released).
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
 end if;
 raise exception 'claim match read verb invalid' using errcode='22023';
end $$;

-- 0082 reflow verbs unchanged ('pending' also reports the candidate kind), plus 'release_type'.
create or replace function authz.nexloop_review_reflow(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=authz.nexloop_assert_claim_match_authority(p_digest,p_world,p_text,p_signature,p_payload);c jsonb:=p_payload::jsonb;t text:=a->>'tenant_id';
 v_dec ontology.nexloop_review_decisions%rowtype;x ontology.nexloop_candidate_definitions%rowtype;fp text;claims text[];eligible text[];conv text;
 open_count integer;grant_wait integer;status text;report jsonb;
begin
 if c->>'verb'='pending' then
  return coalesce((select jsonb_agg(jsonb_build_object('decision_id',d.decision_id,'candidate_id',d.candidate_id,'outcome',d.outcome,'publication',d.publication,
    'reflow_status',d.reflow_status,'kind',k.kind) order by d.decided_at,d.decision_id) from ontology.nexloop_review_decisions d
    join ontology.nexloop_candidate_definitions k on k.tenant_id=d.tenant_id and k.world=d.world and k.candidate_id=d.candidate_id
   where d.tenant_id=t and d.world=p_world and d.reflow_status in ('pending','waiting')),'[]'::jsonb);
 elsif c->>'verb'='record' then
  if c->>'status' not in ('done','waiting') or jsonb_typeof(c->'report') is distinct from 'object' then raise exception 'reflow record invalid' using errcode='22023';end if;
  update ontology.nexloop_review_decisions set reflow_status=c->>'status',reflow_report=c->'report',reflowed_at=clock_timestamp(),
   publication=case when publication is null then null else jsonb_set(publication,'{applied_claim_count}',to_jsonb(coalesce((c->'report'->>'applied')::integer,0))) end,
   record=case when record ? 'publication' then jsonb_set(record,'{publication,applied_claim_count}',to_jsonb(coalesce((c->'report'->>'applied')::integer,0))) else record end
   where tenant_id=t and world=p_world and decision_id=c->>'decision_id' and reflow_status in ('pending','waiting');
  if not found then raise exception 'reflow decision unavailable' using errcode='42501';end if;
  return jsonb_build_object('decision_id',c->>'decision_id','reflow_status',c->>'status');
 elsif c->>'verb'='release_type' then
  select * into v_dec from ontology.nexloop_review_decisions where tenant_id=t and world=p_world and decision_id=c->>'decision_id'
   and outcome='published' and reflow_status in ('pending','waiting') for update;
  if not found then raise exception 'reflow decision unavailable' using errcode='42501';end if;
  select * into x from ontology.nexloop_candidate_definitions where tenant_id=t and world=p_world and candidate_id=v_dec.candidate_id;
  if x.kind is distinct from 'object_type' then raise exception 'reflow decision unavailable' using errcode='42501';end if;
  fp:=authz.nexloop_tenant_authority_fingerprint(t);
  select coalesce(array_agg(substr(v,7) order by v),'{}') into claims from unnest(x.dependent_claims) v where v like 'claim:%';
  -- First release: every dependent Claim still waiting for this type. Later: a Claim the matcher left in
  -- needs_resolution (e.g. type not readable yet) is released again only after the authority changed.
  select coalesce(array_agg(cl.claim_id order by cl.claim_id),'{}') into eligible from ontology.nexloop_claims cl
   left join ontology.nexloop_claim_rematch r on r.tenant_id=cl.tenant_id and r.world=cl.world and r.claim_id=cl.claim_id
   where cl.tenant_id=t and cl.world=p_world and cl.claim_id=any(claims)
    and ((r.claim_id is null and cl.resolution_state='awaiting_definition')
     or (r.cause='type:'||x.candidate_id and cl.resolution_state='needs_resolution' and r.authority_fingerprint<>fp));
  if cardinality(eligible)>0 then
   update ontology.nexloop_claims set resolution_state='unresolved' where tenant_id=t and world=p_world and claim_id=any(eligible);
   insert into ontology.nexloop_claim_rematch as r(tenant_id,world,claim_id,generation,cause,authority_fingerprint)
    select t,p_world,e,1,'type:'||x.candidate_id,fp from unnest(eligible) e
   on conflict(tenant_id,world,claim_id) do update set generation=r.generation+1,cause=excluded.cause,authority_fingerprint=excluded.authority_fingerprint,released_at=clock_timestamp();
   for conv in select distinct cl.conversation_id from ontology.nexloop_claims cl where cl.tenant_id=t and cl.world=p_world and cl.claim_id=any(eligible) loop
    perform authz.nexloop_work_feed_touch(t,p_world,'claim-match',conv,jsonb_build_object('conversation_id',conv));
   end loop;
  end if;
  select count(*) filter(where cl.resolution_state='unresolved'),count(*) filter(where cl.resolution_state='needs_resolution')
   into open_count,grant_wait from ontology.nexloop_claims cl where cl.tenant_id=t and cl.world=p_world and cl.claim_id=any(claims);
  -- Done once no dependent Claim waits for the matcher; other outcomes (applied, new candidates, rejected) are their own flows.
  status:=case when open_count+grant_wait=0 then 'done' else 'waiting' end;
  report:=jsonb_build_object('applied',0,'released',to_jsonb(eligible),'claims',to_jsonb(claims),
   'reason',case when open_count>0 then 'awaiting_matching' when grant_wait>0 then 'awaiting_grants' else null end);
  update ontology.nexloop_review_decisions set reflow_status=status,reflow_report=jsonb_strip_nulls(report),reflowed_at=clock_timestamp()
   where tenant_id=t and world=p_world and decision_id=v_dec.decision_id;
  return jsonb_strip_nulls(report)||jsonb_build_object('status',status);
 end if;
 raise exception 'reflow verb invalid' using errcode='22023';
end $$;

do $grants$
declare f text;
begin
 foreach f in array array['authz.nexloop_read_claim_matching(text,text,text,text,text)','authz.nexloop_review_reflow(text,text,text,text,text)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';
  execute 'revoke all on function '||f||' from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator';
 end loop;
end $grants$;
grant execute on function authz.nexloop_read_claim_matching(text,text,text,text,text),authz.nexloop_review_reflow(text,text,text,text,text) to nexloop_domain_worker;
