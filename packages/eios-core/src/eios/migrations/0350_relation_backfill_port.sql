-- NX-054 P3 slice 2 (docs/design/relation-backfill-p3.md §2): the signed port of the relation backfill,
-- authz.nexloop_relation_backfill_command (eios:action:nexloop.relation.backfill:1; protocol nexloop-relation-backfill-v1; the
-- domain worker role, like the retention keeper's port). The service principal's signed command is verified by
-- authz.nexloop_plan_port_tenant (signature; an expiry the signer sets at most 20 s ahead, SIGNED_COMMAND_TTL, and SQL accepts
-- at most 30 s ahead; an active service credential; the Action authority), which also yields
-- the tenant: nothing here takes a tenant from the payload. The world of the command is the authority's world (real); the engine
-- walks every world of the tenant (design §1 S-4: relation rows only, no effect, no credential).
-- Every verb runs with lock_timeout 200 ms set here; batch, rescan and revert_batch need the caller's statement_timeout in
-- (0, 1000 ms] (the engine checks it; nexloop_eios.relation_backfill sets it), complete and abort run under the same caller setting.
-- A batch is idempotent by its business intent run:type:world:after:batch (design N-6): the answer is stored with the batch in
-- its transaction; the same intent replays the stored answer (after the cursor moved on, instead of the engine's 40001). A batch
-- given up (55P03, 57014) rolls back with its intent: the retry runs anew. A run's intents go when it ends (completed, aborted,
-- reverted: nothing replays a batch of a finished run). Temporary number; the dispatcher renumbers.

create table control.nexloop_relation_backfill_intents (
 tenant_id text not null,run_id text not null,intent_id text not null check(char_length(intent_id) between 1 and 400),
 payload_digest text not null check(payload_digest~'^[0-9a-f]{64}$'),answer jsonb not null,recorded_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,intent_id),foreign key(tenant_id,run_id) references control.nexloop_relation_backfill_runs(tenant_id,run_id));
alter table control.nexloop_relation_backfill_intents owner to nexloop_owner;
alter table control.nexloop_relation_backfill_intents enable row level security;
alter table control.nexloop_relation_backfill_intents force row level security;
create policy tenant_boundary on control.nexloop_relation_backfill_intents to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_relation_backfill_intents from public;

create function authz.nexloop_relation_backfill_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-relation-backfill-v1',
  'eios:action:nexloop.relation.backfill:1',array['nexloop_domain_worker']);
 c jsonb:=p_payload::jsonb;v_verb text:=c->>'verb';v_run text:=c->>'run_id';v_principal text:=(p_text::jsonb)->>'principal_id';
 v_intent text;v_digest text:=encode(sha256(convert_to(p_payload,'UTF8')),'hex');stored control.nexloop_relation_backfill_intents;v jsonb;
 v_limit integer;
