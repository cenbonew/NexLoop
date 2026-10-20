-- NX-039 V4 semantic release and rollback (design docs/implementation/NX-039-design.md §7, §8, §11 V4; owner D39-7, D39-8, D39-9,
-- D39-10, D39-12, D39-13, D39-14 decided 2026-10-20). The migration number lives only in the file name and the catalog (renumbered at
-- integration); nothing below repeats it.
--
-- 1. ontology.nexloop_semantic_releases (one row per version, MAJOR.MINOR.PATCH per tenant and world, never moving back),
--    ontology.nexloop_semantic_release_items (the full set of a version: aliases and semantic notes, each with its source candidate
--    and the Consumers its candidate came from) and ontology.nexloop_semantic_release_pointers (the current version). Items are their
--    own table; the NX-045 alias table is not changed (D39-13). Releases and items are immutable; only the retention sweep deletes.
-- 2. Governed human Actions, owner only (D39-8; the "ontology.semantic.release" scope is these two owner-only Action grants):
--    semantic_release.publish / publish_semantic_release and semantic_release.rollback / rollback_semantic_release.
--    Publish: only a semantic_content candidate in accepted_for_release whose latest report accepts it with change class patch or
--    minor (the class is the one classify_change wrote into the report, D39-7: never a parameter); a major or refused class is closed;
--    the dimension must not be stopped; the Parent must be the current release ('release:<version>', or none yet); no source Consumer
--    erasing. The owner supplies the item text: an alias must hash (sha256 of the trimmed text, the E3 term hash) to one of the
--    patch's term hashes and target one of its refs, and must not already name another definition; a semantic note (<= 200
--    characters) is the only way a note exists (D39-14). The new version carries the current items plus these.
--    Rollback: a new version whose items equal the target version's, minus items whose source Consumer is being erased (§9); the
--    version number keeps rising (§7.2). Claims are never changed (D39-9): the release records a report with the ids and the count
--    of Claims matched during the rolled-back window through a definition whose alias leaves. No automatic rollback (D39-10).
-- 3. Reads: authz.nexloop_context_read (renamed and wrapped, body unchanged) merges the current release's aliases into each
--    definition's `aliases` and adds its `semantic_note`, for the semantic browse/resolve that feeds Context `semantics`; the recall
--    index gets one lexical alias row per current release alias (source release-alias:*), rewritten on every publish and rollback.
-- 4. Retention (D39-12): class semantic_release, 365 days (settings v6): release records and evaluation reports older than that leave,
--    except the current version and the one before it and any report a retained release or a review link still names.
-- 5. Workbench observe verb semantic_releases (owner only): the current version, the history with rollback reports, and the
--    candidates that may be published (ids, class, report digest, refs, term-hash counts; never a message).

-- 1. Records ------------------------------------------------------------------------------------------------------------------------
create table ontology.nexloop_semantic_releases (
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null check(world='real'),release_id uuid not null,
 version text not null check(version~'^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,6}$'),
 major integer not null check(major>=0),minor integer not null check(minor>=0),patch integer not null check(patch>=0),
 reason text not null check(reason in ('publish','rollback')),
 candidate_id uuid,report_digest text check(report_digest is null or report_digest~'^[0-9a-f]{64}$'),change_class text check(change_class in ('patch','minor')),
 rolled_back_to text,rollback_report jsonb not null default '{}'::jsonb check(jsonb_typeof(rollback_report)='object' and octet_length(rollback_report::text)<=262144),
 owner_reason text not null check(char_length(owner_reason) between 1 and 500),published_by text not null,request_intent text not null,
 recorded_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,release_id),unique(tenant_id,world,version),
 check(version=major||'.'||minor||'.'||patch),
 check((reason='publish')=(candidate_id is not null and report_digest is not null and change_class is not null)),
 check((reason='rollback')=(rolled_back_to is not null)));
create table ontology.nexloop_semantic_release_items (
 tenant_id text not null,world text not null,release_id uuid not null,item_no integer not null check(item_no between 1 and 2000),
 kind text not null check(kind in ('alias','semantic_note')),
 ref text not null check(char_length(ref)<=512 and ref~'^(eios:object_type:|eios:property:|nexloop:vocabulary:)[^[:cntrl:][:space:]]+$'),
 body text not null check(char_length(body) between 1 and 200 and body!~'[[:cntrl:]]' and body=btrim(body)),
 source_candidate_id uuid not null,source_consumer_ids text[] not null default '{}'
  check(array_to_string(source_consumer_ids,',')~'^([a-f0-9]{64}(,[a-f0-9]{64})*)?$'),
 primary key(tenant_id,world,release_id,item_no),
 foreign key(tenant_id,world,release_id) references ontology.nexloop_semantic_releases(tenant_id,world,release_id) on delete cascade);
create unique index nexloop_semantic_release_items_one on ontology.nexloop_semantic_release_items(tenant_id,world,release_id,kind,ref,body);
create table ontology.nexloop_semantic_release_pointers (
 tenant_id text not null,world text not null check(world='real'),release_id uuid not null,version text not null,
 updated_at timestamptz not null default clock_timestamp(),primary key(tenant_id,world),
 foreign key(tenant_id,world,release_id) references ontology.nexloop_semantic_releases(tenant_id,world,release_id));

