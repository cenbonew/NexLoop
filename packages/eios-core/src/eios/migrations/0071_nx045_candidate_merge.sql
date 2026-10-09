-- NX-045 / ADR-019 §3.4: candidate staging state machine, merge (glue) scoring
-- with versioned tenant configuration, alias source of truth, similar-candidate
-- dedupe and the pending_review queue read for NX-046.
-- Human review decisions (approve / merge_into / reject) and Schema publication
-- belong to NX-044: pending_review transitions here require actor_kind='human'
-- and are only reachable through ontology.nexloop_candidate_transition, which has
-- no application grant; NX-044's governed human Action definer is its caller.
alter table ontology.nexloop_candidate_definitions drop constraint nexloop_candidate_definitions_status_check;
alter table ontology.nexloop_candidate_definitions
 add constraint nexloop_candidate_definitions_status_check check(status in ('staged','merged','pending_review','published','rejected','superseded')),
 add column revision bigint not null default 1 check(revision>0),
 add column merge_scores jsonb check(merge_scores is null or jsonb_typeof(merge_scores)='object'),
 add column merge_target_ref text check(merge_target_ref is null or merge_target_ref~'^[A-Za-z][A-Za-z0-9_.-]*:[^\s]+$'),
 add column config_version text,
 add column superseded_by text,
 add column status_reason text not null default '',
 add constraint nexloop_candidate_definitions_merge check((status='merged')=(merge_target_ref is not null) or status in ('published','rejected')),
 add constraint nexloop_candidate_definitions_superseded check((status='superseded')=(superseded_by is not null)),
 add constraint nexloop_candidate_definitions_scored check(status not in ('merged','pending_review') or (merge_scores is not null and config_version is not null));
create index nexloop_candidate_definitions_queue on ontology.nexloop_candidate_definitions(tenant_id,world,status,created_at);

-- Every status change, with who caused it (service glue vs. human review).
create table ontology.nexloop_candidate_events (
 tenant_id text not null,world text not null,candidate_id text not null,event_id bigint generated always as identity,
 from_status text not null,to_status text not null,actor_kind text not null check(actor_kind in ('service','human')),actor_ref text not null,
 reason text not null default '',details jsonb not null default '{}'::jsonb,recorded_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,candidate_id,event_id),
 foreign key(tenant_id,world,candidate_id) references ontology.nexloop_candidate_definitions
);

-- Tenant glue configuration (ADR-019 §3.4.2): weights + threshold, versioned; one active.
create table ontology.nexloop_merge_configurations (
 tenant_id text not null,config_version text not null check(config_version~'^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$'),
 weights jsonb not null,merge_threshold numeric(4,3) not null check(merge_threshold between 0 and 1),
 dedupe_threshold numeric(4,3) not null check(dedupe_threshold between 0 and 1),
 whitelist jsonb not null default '[]'::jsonb check(jsonb_typeof(whitelist)='array'),
 calibration jsonb not null default '{}'::jsonb check(jsonb_typeof(calibration)='object'),
 active boolean not null default false,created_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,config_version),
 check(jsonb_typeof(weights)='object' and weights ?& array['core_term_containment','lexical_similarity','rule_whitelist','vector_cluster']
  and weights-array['core_term_containment','lexical_similarity','rule_whitelist','vector_cluster']='{}'::jsonb
  and abs((weights->>'lexical_similarity')::numeric+(weights->>'core_term_containment')::numeric+(weights->>'vector_cluster')::numeric+(weights->>'rule_whitelist')::numeric-1)<0.0005)
);
create unique index nexloop_merge_configurations_active on ontology.nexloop_merge_configurations(tenant_id) where active;

-- Alias mappings: the source of truth behind NX-021 recall alias rows.
create table ontology.nexloop_definition_aliases (
 tenant_id text not null,world text not null,alias_id text not null check(alias_id~'^alias-[0-9a-f]{32}$'),
 alias_text text not null check(char_length(alias_text) between 1 and 200),canonical_ref text not null check(canonical_ref~'^[A-Za-z][A-Za-z0-9_.-]*:[^\s]+$'),
 candidate_id text not null,config_version text not null,created_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,alias_id),unique(tenant_id,world,canonical_ref,alias_text),
 foreign key(tenant_id,world,candidate_id) references ontology.nexloop_candidate_definitions
);

do $isolation$
declare t text;
begin
 foreach t in array array['nexloop_candidate_events','nexloop_merge_configurations','nexloop_definition_aliases'] loop
  execute format('alter table ontology.%I owner to nexloop_owner',t);
  execute format('alter table ontology.%I enable row level security',t);
  execute format('alter table ontology.%I force row level security',t);
  execute format('create policy tenant_boundary on ontology.%I to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on ontology.%I from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator',t);
 end loop;
end $isolation$;

