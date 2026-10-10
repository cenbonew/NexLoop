-- NX-029 slice 1 (temporary number 0141): retention settings, governed erasure of append-only rows, tombstones and the
-- expiry sweeps of the retention keeper. docs/implementation/NX-029-design.md §3, §7, §8 (owner D1–D7, dispatcher D8/D9).
--
-- 1. control.nexloop_retention_settings: tenant-level (D9) versioned deployment configuration, seeded with the
--    canonical deploy/configuration/retention.v1.json; a derived class never outlives its source.
-- 2. Governed erasure (§8): append-only rows stay append-only for everyone. Only a transaction that registered the
--    table (and its columns) in control.nexloop_erasure_scope - a table only nexloop_owner can write - may set those
--    columns to an erasure value (null, '' or an object marked erased) or delete rows of a table registered for delete.
--    The shared append-only trigger functions and the two NX-027 commercial guards ask runtime.nexloop_erasure_permits
--    first (bodies replaced in place; triggers unchanged). Session settings are never trusted for this.
-- 3. runtime.nexloop_erasure_tombstones: what was erased, when, by which pass - never content, amounts or external ids.
-- 4. Sweeps (one bounded batch per call, skip-locked, idempotent): message text (stream records, relay outbox, Message
--    objects), Claim quotes, prompt text, job inputs of terminal jobs (redacted in place); commercial receipts, metric
--    observations, commercial records with their events and links, cost entries and settlements (deleted, NX-027 D6:
--    financial_retention_days). Retention never runs ahead of a class's own clock.
-- 5. Signed port authz.nexloop_retention_command (eios:action:nexloop.retention.execute:1; domain worker): due, sweep,
--    status. The keeper holds no table privilege and never sees content.
-- Published migrations are not edited.

-- 1. Settings ----------------------------------------------------------------------------------------------------------
create table control.nexloop_retention_settings (
 version integer primary key check(version>=1),definition jsonb not null check(jsonb_typeof(definition)='object'),
 definition_digest text not null check(definition_digest~'^[0-9a-f]{64}$'),published_by text not null,
 published_at timestamptz not null default clock_timestamp(),check((definition->>'version')::integer=version),
 check(definition->>'schema'='nexloop-retention/1' and jsonb_typeof(definition->'classes')='object' and jsonb_typeof(definition->'sweep')='object')
);
alter table control.nexloop_retention_settings owner to nexloop_owner;
create trigger nx029_append_only before update or delete on control.nexloop_retention_settings for each row execute function control.nexloop_nx022_append_only();
revoke all on control.nexloop_retention_settings from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;

-- Every class bounded; a class with a source never outlives it.
create function control.nexloop_retention_settings_check() returns trigger
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare k text;v jsonb;
begin
 for k,v in select * from jsonb_each(new.definition->'classes') loop
  if k not in ('message_text','claim_quote','prompt_text','job_input','commercial_receipt','metric_observation')
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
alter function control.nexloop_retention_settings_check() owner to nexloop_owner;
create trigger nx029_settings_check before insert on control.nexloop_retention_settings for each row execute function control.nexloop_retention_settings_check();

insert into control.nexloop_retention_settings(version,definition,definition_digest,published_by)
 values(1,'{"classes":{"claim_quote":{"days":90,"source":"message_text"},"commercial_receipt":{"days":90,"source":null},"job_input":{"days":30,"source":"message_text"},"message_text":{"days":90,"source":null},"metric_observation":{"days":365,"source":null},"prompt_text":{"days":30,"source":"message_text"}},"decision":"NX-029 slice 1 (design docs/implementation/NX-029-design.md §3, owner decisions D1–D7 ''all as recommended'' 2026-10-10, dispatcher D8/D9): tenant-level retention (D9) for the classes the retention keeper executes in slice 1. Product defaults, not legal periods (03:96, 13:31); enterprises adjust by a new version. A derived class never outlives its source (prompt text, Claim quotes and job inputs follow the message text). Financial records and cost entries use financial_retention_days of the commercial settings (NX-027 D6), not a second value here. Expired message text, Claim quotes, prompt text and job inputs are redacted in place (rows, digests and order stay); commercial receipts, metric observations, commercial records and cost entries are deleted; every pass leaves a tombstone without content.","schema":"nexloop-retention/1","sweep":{"batch":200,"interval_seconds":3600,"max_passes":20},"version":1}'::jsonb,'d3fe6bec7e2f62760f901aa6ea988dfd2c91bce976aaafa3165ccc0650a00cb3','deploy/configuration/retention.v1.json');

