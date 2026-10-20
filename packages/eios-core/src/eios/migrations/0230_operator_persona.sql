-- Operator persona, slice 1 (temporary number 0230; design docs/design/operator-persona-design.md, owner decisions 2026-10-19:
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
 event_id bigint generated always as identity primary key,tenant_id text not null,world text not null,
 consumer_id text not null check(consumer_id~'^([a-f0-9]{64}|pseudonym:[0-9a-f]{32})$'),  -- pseudonym after the Consumer's erasure
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
-- Append-only, except what a registered Consumer erasure may change (NX-029 scope: pseudonym, cleared text).
create function control.nexloop_operator_persona_append_only() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if runtime.nexloop_erasure_permits(current_user::text,tg_table_schema||'.'||tg_table_name,tg_op,to_jsonb(old),case when tg_op='UPDATE' then to_jsonb(new) end) then
  if tg_op='DELETE' then return old;end if;
  return new;
 end if;
 raise exception 'operator persona records are append-only' using errcode='42501';
end $$;
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
 -- coalesce: a missing field makes the conjunction NULL; NULL is invalid (callers also test IS NOT TRUE).
 select coalesce(jsonb_typeof(d)='object' and octet_length(d::text)<=8192
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
  and coalesce(jsonb_typeof(d->'ai_disclosure'->'statement'),'string')='string' and coalesce(jsonb_typeof(d->'ai_disclosure'->'deflect_statement'),'string')='string'
  and coalesce(jsonb_typeof(d->'expression'->'vocabulary'),'string')='string' and coalesce(jsonb_typeof(d->'expression'->'language'),'string')='string',false)
$$;
create function control.nexloop_operator_persona_rules_ok(r jsonb) returns boolean
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select coalesce(jsonb_typeof(r)='array' and jsonb_array_length(r)<=50 and not exists(select 1 from jsonb_array_elements(r) x where not (
  jsonb_typeof(x)='object' and (select array_agg(k order by k) from jsonb_object_keys(x) k)=array['persona_id','when']
  and coalesce(x->>'persona_id','')~'^[a-z][a-z0-9_-]{0,63}$' and jsonb_typeof(x->'when')='object'
  and (((select array_agg(k order by k) from jsonb_object_keys(x->'when') k)=array['role_id'] and coalesce(x->'when'->>'role_id','')~'^[a-f0-9]{64}$')
   or ((select array_agg(k order by k) from jsonb_object_keys(x->'when') k)=array['equals','property'] and coalesce(x->'when'->>'property','')~'^[a-z][a-z0-9_]{0,63}$'
     and jsonb_typeof(x->'when'->'equals')='string' and char_length(x->'when'->>'equals') between 1 and 200)))),false)
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
-- Serializes everything that changes one Consumer's assignment (the owner's Action, a Context bind's settle): taken first, so the
-- desired persona is computed after any concurrent change has committed (Codex review, PR 13).
create function runtime.nexloop_operator_persona_lock(p_tenant text,p_world text,p_consumer text) returns void
 language sql set search_path=pg_catalog,pg_temp as $$
 select pg_advisory_xact_lock(hashtextextended('nexloop-operator-persona:'||p_tenant||'/'||p_world||'/'||p_consumer,0))
$$;
create function runtime.nexloop_operator_persona_settle(p_tenant text,p_world text,p_consumer text,p_by text,p_intent text,p_reason text,p_owner_source text default null) returns bigint
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare d record;cur control.nexloop_operator_persona_current;v_source text;v_event bigint;v_settled boolean;
begin
 perform runtime.nexloop_operator_persona_lock(p_tenant,p_world,p_consumer);
 select * into cur from control.nexloop_operator_persona_current where tenant_id=p_tenant and world=p_world and consumer_id=p_consumer for update;
 v_settled:=found;
 select * into d from runtime.nexloop_operator_persona_desired(p_tenant,p_world,p_consumer);
 if d.persona_id is null then return null;end if;
 if v_settled and cur.persona_id=d.persona_id and cur.source=d.source then return cur.event_id;end if;
 v_source:=case when p_owner_source is not null then p_owner_source when not v_settled then 'initial'
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
  if control.nexloop_operator_persona_document_ok(body->'document') is not true then raise exception 'operator persona: document_invalid' using errcode='22023';end if;
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
 perform runtime.nexloop_operator_persona_lock(p_tenant,p_world,v_consumer);
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
  or not body ? 'default_persona_id' or jsonb_typeof(body->'version') is distinct from 'number' or control.nexloop_operator_persona_rules_ok(body->'rules') is not true
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
   'runtime.nexloop_operator_persona_lock(text,text,text)',
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

-- 5. The Context v6 bind (0187 body; changes marked 0230) ------------------------------------------------------------------
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
  -- 0230: the operator persona section is exactly the re-derived one, present only when the tenant has a usable persona for this
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
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) rename to nexloop_workbench_observe_read_before_operator_persona_v0230;
revoke all on function authz.nexloop_workbench_observe_read_before_operator_persona_v0230(text,text,text,text,text) from public,nexloop_api;
create function authz.nexloop_workbench_observe_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;w jsonb;cfg control.nexloop_workbench_configurations;v_tenant text;
 v_action text:='eios:action:nexloop.workbench.read:1';v_consumer text;
