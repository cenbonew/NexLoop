-- NX-044 / ADR-019 §3.5: governed HUMAN review decisions (approve / merge_into /
-- reject) on pending_review candidates, persisted as review-decision records, and
-- the human-approved Schema publication path (additive only, docs/08 §5 gates).
-- Only an actual browser Human session holding current EXECUTE on
-- eios:action:ontology.schema.review:1 may decide; service and agent (model)
-- credentials are refused. Publication writes no authority fact (ADR-020 §3):
-- grants for successor Action versions / new properties come from trusted
-- configuration, after which the service reflow applies the waiting Claims.
create table ontology.nexloop_review_decisions (
 tenant_id text not null,world text not null,
 decision_id text not null check(decision_id~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
 candidate_id text not null,decision text not null check(decision in ('approve','merge_into','reject')),
 outcome text not null check(outcome in ('rejected','merged','published','publication_failed')),
 reviewer_ref text not null,reviewer_principal text not null,expected_revision bigint not null,rationale text not null check(char_length(rationale) between 1 and 2000),
 merge_target_ref text,record jsonb not null check(jsonb_typeof(record)='object'),publication jsonb,authority jsonb not null,
 reflow_status text not null check(reflow_status in ('none','pending','done','waiting')),reflow_report jsonb,
 decided_at timestamptz not null,reflowed_at timestamptz,
 primary key(tenant_id,world,decision_id),
 check((decision='merge_into')=(merge_target_ref is not null)),
 check((outcome in ('merged','published'))=(reflow_status<>'none')),
 foreign key(tenant_id,world,candidate_id) references ontology.nexloop_candidate_definitions
);
create index nexloop_review_decisions_reflow on ontology.nexloop_review_decisions(tenant_id,world,reflow_status,decided_at);
alter table ontology.nexloop_review_decisions owner to nexloop_owner;
alter table ontology.nexloop_review_decisions enable row level security;
alter table ontology.nexloop_review_decisions force row level security;
create policy tenant_boundary on ontology.nexloop_review_decisions to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on ontology.nexloop_review_decisions from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;

-- Owner type / property a publication extends (latest version).
create function ontology.nexloop_review_owner_type(p_candidate jsonb,p_kind text) returns text
 language sql immutable set search_path=pg_catalog as $$
 select case p_kind
  when 'property' then substring(p_candidate->'proposed'->>'owner_type_ref' from '^eios:object_type:([A-Za-z][A-Za-z0-9_]*)$')
  when 'vocabulary_value' then substring(p_candidate->'proposed'->>'property_ref' from '^eios:property:([A-Za-z][A-Za-z0-9_]*)/')
  when 'object_type' then (select string_agg(upper(left(w,1))||substr(w,2),'' order by n) from regexp_split_to_table(p_candidate->'proposed'->>'name','_') with ordinality x(w,n))
 end
$$;

-- Fingerprint of every authority fact of a tenant: publication must leave it unchanged.
create function authz.nexloop_tenant_authority_fingerprint(p_tenant text) returns text
 language sql stable security definer set search_path=pg_catalog set row_security=on as $$
 select encode(sha256(convert_to(coalesce(string_agg(f.fact_kind||'|'||f.entity_key::text||'|'||f.payload::text,E'\n' order by f.fact_kind,f.entity_key),''),'UTF8')),'hex')
 from authz.nexloop_authority_facts f where f.tenant_id=p_tenant
$$;

-- Publication basis for the reviewer's backend: the candidate, the latest owner
-- schema and the active Actions bound to it (review read authority, 0074).
create function authz.nexloop_read_review_publication_basis(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
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
    and exists(select 1 from jsonb_array_elements(a.definition->'object_types') r where r->>'stable_name'=owner and (r->>'version')::integer=v)),'[]'::jsonb));
end $$;

