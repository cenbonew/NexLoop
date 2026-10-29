-- World-model glossary (owner 2026-10-29; docs/design/world-model-glossary.md r2, L6 PASS 9a4dcc4f + I5/S4-S6). Temporary number; the
-- dispatcher renumbers. Published migrations are not edited.
--
-- Chinese glosses for the English identifiers of the enterprise world model (Consumer -> 客户). Display metadata only: no identifier,
-- contract or Action name changes, nothing in ontology, authorization, planning or context. One glossary per tenant, shared by every
-- world (the identifiers are one set of definitions); the owner's correction is accepted in the real world only.
--
-- * control.nexloop_world_glossary: the current gloss of (tenant, kind, identifier). source manual (the owner's manifest or correction;
--   '' = cleared by hand, English only) or machine (the translator; never over a manual row). kind relation covers relation names and
--   inverse names alike (they are distinct strings; the v1 manifest lists both under relations).
-- * control.nexloop_world_glossary_revisions: append-only history of every write (manifest, machine, correction, restore).
-- * control.nexloop_world_glossary_publications: the manifest publications (configurator only, file text + sha256 + expected revision,
--   as 0351). An entry omitted from a later manifest keeps its gloss; an entry given as null is restored to automatic translation (the
--   row goes, the translator picks it up again); '' is a manual clearing.
-- * control.nexloop_world_glossary_jobs / _usage / _quota: the translator's queue, its per-call usage (tokens, cost) and the per tenant
--   and UTC day reservation counter (I5: reserved before the model is called; a reservation is never returned).
-- * authz.nexloop_world_glossary_port: the translator's only way in (signed service port under the service Action
--   nexloop.world_glossary.translate, as the claim feed 0069): scan, due, reserve, record. No table privileges.
-- * nexloop.world_glossary.set: the owner's correction through the governed entry (handler below).
-- * authz.nexloop_workbench_world_model is renamed and wrapped: every identifier gains gloss / gloss_source, relation types also
--   inverse_gloss / inverse_gloss_source, and the snapshot gains glossary {manifest_version, sections}.

create function control.nexloop_world_glossary_valid(p_gloss text) returns boolean language sql immutable set search_path=pg_catalog,pg_temp as $$
 select p_gloss is not null and (p_gloss='' or (char_length(p_gloss)<=24 and p_gloss ~ '[一-鿿]' and p_gloss=btrim(p_gloss)
  and p_gloss !~ '[()\n\r]'))  -- full-width parentheses allowed (the v1 manifest has 金额（最小单位）); the translator refuses any
$$;
alter function control.nexloop_world_glossary_valid(text) owner to nexloop_owner;

