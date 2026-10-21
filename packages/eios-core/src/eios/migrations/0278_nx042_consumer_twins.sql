-- NX-042 T1 (design §2, §3.3, D1, D4, D5, D7): consumer twin definitions and versions. The migration number lives only in the file
-- name and the catalog; nothing below repeats it.
-- 1. control.nexloop_consumer_twins: one row per published twin version (contract nexloop-consumer-twin/1): the hypotheses (statements,
--    never facts), the decision-layer parameters (every behaviour an interval at its coverage, D4: no single probability), the generator
--    version, the definition digest and the publisher's calibration report reference (informational only: a reference proves nothing).
--    A version is frozen: no update, no delete, no truncate (the frozen triggers rely on session_replication_role=replica being a
--    superuser setting). It holds no real content (D5: hypotheses and descriptions that look like an email, a phone or member number or
--    an identifier digest are refused) and has no world: it is kept for good and never leaves with a simulation world (NX-041 D2).
--    D4: every behaviour is an interval at least runtime.nexloop_twin_min_interval_width() wide - low = high would be a single probability.
-- 2. control.nexloop_consumer_twin_events: the version's status as appended events (published, then possibly retired with a reason);
--    retiring never changes the version row.
-- 2b. control.nexloop_consumer_twin_calibrations: the version's calibration as appended events (report reference, verdict passed or
--    failed, time). runtime.nexloop_consumer_twin_calibration derives the status from the latest event: calibrated only when it passed.
--    NX-042 T5 writes the events after checking that the report exists, passed and covers this version's digest; no writer exists
--    yet, so every version is uncalibrated. The version row never changes.
-- 3. runtime.nexloop_consumer_twin_selectable(tenant, twin, version): what NX-043 may select - published, not retired and calibrated.
-- 4. Governed owner Actions consumer_twin.publish / publish_consumer_twin and consumer_twin.retire / retire_consumer_twin (D7: owner only;
--    the server assigns the version and freezes the definition), and consumer_twin.erase_text / erase_consumer_twin_text (D5).
-- 5. D5 erasable text: the free text of a definition (description, hypothesis statements) lives in control.nexloop_consumer_twin_texts;
--    the frozen definition holds only 'sha256:<hex>' of each text, so the definition digest never changes. The owner erases a
--    version's texts through erase_consumer_twin_text: the text becomes unreadable, its hash and the version stay traceable.
--    runtime.nexloop_consumer_twin_definition gives the readable definition (null for an erased text). The regex stays the first guard.
-- 6. Generators are a controlled list (control.nexloop_twin_generators: whether a generator calls a model and its budget account);
--    a definition naming an unlisted generator is refused (generator_unknown). Only the deterministic twin-template/1 is listed.
--    Numbers of the frozen definition are normalised (trim_scale) so the digest does not depend on how a number was written.

-- 1. Versions --------------------------------------------------------------------------------------------------------------------------
create table control.nexloop_consumer_twins (
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 twin_id text not null check(twin_id~'^[a-z][a-z0-9_.-]{0,63}$'),
 version integer not null check(version>=1),
 definition jsonb not null check(jsonb_typeof(definition)='object'),
 definition_digest text not null check(definition_digest~'^[0-9a-f]{64}$'),
 generator_version text not null check(generator_version~'^[a-z][a-z0-9_.-]{0,39}/[0-9]+$'),
 calibration_report_ref text check(calibration_report_ref~'^twin-calibration:[0-9a-f]{64}$'),
 request_id text not null check(char_length(request_id) between 1 and 200),
 published_by text not null check(char_length(published_by) between 1 and 200),
 published_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,twin_id,version),
 unique(tenant_id,request_id));
create table control.nexloop_consumer_twin_events (
 event_id bigint generated always as identity primary key,
 tenant_id text not null,twin_id text not null,version integer not null,
 status text not null check(status in ('published','retired')),
 reason text check(char_length(reason) between 1 and 500),
 recorded_by text not null check(char_length(recorded_by) between 1 and 200),
 request_id text not null check(char_length(request_id) between 1 and 200),
 recorded_at timestamptz not null default clock_timestamp(),
 unique(tenant_id,request_id),
 check((status='retired')=(reason is not null)),
 foreign key(tenant_id,twin_id,version) references control.nexloop_consumer_twins(tenant_id,twin_id,version));
