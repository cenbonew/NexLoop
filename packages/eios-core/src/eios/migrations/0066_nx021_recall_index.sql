-- NX-021 / ADR-019 §3.2, §3.6: hybrid recall index (vector + FTS + n-gram) over
-- published Schema definitions, aliases and object instances.
-- Derived, rebuildable layer: every indexed text is read here from the stored
-- definition/object row, never accepted from the caller; aliases must point at
-- a live definition ref. Tenant comes from the authenticated credential; world
-- and property gates are applied before scoring, and stale rows are dropped by
-- source revision at query time. No business fact is written here.
create extension pg_trgm with schema extensions;
create extension vector with schema extensions;
-- Same as 0005: only the owner-run definers may call the extension functions used here.
grant execute on function extensions.show_trgm(text),extensions.vector_dims(extensions.vector),extensions.vector_norm(extensions.vector),
 extensions.cosine_distance(extensions.vector,extensions.vector) to nexloop_owner;

create table ontology.nexloop_embedding_profiles(
  tenant_id text not null,
  profile_id text not null,
  model text not null check (model ~ '^[!-~]{1,160}$'),
  dimension integer not null check (dimension between 1 and 16000),
  created_at timestamptz not null default clock_timestamp(),
  primary key(tenant_id,profile_id),
  check (profile_id=model||'@'||dimension)
);

create table ontology.nexloop_recall_settings(
  tenant_id text primary key,
  active_profile_id text not null,
  foreign key(tenant_id,active_profile_id) references ontology.nexloop_embedding_profiles(tenant_id,profile_id),
  revision bigint not null default 1 check (revision>0),
  updated_at timestamptz not null default clock_timestamp()
);

create table ontology.nexloop_recall_entries(
  entry_id bigint generated always as identity primary key,
  tenant_id text not null,
  world text,
  index_kind text not null check (index_kind in ('definition','instance')),
  source_key text not null check (char_length(source_key) between 1 and 600),
  source_revision bigint not null check (source_revision>0),
  ref text not null check (char_length(ref)<=512 and ref ~ '^[A-Za-z][A-Za-z0-9_.-]*:[^\s]+$'),
  target_kind text not null check (target_kind in ('object_type','property','vocabulary_value','object_instance')),
  gate_type text not null check (gate_type ~ '^[A-Za-z][A-Za-z0-9_]*$'),
  gate_property text check (gate_property ~ '^[A-Za-z][A-Za-z0-9_]*$'),
  object_id text,
  field text not null check (field in ('name','display_name','description','alias','vocabulary_value','title','strong_id')),
  strong_key text,
  property_group text,
  body text not null check (char_length(body) between 1 and 4000),
  tokenizer_version text not null,
  tsv tsvector not null,
  ngrams text[] not null,
  created_at timestamptz not null default clock_timestamp(),
  check ((index_kind='instance')=(object_id is not null)),
  check (index_kind='definition' or world is not null),
  check (index_kind<>'definition' or (field='alias')=(world is not null)),
  check ((field='strong_id')=(strong_key is not null))
);
create index nexloop_recall_entries_scope on ontology.nexloop_recall_entries(tenant_id,index_kind,world,gate_type);
create index nexloop_recall_entries_source on ontology.nexloop_recall_entries(tenant_id,source_key);
create index nexloop_recall_entries_strong on ontology.nexloop_recall_entries(tenant_id,world,strong_key,body) where field='strong_id';
create index nexloop_recall_entries_tsv on ontology.nexloop_recall_entries using gin(tsv);
create index nexloop_recall_entries_ngrams on ontology.nexloop_recall_entries using gin(ngrams);

create table ontology.nexloop_recall_embeddings(
  entry_id bigint not null references ontology.nexloop_recall_entries(entry_id) on delete cascade,
  tenant_id text not null,
  profile_id text not null,
  embedding extensions.vector not null,
  primary key(entry_id,profile_id),
  foreign key(tenant_id,profile_id) references ontology.nexloop_embedding_profiles(tenant_id,profile_id)
);
create index nexloop_recall_embeddings_profile on ontology.nexloop_recall_embeddings(tenant_id,profile_id);