begin
 if c->>'verb' is distinct from 'operator_personas' then
  return authz.nexloop_workbench_observe_read_before_operator_persona_v0230(p_digest,p_world,p_text,p_signature,p_payload);end if;
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


-- 7. Consumer erasure reaches the persona tables (Codex review, PR 13) -------------------------------------------------------
-- ADR-034's persona events (0220) take a pseudonym and empty request/reason under a registered erasure; nothing else may change.
alter table control.nexloop_consumer_persona_events drop constraint nexloop_consumer_persona_events_consumer_id_check,
 add constraint nexloop_consumer_persona_events_consumer_id_check check(consumer_id~'^([a-f0-9]{64}|pseudonym:[0-9a-f]{32})$');
alter table control.nexloop_consumer_persona_events drop constraint nexloop_consumer_persona_events_reason_check,
 add constraint nexloop_consumer_persona_events_reason_check check(char_length(reason) between 1 and 500 or (reason='' and consumer_id like 'pseudonym:%'));
create or replace function control.nexloop_consumer_persona_events_append_only() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if runtime.nexloop_erasure_permits(current_user::text,tg_table_schema||'.'||tg_table_name,tg_op,to_jsonb(old),case when tg_op='UPDATE' then to_jsonb(new) end) then
  if tg_op='DELETE' then return old;end if;
  return new;
 end if;
 raise exception 'consumer persona events are append-only' using errcode='42501';