create index nexloop_consumer_twin_events_version on control.nexloop_consumer_twin_events(tenant_id,twin_id,version,event_id);
create table control.nexloop_consumer_twin_texts (
 tenant_id text not null,twin_id text not null,version integer not null,
 path text not null check(path='description' or path~'^hypotheses/[a-z][a-z0-9_]{0,63}$'),
 text_sha256 text not null check(text_sha256~'^[0-9a-f]{64}$'),
 text_body text check(char_length(text_body) between 1 and 500),
 erased_at timestamptz,erased_by text check(char_length(erased_by) between 1 and 200),
 erase_reason text check(char_length(erase_reason) between 1 and 500),erase_request_id text check(char_length(erase_request_id) between 1 and 200),
 primary key(tenant_id,twin_id,version,path),
 check((text_body is null)=(erased_at is not null) and (erased_at is null)=(erased_by is null) and (erased_at is null)=(erase_reason is null)
  and (erased_at is null)=(erase_request_id is null)),
 foreign key(tenant_id,twin_id,version) references control.nexloop_consumer_twins(tenant_id,twin_id,version));
create table control.nexloop_twin_generators (
 generator_version text primary key check(generator_version~'^[a-z][a-z0-9_.-]{0,39}/[0-9]+$'),
 uses_model boolean not null,
 budget_account text check(budget_account in ('simulation')),
 check(not uses_model or budget_account is not null));
alter table control.nexloop_twin_generators owner to nexloop_owner;
revoke all on control.nexloop_twin_generators from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_stand_in_worker,nexloop_twin_port,nexloop_sim_worker,nexloop_sim_guard,nexloop_sim_api;
-- The deterministic template generator only; a generator that calls a model is listed with its budget account by the slice adding it.
insert into control.nexloop_twin_generators(generator_version,uses_model) values('twin-template/1',false);
create table control.nexloop_consumer_twin_calibrations (
 event_id bigint generated always as identity primary key,
 tenant_id text not null,twin_id text not null,version integer not null,
 report_ref text not null check(report_ref~'^twin-calibration:[0-9a-f]{64}$'),
 verdict text not null check(verdict in ('passed','failed')),
 recorded_by text not null check(char_length(recorded_by) between 1 and 200),
 request_id text not null check(char_length(request_id) between 1 and 200),
 recorded_at timestamptz not null default clock_timestamp(),
 unique(tenant_id,request_id),
 foreign key(tenant_id,twin_id,version) references control.nexloop_consumer_twins(tenant_id,twin_id,version));
create index nexloop_consumer_twin_calibrations_version on control.nexloop_consumer_twin_calibrations(tenant_id,twin_id,version,event_id);
do $$declare t text;begin
 foreach t in array array['control.nexloop_consumer_twins','control.nexloop_consumer_twin_events','control.nexloop_consumer_twin_calibrations','control.nexloop_consumer_twin_texts'] loop
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy tenant_boundary on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on %s from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_stand_in_worker,nexloop_twin_port,nexloop_sim_worker,nexloop_sim_guard,nexloop_sim_api',t);
 end loop;