create function runtime.nexloop_retention_settings_current() returns jsonb
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select definition from control.nexloop_retention_settings order by version desc limit 1
$$;
alter function runtime.nexloop_retention_settings_current() owner to nexloop_owner;
revoke all on function runtime.nexloop_retention_settings_current() from public;

-- Days of a class: the retention settings, or the commercial settings for financial records (NX-027 D6).
create function runtime.nexloop_retention_days(p_class text) returns integer
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select case when p_class='financial' then (runtime.nexloop_commercial_settings_current()->>'financial_retention_days')::integer
  else (runtime.nexloop_retention_settings_current()->'classes'->p_class->>'days')::integer end
$$;
alter function runtime.nexloop_retention_days(text) owner to nexloop_owner;
revoke all on function runtime.nexloop_retention_days(text) from public;

-- 2. Governed erasure --------------------------------------------------------------------------------------------------
-- Transaction-local registrations without a tenant dimension (same kind as control.nexloop_commitment_settings): control
-- schema, owner-only, no application role access; rows live only inside the sweep transaction that wrote them.
create table control.nexloop_erasure_scope (
 txid bigint not null,table_name text not null check(table_name~'^[a-z_]+\.[a-z_]+$'),columns text[] not null default '{}',
 allow_delete boolean not null default false,request_ref text not null check(char_length(request_ref) between 1 and 200),
 primary key(txid,table_name)
);
alter table control.nexloop_erasure_scope owner to nexloop_owner;
revoke all on control.nexloop_erasure_scope from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;

create function runtime.nexloop_erasure_open(p_table text,p_columns text[],p_delete boolean,p_ref text) returns void
 language sql security definer set search_path=pg_catalog,pg_temp as $$
 insert into control.nexloop_erasure_scope(txid,table_name,columns,allow_delete,request_ref) values(txid_current(),p_table,coalesce(p_columns,'{}'),p_delete,p_ref)
 on conflict(txid,table_name) do update set columns=excluded.columns,allow_delete=excluded.allow_delete,request_ref=excluded.request_ref
$$;
create function runtime.nexloop_erasure_close() returns void
 language sql security definer set search_path=pg_catalog,pg_temp as $$ delete from control.nexloop_erasure_scope where txid=txid_current() $$;
-- Asked by the append-only triggers: true only for the owner inside a transaction that registered this table, and only
-- for a delete of a delete-registered table or an update that sets registered columns to an erasure value.
create function runtime.nexloop_erasure_permits(p_user text,p_table text,p_op text,p_old jsonb,p_new jsonb) returns boolean
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp as $$
declare s control.nexloop_erasure_scope;k text;
begin
 if p_user is distinct from 'nexloop_owner' then return false;end if;
 select * into s from control.nexloop_erasure_scope where txid=txid_current() and table_name=p_table;
 if not found then return false;end if;
 if p_op='DELETE' then return s.allow_delete;end if;
 if p_op<>'UPDATE' or p_new is null then return false;end if;
 for k in select key from jsonb_each(p_new) loop
  if p_old->k is distinct from p_new->k then
   if not (k=any(s.columns)) or not (p_new->k='null'::jsonb or p_new->k='""'::jsonb
     or (jsonb_typeof(p_new->k)='object' and p_new->k->>'erased'='true')) then return false;end if;
  end if;
 end loop;
 return true;
end $$;
do $owners$
declare f text;
begin
 foreach f in array array['runtime.nexloop_erasure_open(text,text[],boolean,text)','runtime.nexloop_erasure_close()'] loop
  execute 'alter function '||f||' owner to nexloop_owner';execute 'revoke all on function '||f||' from public';
 end loop;
end $owners$;
alter function runtime.nexloop_erasure_permits(text,text,text,jsonb,jsonb) owner to nexloop_owner;
-- Executable by any caller of the append-only triggers; it answers false unless the owner registered the transaction.
grant execute on function runtime.nexloop_erasure_permits(text,text,text,jsonb,jsonb) to public;

