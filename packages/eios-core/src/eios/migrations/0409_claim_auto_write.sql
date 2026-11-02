-- Claim → Consumer property, phase one (plan docs/implementation/claim-to-consumer-property-plan.md §9.1 and §10 item 1, L6
-- condition 1; temporary number, the dispatcher numbers it at batch time): the per-property auto-write whitelist, enforced in SQL.
-- * control.nexloop_auto_write_properties: the (type, property) pairs the claim matcher may write without review, per tenant and
--   world. Empty by default: default deny. Published only from a manifest by the trusted configuration identity
--   (control.nexloop_configure_auto_write; scripts/deploy/auto_write_apply.py, deploy/configuration/auto-write.v<N>.json), versions
--   immutable, recorded in control.nexloop_auto_write_publications.
-- * control.nexloop_auto_write_sensitive(type, property): identity, contact, address and payment properties are refused for good,
--   whatever a manifest says: a fixed list (Consumer display_name, external_id_refs, contact_preferences), a name pattern for
--   contact, address, payment and identity documents on every type, and names, ids, gender and age on the Consumer. A
--   manifest naming one is refused as a whole; the check is repeated at every use, so a row can never make one writable.
-- * authz.nexloop_record_claim_match is 0074's body (pg_temp last in its search_path, as every pinned routine) with two edits: a proposal outside the whitelist is recorded with reason
--   auto_write_not_allowed (status proposed, its Claim stays needs_resolution, for review), and the transition to applying is
--   refused (42501 'auto write not allowed') unless every property it writes is whitelisted now. The matcher's configuration
--   (edit_actions) cannot loosen this. Nothing else changes.
create table control.nexloop_auto_write_properties (
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null check(world~'^[A-Za-z][A-Za-z0-9_.:-]{0,127}$'),
 type_name text not null check(type_name~'^[A-Z][A-Za-z0-9_]{0,63}$'),property_name text not null check(property_name~'^[a-z][a-z0-9_]{0,63}$'),
 revision bigint not null check(revision>0),set_by text not null,set_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,type_name,property_name)
);
create table control.nexloop_auto_write_publications (
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null,revision bigint not null check(revision>0),
 manifest_sha256 text not null check(manifest_sha256~'^[0-9a-f]{64}$'),properties integer not null check(properties>=0),
 published_by text not null,published_at timestamptz not null default clock_timestamp(),primary key(tenant_id,world,revision)
);
do $tables$
declare x text;
begin
 foreach x in array array['control.nexloop_auto_write_properties','control.nexloop_auto_write_publications'] loop
  execute format('alter table %s owner to nexloop_owner',x);
  execute format('alter table %s enable row level security',x);
  execute format('alter table %s force row level security',x);
  execute format('create policy tenant_boundary on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',x);
  execute format('revoke all on %s from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator',x);
 end loop;
end $tables$;

create function control.nexloop_auto_write_sensitive(p_type text,p_property text) returns boolean
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select p_property is null or (p_type,p_property) in (('Consumer','display_name'),('Consumer','external_id_refs'),('Consumer','contact_preferences'))
  -- contact, address, payment and identity documents, on every type
  or p_property ~ '(^|_)(email|emails|phone|phones|mobile|tel|telephone|fax|wechat|whatsapp|telegram|qq|contact|contacts|address|addresses|street|city|province|country|postal|postcode|zip|location|locations|geo|latitude|longitude|card|cards|iban|swift|bank|payment|payments|billing|account|accounts|wallet|passport|national|ssn|tax|id_number|birth|birthday|birthdate|dob|password|credential|credentials|token|tokens|secret|secrets)(_|$)'
  -- a person's identity on the Consumer
  or (p_type='Consumer' and p_property ~ '(^|_)(name|names|nickname|id|ids|identity|identities|external|gender|age)(_|$)')
$$;

alter function control.nexloop_auto_write_sensitive(text,text) owner to nexloop_owner;
revoke all on function control.nexloop_auto_write_sensitive(text,text) from public;
grant execute on function control.nexloop_auto_write_sensitive(text,text) to nexloop_configurator;

-- The properties a proposal writes: its property (set_property) or every property of the object it creates.
create function ontology.nexloop_proposal_properties(p_op text,p_property text,p_proposal jsonb) returns text[]
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select case when p_op='set_property' then array[p_property]
  when p_op='create_object' and jsonb_typeof(p_proposal->'operations'->0->'value')='object'
   then array(select k from jsonb_object_keys(p_proposal->'operations'->0->'value') k order by k)
  else '{}'::text[] end
$$;

alter function ontology.nexloop_proposal_properties(text,text,jsonb) owner to nexloop_owner;
revoke all on function ontology.nexloop_proposal_properties(text,text,jsonb) from public;

-- Whitelisted now: at least one property, none sensitive, every one listed for the tenant, world and type.
create function ontology.nexloop_auto_write_allowed(p_tenant text,p_world text,p_type text,p_properties text[]) returns boolean
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v boolean;
begin
 if coalesce(cardinality(p_properties),0)=0 or exists(select 1 from unnest(p_properties) x where control.nexloop_auto_write_sensitive(p_type,x)) then return false;end if;
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 v:=not exists(select 1 from unnest(p_properties) x where not exists(select 1 from control.nexloop_auto_write_properties w
  where w.tenant_id=p_tenant and w.world=p_world and w.type_name=p_type and w.property_name=x));
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
alter function ontology.nexloop_auto_write_allowed(text,text,text,text[]) owner to nexloop_owner;
revoke all on function ontology.nexloop_auto_write_allowed(text,text,text,text[]) from public;

