-- I26 option A (owner 2026-10-30, relayed by the dispatcher): the world-model glossary (0374) takes part in recall. Temporary
-- number; the dispatcher renumbers. Published migrations are not edited. This changes 0374's "display metadata only" boundary for
-- recall alone: nothing in authorization, planning or the Context pack reads the glossary.
--
-- 1. ontology.nexloop_recall_entries.gloss_source (manual | machine): a glossary gloss is an alias row of the definition it names,
--    source_key 'glossary:<kind>/<identifier>', world real (simulation and test worlds never see it), gated by that definition's
--    own READ through the recall scope (an alias lives only while its canonical ref is in the latest definition).
--    Mapped: object_type -> eios:object_type:<identifier>; property -> eios:property:<type>/<identifier> for every type whose latest
--    version has that property (glossary property identifiers are bare names). Not mapped (no recall definition ref): section,
--    relation, event_type, function, action.
-- 2. ontology.nexloop_recall_glossary_reflow(tenant): rewrites every glossary alias row of the tenant from the current glossary and
--    the latest definitions. Statement triggers run it after every write to control.nexloop_world_glossary (manifest publish,
--    machine translation, owner correction, clearing, restore) and after a new type version, so no path can leave a stale alias.
-- 3. control.nexloop_recall_search (0067 body) reports a glossary hit's field as gloss_manual / gloss_machine.
-- 4. runtime.nexloop_semantic_accesses.gloss_hits {manual, machine}: how many returned definitions were reached through a gloss of
--    each source (authz.nexloop_semantic_access_record, 0375 body, stores it).
-- 5. A manifest publication and an owner correction each open a 7-day release watch (0250 runtime.nexloop_evo_watch_start):
--    release_ref glossary:<manifest_version> or glossary-correction:<kind>.<identifier>.<revision>; the existing regression alert
--    (evolution_release_regressing) covers it. Machine translations open none (they arrive continuously).

alter table ontology.nexloop_recall_entries add column gloss_source text check(gloss_source in ('manual','machine')),
 add constraint nexloop_recall_entries_gloss check(gloss_source is null or (field='alias' and index_kind='definition' and source_key like 'glossary:%'));

create function ontology.nexloop_recall_glossary_reflow(p_tenant text) returns integer
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare g record;r record;v integer;n integer:=0;id bigint;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 delete from ontology.nexloop_recall_entries where tenant_id=p_tenant and source_key like 'glossary:%';
 for g in select kind,identifier,gloss,source from control.nexloop_world_glossary
   where tenant_id=p_tenant and gloss<>'' and kind in ('object_type','property') order by kind,identifier loop
  for r in select l.type_name,l.v from (select type_name,max(version) v from ontology.object_type_versions where tenant_id=p_tenant
     and (g.kind='property' or type_name=g.identifier) group by type_name) l order by l.type_name loop
   for v in select 1 from ontology.nexloop_recall_definition_rows(p_tenant,r.type_name,r.v) d
     where d.ref=case when g.kind='object_type' then 'eios:object_type:'||g.identifier else 'eios:property:'||r.type_name||'/'||g.identifier end limit 1 loop
    id:=ontology.nexloop_recall_insert(p_tenant,'real','definition','glossary:'||g.kind||'/'||g.identifier,r.v,
     case when g.kind='object_type' then 'eios:object_type:'||g.identifier else 'eios:property:'||r.type_name||'/'||g.identifier end,
     g.kind,r.type_name,case when g.kind='property' then g.identifier end,null,'alias',null,null,g.gloss);
    update ontology.nexloop_recall_entries set gloss_source=g.source where entry_id=id;
    n:=n+1;
   end loop;
  end loop;
 end loop;
 return n;
end $$;
alter function ontology.nexloop_recall_glossary_reflow(text) owner to nexloop_owner;
revoke all on function ontology.nexloop_recall_glossary_reflow(text) from public;

create function ontology.nexloop_recall_glossary_changed() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text;
begin
 for t in select tenant_id from changed_rows union select tenant_id from changed_old loop
  perform ontology.nexloop_recall_glossary_reflow(t);
 end loop;
 return null;
end $$;
alter function ontology.nexloop_recall_glossary_changed() owner to nexloop_owner;
revoke all on function ontology.nexloop_recall_glossary_changed() from public;
create function ontology.nexloop_recall_glossary_inserted() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text;
begin
 for t in select distinct tenant_id from changed_rows loop perform ontology.nexloop_recall_glossary_reflow(t);end loop;
 return null;
