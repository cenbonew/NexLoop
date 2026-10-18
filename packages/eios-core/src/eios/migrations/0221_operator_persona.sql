-- Operator persona, slice 1 (temporary number 0221; design docs/design/operator-persona-design.md, owner decisions 2026-10-19:
-- D1-D10). The persona is how "the operator" speaks to customers; it is not ADR-034's test persona marker (Consumer persona).
-- 1. control.nexloop_operator_personas: immutable versions (persona_id@version, document, its hash) and an append-only status
--    history (published | retired). A version is usable while its latest status is published; a persona resolves to its
--    highest usable version (D4: assigned customers follow new versions).
-- 2. Assignment (D3), most specific first: the owner's per-Consumer assignment; the tenant's segment rules (immutable versions,
--    first match wins; v1 conditions: an active ConsumerRoleLink to a role, or a Consumer property equal to a value); the rules'
--    default persona. runtime.nexloop_operator_persona_desired is pure. Stability: control.nexloop_operator_persona_current keeps
--    each Consumer's settled persona; it changes only with an append-only assignment event naming the source (initial,
--    owner_assign, owner_clear, rule_change, persona_retired). The owner's Actions settle at once; a rule publication or a
--    retirement settles at the Consumer's next Context bind.
-- 3. control.nexloop_ai_disclosure (tenant, revision, append-only events): mode none | first_contact | always (default none),
--    when_asked deflect | handoff (default deflect), block_human_claims (default false: owner D7, no platform baseline; the
--    tenant carries compliance), content_label (default false). The wording lives in the persona document (versioned).
-- 4. Governed human Actions (0124 registry, workbench owner only): operator_persona.publish / publish_operator_persona,
--    operator_persona.assign / assign_operator_persona, operator_persona.rules_publish / publish_persona_rules,
--    ai_disclosure.set / set_ai_disclosure. Refusals are errcode 22023 'operator persona: <code>'.
-- 5. runtime.nexloop_operator_persona_section: the Context section {persona_ref operator-persona:<id>@<version>, persona_id,
--    version, source, document, ai_disclosure}, or null when the tenant has no usable persona; authz.nexloop_context_operator_persona
--    serves it to the Context assembler (service identity of the tenant). The Context v6 bind (below, 0187 body) requires the pack
--    to carry exactly this section when it is not null, settles the assignment and cites it as a role source in the manifest.
-- 6. Workbench observe verb operator_personas (owner only, read only): personas and their statuses, the current rules, the AI
--    disclosure, and for one Consumer its desired and settled persona.

-- 1. Persona versions ----------------------------------------------------------------------------------------------------------
create table control.nexloop_operator_personas (
 tenant_id text not null,persona_id text not null check(persona_id~'^[a-z][a-z0-9_-]{0,63}$'),version integer not null check(version between 1 and 999999),
 document jsonb not null check(jsonb_typeof(document)='object'),document_hash text not null check(document_hash~'^[0-9a-f]{64}$'),
 created_by text not null,request_intent text not null,created_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,persona_id,version),foreign key(tenant_id) references control.nexloop_tenants(tenant_id)
);
create table control.nexloop_operator_persona_status_events (
 event_id bigint generated always as identity primary key,tenant_id text not null,persona_id text not null,version integer not null,
 status text not null check(status in ('published','retired')),set_by text not null,request_intent text not null,
 reason text not null check(char_length(reason) between 1 and 500),recorded_at timestamptz not null default clock_timestamp(),
 foreign key(tenant_id,persona_id,version) references control.nexloop_operator_personas(tenant_id,persona_id,version),
 foreign key(tenant_id) references control.nexloop_tenants(tenant_id)
);
create table control.nexloop_operator_persona_rules (
 tenant_id text not null,version integer not null check(version between 1 and 999999),default_persona_id text check(default_persona_id~'^[a-z][a-z0-9_-]{0,63}$'),
 rules jsonb not null check(jsonb_typeof(rules)='array'),published_by text not null,request_intent text not null,
 reason text not null check(char_length(reason) between 1 and 500),published_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,version),foreign key(tenant_id) references control.nexloop_tenants(tenant_id)
);
create table control.nexloop_operator_persona_assignments (
 tenant_id text not null,world text not null,consumer_id text not null check(consumer_id~'^[a-f0-9]{64}$'),
 persona_id text check(persona_id~'^[a-z][a-z0-9_-]{0,63}$'),revision bigint not null check(revision>0),
 assigned_by text not null,request_intent text not null,updated_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,consumer_id),foreign key(tenant_id) references control.nexloop_tenants(tenant_id)
);
create table control.nexloop_operator_persona_current (
 tenant_id text not null,world text not null,consumer_id text not null check(consumer_id~'^[a-f0-9]{64}$'),
 persona_id text not null check(persona_id~'^[a-z][a-z0-9_-]{0,63}$'),source text not null check(source in ('owner_assign','rule','default')),
 rules_version integer,event_id bigint not null,updated_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,consumer_id),foreign key(tenant_id) references control.nexloop_tenants(tenant_id)
);
create table control.nexloop_operator_persona_assignment_events (
 event_id bigint generated always as identity primary key,tenant_id text not null,world text not null,consumer_id text not null check(consumer_id~'^[a-f0-9]{64}$'),
 previous text,persona_id text not null,source text not null check(source in ('initial','owner_assign','owner_clear','rule_change','persona_retired')),
 rules_version integer,set_by text not null,request_intent text,reason text check(reason is null or char_length(reason) between 1 and 500),
 recorded_at timestamptz not null default clock_timestamp(),foreign key(tenant_id) references control.nexloop_tenants(tenant_id)
);
create table control.nexloop_ai_disclosure (
 tenant_id text primary key,mode text not null check(mode in ('none','first_contact','always')),when_asked text not null check(when_asked in ('deflect','handoff')),
 block_human_claims boolean not null,content_label boolean not null,revision bigint not null check(revision>0),
 updated_by text not null,request_intent text not null,updated_at timestamptz not null default clock_timestamp(),
 foreign key(tenant_id) references control.nexloop_tenants(tenant_id)
);
create table control.nexloop_ai_disclosure_events (
 event_id bigint generated always as identity primary key,tenant_id text not null,revision bigint not null,setting jsonb not null,
 set_by text not null,request_intent text not null,reason text not null check(char_length(reason) between 1 and 500),
 recorded_at timestamptz not null default clock_timestamp(),unique(tenant_id,revision),foreign key(tenant_id) references control.nexloop_tenants(tenant_id)
);
create function control.nexloop_operator_persona_append_only() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin raise exception 'operator persona records are append-only' using errcode='42501';end $$;
alter function control.nexloop_operator_persona_append_only() owner to nexloop_owner;
do $tables$
declare t text;
begin
 foreach t in array array['control.nexloop_operator_personas','control.nexloop_operator_persona_status_events','control.nexloop_operator_persona_rules',
   'control.nexloop_operator_persona_assignments','control.nexloop_operator_persona_current','control.nexloop_operator_persona_assignment_events',
   'control.nexloop_ai_disclosure','control.nexloop_ai_disclosure_events'] loop
  execute 'alter table '||t||' owner to nexloop_owner';execute 'alter table '||t||' enable row level security';
  execute 'alter table '||t||' force row level security';execute 'revoke all on '||t||' from public';
  execute format('create policy %I on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',
   'tenant_'||replace(t,'control.',''),t);
 end loop;
 foreach t in array array['control.nexloop_operator_personas','control.nexloop_operator_persona_status_events','control.nexloop_operator_persona_rules',
   'control.nexloop_operator_persona_assignment_events','control.nexloop_ai_disclosure_events'] loop
  execute format('create trigger operator_persona_append_only before update or delete on %s for each row execute function control.nexloop_operator_persona_append_only()',t);
 end loop;