create table control.nexloop_world_glossary (
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 kind text not null check(kind in ('section','object_type','property','relation','event_type','function','action')),
 identifier text not null check(char_length(identifier) between 1 and 200),
 gloss text not null check(control.nexloop_world_glossary_valid(gloss)),
 source text not null check(source in ('manual','machine')),
 revision integer not null check(revision>=1),
 model_provider text,model_id text,input_sha256 text check(input_sha256~'^[0-9a-f]{64}$'),translated_at timestamptz,
 updated_at timestamptz not null default clock_timestamp(),
 check((source='machine')=(model_id is not null and model_provider is not null and input_sha256 is not null and translated_at is not null)),
 check(source='manual' or gloss<>''),
 primary key(tenant_id,kind,identifier)
);
create table control.nexloop_world_glossary_revisions (
 tenant_id text not null references control.nexloop_tenants(tenant_id),kind text not null,identifier text not null,revision integer not null check(revision>=1),
 prior_gloss text,prior_source text,gloss text,source text check(source in ('manual','machine')),
 cause text not null check(cause in ('manifest','machine','correction','restore')),
 manifest_version integer,model_id text,intent_id text,actor_principal text,recorded_at timestamptz not null default clock_timestamp(),
 check((cause='restore')=(gloss is null)),check((cause='correction')=(actor_principal is not null and intent_id is not null)),
 check((cause in ('manifest','restore'))=(manifest_version is not null)),check((cause='machine')=(model_id is not null)),
 primary key(tenant_id,kind,identifier,revision)
);
create table control.nexloop_world_glossary_publications (
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 manifest_version integer not null check(manifest_version between 1 and 999999999),
 manifest_sha256 text not null check(manifest_sha256~'^[0-9a-f]{64}$'),
 expected_revision integer not null check(expected_revision>=0),check(expected_revision<manifest_version),
 entries integer not null check(entries>=0),
 published_by text not null default session_user,published_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,manifest_version)
);
create table control.nexloop_world_glossary_jobs (
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 kind text not null check(kind in ('object_type','property','relation','event_type','function','action')),
 identifier text not null check(char_length(identifier) between 1 and 200),
 status text not null default 'pending' check(status in ('pending','done','failed')),
 attempts integer not null default 0 check(attempts between 0 and 3),
 next_attempt_at timestamptz not null default clock_timestamp(),
 last_error text check(last_error in ('http_429','http_5xx','http_4xx','timeout','transport','response_parse','profile','unavailable')),
 created_at timestamptz not null default clock_timestamp(),updated_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,kind,identifier)
);
create table control.nexloop_world_glossary_quota (
 tenant_id text not null references control.nexloop_tenants(tenant_id),day date not null,reserved integer not null default 0 check(reserved>=0),
 primary key(tenant_id,day)
);
create table control.nexloop_world_glossary_usage (
 tenant_id text not null references control.nexloop_tenants(tenant_id),usage_id text not null check(usage_id~'^wgu_[0-9a-f-]{36}$'),
 day date not null,kind text not null,identifier text not null,
 outcome text check(outcome in ('translated','manual_kept','failed')),error_code text,
 provider text,model_id text,prompt_tokens integer check(prompt_tokens>=0),completion_tokens integer check(completion_tokens>=0),
 amount numeric(24,8) check(amount>=0),currency text check(currency~'^[A-Z]{3}$'),check((amount is null)=(currency is null)),
 reserved_at timestamptz not null default clock_timestamp(),recorded_at timestamptz,
 primary key(tenant_id,usage_id)
);
create index nexloop_world_glossary_usage_day on control.nexloop_world_glossary_usage(tenant_id,day);
do $tables$
declare t text;
begin
 foreach t in array array['control.nexloop_world_glossary','control.nexloop_world_glossary_revisions','control.nexloop_world_glossary_publications',
   'control.nexloop_world_glossary_jobs','control.nexloop_world_glossary_quota','control.nexloop_world_glossary_usage'] loop
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy tenant_boundary on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on %s from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_stand_in_worker',t);
 end loop;
end $tables$;
create trigger world_glossary_revisions_append_only before update or delete on control.nexloop_world_glossary_revisions
 for each row execute function control.nexloop_nx022_append_only();
create trigger world_glossary_publications_append_only before update or delete on control.nexloop_world_glossary_publications
 for each row execute function control.nexloop_nx022_append_only();
create function control.nexloop_world_glossary_revisions_no_truncate() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin raise exception 'world glossary revisions are append-only' using errcode='42501';end $$;
alter function control.nexloop_world_glossary_revisions_no_truncate() owner to nexloop_owner;
revoke all on function control.nexloop_world_glossary_revisions_no_truncate() from public;
create trigger world_glossary_revisions_no_truncate before truncate on control.nexloop_world_glossary_revisions
 for each statement execute function control.nexloop_world_glossary_revisions_no_truncate();

-- The snapshot function is renamed first (wrapped at the end of this file); the identifier list reads the original.
alter function authz.nexloop_workbench_world_model(text,text) rename to nexloop_workbench_world_model_before_glossary;
revoke all on function authz.nexloop_workbench_world_model_before_glossary(text,text) from public;

-- The identifiers of the tenant's world model, with the metadata the translator may see (no instance, nothing personal; a withheld
-- property gives its name only). One row per (kind, identifier): a property name shared by several types gives the first type.
create function control.nexloop_world_glossary_identifiers(p_tenant text,p_world text) returns table(kind text,identifier text,owner_type text,
 description text,display_name text)
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 with m as (select authz.nexloop_workbench_world_model_before_glossary(p_tenant,p_world) m),
 x as (
  select 'object_type' kind,o->>'name' identifier,null::text owner_type,o->>'description' description,o->>'display_name' display_name
   from m,jsonb_array_elements(m->'object_types') o
  union all
  select 'property',p->>'name',o->>'name',case when (p->>'withheld')::boolean then null else p->>'description' end,null
   from m,jsonb_array_elements(m->'object_types') o,jsonb_array_elements(o->'properties') p
  union all
  select 'relation',r->>'name',r->>'source_type',r->>'description',null from m,jsonb_array_elements(m->'relation_types') r
  union all
  select 'relation',r->>'inverse_name',r->>'target_type',r->>'description',null from m,jsonb_array_elements(m->'relation_types') r where coalesce(r->>'inverse_name','')<>''
  union all
  select 'event_type',e->>'name',e->>'object_type',e->>'description',null from m,jsonb_array_elements(m->'event_types') e
  union all
  select 'function',f->>'name',null,null,null from m,jsonb_array_elements(m->'functions') f
  union all
  select 'action',a->>'name',null,null,null from m,jsonb_array_elements(m->'actions') a)
 select distinct on (x.kind,x.identifier) x.kind,x.identifier,x.owner_type,left(nullif(x.description,''),200),left(nullif(x.display_name,''),200)
  from x where coalesce(x.identifier,'')<>'' order by x.kind,x.identifier,x.owner_type