-- The single state machine. Service glue may only leave staged; every decision on
-- a pending_review candidate is a human review (NX-044).
create function ontology.nexloop_candidate_transition(p_tenant text,p_world text,p_candidate text,p_expected_revision bigint,p_from text,p_to text,
  p_actor_kind text,p_actor_ref text,p_reason text,p_details jsonb) returns bigint
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare c ontology.nexloop_candidate_definitions%rowtype;
begin
 select * into c from ontology.nexloop_candidate_definitions where tenant_id=p_tenant and world=p_world and candidate_id=p_candidate for update;
 if not found then raise exception 'candidate unavailable' using errcode='42501';end if;
 if c.status is distinct from p_from or c.revision is distinct from p_expected_revision then
  raise exception 'candidate transition conflict' using errcode='40001';end if;
 if not ((p_from,p_to,p_actor_kind) in (('staged','merged','service'),('staged','pending_review','service'),('staged','superseded','service'),
    ('pending_review','published','human'),('pending_review','merged','human'),('pending_review','rejected','human'),('pending_review','superseded','service'))) then
  raise exception 'candidate transition not allowed' using errcode='42501';end if;
 update ontology.nexloop_candidate_definitions set status=p_to,revision=revision+1,updated_at=clock_timestamp(),status_reason=left(coalesce(p_reason,''),500),
  merge_scores=coalesce(p_details->'merge_scores',merge_scores),config_version=coalesce(p_details->>'config_version',config_version),
  merge_target_ref=case when p_to='merged' then p_details->>'merge_target_ref' else merge_target_ref end,
  superseded_by=case when p_to='superseded' then p_details->>'superseded_by' else superseded_by end
  where tenant_id=p_tenant and world=p_world and candidate_id=p_candidate;
 insert into ontology.nexloop_candidate_events(tenant_id,world,candidate_id,from_status,to_status,actor_kind,actor_ref,reason,details)
  values(p_tenant,p_world,p_candidate,p_from,p_to,p_actor_kind,p_actor_ref,left(coalesce(p_reason,''),500),coalesce(p_details,'{}'::jsonb));
 return c.revision+1;
end $$;