-- Manifest deploy/configuration/auto-write.v<N>.json: {schema_version 'nexloop-auto-write/1', version N, world, properties
-- [{type_name, property_name}]}. Trusted configuration identity only. A version is immutable (same content again: no change;
-- other content under it: refused) and must be newer than the current one; the whitelist is replaced as a whole.
create function control.nexloop_configure_auto_write(p_tenant text,p_manifest jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare m jsonb:=p_manifest;v bigint;w text;sha text:=encode(sha256(convert_to(p_manifest::text,'UTF8')),'hex');cur control.nexloop_auto_write_publications%rowtype;
 head bigint;x jsonb;n integer;
begin
 if session_user<>'nexloop_configurator' then raise exception 'auto write requires trusted configuration' using errcode='42501';end if;
 if jsonb_typeof(m) is distinct from 'object' or exists(select 1 from jsonb_object_keys(m) k where k not in ('schema_version','version','world','properties'))
  or m->>'schema_version' is distinct from 'nexloop-auto-write/1' or jsonb_typeof(m->'version') is distinct from 'number'
  or coalesce(m->>'version','')!~'^[1-9][0-9]{0,8}$' or jsonb_typeof(m->'properties') is distinct from 'array' or jsonb_array_length(m->'properties')>64
  or coalesce(m->>'world','')!~'^[A-Za-z][A-Za-z0-9_.:-]{0,127}$' then
  raise exception 'auto write manifest invalid' using errcode='22023';end if;
 for x in select e from jsonb_array_elements(m->'properties') e loop
  if jsonb_typeof(x) is distinct from 'object' or (select count(*) from jsonb_object_keys(x))<>2
   or coalesce(x->>'type_name','')!~'^[A-Z][A-Za-z0-9_]{0,63}$' or coalesce(x->>'property_name','')!~'^[a-z][a-z0-9_]{0,63}$' then
   raise exception 'auto write manifest invalid' using errcode='22023';end if;
  if control.nexloop_auto_write_sensitive(x->>'type_name',x->>'property_name') then
   raise exception 'auto write property sensitive: %/%',x->>'type_name',x->>'property_name' using errcode='22023';end if;
 end loop;
 if (select count(*) from (select distinct e->>'type_name',e->>'property_name' from jsonb_array_elements(m->'properties') e) d)<>jsonb_array_length(m->'properties') then
  raise exception 'auto write manifest invalid' using errcode='22023';end if;
 v:=(m->>'version')::bigint;w:=m->>'world';
 perform 1 from control.nexloop_tenants where tenant_id=p_tenant and status='active';
 if not found then raise exception 'tenant unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into cur from control.nexloop_auto_write_publications where tenant_id=p_tenant and world=w and revision=v;
 if found then
  if cur.manifest_sha256<>sha then raise exception 'auto write version is immutable' using errcode='42501';end if;
  return jsonb_build_object('version',v,'world',w,'changed',false,'properties',cur.properties,'manifest_sha256',sha);
 end if;
 select max(revision) into head from control.nexloop_auto_write_publications where tenant_id=p_tenant and world=w;
 if head is not null and v<=head then raise exception 'auto write version must be newer than %',head using errcode='42501';end if;
 delete from control.nexloop_auto_write_properties where tenant_id=p_tenant and world=w;
 insert into control.nexloop_auto_write_properties(tenant_id,world,type_name,property_name,revision,set_by)
  select p_tenant,w,e->>'type_name',e->>'property_name',v,session_user from jsonb_array_elements(m->'properties') e;
 get diagnostics n=row_count;
 insert into control.nexloop_auto_write_publications(tenant_id,world,revision,manifest_sha256,properties,published_by) values(p_tenant,w,v,sha,n,session_user);
 return jsonb_build_object('version',v,'world',w,'changed',true,'properties',n,'manifest_sha256',sha);
end $$;
alter function control.nexloop_configure_auto_write(text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_configure_auto_write(text,jsonb) from public;
grant execute on function control.nexloop_configure_auto_write(text,jsonb) to nexloop_configurator;

create or replace function authz.nexloop_record_claim_match(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
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
     effective_at,supersedes_claim,proposal,reason)
    values(t,p_world,p->>'proposal_id',p->>'business_intent_ref',cl.claim_id,v_outcome,'proposed',p->>'op',p->>'type_name',p->>'target_ref',p->>'property_name',
     (p->>'effective_at')::timestamptz,p->>'supersedes_claim',p->'proposal',
     case when ontology.nexloop_auto_write_allowed(t,p_world,p->>'type_name',ontology.nexloop_proposal_properties(p->>'op',p->>'property_name',p->'proposal'))
      then '' else 'auto_write_not_allowed' end)
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
  -- Claim auto-write whitelist: a proposal outside it is never applied by the matcher (it stays for review).
  if c->>'to'='applying' and not ontology.nexloop_auto_write_allowed(t,p_world,pr.type_name,ontology.nexloop_proposal_properties(pr.op,pr.property_name,pr.proposal)) then
   raise exception 'auto write not allowed' using errcode='42501';end if;
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