end $$;
-- Frozen: a version and its events are written once (D5: kept for good; no deletion path, not even a simulation world's).
create function control.nexloop_consumer_twin_frozen() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 raise exception '% is append-only (a twin version is frozen; retire it by a new status event)',tg_table_name using errcode='42501';
end $$;
alter function control.nexloop_consumer_twin_frozen() owner to nexloop_owner;
revoke all on function control.nexloop_consumer_twin_frozen() from public;
do $$declare t text;begin
 foreach t in array array['control.nexloop_consumer_twins','control.nexloop_consumer_twin_events','control.nexloop_consumer_twin_calibrations'] loop
  execute format('create trigger nx042_twin_frozen before update or delete on %s for each row execute function control.nexloop_consumer_twin_frozen()',t);
  execute format('create trigger nx042_twin_no_truncate before truncate on %s for each statement execute function control.nexloop_consumer_twin_frozen()',t);
 end loop;
end $$;
-- D5: a twin text is written once and may only be erased (text to null, with who, why and which request); never deleted.
create function control.nexloop_consumer_twin_text_guard() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if tg_op='UPDATE' and old.text_body is not null and new.text_body is null and new.erased_at is not null
  and (new.tenant_id,new.twin_id,new.version,new.path,new.text_sha256) is not distinct from (old.tenant_id,old.twin_id,old.version,old.path,old.text_sha256) then
  return new;end if;
 raise exception '% only erases (a twin text is kept as its hash)',tg_table_name using errcode='42501';
end $$;
alter function control.nexloop_consumer_twin_text_guard() owner to nexloop_owner;
revoke all on function control.nexloop_consumer_twin_text_guard() from public;
create trigger nx042_twin_text_guard before update or delete on control.nexloop_consumer_twin_texts for each row execute function control.nexloop_consumer_twin_text_guard();
create trigger nx042_twin_text_no_truncate before truncate on control.nexloop_consumer_twin_texts for each statement execute function control.nexloop_consumer_twin_frozen();

-- D4: the narrowest interval a behaviour may have (contract, SQL and Python agree; nexloop_eios.consumer_twins).
create function runtime.nexloop_twin_min_interval_width() returns numeric language sql immutable set search_path=pg_catalog,pg_temp as $$ select 0.01::numeric $$;
alter function runtime.nexloop_twin_min_interval_width() owner to nexloop_owner;
-- D5: free text that looks like real content - an email, a run of 7+ digits (phone, card, account), a member-style number (M-1001),
-- or a 32+ character hex identifier. A coarse guard, not a PII detector; the owner writes hypotheses, never people.
create function runtime.nexloop_twin_text_looks_real(p text) returns boolean language sql immutable set search_path=pg_catalog,pg_temp as $$
 select coalesce(p,'')~'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{2,}|[0-9]{7,}|[A-Za-z]+-[0-9]{3,}|[0-9A-Fa-f]{32,}'
$$;
alter function runtime.nexloop_twin_text_looks_real(text) owner to nexloop_owner;

-- D5: the frozen form of a valid definition - every free text replaced by 'sha256:<hex>', the numbers normalised (trim_scale).
create function runtime.nexloop_consumer_twin_redact(d jsonb) returns jsonb language sql immutable set search_path=pg_catalog,pg_temp as $$
 select d||(case when d ? 'description' then jsonb_build_object('description','sha256:'||encode(sha256(convert_to(d->>'description','UTF8')),'hex')) else '{}'::jsonb end)
  ||jsonb_build_object('hypotheses',(select jsonb_agg(h||jsonb_build_object('statement','sha256:'||encode(sha256(convert_to(h->>'statement','UTF8')),'hex')) order by i)
    from jsonb_array_elements(d->'hypotheses') with ordinality x(h,i)),
   'parameters',jsonb_build_object('behaviors',(select jsonb_object_agg(b.key,b.value||jsonb_build_object('low',trim_scale((b.value->>'low')::numeric),
     'high',trim_scale((b.value->>'high')::numeric),'coverage',trim_scale((b.value->>'coverage')::numeric))) from jsonb_each(d->'parameters'->'behaviors') b)))
$$;
alter function runtime.nexloop_consumer_twin_redact(jsonb) owner to nexloop_owner;

-- The definition's shape (contract nexloop-consumer-twin/1 without its frozen part): true when valid.
create function runtime.nexloop_consumer_twin_valid(d jsonb) returns boolean language sql immutable set search_path=pg_catalog,pg_temp as $$
 select jsonb_typeof(d)='object'
  and not exists(select 1 from jsonb_object_keys(d) k where k not in ('schema','twin_id','description','hypotheses','parameters','generator_version'))
  and d->>'schema' is not distinct from 'nexloop-consumer-twin/1'
  and coalesce(d->>'twin_id','')~'^[a-z][a-z0-9_.-]{0,63}$'
  and (not d ? 'description' or (jsonb_typeof(d->'description')='string' and char_length(d->>'description')<=500
   and not runtime.nexloop_twin_text_looks_real(d->>'description')))
  and coalesce(d->>'generator_version','')~'^[a-z][a-z0-9_.-]{0,39}/[0-9]+$'
  and jsonb_typeof(d->'hypotheses')='array' and jsonb_array_length(d->'hypotheses') between 1 and 32
  and not exists(select 1 from jsonb_array_elements(d->'hypotheses') h where jsonb_typeof(h)<>'object'
   or (select array_agg(k order by k) from jsonb_object_keys(h) k) is distinct from array['key','kind','statement']
   or coalesce(h->>'key','')!~'^[a-z][a-z0-9_]{0,63}$' or h->>'kind' is distinct from 'hypothesis'
   or jsonb_typeof(h->'statement')<>'string' or char_length(h->>'statement') not between 1 and 500
   or runtime.nexloop_twin_text_looks_real(h->>'statement'))
  and (select count(distinct h->>'key') from jsonb_array_elements(d->'hypotheses') h)=jsonb_array_length(d->'hypotheses')
  and jsonb_typeof(d->'parameters')='object'
  and (select array_agg(k) from jsonb_object_keys(d->'parameters') k)=array['behaviors']
  and jsonb_typeof(d->'parameters'->'behaviors')='object'
  and (select count(*) from jsonb_object_keys(d->'parameters'->'behaviors')) between 1 and 32
  and not exists(select 1 from jsonb_each(d->'parameters'->'behaviors') b where b.key!~'^[a-z][a-z0-9_]{0,63}$' or jsonb_typeof(b.value)<>'object'
   or (select array_agg(k order by k) from jsonb_object_keys(b.value) k) is distinct from array['coverage','high','hypotheses','low']
   or jsonb_typeof(b.value->'low')<>'number' or jsonb_typeof(b.value->'high')<>'number' or jsonb_typeof(b.value->'coverage')<>'number'
   or (b.value->>'low')::numeric<0 or (b.value->>'high')::numeric>1 or (b.value->>'high')::numeric-(b.value->>'low')::numeric<runtime.nexloop_twin_min_interval_width()
   or (b.value->>'coverage')::numeric<=0.5 or (b.value->>'coverage')::numeric>=1
   or jsonb_typeof(b.value->'hypotheses')<>'array' or jsonb_array_length(b.value->'hypotheses') not between 1 and 32
   -- every behaviour rests on hypotheses of this definition
   or exists(select 1 from jsonb_array_elements_text(b.value->'hypotheses') r
     where not exists(select 1 from jsonb_array_elements(d->'hypotheses') h where h->>'key'=r)))
$$;
alter function runtime.nexloop_consumer_twin_valid(jsonb) owner to nexloop_owner;
revoke all on function runtime.nexloop_consumer_twin_valid(jsonb) from public;

-- The current status of a version: its latest event.
-- The caller has set the tenant (eios.tenant_id): the table is tenant-bounded.
create function runtime.nexloop_consumer_twin_status(p_tenant text,p_twin text,p_version integer) returns text
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select e.status from control.nexloop_consumer_twin_events e where e.tenant_id=p_tenant and e.twin_id=p_twin and e.version=p_version
  order by e.event_id desc limit 1
$$;
alter function runtime.nexloop_consumer_twin_status(text,text,integer) owner to nexloop_owner;
revoke all on function runtime.nexloop_consumer_twin_status(text,text,integer) from public;

-- The calibration status of a version: calibrated only when its latest calibration event passed (D7; written by NX-042 T5).
create function runtime.nexloop_consumer_twin_calibration(p_tenant text,p_twin text,p_version integer) returns text
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);v text;
begin
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 select c.verdict into v from control.nexloop_consumer_twin_calibrations c where c.tenant_id=p_tenant and c.twin_id=p_twin and c.version=p_version
  order by c.event_id desc limit 1;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return case when v='passed' then 'calibrated' else 'uncalibrated' end;