end $$;
alter function ontology.nexloop_recall_glossary_inserted() owner to nexloop_owner;
revoke all on function ontology.nexloop_recall_glossary_inserted() from public;
create function ontology.nexloop_recall_glossary_deleted() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text;
begin
 for t in select distinct tenant_id from changed_old loop perform ontology.nexloop_recall_glossary_reflow(t);end loop;
 return null;
end $$;
alter function ontology.nexloop_recall_glossary_deleted() owner to nexloop_owner;
revoke all on function ontology.nexloop_recall_glossary_deleted() from public;
create trigger nexloop_recall_glossary_insert after insert on control.nexloop_world_glossary
 referencing new table as changed_rows for each statement execute function ontology.nexloop_recall_glossary_inserted();
create trigger nexloop_recall_glossary_update after update on control.nexloop_world_glossary
 referencing new table as changed_rows old table as changed_old for each statement execute function ontology.nexloop_recall_glossary_changed();
create trigger nexloop_recall_glossary_delete after delete on control.nexloop_world_glossary
 referencing old table as changed_old for each statement execute function ontology.nexloop_recall_glossary_deleted();
create trigger nexloop_recall_glossary_type_version after insert on ontology.object_type_versions
 referencing new table as changed_rows for each statement execute function ontology.nexloop_recall_glossary_inserted();

create function ontology.nexloop_glossary_watch_start() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if tg_table_name='nexloop_world_glossary_publications' then
  perform runtime.nexloop_evo_watch_start(new.tenant_id,'real','glossary:'||new.manifest_version,new.published_at);
 elsif new.cause='correction' then
  perform runtime.nexloop_evo_watch_start(new.tenant_id,'real',left('glossary-correction:'||new.kind||'.'||new.identifier||'.'||new.revision,160),new.recorded_at);
 end if;
 return null;
end $$;
alter function ontology.nexloop_glossary_watch_start() owner to nexloop_owner;
revoke all on function ontology.nexloop_glossary_watch_start() from public;
create trigger nexloop_glossary_publication_watch after insert on control.nexloop_world_glossary_publications
 for each row execute function ontology.nexloop_glossary_watch_start();
create trigger nexloop_glossary_correction_watch after insert on control.nexloop_world_glossary_revisions
 for each row execute function ontology.nexloop_glossary_watch_start();

-- First sync of the glossary already published (the triggers cover every later write).
do $sync$
declare t text;
begin
 for t in select distinct tenant_id from control.nexloop_world_glossary loop perform ontology.nexloop_recall_glossary_reflow(t);end loop;
end $sync$;

