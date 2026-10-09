-- O5b level 1 (temporary number): one statement, one visible snapshot, no new advisory
-- lock, no own write -> a repeated identical read-authority assertion is answered from a
-- per-session owner-only memo after re-checking every clock condition. Any visible commit
-- or abort (snapshot change), any newly acquired advisory lock, any own write to an
-- authority input table, or a new statement makes the next assertion a full check.
-- Only allows are memoized. Design: docs/implementation/perf-o5b-design.md.

do $grant$ begin execute format('grant temporary on database %I to nexloop_owner',current_database());end $grant$;

-- Governed kill switch (owner-only); a session GUC nexloop.read_memo=off can only disable.
create table authz.nexloop_read_memo_settings(
 singleton boolean primary key default true check(singleton),
 enabled boolean not null
);
alter table authz.nexloop_read_memo_settings owner to nexloop_owner;
revoke all on authz.nexloop_read_memo_settings from public;
insert into authz.nexloop_read_memo_settings values(true,true);

-- The memo table is usable only when this session's pg_temp.nexloop_read_memo is the
-- owner's own ordinary temp table with no grants. A table pre-created by the session
-- role (or anything else under that name) disables the memo; every reference below is
-- schema-qualified and search_path puts pg_temp last, so temp objects cannot shadow.
create function authz.nexloop_read_memo_table_ok() returns boolean
language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
declare rel oid:=pg_catalog.to_regclass('pg_temp.nexloop_read_memo');
begin
 return rel is not null and exists(select 1 from pg_catalog.pg_class c where c.oid=rel and c.relkind='r' and c.relpersistence='t'
  and c.relnamespace=pg_catalog.pg_my_temp_schema() and c.relowner=pg_catalog.to_regrole('nexloop_owner') and c.relacl is null);
end $$;

create function authz.nexloop_read_memo_ready() returns boolean
language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
begin
 if pg_catalog.current_setting('nexloop.read_memo',true) is not distinct from 'off'
  or not coalesce((select s.enabled from authz.nexloop_read_memo_settings s where s.singleton),false) then return false;end if;
 if pg_catalog.to_regclass('pg_temp.nexloop_read_memo') is null then
  -- CREATE is not allowed in a read-only transaction: such a transaction simply runs full checks.
  if pg_catalog.current_setting('transaction_read_only')='on' then return false;end if;
  create temporary table pg_temp.nexloop_read_memo(
   key text primary key, tenant text, binding jsonb not null, advisory_locks bigint not null
  ) on commit delete rows;
 end if;
 return authz.nexloop_read_memo_table_ok();
end $$;

-- Advisory locks are the waits a snapshot cannot reveal (a holder may have no xid): the
-- number this backend holds is part of the hit condition, so taking any new advisory
-- lock (waiting or not) acts as a barrier. Row-lock waits always end with the holder's
-- xid leaving the snapshot, so they are covered by the snapshot itself.
create function authz.nexloop_read_memo_advisory_locks() returns bigint
language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select pg_catalog.count(*) from pg_catalog.pg_locks l where l.pid=pg_catalog.pg_backend_pid() and l.locktype='advisory' and l.granted
$$;

create function authz.nexloop_read_memo_key(p_digest text,p_world text,p_claims jsonb) returns text
language sql volatile security definer set search_path=pg_catalog,pg_temp as $$
 -- Taken fresh at every call (VOLATILE: new snapshot per evaluation), i.e. after every
 -- lock acquired earlier in the statement; never a snapshot remembered from before.
 select pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(pg_catalog.concat_ws(pg_catalog.chr(31),
  pg_catalog.statement_timestamp()::text,pg_catalog.pg_current_snapshot()::text,pg_catalog.current_setting('TimeZone'),
  coalesce(pg_catalog.current_setting('eios.tenant_id',true),''),p_digest,p_world,p_claims::text),'UTF8')),'hex')
$$;

-- Own writes are invisible to the snapshot: any write to a table the assertion chain
-- reads empties the memo for the rest of the transaction (regardless of the switches).
create function authz.nexloop_read_memo_invalidate() returns trigger
language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
begin
 if authz.nexloop_read_memo_table_ok() then delete from pg_temp.nexloop_read_memo;end if;
 return null;
end $$;

