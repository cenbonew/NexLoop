-- NX-046 / ADR-019 §3.5–3.6: review workbench reads, reject cooldown (AT-068)
-- and the reflow entry points NX-044's governed human review Actions will call.
-- No review decision is executable here: approve / merge_into / reject remain
-- NX-044 (human Action, ontology.schema.review, Schema publication, audit).

-- Rejected or superseded candidates no longer hold the exact-text key, so the
-- same text can re-open after its cooldown; open candidates stay unique.
do $drop$
declare n text;
begin
 -- The generated name is truncated at 63 bytes; find the 0070 unique key by its columns.
 select conname into strict n from pg_constraint where conrelid='ontology.nexloop_candidate_definitions'::regclass and contype='u'
  and pg_get_constraintdef(oid)='UNIQUE (tenant_id, world, kind, dedupe_key)';
 execute format('alter table ontology.nexloop_candidate_definitions drop constraint %I',n);
end $drop$;
create unique index nexloop_candidate_definitions_open_key on ontology.nexloop_candidate_definitions(tenant_id,world,kind,dedupe_key)
 where status not in ('rejected','superseded');

alter table ontology.nexloop_merge_configurations
 add column reject_cooldown_seconds integer not null default 2592000 check(reject_cooldown_seconds between 0 and 31536000);

create table ontology.nexloop_candidate_rejections (
 tenant_id text not null,world text not null,candidate_id text not null,kind text not null,dedupe_key text not null,
 rejected_at timestamptz not null default clock_timestamp(),cooldown_until timestamptz not null,config_version text,
 primary key(tenant_id,world,candidate_id),check(cooldown_until>=rejected_at),
 foreign key(tenant_id,world,candidate_id) references ontology.nexloop_candidate_definitions
);
create index nexloop_candidate_rejections_key on ontology.nexloop_candidate_rejections(tenant_id,world,kind,dedupe_key,cooldown_until);
alter table ontology.nexloop_candidate_rejections owner to nexloop_owner;
alter table ontology.nexloop_candidate_rejections enable row level security;
alter table ontology.nexloop_candidate_rejections force row level security;
create policy tenant_boundary on ontology.nexloop_candidate_rejections to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on ontology.nexloop_candidate_rejections from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;

-- Whatever path rejects a candidate (NX-044's human Action through
-- ontology.nexloop_candidate_transition), the cooldown and the Claim state follow.
create function ontology.nexloop_candidate_rejected() returns trigger
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare seconds integer;version text;
begin
 select reject_cooldown_seconds,config_version into seconds,version from ontology.nexloop_merge_configurations where tenant_id=new.tenant_id and active;
 insert into ontology.nexloop_candidate_rejections(tenant_id,world,candidate_id,kind,dedupe_key,rejected_at,cooldown_until,config_version)
  values(new.tenant_id,new.world,new.candidate_id,new.kind,new.dedupe_key,clock_timestamp(),clock_timestamp()+make_interval(secs=>coalesce(seconds,2592000)),version);
 -- Dependent Claims stay as original evidence only (never formal, never Context properties).
 update ontology.nexloop_claims set resolution_state='rejected_definition' where tenant_id=new.tenant_id and world=new.world
  and 'claim:'||claim_id=any(new.dependent_claims) and resolution_state='awaiting_definition';
 return new;
end $$;
create trigger nexloop_candidate_rejected after update of status on ontology.nexloop_candidate_definitions
 for each row when (new.status='rejected' and old.status is distinct from 'rejected') execute function ontology.nexloop_candidate_rejected();

