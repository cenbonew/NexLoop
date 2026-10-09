-- NX-020 / ADR-019 §3.3: four-layer matching outcome per Claim, mutation proposal
-- lifecycle (docs/04 §6) and staged candidate definitions (minimal store; staging
-- policy, merge and review queue belong to NX-045/046).
-- Proposals and candidates are records, not business writes: formal objects only
-- change through the existing governed object create/edit Actions. Only a current
-- service credential with nexloop.claim.match EXECUTE may record, through these
-- definers; restricted roles have no table privileges.
create table ontology.nexloop_mutation_proposals (
 tenant_id text not null,world text not null,proposal_id text not null check(proposal_id~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
 business_intent_ref text not null check(business_intent_ref~'^nx020:[0-9a-f]{64}$'),
 claim_id text not null,outcome text not null check(outcome in ('full_match','partial_match')),
 status text not null check(status in ('proposed','applying','applied','conflict','rejected','superseded')),
 op text not null check(op in ('create_object','set_property')),type_name text not null,target_ref text not null,property_name text,
 effective_at timestamptz not null,supersedes_claim text,proposal jsonb not null check(jsonb_typeof(proposal)='object'),
 apply_args jsonb,receipt jsonb,reason text not null default '',attempts integer not null default 0 check(attempts between 0 and 16),
 created_at timestamptz not null default clock_timestamp(),updated_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,proposal_id),unique(tenant_id,world,business_intent_ref),
 check((op='set_property')=(property_name is not null)),
 foreign key(tenant_id,world,claim_id) references ontology.nexloop_claims
);
create index nexloop_mutation_proposals_target on ontology.nexloop_mutation_proposals(tenant_id,world,target_ref,property_name,status);
create table ontology.nexloop_candidate_definitions (
 tenant_id text not null,world text not null,candidate_id text not null check(candidate_id~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
 kind text not null check(kind in ('object_type','property','vocabulary_value','alias','object_instance')),
 dedupe_key text not null check(dedupe_key~'^[0-9a-f]{64}$'),status text not null default 'staged' check(status='staged'),
 candidate jsonb not null check(jsonb_typeof(candidate)='object'),dependent_claims text[] not null check(cardinality(dependent_claims) between 1 and 1024),
 created_at timestamptz not null default clock_timestamp(),updated_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,candidate_id),unique(tenant_id,world,kind,dedupe_key)
);
create table ontology.nexloop_claim_matches (
 tenant_id text not null,world text not null,claim_id text not null,matcher_version text not null check(length(matcher_version) between 1 and 80),
 outcome text not null check(outcome in ('full_match','partial_match','no_match','hypothesis','needs_resolution','noop')),
 decision jsonb not null check(jsonb_typeof(decision)='object'),recall jsonb not null check(jsonb_typeof(recall)='array'),
 reason text not null default '',proposal_id text,candidate_id text,recorded_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,claim_id,matcher_version),
 check(outcome not in ('full_match','partial_match') or proposal_id is not null),check(outcome<>'no_match' or candidate_id is not null),
 foreign key(tenant_id,world,claim_id) references ontology.nexloop_claims,
 foreign key(tenant_id,world,proposal_id) references ontology.nexloop_mutation_proposals,
 foreign key(tenant_id,world,candidate_id) references ontology.nexloop_candidate_definitions
);
do $isolation$
declare t text;
begin
 foreach t in array array['nexloop_mutation_proposals','nexloop_candidate_definitions','nexloop_claim_matches'] loop
  execute format('alter table ontology.%I owner to nexloop_owner',t);
  execute format('alter table ontology.%I enable row level security',t);
  execute format('alter table ontology.%I force row level security',t);
  execute format('create policy tenant_boundary on ontology.%I to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on ontology.%I from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity',t);
 end loop;
end $isolation$;