-- Shared append-only trigger bodies (same functions, same triggers): ask the erasure scope first.
create or replace function control.nexloop_nx022_append_only() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if runtime.nexloop_erasure_permits(current_user::text,tg_table_schema||'.'||tg_table_name,tg_op,to_jsonb(old),case when tg_op='UPDATE' then to_jsonb(new) end) then
  if tg_op='DELETE' then return old;end if;
  return new;
 end if;
 raise exception '% is append-only',tg_table_name using errcode='42501';
end $$;
create or replace function control.nexloop_context_append_only() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if runtime.nexloop_erasure_permits(current_user::text,tg_table_schema||'.'||tg_table_name,tg_op,to_jsonb(old),case when tg_op='UPDATE' then to_jsonb(new) end) then
  if tg_op='DELETE' then return old;end if;
  return new;
 end if;
 raise exception 'context records are append-only' using errcode='22023';
end $$;
-- NX-027 guards: events and CommercialRecord objects leave only by retention, i.e. through a registered erasure.
create or replace function runtime.nexloop_commercial_event_guard() returns trigger
 language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if tg_op='DELETE' then
  if runtime.nexloop_erasure_permits(current_user::text,'runtime.nexloop_commercial_events','DELETE',to_jsonb(old),null) then return old;end if;
  raise exception 'commercial events leave only by retention (NX-029)' using errcode='42501';
 end if;
 if (to_jsonb(new)-'disposition'-'recorded_at'-'customer_ref') is distinct from (to_jsonb(old)-'disposition'-'recorded_at'-'customer_ref')
  or (old.disposition is not null and (new.disposition is distinct from old.disposition or new.recorded_at is distinct from old.recorded_at))
  or (new.customer_ref is distinct from old.customer_ref and (new.customer_ref!~'^pseudonym:[0-9a-f]{32}$' or old.customer_ref~'^pseudonym:'))
 then raise exception 'commercial event is immutable' using errcode='42501';end if;
 return new;
end $$;
create or replace function ontology.nexloop_commercial_record_guard() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);r runtime.nexloop_commercial_records;v jsonb;
begin
 if tg_op='DELETE' then
  -- Definer function: the owner identity is ours; the registered erasure scope is the gate.
  if old.type_name='CommercialRecord' and not runtime.nexloop_erasure_permits('nexloop_owner','ontology.objects','DELETE',to_jsonb(old),null) then
   raise exception 'a CommercialRecord leaves only by retention (NX-029)' using errcode='42501';end if;
  return old;
 end if;
 if new.type_name<>'CommercialRecord' and (tg_op='INSERT' or old.type_name<>'CommercialRecord') then return new;end if;
 if tg_op='UPDATE' and (old.type_name<>'CommercialRecord' or new.type_name<>'CommercialRecord' or new.object_id<>old.object_id
   or new.tenant_id<>old.tenant_id or new.world<>old.world) then raise exception 'commercial record identity is immutable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',new.tenant_id,true);
 select * into r from runtime.nexloop_commercial_records c where c.tenant_id=new.tenant_id and c.world=new.world and c.object_id=new.object_id;
 if not found or (tg_op='INSERT' and r.intent_id is distinct from new.source_ref) then
  raise exception 'a CommercialRecord comes only from verified commercial events' using errcode='42501';end if;
 v:=runtime.nexloop_commercial_target(new.tenant_id,new.world,r.record_key);
 if v is null or new.properties is distinct from v->'properties' then
  raise exception 'commercial record state is not derived from its verified events' using errcode='23514';end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return new;
end $$;