end $$;
alter function runtime.nexloop_consumer_twin_calibration(text,text,integer) owner to nexloop_owner;
revoke all on function runtime.nexloop_consumer_twin_calibration(text,text,integer) from public;

-- D5: the readable definition of a version - each text from the side table, null once erased.
create function runtime.nexloop_consumer_twin_definition(p_tenant text,p_twin text,p_version integer) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);d jsonb;
begin
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 select t.definition into d from control.nexloop_consumer_twins t where t.tenant_id=p_tenant and t.twin_id=p_twin and t.version=p_version;
 if d is not null then
  if d ? 'description' then
   d:=jsonb_set(d,'{description}',coalesce(to_jsonb((select x.text_body from control.nexloop_consumer_twin_texts x where x.tenant_id=p_tenant and x.twin_id=p_twin
     and x.version=p_version and x.path='description')),'null'::jsonb));
  end if;
  d:=jsonb_set(d,'{hypotheses}',(select jsonb_agg(h||jsonb_build_object('statement',coalesce(to_jsonb((select x.text_body from control.nexloop_consumer_twin_texts x
     where x.tenant_id=p_tenant and x.twin_id=p_twin and x.version=p_version and x.path='hypotheses/'||(h->>'key'))),'null'::jsonb)) order by i)
   from jsonb_array_elements(d->'hypotheses') with ordinality y(h,i)));
 end if;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return d;