-- Service glue: staged → merged | pending_review | superseded (signed nexloop.claim.match EXECUTE, 0070).
create function authz.nexloop_record_candidate_glue(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=authz.nexloop_assert_claim_match_authority(p_digest,p_world,p_text,p_signature,p_payload);c jsonb:=p_payload::jsonb;
 t text:=a->>'tenant_id';cand ontology.nexloop_candidate_definitions%rowtype;tgt ontology.nexloop_candidate_definitions%rowtype;
 cfg ontology.nexloop_merge_configurations%rowtype;rev bigint;alias_id text;claims text[];
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
  alias_id:='alias-'||left(encode(sha256(convert_to(t||'|'||p_world||'|'||(c->>'merge_target_ref')||'|'||btrim(c->>'alias_text'),'UTF8')),'hex'),32);
  insert into ontology.nexloop_definition_aliases(tenant_id,world,alias_id,alias_text,canonical_ref,candidate_id,config_version)
   values(t,p_world,alias_id,btrim(c->>'alias_text'),c->>'merge_target_ref',cand.candidate_id,cfg.config_version) on conflict do nothing;
  rev:=ontology.nexloop_candidate_transition(t,p_world,cand.candidate_id,cand.revision,'staged','merged','service',a->>'principal_id','merge_threshold_reached',
   jsonb_build_object('merge_target_ref',c->>'merge_target_ref','merge_scores',c->'merge_scores','config_version',cfg.config_version,'alias_id',alias_id));
  -- Dependent Claims (the accumulated column, not the first-Claim JSON) are re-pointed:
  -- they leave awaiting_definition and are re-matched against the merged definition.
  select array_agg(substr(x,7)) into claims from unnest(cand.dependent_claims) x where x like 'claim:%';
  update ontology.nexloop_claims set resolution_state='unresolved' where tenant_id=t and world=p_world and claim_id=any(claims) and resolution_state='awaiting_definition';
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

-- Glue inputs: open candidates, active configuration and aliases (domain worker).
create function authz.nexloop_read_candidate_glue(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=authz.nexloop_assert_claim_match_authority(p_digest,p_world,p_text,p_signature,p_payload);c jsonb:=p_payload::jsonb;t text:=a->>'tenant_id';
begin
 if c->>'verb'='configuration' then
  return (select to_jsonb(x)-'tenant_id' from ontology.nexloop_merge_configurations x where x.tenant_id=t and x.active);
 elsif c->>'verb'='candidates' then
  return coalesce((select jsonb_agg(to_jsonb(x)-'tenant_id' order by x.created_at,x.candidate_id) from ontology.nexloop_candidate_definitions x
   where x.tenant_id=t and x.world=p_world and (c->'status' is null or x.status=any(array(select jsonb_array_elements_text(c->'status'))))),'[]'::jsonb);
 elsif c->>'verb'='aliases' then
  return coalesce((select jsonb_agg(jsonb_build_object('alias_id',x.alias_id,'alias_text',x.alias_text,'canonical_ref',x.canonical_ref) order by x.alias_id)
   from ontology.nexloop_definition_aliases x where x.tenant_id=t and x.world=p_world),'[]'::jsonb);
 elsif c->>'verb'='claims' then
  -- Locator only: Claim content is read through the Conversation-READ view (NX-019).
  return coalesce((select jsonb_agg(jsonb_build_object('claim_id',x.claim_id,'conversation_id',x.conversation_id) order by x.claim_id)
   from ontology.nexloop_claims x where x.tenant_id=t and x.world=p_world and x.claim_id=any(array(select jsonb_array_elements_text(c->'claim_ids')))),'[]'::jsonb);
 end if;
 raise exception 'glue read verb invalid' using errcode='22023';
end $$;

-- NX-046 queue: pending_review of the caller's tenant/world only, for a principal
-- holding current EXECUTE on the review Action (ontology.schema.review, NX-044).
create function authz.nexloop_read_review_queue(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;k bytea;c jsonb:=p_payload::jsonb;t text:=a->>'tenant_id';lim integer:=coalesce((c->>'limit')::integer,50);
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker') or p_world is null or octet_length(p_text)>1048576 or octet_length(p_payload)>65536
  or a->>'protocol' is distinct from 'nexloop-review-queue-v1' or a->>'operation' is distinct from 'execute'
  or a->>'action_resource' is distinct from 'eios:action:ontology.schema.review:1'
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') or lim not between 1 and 200 then
  raise exception 'review queue unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-review-queue-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'review queue unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 perform set_config('eios.tenant_id',t,true);
 return coalesce((select jsonb_agg(item order by item->>'created_at',item->>'candidate_id') from (
  select jsonb_build_object('candidate_id',x.candidate_id,'kind',x.kind,'status',x.status,'revision',x.revision,'candidate',x.candidate,
   'merge_scores',x.merge_scores,'config_version',x.config_version,'created_at',x.created_at,'dependent_claim_count',cardinality(x.dependent_claims),
   'evidence',(select coalesce(jsonb_agg(jsonb_build_object('claim_id',cl.claim_id,'quote',cl.quote,'source_message_id',cl.source_message_id,
      'span_start',cl.span_start,'span_end',cl.span_end,'resolution_state',cl.resolution_state) order by cl.source_sequence,cl.claim_id),'[]'::jsonb)
     from ontology.nexloop_claims cl where cl.tenant_id=x.tenant_id and cl.world=x.world and 'claim:'||cl.claim_id=any(x.dependent_claims))) item
  from ontology.nexloop_candidate_definitions x where x.tenant_id=t and x.world=p_world and x.status='pending_review'
  order by x.created_at,x.candidate_id limit lim) q),'[]'::jsonb);
end $$;

-- Trusted configuration identity publishes a glue configuration version.
create function control.nexloop_publish_merge_configuration(p_tenant text,p_version text,p_weights jsonb,p_merge numeric,p_dedupe numeric,p_whitelist jsonb,p_calibration jsonb)
 returns text language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
begin
 if session_user<>'nexloop_configurator' then raise exception 'merge configuration requires trusted configuration' using errcode='42501';end if;
 perform 1 from control.nexloop_tenants where tenant_id=p_tenant and status='active';
 if not found then raise exception 'tenant unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 update ontology.nexloop_merge_configurations set active=false where tenant_id=p_tenant and active;
 insert into ontology.nexloop_merge_configurations(tenant_id,config_version,weights,merge_threshold,dedupe_threshold,whitelist,calibration,active)
  values(p_tenant,p_version,p_weights,p_merge,p_dedupe,coalesce(p_whitelist,'[]'::jsonb),coalesce(p_calibration,'{}'::jsonb),true);
 return p_version;
end $$;

do $grants$
declare f text;
begin
 foreach f in array array['ontology.nexloop_candidate_transition(text,text,text,bigint,text,text,text,text,text,jsonb)',
  'authz.nexloop_record_candidate_glue(text,text,text,text,text)','authz.nexloop_read_candidate_glue(text,text,text,text,text)',
  'authz.nexloop_read_review_queue(text,text,text,text,text)','control.nexloop_publish_merge_configuration(text,text,jsonb,numeric,numeric,jsonb,jsonb)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';
  execute 'revoke all on function '||f||' from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator';
 end loop;
end $grants$;
grant execute on function authz.nexloop_record_candidate_glue(text,text,text,text,text),authz.nexloop_read_candidate_glue(text,text,text,text,text) to nexloop_domain_worker;
grant execute on function authz.nexloop_read_review_queue(text,text,text,text,text) to nexloop_api,nexloop_domain_worker;
grant usage on schema control to nexloop_configurator;
grant execute on function control.nexloop_publish_merge_configuration(text,text,jsonb,numeric,numeric,jsonb,jsonb) to nexloop_configurator;