$$;

-- The next revision of an identifier (history continues across a restore, which removes the current row).
create function control.nexloop_world_glossary_next_revision(p_tenant text,p_kind text,p_identifier text) returns integer
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select coalesce(max(r.revision),0)+1 from control.nexloop_world_glossary_revisions r
  where r.tenant_id=p_tenant and r.kind=p_kind and r.identifier=p_identifier
$$;
alter function control.nexloop_world_glossary_next_revision(text,text,text) owner to nexloop_owner;
revoke all on function control.nexloop_world_glossary_next_revision(text,text,text) from public;

-- A manual write (manifest or correction): replaces any current row, appends the revision.
create function control.nexloop_world_glossary_write_manual(p_tenant text,p_kind text,p_identifier text,p_gloss text,p_cause text,
 p_manifest integer,p_intent text,p_actor text) returns integer
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare cur control.nexloop_world_glossary;rev integer;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into cur from control.nexloop_world_glossary where tenant_id=p_tenant and kind=p_kind and identifier=p_identifier for update;
 if found and cur.source='manual' and cur.gloss=p_gloss then return cur.revision;end if;  -- unchanged: nothing written
 rev:=control.nexloop_world_glossary_next_revision(p_tenant,p_kind,p_identifier);
 insert into control.nexloop_world_glossary(tenant_id,kind,identifier,gloss,source,revision,updated_at)
  values(p_tenant,p_kind,p_identifier,p_gloss,'manual',rev,clock_timestamp())
  on conflict(tenant_id,kind,identifier) do update set gloss=excluded.gloss,source='manual',revision=excluded.revision,model_provider=null,model_id=null,
   input_sha256=null,translated_at=null,updated_at=excluded.updated_at;
 insert into control.nexloop_world_glossary_revisions(tenant_id,kind,identifier,revision,prior_gloss,prior_source,gloss,source,cause,manifest_version,intent_id,actor_principal)
  values(p_tenant,p_kind,p_identifier,rev,cur.gloss,cur.source,p_gloss,'manual',p_cause,p_manifest,p_intent,p_actor);
 -- A manual gloss ends any pending translation of it.
 update control.nexloop_world_glossary_jobs set status='done',updated_at=clock_timestamp() where tenant_id=p_tenant and kind=p_kind and identifier=p_identifier
  and status<>'done';
 return rev;
end $$;
alter function control.nexloop_world_glossary_write_manual(text,text,text,text,text,integer,text,text) owner to nexloop_owner;
revoke all on function control.nexloop_world_glossary_write_manual(text,text,text,text,text,integer,text,text) from public;

-- Manifest publication (configurator only; file text + sha256 + expected revision, one at a time per tenant).
create function control.nexloop_publish_world_glossary(p_tenant text,p_text text,p_digest text,p_expected integer) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);m jsonb;v integer;cur integer;pub control.nexloop_world_glossary_publications;
 grp record;e record;kinds constant jsonb:='{"sections":"section","object_types":"object_type","properties":"property","relations":"relation",
  "event_types":"event_type","functions":"function","actions":"action"}';n integer:=0;written integer:=0;restored integer:=0;
 k text;rev integer;old control.nexloop_world_glossary;