begin
 if p_world<>'real' then raise exception 'relation backfill runs from the real world authority' using errcode='42501';end if;
 perform set_config('lock_timeout','200ms',true);
 perform set_config('eios.tenant_id',t,true);
 if v_verb in ('preflight','verify') then
  if (select array_agg(k order by k) from jsonb_object_keys(c) k) is distinct from array['verb'] then raise exception 'relation backfill command invalid' using errcode='22023';end if;
  return case v_verb when 'preflight' then control.nexloop_relation_backfill_preflight(t) else control.nexloop_relation_backfill_verify(t) end;
 end if;
 if v_verb='sample' then
  if (select array_agg(k order by k) from jsonb_object_keys(c) k) is distinct from array['verb'] then raise exception 'relation backfill command invalid' using errcode='22023';end if;
  -- Background only (design §5, R-5: the waiters of a batch are counted inside it): table-level lock waits on the three tables,
  -- dead tuples, multixact age and the WAL position. Counts only.
  return jsonb_build_object(
   'table_lock_waits',(select count(*) from pg_locks l where not l.granted and l.locktype='relation'
     and l.relation in ('ontology.objects'::regclass,'ontology.relations'::regclass,'control.nexloop_consumer_erasures'::regclass)),
   'dead_tuples',(select coalesce(jsonb_object_agg(s.relname,s.n_dead_tup),'{}'::jsonb) from pg_stat_user_tables s
     where (s.schemaname,s.relname) in (('ontology','objects'),('ontology','relations'))),
   'live_tuples',(select coalesce(jsonb_object_agg(s.relname,s.n_live_tup),'{}'::jsonb) from pg_stat_user_tables s
     where (s.schemaname,s.relname) in (('ontology','objects'),('ontology','relations'))),
   'mxid_age',(select mxid_age(datminmxid) from pg_database where datname=current_database()),
   'wal_lsn',pg_current_wal_lsn()::text);
 end if;
 if coalesce(v_run,'')!~'^[A-Za-z0-9._-]{1,80}$' then raise exception 'relation backfill command invalid' using errcode='22023';end if;
 if v_verb='start' then
  if (select array_agg(k order by k) from jsonb_object_keys(c) k) is distinct from array['digest','run_id','verb'] then raise exception 'relation backfill command invalid' using errcode='22023';end if;
  return control.nexloop_relation_backfill_start(t,v_run,c->>'digest','service:'||v_principal);
 elsif v_verb in ('status','complete') then
  if (select array_agg(k order by k) from jsonb_object_keys(c) k) is distinct from array['run_id','verb'] then raise exception 'relation backfill command invalid' using errcode='22023';end if;
  if v_verb='status' then return control.nexloop_relation_backfill_status(t,v_run);end if;
  v:=control.nexloop_relation_backfill_complete(t,v_run);
  if (v->>'completed')::boolean then delete from control.nexloop_relation_backfill_intents where tenant_id=t and run_id=v_run;end if;
  return v;
 elsif v_verb='abort' then
  if (select array_agg(k order by k) from jsonb_object_keys(c) k) is distinct from array['reason','run_id','verb']
   or coalesce(c->>'reason','')!~'^[a-z][a-z0-9_]{0,63}$' then raise exception 'relation backfill command invalid' using errcode='22023';end if;
  v:=control.nexloop_relation_backfill_abort(t,v_run,c->>'reason');
  delete from control.nexloop_relation_backfill_intents where tenant_id=t and run_id=v_run;
  return v;
 elsif v_verb in ('rescan','revert_batch') then
  if (select array_agg(k order by k) from jsonb_object_keys(c) k) is distinct from array['limit','run_id','verb']
   or jsonb_typeof(c->'limit') is distinct from 'number' or (c->>'limit')!~'^[0-9]{1,4}$' then raise exception 'relation backfill command invalid' using errcode='22023';end if;
  v_limit:=(c->>'limit')::integer;
  if v_verb='rescan' then return control.nexloop_relation_backfill_rescan(t,v_run,v_limit);end if;
  v:=control.nexloop_relation_backfill_revert_batch(t,v_run,v_limit);
  if v->>'state'='reverted' then delete from control.nexloop_relation_backfill_intents where tenant_id=t and run_id=v_run;end if;
  return v;
 elsif v_verb='batch' then
  if (select array_agg(k order by k) from jsonb_object_keys(c) k) is distinct from array['after','batch','run_id','source_type','verb','world']
   or coalesce(c->>'source_type','')!~'^[A-Z][A-Za-z0-9_]{0,63}$' or not runtime.nexloop_world_valid(c->>'world')
   or jsonb_typeof(c->'after') is distinct from 'string' or (c->>'after')!~'^([a-f0-9]{64})?$'
   or jsonb_typeof(c->'batch') is distinct from 'number' or (c->>'batch')!~'^[0-9]{1,4}$' then
   raise exception 'relation backfill command invalid' using errcode='22023';end if;
  v_intent:=v_run||':'||(c->>'source_type')||':'||(c->>'world')||':'||(c->>'after')||':'||(c->>'batch');
  select * into stored from control.nexloop_relation_backfill_intents where tenant_id=t and intent_id=v_intent;
  if found then
   if stored.payload_digest<>v_digest then raise exception 'relation backfill intent conflict' using errcode='40001';end if;
   return stored.answer||jsonb_build_object('replayed',true);
  end if;
  v:=control.nexloop_relation_backfill_batch(t,v_run,c->>'source_type',c->>'world',c->>'after',(c->>'batch')::integer);
  insert into control.nexloop_relation_backfill_intents(tenant_id,run_id,intent_id,payload_digest,answer) values(t,v_run,v_intent,v_digest,v);
  return v;
 end if;
 raise exception 'relation backfill command invalid' using errcode='22023';
end $$;
alter function authz.nexloop_relation_backfill_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_relation_backfill_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_relation_backfill_command(text,text,text,text,text) to nexloop_domain_worker;