-- Docs/08 §5 + ADR-019 §3.5 structural gates for the additive publication.
create function ontology.nexloop_review_publication_gates(p_tenant text,p_world text,x ontology.nexloop_candidate_definitions,p_pub jsonb)
 returns text[] language plpgsql stable security definer set search_path=pg_catalog set row_security=on as $$
declare f text[]:='{}';owner text:=ontology.nexloop_review_owner_type(x.candidate,x.kind);p jsonb:=x.candidate->'proposed';
 old jsonb;oldv integer;new jsonb:=p_pub->'schema';np jsonb;expected_props jsonb;expected_groups jsonb;vt text;g jsonb;found_group boolean:=false;
 prop jsonb;target text;a jsonb;pred jsonb;i integer;
begin
 if p_world<>'real' then return array['simulation_or_shadow_candidate_cannot_publish_real_schema'];end if;
 -- EIOS model / compatibility failures found by the reviewer's backend are kept as reasons.
 if jsonb_typeof(p_pub->'builder_failures')='array' and jsonb_array_length(p_pub->'builder_failures')>0 then
  return array(select 'eios_'||left(value,200) from jsonb_array_elements_text(p_pub->'builder_failures'));end if;
 if owner is null or owner!~'^[A-Za-z][A-Za-z0-9_]*$' then return array['invalid_owner_reference'];end if;
 if jsonb_typeof(new) is distinct from 'object' or new->>'type_name' is distinct from owner then return array['publication_schema_missing_or_wrong_type'];end if;
 select d.version,d.definition into oldv,old from ontology.object_type_versions d where d.tenant_id=p_tenant and d.type_name=owner order by d.version desc limit 1;
 if x.kind='object_type' then
  if oldv is not null then f:=array_append(f,'type_already_exists'::text);end if;
  if owner!~'^[A-Z][A-Za-z0-9]{0,63}$' then f:=array_append(f,'invalid_type_name'::text);end if;
  if new is distinct from jsonb_build_object('type_name',owner,'display_name',p->>'display_name','description',coalesce(p->>'description',''),'properties','[]'::jsonb,
    'version',1,'title_property','','icon','','color','','plural_display_name','','property_groups','[]'::jsonb,'primary_key','[]'::jsonb,
    'derived_properties','[]'::jsonb,'only_edit_via_actions',true) then f:=array_append(f,'type_definition_not_the_reviewed_candidate'::text);end if;
  if jsonb_array_length(coalesce(p_pub->'actions','[]'::jsonb))<>0 then f:=array_append(f,'new_type_publishes_no_actions'::text);end if;
  return f;
 end if;
 if oldv is null then return array['owner_type_unavailable'];end if;
 if (new->>'version')::integer is distinct from oldv+1 then f:=array_append(f,'not_the_next_schema_version'::text);end if;
 if x.kind='property' then
  vt:=case p->>'value_type' when 'string' then 'string' when 'integer' then 'integer' when 'decimal' then 'number' when 'boolean' then 'boolean' when 'datetime' then 'datetime' end;
  if vt is null or coalesce((p->>'closed_vocabulary')::boolean,false) then f:=array_append(f,'closed_or_enum_property_requires_values'::text);end if;
  if p->>'name' !~ '^[a-z][a-z0-9_]{0,63}$' then f:=array_append(f,'invalid_property_name'::text);end if;
  if exists(select 1 from jsonb_array_elements(old->'properties') e where e->>'property_name'=p->>'name') then f:=array_append(f,'property_already_exists'::text);end if;
  np:=jsonb_build_object('property_name',p->>'name','value_type',vt,'required',false,'nullable',false,'default_value',null,
   'description',coalesce(p->>'description',''),'display_name',p->>'display_name','render_hint','plain','visibility','normal','type_descriptor',null);
  expected_props:=old->'properties'||jsonb_build_array(np);
  expected_groups:='[]'::jsonb;
  for g in select value from jsonb_array_elements(coalesce(old->'property_groups','[]'::jsonb)) loop
   if g->>'group_name'=p->>'property_group' then g:=jsonb_set(g,'{property_names}',(g->'property_names')||to_jsonb(p->>'name'));found_group:=true;end if;
   expected_groups:=expected_groups||jsonb_build_array(g);
  end loop;
  if not found_group then expected_groups:=expected_groups||jsonb_build_array(jsonb_build_object('group_name',p->>'property_group','display_name','','property_names',jsonb_build_array(p->>'name')));end if;
  if new is distinct from (old||jsonb_build_object('version',oldv+1,'properties',expected_props,'property_groups',expected_groups)) then
   f:=array_append(f,'schema_change_not_exactly_the_reviewed_additive_property'::text);end if;
 elsif x.kind='vocabulary_value' then
  target:=substring(p->>'property_ref' from '/([A-Za-z][A-Za-z0-9_]*)$');
  select e into prop from jsonb_array_elements(old->'properties') e where e->>'property_name'=target;
  if prop is null or jsonb_typeof(prop->'type_descriptor'->'enum') is distinct from 'array' or jsonb_array_length(prop->'type_descriptor'->'enum')=0 then
   f:=array_append(f,'vocabulary_property_not_closed'::text);
  elsif prop->'type_descriptor'->'enum' @> jsonb_build_array(p->'value') then f:=array_append(f,'vocabulary_value_already_exists'::text);
  elsif jsonb_typeof(p->'value') is distinct from jsonb_typeof(prop->'type_descriptor'->'enum'->0) then f:=array_append(f,'vocabulary_value_type_mismatch'::text);
  else
   select jsonb_agg(case when e->>'property_name'=target then jsonb_set(e,'{type_descriptor,enum}',(e->'type_descriptor'->'enum')||jsonb_build_array(p->'value')) else e end order by n)
    into expected_props from jsonb_array_elements(old->'properties') with ordinality q(e,n);
   if new is distinct from (old||jsonb_build_object('version',oldv+1,'properties',expected_props)) then
    f:=array_append(f,'schema_change_not_exactly_the_reviewed_vocabulary_value'::text);end if;
  end if;
 else return array['kind_not_publishable'];
 end if;
 -- Successor Actions: exactly the active Actions bound to the old version, each changed
 -- only in version / bound schema reference / derived digests. No new capability or scope.
 if jsonb_array_length(coalesce(p_pub->'actions','[]'::jsonb))<>(select count(*) from control.nexloop_action_definitions d where d.tenant_id=p_tenant and d.world='real' and d.active
   and exists(select 1 from jsonb_array_elements(d.definition->'object_types') r where r->>'stable_name'=owner and (r->>'version')::integer=oldv)) then
  f:=array_append(f,'successor_actions_incomplete'::text);
 end if;
 for a in select value from jsonb_array_elements(coalesce(p_pub->'actions','[]'::jsonb)) loop
  select jsonb_build_object('definition',d.definition,'capability',d.capability,'resource_id',d.resource_id) into pred from control.nexloop_action_definitions d
   where d.tenant_id=p_tenant and d.world='real' and d.active and d.definition->>'stable_name'=a->'definition'->>'stable_name'
    and (d.definition->>'version')::integer=(a->'definition'->>'version')::integer-1
    and exists(select 1 from jsonb_array_elements(d.definition->'object_types') r where r->>'stable_name'=owner and (r->>'version')::integer=oldv);
  if pred is null or a->'capability' is distinct from pred->'capability'
   or exists(select 1 from control.nexloop_action_definitions d where d.tenant_id=p_tenant and d.world='real' and d.resource_id='eios:action:'||(a->'definition'->>'stable_name')||':'||(a->'definition'->>'version'))
   or ((a->'definition')-'version'-'object_types'-'governance'-'contract_digest'-'previous_version'-'created_at')
      is distinct from ((pred->'definition')-'version'-'object_types'-'governance'-'contract_digest'-'previous_version'-'created_at')
   or ((a->'definition'->'governance')-'change_scope') is distinct from ((pred->'definition'->'governance')-'change_scope')
   or ((a->'definition'->'governance'->'change_scope')-'object_types') is distinct from ((pred->'definition'->'governance'->'change_scope')-'object_types') then
   f:=f||('successor_action_changes_more_than_schema_binding:'||coalesce(a->'definition'->>'stable_name','?'));
  end if;
  for i in 0..greatest(jsonb_array_length(a->'definition'->'object_types'),jsonb_array_length(coalesce(pred->'definition'->'object_types','[]'::jsonb)))-1 loop
   if pred is null then exit;end if;
   if ((a->'definition'->'object_types'->i)-'version'-'schema_digest') is distinct from ((pred->'definition'->'object_types'->i)-'version'-'schema_digest')
    or (pred->'definition'->'object_types'->i->>'stable_name'<>owner and a->'definition'->'object_types'->i is distinct from pred->'definition'->'object_types'->i)
    or (pred->'definition'->'object_types'->i->>'stable_name'=owner and (a->'definition'->'object_types'->i->>'version')::integer<>oldv+1) then
    f:=f||('successor_action_reference_invalid:'||coalesce(a->'definition'->>'stable_name','?'));exit;
   end if;
  end loop;
 end loop;
 return f;