create or replace function control.nexloop_recall_search(p_digest text,p_world text,p_request jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=ontology.nexloop_recall_tenant(p_digest,p_world);q jsonb:=p_request;kind text;method text;lim integer;
 types text[];props text[];scope text[];qv extensions.vector;qtsv tsvector;qlex text[];qgrams text[];tsq tsquery;lex text;res jsonb;
begin
 kind:=q->>'index_kind';method:=q->>'method';lim:=(q->>'limit')::integer;
 if kind not in ('definition','instance') or method not in ('vector','fts','trgm','strong_id') or lim is null or lim not between 1 and 200
  or jsonb_typeof(q->'readable_types') is distinct from 'array' or jsonb_typeof(q->'readable_properties') is distinct from 'array' then
  raise exception 'recall request invalid' using errcode='22023';end if;
 select coalesce(array_agg(value),'{}') into types from jsonb_array_elements_text(q->'readable_types');
 select coalesce(array_agg(value),'{}') into props from jsonb_array_elements_text(q->'readable_properties');
 if jsonb_typeof(q->'type_names')='array' then select coalesce(array_agg(value),'{}') into scope from jsonb_array_elements_text(q->'type_names');end if;
 if method='strong_id' then
  if kind<>'instance' or jsonb_typeof(q->'strong_ids') is distinct from 'array' then raise exception 'recall strong id request invalid' using errcode='22023';end if;
  select coalesce(jsonb_agg(row_to_json(x)::jsonb),'[]'::jsonb) into res from (
   select e.ref,case when e.gloss_source is null then e.field else 'gloss_'||e.gloss_source end field,e.gate_type,e.gate_property,e.object_id,e.tenant_id,e.world,e.source_revision,1.0::double precision score
   from ontology.nexloop_recall_entries e join ontology.nexloop_recall_scope(t,p_world,kind,types,props,scope) s(entry_id) using(entry_id)
   where e.field='strong_id' and exists(select 1 from jsonb_array_elements(q->'strong_ids') k
     where k->>'key'=e.strong_key and lower(btrim(k->>'value'))=e.body and (k->>'type_name' is null or k->>'type_name'=e.gate_type))
    or (jsonb_typeof(q->'verified_refs')='array' and q->'verified_refs' ? e.ref)
   order by e.ref limit lim) x;
 elsif method='vector' then
  perform ontology.nexloop_recall_active_profile(t,q->>'profile_id',(q->>'dimension')::integer);
  qv:=(q->>'vector')::extensions.vector;
  if extensions.vector_dims(qv)<>(q->>'dimension')::integer then raise exception 'embedding profile or dimension mismatch' using errcode='22023';end if;
  select coalesce(jsonb_agg(row_to_json(x)::jsonb),'[]'::jsonb) into res from (
   select e.ref,case when e.gloss_source is null then e.field else 'gloss_'||e.gloss_source end field,e.gate_type,e.gate_property,e.object_id,e.tenant_id,e.world,e.source_revision,
    greatest(0,least(1,1-(m.embedding operator(extensions.<=>) qv)))::double precision score
   from ontology.nexloop_recall_entries e join ontology.nexloop_recall_scope(t,p_world,kind,types,props,scope) s(entry_id) using(entry_id)
   join ontology.nexloop_recall_embeddings m on m.entry_id=e.entry_id and m.profile_id=q->>'profile_id' and m.tenant_id=t
   order by m.embedding operator(extensions.<=>) qv,e.entry_id limit lim) x;
 elsif method='fts' then
  qtsv:=ontology.nexloop_recall_tsv(coalesce(q->>'text',''));
  qlex:=tsvector_to_array(qtsv);
  if cardinality(qlex)=0 then return '[]'::jsonb;end if;
  foreach lex in array qlex loop
   tsq:=case when tsq is null then plainto_tsquery('simple'::regconfig,lex) else tsq||plainto_tsquery('simple'::regconfig,lex) end;
  end loop;
  select coalesce(jsonb_agg(row_to_json(x)::jsonb),'[]'::jsonb) into res from (
   select e.ref,case when e.gloss_source is null then e.field else 'gloss_'||e.gloss_source end field,e.gate_type,e.gate_property,e.object_id,e.tenant_id,e.world,e.source_revision,
    ((select count(*) from unnest(qlex) l where l=any(tsvector_to_array(e.tsv)))::double precision/cardinality(qlex)) score
   from ontology.nexloop_recall_entries e join ontology.nexloop_recall_scope(t,p_world,kind,types,props,scope) s(entry_id) using(entry_id)
   where e.tsv @@ tsq and e.field<>'strong_id'
   order by score desc,e.entry_id limit lim) x;
 else
  qgrams:=ontology.nexloop_recall_ngrams(coalesce(q->>'text',''));
  if cardinality(qgrams)=0 then return '[]'::jsonb;end if;
  -- Fuzzy matching is for names/aliases/values only, not unbounded descriptions.
  select coalesce(jsonb_agg(row_to_json(x)::jsonb),'[]'::jsonb) into res from (
   select e.ref,case when e.gloss_source is null then e.field else 'gloss_'||e.gloss_source end field,e.gate_type,e.gate_property,e.object_id,e.tenant_id,e.world,e.source_revision,
    ontology.nexloop_recall_jaccard(e.ngrams,qgrams) score
   from ontology.nexloop_recall_entries e join ontology.nexloop_recall_scope(t,p_world,kind,types,props,scope) s(entry_id) using(entry_id)
   where e.ngrams && qgrams and e.field in ('name','display_name','alias','vocabulary_value','title')
   order by score desc,e.entry_id limit lim) x;
 end if;
 return res;
end $$;


alter table runtime.nexloop_semantic_accesses add column gloss_hits jsonb not null default '{}'::jsonb
 check(jsonb_typeof(gloss_hits)='object' and gloss_hits - array['manual','machine'] = '{}'::jsonb);

create or replace function authz.nexloop_semantic_access_record(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;ident jsonb;v_tenant text;v_run uuid;v_id uuid;v_access runtime.nexloop_semantic_accesses;
 r jsonb;v_refs jsonb:='[]'::jsonb;v_terms text[];
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker') or octet_length(p_text)>65536 or octet_length(p_payload)>65536
  or a->>'protocol' is distinct from 'nexloop-semantic-access-v1' or c->>'kind' not in ('access','use') or c->>'kind' is null
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'semantic access record unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-semantic-access-v1:'||p_text,'UTF8'),k,'sha256'),'hex') then
  raise exception 'semantic access record unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);v_tenant:=ident->'binding'->>'tenant_id';
 if v_tenant is null or a->>'tenant_id' is distinct from v_tenant or a->>'world' is distinct from p_world then
  raise exception 'semantic access record unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 if coalesce(c->>'access_id','')!~'^[0-9a-f-]{36}$' or (c ? 'run_id' and jsonb_typeof(c->'run_id')<>'null' and coalesce(c->>'run_id','')!~'^[0-9a-f-]{36}$') then
  raise exception 'semantic access record invalid' using errcode='22023';end if;
 v_id:=(c->>'access_id')::uuid;v_run:=nullif(c->>'run_id','')::uuid;
 if v_run is not null and not exists(select 1 from authz.nexloop_runtime_run_bindings b where b.tenant_id=v_tenant and b.world=p_world and b.run_id=v_run)
  and not exists(select 1 from authz.nexloop_run_credentials rc where rc.run_id=v_run and rc.world=p_world and rc.source_digest=p_digest and rc.status='active') then
  raise exception 'semantic access run unknown' using errcode='42501';end if;
 if jsonb_typeof(c->'refs') is distinct from 'array' or jsonb_array_length(c->'refs')>320 then raise exception 'semantic access record invalid' using errcode='22023';end if;
 for r in select value from jsonb_array_elements(c->'refs') loop
  if jsonb_typeof(r)<>'object' or (select array_agg(key order by key) from jsonb_object_keys(r) key) is distinct from array['ref','revision']
   or coalesce(r->>'ref','')!~'^(eios:object_type:|eios:property:|nexloop:vocabulary:)[^[:cntrl:]]+$' or char_length(r->>'ref')>512 or coalesce(r->>'revision','')!~'^[A-Za-z0-9._:@-]{1,128}$' then
   raise exception 'semantic access record invalid' using errcode='22023';end if;
  v_refs:=v_refs||jsonb_build_array(r);
 end loop;
 if c->>'kind'='use' then
  if coalesce(c->>'accessed_id','')!~'^[0-9a-f-]{36}$' then raise exception 'semantic access record invalid' using errcode='22023';end if;
  select * into v_access from runtime.nexloop_semantic_accesses where tenant_id=v_tenant and world=p_world and access_id=(c->>'accessed_id')::uuid and kind='access';
  -- A use names an access of the same Run, and only refs that access returned.
  if not found or v_access.run_id is distinct from v_run
   or exists(select 1 from jsonb_array_elements(v_refs) u where not v_access.result_refs @> jsonb_build_array(u.value)) then
   raise exception 'semantic access use invalid' using errcode='42501';end if;
  insert into runtime.nexloop_semantic_accesses(tenant_id,world,access_id,kind,run_id,context_id,accessed_id,result_refs,result_count)
   values(v_tenant,p_world,v_id,'use',v_run,v_access.context_id,v_access.access_id,v_refs,jsonb_array_length(v_refs)) on conflict do nothing;
 else
  if jsonb_typeof(c->'term_hashes') is distinct from 'array' then raise exception 'semantic access record invalid' using errcode='22023';end if;
  v_terms:=array(select value#>>'{}' from jsonb_array_elements(c->'term_hashes'));
  insert into runtime.nexloop_semantic_accesses(tenant_id,world,access_id,kind,run_id,context_id,operation,query_kind,term_hashes,coverage,ambiguity,
    result_refs,result_count,unresolved_count,miss_reason,gloss_hits)
   values(v_tenant,p_world,v_id,'access',v_run,nullif(c->>'context_id',''),c->>'operation',c->>'query_kind',v_terms,(c->>'coverage')::numeric,
    (c->>'ambiguity')::boolean,v_refs,jsonb_array_length(v_refs),coalesce((c->>'unresolved_count')::integer,0),
    case when jsonb_array_length(v_refs)=0 then nullif(c->>'miss_reason','') end,
    case when jsonb_typeof(c->'gloss_hits')='object' then c->'gloss_hits' else '{}'::jsonb end) on conflict do nothing;
 end if;
 return jsonb_build_object('access_id',v_id,'kind',c->>'kind');
end $$;