-- Index dimension is fixed per profile; a vector of any other length is refused.
create function ontology.nexloop_recall_embedding_guard() returns trigger
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare d integer;
begin
 select dimension into d from ontology.nexloop_embedding_profiles where tenant_id=new.tenant_id and profile_id=new.profile_id;
 if d is null or extensions.vector_dims(new.embedding)<>d then
  raise exception 'embedding dimension mismatch' using errcode='22023';end if;
 if extensions.vector_norm(new.embedding)=0 then raise exception 'zero embedding refused' using errcode='22023';end if;
 if new.tenant_id is distinct from (select tenant_id from ontology.nexloop_recall_entries where entry_id=new.entry_id) then
  raise exception 'embedding tenant mismatch' using errcode='42501';end if;
 return new;
end $$;
alter function ontology.nexloop_recall_embedding_guard() owner to nexloop_owner;
revoke all on function ontology.nexloop_recall_embedding_guard() from public;
create trigger nexloop_recall_embedding_guard before insert or update on ontology.nexloop_recall_embeddings
 for each row execute function ontology.nexloop_recall_embedding_guard();

do $isolation$
declare t text;
begin
 foreach t in array array['nexloop_embedding_profiles','nexloop_recall_settings','nexloop_recall_entries','nexloop_recall_embeddings'] loop
  execute format('alter table ontology.%I owner to nexloop_owner',t);
  execute format('alter table ontology.%I enable row level security',t);
  execute format('alter table ontology.%I force row level security',t);
  execute format('create policy tenant_boundary on ontology.%I to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on ontology.%I from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime',t);
 end loop;
end $isolation$;

-- Tokenizer nexloop-cjk-bigram-v1. CJK runs become overlapping character
-- bigrams (single characters stay unigrams); other text keeps the simple parser.
create function ontology.nexloop_recall_cjk_bigrams(p text) returns text[]
 language plpgsql immutable strict parallel safe set search_path=pg_catalog as $$
declare run text;grams text[]:='{}';i integer;
begin
 for run in select m[1] from regexp_matches(lower(p),'([㐀-䶿一-鿿豈-﫿]+)','g') as m loop
  if char_length(run)=1 then grams:=grams||run;
  else for i in 1..char_length(run)-1 loop grams:=grams||substr(run,i,2);end loop;end if;
 end loop;
 return grams;
end $$;
create function ontology.nexloop_recall_document(p text) returns text
 language sql immutable strict parallel safe set search_path=pg_catalog as $$
 select regexp_replace(lower(p),'[㐀-䶿一-鿿豈-﫿]+',' ','g')||' '||array_to_string(ontology.nexloop_recall_cjk_bigrams(p),' ')
$$;
create function ontology.nexloop_recall_tsv(p text) returns tsvector
 language sql immutable strict parallel safe set search_path=pg_catalog as $$
 select to_tsvector('simple'::regconfig,ontology.nexloop_recall_document(p))
$$;
-- pg_trgm trigrams for non-CJK words plus CJK character bigrams. pg_trgm emits
-- no trigrams for CJK under the C locale, so both share one Jaccard measure.
create function ontology.nexloop_recall_ngrams(p text) returns text[]
 language sql immutable strict parallel safe set search_path=pg_catalog as $$
 select coalesce(array(select distinct g from unnest(
  extensions.show_trgm(regexp_replace(lower(p),'[㐀-䶿一-鿿豈-﫿]+',' ','g'))
  ||ontology.nexloop_recall_cjk_bigrams(p)) g order by g),'{}')
$$;
create function ontology.nexloop_recall_jaccard(a text[],b text[]) returns double precision
 language sql immutable strict parallel safe set search_path=pg_catalog as $$
 select case when cardinality(a)=0 or cardinality(b)=0 then 0::double precision else
  (select count(*) from (select unnest(a) intersect select unnest(b)) i)::double precision/
  (select count(*) from (select unnest(a) union select unnest(b)) u)::double precision end
$$;
create function ontology.nexloop_recall_ref_part(p text) returns text
 language sql immutable strict parallel safe set search_path=pg_catalog as $$
 select replace(replace(replace(replace(replace(replace(replace(p,'%','%25'),' ','%20'),chr(9),'%09'),chr(10),'%0A'),chr(13),'%0D'),chr(12),'%0C'),chr(11),'%0B')