end $$;

-- The human review decision. Replays by decision_id; candidate CAS by revision.
create function authz.nexloop_review_decide(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;ident jsonb;t text;principal text;x ontology.nexloop_candidate_definitions%rowtype;
 prior ontology.nexloop_review_decisions%rowtype;outcome text;reflow text:='none';gates text[];pub jsonb;effects jsonb;authority jsonb;fp_before text;
 owner text;oldv integer;target_ok boolean;alias text;rev bigint;act jsonb;now_ts timestamptz:=clock_timestamp();record jsonb;published_refs jsonb:='[]'::jsonb;upgraded bigint:=0;
begin
 if session_user<>'nexloop_api' or p_world is null or octet_length(p_text)>1048576 or octet_length(p_payload)>2097152
  or a->>'protocol' is distinct from 'nexloop-review-decision-v1' or a->>'operation' is distinct from 'execute'
  or a->>'action_resource' is distinct from 'eios:action:ontology.schema.review:1'
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'review decision unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-review-decision-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'review decision unavailable' using errcode='42501';end if;
 -- Only a browser Human: service and agent (model) credentials never enter a browser realm.
 if not exists(select 1 from authz.nexloop_browser_token_realms where token_digest=p_digest) then
  raise exception 'review decisions require a human session' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ident->'binding'->>'subject_kind' is distinct from 'human' or ident->'binding'->>'tenant_id' is distinct from a->>'tenant_id'
  or ident->'binding'->>'subject_principal_id' is distinct from a->>'principal_id' then
  raise exception 'review decisions require a human session' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 t:=a->>'tenant_id';principal:=a->>'principal_id';
 perform set_config('eios.tenant_id',t,true);
 if c->>'decision' not in ('approve','merge_into','reject') or c->>'decision_id'!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
  or c->>'decision_id' is null or jsonb_typeof(c->'expected_revision') is distinct from 'number' or char_length(coalesce(c->>'rationale',''))not between 1 and 2000
  or ((c->>'decision')='merge_into')<>(c->>'merge_target_ref' is not null) then
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
 if c->>'decision'='reject' then
  rev:=ontology.nexloop_candidate_transition(t,p_world,x.candidate_id,x.revision,'pending_review','rejected','human','human:'||principal,left(c->>'rationale',500),null);
  outcome:='rejected';
 elsif c->>'decision'='merge_into' then
  if x.kind not in ('property','vocabulary_value','object_type') then raise exception 'instance merge requires identity resolution, not an alias' using errcode='42501';end if;
  owner:=substring(c->>'merge_target_ref' from '^(?:eios:object_type:|eios:property:|nexloop:vocabulary:)([A-Za-z][A-Za-z0-9_]*)');
  select exists(select 1 from ontology.nexloop_recall_definition_rows(t,owner,ontology.nexloop_recall_latest_version(t,owner)) d
   where d.ref=c->>'merge_target_ref' and d.target_kind=x.kind
    and (x.kind<>'vocabulary_value' or d.ref like 'nexloop:vocabulary:'||substr(x.candidate->'proposed'->>'property_ref',15)||'/%')
    and (x.kind<>'property' or d.ref like 'eios:property:'||substring(x.candidate->'proposed'->>'owner_type_ref' from '^eios:object_type:(.*)$')||'/%'))
   into target_ok;
  if owner is null or not coalesce(target_ok,false) then raise exception 'merge target is not a live definition of the same kind and scope' using errcode='22023';end if;
  alias:=case x.kind when 'vocabulary_value' then x.candidate->'proposed'->>'value' else x.candidate->'proposed'->>'display_name' end;
  effects:=ontology.nexloop_candidate_merge_effects(t,p_world,x.candidate_id,c->>'merge_target_ref',alias,'review:'||(c->>'decision_id'));
  rev:=ontology.nexloop_candidate_transition(t,p_world,x.candidate_id,x.revision,'pending_review','merged','human','human:'||principal,left(c->>'rationale',500),
   jsonb_build_object('merge_target_ref',c->>'merge_target_ref','alias_id',effects->>'alias_id','decision_id',c->>'decision_id'));
  outcome:='merged';reflow:='pending';
 else
  pub:=c->'publication';
  gates:=ontology.nexloop_review_publication_gates(t,p_world,x,pub);
  owner:=ontology.nexloop_review_owner_type(x.candidate,x.kind);
  select max(version) into oldv from ontology.object_type_versions where tenant_id=t and type_name=owner;
  if cardinality(gates)>0 then
   -- Publication refused: the candidate stays pending_review with the reason; nothing retries.
   update ontology.nexloop_candidate_definitions set status_reason=left('publication_failed: '||array_to_string(gates,', '),500),updated_at=now_ts
    where tenant_id=t and world=p_world and candidate_id=x.candidate_id;
   outcome:='publication_failed';
   pub:=jsonb_build_object('schema_revision_before',owner||'@'||coalesce(oldv::text,'none'),'gate_failures',to_jsonb(gates),'applied_claim_count',0);
  else
   fp_before:=authz.nexloop_tenant_authority_fingerprint(t);
   insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(t,owner,(pub->'schema'->>'version')::integer,pub->'schema');
   for act in select value from jsonb_array_elements(coalesce(pub->'actions','[]'::jsonb)) loop
    insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability,active)
     values(t,'real','eios:action:'||(act->'definition'->>'stable_name')||':'||(act->'definition'->>'version'),act->'definition',act->'capability',true);
    published_refs:=published_refs||to_jsonb('eios:action:'||(act->'definition'->>'stable_name')||':'||(act->'definition'->>'version'));
   end loop;
   -- Additive change: existing objects stay valid; they move to the published version.
   if oldv is not null then
    update ontology.objects set schema_version=oldv+1,updated_at=now_ts where tenant_id=t and type_name=owner and schema_version=oldv;
    get diagnostics upgraded=row_count;
   end if;
   if authz.nexloop_tenant_authority_fingerprint(t) is distinct from fp_before then
    raise exception 'publication must not change authority' using errcode='42501';end if;
   rev:=ontology.nexloop_candidate_transition(t,p_world,x.candidate_id,x.revision,'pending_review','published','human','human:'||principal,left(c->>'rationale',500),
    jsonb_build_object('decision_id',c->>'decision_id'));
   -- Dependent Claims stay awaiting_definition until the service reflow applies them;
   -- applying needs grants for the successor Action / new property from trusted configuration.
   select coalesce(jsonb_agg(substr(d,7) order by d),'[]'::jsonb) into effects from unnest(x.dependent_claims) d where d like 'claim:%';
   published_refs:=jsonb_build_array(case x.kind when 'property' then 'eios:property:'||owner||'/'||(x.candidate->'proposed'->>'name')
     when 'vocabulary_value' then 'nexloop:vocabulary:'||owner||'/'||substring(x.candidate->'proposed'->>'property_ref' from '/([A-Za-z][A-Za-z0-9_]*)$')||'/'||ontology.nexloop_recall_ref_part(x.candidate->'proposed'->>'value')
     else 'eios:object_type:'||owner end,'eios:object_type:'||owner||':'||(pub->'schema'->>'version'))||published_refs;
   pub:=jsonb_build_object('schema_revision_before',owner||'@'||coalesce(oldv::text,'none'),'schema_revision_after',owner||'@'||(pub->'schema'->>'version'),
    'published_refs',published_refs,'applied_claim_count',0,'gate_failures','[]'::jsonb,'upgraded_objects',upgraded,'waiting_claims',effects);
   outcome:='published';reflow:='pending';
  end if;
 end if;
 record:=jsonb_build_object('schema_version','1.0','decision_id',c->>'decision_id','tenant_id',t,'world_id',p_world,'mode',case when p_world='real' then 'real' else 'test' end,
  'candidate_ref','candidate:'||x.candidate_id,'reviewer_ref','human:'||principal,'decision',c->>'decision','rationale',c->>'rationale',
  'expected_candidate_status','pending_review','decided_at',to_char(now_ts at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))
  ||case when c->>'decision'='merge_into' then jsonb_build_object('merge_target_ref',c->>'merge_target_ref') else '{}'::jsonb end
  ||case when c->>'decision'='approve' then jsonb_build_object('publication',pub-'upgraded_objects'-'waiting_claims') else '{}'::jsonb end;
 insert into ontology.nexloop_review_decisions values(t,p_world,c->>'decision_id',x.candidate_id,c->>'decision',outcome,'human:'||principal,principal,
  (c->>'expected_revision')::bigint,c->>'rationale',c->>'merge_target_ref',record,case when c->>'decision'='approve' then pub end,authority,reflow,null,now_ts,null);
 return jsonb_build_object('replay',false,'decision_id',c->>'decision_id','outcome',outcome,'record',record,'publication',pub,'reflow_status',reflow);