-- Signed service claims for nexloop.claim.match EXECUTE (same shape as NX-019).
create function authz.nexloop_assert_claim_match_authority(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;k bytea;
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker') or p_world is null or octet_length(p_text)>1048576 or octet_length(p_payload)>1048576
  or a->>'protocol' is distinct from 'nexloop-claim-match-v1' or a->>'operation' is distinct from 'execute'
  or a->>'action_resource' is distinct from 'eios:action:nexloop.claim.match:1'
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'claim match unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-claim-match-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'claim match unavailable' using errcode='42501';end if;
 if exists(select 1 from authz.nexloop_browser_token_realms where token_digest=p_digest)
  or not exists(select 1 from authz.nexloop_service_credentials t where t.token_digest=p_digest and t.tenant_id=a->>'tenant_id'
     and t.binding->>'subject_kind'='service' and t.status='active') then
  raise exception 'claim match unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 perform set_config('eios.tenant_id',a->>'tenant_id',true);
 return a;
end $$;

-- Allowed proposal transitions (docs/04 §6). applying is never moved back to
-- proposed: an interrupted apply is resumed with the same intent and arguments.
create function authz.nexloop_claim_match_transition_allowed(p_from text,p_to text) returns boolean
 language sql immutable set search_path=pg_catalog as $$
 select (p_from,p_to) in (('proposed','applying'),('proposed','superseded'),('proposed','rejected'),
  ('applying','applying'),('applying','applied'),('applying','conflict'),('applying','rejected'),
  ('conflict','applying'),('conflict','superseded'),('conflict','rejected'))
$$;

create function authz.nexloop_record_claim_match(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=authz.nexloop_assert_claim_match_authority(p_digest,p_world,p_text,p_signature,p_payload);c jsonb:=p_payload::jsonb;
 t text:=a->>'tenant_id';cl ontology.nexloop_claims%rowtype;existing ontology.nexloop_claim_matches%rowtype;p jsonb;cd jsonb;
 v_proposal text;v_candidate text;v_resolution text;v_outcome text:=c->>'outcome';pr ontology.nexloop_mutation_proposals%rowtype;
begin
 if c->>'verb'='match' then
  select * into cl from ontology.nexloop_claims where tenant_id=t and world=p_world and claim_id=c->>'claim_id' for update;
  if not found then raise exception 'claim unavailable' using errcode='42501';end if;
  select * into existing from ontology.nexloop_claim_matches where tenant_id=t and world=p_world and claim_id=cl.claim_id and matcher_version=c->>'matcher_version';
  if found then
   -- Same Claim, same matcher version: replay, never a second proposal or candidate.
   return jsonb_build_object('replay',true,'outcome',existing.outcome,'proposal_id',existing.proposal_id,'candidate_id',existing.candidate_id);
  end if;
  -- Hypotheses go to the hypothesis layer only (ADR-019 decision 2).
  if (cl.epistemic_kind='hypothesis')<>(v_outcome='hypothesis') then raise exception 'hypothesis routing invalid' using errcode='42501';end if;
  if v_outcome in ('full_match','partial_match') then
   p:=c->'proposal';
   if jsonb_typeof(p) is distinct from 'object' or p->'proposal'->>'extraction_ref' is distinct from 'claim:'||cl.claim_id
    or p->'proposal'->>'source_content_hash' is distinct from cl.source_content_hash or cl.source_content_hash is null
    or p->'proposal'->>'world_id' is distinct from p_world or p->'proposal'->>'business_intent_ref' is distinct from p->>'business_intent_ref'
    or p->'proposal'->>'proposal_id' is distinct from p->>'proposal_id' then
    raise exception 'proposal invalid' using errcode='42501';end if;
   insert into ontology.nexloop_mutation_proposals(tenant_id,world,proposal_id,business_intent_ref,claim_id,outcome,status,op,type_name,target_ref,property_name,
     effective_at,supersedes_claim,proposal)
    values(t,p_world,p->>'proposal_id',p->>'business_intent_ref',cl.claim_id,v_outcome,'proposed',p->>'op',p->>'type_name',p->>'target_ref',p->>'property_name',
     (p->>'effective_at')::timestamptz,p->>'supersedes_claim',p->'proposal')
    on conflict(tenant_id,world,business_intent_ref) do nothing;
   select proposal_id into v_proposal from ontology.nexloop_mutation_proposals where tenant_id=t and world=p_world and business_intent_ref=p->>'business_intent_ref';
   v_resolution:='needs_resolution';
  elsif v_outcome='no_match' then
   cd:=c->'candidate';
   if jsonb_typeof(cd) is distinct from 'object' or cd->'candidate'->>'extraction_ref' is distinct from 'claim:'||cl.claim_id
    or cd->'candidate'->>'world_id' is distinct from p_world or cd->'candidate'->>'status' is distinct from 'staged'
    or cd->'candidate'->>'candidate_id' is distinct from cd->>'candidate_id' or cd->'candidate'->>'kind' is distinct from cd->>'kind' then
    raise exception 'candidate invalid' using errcode='42501';end if;
   -- Same tenant/world/kind/normalized text: one staged candidate, dependent Claims accumulate.
   insert into ontology.nexloop_candidate_definitions(tenant_id,world,candidate_id,kind,dedupe_key,candidate,dependent_claims)
    values(t,p_world,cd->>'candidate_id',cd->>'kind',cd->>'dedupe_key',cd->'candidate',array['claim:'||cl.claim_id])
    on conflict(tenant_id,world,kind,dedupe_key) do update set
     dependent_claims=(select array_agg(distinct x order by x) from unnest(ontology.nexloop_candidate_definitions.dependent_claims||excluded.dependent_claims) x),
     updated_at=clock_timestamp();
   select candidate_id into v_candidate from ontology.nexloop_candidate_definitions where tenant_id=t and world=p_world and kind=cd->>'kind' and dedupe_key=cd->>'dedupe_key';
   v_resolution:='awaiting_definition';
  elsif v_outcome='hypothesis' then v_resolution:='hypothesis_only';
  elsif v_outcome='noop' then v_resolution:='resolved';
  elsif v_outcome='needs_resolution' then v_resolution:='needs_resolution';
  else raise exception 'match outcome invalid' using errcode='22023';end if;
  insert into ontology.nexloop_claim_matches values(t,p_world,cl.claim_id,c->>'matcher_version',v_outcome,c->'decision',coalesce(c->'recall','[]'::jsonb),
   left(coalesce(c->>'reason',''),500),v_proposal,v_candidate,default);
  if cl.resolution_state in ('unresolved','needs_resolution') then
   update ontology.nexloop_claims set resolution_state=v_resolution where tenant_id=t and world=p_world and claim_id=cl.claim_id;
  end if;
  return jsonb_build_object('replay',false,'outcome',v_outcome,'proposal_id',v_proposal,'candidate_id',v_candidate);
 elsif c->>'verb'='transition' then
  select * into pr from ontology.nexloop_mutation_proposals where tenant_id=t and world=p_world and proposal_id=c->>'proposal_id' for update;
  if not found then raise exception 'proposal unavailable' using errcode='42501';end if;
  if pr.status is distinct from c->>'from' or not authz.nexloop_claim_match_transition_allowed(pr.status,c->>'to') then
   raise exception 'proposal transition conflict' using errcode='40001';end if;
  if c->>'to'='applying' and (jsonb_typeof(c->'apply_args') is distinct from 'object' or pr.attempts>=16) then
   raise exception 'proposal apply arguments required' using errcode='22023';end if;
  -- Resuming an interrupted apply must reuse the recorded arguments exactly.
  if pr.status='applying' and c->>'to'='applying' and c->'apply_args' is distinct from pr.apply_args then
   raise exception 'proposal apply arguments changed' using errcode='40001';end if;
  if c->>'to'='applied' and jsonb_typeof(c->'receipt') is distinct from 'object' then raise exception 'proposal receipt required' using errcode='22023';end if;
  update ontology.nexloop_mutation_proposals set status=c->>'to',
   apply_args=case when c->>'to'='applying' then c->'apply_args' else apply_args end,
   attempts=attempts+case when c->>'to'='applying' and pr.status<>'applying' then 1 else 0 end,
   receipt=coalesce(c->'receipt',receipt),reason=left(coalesce(c->>'reason',reason),500),updated_at=clock_timestamp()
   where tenant_id=t and world=p_world and proposal_id=pr.proposal_id;
  if c->>'to'='applied' then
   update ontology.nexloop_claims set resolution_state='resolved' where tenant_id=t and world=p_world and claim_id=pr.claim_id and resolution_state<>'superseded';
   -- Correction chain: the corrected Claim is kept as evidence but superseded.
   if pr.supersedes_claim is not null then
    update ontology.nexloop_claims set resolution_state='superseded' where tenant_id=t and world=p_world and claim_id=pr.supersedes_claim;
   end if;
  elsif c->>'to'='superseded' then
   update ontology.nexloop_claims set resolution_state='superseded' where tenant_id=t and world=p_world and claim_id=pr.claim_id;
  elsif c->>'to'='rejected' then
   update ontology.nexloop_claims set resolution_state='needs_resolution' where tenant_id=t and world=p_world and claim_id=pr.claim_id and resolution_state<>'superseded';
  end if;
  return jsonb_build_object('proposal_id',pr.proposal_id,'status',c->>'to');
 end if;
 raise exception 'claim match verb invalid' using errcode='22023';
end $$;

create function authz.nexloop_read_claim_matching(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
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
 end if;
 raise exception 'claim match read verb invalid' using errcode='22023';
end $$;

do $grants$
declare f text;
begin
 foreach f in array array['nexloop_assert_claim_match_authority(text,text,text,text,text)','nexloop_claim_match_transition_allowed(text,text)',
  'nexloop_record_claim_match(text,text,text,text,text)','nexloop_read_claim_matching(text,text,text,text,text)'] loop
  execute 'alter function authz.'||f||' owner to nexloop_owner';
  execute 'revoke all on function authz.'||f||' from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity';
 end loop;
end $grants$;
grant execute on function authz.nexloop_record_claim_match(text,text,text,text,text),authz.nexloop_read_claim_matching(text,text,text,text,text) to nexloop_domain_worker;
