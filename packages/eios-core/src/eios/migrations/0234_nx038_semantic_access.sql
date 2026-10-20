-- NX-038 E3 (design docs/implementation/NX-038-design.md D2, §2.2, §12.5, §12.7; owner D2 2026-10-12, D15–D20 2026-10-20):
-- the per-access record of the Context Engine's semantic browse/resolve. Temporary number; the dispatcher renumbers at merge.
--
-- 1. runtime.nexloop_semantic_accesses: append only. One 'access' row per browse/resolve (operation, query kind, sha256 of
--    each query term - never the text -, coverage, ambiguity, counts, the returned definition refs with their revisions);
--    one 'use' row when the Run's caller reports which of those refs it used (refs only). No update; a delete only from the
--    two owner paths below (retention, Consumer erasure), under a transaction-local flag those paths set.
-- 2. Writer: authz.nexloop_semantic_access_record, the Context Engine's signed port (its own protocol, the context read
--    key): tenant and world from the authenticated service identity, never from the payload; a run_id must be a Run bound
--    to that tenant and world; a 'use' row must name an access of the same tenant, world and Run, and only refs it returned.
-- 3. Reader: role nexloop_evo_reader (D1; created here if absent, restricted like nexloop_metrics) executes only
--    authz.nexloop_semantic_access_read: world real, the caller's tenant (service identity), ids, hashes, refs, counts.
-- 4. NX-029: retention class semantic_access (settings v5, 90 days; retention.v1.json) swept by the keeper like the other
--    classes; a Consumer erasure deletes the records of that Consumer's Runs when it starts purging and again when it
--    completes (a Consumer hold keeps them, as every other class). Each delete leaves a tombstone with counts only.
-- Published migrations are not edited.

-- 3a. The reader role (E2 provisions its login; no grant beyond the read function below) -------------------------------
do $$begin
 if not exists(select 1 from pg_roles where rolname='nexloop_evo_reader') then
  create role nexloop_evo_reader login nosuperuser nocreatedb nocreaterole noinherit nobypassrls;
 end if;
end $$;

-- 1. The record ------------------------------------------------------------------------------------------------------------
create table runtime.nexloop_semantic_accesses (
 tenant_id text not null,world text not null check(world in ('real','test','simulation')),
 access_id uuid not null,kind text not null check(kind in ('access','use')),
 run_id uuid,context_id text check(context_id~'^[A-Za-z0-9._:@-]{1,190}$'),
 accessed_id uuid,
 operation text check(operation in ('browse','resolve')),
 query_kind text check(query_kind in ('object_type','property','vocabulary_value','any')),
 term_hashes text[] not null default '{}' check(cardinality(term_hashes)<=16 and array_to_string(term_hashes,',')~'^([0-9a-f]{64}(,[0-9a-f]{64})*)?$'),
 coverage numeric check(coverage between 0 and 1),ambiguity boolean,
 result_refs jsonb not null default '[]'::jsonb check(jsonb_typeof(result_refs)='array' and jsonb_array_length(result_refs)<=320),
 result_count integer not null default 0 check(result_count between 0 and 320),
 unresolved_count integer not null default 0 check(unresolved_count between 0 and 16),
 recorded_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,access_id),
 check((kind='access' and accessed_id is null and operation is not null and query_kind is not null and coverage is not null and ambiguity is not null
        and cardinality(term_hashes)>=1)
    or (kind='use' and accessed_id is not null and operation is null and query_kind is null and coverage is null and ambiguity is null
        and cardinality(term_hashes)=0 and unresolved_count=0))
);
alter table runtime.nexloop_semantic_accesses add constraint nexloop_semantic_accesses_tenant_fk foreign key (tenant_id) references control.nexloop_tenants(tenant_id);
create index nexloop_semantic_accesses_run on runtime.nexloop_semantic_accesses(tenant_id,world,run_id);
create index nexloop_semantic_accesses_recorded on runtime.nexloop_semantic_accesses(tenant_id,world,recorded_at);
alter table runtime.nexloop_semantic_accesses owner to nexloop_owner;
alter table runtime.nexloop_semantic_accesses enable row level security;
alter table runtime.nexloop_semantic_accesses force row level security;
create policy tenant_boundary on runtime.nexloop_semantic_accesses to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_semantic_accesses from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,
 nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_evo_reader;