begin
 if session_user<>'nexloop_configurator' then raise exception 'world glossary is trusted configuration only' using errcode='42501';end if;
 if p_tenant is null or p_text is null or octet_length(p_text)>1048576 or p_expected is null or p_expected<0
  or p_digest is distinct from encode(sha256(convert_to(p_text,'UTF8')),'hex') then
  raise exception 'world glossary: invalid' using errcode='22023';end if;
 begin m:=p_text::jsonb;exception when others then raise exception 'world glossary: invalid' using errcode='22023';end;
 if jsonb_typeof(m) is distinct from 'object' or m->>'schema_version' is distinct from 'nexloop-world-model-glossary/1'
  or jsonb_typeof(m->'manifest_version') is distinct from 'number' or coalesce(m->>'manifest_version','')!~'^[1-9][0-9]{0,8}$'
  or exists(select 1 from jsonb_object_keys(m) x where x not in ('schema_version','manifest_version','decision','source','sections','object_types',
    'properties','relations','event_types','functions','actions'))
  or exists(select 1 from jsonb_each(m) x where x.key in (select jsonb_object_keys(kinds)) and jsonb_typeof(x.value) is distinct from 'object')
  -- I8 (L6): the section names are the v2 contract's enum (workbench-world-model-v2 glossary.sections propertyNames), exactly.
  or exists(select 1 from jsonb_object_keys(case when jsonb_typeof(m->'sections')='object' then m->'sections' else '{}' end) x
    where x not in ('Object Type','Object','Property','Link','Function','Action','Object Set','Event Type','Payload Property','Table','Graph','Object View','Interface','Shared Property'))
  or exists(select 1 from jsonb_each(m) x,jsonb_each(x.value) y where x.key in (select jsonb_object_keys(kinds))
    and (char_length(y.key) not between 1 and 200 or not (jsonb_typeof(y.value)='null' or (jsonb_typeof(y.value)='string'
     and control.nexloop_world_glossary_valid(y.value#>>'{}'))))) then
  raise exception 'world glossary: invalid' using errcode='22023';end if;
 v:=(m->>'manifest_version')::integer;
 perform pg_advisory_xact_lock(hashtextextended('nexloop-world-glossary:'||p_tenant,0));
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into pub from control.nexloop_world_glossary_publications q where q.tenant_id=p_tenant and q.manifest_version=v;
 if found then
  perform set_config('eios.tenant_id',coalesce(prior,''),true);
  if pub.manifest_sha256<>p_digest then raise exception 'world glossary: version % published with another text',v using errcode='40001';end if;
  return jsonb_build_object('configured',true,'manifest_version',v,'manifest_sha256',p_digest,'expected_revision',pub.expected_revision,
   'entries',pub.entries,'replay',true);
 end if;
 select coalesce(max(q.manifest_version),0) into cur from control.nexloop_world_glossary_publications q where q.tenant_id=p_tenant;
 if p_expected<>cur or v<=cur then
  perform set_config('eios.tenant_id',coalesce(prior,''),true);
  raise exception 'world glossary: expected revision %, current %',p_expected,cur using errcode='40001';end if;
 for grp in select x.key,x.value from jsonb_each(m) x where x.key in (select jsonb_object_keys(kinds)) loop
  k:=kinds->>grp.key;
  for e in select y.key,y.value from jsonb_each(grp.value) y order by y.key loop
   n:=n+1;
   if jsonb_typeof(e.value)='null' then
    -- Restore to automatic translation: the current row goes (S4); the translator may translate it again.
    select * into old from control.nexloop_world_glossary where tenant_id=p_tenant and kind=k and identifier=e.key for update;
    if found then
     rev:=control.nexloop_world_glossary_next_revision(p_tenant,k,e.key);
     delete from control.nexloop_world_glossary where tenant_id=p_tenant and kind=k and identifier=e.key;
     insert into control.nexloop_world_glossary_revisions(tenant_id,kind,identifier,revision,prior_gloss,prior_source,gloss,source,cause,manifest_version)
      values(p_tenant,k,e.key,rev,old.gloss,old.source,null,null,'restore',v);
     update control.nexloop_world_glossary_jobs set status='pending',attempts=0,last_error=null,next_attempt_at=clock_timestamp(),updated_at=clock_timestamp()
      where tenant_id=p_tenant and kind=k and identifier=e.key;
     restored:=restored+1;
    end if;
   else
    rev:=control.nexloop_world_glossary_write_manual(p_tenant,k,e.key,e.value#>>'{}','manifest',v,null,null);written:=written+1;
   end if;
  end loop;
 end loop;
 insert into control.nexloop_world_glossary_publications(tenant_id,manifest_version,manifest_sha256,expected_revision,entries)
  values(p_tenant,v,p_digest,p_expected,n);
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return jsonb_build_object('configured',true,'manifest_version',v,'manifest_sha256',p_digest,'expected_revision',p_expected,'entries',n,
  'manual_entries',written,'restored',restored,'replay',false);
end $$;
alter function control.nexloop_publish_world_glossary(text,text,text,integer) owner to nexloop_owner;
revoke all on function control.nexloop_publish_world_glossary(text,text,text,integer) from public;
grant execute on function control.nexloop_publish_world_glossary(text,text,text,integer) to nexloop_configurator;

-- The owner's correction (governed entry; capability world_glossary.set). tenant and world come from the entry's verified context, never
-- from the body; the identifier must exist in the world model when the Action is dispatched.
create function control.nexloop_governed_world_glossary_set(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare v_kind text:=body->>'kind';v_id text:=body->>'identifier';v_gloss text:=body->>'gloss';rev integer;
begin
 if jsonb_typeof(body) is distinct from 'object' or body->>'operation' is distinct from 'set_world_glossary'
  or exists(select 1 from jsonb_object_keys(body) x where x not in ('operation','request_id','kind','identifier','gloss'))
  or not (body ? 'kind' and body ? 'identifier' and body ? 'gloss')
  or jsonb_typeof(body->'kind') is distinct from 'string' or jsonb_typeof(body->'identifier') is distinct from 'string'
  or jsonb_typeof(body->'gloss') is distinct from 'string'
  or v_kind not in ('section','object_type','property','relation','event_type','function','action')
  or char_length(v_id) not between 1 and 200 or not control.nexloop_world_glossary_valid(v_gloss) then
  raise exception 'world glossary: invalid' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'world glossary: real world only' using errcode='42501';end if;
 if control.nexloop_workbench_role(p_tenant,p_principal) is distinct from 'owner' then raise exception 'world glossary is owner only' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 -- Checked here, at dispatch (I3): a section is one the published manifests carry; anything else is in the current world model.
 if v_kind='section' then
  if not exists(select 1 from control.nexloop_world_glossary_revisions r where r.tenant_id=p_tenant and r.kind='section' and r.identifier=v_id) then
   raise exception 'world glossary: unknown identifier' using errcode='22023';end if;
 elsif not exists(select 1 from control.nexloop_world_glossary_identifiers(p_tenant,p_world) i where i.kind=v_kind and i.identifier=v_id) then
  raise exception 'world glossary: unknown identifier' using errcode='22023';
 end if;
 rev:=control.nexloop_world_glossary_write_manual(p_tenant,v_kind,v_id,v_gloss,'correction',null,p_intent,p_principal);
 return jsonb_build_object('kind',v_kind,'identifier',v_id,'gloss',v_gloss,'gloss_source','manual','revision',rev);
end $$;
alter function control.nexloop_governed_world_glossary_set(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_governed_world_glossary_set(text,text,text,text,text,jsonb) from public;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('world_glossary.set','set_world_glossary','human','control.nexloop_governed_world_glossary_set(text,text,text,text,text,jsonb)','NX-064');

-- The translator's port (signed service port, as the claim feed 0069, under nexloop.world_glossary.translate).
create function authz.nexloop_world_glossary_port(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;v_tenant text:=a->>'tenant_id';v_verb text:=c->>'verb';v_result jsonb;
 v_limit integer;v_day date:=(clock_timestamp() at time zone 'UTC')::date;q control.nexloop_world_glossary_quota;u control.nexloop_world_glossary_usage;
 j control.nexloop_world_glossary_jobs;v_rev integer;v_count integer;v_gloss text;e runtime.nexloop_cost_entries;v_written boolean;
 c_daily constant integer:=50;
begin
 if session_user is distinct from 'nexloop_domain_worker' or p_world is distinct from 'real' or octet_length(p_text)>1048576 or octet_length(p_payload)>65536
  or a->>'protocol' is distinct from 'nexloop-world-glossary-v1' or a->>'operation' is distinct from 'execute'
  or a->>'action_resource' is distinct from 'eios:action:nexloop.world_glossary.translate:1'
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or v_verb is null or v_verb not in ('scan','due','reserve','record') then
  raise exception 'world glossary port unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-world-glossary-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' or (a->>'expires_at')::timestamptz<=clock_timestamp() then
  raise exception 'world glossary port unavailable' using errcode='42501';end if;
 if exists(select 1 from authz.nexloop_browser_token_realms where token_digest=p_digest)
  or not exists(select 1 from authz.nexloop_service_credentials t where t.token_digest=p_digest and t.tenant_id=v_tenant
     and t.binding->>'subject_kind'='service' and t.status='active') then
  raise exception 'world glossary port unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 perform set_config('eios.tenant_id',v_tenant,true);
 if v_verb='scan' then
  -- Identifiers of the world model with no current gloss and no job yet become pending jobs.
  insert into control.nexloop_world_glossary_jobs(tenant_id,kind,identifier)
   select v_tenant,i.kind,i.identifier from control.nexloop_world_glossary_identifiers(v_tenant,p_world) i
    where not exists(select 1 from control.nexloop_world_glossary g where g.tenant_id=v_tenant and g.kind=i.kind and g.identifier=i.identifier)
   on conflict do nothing;
  get diagnostics v_count=row_count;
  v_result:=jsonb_build_object('enqueued',v_count);
 elsif v_verb='due' then
  v_limit:=(c->>'limit')::integer;
  if v_limit is null or v_limit not between 1 and 10 then raise exception 'world glossary port unavailable' using errcode='42501';end if;
  -- The translator's whole input: identifier metadata and the tenant's object type glosses as terminology (no instance, nothing personal).
  select coalesce(jsonb_agg(jsonb_build_object('kind',x.kind,'identifier',x.identifier,'owner_type',x.owner_type,'description',x.description,
    'display_name',x.display_name) order by x.next_attempt_at,x.kind,x.identifier),'[]'::jsonb) into v_result
  from (select jb.kind,jb.identifier,jb.next_attempt_at,i.owner_type,i.description,i.display_name from control.nexloop_world_glossary_jobs jb
    join control.nexloop_world_glossary_identifiers(v_tenant,p_world) i on i.kind=jb.kind and i.identifier=jb.identifier
    where jb.tenant_id=v_tenant and jb.status='pending' and jb.next_attempt_at<=clock_timestamp()
    order by jb.next_attempt_at,jb.kind,jb.identifier limit v_limit) x;
  v_result:=jsonb_build_object('items',v_result,'terms',coalesce((select jsonb_object_agg(g.identifier,g.gloss) from control.nexloop_world_glossary g
    where g.tenant_id=v_tenant and g.kind='object_type' and g.source='manual' and g.gloss<>''),'{}'::jsonb));
 elsif v_verb='reserve' then
  -- I5: one reservation per model call, taken before the call, under the (tenant, UTC day) row lock; never returned.
  if c->>'kind' is null or c->>'identifier' is null
   or not exists(select 1 from control.nexloop_world_glossary_jobs jb where jb.tenant_id=v_tenant and jb.kind=c->>'kind' and jb.identifier=c->>'identifier'
    and jb.status='pending') then
   return jsonb_build_object('allowed',false,'reason','not_pending');end if;
  insert into control.nexloop_world_glossary_quota(tenant_id,day,reserved) values(v_tenant,v_day,0) on conflict do nothing;
  select * into q from control.nexloop_world_glossary_quota where tenant_id=v_tenant and day=v_day for update;
  if q.reserved>=c_daily then return jsonb_build_object('allowed',false,'reason','full','daily_limit',c_daily);end if;
  update control.nexloop_world_glossary_quota set reserved=reserved+1 where tenant_id=v_tenant and day=v_day;
  insert into control.nexloop_world_glossary_usage(tenant_id,usage_id,day,kind,identifier)
   values(v_tenant,'wgu_'||pg_catalog.gen_random_uuid()::text,v_day,c->>'kind',c->>'identifier') returning * into u;
  v_result:=jsonb_build_object('allowed',true,'usage_id',u.usage_id,'daily_limit',c_daily,'reserved',q.reserved+1);
 else
  -- record: the outcome of one reserved call. A gloss is written only over no row or a machine row (I1: the conflict update's own
  -- predicate, under the row lock, so a concurrent correction always stays).
  select * into u from control.nexloop_world_glossary_usage where tenant_id=v_tenant and usage_id=c->>'usage_id' and recorded_at is null for update;
  if not found then raise exception 'world glossary port unavailable' using errcode='42501';end if;
  if exists(select 1 from jsonb_object_keys(c) x where x not in ('verb','usage_id','gloss','error_code','provider','model_id','input_sha256',
     'prompt_tokens','completion_tokens','amount','currency'))
   or (c ? 'gloss')=(c ? 'error_code') or coalesce(c->>'model_id','')='' or coalesce(c->>'provider','')=''
   or coalesce(c->>'input_sha256','')!~'^[0-9a-f]{64}$' or (c->'amount' is null)<>(c->'currency' is null) then
   raise exception 'world glossary port unavailable' using errcode='42501';end if;
  select * into j from control.nexloop_world_glossary_jobs where tenant_id=v_tenant and kind=u.kind and identifier=u.identifier for update;
  v_gloss:=c->>'gloss';
  if v_gloss is not null and (v_gloss='' or not control.nexloop_world_glossary_valid(v_gloss) or strpos(lower(v_gloss),lower(u.identifier))>0 or v_gloss ~ '[（）]') then
   v_gloss:=null;c:=c-'gloss'||jsonb_build_object('error_code','response_parse');end if;
  if v_gloss is not null then
   v_rev:=control.nexloop_world_glossary_next_revision(v_tenant,u.kind,u.identifier);
   insert into control.nexloop_world_glossary(tenant_id,kind,identifier,gloss,source,revision,model_provider,model_id,input_sha256,translated_at,updated_at)
    values(v_tenant,u.kind,u.identifier,v_gloss,'machine',v_rev,c->>'provider',c->>'model_id',c->>'input_sha256',clock_timestamp(),clock_timestamp())
    on conflict(tenant_id,kind,identifier) do update set gloss=excluded.gloss,source='machine',revision=excluded.revision,
     model_provider=excluded.model_provider,model_id=excluded.model_id,input_sha256=excluded.input_sha256,translated_at=excluded.translated_at,
     updated_at=excluded.updated_at
     where control.nexloop_world_glossary.source<>'manual'
    returning true into v_written;
   if coalesce(v_written,false) then
    insert into control.nexloop_world_glossary_revisions(tenant_id,kind,identifier,revision,gloss,source,cause,model_id)
     values(v_tenant,u.kind,u.identifier,v_rev,v_gloss,'machine','machine',c->>'model_id');
   end if;
   update control.nexloop_world_glossary_jobs set status='done',last_error=null,updated_at=clock_timestamp() where tenant_id=v_tenant and kind=u.kind and identifier=u.identifier;
   update control.nexloop_world_glossary_usage set outcome=case when coalesce(v_written,false) then 'translated' else 'manual_kept' end
    where tenant_id=v_tenant and usage_id=u.usage_id;
  else
   if coalesce(c->>'error_code','') not in ('http_429','http_5xx','http_4xx','timeout','transport','response_parse','profile','unavailable') then
    raise exception 'world glossary port unavailable' using errcode='42501';end if;
   update control.nexloop_world_glossary_jobs set attempts=least(attempts+1,3),last_error=c->>'error_code',
    status=case when attempts+1>=3 then 'failed' else 'pending' end,
    next_attempt_at=clock_timestamp()+make_interval(secs=>least(3600,60*power(2,attempts))::double precision),updated_at=clock_timestamp()
    where tenant_id=v_tenant and kind=u.kind and identifier=u.identifier and status='pending';
   update control.nexloop_world_glossary_usage set outcome='failed',error_code=c->>'error_code' where tenant_id=v_tenant and usage_id=u.usage_id;
  end if;
  update control.nexloop_world_glossary_usage set provider=c->>'provider',model_id=c->>'model_id',prompt_tokens=(c->>'prompt_tokens')::integer,
   completion_tokens=(c->>'completion_tokens')::integer,amount=(c->>'amount')::numeric,currency=c->>'currency',recorded_at=clock_timestamp()
   where tenant_id=v_tenant and usage_id=u.usage_id;
  -- The cost, in the existing ledger (0120): configured_rate when the tariff priced it, otherwise unpriced (units only, S6).
  e.tenant_id:=v_tenant;e.world:=p_world;e.entry_id:='model:world-glossary:'||u.usage_id;e.data_mode:=runtime.nexloop_cost_data_mode(p_world);
  e.cost_kind:='model';e.units:=1;e.amount:=(c->>'amount')::numeric;e.currency:=c->>'currency';
  e.amount_unit:=case when c ? 'amount' then 'major' end;e.basis:=case when c ? 'amount' then 'configured_rate' else 'unpriced' end;
  e.source_ref:='world-glossary-usage:'||u.usage_id;e.occurred_at:=clock_timestamp();e.recorded_by:='nexloop:world-glossary-translator';
  perform runtime.nexloop_cost_record(e);
  perform set_config('eios.tenant_id',v_tenant,true);
  v_result:=jsonb_build_object('usage_id',u.usage_id,'written',coalesce(v_written,false),'error_code',c->>'error_code');
 end if;
 return v_result;
end $$;
alter function authz.nexloop_world_glossary_port(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_world_glossary_port(text,text,text,text,text) from public;
grant execute on function authz.nexloop_world_glossary_port(text,text,text,text,text) to nexloop_domain_worker;

-- The workbench snapshot with glosses.
alter function control.nexloop_world_glossary_identifiers(text,text) owner to nexloop_owner;
revoke all on function control.nexloop_world_glossary_identifiers(text,text) from public;

create function control.nexloop_world_glossary_of(p_tenant text,p_kind text,p_identifier text,p_prefix text default '') returns jsonb
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select coalesce((select jsonb_build_object(p_prefix||'gloss',g.gloss,p_prefix||'gloss_source',g.source) from control.nexloop_world_glossary g
   where g.tenant_id=p_tenant and g.kind=p_kind and g.identifier=p_identifier),
  jsonb_build_object(p_prefix||'gloss',null,p_prefix||'gloss_source',null))
$$;
alter function control.nexloop_world_glossary_of(text,text,text,text) owner to nexloop_owner;
revoke all on function control.nexloop_world_glossary_of(text,text,text,text) from public;

create function authz.nexloop_workbench_world_model(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare m jsonb:=authz.nexloop_workbench_world_model_before_glossary(p_tenant,p_world);
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 return m||jsonb_build_object(
  'glossary',jsonb_build_object('manifest_version',coalesce((select max(p.manifest_version) from control.nexloop_world_glossary_publications p where p.tenant_id=p_tenant),0),
   'sections',coalesce((select jsonb_object_agg(g.identifier,g.gloss) from control.nexloop_world_glossary g where g.tenant_id=p_tenant and g.kind='section'),'{}'::jsonb)),
  'object_types',coalesce((select jsonb_agg(o.value||control.nexloop_world_glossary_of(p_tenant,'object_type',o.value->>'name')
     ||jsonb_build_object('properties',coalesce((select jsonb_agg(p.value||control.nexloop_world_glossary_of(p_tenant,'property',p.value->>'name') order by p.ordinality)
       from jsonb_array_elements(o.value->'properties') with ordinality p),'[]'::jsonb)) order by o.ordinality)
    from jsonb_array_elements(m->'object_types') with ordinality o),'[]'::jsonb),
  'relation_types',coalesce((select jsonb_agg(r.value||control.nexloop_world_glossary_of(p_tenant,'relation',r.value->>'name')
     ||case when coalesce(r.value->>'inverse_name','')='' then jsonb_build_object('inverse_gloss',null,'inverse_gloss_source',null)
       else control.nexloop_world_glossary_of(p_tenant,'relation',r.value->>'inverse_name','inverse_') end order by r.ordinality)
    from jsonb_array_elements(m->'relation_types') with ordinality r),'[]'::jsonb),
  'event_types',coalesce((select jsonb_agg(x.value||control.nexloop_world_glossary_of(p_tenant,'event_type',x.value->>'name') order by x.ordinality)
    from jsonb_array_elements(m->'event_types') with ordinality x),'[]'::jsonb),
  'actions',coalesce((select jsonb_agg(x.value||control.nexloop_world_glossary_of(p_tenant,'action',x.value->>'name') order by x.ordinality)
    from jsonb_array_elements(m->'actions') with ordinality x),'[]'::jsonb),
  'functions',coalesce((select jsonb_agg(x.value||control.nexloop_world_glossary_of(p_tenant,'function',x.value->>'name') order by x.ordinality)
    from jsonb_array_elements(m->'functions') with ordinality x),'[]'::jsonb));
end $$;
alter function authz.nexloop_workbench_world_model(text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_world_model(text,text) from public;

-- I7 (L6): the timer can keep succeeding while every translation fails (a key revoked for good, a provider that always refuses), and
-- the workbench would show English only forever. The metrics snapshot (world real) gains world_glossary for alert rules v22:
--   failed_24h                      jobs that reached failed (3 attempts) in the last 24 hours
--   pending                         jobs still waiting
--   calls_24h / translated_24h      model calls recorded and calls that wrote a gloss, last 24 hours
--   pending_without_success_24h     pending jobs while the last 24 hours had at least 3 calls and not one wrote a gloss (else 0)
alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_world_glossary;
revoke all on function runtime.nexloop_metrics_snapshot_before_world_glossary(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v jsonb:=runtime.nexloop_metrics_snapshot_before_world_glossary(p_tenant,p_world);prior text:=current_setting('eios.tenant_id',true);
 since timestamptz:=clock_timestamp()-interval '24 hours';v_failed bigint;v_pending bigint;v_calls bigint;v_translated bigint;
begin
 if p_world is distinct from 'real' then return v;end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select count(*) filter (where j.status='failed' and j.updated_at>=since),count(*) filter (where j.status='pending')
  into v_failed,v_pending from control.nexloop_world_glossary_jobs j where j.tenant_id=p_tenant;
 select count(*) filter (where u.recorded_at is not null),count(*) filter (where u.outcome='translated')
  into v_calls,v_translated from control.nexloop_world_glossary_usage u where u.tenant_id=p_tenant and u.reserved_at>=since;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v||jsonb_build_object('world_glossary',jsonb_build_object('failed_24h',v_failed,'pending',v_pending,'calls_24h',v_calls,
  'translated_24h',v_translated,'pending_without_success_24h',case when v_pending>0 and v_calls>=3 and v_translated=0 then v_pending else 0 end));
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;