-- 3. Tombstones and sweep bookkeeping ------------------------------------------------------------------------------------
create table runtime.nexloop_erasure_tombstones (
 tenant_id text not null,world text not null,tombstone_id text not null check(tombstone_id~'^[0-9a-f]{64}$'),
 reason text not null check(reason in ('retention_expiry','consumer_erasure','message_erasure','correction_expiry')),
 request_ref text not null check(char_length(request_ref) between 1 and 200),
 item_class text not null check(item_class~'^[a-z_]{1,64}$'),
 subject_kind text not null check(subject_kind~'^[a-z_.]{1,128}$'),subject_key text not null check(char_length(subject_key) between 1 and 200),
 action text not null check(action in ('redacted','deleted','pseudonymized','skipped_hold')),
 counts jsonb not null default '{}'::jsonb check(jsonb_typeof(counts)='object'),
 executed_at timestamptz not null default clock_timestamp(),executed_by text not null,
 primary key(tenant_id,world,tombstone_id)
);
create index nexloop_erasure_tombstones_class on runtime.nexloop_erasure_tombstones(tenant_id,world,item_class,executed_at);
create table runtime.nexloop_retention_sweeps (
 tenant_id text not null,world text not null,item_class text not null,last_started_at timestamptz,last_completed_at timestamptz,
 last_counts jsonb not null default '{}'::jsonb,primary key(tenant_id,world,item_class)
);
do $tables$
declare t text;
begin
 foreach t in array array['runtime.nexloop_erasure_tombstones','runtime.nexloop_retention_sweeps'] loop
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy tenant_boundary on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on %s from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator',t);
 end loop;
end $tables$;
create trigger nx029_append_only before update or delete on runtime.nexloop_erasure_tombstones for each row execute function control.nexloop_nx022_append_only();

create function runtime.nexloop_erasure_tombstone(p_tenant text,p_world text,p_reason text,p_ref text,p_class text,p_kind text,p_key text,p_action text,
  p_counts jsonb,p_by text) returns void
 language sql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 insert into runtime.nexloop_erasure_tombstones(tenant_id,world,tombstone_id,reason,request_ref,item_class,subject_kind,subject_key,action,counts,executed_by)
 values(p_tenant,p_world,encode(sha256(convert_to(jsonb_build_array(p_ref,p_class,p_kind,p_key)::text,'UTF8')),'hex'),p_reason,p_ref,p_class,p_kind,p_key,p_action,
  p_counts,p_by) on conflict do nothing