end $$;

-- Service reflow bookkeeping (claim-match authority, 0070): list pending decisions, record the result.
create function authz.nexloop_review_reflow(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=authz.nexloop_assert_claim_match_authority(p_digest,p_world,p_text,p_signature,p_payload);c jsonb:=p_payload::jsonb;t text:=a->>'tenant_id';
begin
 if c->>'verb'='pending' then
  return coalesce((select jsonb_agg(jsonb_build_object('decision_id',d.decision_id,'candidate_id',d.candidate_id,'outcome',d.outcome,'publication',d.publication,
    'reflow_status',d.reflow_status) order by d.decided_at,d.decision_id) from ontology.nexloop_review_decisions d
   where d.tenant_id=t and d.world=p_world and d.reflow_status in ('pending','waiting')),'[]'::jsonb);
 elsif c->>'verb'='record' then
  if c->>'status' not in ('done','waiting') or jsonb_typeof(c->'report') is distinct from 'object' then raise exception 'reflow record invalid' using errcode='22023';end if;
  update ontology.nexloop_review_decisions set reflow_status=c->>'status',reflow_report=c->'report',reflowed_at=clock_timestamp(),
   publication=case when publication is null then null else jsonb_set(publication,'{applied_claim_count}',to_jsonb(coalesce((c->'report'->>'applied')::integer,0))) end,
   record=case when record ? 'publication' then jsonb_set(record,'{publication,applied_claim_count}',to_jsonb(coalesce((c->'report'->>'applied')::integer,0))) else record end
   where tenant_id=t and world=p_world and decision_id=c->>'decision_id' and reflow_status in ('pending','waiting');
  if not found then raise exception 'reflow decision unavailable' using errcode='42501';end if;
  return jsonb_build_object('decision_id',c->>'decision_id','reflow_status',c->>'status');
 end if;
 raise exception 'reflow verb invalid' using errcode='22023';
end $$;

-- Workbench: decided items whose Claims still wait for reflow or for grants (never silent).
create function authz.nexloop_read_review_reflow_status(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare t text:=authz.nexloop_assert_review_read(p_digest,p_world,p_text,p_signature,p_payload);
begin
 return coalesce((select jsonb_agg(jsonb_build_object('decision_id',d.decision_id,'candidate_id',d.candidate_id,'decision',d.decision,'outcome',d.outcome,
   'display_name',c.candidate->'proposed'->>'display_name','reflow_status',d.reflow_status,'reason',d.reflow_report->>'reason',
   'published_refs',coalesce(d.publication->'published_refs','[]'::jsonb),'waiting_claim_count',cardinality(c.dependent_claims),'decided_at',d.decided_at)
   order by d.decided_at,d.decision_id) from ontology.nexloop_review_decisions d join ontology.nexloop_candidate_definitions c
   on c.tenant_id=d.tenant_id and c.world=d.world and c.candidate_id=d.candidate_id
  where d.tenant_id=t and d.world=p_world and d.reflow_status in ('pending','waiting')),'[]'::jsonb);
end $$;

-- Trusted configuration doctor: Action versions published by human review decisions
-- (successors that need a grant decision in deploy/authorization/service-grants).
create function control.nexloop_review_published_actions(p_tenant text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
begin
 if session_user<>'nexloop_configurator' then raise exception 'trusted configuration only' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 return coalesce((select jsonb_agg(jsonb_build_object('resource_id',r.ref,'decision_id',d.decision_id,'candidate_id',d.candidate_id,'decided_at',d.decided_at,
   'active',a.active,'predecessor',regexp_replace(r.ref,':([0-9]+)$','')||':'||((substring(r.ref from ':([0-9]+)$'))::integer-1))
   order by d.decided_at,r.ref)
  from ontology.nexloop_review_decisions d cross join lateral jsonb_array_elements_text(coalesce(d.publication->'published_refs','[]'::jsonb)) r(ref)
  left join control.nexloop_action_definitions a on a.tenant_id=d.tenant_id and a.world='real' and a.resource_id=r.ref
  where d.tenant_id=p_tenant and d.outcome='published' and r.ref like 'eios:action:%'),'[]'::jsonb);
end $$;

do $grants$
declare f text;
begin
 foreach f in array array['ontology.nexloop_review_owner_type(jsonb,text)','authz.nexloop_read_review_reflow_status(text,text,text,text,text)',
  'control.nexloop_review_published_actions(text)','authz.nexloop_tenant_authority_fingerprint(text)',
  'authz.nexloop_read_review_publication_basis(text,text,text,text,text)',
  'ontology.nexloop_review_publication_gates(text,text,ontology.nexloop_candidate_definitions,jsonb)',
  'authz.nexloop_review_decide(text,text,text,text,text)','authz.nexloop_review_reflow(text,text,text,text,text)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';
  execute 'revoke all on function '||f||' from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator';
 end loop;
end $grants$;
grant execute on function authz.nexloop_read_review_publication_basis(text,text,text,text,text),authz.nexloop_review_decide(text,text,text,text,text) to nexloop_api;
grant execute on function authz.nexloop_review_reflow(text,text,text,text,text) to nexloop_domain_worker;
grant execute on function authz.nexloop_read_review_reflow_status(text,text,text,text,text) to nexloop_api,nexloop_domain_worker;
grant execute on function control.nexloop_review_published_actions(text) to nexloop_configurator;