do $triggers$
declare t text;
begin
 foreach t in array array['authz.nexloop_authority_facts','authz.nexloop_service_credentials','authz.nexloop_run_credentials',
  'authz.nexloop_browser_token_realms','authz.nexloop_authority_signing_keys','control.nexloop_tenants',
  'control.nexloop_browser_accounts','control.nexloop_browser_applications','control.nexloop_browser_business_applications',
  'control.nexloop_browser_memberships','control.nexloop_browser_sessions','control.nexloop_browser_subjects',
  'ontology.objects','ontology.object_type_versions','ontology.nexloop_candidate_definitions','ontology.nexloop_review_decisions',
  'runtime.nexloop_conversations','runtime.nexloop_conversation_messages','runtime.nexloop_message_outbox','runtime.nexloop_outbound_messages'] loop
  execute format('create trigger nexloop_read_memo_invalidate after insert or update or delete or truncate on %s for each statement execute function authz.nexloop_read_memo_invalidate()',t);
 end loop;
end $triggers$;

alter function authz.nexloop_assert_read_authority(text,text,jsonb) rename to nexloop_assert_read_authority_before_read_memo_v0095;

create function authz.nexloop_assert_read_authority(p_digest text,p_world text,p_claims jsonb) returns jsonb
language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_key text;v_locks bigint;v_hit_locks bigint;v_hit_tenant text;v_hit_binding jsonb;v_identity jsonb;v_binding jsonb;v_ok boolean:=false;
begin
 -- Accepted-Message derivations compare rule validity with the clock separately; they
 -- always run the full check. Configured and type-property-v1 claims bound every nested
 -- deadline by claims.expires_at, which every hit re-checks.
 if (p_claims ? 'derivation' and p_claims->>'derivation' is distinct from 'type-property-v1')
  or p_claims->>'directory_hash' is null or p_claims->>'expires_at' is null or not authz.nexloop_read_memo_ready() then
  return authz.nexloop_assert_read_authority_before_read_memo_v0095(p_digest,p_world,p_claims);
 end if;
 v_key:=authz.nexloop_read_memo_key(p_digest,p_world,p_claims);
 v_locks:=authz.nexloop_read_memo_advisory_locks();
 -- No row type of the temp table is declared: it may be recreated within the session.
 select m.advisory_locks,m.tenant,m.binding into v_hit_locks,v_hit_tenant,v_hit_binding from pg_temp.nexloop_read_memo m where m.key=v_key;
 if found and v_hit_locks=v_locks and (p_claims->>'expires_at')::timestamptz>pg_catalog.clock_timestamp() then
  -- Every clock condition of the identity (credential, Run, source, browser session and
  -- membership deadlines) is re-evaluated by the same code; any failure means full check.
  begin
   v_identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
   v_ok:=v_identity->>'directory_hash' is not distinct from p_claims->>'directory_hash'
    and (p_claims->>'expires_at')::timestamptz>pg_catalog.clock_timestamp();
  exception when others then v_ok:=false;
  end;
  if v_ok then
   perform pg_catalog.set_config('eios.tenant_id',v_hit_tenant,true);  -- same session state as the full check
   return v_hit_binding;
  end if;
 end if;
 v_binding:=authz.nexloop_assert_read_authority_before_read_memo_v0095(p_digest,p_world,p_claims);
 -- Stored under the key computed before the full check: a later hit needs the visible
 -- snapshot to be unchanged since before this check began (including any wait inside it).
 insert into pg_temp.nexloop_read_memo(key,tenant,binding,advisory_locks)
  values(v_key,pg_catalog.current_setting('eios.tenant_id',true),v_binding,authz.nexloop_read_memo_advisory_locks())
  on conflict(key) do update set tenant=excluded.tenant,binding=excluded.binding,advisory_locks=excluded.advisory_locks;
 return v_binding;
end $$;

do $grants$
declare f text;
begin
 foreach f in array array['authz.nexloop_read_memo_table_ok()','authz.nexloop_read_memo_ready()','authz.nexloop_read_memo_advisory_locks()',
  'authz.nexloop_read_memo_key(text,text,jsonb)','authz.nexloop_read_memo_invalidate()',
  'authz.nexloop_assert_read_authority(text,text,jsonb)','authz.nexloop_assert_read_authority_before_read_memo_v0095(text,text,jsonb)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';
  execute 'revoke all on function '||f||' from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator';
 end loop;
end $grants$;