create function runtime.nexloop_semantic_access_append_only() returns trigger
 language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if tg_op='DELETE' and current_user='nexloop_owner' and current_setting('nexloop.semantic_access_purge',true)='on' then return old;end if;
 raise exception 'semantic access records are append-only' using errcode='42501';
end $$;
alter function runtime.nexloop_semantic_access_append_only() owner to nexloop_owner;
revoke all on function runtime.nexloop_semantic_access_append_only() from public;
create trigger nx038_append_only before update or delete on runtime.nexloop_semantic_accesses for each row execute function runtime.nexloop_semantic_access_append_only();
create trigger nx038_no_truncate before truncate on runtime.nexloop_semantic_accesses for each statement execute function runtime.nexloop_semantic_access_append_only();

-- 2. The writer --------------------------------------------------------------------------------------------------------------
create function authz.nexloop_semantic_access_record(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
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
 if v_run is not null and not exists(select 1 from authz.nexloop_runtime_run_bindings b where b.tenant_id=v_tenant and b.world=p_world and b.run_id=v_run) then
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
    result_refs,result_count,unresolved_count)
   values(v_tenant,p_world,v_id,'access',v_run,nullif(c->>'context_id',''),c->>'operation',c->>'query_kind',v_terms,(c->>'coverage')::numeric,
    (c->>'ambiguity')::boolean,v_refs,jsonb_array_length(v_refs),coalesce((c->>'unresolved_count')::integer,0)) on conflict do nothing;
 end if;
 return jsonb_build_object('access_id',v_id,'kind',c->>'kind');
end $$;
alter function authz.nexloop_semantic_access_record(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_semantic_access_record(text,text,text,text,text) from public;
grant execute on function authz.nexloop_semantic_access_record(text,text,text,text,text) to nexloop_api,nexloop_domain_worker;

-- 3b. The reader ----------------------------------------------------------------------------------------------------------------
create function authz.nexloop_semantic_access_read(p_digest text,p_world text,p_since timestamptz,p_limit integer) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text;
begin
 if session_user is distinct from 'nexloop_evo_reader' or p_world is distinct from 'real' or p_limit not between 1 and 1000 or p_since is null then
  raise exception 'semantic access read unavailable' using errcode='42501';end if;
 v_tenant:=authz.nexloop_service_identity_snapshot(p_digest,p_world)->'binding'->>'tenant_id';
 if v_tenant is null then raise exception 'semantic access read unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 return coalesce((select jsonb_agg(jsonb_build_object('access_id',s.access_id,'kind',s.kind,'run_id',s.run_id,'context_id',s.context_id,'accessed_id',s.accessed_id,
   'operation',s.operation,'query_kind',s.query_kind,'term_hashes',to_jsonb(s.term_hashes),'coverage',s.coverage,'ambiguity',s.ambiguity,
   'result_refs',s.result_refs,'result_count',s.result_count,'unresolved_count',s.unresolved_count,'recorded_at',s.recorded_at) order by s.recorded_at,s.access_id)
  from (select * from runtime.nexloop_semantic_accesses where tenant_id=v_tenant and world='real' and recorded_at>=p_since
   order by recorded_at,access_id limit p_limit) s),'[]'::jsonb);
end $$;
alter function authz.nexloop_semantic_access_read(text,text,timestamptz,integer) owner to nexloop_owner;
revoke all on function authz.nexloop_semantic_access_read(text,text,timestamptz,integer) from public;
grant usage on schema authz to nexloop_evo_reader;
grant execute on function authz.nexloop_semantic_access_read(text,text,timestamptz,integer) to nexloop_evo_reader;