end $$;
-- Source of the body below: 0145_at043_incentives.sql (the latest runtime.nexloop_erasure_step), copied unchanged except the
-- marked 0230 block in the consumer phase and its two counts (operator_persona_events, persona_events).
create or replace function runtime.nexloop_erasure_step(p_tenant text,p_world text,p_request text,p_batch integer,p_by text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare e control.nexloop_consumer_erasures;n2 integer:=0;n3 integer:=0;v_ref text;n integer:=0;m integer:=0;x record;v_phases text[]:=array['messages','claims','runs_text','effects','commercial',
 'commitments','incentives','plans','references','relay','runs_files','artifacts','consumer'];v_next text;v_pseudonym text;v_counts jsonb:='{}'::jsonb;k bytea;v_held boolean;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into e from control.nexloop_consumer_erasures where tenant_id=p_tenant and world=p_world and request_id=p_request for update skip locked;
 if not found or e.state in ('pending_confirmation','completed','completed_with_holds') then return jsonb_build_object('request_id',p_request,'state',e.state,'progress',false);end if;
 v_ref:='erasure:'||e.request_id;
 v_held:=runtime.nexloop_retention_held(p_tenant,p_world,'consumer',e.consumer_id);
 if e.state in ('blocking','settling','waiting_unknown_effects') then
  -- Unknown external results are reconciled first (0065); never resent blindly.
  if exists(select 1 from runtime.nexloop_effect_intents i where i.tenant_id=p_tenant and i.world=p_world and i.consumer_id=e.consumer_id
    and i.state in ('dispatching','unknown')) then
   update control.nexloop_consumer_erasures set state='waiting_unknown_effects',updated_at=clock_timestamp() where tenant_id=p_tenant and world=p_world and request_id=p_request;
   return jsonb_build_object('request_id',p_request,'state','waiting_unknown_effects','progress',false);
  end if;
  update control.nexloop_consumer_erasures set state='purging',phase='messages',updated_at=clock_timestamp() where tenant_id=p_tenant and world=p_world and request_id=p_request
   returning * into e;
 end if;
 if v_held then
  -- A Consumer hold keeps everything; the request completes with the hold recorded and nothing purged.
  perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,'consumer_erasure',v_ref,'consumer','consumer',e.consumer_id,'skipped_hold','{}'::jsonb,p_by);
  update control.nexloop_consumer_erasures set state='completed_with_holds',phase=null,completed_at=clock_timestamp(),updated_at=clock_timestamp()
   where tenant_id=p_tenant and world=p_world and request_id=p_request;
  return jsonb_build_object('request_id',p_request,'state','completed_with_holds','progress',true);
 end if;
 if e.phase='messages' then
  for x in select m.message_id from runtime.nexloop_conversation_messages m join runtime.nexloop_conversations c on c.tenant_id=m.tenant_id and c.world=m.world
    and c.conversation_id=m.conversation_id where m.tenant_id=p_tenant and m.world=p_world and c.consumer_id=e.consumer_id
    and (m.record->>'body'<>'' or exists(select 1 from ontology.objects o where o.tenant_id=m.tenant_id and o.world=m.world and o.type_name='Message' and o.object_id=m.message_id))
    order by m.conversation_id,m.sequence limit p_batch loop
   perform runtime.nexloop_erase_message(p_tenant,p_world,x.message_id,v_ref,'consumer_erasure',p_by);n:=n+1;
  end loop;
  v_counts:=jsonb_build_object('messages',n);
 elsif e.phase='claims' then
  with due as (select claim_id from ontology.nexloop_claims where tenant_id=p_tenant and world=p_world and consumer_id=e.consumer_id
    and not (value ? 'erased') limit p_batch for update skip locked)
  update ontology.nexloop_claims c set quote='',subject_text='',condition_text='',time_expression='',value='{"type":"erased","value":null,"erased":true}'::jsonb
   from due where c.tenant_id=p_tenant and c.world=p_world and c.claim_id=due.claim_id;
  get diagnostics n=row_count;v_counts:=jsonb_build_object('claims',n);
 elsif e.phase='runs_text' then
  perform runtime.nexloop_erasure_open('runtime.nexloop_model_request_prompts',array['request_text'],false,v_ref);
  update runtime.nexloop_model_request_prompts set request_text='' where tenant_id=p_tenant and world=p_world and request_text<>''
   and run_id in (select runtime.nexloop_consumer_runs(p_tenant,p_world,e.consumer_id));
  get diagnostics n=row_count;
  update runtime.jobs j set normalized_input=j.normalized_input||'{"input":"","input_erased":true}'::jsonb from authz.nexloop_runtime_run_bindings b
   where b.tenant_id=p_tenant and b.world=p_world and j.tenant_id=b.tenant_id and j.job_id=b.task_id and coalesce(j.normalized_input->>'input','')<>''
    and b.run_id in (select runtime.nexloop_consumer_runs(p_tenant,p_world,e.consumer_id));
  get diagnostics m=row_count;
  perform runtime.nexloop_erasure_close();
  v_counts:=jsonb_build_object('prompts',n,'jobs',m);n:=0;
 elsif e.phase='effects' then
  -- Frozen requests carry the text sent or requested; dispatching/unknown were reconciled before purging began.
  update runtime.nexloop_effect_intents set frozen_request='{"erased":true}'::jsonb where tenant_id=p_tenant and world=p_world
   and consumer_id=e.consumer_id and state not in ('dispatching','unknown') and not (frozen_request ? 'erased');
  get diagnostics m=row_count;v_counts:=jsonb_build_object('effect_requests',m);
 elsif e.phase='plans' then
  perform runtime.nexloop_erasure_open('runtime.nexloop_strategies',array['content'],false,v_ref);
  perform runtime.nexloop_erasure_open('runtime.nexloop_plan_steps',array['expected_result','prerequisites'],false,v_ref);
  perform runtime.nexloop_erasure_open('runtime.nexloop_plan_events',array['reason'],false,v_ref);
  perform runtime.nexloop_erasure_open('runtime.nexloop_plan_outcomes',array['outcome'],false,v_ref);
  update runtime.nexloop_strategies set content='' where tenant_id=p_tenant and world=p_world and consumer_id=e.consumer_id and content<>'';
  get diagnostics m=row_count;
  update runtime.nexloop_plan_steps s set expected_result='',prerequisites='[]'::jsonb from runtime.nexloop_plans p
   where p.tenant_id=p_tenant and p.world=p_world and p.consumer_id=e.consumer_id and s.tenant_id=p.tenant_id and s.world=p.world
    and s.plan_id=p.plan_id and s.version=p.version and (s.expected_result<>'' or s.prerequisites<>'[]'::jsonb);
  get diagnostics n2=row_count;
  -- Reason codes (snake_case) stay: they are the status chain; free text goes.
  update runtime.nexloop_plan_events v set reason='' where v.tenant_id=p_tenant and v.world=p_world and v.reason<>'' and v.reason!~'^[a-z][a-z0-9_]{0,63}$'
   and exists(select 1 from runtime.nexloop_plans p where p.tenant_id=v.tenant_id and p.world=v.world and p.plan_id=v.plan_id and p.consumer_id=e.consumer_id);
  get diagnostics n3=row_count;
  update runtime.nexloop_plan_outcomes o set outcome='{"erased":true}'::jsonb where o.tenant_id=p_tenant and o.world=p_world and not (o.outcome ? 'erased')
   and exists(select 1 from runtime.nexloop_plans p where p.tenant_id=o.tenant_id and p.world=o.world and p.plan_id=o.plan_id and p.consumer_id=e.consumer_id);
  get diagnostics n=row_count;
  perform runtime.nexloop_erasure_close();
  v_counts:=jsonb_build_object('strategies',m,'plan_steps',n2,'plan_reasons',n3,'plan_outcomes',n);n:=0;
 elsif e.phase='references' then
  -- Formal objects that name the Consumer: deleted with their events, relations and (assessments) revisions.
  perform runtime.nexloop_erasure_open('ontology.objects',null,true,v_ref);
  perform runtime.nexloop_erasure_open('ontology.nexloop_assessment_revisions',null,true,v_ref);
  for x in select o.type_name,o.object_id from ontology.objects o where o.tenant_id=p_tenant and o.world=p_world
    and o.type_name not in ('Consumer','Commitment','CommercialRecord','Message')
    and (o.properties->>'consumer_id'=e.consumer_id
     or (o.type_name='RelationshipAssessment' and (e.consumer_id in (o.properties->>'source_id',o.properties->>'target_id')
      or exists(select 1 from runtime.nexloop_conversation_messages cm join runtime.nexloop_conversations cv on cv.tenant_id=cm.tenant_id and cv.world=cm.world
        and cv.conversation_id=cm.conversation_id where cm.tenant_id=p_tenant and cm.world=p_world and cm.message_id=o.properties->>'evidence_message_id'
        and cv.consumer_id=e.consumer_id))))
    order by o.type_name,o.object_id limit p_batch loop
   delete from ontology.events where tenant_id=p_tenant and object_type=x.type_name and object_id=x.object_id;
   delete from ontology.relations where tenant_id=p_tenant and ((source_type=x.type_name and source_object_id=x.object_id) or (target_type=x.type_name and target_object_id=x.object_id));
   if x.type_name='RelationshipAssessment' then
    delete from ontology.nexloop_assessment_revisions where tenant_id=p_tenant and world=p_world and assessment_id=x.object_id;
   end if;
   delete from ontology.objects where tenant_id=p_tenant and world=p_world and type_name=x.type_name and object_id=x.object_id;
   perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,'consumer_erasure',v_ref,'formal_object','object',x.type_name||'/'||x.object_id,'deleted','{}'::jsonb,p_by);
   n:=n+1;v_counts:=v_counts||jsonb_build_object('objects:'||x.type_name,coalesce((v_counts->>('objects:'||x.type_name))::integer,0)+1);
  end loop;
  perform runtime.nexloop_erasure_close();
 elsif e.phase='relay' then
  -- Relay multi-consumer (0160–0163): the consumer's relay enrollments (withdrawn by the erasure itself: the relay already
  -- skipped it while erasing) are cleared with its rotation cursor and claim records; one tombstone, counts only.
  perform runtime.nexloop_erasure_open('control.nexloop_relay_enrollments',null,true,v_ref);
  delete from control.nexloop_relay_enrollments where tenant_id=p_tenant and world=p_world and consumer_id=e.consumer_id;
  get diagnostics n2=row_count;
  perform runtime.nexloop_erasure_close();
  delete from runtime.nexloop_relay_consumer_cursors where tenant_id=p_tenant and world=p_world and consumer_id=e.consumer_id;
  get diagnostics n3=row_count;
  delete from runtime.nexloop_relay_claims where tenant_id=p_tenant and world=p_world and consumer_id=e.consumer_id;
  get diagnostics m=row_count;
  perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,'consumer_erasure',v_ref,'relay_enrollment','control.nexloop_relay_enrollments',e.consumer_id,'deleted',
   jsonb_build_object('relay_enrollments',n2,'relay_cursors',n3,'relay_claims',m),p_by);
  v_counts:=jsonb_build_object('relay_enrollments',n2,'relay_cursors',n3,'relay_claims',m);n2:=0;n3:=0;m:=0;
 elsif e.phase='commercial' then
  v_counts:=runtime.nexloop_commercial_pseudonymize_consumer(p_tenant,p_world,e.consumer_id);
  perform set_config('eios.tenant_id',p_tenant,true);
  v_pseudonym:='pseudonym:'||encode(extensions.gen_random_bytes(16),'hex');
  perform runtime.nexloop_erasure_open('runtime.nexloop_cost_entries',array['consumer_ref'],false,v_ref);
  perform runtime.nexloop_erasure_open('control.nexloop_metric_cohorts',array['member_ref'],false,v_ref);
  update runtime.nexloop_cost_entries set consumer_ref=null where tenant_id=p_tenant and world=p_world and consumer_ref=e.consumer_id;
  get diagnostics n=row_count;
  update control.nexloop_metric_cohorts set member_ref=v_pseudonym where tenant_id=p_tenant and world=p_world and member_ref=e.consumer_id;
  get diagnostics m=row_count;
  perform runtime.nexloop_erasure_close();
  v_counts:=v_counts||jsonb_build_object('cost_entries',n,'cohort_members',m);n:=0;
 elsif e.phase='incentives' then
  -- AT-043: an issue whose provider answer is outstanding is settled first (queried, never re-sent); an accepted one is
  -- refused; then the rows keep amounts, states and ids but not the person: a random pseudonym for the Consumer, no
  -- notification text, no reply binding, no control snapshot (it names the Consumer).
  if exists(select 1 from runtime.nexloop_incentive_issues where tenant_id=p_tenant and world=p_world and consumer_id=e.consumer_id
    and state in ('dispatching','unknown')) then
   return jsonb_build_object('request_id',p_request,'state','purging','phase','incentives','progress',false,'waiting','incentive_results');
  end if;
  update runtime.nexloop_incentive_issues set state='refused',refusal_code='consumer_erasing',finished_at=clock_timestamp(),updated_at=clock_timestamp()
   where tenant_id=p_tenant and world=p_world and consumer_id=e.consumer_id and state='accepted';
  get diagnostics m=row_count;
  perform runtime.nexloop_erasure_open('runtime.nexloop_incentive_issues',array['consumer_id','notification_text','reply_to_message_id','control_snapshot'],false,v_ref);
  update runtime.nexloop_incentive_issues set consumer_id='pseudonym:'||encode(extensions.gen_random_bytes(16),'hex'),notification_text='',
   reply_to_message_id=null,control_snapshot='{"erased":true}'::jsonb where tenant_id=p_tenant and world=p_world and consumer_id=e.consumer_id;
  get diagnostics n2=row_count;
  perform runtime.nexloop_erasure_close();
  v_counts:=jsonb_build_object('incentive_issues',n2,'incentive_refused',m);
 elsif e.phase='commitments' then
  perform runtime.nexloop_erasure_open('ontology.objects',array['commitment_erasure'],false,v_ref);
  update ontology.objects set properties=properties||jsonb_build_object('made_to','erasure:'||e.request_id,'condition',null),updated_at=clock_timestamp()
   where tenant_id=p_tenant and world=p_world and type_name='Commitment' and properties->>'made_to'=e.consumer_id;
  get diagnostics n=row_count;
  perform runtime.nexloop_erasure_close();
  v_counts:=jsonb_build_object('commitments',n);n:=0;
 elsif e.phase='runs_files' then
  insert into runtime.nexloop_run_purge_items(tenant_id,world,run_id,request_ref,reason)
   select p_tenant,p_world,r,v_ref,'consumer_erasure' from runtime.nexloop_consumer_runs(p_tenant,p_world,e.consumer_id) r on conflict do nothing;
  get diagnostics m=row_count;v_counts:=jsonb_build_object('run_purges_queued',m);
 elsif e.phase='artifacts' then
  insert into runtime.nexloop_erasure_artifacts(tenant_id,world,artifact_id,request_id)
   select p_tenant,p_world,k2.artifact_id,e.request_id from runtime.nexloop_context_packs k2 where k2.tenant_id=p_tenant and k2.world=p_world
    and k2.run_id in (select runtime.nexloop_consumer_runs(p_tenant,p_world,e.consumer_id))
   union select p_tenant,p_world,q.prompt_artifact_id,e.request_id from runtime.nexloop_model_requests q where q.tenant_id=p_tenant and q.world=p_world
    and q.run_id in (select runtime.nexloop_consumer_runs(p_tenant,p_world,e.consumer_id))
   on conflict do nothing;
  get diagnostics m=row_count;v_counts:=jsonb_build_object('artifacts_queued',m);
 elsif e.phase='consumer' then
  -- Files first: the request completes only when its Run files and Artifacts are gone (a failed purge keeps it open).
  if exists(select 1 from runtime.nexloop_run_purge_items where tenant_id=p_tenant and world=p_world and request_ref=v_ref and state='pending')
   or exists(select 1 from runtime.nexloop_erasure_artifacts a join runtime.nexloop_local_artifacts l on l.tenant_id=a.tenant_id and l.world=a.world
     and l.artifact_id=a.artifact_id where a.tenant_id=p_tenant and a.world=p_world and a.request_id=e.request_id and l.status<>'deleted') then
   return jsonb_build_object('request_id',p_request,'state','purging','phase','consumer','progress',false,'waiting','files');
  end if;
  -- D5: an identity-free key per external reference (salted per tenant, owner side) keeps a re-imported identity uncontacted.
  insert into control.nexloop_erasure_salts(tenant_id,salt) values(p_tenant,extensions.gen_random_bytes(32)) on conflict do nothing;
  select salt into k from control.nexloop_erasure_salts where tenant_id=p_tenant;
  insert into control.nexloop_erased_contact_keys(tenant_id,world,key_digest,request_id)
   select p_tenant,p_world,encode(extensions.hmac(convert_to(ref.value#>>'{}','UTF8'),k,'sha256'),'hex'),e.request_id
   from ontology.objects o,jsonb_array_elements(case when jsonb_typeof(o.properties->'external_id_refs')='array' then o.properties->'external_id_refs' else '[]'::jsonb end) ref
   where o.tenant_id=p_tenant and o.world=p_world and o.type_name='Consumer' and o.object_id=e.consumer_id on conflict do nothing;
  get diagnostics m=row_count;
  -- 0230 (Codex review, PR 13): the operator persona assignment leaves with the Consumer: the per-Consumer rows are deleted, the
  -- append-only assignment events keep persona ids and times under a random pseudonym, without request or reason. ADR-034's
  -- persona marker (0220) likewise: the row goes, its events keep values and times under the same pseudonym, reason cleared.
  v_pseudonym:='pseudonym:'||encode(extensions.gen_random_bytes(16),'hex');
  delete from control.nexloop_operator_persona_assignments where tenant_id=p_tenant and world=p_world and consumer_id=e.consumer_id;
  delete from control.nexloop_operator_persona_current where tenant_id=p_tenant and world=p_world and consumer_id=e.consumer_id;
  perform runtime.nexloop_erasure_open('control.nexloop_operator_persona_assignment_events',array['consumer_id','request_intent','reason'],false,v_ref);
  update control.nexloop_operator_persona_assignment_events set consumer_id=v_pseudonym,request_intent=null,reason=null
   where tenant_id=p_tenant and world=p_world and consumer_id=e.consumer_id;
  get diagnostics n2=row_count;
  perform runtime.nexloop_erasure_close();
  delete from control.nexloop_consumer_personas where tenant_id=p_tenant and world=p_world and consumer_id=e.consumer_id;
  perform runtime.nexloop_erasure_open('control.nexloop_consumer_persona_events',array['consumer_id','request_intent','reason'],false,v_ref);
  update control.nexloop_consumer_persona_events set consumer_id=v_pseudonym,request_intent='',reason=''
   where tenant_id=p_tenant and world=p_world and consumer_id=e.consumer_id;
  get diagnostics n3=row_count;
  perform runtime.nexloop_erasure_close();
  delete from ontology.events where tenant_id=p_tenant and object_type='Consumer' and object_id=e.consumer_id;
  delete from ontology.relations where tenant_id=p_tenant and ((source_type='Consumer' and source_object_id=e.consumer_id) or (target_type='Consumer' and target_object_id=e.consumer_id));
  delete from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='Consumer' and object_id=e.consumer_id;
  get diagnostics n=row_count;
  perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,'consumer_erasure',v_ref,'consumer','consumer',e.consumer_id,'deleted',
   e.counts||jsonb_build_object('consumer_object',n,'contact_keys',m,'operator_persona_events',n2,'persona_events',n3),p_by);
  update control.nexloop_consumer_erasures set state='completed',phase=null,completed_at=clock_timestamp(),updated_at=clock_timestamp(),
   counts=counts||jsonb_build_object('consumer_object',n,'contact_keys',m,'operator_persona_events',n2,'persona_events',n3) where tenant_id=p_tenant and world=p_world and request_id=p_request;
  return jsonb_build_object('request_id',p_request,'state','completed','progress',true);
 end if;
 -- A phase is done when its batch was not full; then the next phase.
 v_next:=case when n>=p_batch then e.phase else v_phases[array_position(v_phases,e.phase)+1] end;
 update control.nexloop_consumer_erasures set phase=v_next,updated_at=clock_timestamp(),
  counts=counts||(select coalesce(jsonb_object_agg(key,coalesce((counts->>key)::bigint,0)+(value#>>'{}')::bigint),'{}'::jsonb) from jsonb_each(v_counts))
  where tenant_id=p_tenant and world=p_world and request_id=p_request;
 return jsonb_build_object('request_id',p_request,'state','purging','phase',v_next,'progress',true,'counts',v_counts);
end $$;