do $$declare t text;begin
 foreach t in array array['nexloop_semantic_releases','nexloop_semantic_release_items','nexloop_semantic_release_pointers'] loop
  execute format('alter table ontology.%I owner to nexloop_owner',t);
  execute format('alter table ontology.%I enable row level security',t);
  execute format('alter table ontology.%I force row level security',t);
  execute format('create policy tenant_boundary on ontology.%I to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on ontology.%I from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_evo_reader,nexloop_evo_writer,nexloop_evo_evaluator',t);
 end loop;
end $$;

create function ontology.nexloop_semantic_release_immutable() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if tg_op='DELETE' and current_user='nexloop_owner' and current_setting('nexloop.semantic_release_purge',true)='on' then return old;end if;
 raise exception 'semantic releases are immutable' using errcode='42501';
end $$;
alter function ontology.nexloop_semantic_release_immutable() owner to nexloop_owner;
revoke all on function ontology.nexloop_semantic_release_immutable() from public;
create trigger semantic_releases_immutable before update or delete on ontology.nexloop_semantic_releases for each row execute function ontology.nexloop_semantic_release_immutable();
create trigger semantic_release_items_immutable before update or delete on ontology.nexloop_semantic_release_items for each row execute function ontology.nexloop_semantic_release_immutable();
create trigger semantic_releases_no_truncate before truncate on ontology.nexloop_semantic_releases for each statement execute function ontology.nexloop_semantic_release_immutable();
create trigger semantic_release_items_no_truncate before truncate on ontology.nexloop_semantic_release_items for each statement execute function ontology.nexloop_semantic_release_immutable();

-- The candidate event for a publication (the candidate keeps accepted_for_release: the contract has no published status, 08 §5).
alter table runtime.nexloop_evo_candidate_events drop constraint nexloop_evo_candidate_events_event_kind_check;
alter table runtime.nexloop_evo_candidate_events add constraint nexloop_evo_candidate_events_event_kind_check check(event_kind in
 ('diagnosis_opened','diagnosis_closed','proposed','status_changed','budget_reserved','invalidated','bundle_frozen','report_recorded','review_linked',
  'evaluation_opened','evaluation_concluded','l1_opened','l1_closed','published'));  -- V3's kinds kept (integration)