$$;
do $owner$
declare f text;
begin
 foreach f in array array['nexloop_recall_cjk_bigrams(text)','nexloop_recall_document(text)','nexloop_recall_tsv(text)',
  'nexloop_recall_ngrams(text)','nexloop_recall_jaccard(text[],text[])','nexloop_recall_ref_part(text)'] loop
  execute 'alter function ontology.'||f||' owner to nexloop_owner';
  execute 'revoke all on function ontology.'||f||' from public';
 end loop;
end $owner$;

-- Tenant is derived from the authenticated credential for this world only.
create function ontology.nexloop_recall_tenant(p_digest text,p_world text) returns text
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare v jsonb;t text;
begin
 if p_world is null or p_world !~ '^[A-Za-z][A-Za-z0-9_.-]{0,63}$' then raise exception 'recall world invalid' using errcode='22023';end if;
 v:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 t:=v->'binding'->>'tenant_id';
 if t is null or t='' then raise exception 'recall authentication denied' using errcode='42501';end if;
 perform 1 from control.nexloop_tenants where tenant_id=t and status='active';
 if not found then raise exception 'recall tenant inactive' using errcode='42501';end if;
 perform set_config('eios.tenant_id',t,true);
 return t;
end $$;

-- Definition rows derived only from the stored ObjectTypeDefinition version.
create function ontology.nexloop_recall_definition_rows(p_tenant text,p_type text,p_version integer)
 returns table(ref text,target_kind text,gate_type text,gate_property text,field text,property_group text,body text)
 language plpgsql stable security definer set search_path=pg_catalog set row_security=on as $$