$$;
alter function runtime.nexloop_erasure_tombstone(text,text,text,text,text,text,text,text,jsonb,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_erasure_tombstone(text,text,text,text,text,text,text,text,jsonb,text) from public;

-- 4. Sweeps (one batch; returns {processed, more}); the caller sets the tenant and closes the erasure scope ---------------
create function runtime.nexloop_retention_sweep(p_tenant text,p_world text,p_class text,p_batch integer,p_ref text,p_by text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_cut timestamptz:=clock_timestamp()-make_interval(days=>runtime.nexloop_retention_days(p_class));n integer:=0;m integer:=0;o integer:=0;
 x record;v_counts jsonb:='{}'::jsonb;v_ids text[];v_keys text[];
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 if v_cut is null then raise exception 'retention class unavailable' using errcode='22023';end if;
 if p_class='message_text' then
  -- Stream records, the relay outbox copy and the Message object: body becomes '' (the record says erased).
  with due as (select conversation_id,sequence,message_id from runtime.nexloop_conversation_messages
    where tenant_id=p_tenant and world=p_world and not (record ? 'erased') and (record->>'accepted_at')::timestamptz<v_cut
    order by (record->>'accepted_at')::timestamptz,conversation_id,sequence limit p_batch for update skip locked),
   done as (update runtime.nexloop_conversation_messages m set record=m.record||'{"body":"","erased":true}'::jsonb from due
    where m.tenant_id=p_tenant and m.world=p_world and m.conversation_id=due.conversation_id and m.sequence=due.sequence returning m.message_id)
  select array_agg(message_id) into v_ids from done;
  n:=coalesce(cardinality(v_ids),0);
  if n>0 then
   update runtime.nexloop_message_outbox set record=record||'{"body":"","erased":true}'::jsonb
    where tenant_id=p_tenant and world=p_world and message_id=any(v_ids) and not (record ? 'erased');
   get diagnostics m=row_count;
   update ontology.objects set properties=properties||'{"body":""}'::jsonb,updated_at=clock_timestamp()
    where tenant_id=p_tenant and world=p_world and type_name='Message' and object_id=any(v_ids) and properties->>'body'<>'';
   get diagnostics o=row_count;
   v_counts:=jsonb_build_object('messages',n,'outbox',m,'message_objects',o);
  end if;
 elsif p_class='claim_quote' then
  -- A Claim's quote follows its source message (D2); structured values stay.
  with due as (select c.claim_id from ontology.nexloop_claims c join runtime.nexloop_conversation_messages s
     on s.tenant_id=c.tenant_id and s.world=c.world and s.message_id=c.source_message_id
    where c.tenant_id=p_tenant and c.world=p_world and c.quote<>'' and (s.record ? 'erased' or (s.record->>'accepted_at')::timestamptz<v_cut)
    limit p_batch for update of c skip locked)
  update ontology.nexloop_claims c set quote='' from due where c.tenant_id=p_tenant and c.world=p_world and c.claim_id=due.claim_id;
  get diagnostics n=row_count;v_counts:=jsonb_build_object('claims',n);
 elsif p_class='prompt_text' then
  perform runtime.nexloop_erasure_open('runtime.nexloop_model_request_prompts',array['request_text'],false,p_ref);
  with due as (select p.run_id,p.call_sequence from runtime.nexloop_model_request_prompts p join runtime.nexloop_model_requests r
     on r.tenant_id=p.tenant_id and r.world=p.world and r.run_id=p.run_id and r.call_sequence=p.call_sequence
    where p.tenant_id=p_tenant and p.world=p_world and p.request_text<>'' and r.requested_at<v_cut
    order by r.requested_at limit p_batch for update of p skip locked)
  update runtime.nexloop_model_request_prompts p set request_text='' from due
   where p.tenant_id=p_tenant and p.world=p_world and p.run_id=due.run_id and p.call_sequence=due.call_sequence;
  get diagnostics n=row_count;v_counts:=jsonb_build_object('prompts',n);
 elsif p_class='job_input' then
  -- Terminal jobs only: the input text the Run received (its digests and result stay).
  with due as (select job_id from runtime.jobs where tenant_id=p_tenant and status in ('succeeded','failed','dead_lettered','cancelled')
    and updated_at<v_cut and coalesce(normalized_input->>'input','')<>'' order by updated_at limit p_batch for update skip locked)
  update runtime.jobs j set normalized_input=j.normalized_input||'{"input":"","input_erased":true}'::jsonb from due where j.job_id=due.job_id;
  get diagnostics n=row_count;v_counts:=jsonb_build_object('jobs',n);
 elsif p_class='commercial_receipt' then
  perform runtime.nexloop_erasure_open('runtime.nexloop_commercial_receipts',null,true,p_ref);
  with due as (select receipt_number from runtime.nexloop_commercial_receipts where tenant_id=p_tenant and world=p_world and received_at<v_cut
    order by receipt_number limit p_batch for update skip locked)
  delete from runtime.nexloop_commercial_receipts r using due where r.receipt_number=due.receipt_number;
  get diagnostics n=row_count;v_counts:=jsonb_build_object('receipts',n);
 elsif p_class='metric_observation' then
  perform runtime.nexloop_erasure_open('control.nexloop_metric_observations',null,true,p_ref);
  with due as (select metric_id,metric_version,observation_id from control.nexloop_metric_observations where tenant_id=p_tenant and world=p_world
    and occurred_at<v_cut order by occurred_at limit p_batch for update skip locked)
  delete from control.nexloop_metric_observations o using due where o.tenant_id=p_tenant and o.world=p_world and o.metric_id=due.metric_id
   and o.metric_version=due.metric_version and o.observation_id=due.observation_id;
  get diagnostics n=row_count;v_counts:=jsonb_build_object('observations',n);
 elsif p_class='financial' then
  -- NX-027 D6: a record whose last verified event is older than financial_retention_days leaves with its events, links
  -- and exceptions; one tombstone per record (no amount, no external id). Cost entries and settlements likewise.
  perform runtime.nexloop_erasure_open('ontology.objects',null,true,p_ref);
  perform runtime.nexloop_erasure_open('runtime.nexloop_commercial_events',null,true,p_ref);
  perform runtime.nexloop_erasure_open('runtime.nexloop_commercial_records',null,true,p_ref);
  perform runtime.nexloop_erasure_open('runtime.nexloop_commercial_exceptions',null,true,p_ref);
  perform runtime.nexloop_erasure_open('runtime.nexloop_commercial_wakes',null,true,p_ref);
  perform runtime.nexloop_erasure_open('runtime.nexloop_commercial_commitment_bindings',null,true,p_ref);
  perform runtime.nexloop_erasure_open('runtime.nexloop_cost_entries',null,true,p_ref);
  perform runtime.nexloop_erasure_open('runtime.nexloop_budget_settlements',null,true,p_ref);
  for x in select r.record_key,r.object_id from runtime.nexloop_commercial_records r where r.tenant_id=p_tenant and r.world=p_world
    and not exists(select 1 from runtime.nexloop_commercial_events e where e.tenant_id=r.tenant_id and e.world=r.world and e.record_key=r.record_key
     and greatest(e.occurred_at,e.received_at)>=v_cut)
    order by r.record_key limit p_batch for update of r skip locked loop
   delete from runtime.nexloop_commercial_events where tenant_id=p_tenant and world=p_world and record_key=x.record_key;
   get diagnostics m=row_count;
   delete from ontology.events where tenant_id=p_tenant and object_type='CommercialRecord' and object_id=x.object_id;
   delete from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='CommercialRecord' and object_id=x.object_id;
   delete from runtime.nexloop_commercial_exceptions where tenant_id=p_tenant and world=p_world and subject_ref='record:'||x.record_key;
   delete from runtime.nexloop_commercial_wakes where tenant_id=p_tenant and world=p_world and record_key=x.record_key;
   delete from runtime.nexloop_commercial_commitment_bindings where tenant_id=p_tenant and world=p_world and record_key=x.record_key;
   delete from runtime.nexloop_commercial_records where tenant_id=p_tenant and world=p_world and record_key=x.record_key;
   perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,'retention_expiry',p_ref,'financial','commercial_record',x.object_id,'deleted',
    jsonb_build_object('events',m),p_by);
   n:=n+1;
  end loop;
  with due as (select entry_id from runtime.nexloop_cost_entries where tenant_id=p_tenant and world=p_world and occurred_at<v_cut
    order by occurred_at,entry_id limit p_batch for update skip locked)
  delete from runtime.nexloop_cost_entries c using due where c.tenant_id=p_tenant and c.world=p_world and c.entry_id=due.entry_id;
  get diagnostics m=row_count;
  with due as (select consumption_id from runtime.nexloop_budget_settlements where tenant_id=p_tenant and world=p_world and settled_at<v_cut
    order by settled_at limit p_batch for update skip locked)
  delete from runtime.nexloop_budget_settlements s using due where s.tenant_id=p_tenant and s.world=p_world and s.consumption_id=due.consumption_id;
  get diagnostics o=row_count;
  v_counts:=jsonb_build_object('records',n,'cost_entries',m,'settlements',o);
  n:=n+m+o;
 else
  raise exception 'retention class unavailable' using errcode='22023';
 end if;
 perform runtime.nexloop_erasure_close();
 if n>0 and p_class<>'financial' then
  perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,'retention_expiry',p_ref,p_class,'batch',p_class,
   case when p_class in ('commercial_receipt','metric_observation') then 'deleted' else 'redacted' end,v_counts,p_by);
 elsif p_class='financial' and (v_counts->>'cost_entries')::integer+(v_counts->>'settlements')::integer>0 then
  perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,'retention_expiry',p_ref,'financial','batch','cost_entries','deleted',
   v_counts-'records',p_by);
 end if;
 return jsonb_build_object('item_class',p_class,'processed',n,'more',n>=p_batch,'counts',v_counts,'cutoff',runtime.nexloop_commercial_ts(v_cut));