end $tables$;

-- 2. Validation, resolution -------------------------------------------------------------------------------------------------
create function control.nexloop_operator_persona_document_ok(d jsonb) returns boolean
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select jsonb_typeof(d)='object' and octet_length(d::text)<=8192
  and not exists(select 1 from jsonb_object_keys(d) k where k not in ('display_name','signature','self_intro','tone','values','expression','scenarios','not_for','ai_disclosure'))
  and jsonb_typeof(d->'display_name')='string' and char_length(d->>'display_name') between 1 and 40
  and coalesce(jsonb_typeof(d->'signature'),'string')='string' and char_length(coalesce(d->>'signature','')) <= 40
  and coalesce(jsonb_typeof(d->'self_intro'),'string')='string' and char_length(coalesce(d->>'self_intro','')) <= 500
  and jsonb_typeof(d->'tone')='string' and char_length(d->>'tone') between 1 and 200
  and coalesce(jsonb_typeof(d->'values'),'array')='array' and jsonb_array_length(coalesce(d->'values','[]'))<=5
  and not exists(select 1 from jsonb_array_elements(coalesce(d->'values','[]')) v where jsonb_typeof(v)<>'string' or char_length(v#>>'{}') not between 1 and 200)
  and jsonb_typeof(d->'expression')='object'
  and not exists(select 1 from jsonb_object_keys(d->'expression') k where k not in ('max_chars','emoji','vocabulary','forbidden_phrases','language'))
  and jsonb_typeof(d->'expression'->'max_chars')='number' and (d->'expression'->>'max_chars')::numeric between 20 and 2000
  and (d->'expression'->>'max_chars')::numeric=floor((d->'expression'->>'max_chars')::numeric)
  and d->'expression'->>'emoji' in ('none','few','allowed')
  and char_length(coalesce(d->'expression'->>'vocabulary',''))<=500 and char_length(coalesce(d->'expression'->>'language',''))<=16
  and coalesce(jsonb_typeof(d->'expression'->'forbidden_phrases'),'array')='array' and jsonb_array_length(coalesce(d->'expression'->'forbidden_phrases','[]'))<=50
  and not exists(select 1 from jsonb_array_elements(coalesce(d->'expression'->'forbidden_phrases','[]')) v where jsonb_typeof(v)<>'string' or char_length(v#>>'{}') not between 1 and 100)
  and not exists(select 1 from jsonb_array_elements(coalesce(d->'scenarios','[]')||coalesce(d->'not_for','[]')) v where jsonb_typeof(v)<>'string' or char_length(v#>>'{}') not between 1 and 60)
  and coalesce(jsonb_typeof(d->'scenarios'),'array')='array' and jsonb_array_length(coalesce(d->'scenarios','[]'))<=10
  and coalesce(jsonb_typeof(d->'not_for'),'array')='array' and jsonb_array_length(coalesce(d->'not_for','[]'))<=10
  and coalesce(jsonb_typeof(d->'ai_disclosure'),'object')='object'
  and not exists(select 1 from jsonb_object_keys(coalesce(d->'ai_disclosure','{}')) k where k not in ('statement','deflect_statement'))
  and char_length(coalesce(d->'ai_disclosure'->>'statement',''))<=300 and char_length(coalesce(d->'ai_disclosure'->>'deflect_statement',''))<=300
$$;
create function control.nexloop_operator_persona_rules_ok(r jsonb) returns boolean
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select jsonb_typeof(r)='array' and jsonb_array_length(r)<=50 and not exists(select 1 from jsonb_array_elements(r) x where not (
  jsonb_typeof(x)='object' and (select array_agg(k order by k) from jsonb_object_keys(x) k)=array['persona_id','when']
  and coalesce(x->>'persona_id','')~'^[a-z][a-z0-9_-]{0,63}$' and jsonb_typeof(x->'when')='object'
  and (((select array_agg(k order by k) from jsonb_object_keys(x->'when') k)=array['role_id'] and coalesce(x->'when'->>'role_id','')~'^[a-f0-9]{64}$')
   or ((select array_agg(k order by k) from jsonb_object_keys(x->'when') k)=array['equals','property'] and coalesce(x->'when'->>'property','')~'^[a-z][a-z0-9_]{0,63}$'
     and jsonb_typeof(x->'when'->'equals')='string' and char_length(x->'when'->>'equals') between 1 and 200))))
$$;
-- The highest version whose latest status is published.
create function runtime.nexloop_operator_persona_usable(p_tenant text,p_persona text) returns integer
 language sql stable set search_path=pg_catalog,pg_temp as $$
 select max(p.version) from control.nexloop_operator_personas p where p.tenant_id=p_tenant and p.persona_id=p_persona
  and (select e.status from control.nexloop_operator_persona_status_events e where e.tenant_id=p.tenant_id and e.persona_id=p.persona_id and e.version=p.version
       order by e.event_id desc limit 1)='published'
$$;
create function runtime.nexloop_operator_persona_desired(p_tenant text,p_world text,p_consumer text,out persona_id text,out source text,out rules_version integer)
 language plpgsql stable set search_path=pg_catalog,pg_temp as $$
declare a control.nexloop_operator_persona_assignments;r control.nexloop_operator_persona_rules;x jsonb;
begin
 select * into a from control.nexloop_operator_persona_assignments where tenant_id=p_tenant and world=p_world and consumer_id=p_consumer;
 if a.persona_id is not null and runtime.nexloop_operator_persona_usable(p_tenant,a.persona_id) is not null then
  persona_id:=a.persona_id;source:='owner_assign';return;end if;
 select * into r from control.nexloop_operator_persona_rules where tenant_id=p_tenant order by version desc limit 1;
 if not found then return;end if;
 rules_version:=r.version;
 for x in select value from jsonb_array_elements(r.rules) loop
  if runtime.nexloop_operator_persona_usable(p_tenant,x->>'persona_id') is not null and (
     (x->'when' ? 'role_id' and exists(select 1 from ontology.objects o where o.tenant_id=p_tenant and o.world=p_world and o.type_name='ConsumerRoleLink'
       and o.properties->>'consumer_id'=p_consumer and o.properties->>'role_id'=x->'when'->>'role_id' and (o.properties->>'active')::boolean
       and (o.properties->>'valid_from')::timestamptz<=clock_timestamp() and (o.properties->>'valid_until')::timestamptz>clock_timestamp()))
     or (x->'when' ? 'property' and exists(select 1 from ontology.objects o where o.tenant_id=p_tenant and o.world=p_world and o.type_name='Consumer'
       and o.object_id=p_consumer and o.properties->>(x->'when'->>'property')=x->'when'->>'equals'))) then
   persona_id:=x->>'persona_id';source:='rule';return;end if;
 end loop;
 if r.default_persona_id is not null and runtime.nexloop_operator_persona_usable(p_tenant,r.default_persona_id) is not null then
  persona_id:=r.default_persona_id;source:='default';end if;
end $$;
create function runtime.nexloop_ai_disclosure_setting(p_tenant text) returns jsonb
 language sql stable set search_path=pg_catalog,pg_temp as $$
 select coalesce((select jsonb_build_object('mode',mode,'when_asked',when_asked,'block_human_claims',block_human_claims,'content_label',content_label,'revision',revision)
   from control.nexloop_ai_disclosure where tenant_id=p_tenant),
  jsonb_build_object('mode','none','when_asked','deflect','block_human_claims',false,'content_label',false,'revision',0))
$$;
create function runtime.nexloop_operator_persona_section(p_tenant text,p_world text,p_consumer text) returns jsonb
 language plpgsql stable set search_path=pg_catalog,pg_temp as $$
declare d record;v integer;doc jsonb;
begin
 select * into d from runtime.nexloop_operator_persona_desired(p_tenant,p_world,p_consumer);
 if d.persona_id is null then return null;end if;
 v:=runtime.nexloop_operator_persona_usable(p_tenant,d.persona_id);
 select document into doc from control.nexloop_operator_personas where tenant_id=p_tenant and persona_id=d.persona_id and version=v;
 return jsonb_build_object('persona_ref','operator-persona:'||d.persona_id||'@'||v,'persona_id',d.persona_id,'version',v,'source',d.source,
  'document',doc,'ai_disclosure',runtime.nexloop_ai_disclosure_setting(p_tenant)-'revision');
end $$;
-- Settle a Consumer's assignment on the desired persona; an assignment event for every change (the first one too).
create function runtime.nexloop_operator_persona_settle(p_tenant text,p_world text,p_consumer text,p_by text,p_intent text,p_reason text,p_owner_source text default null) returns bigint
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare d record;cur control.nexloop_operator_persona_current;v_source text;v_event bigint;
begin
 select * into d from runtime.nexloop_operator_persona_desired(p_tenant,p_world,p_consumer);
 select * into cur from control.nexloop_operator_persona_current where tenant_id=p_tenant and world=p_world and consumer_id=p_consumer for update;
 if d.persona_id is null then return null;end if;
 if found and cur.persona_id=d.persona_id and cur.source=d.source then return cur.event_id;end if;
 v_source:=case when p_owner_source is not null then p_owner_source when not found then 'initial'
  when runtime.nexloop_operator_persona_usable(p_tenant,cur.persona_id) is null then 'persona_retired' else 'rule_change' end;
 insert into control.nexloop_operator_persona_assignment_events(tenant_id,world,consumer_id,previous,persona_id,source,rules_version,set_by,request_intent,reason)
  values(p_tenant,p_world,p_consumer,cur.persona_id,d.persona_id,v_source,d.rules_version,p_by,p_intent,p_reason) returning event_id into v_event;
 insert into control.nexloop_operator_persona_current(tenant_id,world,consumer_id,persona_id,source,rules_version,event_id)
  values(p_tenant,p_world,p_consumer,d.persona_id,d.source,d.rules_version,v_event)
  on conflict(tenant_id,world,consumer_id) do update set persona_id=excluded.persona_id,source=excluded.source,rules_version=excluded.rules_version,
   event_id=excluded.event_id,updated_at=clock_timestamp();
 return v_event;
end $$;

-- 3. Governed owner Actions -----------------------------------------------------------------------------------------------------
create function control.nexloop_governed_publish_operator_persona(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare v_id text:=body->>'persona_id';v_version integer;v_status text:=body->>'status';cur control.nexloop_operator_personas;v_hash text;v_last text;
begin
 if exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','persona_id','version','status','document','reason'))
  or coalesce(v_id,'')!~'^[a-z][a-z0-9_-]{0,63}$' or jsonb_typeof(body->'version') is distinct from 'number' or coalesce(v_status,'') not in ('published','retired')
  or length(coalesce(body->>'reason','')) not between 1 and 500 then raise exception 'operator persona: invalid' using errcode='22023';end if;
 v_version:=(body->>'version')::integer;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into cur from control.nexloop_operator_personas where tenant_id=p_tenant and persona_id=v_id and version=v_version for update;
 if v_status='published' then
  if not control.nexloop_operator_persona_document_ok(body->'document') then raise exception 'operator persona: document_invalid' using errcode='22023';end if;
  v_hash:=encode(sha256(convert_to(runtime.nexloop_canonical_json(body->'document'),'UTF8')),'hex');
  if found and cur.document_hash<>v_hash then raise exception 'operator persona: version_exists' using errcode='22023';end if;
  if not found then
   if v_version<>coalesce((select max(version) from control.nexloop_operator_personas where tenant_id=p_tenant and persona_id=v_id),0)+1 then
    raise exception 'operator persona: version_not_next' using errcode='22023';end if;
   insert into control.nexloop_operator_personas(tenant_id,persona_id,version,document,document_hash,created_by,request_intent)
    values(p_tenant,v_id,v_version,body->'document',v_hash,p_principal,p_intent);
  end if;
 else
  if body ? 'document' then raise exception 'operator persona: invalid' using errcode='22023';end if;
  if not found then raise exception 'operator persona: unknown_version' using errcode='22023';end if;
  v_hash:=cur.document_hash;
 end if;
 select status into v_last from control.nexloop_operator_persona_status_events where tenant_id=p_tenant and persona_id=v_id and version=v_version order by event_id desc limit 1;
 if v_last is distinct from v_status then
  insert into control.nexloop_operator_persona_status_events(tenant_id,persona_id,version,status,set_by,request_intent,reason)
   values(p_tenant,v_id,v_version,v_status,p_principal,p_intent,body->>'reason');
 end if;
 return jsonb_build_object('persona_ref','operator-persona:'||v_id||'@'||v_version,'status',v_status,'document_hash',v_hash,'changed',v_last is distinct from v_status);
end $$;

create function control.nexloop_governed_assign_operator_persona(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare v_consumer text:=body->>'consumer_id';v_persona text:=body->>'persona_id';cur control.nexloop_operator_persona_assignments;v_event bigint;
begin
 if exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','consumer_id','persona_id','reason'))
  or not body ? 'persona_id' or coalesce(v_consumer,'')!~'^[a-f0-9]{64}$' or (v_persona is not null and v_persona!~'^[a-z][a-z0-9_-]{0,63}$')
  or length(coalesce(body->>'reason','')) not between 1 and 500 then raise exception 'operator persona: invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 if not exists(select 1 from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='Consumer' and object_id=v_consumer) then
  raise exception 'consumer unavailable' using errcode='42501';end if;
 if v_persona is not null and runtime.nexloop_operator_persona_usable(p_tenant,v_persona) is null then
  raise exception 'operator persona: not_published' using errcode='22023';end if;
 select * into cur from control.nexloop_operator_persona_assignments where tenant_id=p_tenant and world=p_world and consumer_id=v_consumer for update;
 if found and cur.persona_id is not distinct from v_persona then
  return jsonb_build_object('consumer_id',v_consumer,'persona_id',v_persona,'revision',cur.revision,'changed',false);end if;
 if not found and v_persona is null then
  return jsonb_build_object('consumer_id',v_consumer,'persona_id',null,'revision',0,'changed',false);end if;
 insert into control.nexloop_operator_persona_assignments(tenant_id,world,consumer_id,persona_id,revision,assigned_by,request_intent)
  values(p_tenant,p_world,v_consumer,v_persona,1,p_principal,p_intent)
  on conflict(tenant_id,world,consumer_id) do update set persona_id=excluded.persona_id,revision=control.nexloop_operator_persona_assignments.revision+1,
   assigned_by=excluded.assigned_by,request_intent=excluded.request_intent,updated_at=clock_timestamp();
 v_event:=runtime.nexloop_operator_persona_settle(p_tenant,p_world,v_consumer,p_principal,p_intent,body->>'reason',case when v_persona is null then 'owner_clear' else 'owner_assign' end);
 return jsonb_build_object('consumer_id',v_consumer,'persona_id',v_persona,'changed',true,'assignment_event',v_event,
  'revision',(select revision from control.nexloop_operator_persona_assignments where tenant_id=p_tenant and world=p_world and consumer_id=v_consumer));
end $$;

create function control.nexloop_governed_publish_persona_rules(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare v_version integer;v_default text:=body->>'default_persona_id';cur control.nexloop_operator_persona_rules;x jsonb;
begin
 if exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','version','default_persona_id','rules','reason'))
  or not body ? 'default_persona_id' or jsonb_typeof(body->'version') is distinct from 'number' or not control.nexloop_operator_persona_rules_ok(body->'rules')
  or (v_default is not null and v_default!~'^[a-z][a-z0-9_-]{0,63}$') or length(coalesce(body->>'reason','')) not between 1 and 500 then
  raise exception 'operator persona: invalid' using errcode='22023';end if;
 v_version:=(body->>'version')::integer;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into cur from control.nexloop_operator_persona_rules where tenant_id=p_tenant and version=v_version;
 if found then
  if cur.rules=body->'rules' and cur.default_persona_id is not distinct from v_default then
   return jsonb_build_object('version',v_version,'changed',false);end if;
  raise exception 'operator persona: version_exists' using errcode='22023';end if;
 if v_version<>coalesce((select max(version) from control.nexloop_operator_persona_rules where tenant_id=p_tenant),0)+1 then
  raise exception 'operator persona: version_not_next' using errcode='22023';end if;
 for x in select value from jsonb_array_elements(body->'rules'||coalesce(case when v_default is null then null else jsonb_build_array(jsonb_build_object('persona_id',v_default)) end,'[]'::jsonb)) loop
  if runtime.nexloop_operator_persona_usable(p_tenant,x->>'persona_id') is null then raise exception 'operator persona: not_published' using errcode='22023';end if;
 end loop;
 insert into control.nexloop_operator_persona_rules(tenant_id,version,default_persona_id,rules,published_by,request_intent,reason)
  values(p_tenant,v_version,v_default,body->'rules',p_principal,p_intent,body->>'reason');
 return jsonb_build_object('version',v_version,'changed',true,'rules',jsonb_array_length(body->'rules'));
end $$;

create function control.nexloop_governed_set_ai_disclosure(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare cur control.nexloop_ai_disclosure;v_setting jsonb;v_revision bigint;
begin
 if exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','mode','when_asked','block_human_claims','content_label','reason'))
  or coalesce(body->>'mode','') not in ('none','first_contact','always') or coalesce(body->>'when_asked','') not in ('deflect','handoff')
  or jsonb_typeof(body->'block_human_claims') is distinct from 'boolean' or jsonb_typeof(body->'content_label') is distinct from 'boolean'
  or length(coalesce(body->>'reason','')) not between 1 and 500 then raise exception 'operator persona: invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 v_setting:=jsonb_build_object('mode',body->>'mode','when_asked',body->>'when_asked','block_human_claims',(body->>'block_human_claims')::boolean,'content_label',(body->>'content_label')::boolean);
 select * into cur from control.nexloop_ai_disclosure where tenant_id=p_tenant for update;
 if (runtime.nexloop_ai_disclosure_setting(p_tenant)-'revision')=v_setting then
  return v_setting||jsonb_build_object('revision',coalesce(cur.revision,0),'changed',false);end if;
 v_revision:=coalesce(cur.revision,0)+1;
 insert into control.nexloop_ai_disclosure(tenant_id,mode,when_asked,block_human_claims,content_label,revision,updated_by,request_intent)
  values(p_tenant,v_setting->>'mode',v_setting->>'when_asked',(v_setting->>'block_human_claims')::boolean,(v_setting->>'content_label')::boolean,v_revision,p_principal,p_intent)
  on conflict(tenant_id) do update set mode=excluded.mode,when_asked=excluded.when_asked,block_human_claims=excluded.block_human_claims,
   content_label=excluded.content_label,revision=excluded.revision,updated_by=excluded.updated_by,request_intent=excluded.request_intent,updated_at=clock_timestamp();
 insert into control.nexloop_ai_disclosure_events(tenant_id,revision,setting,set_by,request_intent,reason) values(p_tenant,v_revision,v_setting,p_principal,p_intent,body->>'reason');
 return v_setting||jsonb_build_object('revision',v_revision,'changed',true);
end $$;

do $owners$
declare f text;
begin
 foreach f in array array['control.nexloop_operator_persona_document_ok(jsonb)','control.nexloop_operator_persona_rules_ok(jsonb)',
   'runtime.nexloop_operator_persona_usable(text,text)','runtime.nexloop_operator_persona_desired(text,text,text)','runtime.nexloop_ai_disclosure_setting(text)',
   'runtime.nexloop_operator_persona_section(text,text,text)','runtime.nexloop_operator_persona_settle(text,text,text,text,text,text,text)',
   'control.nexloop_governed_publish_operator_persona(text,text,text,text,text,jsonb)','control.nexloop_governed_assign_operator_persona(text,text,text,text,text,jsonb)',
   'control.nexloop_governed_publish_persona_rules(text,text,text,text,text,jsonb)','control.nexloop_governed_set_ai_disclosure(text,text,text,text,text,jsonb)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';execute 'revoke all on function '||f||' from public';
 end loop;
end $owners$;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('operator_persona.publish','publish_operator_persona','human','control.nexloop_governed_publish_operator_persona(text,text,text,text,text,jsonb)','NX-023'),
 ('operator_persona.assign','assign_operator_persona','human','control.nexloop_governed_assign_operator_persona(text,text,text,text,text,jsonb)','NX-023'),
 ('operator_persona.rules_publish','publish_persona_rules','human','control.nexloop_governed_publish_persona_rules(text,text,text,text,text,jsonb)','NX-023'),
 ('ai_disclosure.set','set_ai_disclosure','human','control.nexloop_governed_set_ai_disclosure(text,text,text,text,text,jsonb)','NX-023');

-- 4. The Context assembler's read (service identity of the tenant; tenant configuration only, no customer data) -------------
create function authz.nexloop_context_operator_persona(p_digest text,p_world text,p_consumer text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;v_tenant text;
begin
 if session_user<>'nexloop_api' or p_world is distinct from 'real' or coalesce(p_consumer,'')!~'^[a-f0-9]{64}$' then
  raise exception 'operator persona unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ident->'binding'->>'subject_kind' is distinct from 'service' then raise exception 'operator persona unavailable' using errcode='42501';end if;
 v_tenant:=ident->'binding'->>'tenant_id';perform set_config('eios.tenant_id',v_tenant,true);
 return runtime.nexloop_operator_persona_section(v_tenant,p_world,p_consumer);
end $$;
alter function authz.nexloop_context_operator_persona(text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_context_operator_persona(text,text,text) from public;
grant execute on function authz.nexloop_context_operator_persona(text,text,text) to nexloop_api;

-- 5. The Context v6 bind (0187 body; changes marked 0221) ------------------------------------------------------------------
CREATE OR REPLACE FUNCTION authz.nexloop_context_v6_command(p_digest text, p_world text, p_text text, p_signature text, p_payload text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare a jsonb:=p_text::jsonb;p jsonb;k bytea;ident jsonb;tenant text;principal text;ca jsonb;cp jsonb;snapshot jsonb;pack jsonb;
 b runtime.nexloop_context_artifact_bindings;artifact runtime.nexloop_local_artifacts;cl runtime.nexloop_action_claims;
 conv runtime.nexloop_conversations;consumer text;proof jsonb;key text;fresh boolean;namespace text;context_uuid uuid;run uuid;rel boolean;core jsonb;opersona jsonb;
begin
 if session_user<>'nexloop_api' or p_world is distinct from 'real' or octet_length(p_text)>1048576 or octet_length(p_payload)>1048576
  or a->>'protocol' is distinct from 'nexloop-context-v6-v1' or a->>'resource_id' is distinct from 'eios:action:nexloop.context.assemble:1'
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds' then
  raise exception 'context v6 unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-context-v6-v1:'||p_text,'UTF8'),k,'sha256'),'hex') then
  raise exception 'context v6 unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_context_assemble(p_digest,p_world,a,p_payload);
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ident->'run_context' is distinct from 'null'::jsonb or ident->'binding'->>'subject_kind' is distinct from 'service' then raise exception 'context v6 unavailable' using errcode='42501';end if;
 tenant:=ident->'binding'->>'tenant_id';principal:=ident->'binding'->>'subject_principal_id';perform set_config('eios.tenant_id',tenant,true);
 p:=p_payload::jsonb;
 if p->>'verb' is distinct from 'bind' or jsonb_typeof(p->'core') is distinct from 'object' or jsonb_typeof(p->'omitted_items') is distinct from 'array'
  or jsonb_array_length(p->'omitted_items')>1024 or p->>'pack_text' is null or octet_length(p->>'pack_text')>65536 then raise exception 'context v6 unavailable' using errcode='42501';end if;
 ca:=(p->'core'->>'text')::jsonb;cp:=(p->'core'->>'payload')::jsonb;
 if cp->>'verb' is distinct from 'snapshot' then raise exception 'context v6 unavailable' using errcode='42501';end if;
 pack:=(p->>'pack_text')::jsonb;rel:=pack ? 'relationship_context';
 -- Core: the frozen v2 chain (or the v4 chain when the pack carries relationship assessments)
 -- re-derives and authorizes its sections under its own signed envelope.
 if rel then snapshot:=authz.nexloop_relationship_context_artifact_command(p_digest,p_world,p->'core'->>'text',p->'core'->>'signature',p->'core'->>'payload');
 else snapshot:=authz.nexloop_context_artifact_command(p_digest,p_world,p->'core'->>'text',p->'core'->>'signature',p->'core'->>'payload');end if;
 if p->>'pack_digest' is distinct from encode(sha256(convert_to(p->>'pack_text','UTF8')),'hex') or runtime.nexloop_canonical_json(pack) is distinct from p->>'pack_text'
  or pack->>'schema_version' is distinct from 'nexloop.context-pack.v6' or (select count(*) from jsonb_object_keys(pack))<>(case when rel then 19 else 18 end)+(case when pack ? 'effect_supplies' then 1 else 0 end)+(case when pack ? 'operator_persona' then 1 else 0 end)
  or not pack ?& array['strategy_ref','bindings','role','current_event','user_statement','goal','formal_facts','current_constraints','supply','constraints','consumer_state',
   'open_work','evidence','semantics','experience','budget_report','insufficient'] then raise exception 'context v6 pack invalid' using errcode='42501';end if;
 foreach key in array array['bindings','user_statement','formal_facts','current_constraints','supply','effect_supplies']||case when rel then array['relationship_context'] else array[]::text[] end loop
  if pack->key is distinct from snapshot->key then raise exception 'context v6 core mismatch' using errcode='42501';end if;
 end loop;
 if pack->'role' is distinct from 'null'::jsonb then raise exception 'context v6 Role section unsupported' using errcode='42501';end if;
 if pack->'current_event' is distinct from jsonb_build_object('kind','consumer_message','message_id',pack->'user_statement'->'message_id','provenance',pack->'user_statement'->'provenance') then
  raise exception 'context v6 current event mismatch' using errcode='42501';end if;
 context_uuid:=(pack->'bindings'->>'context_id')::uuid;run:=(pack->'bindings'->>'run_id')::uuid;
 select * into conv from runtime.nexloop_conversations where tenant_id=tenant and world=p_world and conversation_id=pack->'user_statement'->>'conversation_id' for share;
 select value->>'id' into consumer from jsonb_array_elements(pack->'formal_facts') where value->>'type'='Consumer';
 if not found or conv.consumer_id is distinct from consumer then raise exception 'context v6 consumer mismatch' using errcode='42501';end if;
 -- A bound pack replays unchanged: it was fully verified when bound (its read proofs have
 -- since expired); only the Artifact, the bind claim and the core above are rechecked.
 select * into b from runtime.nexloop_context_artifact_bindings where tenant_id=tenant and world=p_world and message_id=pack->'user_statement'->>'message_id' for update;
 fresh:=not found;
 if not fresh and (b.pack_text is distinct from p->>'pack_text' or b.run_id is distinct from run) then raise exception 'context artifact conflict' using errcode='23505';end if;
 if fresh then
  -- Core sources: what the frozen chain re-derived, cited under the envelope it verified;
  -- v4 relationship rows keep their v4 meaning (hypotheses are evidence only).
  core:=jsonb_build_array(
    jsonb_build_object('section','current_event','ref','eios:object:Message/'||(pack->'user_statement'->>'message_id'),'revision',pack->'user_statement'->>'sequence',
     'hash',runtime.nexloop_context_hash(pack->'user_statement'),'kind','current_message','decision',runtime.nexloop_context_hash(coalesce(ca->'message_read_envelope',ca->'trigger_message_envelope'))),
    jsonb_build_object('section','constraints','ref','eios:action:nexloop.service.request:1','revision',runtime.nexloop_context_hash(pack->'current_constraints'),
     'hash',runtime.nexloop_context_hash(pack->'current_constraints'),'kind','policy','decision',runtime.nexloop_context_hash(ca)),
    jsonb_build_object('section','constraints','ref','eios:object:'||(pack->'supply'->>'offering_id'),'revision',pack->'supply'->>'offering_revision',
     'hash',runtime.nexloop_context_hash(pack->'supply'),'kind','formal_object','decision',runtime.nexloop_context_hash(ca->'catalog_envelope')))
   ||coalesce((select jsonb_agg(jsonb_build_object('section','constraints','ref','eios:object:'||(f->>'type')||'/'||(f->>'id'),'revision',f->>'revision',
     'hash',runtime.nexloop_context_hash(f),'kind','formal_object','decision',runtime.nexloop_context_hash(ca))) from jsonb_array_elements(pack->'formal_facts') f),'[]'::jsonb)
   ||coalesce((select jsonb_agg(jsonb_build_object('section','constraints','ref','eios:object:'||(x->>'offering_id'),'revision',x->>'offering_revision',
     'hash',runtime.nexloop_context_hash(x),'kind','formal_object','decision',runtime.nexloop_context_hash(ca))) from jsonb_array_elements(coalesce(pack->'effect_supplies','[]'::jsonb)) x),'[]'::jsonb)
   ||case when rel then coalesce((select jsonb_agg(jsonb_build_object('section','evidence','ref',x->>'assessment_ref','revision',x->>'revision','hash',runtime.nexloop_context_hash(x),
     'kind',x->>'epistemic_kind','decision',runtime.nexloop_context_hash(ca->'relationship_envelopes')))
     from jsonb_array_elements((pack->'relationship_context'->'current_statements')||(pack->'relationship_context'->'evidence')) x),'[]'::jsonb) else '[]'::jsonb end;
  -- 0221: the operator persona section is exactly the re-derived one, present only when the tenant has a usable persona for this
  -- Consumer; it is cited as a role source (persona@version) and the Consumer's assignment is settled (an event on any change).
  opersona:=runtime.nexloop_operator_persona_section(tenant,p_world,consumer);
  if (pack ? 'operator_persona') is distinct from (opersona is not null) or (opersona is not null and pack->'operator_persona' is distinct from opersona) then
   raise exception 'context v6 operator persona mismatch' using errcode='42501';end if;
  if opersona is not null then
   core:=core||jsonb_build_array(jsonb_build_object('section','role','ref',opersona->>'persona_ref','revision',opersona->>'version',
    'hash',runtime.nexloop_context_hash(opersona),'kind','policy','decision',runtime.nexloop_context_hash(ca)));
   perform runtime.nexloop_operator_persona_settle(tenant,p_world,consumer,principal,'context:'||context_uuid::text,null);
  end if;
  perform authz.nexloop_context_v6_sections(p_digest,p_world,tenant,principal,a,p,pack,array[conv.conversation_id],consumer,run,context_uuid,core);
 end if;
 -- Artifact and governed bind claim: same rules as the frozen bind, against the v6 bytes.
 namespace:=encode(sha256(convert_to(tenant,'UTF8')||decode('00','hex')||convert_to(p_world,'UTF8')),'hex');
 select * into artifact from runtime.nexloop_local_artifacts where tenant_id=tenant and world=p_world and artifact_id=p->>'artifact_id' for share;
 if not found or artifact.artifact_id is distinct from pack->'bindings'->>'artifact_id' or artifact.status is distinct from 'available' or artifact.principal_id is distinct from principal
  or artifact.sha256 is distinct from p->>'pack_digest' or artifact.size_bytes is distinct from octet_length(p->>'pack_text')
  or artifact.media_type is distinct from 'application/vnd.nexloop.context+json' or artifact.retention_until<=clock_timestamp()
  or artifact.object_key is distinct from namespace||'/'||artifact.artifact_id then raise exception 'context artifact unavailable' using errcode='42501';end if;
 select * into cl from runtime.nexloop_action_claims where tenant_id=tenant and world=p_world and action_name='nexloop.context.bind' and intent_id=p->>'claim_id' for share;
 if not found or cl.principal_id is distinct from principal or cl.claim->'binding' is distinct from ca->'claim_binding' or cl.claim->>'state' not in ('active','terminal')
  or (cl.claim->>'state'='active' and (cl.claim->>'lease_expires_at')::timestamptz<=clock_timestamp())
  or (cl.claim->>'state'='terminal' and (fresh or cl.claim->'terminal_outcome'->>'status' is distinct from 'succeeded' or cl.claim->'terminal_outcome'->>'outcome_digest' is distinct from artifact.sha256)) then
  raise exception 'context artifact unavailable' using errcode='42501';end if;
 if fresh then
  insert into runtime.nexloop_context_artifact_bindings values(tenant,p_world,pack->'user_statement'->>'message_id',run,principal,p_digest,context_uuid,artifact.artifact_id,namespace,
   p->>'pack_text',p->>'pack_digest',authz.nexloop_context_command_binding(cp->'command'),clock_timestamp());
 elsif not exists(select 1 from runtime.nexloop_context_packs c where c.tenant_id=tenant and c.world=p_world and c.context_id=context_uuid and c.run_id=run and c.pack_digest=p->>'pack_digest') then
  raise exception 'context artifact conflict' using errcode='23505';
 end if;
 -- Tail: every authority used above is still current at return.
 for proof in select value from jsonb_array_elements(a->'read_proofs') loop
  if (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'context read proof expired' using errcode='42501';end if;
  perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
 end loop;
 perform authz.nexloop_assert_context_assemble(p_digest,p_world,a,p_payload);
 return jsonb_build_object('context_id',context_uuid,'run_id',run,'pack_digest',p->>'pack_digest','strategy_ref',pack->>'strategy_ref','bound',true);
end $function$;

-- 6. The owner's persona overview (workbench observe; the 0220 body kept for every other verb) ----------------------------
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) rename to nexloop_workbench_observe_read_before_operator_persona_v0221;
revoke all on function authz.nexloop_workbench_observe_read_before_operator_persona_v0221(text,text,text,text,text) from public,nexloop_api;
create function authz.nexloop_workbench_observe_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;w jsonb;cfg control.nexloop_workbench_configurations;v_tenant text;
 v_action text:='eios:action:nexloop.workbench.read:1';v_consumer text;
begin
 if c->>'verb' is distinct from 'operator_personas' then
  return authz.nexloop_workbench_observe_read_before_operator_persona_v0221(p_digest,p_world,p_text,p_signature,p_payload);end if;
 -- The 0133 verification, for this verb (as 0210/0220).
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
 v_consumer:=c->>'consumer_id';
 if (select count(*) from jsonb_object_keys(c))-(c ? 'consumer_id')::integer<>1 or (c ? 'consumer_id' and coalesce(v_consumer,'')!~'^[a-f0-9]{64}$') then
  raise exception 'workbench observe invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 return jsonb_build_object(
  'personas',coalesce((select jsonb_agg(jsonb_build_object('persona_id',x.persona_id,'version',x.version,'document_hash',x.document_hash,'created_at',x.created_at,
    'status',(select e.status from control.nexloop_operator_persona_status_events e where e.tenant_id=x.tenant_id and e.persona_id=x.persona_id and e.version=x.version order by e.event_id desc limit 1),
    'display_name',x.document->>'display_name') order by x.persona_id,x.version) from control.nexloop_operator_personas x where x.tenant_id=v_tenant),'[]'::jsonb),
  'rules',(select jsonb_build_object('version',r.version,'default_persona_id',r.default_persona_id,'rules',r.rules,'published_at',r.published_at)
    from control.nexloop_operator_persona_rules r where r.tenant_id=v_tenant order by r.version desc limit 1),
  'ai_disclosure',runtime.nexloop_ai_disclosure_setting(v_tenant),
  'consumer',case when v_consumer is null then null else jsonb_build_object('consumer_id',v_consumer,
    'desired',(select to_jsonb(d) from runtime.nexloop_operator_persona_desired(v_tenant,p_world,v_consumer) d),
    'settled',(select jsonb_build_object('persona_id',cur.persona_id,'source',cur.source,'rules_version',cur.rules_version,'event_id',cur.event_id,'updated_at',cur.updated_at)
      from control.nexloop_operator_persona_current cur where cur.tenant_id=v_tenant and cur.world=p_world and cur.consumer_id=v_consumer),
    'events',coalesce((select jsonb_agg(jsonb_build_object('event_id',e.event_id,'previous',e.previous,'persona_id',e.persona_id,'source',e.source,'rules_version',e.rules_version,
      'recorded_at',e.recorded_at) order by e.event_id) from control.nexloop_operator_persona_assignment_events e where e.tenant_id=v_tenant and e.world=p_world and e.consumer_id=v_consumer),'[]'::jsonb)) end);
end $$;
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_observe_read(text,text,text,text,text) from public;
grant execute on function authz.nexloop_workbench_observe_read(text,text,text,text,text) to nexloop_api;