end $$;
alter function runtime.nexloop_consumer_twin_definition(text,text,integer) owner to nexloop_owner;
revoke all on function runtime.nexloop_consumer_twin_definition(text,text,integer) from public;

-- 3. What NX-043 may select: published, not retired, calibrated (D7).
create function runtime.nexloop_consumer_twin_selectable(p_tenant text,p_twin text,p_version integer) returns boolean
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);v boolean;
begin
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 select runtime.nexloop_consumer_twin_calibration(p_tenant,p_twin,p_version)='calibrated' and runtime.nexloop_consumer_twin_status(p_tenant,p_twin,p_version)='published' into v
  from control.nexloop_consumer_twins t where t.tenant_id=p_tenant and t.twin_id=p_twin and t.version=p_version;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return coalesce(v,false);
end $$;
alter function runtime.nexloop_consumer_twin_selectable(text,text,integer) owner to nexloop_owner;
revoke all on function runtime.nexloop_consumer_twin_selectable(text,text,integer) from public;

-- 4. The governed owner Actions ------------------------------------------------------------------------------------------------------
create function control.nexloop_governed_publish_consumer_twin(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare d jsonb:=body->'definition';v_ref text:=body->>'calibration_report_ref';t control.nexloop_consumer_twins;v_version integer;v_digest text;v_frozen jsonb;
begin
 if jsonb_typeof(body) is distinct from 'object' or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','definition','calibration_report_ref'))
  or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200
  or not runtime.nexloop_consumer_twin_valid(d)
  or (body ? 'calibration_report_ref' and jsonb_typeof(body->'calibration_report_ref') not in ('null','string'))
  or (v_ref is not null and v_ref!~'^twin-calibration:[0-9a-f]{64}$') then
  raise exception 'consumer twin: invalid' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'consumer twin: owner_world_only' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 if not exists(select 1 from control.nexloop_twin_generators g where g.generator_version=d->>'generator_version') then
  raise exception 'consumer twin: generator_unknown' using errcode='22023';end if;
 -- The digest is sha256 of the frozen form's jsonb text (texts as hashes, numbers normalised; nexloop_eios.consumer_twins mirrors it).
 v_frozen:=runtime.nexloop_consumer_twin_redact(d);
 v_digest:=encode(sha256(convert_to(v_frozen::text,'UTF8')),'hex');
 -- Serialise the request first so that a concurrent retry replays instead of hitting the unique key.
 perform pg_advisory_xact_lock(hashtextextended('nexloop:consumer-twin-request:'||p_tenant||':'||(body->>'request_id'),0));
 -- Idempotent per request: the same request gives the same version; the same request with another definition is refused.
 select * into t from control.nexloop_consumer_twins where tenant_id=p_tenant and request_id=body->>'request_id';
 if found then
  if t.definition_digest<>v_digest or t.twin_id<>d->>'twin_id' or t.calibration_report_ref is distinct from v_ref then
   raise exception 'consumer twin: request_reused' using errcode='22023';end if;
  return jsonb_build_object('twin_id',t.twin_id,'version',t.version,'definition_digest',t.definition_digest,
   'calibration',jsonb_build_object('report_ref',t.calibration_report_ref,'status',runtime.nexloop_consumer_twin_calibration(p_tenant,t.twin_id,t.version)),
   'status',runtime.nexloop_consumer_twin_status(p_tenant,t.twin_id,t.version),'published',false);
 end if;
 perform pg_advisory_xact_lock(hashtextextended('nexloop:consumer-twin:'||p_tenant||':'||(d->>'twin_id'),0));
 select coalesce(max(version),0)+1 into v_version from control.nexloop_consumer_twins where tenant_id=p_tenant and twin_id=d->>'twin_id';
 -- D7: a new version has no calibration event, so it is uncalibrated - with or without a report reference.
 insert into control.nexloop_consumer_twins(tenant_id,twin_id,version,definition,definition_digest,generator_version,calibration_report_ref,
   request_id,published_by)
  values(p_tenant,d->>'twin_id',v_version,v_frozen,v_digest,d->>'generator_version',v_ref,body->>'request_id',p_principal)
  returning * into t;
 -- D5: the texts, erasable; the frozen definition keeps their hashes.
 insert into control.nexloop_consumer_twin_texts(tenant_id,twin_id,version,path,text_sha256,text_body)
  select p_tenant,t.twin_id,t.version,'description',encode(sha256(convert_to(d->>'description','UTF8')),'hex'),d->>'description' where d ? 'description'
  union all
  select p_tenant,t.twin_id,t.version,'hypotheses/'||(h->>'key'),encode(sha256(convert_to(h->>'statement','UTF8')),'hex'),h->>'statement'
   from jsonb_array_elements(d->'hypotheses') h;
 insert into control.nexloop_consumer_twin_events(tenant_id,twin_id,version,status,recorded_by,request_id)
  values(p_tenant,t.twin_id,t.version,'published',p_principal,body->>'request_id');
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('consumer-twin:publish:'||t.twin_id||':'||t.version||':'||md5(p_tenant),'nexloop.consumer_twin.published',p_tenant,p_principal,'consumer_twin',p_intent,
   jsonb_build_object('twin_id',t.twin_id,'version',t.version,'definition_digest',t.definition_digest,'calibration_status','uncalibrated'),clock_timestamp());
 return jsonb_build_object('twin_id',t.twin_id,'version',t.version,'definition_digest',t.definition_digest,
  'calibration',jsonb_build_object('report_ref',t.calibration_report_ref,'status','uncalibrated'),'status','published','published',true);
end $$;
alter function control.nexloop_governed_publish_consumer_twin(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_governed_publish_consumer_twin(text,text,text,text,text,jsonb) from public;

create function control.nexloop_governed_retire_consumer_twin(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_status text;e control.nexloop_consumer_twin_events;
begin
 if jsonb_typeof(body) is distinct from 'object' or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','twin_id','version','reason'))
  or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200
  or coalesce(body->>'twin_id','')!~'^[a-z][a-z0-9_.-]{0,63}$' or jsonb_typeof(body->'version') is distinct from 'number'
  or (body->>'version')!~'^[0-9]{1,9}$' or (body->>'version')::integer<1
  or jsonb_typeof(body->'reason') is distinct from 'string' or char_length(btrim(body->>'reason')) not between 1 and 500 then
  raise exception 'consumer twin: invalid' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'consumer twin: owner_world_only' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into e from control.nexloop_consumer_twin_events where tenant_id=p_tenant and request_id=body->>'request_id';
 if found then
  if (e.twin_id,e.version,e.status) is distinct from (body->>'twin_id',(body->>'version')::integer,'retired') then
   raise exception 'consumer twin: request_reused' using errcode='22023';end if;
  return jsonb_build_object('twin_id',e.twin_id,'version',e.version,'status','retired','retired',false);
 end if;
 perform 1 from control.nexloop_consumer_twins where tenant_id=p_tenant and twin_id=body->>'twin_id' and version=(body->>'version')::integer for share;
 if not found then raise exception 'consumer twin: not_found' using errcode='22023';end if;
 v_status:=runtime.nexloop_consumer_twin_status(p_tenant,body->>'twin_id',(body->>'version')::integer);
 if v_status='retired' then raise exception 'consumer twin: already_retired' using errcode='22023';end if;
 insert into control.nexloop_consumer_twin_events(tenant_id,twin_id,version,status,reason,recorded_by,request_id)
  values(p_tenant,body->>'twin_id',(body->>'version')::integer,'retired',btrim(body->>'reason'),p_principal,body->>'request_id');
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('consumer-twin:retire:'||(body->>'twin_id')||':'||(body->>'version')||':'||md5(p_tenant),'nexloop.consumer_twin.retired',p_tenant,p_principal,'consumer_twin',p_intent,
   jsonb_build_object('twin_id',body->>'twin_id','version',(body->>'version')::integer),clock_timestamp());
 return jsonb_build_object('twin_id',body->>'twin_id','version',(body->>'version')::integer,'status','retired','retired',true);
end $$;
alter function control.nexloop_governed_retire_consumer_twin(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_governed_retire_consumer_twin(text,text,text,text,text,jsonb) from public;

-- D5: the owner erases a version's free texts (description, statements); the hashes, the frozen definition and its digest stay.
create function control.nexloop_governed_erase_consumer_twin_text(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_done integer;v_prior integer;
begin
 if jsonb_typeof(body) is distinct from 'object' or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','twin_id','version','reason'))
  or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200
  or coalesce(body->>'twin_id','')!~'^[a-z][a-z0-9_.-]{0,63}$' or jsonb_typeof(body->'version') is distinct from 'number'
  or (body->>'version')!~'^[0-9]{1,9}$' or (body->>'version')::integer<1
  or jsonb_typeof(body->'reason') is distinct from 'string' or char_length(btrim(body->>'reason')) not between 1 and 500 then
  raise exception 'consumer twin: invalid' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'consumer twin: owner_world_only' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform pg_advisory_xact_lock(hashtextextended('nexloop:consumer-twin-request:'||p_tenant||':'||(body->>'request_id'),0));
 -- A retried request replays (its texts are erased under this request id).
 select count(*) into v_prior from control.nexloop_consumer_twin_texts where tenant_id=p_tenant and erase_request_id=body->>'request_id';
 if v_prior>0 then
  if exists(select 1 from control.nexloop_consumer_twin_texts where tenant_id=p_tenant and erase_request_id=body->>'request_id'
    and (twin_id,version) is distinct from (body->>'twin_id',(body->>'version')::integer)) then
   raise exception 'consumer twin: request_reused' using errcode='22023';end if;
  return jsonb_build_object('twin_id',body->>'twin_id','version',(body->>'version')::integer,'erased',v_prior,'replayed',true);
 end if;
 perform 1 from control.nexloop_consumer_twins where tenant_id=p_tenant and twin_id=body->>'twin_id' and version=(body->>'version')::integer for share;
 if not found then raise exception 'consumer twin: not_found' using errcode='22023';end if;
 update control.nexloop_consumer_twin_texts set text_body=null,erased_at=clock_timestamp(),erased_by=p_principal,erase_reason=btrim(body->>'reason'),
   erase_request_id=body->>'request_id'
  where tenant_id=p_tenant and twin_id=body->>'twin_id' and version=(body->>'version')::integer and text_body is not null;
 get diagnostics v_done=row_count;
 if v_done=0 then raise exception 'consumer twin: already_erased' using errcode='22023';end if;
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('consumer-twin:erase-text:'||(body->>'twin_id')||':'||(body->>'version')||':'||md5(p_tenant||(body->>'request_id')),'nexloop.consumer_twin.text_erased',p_tenant,
   p_principal,'consumer_twin',p_intent,jsonb_build_object('twin_id',body->>'twin_id','version',(body->>'version')::integer,'erased',v_done),clock_timestamp());
 return jsonb_build_object('twin_id',body->>'twin_id','version',(body->>'version')::integer,'erased',v_done,'replayed',false);
end $$;
alter function control.nexloop_governed_erase_consumer_twin_text(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_governed_erase_consumer_twin_text(text,text,text,text,text,jsonb) from public;

insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('consumer_twin.publish','publish_consumer_twin','human','control.nexloop_governed_publish_consumer_twin(text,text,text,text,text,jsonb)','NX-042'),
 ('consumer_twin.retire','retire_consumer_twin','human','control.nexloop_governed_retire_consumer_twin(text,text,text,text,text,jsonb)','NX-042'),
 ('consumer_twin.erase_text','erase_consumer_twin_text','human','control.nexloop_governed_erase_consumer_twin_text(text,text,text,text,text,jsonb)','NX-042');