end $$;
alter function runtime.nexloop_retention_sweep(text,text,text,integer,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_retention_sweep(text,text,text,integer,text,text) from public;

-- Classes the keeper runs, in order: sources before derivatives is not needed (each class has its own clock).
create function runtime.nexloop_retention_classes() returns text[]
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select array(select k from jsonb_object_keys(runtime.nexloop_retention_settings_current()->'classes') k order by k)||array['financial']
$$;
alter function runtime.nexloop_retention_classes() owner to nexloop_owner;
revoke all on function runtime.nexloop_retention_classes() from public;

-- 5. Keeper port --------------------------------------------------------------------------------------------------------
create function authz.nexloop_retention_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-retention-v1',
  'eios:action:nexloop.retention.execute:1',array['nexloop_domain_worker']);c jsonb:=p_payload::jsonb;s jsonb:=runtime.nexloop_retention_settings_current();
 v jsonb;v_principal text:=(p_text::jsonb)->>'principal_id';v_interval integer:=(s->'sweep'->>'interval_seconds')::integer;
begin
 perform set_config('eios.tenant_id',t,true);
 if c->>'verb'='due' then
  select coalesce(jsonb_agg(k order by k),'[]'::jsonb) into v from unnest(runtime.nexloop_retention_classes()) k
   where not exists(select 1 from runtime.nexloop_retention_sweeps w where w.tenant_id=t and w.world=p_world and w.item_class=k
     and w.last_completed_at is not null and w.last_completed_at>=coalesce(w.last_started_at,w.last_completed_at)
     and w.last_completed_at>clock_timestamp()-make_interval(secs=>v_interval));
  return jsonb_build_object('due',v,'batch',(s->'sweep'->>'batch')::integer,'max_passes',(s->'sweep'->>'max_passes')::integer,'settings_version',(s->>'version')::integer);
 elsif c->>'verb'='sweep' then
  if not (c->>'item_class'=any(runtime.nexloop_retention_classes())) or coalesce(c->>'sweep_ref','')!~'^sweep:[0-9a-f-]{36}$' then
   raise exception 'retention sweep invalid' using errcode='22023';end if;
  insert into runtime.nexloop_retention_sweeps(tenant_id,world,item_class,last_started_at) values(t,p_world,c->>'item_class',clock_timestamp())
   on conflict(tenant_id,world,item_class) do update set last_started_at=clock_timestamp();
  v:=runtime.nexloop_retention_sweep(t,p_world,c->>'item_class',(s->'sweep'->>'batch')::integer,(c->>'sweep_ref')||':'||(c->>'item_class'),'service:'||v_principal);
  perform set_config('eios.tenant_id',t,true);
  if not (v->>'more')::boolean then
   update runtime.nexloop_retention_sweeps set last_completed_at=clock_timestamp(),last_counts=v->'counts'
    where tenant_id=t and world=p_world and item_class=c->>'item_class';
  end if;
  return v;
 elsif c->>'verb'='status' then
  select coalesce(jsonb_agg(jsonb_build_object('item_class',k.item_class,'action',k.action,'tombstones',k.n) order by k.item_class,k.action),'[]'::jsonb) into v
   from (select item_class,action,count(*) n from runtime.nexloop_erasure_tombstones where tenant_id=t and world=p_world group by item_class,action) k;
  return jsonb_build_object('world',p_world,'settings_version',(s->>'version')::integer,'tombstones',v,
   'sweeps',(select coalesce(jsonb_agg(jsonb_build_object('item_class',w.item_class,'last_completed_at',runtime.nexloop_commercial_ts(w.last_completed_at),
     'last_counts',w.last_counts) order by w.item_class),'[]'::jsonb) from runtime.nexloop_retention_sweeps w where w.tenant_id=t and w.world=p_world),
   'warnings',runtime.nexloop_retention_warnings(t,p_world));
 end if;
 raise exception 'retention command invalid' using errcode='22023';
end $$;

-- Doctor (NX-027 open item): financial records leave before an approved metric definition's maturity window ends.
create function runtime.nexloop_retention_warnings(p_tenant text,p_world text) returns jsonb
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select coalesce(jsonb_agg(jsonb_build_object('code','financial_retention_shorter_than_maturity','metric_id',m.metric_id,'metric_version',m.version,
   'maturity_days',ceil(m.maturity_seconds/86400.0)::integer,'financial_retention_days',runtime.nexloop_retention_days('financial')) order by m.metric_id,m.version),'[]'::jsonb)
 from control.nexloop_metric_definitions m join control.nexloop_metric_sources s on s.tenant_id=m.tenant_id and s.world=m.world and s.metric_id=m.metric_id
  and s.metric_version=m.version
 where m.tenant_id=p_tenant and m.world=p_world and m.maturity_seconds>runtime.nexloop_retention_days('financial')*86400
$$;
alter function runtime.nexloop_retention_warnings(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_retention_warnings(text,text) from public;
alter function authz.nexloop_retention_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_retention_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_retention_command(text,text,text,text,text) to nexloop_domain_worker;