-- 4a. Consumer erasure: the records of the Consumer's Runs leave when purging begins and again when it completes ----------
create function runtime.nexloop_semantic_access_purge_consumer(p_tenant text,p_world text,p_consumer text,p_ref text,p_by text) returns integer
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare n integer;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 perform set_config('nexloop.semantic_access_purge','on',true);
 delete from runtime.nexloop_semantic_accesses where tenant_id=p_tenant and world=p_world
  and run_id in (select runtime.nexloop_consumer_runs(p_tenant,p_world,p_consumer));
 get diagnostics n=row_count;
 perform set_config('nexloop.semantic_access_purge','',true);
 if n>0 then
  perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,'consumer_erasure',p_ref,'semantic_access','consumer_runs',p_consumer,'deleted',
   jsonb_build_object('semantic_accesses',n),p_by);
 end if;
 return n;
end $$;
alter function runtime.nexloop_semantic_access_purge_consumer(text,text,text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_semantic_access_purge_consumer(text,text,text,text,text) from public;

create function control.nexloop_semantic_access_erasure() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
begin
 if new.state in ('purging','completed') and old.state is distinct from new.state then
  perform runtime.nexloop_semantic_access_purge_consumer(new.tenant_id,new.world,new.consumer_id,'erasure:'||new.request_id,'service:retention_keeper');
 end if;
 return null;
end $$;
alter function control.nexloop_semantic_access_erasure() owner to nexloop_owner;
revoke all on function control.nexloop_semantic_access_erasure() from public;
create trigger nx038_semantic_access_erasure after update of state on control.nexloop_consumer_erasures
 for each row execute function control.nexloop_semantic_access_erasure();

-- 4b. Retention class semantic_access (settings v5) ----------------------------------------------------------------------------
create or replace function control.nexloop_retention_settings_check() returns trigger
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare k text;v jsonb;
begin
 for k,v in select * from jsonb_each(new.definition->'classes') loop
  if k not in ('message_text','claim_quote','prompt_text','job_input','commercial_receipt','metric_observation','run_files','commitment_closed','claim_superseded',
   'plan_closed','effect_text','job_records','model_request_manifest','read_audit','governance_audit','semantic_access')
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
insert into control.nexloop_retention_settings(version,definition,definition_digest,published_by)
 values(5,'{"classes":{"claim_quote":{"days":90,"source":"message_text"},"claim_superseded":{"days":365,"source":null},"commercial_receipt":{"days":90,"source":null},"commitment_closed":{"days":365,"source":null},"effect_text":{"days":90,"source":"message_text"},"governance_audit":{"days":365,"source":null},"job_input":{"days":30,"source":"message_text"},"job_records":{"days":365,"source":null},"message_text":{"days":90,"source":null},"metric_observation":{"days":365,"source":null},"model_request_manifest":{"days":365,"source":null},"plan_closed":{"days":365,"source":null},"prompt_text":{"days":30,"source":"message_text"},"read_audit":{"days":365,"source":null},"run_files":{"days":30,"source":null},"semantic_access":{"days":90,"source":null}},"decision":"NX-029 slice 1 (design docs/implementation/NX-029-design.md §3, owner decisions D1–D7 ''all as recommended'' 2026-10-10, dispatcher D8/D9): tenant-level retention (D9) for the classes the retention keeper executes in slice 1. Product defaults, not legal periods (03:96, 13:31); enterprises adjust by a new version. A derived class never outlives its source (prompt text, Claim quotes and job inputs follow the message text). Financial records and cost entries use financial_retention_days of the commercial settings (NX-027 D6), not a second value here. Expired message text, Claim quotes, prompt text and job inputs are redacted in place (rows, digests and order stay); commercial receipts, metric observations, commercial records and cost entries are deleted; every pass leaves a tombstone without content. v2 (NX-029 slice 4, NX-035 D4 confirmed by the owner 2026-10-10): records — deletion ledger and tombstones are kept 365 days, longer than the 35-day maximum backup retention, so every deletion still in a backup can be replayed after a restore (NX-035 §5, §6.2); a version with backup_max_retention_days not below both is refused. v3 (NX-029 final, AT-026): three expiry classes the keeper now executes - run_files: a Pi Run directory 30 days after its Run is terminal (queued for the Agent Host runs/purge, deleted only when the Host confirms; ADR-005, 06:92); commitment_closed: a Commitment 365 days after it was closed (fulfilled, cancelled or breached), object and ledger deleted (D4); claim_superseded: the structured value and remaining text of a superseded Claim 365 days after it was superseded, ids and the correction chain kept (D2). v4 (NX-029 completion): the remaining expiry classes of the design table - plan_closed: a plan 365 days after its last status became closed or invalidated, all versions, steps, events, Runs, outcomes and its unshared strategies deleted (D3); effect_text: the frozen request text of a terminal effect intent 90 days after it was created, states, receipts and digests kept (follows message_text; ADR-021); job_records: 365 days after a task became terminal its events, inbox and outbox rows and old idempotency records are deleted, the task row too unless a Run binding still refers to it (then its payloads are erased); model_request_manifest: model request rows and their prompt rows 365 days after the request (D3; cost entries keep their own financial retention); read_audit: read audit rows 365 days after the read, folded into per-day counts (D6); governance_audit: audit events, superseded control events (the latest per scope stays), human requests, export requests and completed deletion requests whose ledger entries are gone, 365 days after the event (D6). records: ledger entries and tombstones leave after deletion_ledger_days / tombstone_days, never while a restore replay is open; both must exceed backup_max_retention_days. v5 (NX-038 E3, owner D2 2026-10-12): semantic_access - the append-only per-access records of the Context Engine''s semantic browse/resolve (term hashes, refs and counts, no text) are deleted 90 days after they were recorded; a Consumer erasure deletes the records of that Consumer''s Runs when purging begins.","records":{"backup_max_retention_days":35,"deletion_ledger_days":365,"tombstone_days":365},"schema":"nexloop-retention/1","sweep":{"batch":200,"interval_seconds":3600,"max_passes":20},"version":5}','6a34e4238eec9680d471f5982d3ff24eeabe18154c55c3bba5b7defd233dc263','migration:nx038-e3');

create function runtime.nexloop_retention_sweep_semantic_access(p_tenant text,p_world text,p_class text,p_batch integer,p_ref text,p_by text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_cut timestamptz;n integer;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 v_cut:=clock_timestamp()-make_interval(days=>runtime.nexloop_retention_days(p_class));
 if p_class<>'semantic_access' or v_cut is null then raise exception 'retention class unavailable' using errcode='22023';end if;
 perform set_config('nexloop.semantic_access_purge','on',true);
 with due as (select access_id from runtime.nexloop_semantic_accesses where tenant_id=p_tenant and world=p_world and recorded_at<v_cut
   order by recorded_at limit p_batch for update skip locked)
 delete from runtime.nexloop_semantic_accesses s using due where s.tenant_id=p_tenant and s.world=p_world and s.access_id=due.access_id;
 get diagnostics n=row_count;
 perform set_config('nexloop.semantic_access_purge','',true);
 if n>0 then
  perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,'retention_expiry',p_ref,p_class,'batch',p_class,'deleted',jsonb_build_object('semantic_accesses',n),p_by);
 end if;
 return jsonb_build_object('item_class',p_class,'processed',n,'more',n>=p_batch,'counts',jsonb_build_object('semantic_accesses',n),'cutoff',runtime.nexloop_commercial_ts(v_cut));
end $$;
alter function runtime.nexloop_retention_sweep_semantic_access(text,text,text,integer,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_retention_sweep_semantic_access(text,text,text,integer,text,text) from public;
-- The hold wrapper (0140 body, replaced in place) dispatches the new class; holds are checked first as before.
create or replace function runtime.nexloop_retention_sweep(p_tenant text,p_world text,p_class text,p_batch integer,p_ref text,p_by text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if runtime.nexloop_retention_held(p_tenant,p_world,'item_class',p_class) then
  perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,'retention_expiry',p_ref,p_class,'batch',p_class,'skipped_hold','{}'::jsonb,p_by);
  return jsonb_build_object('item_class',p_class,'processed',0,'more',false,'counts','{}'::jsonb,'held',true);
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