-- Reflow effects shared by service glue (0072) and NX-044 merge_into: alias row,
-- dependent Claims re-pointed (awaiting_definition → unresolved). The caller
-- then reflows recall (RecallIndexer.index_alias) and re-matches the Claims.
create function ontology.nexloop_candidate_merge_effects(p_tenant text,p_world text,p_candidate text,p_target text,p_alias text,p_config text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare cand ontology.nexloop_candidate_definitions%rowtype;v_alias text;claims text[];
begin
 select * into cand from ontology.nexloop_candidate_definitions where tenant_id=p_tenant and world=p_world and candidate_id=p_candidate for update;
 if not found then raise exception 'candidate unavailable' using errcode='42501';end if;
 if cand.kind not in ('property','vocabulary_value','object_type') then
  raise exception 'instance merge requires identity resolution, not an alias' using errcode='42501';end if;
 if p_target is null or p_target!~'^[A-Za-z][A-Za-z0-9_.-]*:[^\s]+$' or p_alias is null or char_length(btrim(p_alias)) not between 1 and 200 then
  raise exception 'merge effects invalid' using errcode='22023';end if;
 v_alias:='alias-'||left(encode(sha256(convert_to(p_tenant||'|'||p_world||'|'||p_target||'|'||btrim(p_alias),'UTF8')),'hex'),32);
 insert into ontology.nexloop_definition_aliases(tenant_id,world,alias_id,alias_text,canonical_ref,candidate_id,config_version)
  values(p_tenant,p_world,v_alias,btrim(p_alias),p_target,cand.candidate_id,coalesce(p_config,'review')) on conflict do nothing;
 select array_agg(substr(x,7)) into claims from unnest(cand.dependent_claims) x where x like 'claim:%';
 select array_agg(claim_id order by claim_id) into claims from ontology.nexloop_claims where tenant_id=p_tenant and world=p_world and claim_id=any(claims)
  and resolution_state='awaiting_definition';
 update ontology.nexloop_claims set resolution_state='unresolved' where tenant_id=p_tenant and world=p_world and claim_id=any(claims);
 return jsonb_build_object('alias_id',v_alias,'claims',to_jsonb(coalesce(claims,'{}')));
end $$;

-- NX-044 approve after a successful Schema publication: dependent Claims are
-- re-pointed; the caller reflows recall (index_object_type) and re-matches.
create function ontology.nexloop_candidate_publish_effects(p_tenant text,p_world text,p_candidate text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare cand ontology.nexloop_candidate_definitions%rowtype;claims text[];
begin
 select * into cand from ontology.nexloop_candidate_definitions where tenant_id=p_tenant and world=p_world and candidate_id=p_candidate for update;
 if not found or cand.status<>'published' then raise exception 'published candidate required' using errcode='42501';end if;
 select array_agg(substr(x,7)) into claims from unnest(cand.dependent_claims) x where x like 'claim:%';
 select array_agg(claim_id order by claim_id) into claims from ontology.nexloop_claims where tenant_id=p_tenant and world=p_world and claim_id=any(claims)
  and resolution_state='awaiting_definition';
 update ontology.nexloop_claims set resolution_state='unresolved' where tenant_id=p_tenant and world=p_world and claim_id=any(claims);
 return jsonb_build_object('claims',to_jsonb(coalesce(claims,'{}')));
end $$;

create or replace function authz.nexloop_record_claim_match(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=authz.nexloop_assert_claim_match_authority(p_digest,p_world,p_text,p_signature,p_payload);c jsonb:=p_payload::jsonb;
 t text:=a->>'tenant_id';cl ontology.nexloop_claims%rowtype;existing ontology.nexloop_claim_matches%rowtype;p jsonb;cd jsonb;
 v_proposal text;v_candidate text;v_new_id text;v_resolution text;v_outcome text:=c->>'outcome';pr ontology.nexloop_mutation_proposals%rowtype;
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
   -- AT-068: the same text rejected within its cooldown is kept as evidence only,
   -- never queued again; the Claim becomes rejected_definition.
   select r.candidate_id into v_candidate from ontology.nexloop_candidate_rejections r
    where r.tenant_id=t and r.world=p_world and r.kind=cd->>'kind' and r.dedupe_key=cd->>'dedupe_key' and r.cooldown_until>clock_timestamp()
    order by r.rejected_at desc limit 1;
   if v_candidate is not null then
    update ontology.nexloop_candidate_definitions set updated_at=clock_timestamp(),
     dependent_claims=(select array_agg(distinct x order by x) from unnest(dependent_claims||array['claim:'||cl.claim_id]) x)
     where tenant_id=t and world=p_world and candidate_id=v_candidate;
    v_resolution:='rejected_definition';
   else
    -- After a cooldown the same text re-opens as a new candidate with a new id.
    v_new_id:=cd->>'candidate_id';
    if exists(select 1 from ontology.nexloop_candidate_definitions x where x.tenant_id=t and x.world=p_world and x.candidate_id=v_new_id and x.status in ('rejected','superseded')) then
     v_new_id:=md5(v_new_id||':reopened:'||(select count(*) from ontology.nexloop_candidate_definitions x where x.tenant_id=t and x.world=p_world
      and x.kind=cd->>'kind' and x.dedupe_key=cd->>'dedupe_key'))::uuid::text;
    end if;
    -- Same tenant/world/kind/normalized text: one open candidate, dependent Claims accumulate.
    insert into ontology.nexloop_candidate_definitions(tenant_id,world,candidate_id,kind,dedupe_key,candidate,dependent_claims)
     values(t,p_world,v_new_id,cd->>'kind',cd->>'dedupe_key',jsonb_set(cd->'candidate','{candidate_id}',to_jsonb(v_new_id)),array['claim:'||cl.claim_id])
     on conflict(tenant_id,world,kind,dedupe_key) where status not in ('rejected','superseded') do update set
      dependent_claims=(select array_agg(distinct x order by x) from unnest(ontology.nexloop_candidate_definitions.dependent_claims||excluded.dependent_claims) x),
      updated_at=clock_timestamp();
    select candidate_id into v_candidate from ontology.nexloop_candidate_definitions where tenant_id=t and world=p_world and kind=cd->>'kind'
     and dedupe_key=cd->>'dedupe_key' and status not in ('rejected','superseded');
    v_resolution:='awaiting_definition';
   end if;
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

create or replace function authz.nexloop_record_candidate_glue(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=authz.nexloop_assert_claim_match_authority(p_digest,p_world,p_text,p_signature,p_payload);c jsonb:=p_payload::jsonb;
 t text:=a->>'tenant_id';cand ontology.nexloop_candidate_definitions%rowtype;tgt ontology.nexloop_candidate_definitions%rowtype;
 cfg ontology.nexloop_merge_configurations%rowtype;rev bigint;alias_id text;claims text[];v_effects jsonb;
begin
 select * into cand from ontology.nexloop_candidate_definitions where tenant_id=t and world=p_world and candidate_id=c->>'candidate_id' for update;
 if not found then raise exception 'candidate unavailable' using errcode='42501';end if;
 -- A later Claim proposing the same text as an already merged candidate is attached
 -- there by exact-text dedupe (0070); it is re-pointed instead of left waiting.
 if c->>'to'='repoint' then
  if cand.status<>'merged' then raise exception 'repoint requires a merged candidate' using errcode='42501';end if;
  select array_agg(substr(x,7)) into claims from unnest(cand.dependent_claims) x where x like 'claim:%';
  select array_agg(claim_id) into claims from ontology.nexloop_claims where tenant_id=t and world=p_world and claim_id=any(claims) and resolution_state='awaiting_definition';
  update ontology.nexloop_claims set resolution_state='unresolved' where tenant_id=t and world=p_world and claim_id=any(claims);
  return jsonb_build_object('status','merged','candidate_id',cand.candidate_id,'claims',to_jsonb(coalesce(claims,'{}')));
 end if;
 if cand.status<>'staged' then return jsonb_build_object('replay',true,'status',cand.status,'candidate_id',cand.candidate_id);end if;
 select * into cfg from ontology.nexloop_merge_configurations where tenant_id=t and active;
 if not found or cfg.config_version is distinct from c->>'config_version' then raise exception 'merge configuration changed' using errcode='40001';end if;
 if jsonb_typeof(c->'merge_scores') is distinct from 'object' or c->'merge_scores'->>'config_version' is distinct from cfg.config_version then
  raise exception 'merge scores invalid' using errcode='22023';end if;
 if c->>'to'='superseded' then
  -- Similar open candidate: fold dependent Claims into it, keep this one as evidence.
  select * into tgt from ontology.nexloop_candidate_definitions where tenant_id=t and world=p_world and candidate_id=c->>'superseded_by'
   and candidate_id<>cand.candidate_id and kind=cand.kind and status in ('staged','pending_review') for update;
  if not found then raise exception 'dedupe target unavailable' using errcode='42501';end if;
  if (c->'merge_scores'->>'weighted_total')::numeric<cfg.dedupe_threshold then raise exception 'below dedupe threshold' using errcode='22023';end if;
  update ontology.nexloop_candidate_definitions set dependent_claims=(select array_agg(distinct x order by x) from unnest(tgt.dependent_claims||cand.dependent_claims) x),
   updated_at=clock_timestamp() where tenant_id=t and world=p_world and candidate_id=tgt.candidate_id;
  rev:=ontology.nexloop_candidate_transition(t,p_world,cand.candidate_id,cand.revision,'staged','superseded','service',a->>'principal_id','similar_candidate',
   jsonb_build_object('superseded_by',tgt.candidate_id,'merge_scores',c->'merge_scores','config_version',cfg.config_version));
  return jsonb_build_object('replay',false,'status','superseded','candidate_id',cand.candidate_id,'superseded_by',tgt.candidate_id);
 elsif c->>'to'='merged' then
  if cand.kind not in ('property','vocabulary_value','object_type') or c->>'merge_target_ref' is null
   or (c->'merge_scores'->>'weighted_total')::numeric<cfg.merge_threshold or c->'merge_scores'->>'best_match_ref' is distinct from c->>'merge_target_ref'
   or c->>'alias_text' is null or char_length(btrim(c->>'alias_text')) not between 1 and 200 then
   raise exception 'merge not admissible' using errcode='42501';end if;
  v_effects:=ontology.nexloop_candidate_merge_effects(t,p_world,cand.candidate_id,c->>'merge_target_ref',c->>'alias_text',cfg.config_version);
  alias_id:=v_effects->>'alias_id';
  rev:=ontology.nexloop_candidate_transition(t,p_world,cand.candidate_id,cand.revision,'staged','merged','service',a->>'principal_id','merge_threshold_reached',
   jsonb_build_object('merge_target_ref',c->>'merge_target_ref','merge_scores',c->'merge_scores','config_version',cfg.config_version,'alias_id',alias_id));
  select array_agg(x) into claims from jsonb_array_elements_text(v_effects->'claims') x;
  return jsonb_build_object('replay',false,'status','merged','candidate_id',cand.candidate_id,'alias_id',alias_id,'claims',to_jsonb(coalesce(claims,'{}')));
 elsif c->>'to'='pending_review' then
  if (c->'merge_scores'->>'weighted_total')::numeric>=cfg.merge_threshold and cand.kind<>'object_instance' then
   raise exception 'merge threshold reached; not a review case' using errcode='22023';end if;
  rev:=ontology.nexloop_candidate_transition(t,p_world,cand.candidate_id,cand.revision,'staged','pending_review','service',a->>'principal_id',
   coalesce(c->>'reason','below_merge_threshold'),jsonb_build_object('merge_scores',c->'merge_scores','config_version',cfg.config_version));
  return jsonb_build_object('replay',false,'status','pending_review','candidate_id',cand.candidate_id);
 end if;
 raise exception 'glue verb invalid' using errcode='22023';
end $$;

-- Review reads share one check: signed claims for current EXECUTE on the review
-- Action (human browser session or service), tenant/world from the credential.
create function authz.nexloop_assert_review_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns text
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;k bytea;
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker') or p_world is null or octet_length(p_text)>1048576 or octet_length(p_payload)>65536
  or a->>'protocol' is distinct from 'nexloop-review-queue-v1' or a->>'operation' is distinct from 'execute'
  or a->>'action_resource' is distinct from 'eios:action:ontology.schema.review:1'
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'review read unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-review-queue-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'review read unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 perform set_config('eios.tenant_id',a->>'tenant_id',true);
 return a->>'tenant_id';
end $$;

-- Workbench detail for one pending_review candidate of the caller's tenant/world:
-- original spans, recall, glue scores and configuration version, dependent Claim
-- count, the similar candidates folded into it and any cooldown on its text.
create function authz.nexloop_read_review_candidate(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare t text:=authz.nexloop_assert_review_read(p_digest,p_world,p_text,p_signature,p_payload);c jsonb:=p_payload::jsonb;x ontology.nexloop_candidate_definitions%rowtype;
begin
 select * into x from ontology.nexloop_candidate_definitions d where d.tenant_id=t and d.world=p_world and d.candidate_id=c->>'candidate_id' and d.status='pending_review';
 if not found then return null;end if;
 return jsonb_build_object('candidate_id',x.candidate_id,'kind',x.kind,'status',x.status,'revision',x.revision,'candidate',x.candidate,
  'merge_scores',x.merge_scores,'config_version',x.config_version,'created_at',x.created_at,'dependent_claim_count',cardinality(x.dependent_claims),
  'evidence',(select coalesce(jsonb_agg(jsonb_build_object('claim_id',cl.claim_id,'quote',cl.quote,'source_message_id',cl.source_message_id,
     'span_start',cl.span_start,'span_end',cl.span_end,'predicate',cl.predicate,'value',cl.value,'resolution_state',cl.resolution_state)
     order by cl.source_sequence,cl.claim_id),'[]'::jsonb)
    from ontology.nexloop_claims cl where cl.tenant_id=x.tenant_id and cl.world=x.world and 'claim:'||cl.claim_id=any(x.dependent_claims)),
  'similar',(select coalesce(jsonb_agg(jsonb_build_object('candidate_id',s.candidate_id,'status',s.status,'proposed',s.candidate->'proposed',
     'merge_scores',s.merge_scores) order by s.created_at,s.candidate_id),'[]'::jsonb)
    from ontology.nexloop_candidate_definitions s where s.tenant_id=x.tenant_id and s.world=x.world and s.superseded_by=x.candidate_id),
  'cooldown',(select jsonb_build_object('candidate_id',r.candidate_id,'cooldown_until',r.cooldown_until) from ontology.nexloop_candidate_rejections r
    where r.tenant_id=x.tenant_id and r.world=x.world and r.kind=x.kind and r.dedupe_key=x.dedupe_key order by r.rejected_at desc limit 1));
end $$;

do $grants$
declare f text;
begin
 foreach f in array array['ontology.nexloop_candidate_rejected()','ontology.nexloop_candidate_merge_effects(text,text,text,text,text,text)',
  'ontology.nexloop_candidate_publish_effects(text,text,text)','authz.nexloop_assert_review_read(text,text,text,text,text)',
  'authz.nexloop_read_review_candidate(text,text,text,text,text)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';
  execute 'revoke all on function '||f||' from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator';
 end loop;
end $grants$;
grant execute on function authz.nexloop_read_review_candidate(text,text,text,text,text) to nexloop_api,nexloop_domain_worker;

-- Versioned glue configuration manifest (deploy/ontology/merge-config.v*.json),
-- published by the trusted configuration identity only. Versions are immutable:
-- the same content re-applies as a no-op (or re-activates an older version for a
-- rollback); different content under an existing version is refused. The
-- calibration's embedding profile must be the tenant's active recall profile.
alter table ontology.nexloop_merge_configurations
 add column manifest_sha256 text check(manifest_sha256 is null or manifest_sha256~'^[0-9a-f]{64}$'),
 add column published_by text;

create function control.nexloop_publish_merge_configuration_manifest(p_tenant text,p_manifest jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare m jsonb:=p_manifest;v text:=p_manifest->>'config_version';cur ontology.nexloop_merge_configurations%rowtype;active_profile text;existing boolean;
 p_sha256 text:=encode(sha256(convert_to(p_manifest::text,'UTF8')),'hex');
begin
 if session_user<>'nexloop_configurator' then raise exception 'merge configuration requires trusted configuration' using errcode='42501';end if;
 if m->>'schema_version' is distinct from 'nexloop-merge-config/1' or jsonb_typeof(m->'embedding_profile') is distinct from 'object' then
  raise exception 'merge configuration manifest invalid' using errcode='22023';end if;
 perform 1 from control.nexloop_tenants where tenant_id=p_tenant and status='active';
 if not found then raise exception 'tenant unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select active_profile_id into active_profile from ontology.nexloop_recall_settings where tenant_id=p_tenant;
 if active_profile is distinct from (m->'embedding_profile'->>'model')||'@'||(m->'embedding_profile'->>'dimension') then
  raise exception 'calibrated embedding profile is not the active recall profile' using errcode='42501';end if;
 select * into cur from ontology.nexloop_merge_configurations where tenant_id=p_tenant and config_version=v for update;
 existing:=found;
 if existing then
  if cur.manifest_sha256 is distinct from p_sha256 then raise exception 'merge configuration version is immutable' using errcode='42501';end if;
  if cur.active then return jsonb_build_object('config_version',v,'changed',false,'manifest_sha256',p_sha256);end if;
 end if;
 update ontology.nexloop_merge_configurations set active=false where tenant_id=p_tenant and active;
 if existing then
  update ontology.nexloop_merge_configurations set active=true where tenant_id=p_tenant and config_version=v;
 else
  insert into ontology.nexloop_merge_configurations(tenant_id,config_version,weights,merge_threshold,dedupe_threshold,whitelist,calibration,active,
    reject_cooldown_seconds,manifest_sha256,published_by)
   values(p_tenant,v,m->'weights',(m->>'merge_threshold')::numeric,(m->>'dedupe_threshold')::numeric,m->'whitelist',
    m->'calibration'||jsonb_build_object('embedding_profile',m->'embedding_profile','feature_version',m->>'feature_version'),true,
    (m->>'reject_cooldown_seconds')::integer,p_sha256,session_user);
 end if;
 return jsonb_build_object('config_version',v,'changed',true,'manifest_sha256',p_sha256);
end $$;

create function control.nexloop_read_merge_configuration(p_tenant text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
begin
 if session_user<>'nexloop_configurator' then raise exception 'merge configuration requires trusted configuration' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 return jsonb_build_object(
  'active',(select jsonb_build_object('config_version',c.config_version,'manifest_sha256',c.manifest_sha256,'weights',c.weights,
     'merge_threshold',c.merge_threshold,'dedupe_threshold',c.dedupe_threshold,'reject_cooldown_seconds',c.reject_cooldown_seconds)
    from ontology.nexloop_merge_configurations c where c.tenant_id=p_tenant and c.active),
  'recall_profile',(select active_profile_id from ontology.nexloop_recall_settings where tenant_id=p_tenant));
end $$;

do $grants$
declare f text;
begin
 foreach f in array array['control.nexloop_publish_merge_configuration_manifest(text,jsonb)','control.nexloop_read_merge_configuration(text)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';
  execute 'revoke all on function '||f||' from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity';
  execute 'grant execute on function '||f||' to nexloop_configurator';
 end loop;
end $grants$;