-- 2. Helpers --------------------------------------------------------------------------------------------------------------------------
-- The current release's items for one ref (aliases sorted; the latest note), empty when nothing is released.
create function runtime.nexloop_semantic_release_view(p_tenant text,p_world text,p_ref text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 return (select jsonb_build_object(
  'aliases',coalesce((select jsonb_agg(i.body order by i.body) from ontology.nexloop_semantic_release_items i join ontology.nexloop_semantic_release_pointers p
    on p.tenant_id=i.tenant_id and p.world=i.world and p.release_id=i.release_id where i.tenant_id=p_tenant and i.world=p_world and i.ref=p_ref and i.kind='alias'),'[]'::jsonb),
  'semantic_note',(select i.body from ontology.nexloop_semantic_release_items i join ontology.nexloop_semantic_release_pointers p
    on p.tenant_id=i.tenant_id and p.world=i.world and p.release_id=i.release_id where i.tenant_id=p_tenant and i.world=p_world and i.ref=p_ref and i.kind='semantic_note'
    order by i.item_no desc limit 1),
  'version',(select p.version from ontology.nexloop_semantic_release_pointers p where p.tenant_id=p_tenant and p.world=p_world)));
end $$;

-- The recall index carries one lexical alias row per current release alias (no vectors: lexical recall finds it at once).
create function runtime.nexloop_semantic_release_reindex(p_tenant text,p_world text) returns integer
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r record;d record;n integer:=0;ty text;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 delete from ontology.nexloop_recall_entries where tenant_id=p_tenant and source_key like 'release-alias:%';
 for r in select i.ref,i.body from ontology.nexloop_semantic_release_items i join ontology.nexloop_semantic_release_pointers p
   on p.tenant_id=i.tenant_id and p.world=i.world and p.release_id=i.release_id where i.tenant_id=p_tenant and i.world=p_world and i.kind='alias' order by i.item_no loop
  ty:=substring(r.ref from '^(?:eios:object_type:|eios:property:|nexloop:vocabulary:)([A-Za-z][A-Za-z0-9_]*)');
  -- A ref whose definition is not (or no longer) indexed is skipped; the release itself never fails on the index.
  begin
   select * into d from ontology.nexloop_recall_definition_rows(p_tenant,ty,ontology.nexloop_recall_latest_version(p_tenant,ty)) x where x.ref=r.ref limit 1;
  exception when insufficient_privilege or invalid_parameter_value then continue;
  end;
  if found then
   perform ontology.nexloop_recall_insert(p_tenant,p_world,'definition','release-alias:'||encode(sha256(convert_to(r.ref||chr(10)||r.body,'UTF8')),'hex'),1,
    d.ref,d.target_kind,d.gate_type,d.gate_property,null,'alias',null,d.property_group,r.body);
   n:=n+1;
  end if;
 end loop;
 return n;
end $$;

create function runtime.nexloop_semantic_release_write(p_tenant text,p_world text,p_version int[],p_reason text,p_candidate uuid,p_report text,p_class text,
  p_target text,p_report_json jsonb,p_owner_reason text,p_principal text,p_intent text,p_items jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_id uuid:=gen_random_uuid();v_version text:=p_version[1]||'.'||p_version[2]||'.'||p_version[3];it jsonb;k integer:=0;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 insert into ontology.nexloop_semantic_releases(tenant_id,world,release_id,version,major,minor,patch,reason,candidate_id,report_digest,change_class,rolled_back_to,
   rollback_report,owner_reason,published_by,request_intent)
  values(p_tenant,p_world,v_id,v_version,p_version[1],p_version[2],p_version[3],p_reason,p_candidate,p_report,p_class,p_target,coalesce(p_report_json,'{}'::jsonb),
   p_owner_reason,p_principal,p_intent);
 for it in select value from jsonb_array_elements(p_items) loop
  k:=k+1;
  insert into ontology.nexloop_semantic_release_items(tenant_id,world,release_id,item_no,kind,ref,body,source_candidate_id,source_consumer_ids)
   values(p_tenant,p_world,v_id,k,it->>'kind',it->>'ref',it->>'body',(it->>'source_candidate_id')::uuid,
    array(select e#>>'{}' from jsonb_array_elements(coalesce(it->'source_consumer_ids','[]'::jsonb)) e));
 end loop;
 insert into ontology.nexloop_semantic_release_pointers(tenant_id,world,release_id,version) values(p_tenant,p_world,v_id,v_version)
  on conflict(tenant_id,world) do update set release_id=excluded.release_id,version=excluded.version,updated_at=clock_timestamp();
 perform runtime.nexloop_semantic_release_reindex(p_tenant,p_world);
 return jsonb_build_object('release_id',v_id,'version',v_version,'items',k);
end $$;

-- The items of one release as the writer takes them.
create function runtime.nexloop_semantic_release_items_of(p_tenant text,p_world text,p_release uuid) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 return (select coalesce(jsonb_agg(jsonb_build_object('kind',i.kind,'ref',i.ref,'body',i.body,'source_candidate_id',i.source_candidate_id,
   'source_consumer_ids',to_jsonb(i.source_consumer_ids)) order by i.item_no),'[]'::jsonb)
  from ontology.nexloop_semantic_release_items i where i.tenant_id=p_tenant and i.world=p_world and i.release_id=p_release);
end $$;

-- 3. Publish ---------------------------------------------------------------------------------------------------------------------------
create function control.nexloop_governed_publish_semantic_release(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare x runtime.nexloop_evo_candidates;rep runtime.nexloop_evo_reports;cur ontology.nexloop_semantic_release_pointers;patch jsonb;v_sources text[];
 v_class text;it jsonb;v_text text;v_kind text;v_items jsonb:='[]'::jsonb;v_new jsonb:='[]'::jsonb;v_ver int[];v_out jsonb;
begin
 if exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','candidate_id','items','reason'))
  or coalesce(body->>'candidate_id','')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
  or jsonb_typeof(body->'items') is distinct from 'array' or jsonb_array_length(body->'items') not between 1 and 20
  or length(coalesce(body->>'reason','')) not between 1 and 500 then raise exception 'semantic release: invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into x from runtime.nexloop_evo_candidates where tenant_id=p_tenant and world=p_world and candidate_id=(body->>'candidate_id')::uuid for update;
 if not found then raise exception 'semantic release: unknown_candidate' using errcode='22023';end if;
 if x.dimension<>'semantic_content' then raise exception 'semantic release: dimension_closed' using errcode='22023';end if;
 if exists(select 1 from control.nexloop_evo_dimension_stops s where s.tenant_id=p_tenant and s.world=p_world and s.dimension=x.dimension and s.stopped) then
  raise exception 'semantic release: dimension_stopped' using errcode='22023';end if;
 if x.status<>'accepted_for_release' then raise exception 'semantic release: not_accepted' using errcode='22023';end if;
 if exists(select 1 from ontology.nexloop_semantic_releases r where r.tenant_id=p_tenant and r.world=p_world and r.candidate_id=x.candidate_id) then
  raise exception 'semantic release: already_published' using errcode='22023';end if;
 select * into rep from runtime.nexloop_evo_reports r where r.tenant_id=p_tenant and r.world=p_world and r.candidate_id=x.candidate_id order by r.recorded_at desc limit 1;
 if not found or rep.conclusion<>'accept' then raise exception 'semantic release: not_accepted' using errcode='22023';end if;
 -- D39-7: the class is the evaluator's classify_change result in the report; nothing in the request can change it.
 v_class:=rep.report->>'change_class';
 if v_class is null or v_class not in ('patch','minor') then raise exception 'semantic release: breaking_change' using errcode='22023';end if;
 if exists(select 1 from runtime.nexloop_evo_review_links l where l.tenant_id=p_tenant and l.world=p_world and l.candidate_id=x.candidate_id
   and l.report_digest<>rep.report_digest) then raise exception 'semantic release: report_mismatch' using errcode='22023';end if;
 select * into cur from ontology.nexloop_semantic_release_pointers where tenant_id=p_tenant and world=p_world for update;
 if (cur.version is null and x.parent_revision like 'release:%') or (cur.version is not null and x.parent_revision is distinct from 'release:'||cur.version) then
  raise exception 'semantic release: parent_moved' using errcode='22023';end if;
 select p.body into patch from runtime.nexloop_evo_patches p where p.tenant_id=p_tenant and p.world=p_world and p.patch_id=x.patch_id;
 v_sources:=array(select s.consumer_id from runtime.nexloop_evo_patch_sources s where s.tenant_id=p_tenant and s.world=p_world and s.patch_id=x.patch_id order by 1);
 if patch is null or exists(select 1 from unnest(v_sources) c where runtime.nexloop_consumer_erasing(p_tenant,p_world,c)) then
  raise exception 'semantic release: missing_data' using errcode='22023';end if;
 v_kind:=case patch->>'change_kind' when 'alias' then 'alias' when 'description' then 'semantic_note' end;
 for it in select value from jsonb_array_elements(body->'items') loop
  if jsonb_typeof(it) is distinct from 'object' or (select array_agg(k order by k) from jsonb_object_keys(it) k) is distinct from array['kind','ref','text']
   or jsonb_typeof(it->'text') is distinct from 'string' then raise exception 'semantic release: invalid' using errcode='22023';end if;
  v_text:=btrim(it->>'text');
  if char_length(v_text) not between 1 and 200 or v_text~'[[:cntrl:]]' then raise exception 'semantic release: invalid_text' using errcode='22023';end if;
  if it->>'kind' is distinct from v_kind or not (patch->'target_refs') ? (it->>'ref') then raise exception 'semantic release: item_not_in_patch' using errcode='22023';end if;
  if v_kind='alias' then
   if not (patch->'term_hashes') ? encode(sha256(convert_to(v_text,'UTF8')),'hex') then raise exception 'semantic release: alias_not_in_patch' using errcode='22023';end if;
   -- G8 at publication: an alias text already naming another definition (NX-045 or the current release) is a meaning change.
   if exists(select 1 from ontology.nexloop_definition_aliases a where a.tenant_id=p_tenant and a.world=p_world and lower(a.alias_text)=lower(v_text) and a.canonical_ref<>it->>'ref')
    or exists(select 1 from ontology.nexloop_semantic_release_items i where i.tenant_id=p_tenant and i.world=p_world and i.release_id=cur.release_id and i.kind='alias'
      and lower(i.body)=lower(v_text) and i.ref<>it->>'ref') then
    raise exception 'semantic release: alias_conflict' using errcode='22023';end if;
  end if;
  v_new:=v_new||jsonb_build_array(jsonb_build_object('kind',v_kind,'ref',it->>'ref','body',v_text,'source_candidate_id',x.candidate_id,'source_consumer_ids',to_jsonb(v_sources)));
 end loop;
 -- The new version: the current items (a new note replaces the note of its ref), then these (an alias already present is not doubled).
 if cur.release_id is not null then
  v_items:=coalesce((select jsonb_agg(o order by ord) from jsonb_array_elements(runtime.nexloop_semantic_release_items_of(p_tenant,p_world,cur.release_id)) with ordinality e(o,ord)
   where not (o->>'kind'='semantic_note' and exists(select 1 from jsonb_array_elements(v_new) n where n->>'kind'='semantic_note' and n->>'ref'=o->>'ref'))),'[]'::jsonb);
 end if;
 v_items:=v_items||coalesce((select jsonb_agg(n) from (select distinct on (n->>'kind',n->>'ref',n->>'body') n from jsonb_array_elements(v_new) n) d
  where not exists(select 1 from jsonb_array_elements(v_items) o where o->>'kind'=n->>'kind' and o->>'ref'=n->>'ref' and o->>'body'=n->>'body')),'[]'::jsonb);
 select case when cur.release_id is null then array[0,0,0] else array[r.major,r.minor,r.patch] end into v_ver
  from (select 1) one left join ontology.nexloop_semantic_releases r on r.tenant_id=p_tenant and r.world=p_world and r.release_id=cur.release_id;
 v_ver:=case v_class when 'minor' then array[v_ver[1],v_ver[2]+1,0] else array[v_ver[1],v_ver[2],v_ver[3]+1] end;
 v_out:=runtime.nexloop_semantic_release_write(p_tenant,p_world,v_ver,'publish',x.candidate_id,rep.report_digest,v_class,null,null,body->>'reason',p_principal,p_intent,v_items);
 perform runtime.nexloop_evo_event(p_tenant,x.diagnosis_id,x.candidate_id,'published',x.status,x.status,null,
  jsonb_build_object('release_id',v_out->>'release_id','version',v_out->>'version','change_class',v_class,'report_digest',rep.report_digest),'human:'||p_principal);
 return v_out||jsonb_build_object('change_class',v_class,'reason','publish');
end $$;

-- 4. Rollback --------------------------------------------------------------------------------------------------------------------------
create function control.nexloop_governed_rollback_semantic_release(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare cur ontology.nexloop_semantic_release_pointers;now_r ontology.nexloop_semantic_releases;tgt ontology.nexloop_semantic_releases;v_items jsonb;v_kept jsonb;
 v_dropped integer;v_removed jsonb;v_refs text[];v_since timestamptz;v_claims text[];v_report jsonb;v_out jsonb;
begin
 if exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','target_version','reason'))
  or coalesce(body->>'target_version','')!~'^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,6}$' or length(coalesce(body->>'reason','')) not between 1 and 500 then
  raise exception 'semantic release: invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into cur from ontology.nexloop_semantic_release_pointers where tenant_id=p_tenant and world=p_world for update;
 if not found then raise exception 'semantic release: no_release' using errcode='22023';end if;
 select * into now_r from ontology.nexloop_semantic_releases where tenant_id=p_tenant and world=p_world and release_id=cur.release_id;
 select * into tgt from ontology.nexloop_semantic_releases where tenant_id=p_tenant and world=p_world and version=body->>'target_version';
 if not found then raise exception 'semantic release: unknown_version' using errcode='22023';end if;
 if tgt.release_id=cur.release_id then raise exception 'semantic release: already_current' using errcode='22023';end if;
 v_items:=runtime.nexloop_semantic_release_items_of(p_tenant,p_world,tgt.release_id);
 -- §9: an item whose source Consumer is being erased never comes back with a rollback.
 v_kept:=coalesce((select jsonb_agg(i order by ord) from jsonb_array_elements(v_items) with ordinality e(i,ord)
   where not exists(select 1 from jsonb_array_elements_text(i->'source_consumer_ids') c where runtime.nexloop_consumer_erasing(p_tenant,p_world,c))),'[]'::jsonb);
 v_dropped:=jsonb_array_length(v_items)-jsonb_array_length(v_kept);
 -- D39-9: Claims stay as they are; the report names the Claims matched, while the current version was live, through a definition
 -- whose alias the rollback removes (ids and count only, for human review).
 v_removed:=coalesce((select jsonb_agg(o) from jsonb_array_elements(runtime.nexloop_semantic_release_items_of(p_tenant,p_world,cur.release_id)) o
   where o->>'kind'='alias' and not exists(select 1 from jsonb_array_elements(v_kept) k where k->>'kind'='alias' and k->>'ref'=o->>'ref' and k->>'body'=o->>'body')),'[]'::jsonb);
 v_refs:=array(select distinct o->>'ref' from jsonb_array_elements(v_removed) o);
 select min(r.recorded_at) into v_since from ontology.nexloop_semantic_releases r where r.tenant_id=p_tenant and r.world=p_world
  and (r.major,r.minor,r.patch)>(tgt.major,tgt.minor,tgt.patch);
 v_claims:=array(select distinct m.claim_id from ontology.nexloop_claim_matches m where m.tenant_id=p_tenant and m.world=p_world
   and m.outcome in ('full_match','partial_match') and m.recorded_at>=coalesce(v_since,now_r.recorded_at)
   and exists(select 1 from jsonb_array_elements(m.recall) h where jsonb_typeof(h)='object' and h->>'ref'=any(v_refs)) order by 1);
 v_report:=jsonb_build_object('from_version',cur.version,'to_version',tgt.version,'window_start',coalesce(v_since,now_r.recorded_at),
  'removed_aliases',jsonb_array_length(v_removed),'removed_refs',to_jsonb(v_refs),'dropped_erased_items',v_dropped,
  'claim_count',cardinality(v_claims),'claim_ids',to_jsonb(v_claims[1:1000]),'claims_changed',0);
 v_out:=runtime.nexloop_semantic_release_write(p_tenant,p_world,array[now_r.major,now_r.minor,now_r.patch+1],'rollback',null,null,null,tgt.version,v_report,
  body->>'reason',p_principal,p_intent,v_kept);
 return v_out||jsonb_build_object('reason','rollback','rolled_back_to',tgt.version,'report',v_report);
end $$;

do $owners$
declare f text;
begin
 foreach f in array array['runtime.nexloop_semantic_release_view(text,text,text)','runtime.nexloop_semantic_release_reindex(text,text)',
   'runtime.nexloop_semantic_release_write(text,text,integer[],text,uuid,text,text,text,jsonb,text,text,text,jsonb)',
   'runtime.nexloop_semantic_release_items_of(text,text,uuid)',
   'control.nexloop_governed_publish_semantic_release(text,text,text,text,text,jsonb)','control.nexloop_governed_rollback_semantic_release(text,text,text,text,text,jsonb)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';execute 'revoke all on function '||f||' from public';
 end loop;
end $owners$;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('semantic_release.publish','publish_semantic_release','human','control.nexloop_governed_publish_semantic_release(text,text,text,text,text,jsonb)','NX-039'),
 ('semantic_release.rollback','rollback_semantic_release','human','control.nexloop_governed_rollback_semantic_release(text,text,text,text,text,jsonb)','NX-039');

-- 5. Definition reads merge the release (context read renamed and wrapped; its body is unchanged) ---------------------------------------
alter function authz.nexloop_context_read(text,text,text,text,text) rename to nexloop_context_read_before_semantic_release;
revoke all on function authz.nexloop_context_read_before_semantic_release(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker;
create function authz.nexloop_context_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v jsonb:=authz.nexloop_context_read_before_semantic_release(p_digest,p_world,p_text,p_signature,p_payload);v_tenant text;
begin
 if (p_payload::jsonb)->>'verb' is distinct from 'definitions' or v is null or jsonb_typeof(v->'items') is distinct from 'array' then return v;end if;
 v_tenant:=(p_text::jsonb)->>'tenant_id';  -- checked against the authenticated identity by the wrapped read
 return v||jsonb_build_object('items',coalesce((select jsonb_agg(
   case when rv->'aliases'='[]'::jsonb and rv->'semantic_note'='null'::jsonb then i
    else i||jsonb_build_object('aliases',(select coalesce(jsonb_agg(a order by a),'[]'::jsonb) from (select jsonb_array_elements_text(coalesce(i->'aliases','[]'::jsonb)) a
       union select jsonb_array_elements_text(rv->'aliases')) u))
     ||case when rv->'semantic_note'='null'::jsonb then '{}'::jsonb else jsonb_build_object('semantic_note',rv->'semantic_note') end end order by ord)
  from jsonb_array_elements(v->'items') with ordinality e(i,ord) cross join lateral runtime.nexloop_semantic_release_view(v_tenant,p_world,i->>'ref') rv),'[]'::jsonb));
end $$;
alter function authz.nexloop_context_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_context_read(text,text,text,text,text) from public;
grant execute on function authz.nexloop_context_read(text,text,text,text,text) to nexloop_api,nexloop_domain_worker;

-- 6. Retention (D39-12): class semantic_release, 365 days ---------------------------------------------------------------------------------
create or replace function runtime.nexloop_evo_frozen_rows() returns trigger
 language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 -- D39-12: the retention sweep alone may delete an expired report, under its transaction-local flag.
 if tg_op='DELETE' and tg_table_name='nexloop_evo_reports' and current_user='nexloop_owner' and current_setting('nexloop.semantic_release_purge',true)='on' then
  return old;end if;
 raise exception 'evaluation bundles, reports and review links are frozen' using errcode='42501';
end $$;

create or replace function control.nexloop_retention_settings_check() returns trigger
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare k text;v jsonb;
begin
 for k,v in select * from jsonb_each(new.definition->'classes') loop
  if k not in ('message_text','claim_quote','prompt_text','job_input','commercial_receipt','metric_observation','run_files','commitment_closed','claim_superseded',
   'plan_closed','effect_text','job_records','model_request_manifest','read_audit','governance_audit','semantic_access','semantic_release')
   or jsonb_typeof(v->'days') is distinct from 'number' or (v->>'days')!~'^[0-9]{1,5}$' or (v->>'days')::integer not between 1 and 36500
   or (jsonb_typeof(v->'source')<>'null' and (new.definition->'classes'->(v->>'source') is null
     or (v->>'days')::integer>(new.definition->'classes'->(v->>'source')->>'days')::integer)) then
   raise exception 'retention class % invalid',k using errcode='22023';end if;
 end loop;
 if (new.definition->'sweep'->>'batch')::integer not between 1 and 1000 or (new.definition->'sweep'->>'max_passes')::integer not between 1 and 100
  or (new.definition->'sweep'->>'interval_seconds')::integer not between 60 and 86400 then
  raise exception 'retention sweep settings invalid' using errcode='22023';end if;
 return new;
end $$;
-- settings v6: the file deploy/configuration/retention.v1.json version 6 (canonical content), v5 plus semantic_release.
insert into control.nexloop_retention_settings(version,definition,definition_digest,published_by)
 values(6,'{"classes":{"claim_quote":{"days":90,"source":"message_text"},"claim_superseded":{"days":365,"source":null},"commercial_receipt":{"days":90,"source":null},"commitment_closed":{"days":365,"source":null},"effect_text":{"days":90,"source":"message_text"},"governance_audit":{"days":365,"source":null},"job_input":{"days":30,"source":"message_text"},"job_records":{"days":365,"source":null},"message_text":{"days":90,"source":null},"metric_observation":{"days":365,"source":null},"model_request_manifest":{"days":365,"source":null},"plan_closed":{"days":365,"source":null},"prompt_text":{"days":30,"source":"message_text"},"read_audit":{"days":365,"source":null},"run_files":{"days":30,"source":null},"semantic_access":{"days":90,"source":null},"semantic_release":{"days":365,"source":null}},"decision":"NX-029 slice 1 (design docs/implementation/NX-029-design.md §3, owner decisions D1–D7 ''all as recommended'' 2026-10-10, dispatcher D8/D9): tenant-level retention (D9) for the classes the retention keeper executes in slice 1. Product defaults, not legal periods (03:96, 13:31); enterprises adjust by a new version. A derived class never outlives its source (prompt text, Claim quotes and job inputs follow the message text). Financial records and cost entries use financial_retention_days of the commercial settings (NX-027 D6), not a second value here. Expired message text, Claim quotes, prompt text and job inputs are redacted in place (rows, digests and order stay); commercial receipts, metric observations, commercial records and cost entries are deleted; every pass leaves a tombstone without content. v2 (NX-029 slice 4, NX-035 D4 confirmed by the owner 2026-10-10): records — deletion ledger and tombstones are kept 365 days, longer than the 35-day maximum backup retention, so every deletion still in a backup can be replayed after a restore (NX-035 §5, §6.2); a version with backup_max_retention_days not below both is refused. v3 (NX-029 final, AT-026): three expiry classes the keeper now executes - run_files: a Pi Run directory 30 days after its Run is terminal (queued for the Agent Host runs/purge, deleted only when the Host confirms; ADR-005, 06:92); commitment_closed: a Commitment 365 days after it was closed (fulfilled, cancelled or breached), object and ledger deleted (D4); claim_superseded: the structured value and remaining text of a superseded Claim 365 days after it was superseded, ids and the correction chain kept (D2). v4 (NX-029 completion): the remaining expiry classes of the design table - plan_closed: a plan 365 days after its last status became closed or invalidated, all versions, steps, events, Runs, outcomes and its unshared strategies deleted (D3); effect_text: the frozen request text of a terminal effect intent 90 days after it was created, states, receipts and digests kept (follows message_text; ADR-021); job_records: 365 days after a task became terminal its events, inbox and outbox rows and old idempotency records are deleted, the task row too unless a Run binding still refers to it (then its payloads are erased); model_request_manifest: model request rows and their prompt rows 365 days after the request (D3; cost entries keep their own financial retention); read_audit: read audit rows 365 days after the read, folded into per-day counts (D6); governance_audit: audit events, superseded control events (the latest per scope stays), human requests, export requests and completed deletion requests whose ledger entries are gone, 365 days after the event (D6). records: ledger entries and tombstones leave after deletion_ledger_days / tombstone_days, never while a restore replay is open; both must exceed backup_max_retention_days. v5 (NX-038 E3, owner D2 2026-10-12): semantic_access - the append-only per-access records of the Context Engine''s semantic browse/resolve (term hashes, refs and counts, no text) are deleted 90 days after they were recorded; a Consumer erasure deletes the records of that Consumer''s Runs when purging begins. v6 (NX-039 V4, owner D39-12 2026-10-20): semantic_release - semantic release records (with their items and rollback reports) and evaluation reports are deleted 365 days after they were recorded; the current release version and the one before it are always kept, and so is a report that a kept release or a review link still names.","records":{"backup_max_retention_days":35,"deletion_ledger_days":365,"tombstone_days":365},"schema":"nexloop-retention/1","sweep":{"batch":200,"interval_seconds":3600,"max_passes":20},"version":6}','58918c86d673fc7a3dbf4314bd9283fdff32cbf2b46885331ffb879d1bcb4128','migration:nx039-v4');

create function runtime.nexloop_retention_sweep_semantic_release(p_tenant text,p_world text,p_class text,p_batch integer,p_ref text,p_by text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_cut timestamptz;cur ontology.nexloop_semantic_release_pointers;v_prev uuid;n integer;m integer;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 v_cut:=clock_timestamp()-make_interval(days=>runtime.nexloop_retention_days(p_class));
 if p_class<>'semantic_release' or v_cut is null then raise exception 'retention class unavailable' using errcode='22023';end if;
 select * into cur from ontology.nexloop_semantic_release_pointers where tenant_id=p_tenant and world=p_world;
 select r.release_id into v_prev from ontology.nexloop_semantic_releases r join ontology.nexloop_semantic_releases c
   on c.tenant_id=r.tenant_id and c.world=r.world and c.release_id=cur.release_id
  where r.tenant_id=p_tenant and r.world=p_world and (r.major,r.minor,r.patch)<(c.major,c.minor,c.patch) order by r.major desc,r.minor desc,r.patch desc limit 1;
 perform set_config('nexloop.semantic_release_purge','on',true);
 with due as (select release_id from ontology.nexloop_semantic_releases where tenant_id=p_tenant and world=p_world and recorded_at<v_cut
   and release_id is distinct from cur.release_id and release_id is distinct from v_prev order by recorded_at limit p_batch)
 delete from ontology.nexloop_semantic_releases r using due where r.tenant_id=p_tenant and r.world=p_world and r.release_id=due.release_id;
 get diagnostics n=row_count;
 with due as (select report_id from runtime.nexloop_evo_reports e where e.tenant_id=p_tenant and e.world=p_world and e.recorded_at<v_cut
   and not exists(select 1 from ontology.nexloop_semantic_releases r where r.tenant_id=e.tenant_id and r.world=e.world and r.report_digest=e.report_digest)
   and not exists(select 1 from runtime.nexloop_evo_review_links l where l.tenant_id=e.tenant_id and l.world=e.world and l.report_digest=e.report_digest)
   order by e.recorded_at limit p_batch)
 delete from runtime.nexloop_evo_reports e using due where e.tenant_id=p_tenant and e.world=p_world and e.report_id=due.report_id;
 get diagnostics m=row_count;
 perform set_config('nexloop.semantic_release_purge','',true);
 if n+m>0 then
  perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,'retention_expiry',p_ref,p_class,'batch',p_class,'deleted',
   jsonb_build_object('semantic_releases',n,'evaluation_reports',m),p_by);
 end if;
 return jsonb_build_object('item_class',p_class,'processed',n+m,'more',n>=p_batch or m>=p_batch,'counts',jsonb_build_object('semantic_releases',n,'evaluation_reports',m),
  'cutoff',runtime.nexloop_commercial_ts(v_cut));
end $$;
alter function runtime.nexloop_retention_sweep_semantic_release(text,text,text,integer,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_retention_sweep_semantic_release(text,text,text,integer,text,text) from public;
-- The hold wrapper (latest body, 0234, copied) dispatches the new class; holds are checked first as before.
create or replace function runtime.nexloop_retention_sweep(p_tenant text,p_world text,p_class text,p_batch integer,p_ref text,p_by text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if runtime.nexloop_retention_held(p_tenant,p_world,'item_class',p_class) then
  perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,'retention_expiry',p_ref,p_class,'batch',p_class,'skipped_hold','{}'::jsonb,p_by);
  return jsonb_build_object('item_class',p_class,'processed',0,'more',false,'counts','{}'::jsonb,'held',true);
 end if;
 if p_class='semantic_release' then  -- NX-039 V4 (D39-12)
  return runtime.nexloop_retention_sweep_semantic_release(p_tenant,p_world,p_class,p_batch,p_ref,p_by);
 end if;
 if p_class='semantic_access' then
  return runtime.nexloop_retention_sweep_semantic_access(p_tenant,p_world,p_class,p_batch,p_ref,p_by);
 end if;
 if p_class in ('plan_closed','effect_text','job_records','model_request_manifest','read_audit','governance_audit','records') then
  return runtime.nexloop_retention_sweep_records(p_tenant,p_world,p_class,p_batch,p_ref,p_by);
 end if;
 if p_class in ('run_files','commitment_closed','claim_superseded') then
  return runtime.nexloop_retention_sweep_expiry(p_tenant,p_world,p_class,p_batch,p_ref,p_by);
 end if;
 return runtime.nexloop_retention_sweep_before_holds_v0142(p_tenant,p_world,p_class,p_batch,p_ref,p_by);
end $$;

-- 7. The owner's view (workbench observe; every other verb unchanged) ---------------------------------------------------------------------
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) rename to nexloop_workbench_observe_read_before_semantic_release;
revoke all on function authz.nexloop_workbench_observe_read_before_semantic_release(text,text,text,text,text) from public,nexloop_api;
create function authz.nexloop_workbench_observe_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;w jsonb;cfg control.nexloop_workbench_configurations;v_tenant text;
 v_action text:='eios:action:nexloop.workbench.read:1';cur ontology.nexloop_semantic_release_pointers;
begin
 if c->>'verb' is distinct from 'semantic_releases' then
  return authz.nexloop_workbench_observe_read_before_semantic_release(p_digest,p_world,p_text,p_signature,p_payload);end if;
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
 w:=authz.nexloop_workbench_member(p_digest,p_world);v_tenant:=w->>'tenant_id';cfg:=control.nexloop_workbench_current(v_tenant);
 if a->>'tenant_id' is distinct from v_tenant or a->>'principal_id' is distinct from w->>'principal_id' or not (cfg.roles->(w->>'role') ? v_action)
  or w->>'role'<>'owner' then
  raise exception 'workbench observe forbidden' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 if (select count(*) from jsonb_object_keys(c))<>1 then raise exception 'workbench observe invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 select * into cur from ontology.nexloop_semantic_release_pointers where tenant_id=v_tenant and world=p_world;
 return jsonb_build_object('current',cur.version,
  'releases',coalesce((select jsonb_agg(jsonb_build_object('version',r.version,'reason',r.reason,'candidate_id',r.candidate_id,'change_class',r.change_class,
     'report_digest',r.report_digest,'rolled_back_to',r.rolled_back_to,'rollback_report',case when r.reason='rollback' then r.rollback_report end,
     'owner_reason',r.owner_reason,'recorded_at',r.recorded_at,
     'items',(select coalesce(jsonb_agg(jsonb_build_object('kind',i.kind,'ref',i.ref,'body',i.body) order by i.item_no),'[]'::jsonb)
       from ontology.nexloop_semantic_release_items i where i.tenant_id=r.tenant_id and i.world=r.world and i.release_id=r.release_id))
    order by r.major desc,r.minor desc,r.patch desc) from (select * from ontology.nexloop_semantic_releases where tenant_id=v_tenant and world=p_world
     order by major desc,minor desc,patch desc limit 20) r),'[]'::jsonb),
  'publishable',coalesce((select jsonb_agg(jsonb_build_object('candidate_id',x.candidate_id,'change_class',rep.report->>'change_class','report_digest',rep.report_digest,
     'change_kind',p.body->>'change_kind','target_refs',p.body->'target_refs','term_hashes',jsonb_array_length(p.body->'term_hashes'),
     'parent_ok',(cur.version is null and x.parent_revision not like 'release:%') or x.parent_revision='release:'||cur.version) order by x.updated_at desc)
    from runtime.nexloop_evo_candidates x
    join lateral (select * from runtime.nexloop_evo_reports e where e.tenant_id=x.tenant_id and e.world=x.world and e.candidate_id=x.candidate_id
      order by e.recorded_at desc limit 1) rep on true
    join runtime.nexloop_evo_patches p on p.tenant_id=x.tenant_id and p.world=x.world and p.patch_id=x.patch_id
    where x.tenant_id=v_tenant and x.world=p_world and x.status='accepted_for_release' and x.dimension='semantic_content' and rep.conclusion='accept'
     and rep.report->>'change_class' in ('patch','minor')
     and not exists(select 1 from ontology.nexloop_semantic_releases r where r.tenant_id=x.tenant_id and r.world=x.world and r.candidate_id=x.candidate_id)),'[]'::jsonb));
end $$;
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_observe_read(text,text,text,text,text) from public;
grant execute on function authz.nexloop_workbench_observe_read(text,text,text,text,text) to nexloop_api;