declare d jsonb;p jsonb;g text;v text;tref text;pref text;
begin
 select definition into d from ontology.object_type_versions where tenant_id=p_tenant and type_name=p_type and version=p_version;
 if not found then raise exception 'recall definition unavailable' using errcode='42501';end if;
 tref:='eios:object_type:'||p_type;
 return query select tref,'object_type',p_type,null::text,x.f,null::text,x.b from (values
  ('name',p_type),('display_name',nullif(btrim(d->>'display_name'),'')),('display_name',nullif(btrim(d->>'plural_display_name'),'')),
  ('description',nullif(btrim(d->>'description'),''))) x(f,b) where x.b is not null;
 for p in select value from jsonb_array_elements(coalesce(d->'properties','[]'::jsonb)) loop
  pref:='eios:property:'||p_type||'/'||(p->>'property_name');
  select pg.value->>'group_name' into g from jsonb_array_elements(coalesce(d->'property_groups','[]'::jsonb)) pg
   where pg.value->'property_names' ? (p->>'property_name') order by pg.value->>'group_name' limit 1;
  return query select pref,'property',p_type,p->>'property_name',x.f,g,x.b from (values
   ('name',p->>'property_name'),('display_name',nullif(btrim(p->>'display_name'),'')),('description',nullif(btrim(p->>'description'),''))) x(f,b)
   where x.b is not null;
  for v in select coalesce(e.value#>>'{}','') from jsonb_array_elements(coalesce(p->'type_descriptor'->'enum','[]'::jsonb)) e loop
   if btrim(v)<>'' then
    return query select 'nexloop:vocabulary:'||p_type||'/'||(p->>'property_name')||'/'||ontology.nexloop_recall_ref_part(v),
     'vocabulary_value',p_type,p->>'property_name','vocabulary_value',g,v;
   end if;
  end loop;
 end loop;
end $$;

create function ontology.nexloop_recall_latest_version(p_tenant text,p_type text) returns integer
 language sql stable security definer set search_path=pg_catalog set row_security=on as $$
 select max(version) from ontology.object_type_versions where tenant_id=p_tenant and type_name=p_type
$$;

-- Instance rows derived only from the stored object row and its type definition.
create function ontology.nexloop_recall_instance_rows(p_tenant text,p_world text,p_type text,p_object text,p_spec jsonb)
 returns table(revision bigint,ref text,field text,strong_key text,gate_property text,body text)
 language plpgsql stable security definer set search_path=pg_catalog set row_security=on as $$
declare d jsonb;o ontology.objects%rowtype;declared text[];skey text;fkind text;fname text;el jsonb;val jsonb;item jsonb;
 spec jsonb:=coalesce(p_spec,'{}'::jsonb);
begin
 if jsonb_typeof(spec) is distinct from 'object' or exists(select 1 from jsonb_object_keys(spec) k where k not in ('name_fields','alias_fields','strong_id_fields')) then
  raise exception 'recall instance spec invalid' using errcode='22023';end if;
 select definition into d from ontology.object_type_versions where tenant_id=p_tenant and type_name=p_type
  and version=ontology.nexloop_recall_latest_version(p_tenant,p_type);
 if not found then raise exception 'recall definition unavailable' using errcode='42501';end if;
 select * into o from ontology.objects where tenant_id=p_tenant and world=p_world and type_name=p_type and object_id=p_object;
 if not found then raise exception 'recall object unavailable' using errcode='42501';end if;
 select coalesce(array_agg(x.value->>'property_name'),'{}') into declared from jsonb_array_elements(coalesce(d->'properties','[]'::jsonb)) x;
 -- Defaults: title_property is the name, the declared natural key is the strong identifier.
 if not spec ? 'name_fields' then spec:=spec||jsonb_build_object('name_fields',case when coalesce(d->>'title_property','')='' then '[]'::jsonb else jsonb_build_array(d->>'title_property') end);end if;
 if not spec ? 'alias_fields' then spec:=spec||jsonb_build_object('alias_fields','[]'::jsonb);end if;
 if not spec ? 'strong_id_fields' then spec:=spec||jsonb_build_object('strong_id_fields',coalesce(d->'primary_key','[]'::jsonb));end if;
 for skey,fkind in select s.k,s.f from (values ('name_fields','title'),('alias_fields','alias'),('strong_id_fields','strong_id')) s(k,f) loop
  if jsonb_typeof(spec->skey) is distinct from 'array' or jsonb_array_length(spec->skey)>16 then raise exception 'recall instance spec invalid' using errcode='22023';end if;
  for el in select value from jsonb_array_elements(spec->skey) loop
   fname:=el#>>'{}';
   if jsonb_typeof(el) is distinct from 'string' or not fname=any(declared) then raise exception 'recall instance field undeclared' using errcode='22023';end if;
   val:=o.properties->fname;
   continue when val is null or jsonb_typeof(val)='null';
   for item in select case when jsonb_typeof(val)='array' and fkind='alias' then a.value else val end
    from jsonb_array_elements(case when jsonb_typeof(val)='array' and fkind='alias' then val else '[null]'::jsonb end) a loop
    if jsonb_typeof(item) not in ('string','number') then
     continue when fkind='alias';
     raise exception 'recall instance field not scalar' using errcode='22023';end if;
    continue when btrim(item#>>'{}')='' or char_length(btrim(item#>>'{}'))>4000;
    return query select o.nexloop_revision,'eios:object:'||p_type||'/'||p_object,fkind,
     case when fkind='strong_id' then fname end,fname,
     case when fkind='strong_id' then lower(btrim(item#>>'{}')) else btrim(item#>>'{}') end;
   end loop;
  end loop;
 end loop;
end $$;

-- Gates on live entries. Names only; the trusted backend decides each through EIOS.
create function control.nexloop_recall_gates(p_digest text,p_world text,p_index_kind text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare t text:=ontology.nexloop_recall_tenant(p_digest,p_world);
begin
 if p_index_kind not in ('definition','instance') then raise exception 'recall index invalid' using errcode='22023';end if;
 return jsonb_build_object(
  'types',coalesce((select jsonb_agg(distinct e.gate_type) from ontology.nexloop_recall_entries e
   where e.tenant_id=t and e.index_kind=p_index_kind and (e.world is null or e.world=p_world)),'[]'::jsonb),
  'properties',coalesce((select jsonb_agg(distinct jsonb_build_array(e.gate_type,e.gate_property)) from ontology.nexloop_recall_entries e
   where e.tenant_id=t and e.index_kind=p_index_kind and (e.world is null or e.world=p_world) and e.gate_property is not null),'[]'::jsonb));
end $$;

create function control.nexloop_recall_activate_profile(p_digest text,p_world text,p_model text,p_dimension integer,p_expected text)
 returns text language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare t text:=ontology.nexloop_recall_tenant(p_digest,p_world);pid text;cur text;
begin
 if p_model is null or p_model !~ '^[!-~]{1,160}$' or p_dimension is null or p_dimension not between 1 and 16000 then
  raise exception 'embedding profile invalid' using errcode='22023';end if;
 pid:=p_model||'@'||p_dimension;
 insert into ontology.nexloop_embedding_profiles(tenant_id,profile_id,model,dimension) values(t,pid,p_model,p_dimension) on conflict do nothing;
 select active_profile_id into cur from ontology.nexloop_recall_settings where tenant_id=t for update;
 if cur is not distinct from pid then return pid;end if;
 -- Never silently switch: the caller must name the profile it is replacing.
 if cur is distinct from p_expected then raise exception 'embedding profile change requires explicit replacement' using errcode='40001';end if;
 if cur is null then insert into ontology.nexloop_recall_settings(tenant_id,active_profile_id) values(t,pid);
 else update ontology.nexloop_recall_settings set active_profile_id=pid,revision=revision+1,updated_at=clock_timestamp() where tenant_id=t;end if;
 return pid;
end $$;

create function ontology.nexloop_recall_active_profile(p_tenant text,p_profile text,p_dimension integer) returns void
 language plpgsql stable security definer set search_path=pg_catalog set row_security=on as $$
declare a text;d integer;
begin
 select s.active_profile_id,p.dimension into a,d from ontology.nexloop_recall_settings s
  join ontology.nexloop_embedding_profiles p on p.tenant_id=s.tenant_id and p.profile_id=s.active_profile_id where s.tenant_id=p_tenant;
 if a is null or a is distinct from p_profile or d is distinct from p_dimension then
  raise exception 'embedding profile or dimension mismatch' using errcode='22023';end if;
end $$;

-- Attach caller-computed vectors to SQL-derived rows; every non-strong row
-- needs exactly one vector when a profile is named, none otherwise.
create function ontology.nexloop_recall_attach(p_tenant text,p_ids bigint[],p_profile text,p_dimension integer,p_vectors jsonb) returns void
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare need integer;given integer;
begin
 if jsonb_typeof(coalesce(p_vectors,'[]'::jsonb)) is distinct from 'array' then raise exception 'recall vectors invalid' using errcode='22023';end if;
 if p_profile is null then
  if jsonb_array_length(coalesce(p_vectors,'[]'::jsonb))>0 then raise exception 'vectors require an embedding profile' using errcode='22023';end if;
  return;
 end if;
 perform ontology.nexloop_recall_active_profile(p_tenant,p_profile,p_dimension);
 select count(*) into need from ontology.nexloop_recall_entries where entry_id=any(p_ids) and field<>'strong_id';
 select count(distinct (v->>'ref',v->>'field',v->>'body')) into given from jsonb_array_elements(p_vectors) v;
 if given<>jsonb_array_length(p_vectors) then raise exception 'recall vectors duplicated' using errcode='22023';end if;
 insert into ontology.nexloop_recall_embeddings(entry_id,tenant_id,profile_id,embedding)
  select e.entry_id,p_tenant,p_profile,(v->>'vector')::extensions.vector
  from ontology.nexloop_recall_entries e join jsonb_array_elements(p_vectors) v
   on v->>'ref'=e.ref and v->>'field'=e.field and v->>'body'=e.body
  where e.entry_id=any(p_ids) and e.field<>'strong_id';
 get diagnostics given=row_count;
 if given<>need or need<>jsonb_array_length(p_vectors) then raise exception 'recall vectors do not cover indexed rows' using errcode='22023';end if;
end $$;

create function ontology.nexloop_recall_insert(p_tenant text,p_world text,p_kind text,p_source text,p_revision bigint,p_ref text,p_target text,
  p_gate_type text,p_gate_property text,p_object text,p_field text,p_strong text,p_group text,p_body text) returns bigint
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare id bigint;
begin
 insert into ontology.nexloop_recall_entries(tenant_id,world,index_kind,source_key,source_revision,ref,target_kind,gate_type,gate_property,object_id,
   field,strong_key,property_group,body,tokenizer_version,tsv,ngrams)
  values(p_tenant,p_world,p_kind,p_source,p_revision,p_ref,p_target,p_gate_type,p_gate_property,p_object,p_field,p_strong,p_group,p_body,
   'nexloop-cjk-bigram-v1',ontology.nexloop_recall_tsv(p_body),ontology.nexloop_recall_ngrams(p_body))
  returning entry_id into id;
 return id;
end $$;

create function control.nexloop_recall_definition_texts(p_digest text,p_world text,p_type text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare t text:=ontology.nexloop_recall_tenant(p_digest,p_world);v integer;
begin
 v:=ontology.nexloop_recall_latest_version(t,p_type);
 if v is null then raise exception 'recall definition unavailable' using errcode='42501';end if;
 return jsonb_build_object('type_name',p_type,'version',v,'rows',coalesce((select jsonb_agg(jsonb_build_object('ref',r.ref,'field',r.field,'body',r.body) order by r.ref,r.field,r.body)
  from (select distinct x.ref,x.field,x.body from ontology.nexloop_recall_definition_rows(t,p_type,v) x) r),'[]'::jsonb));
end $$;

create function control.nexloop_recall_index_definition(p_digest text,p_world text,p_type text,p_version integer,p_profile text,p_dimension integer,p_vectors jsonb)
 returns integer language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare t text:=ontology.nexloop_recall_tenant(p_digest,p_world);ids bigint[]:='{}';r record;
begin
 perform 1 from ontology.object_type_versions where tenant_id=t and type_name=p_type and version=p_version for share;
 if not found or p_version<>ontology.nexloop_recall_latest_version(t,p_type) then
  raise exception 'recall definition version stale' using errcode='40001';end if;
 delete from ontology.nexloop_recall_entries where tenant_id=t and source_key='type:'||p_type;
 for r in select distinct * from ontology.nexloop_recall_definition_rows(t,p_type,p_version) loop
  ids:=ids||ontology.nexloop_recall_insert(t,null,'definition','type:'||p_type,p_version,r.ref,r.target_kind,r.gate_type,r.gate_property,null,r.field,null,r.property_group,r.body);
 end loop;
 perform ontology.nexloop_recall_attach(t,ids,p_profile,p_dimension,p_vectors);
 return cardinality(ids);
end $$;

-- Reflow of an approved alias (NX-045/046). The canonical ref must be a live
-- definition element; the alias inherits its gates.
create function control.nexloop_recall_index_alias(p_digest text,p_world text,p_alias_id text,p_canonical text,p_alias text,p_profile text,p_dimension integer,p_vectors jsonb)
 returns integer language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare t text:=ontology.nexloop_recall_tenant(p_digest,p_world);ty text;r record;id bigint;
begin
 if p_alias_id is null or p_alias_id !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$' or p_alias is null or char_length(btrim(p_alias)) not between 1 and 200 then
  raise exception 'recall alias invalid' using errcode='22023';end if;
 ty:=substring(p_canonical from '^(?:eios:object_type:|eios:property:|nexloop:vocabulary:)([A-Za-z][A-Za-z0-9_]*)');
 if ty is null then raise exception 'recall alias target invalid' using errcode='22023';end if;
 select * into r from ontology.nexloop_recall_definition_rows(t,ty,ontology.nexloop_recall_latest_version(t,ty)) d where d.ref=p_canonical limit 1;
 if not found then raise exception 'recall alias target unavailable' using errcode='42501';end if;
 delete from ontology.nexloop_recall_entries where tenant_id=t and source_key='alias:'||p_alias_id;
 id:=ontology.nexloop_recall_insert(t,p_world,'definition','alias:'||p_alias_id,1,r.ref,r.target_kind,r.gate_type,r.gate_property,null,'alias',null,r.property_group,btrim(p_alias));
 perform ontology.nexloop_recall_attach(t,array[id],p_profile,p_dimension,p_vectors);
 return 1;
end $$;

create function control.nexloop_recall_instance_texts(p_digest text,p_world text,p_type text,p_object text,p_spec jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare t text:=ontology.nexloop_recall_tenant(p_digest,p_world);rev bigint;
begin
 select revision into rev from ontology.nexloop_recall_instance_rows(t,p_world,p_type,p_object,p_spec) limit 1;
 if rev is null then select nexloop_revision into rev from ontology.objects where tenant_id=t and world=p_world and type_name=p_type and object_id=p_object;end if;
 return jsonb_build_object('revision',rev,'rows',coalesce((select jsonb_agg(jsonb_build_object('ref',r.ref,'field',r.field,'body',r.body) order by r.field,r.body)
  from (select distinct x.ref,x.field,x.body from ontology.nexloop_recall_instance_rows(t,p_world,p_type,p_object,p_spec) x where x.field<>'strong_id') r),'[]'::jsonb));
end $$;

create function control.nexloop_recall_index_instance(p_digest text,p_world text,p_type text,p_object text,p_spec jsonb,p_revision bigint,p_profile text,p_dimension integer,p_vectors jsonb)
 returns integer language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare t text:=ontology.nexloop_recall_tenant(p_digest,p_world);ids bigint[]:='{}';r record;src text;
begin
 perform 1 from ontology.objects where tenant_id=t and world=p_world and type_name=p_type and object_id=p_object and nexloop_revision=p_revision for share;
 if not found then raise exception 'recall object revision stale' using errcode='40001';end if;
 src:='object:'||p_world||'/'||p_type||'/'||p_object;
 delete from ontology.nexloop_recall_entries where tenant_id=t and source_key=src;
 for r in select distinct * from ontology.nexloop_recall_instance_rows(t,p_world,p_type,p_object,p_spec) loop
  ids:=ids||ontology.nexloop_recall_insert(t,p_world,'instance',src,p_revision,r.ref,'object_instance',p_type,r.gate_property,p_object,r.field,r.strong_key,null,r.body);
 end loop;
 perform ontology.nexloop_recall_attach(t,ids,p_profile,p_dimension,p_vectors);
 return cardinality(ids);
end $$;

create function control.nexloop_recall_remove_source(p_digest text,p_world text,p_source text) returns integer
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare t text:=ontology.nexloop_recall_tenant(p_digest,p_world);n integer;
begin
 delete from ontology.nexloop_recall_entries where tenant_id=t and source_key=p_source
  and (world is null or world=p_world);
 get diagnostics n=row_count;
 return n;
end $$;

-- Pre-filter: tenant, world, readable gates, current tokenizer and live source revision.
create function ontology.nexloop_recall_scope(p_tenant text,p_world text,p_kind text,p_types text[],p_props text[],p_scope text[])
 returns setof bigint language sql stable security definer set search_path=pg_catalog set row_security=on as $$
 select e.entry_id from ontology.nexloop_recall_entries e
 where e.tenant_id=p_tenant and e.index_kind=p_kind and e.tokenizer_version='nexloop-cjk-bigram-v1'
  and (case when p_kind='instance' then e.world=p_world else (e.world is null or e.world=p_world) end)
  and e.gate_type=any(p_types) and (e.gate_property is null or e.gate_type||'/'||e.gate_property=any(p_props))
  and (p_scope is null or e.gate_type=any(p_scope))
  and (case when p_kind='instance' then exists(select 1 from ontology.objects o where o.tenant_id=e.tenant_id and o.world=e.world
        and o.type_name=e.gate_type and o.object_id=e.object_id and o.nexloop_revision=e.source_revision)
       -- An alias stays live only while its canonical ref exists in the latest definition.
       when e.field='alias' then case when ontology.nexloop_recall_latest_version(p_tenant,e.gate_type) is null then false
        else exists(select 1 from ontology.nexloop_recall_definition_rows(p_tenant,e.gate_type,ontology.nexloop_recall_latest_version(p_tenant,e.gate_type)) d where d.ref=e.ref) end
       else e.source_revision=ontology.nexloop_recall_latest_version(p_tenant,e.gate_type) end)
$$;

-- One method per call; rows carry tenant/world/gates/revision for the caller's
-- return check. Entries are filtered by tenant, world, readable gates and live
-- source revision before any score is computed. Text is never returned.
create function control.nexloop_recall_search(p_digest text,p_world text,p_request jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
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
   select e.ref,e.field,e.gate_type,e.gate_property,e.object_id,e.tenant_id,e.world,e.source_revision,1.0::double precision score
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
   select e.ref,e.field,e.gate_type,e.gate_property,e.object_id,e.tenant_id,e.world,e.source_revision,
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
   select e.ref,e.field,e.gate_type,e.gate_property,e.object_id,e.tenant_id,e.world,e.source_revision,
    ((select count(*) from unnest(qlex) l where l=any(tsvector_to_array(e.tsv)))::double precision/cardinality(qlex)) score
   from ontology.nexloop_recall_entries e join ontology.nexloop_recall_scope(t,p_world,kind,types,props,scope) s(entry_id) using(entry_id)
   where e.tsv @@ tsq and e.field<>'strong_id'
   order by score desc,e.entry_id limit lim) x;
 else
  qgrams:=ontology.nexloop_recall_ngrams(coalesce(q->>'text',''));
  if cardinality(qgrams)=0 then return '[]'::jsonb;end if;
  -- Fuzzy matching is for names/aliases/values only, not unbounded descriptions.
  select coalesce(jsonb_agg(row_to_json(x)::jsonb),'[]'::jsonb) into res from (
   select e.ref,e.field,e.gate_type,e.gate_property,e.object_id,e.tenant_id,e.world,e.source_revision,
    ontology.nexloop_recall_jaccard(e.ngrams,qgrams) score
   from ontology.nexloop_recall_entries e join ontology.nexloop_recall_scope(t,p_world,kind,types,props,scope) s(entry_id) using(entry_id)
   where e.ngrams && qgrams and e.field in ('name','display_name','alias','vocabulary_value','title')
   order by score desc,e.entry_id limit lim) x;
 end if;
 return res;
end $$;

do $grants$
declare f text;
begin
 foreach f in array array['nexloop_recall_embedding_guard()',
  'nexloop_recall_tenant(text,text)','nexloop_recall_definition_rows(text,text,integer)','nexloop_recall_latest_version(text,text)',
  'nexloop_recall_instance_rows(text,text,text,text,jsonb)','nexloop_recall_active_profile(text,text,integer)',
  'nexloop_recall_attach(text,bigint[],text,integer,jsonb)',
  'nexloop_recall_insert(text,text,text,text,bigint,text,text,text,text,text,text,text,text,text)',
  'nexloop_recall_scope(text,text,text,text[],text[],text[])'] loop
  execute 'alter function ontology.'||f||' owner to nexloop_owner';
  execute 'revoke all on function ontology.'||f||' from public';
 end loop;
 -- Entry points live in control (application roles have schema usage there only).
 foreach f in array array['nexloop_recall_gates(text,text,text)','nexloop_recall_activate_profile(text,text,text,integer,text)',
  'nexloop_recall_definition_texts(text,text,text)','nexloop_recall_index_definition(text,text,text,integer,text,integer,jsonb)',
  'nexloop_recall_index_alias(text,text,text,text,text,text,integer,jsonb)','nexloop_recall_instance_texts(text,text,text,text,jsonb)',
  'nexloop_recall_index_instance(text,text,text,text,jsonb,bigint,text,integer,jsonb)','nexloop_recall_remove_source(text,text,text)',
  'nexloop_recall_search(text,text,jsonb)'] loop
  execute 'alter function control.'||f||' owner to nexloop_owner';
  execute 'revoke all on function control.'||f||' from public';
 end loop;
end $grants$;
-- Recall reads: API and domain worker. Index reflow/profile: domain worker only.
grant execute on function control.nexloop_recall_search(text,text,jsonb),control.nexloop_recall_gates(text,text,text) to nexloop_api,nexloop_domain_worker;
grant execute on function control.nexloop_recall_activate_profile(text,text,text,integer,text),
 control.nexloop_recall_definition_texts(text,text,text),control.nexloop_recall_index_definition(text,text,text,integer,text,integer,jsonb),
 control.nexloop_recall_index_alias(text,text,text,text,text,text,integer,jsonb),control.nexloop_recall_instance_texts(text,text,text,text,jsonb),
 control.nexloop_recall_index_instance(text,text,text,text,jsonb,bigint,text,integer,jsonb),control.nexloop_recall_remove_source(text,text,text)
 to nexloop_domain_worker;
