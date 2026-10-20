-- NX-041 S2 (design D2, D10, §11 S2, migration B): creating and deleting simulation worlds, and credentials per world. The migration
-- number lives only in the file name and the catalog; nothing below repeats it.
-- 1. control.nexloop_sim_worlds: one row per sim:<uuid> or shadow:<uuid> world (S1 check; real and test are never registered) with its
--    tenant, creator, purpose, status (active, deleting, deleted), creation, expiry and deletion times.
-- 2. Credentials per world (D10, AT-046): a service credential that holds a sim or shadow world holds exactly that one world, the world is
--    registered and active for the credential's tenant, at most one active credential per world, its worlds never change, and its
--    principal is never the principal of a real or test credential (a real credential, or one that causes real effects, is never
--    carried into a simulation world). Real dispatch stays closed for any world but real (0165 real alone, the dispatch world layer).
--    Deleting a world revokes its credentials and Run credentials.
-- 3. Governed owner Actions sim_world.create / create_sim_world and sim_world.delete / delete_sim_world (D10: owner only).
-- 4. Whole deletion (D2): runtime.nexloop_sim_world_purge deletes the world's rows from every table with a world column, in bounded
--    batches, each call its own transaction (resumable after an interruption; idempotent once deleted). Before the first row leaves,
--    control.nexloop_sim_world_summaries keeps per-table counts and digests (no content); retention class sim_world_summary (settings
--    v8) removes it 30 days after the deletion completed and resumes worlds still deleting. Only rows whose world equals the
--    registered sim or shadow world are touched; real and test rows never are.
-- 5. authz.nexloop_metrics_export (S1: real and test) also iterates the registered active simulation and shadow worlds.

-- 1. The registry --------------------------------------------------------------------------------------------------------------
create table control.nexloop_sim_worlds (
 world text primary key check(coalesce(runtime.nexloop_world_kind(world),'') in ('simulation','shadow')),
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 created_by text not null check(char_length(created_by) between 1 and 200),
 purpose text not null check(char_length(btrim(purpose)) between 1 and 500),
 status text not null check(status in ('active','deleting','deleted')),
 created_at timestamptz not null default clock_timestamp(),
 expires_at timestamptz not null,
 deleted_at timestamptz,
 request_id text not null check(char_length(request_id) between 1 and 200),
 unique(tenant_id,request_id),
 check(expires_at>created_at),check((status='deleted')=(deleted_at is not null)));
create table control.nexloop_sim_world_summaries (
 world text primary key,tenant_id text not null references control.nexloop_tenants(tenant_id),
 counts jsonb not null check(jsonb_typeof(counts)='object'),digests jsonb not null check(jsonb_typeof(digests)='object'),
 summary_digest text not null check(summary_digest~'^[0-9a-f]{64}$'),
 started_at timestamptz not null default clock_timestamp(),completed_at timestamptz,
 progress jsonb not null default '{}'::jsonb check(jsonb_typeof(progress)='object'));
do $$declare t text;begin
 foreach t in array array['nexloop_sim_worlds','nexloop_sim_world_summaries'] loop
  execute format('alter table control.%I owner to nexloop_owner',t);
  execute format('alter table control.%I enable row level security',t);
  execute format('alter table control.%I force row level security',t);
  execute format('create policy tenant_boundary on control.%I to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on control.%I from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics',t);
 end loop;
end $$;
create index nexloop_sim_worlds_tenant on control.nexloop_sim_worlds(tenant_id,status);

-- Registry rows change only forward (active -> deleting -> deleted) and never leave; the summary is fixed once taken.
create function control.nexloop_sim_world_guard() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if tg_op='DELETE' or tg_op='TRUNCATE' then raise exception 'simulation worlds are never deleted from the registry' using errcode='42501';end if;
 if tg_table_name='nexloop_sim_worlds' then
  if (new.world,new.tenant_id,new.created_by,new.purpose,new.created_at,new.expires_at,new.request_id)
     is distinct from (old.world,old.tenant_id,old.created_by,old.purpose,old.created_at,old.expires_at,old.request_id)
   or not ((old.status,new.status) in (('active','active'),('active','deleting'),('deleting','deleting'),('deleting','deleted'),('deleted','deleted'))) then
   raise exception 'simulation world registry: invalid change' using errcode='42501';end if;
 else
  if (new.world,new.tenant_id,new.counts,new.digests,new.summary_digest,new.started_at) is distinct from
     (old.world,old.tenant_id,old.counts,old.digests,old.summary_digest,old.started_at) then
   raise exception 'simulation world summary is fixed' using errcode='42501';end if;
 end if;
 return new;
end $$;
alter function control.nexloop_sim_world_guard() owner to nexloop_owner;
revoke all on function control.nexloop_sim_world_guard() from public;
create trigger nx041_sim_world_guard before update or delete on control.nexloop_sim_worlds for each row execute function control.nexloop_sim_world_guard();
create trigger nx041_sim_world_no_truncate before truncate on control.nexloop_sim_worlds for each statement execute function control.nexloop_sim_world_guard();
-- The summary leaves only through the retention sweep (its own flag), never otherwise.
create function control.nexloop_sim_world_summary_guard() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if tg_op='DELETE' and current_user='nexloop_owner' and current_setting('nexloop.sim_summary_retention',true)='on' then return old;end if;
 if tg_op='UPDATE' then
  if (new.world,new.tenant_id,new.counts,new.digests,new.summary_digest,new.started_at) is distinct from
     (old.world,old.tenant_id,old.counts,old.digests,old.summary_digest,old.started_at) then
   raise exception 'simulation world summary is fixed' using errcode='42501';end if;
  return new;
 end if;
 raise exception 'simulation world summary leaves by retention only' using errcode='42501';
end $$;
alter function control.nexloop_sim_world_summary_guard() owner to nexloop_owner;
revoke all on function control.nexloop_sim_world_summary_guard() from public;
create trigger nx041_sim_summary_guard before update or delete on control.nexloop_sim_world_summaries for each row execute function control.nexloop_sim_world_summary_guard();
create trigger nx041_sim_summary_no_truncate before truncate on control.nexloop_sim_world_summaries for each statement execute function control.nexloop_sim_world_summary_guard();

-- The status of a registered world for a tenant (null if not registered there); the caller's tenant setting is restored.
create function control.nexloop_sim_world_status(p_tenant text,p_world text) returns text
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_old text:=current_setting('eios.tenant_id',true);v text;
begin
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 select status into v from control.nexloop_sim_worlds where world=p_world and tenant_id=p_tenant;
 perform set_config('eios.tenant_id',coalesce(v_old,''),true);
 return v;
end $$;
alter function control.nexloop_sim_world_status(text,text) owner to nexloop_owner;
revoke all on function control.nexloop_sim_world_status(text,text) from public;

-- 2. Credentials per world (D10) --------------------------------------------------------------------------------------------------
create function authz.nexloop_sim_world_credential_check() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
declare v_sim text[];v_principals text[];
begin
 v_sim:=array(select w from unnest(new.worlds) w where runtime.nexloop_world_kind(w) in ('simulation','shadow'));
 v_principals:=array_remove(array[new.binding->>'principal_id',new.binding->>'subject_principal_id'],null);
 if tg_op='UPDATE' then
  if (old.worlds is distinct from new.worlds) and (exists(select 1 from unnest(old.worlds||new.worlds) w where runtime.nexloop_world_kind(w) in ('simulation','shadow'))) then
   raise exception 'a simulation world credential is registered, never changed' using errcode='42501';end if;
  if cardinality(v_sim)=0 or new.status<>'active' or old.status='active' then return new;end if;
  raise exception 'a revoked simulation world credential is never reactivated' using errcode='42501';
 end if;
 if cardinality(v_sim)=0 then
  -- A real or test credential never shares a principal with a simulation world credential.
  if exists(select 1 from authz.nexloop_service_credentials c where c.tenant_id=new.tenant_id
     and exists(select 1 from unnest(c.worlds) w where runtime.nexloop_world_kind(w) in ('simulation','shadow'))
     and array[c.binding->>'principal_id',c.binding->>'subject_principal_id'] && v_principals) then
   raise exception 'a simulation world principal never holds a real or test credential' using errcode='42501';end if;
  return new;
 end if;
 if new.worlds<>v_sim or cardinality(new.worlds)<>1 then
  raise exception 'a simulation world credential holds exactly its one world' using errcode='42501';end if;
 if control.nexloop_sim_world_status(new.tenant_id,new.worlds[1]) is distinct from 'active' then
  raise exception 'simulation world not registered or not active' using errcode='42501';end if;
 if new.status<>'active' then return new;end if;
 if exists(select 1 from authz.nexloop_service_credentials c where c.worlds=new.worlds and c.status='active' and c.token_digest<>new.token_digest) then
  raise exception 'a simulation world has one active credential' using errcode='42501';end if;
 if exists(select 1 from authz.nexloop_service_credentials c where c.tenant_id=new.tenant_id
    and exists(select 1 from unnest(c.worlds) w where runtime.nexloop_world_kind(w) in ('real','test'))
    and array[c.binding->>'principal_id',c.binding->>'subject_principal_id'] && v_principals) then
  raise exception 'a real or test principal is never carried into a simulation world' using errcode='42501';end if;
 return new;
end $$;
alter function authz.nexloop_sim_world_credential_check() owner to nexloop_owner;
revoke all on function authz.nexloop_sim_world_credential_check() from public;
create trigger nx041_sim_world_credential before insert or update on authz.nexloop_service_credentials
 for each row execute function authz.nexloop_sim_world_credential_check();

-- 4. Whole deletion (before the Actions, which call it) -------------------------------------------------------------------------
-- Every table with a world column, except the registry and the summary.
create function runtime.nexloop_sim_world_tables() returns setof regclass
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select r.oid::regclass from pg_class r join pg_namespace n on n.oid=r.relnamespace join pg_attribute a on a.attrelid=r.oid
 where a.attname='world' and not a.attisdropped and r.relkind in ('r','p') and not r.relispartition
  and n.nspname in ('authz','control','ontology','runtime')
  and r.oid not in ('control.nexloop_sim_worlds'::regclass,'control.nexloop_sim_world_summaries'::regclass)
 order by n.nspname,r.relname
$$;
alter function runtime.nexloop_sim_world_tables() owner to nexloop_owner;
revoke all on function runtime.nexloop_sim_world_tables() from public;

create function runtime.nexloop_sim_world_has_tenant(p_table regclass) returns boolean
 language sql stable set search_path=pg_catalog,pg_temp as $$
 select exists(select 1 from pg_attribute a where a.attrelid=p_table and a.attname='tenant_id' and not a.attisdropped)
$$;
alter function runtime.nexloop_sim_world_has_tenant(regclass) owner to nexloop_owner;
revoke all on function runtime.nexloop_sim_world_has_tenant(regclass) from public;

-- Per-table counts and digests of the world's rows (digest of the sorted row digests; no row content is kept).
create function runtime.nexloop_sim_world_snapshot(p_tenant text,p_world text) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t regclass;n bigint;d text;v_counts jsonb:='{}';v_digests jsonb:='{}';
begin
 if coalesce(runtime.nexloop_world_kind(p_world),'') not in ('simulation','shadow') then raise exception 'sim world: invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 if exists(select 1 from control.nexloop_sim_world_summaries where world=p_world) then return;end if;
 for t in select * from runtime.nexloop_sim_world_tables() loop
  execute format('select count(*),encode(sha256(convert_to(coalesce(string_agg(h,'','' order by h),''''),''UTF8'')),''hex'') from (select md5(x::text) h from %s x where x.world=$1%s) z',
   t,case when runtime.nexloop_sim_world_has_tenant(t) then ' and x.tenant_id=$2' else '' end) into n,d using p_world,p_tenant;
  if n>0 then v_counts:=v_counts||jsonb_build_object(t::text,n);v_digests:=v_digests||jsonb_build_object(t::text,d);end if;
 end loop;
 insert into control.nexloop_sim_world_summaries(world,tenant_id,counts,digests,summary_digest)
  values(p_world,p_tenant,v_counts,v_digests,encode(sha256(convert_to(jsonb_build_object('world',p_world,'counts',v_counts,'digests',v_digests)::text,'UTF8')),'hex'));
end $$;
alter function runtime.nexloop_sim_world_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_sim_world_snapshot(text,text) from public;

-- One bounded pass: at most p_limit rows leave (per table at most what remains of the limit); a table whose guard or foreign key
-- refuses is retried on the next pass. When nothing of the world remains the world is deleted. Never touches another world.
create function runtime.nexloop_sim_world_purge(p_tenant text,p_world text,p_limit integer) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare w control.nexloop_sim_worlds;t regclass;v_left integer:=greatest(1,least(coalesce(p_limit,1000),100000));n integer;v_total integer:=0;
 v_remaining bigint:=0;v_blocked jsonb:='{}';v_state text;v_err text;r bigint;v_where text;
begin
 if coalesce(runtime.nexloop_world_kind(p_world),'') not in ('simulation','shadow') then raise exception 'sim world: invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into w from control.nexloop_sim_worlds where world=p_world and tenant_id=p_tenant for update;
 if not found then raise exception 'sim world: not_registered' using errcode='22023';end if;
 if w.status='deleted' then
  return jsonb_build_object('world',p_world,'status','deleted','deleted',0,'remaining',0,'blocked','{}'::jsonb);end if;
 if w.status<>'deleting' then raise exception 'sim world: not_deleting' using errcode='22023';end if;
 -- Tables guarded by their own purge flag (0234 semantic access records) open for this transaction; the rows deleted are this
 -- world's only. Append-only tables that consult the erasure scope (0129) are opened per table below; any other guard refuses and
 -- its table is reported as blocked (the world stays deleting).
 perform set_config('nexloop.semantic_access_purge','on',true);
 for t in select * from runtime.nexloop_sim_world_tables() loop
  v_where:=' where x.world=$1'||case when runtime.nexloop_sim_world_has_tenant(t) then ' and x.tenant_id=$2' else '' end;
  if v_left>0 then
   begin
    perform runtime.nexloop_erasure_open(t::text,'{}',true,'sim-world:'||p_world);
    if (select relkind from pg_class where oid=t)='r' then
     execute format('delete from %s d where d.ctid in (select x.ctid from %s x%s limit $3)',t,t,v_where) using p_world,p_tenant,v_left;
    else
     execute format('delete from %s x%s',t,v_where) using p_world,p_tenant;
    end if;
    get diagnostics n=row_count;
    perform runtime.nexloop_erasure_close();
    v_total:=v_total+n;v_left:=v_left-n;
   exception when others then
    get stacked diagnostics v_state=returned_sqlstate,v_err=message_text;
    v_blocked:=v_blocked||jsonb_build_object(t::text,v_state);
   end;
  end if;
  execute format('select count(*) from %s x%s',t,v_where) into r using p_world,p_tenant;
  v_remaining:=v_remaining+r;
  if r=0 then v_blocked:=v_blocked-(t::text);end if;
 end loop;
 perform set_config('nexloop.semantic_access_purge','',true);
 update control.nexloop_sim_world_summaries set progress=jsonb_build_object('deleted',coalesce((progress->>'deleted')::bigint,0)+v_total,
   'remaining',v_remaining,'blocked',v_blocked,'at',runtime.nexloop_commercial_ts(clock_timestamp())),
   completed_at=case when v_remaining=0 then clock_timestamp() else null end
  where world=p_world;
 if v_remaining=0 then
  update control.nexloop_sim_worlds set status='deleted',deleted_at=clock_timestamp() where world=p_world;
 end if;
 return jsonb_build_object('world',p_world,'status',case when v_remaining=0 then 'deleted' else 'deleting' end,'deleted',v_total,'remaining',v_remaining,'blocked',v_blocked);
end $$;
alter function runtime.nexloop_sim_world_purge(text,text,integer) owner to nexloop_owner;
revoke all on function runtime.nexloop_sim_world_purge(text,text,integer) from public;

-- 3. The governed owner Actions -------------------------------------------------------------------------------------------------
create function control.nexloop_governed_create_sim_world(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_kind text:=body->>'kind';v_days integer;v_world text;v_hex text;w control.nexloop_sim_worlds;
begin
 if jsonb_typeof(body) is distinct from 'object' or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','kind','purpose','expires_in_days'))
  or v_kind is null or v_kind not in ('simulation','shadow') or jsonb_typeof(body->'purpose') is distinct from 'string'
  or char_length(btrim(body->>'purpose')) not between 1 and 500 or jsonb_typeof(body->'expires_in_days') is distinct from 'number'
  or (body->>'expires_in_days')!~'^[0-9]{1,3}$' or (body->>'expires_in_days')::integer not between 1 and 365
  or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200 then
  raise exception 'sim world: invalid' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'sim world: owner_world_only' using errcode='22023';end if;
 v_days:=(body->>'expires_in_days')::integer;
 perform set_config('eios.tenant_id',p_tenant,true);
 -- Idempotent per request: the same request gives the same world.
 select * into w from control.nexloop_sim_worlds where tenant_id=p_tenant and request_id=body->>'request_id';
 if found then
  if runtime.nexloop_world_kind(w.world)<>v_kind or w.purpose<>btrim(body->>'purpose') then raise exception 'sim world: request_reused' using errcode='22023';end if;
  return jsonb_build_object('world',w.world,'status',w.status,'expires_at',runtime.nexloop_commercial_ts(w.expires_at),'created',false);
 end if;
 v_hex:=md5('nexloop:sim-world:'||p_tenant||':'||(body->>'request_id'));
 v_world:=case v_kind when 'simulation' then 'sim:' else 'shadow:' end||substr(v_hex,1,8)||'-'||substr(v_hex,9,4)||'-4'||substr(v_hex,14,3)||'-8'||substr(v_hex,18,3)||'-'||substr(v_hex,21,12);
 insert into control.nexloop_sim_worlds(world,tenant_id,created_by,purpose,status,expires_at,request_id)
  values(v_world,p_tenant,p_principal,btrim(body->>'purpose'),'active',clock_timestamp()+make_interval(days=>v_days),body->>'request_id') returning * into w;
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('sim-world:create:'||v_world,'nexloop.sim_world.created',p_tenant,p_principal,'sim_world',p_intent,
   jsonb_build_object('world',v_world,'kind',v_kind,'expires_at',runtime.nexloop_commercial_ts(w.expires_at)),clock_timestamp());
 return jsonb_build_object('world',v_world,'status','active','expires_at',runtime.nexloop_commercial_ts(w.expires_at),'created',true);
end $$;
alter function control.nexloop_governed_create_sim_world(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_governed_create_sim_world(text,text,text,text,text,jsonb) from public;

-- Starts the deletion: summary first, then the world's credentials and Run credentials are revoked, then one bounded pass. Repeating
-- the Action (or the retention pass) continues it; a deleted world answers deleted.
create function control.nexloop_governed_delete_sim_world(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_target text:=body->>'world';w control.nexloop_sim_worlds;v_revoked integer;
begin
 if jsonb_typeof(body) is distinct from 'object' or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','world','reason'))
  or jsonb_typeof(body->'world') is distinct from 'string' or jsonb_typeof(body->'reason') is distinct from 'string'
  or char_length(btrim(body->>'reason')) not between 1 and 500 then
  raise exception 'sim world: invalid' using errcode='22023';end if;
 if runtime.nexloop_world_kind(v_target) is null or runtime.nexloop_world_kind(v_target) not in ('simulation','shadow') then
  raise exception 'sim world: not_a_simulation_world' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'sim world: owner_world_only' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into w from control.nexloop_sim_worlds where world=v_target and tenant_id=p_tenant for update;
 if not found then raise exception 'sim world: not_registered' using errcode='22023';end if;
 if w.status='active' then
  perform runtime.nexloop_sim_world_snapshot(p_tenant,v_target);
  update control.nexloop_sim_worlds set status='deleting' where world=v_target;
  insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
   values('sim-world:delete:'||v_target,'nexloop.sim_world.deleting',p_tenant,p_principal,'sim_world',p_intent,
    jsonb_build_object('world',v_target,'reason',btrim(body->>'reason')),clock_timestamp());
 end if;
 if w.status<>'deleted' then
  update authz.nexloop_service_credentials set status='revoked' where worlds=array[v_target] and status='active';
  get diagnostics v_revoked=row_count;
  update authz.nexloop_run_credentials set status='revoked' where world=v_target and status='active';
 end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 return runtime.nexloop_sim_world_purge(p_tenant,v_target,1000)||jsonb_build_object('credentials_revoked',coalesce(v_revoked,0));
end $$;
alter function control.nexloop_governed_delete_sim_world(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_governed_delete_sim_world(text,text,text,text,text,jsonb) from public;

insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('sim_world.create','create_sim_world','human','control.nexloop_governed_create_sim_world(text,text,text,text,text,jsonb)','NX-041'),
 ('sim_world.delete','delete_sim_world','human','control.nexloop_governed_delete_sim_world(text,text,text,text,text,jsonb)','NX-041');

-- Retention class sim_world_summary (settings v8) ----------------------------------------------------------------------------------
create or replace function control.nexloop_retention_settings_check() returns trigger
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare k text;v jsonb;
begin
 for k,v in select * from jsonb_each(new.definition->'classes') loop
  if k not in ('message_text','claim_quote','prompt_text','job_input','commercial_receipt','metric_observation','run_files','commitment_closed','claim_superseded',
   'plan_closed','effect_text','job_records','model_request_manifest','read_audit','governance_audit','semantic_access','semantic_release','evolution_records',
   'sim_world_summary')
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
 values(8,'{"classes":{"claim_quote":{"days":90,"source":"message_text"},"claim_superseded":{"days":365,"source":null},"commercial_receipt":{"days":90,"source":null},"commitment_closed":{"days":365,"source":null},"effect_text":{"days":90,"source":"message_text"},"evolution_records":{"days":365,"source":null},"governance_audit":{"days":365,"source":null},"job_input":{"days":30,"source":"message_text"},"job_records":{"days":365,"source":null},"message_text":{"days":90,"source":null},"metric_observation":{"days":365,"source":null},"model_request_manifest":{"days":365,"source":null},"plan_closed":{"days":365,"source":null},"prompt_text":{"days":30,"source":"message_text"},"read_audit":{"days":365,"source":null},"run_files":{"days":30,"source":null},"semantic_access":{"days":90,"source":null},"semantic_release":{"days":365,"source":null},"sim_world_summary":{"days":30,"source":null}},"decision":"NX-029 slice 1 (design docs/implementation/NX-029-design.md §3, owner decisions D1–D7 ''all as recommended'' 2026-10-10, dispatcher D8/D9): tenant-level retention (D9) for the classes the retention keeper executes in slice 1. Product defaults, not legal periods (03:96, 13:31); enterprises adjust by a new version. A derived class never outlives its source (prompt text, Claim quotes and job inputs follow the message text). Financial records and cost entries use financial_retention_days of the commercial settings (NX-027 D6), not a second value here. Expired message text, Claim quotes, prompt text and job inputs are redacted in place (rows, digests and order stay); commercial receipts, metric observations, commercial records and cost entries are deleted; every pass leaves a tombstone without content. v2 (NX-029 slice 4, NX-035 D4 confirmed by the owner 2026-10-10): records — deletion ledger and tombstones are kept 365 days, longer than the 35-day maximum backup retention, so every deletion still in a backup can be replayed after a restore (NX-035 §5, §6.2); a version with backup_max_retention_days not below both is refused. v3 (NX-029 final, AT-026): three expiry classes the keeper now executes - run_files: a Pi Run directory 30 days after its Run is terminal (queued for the Agent Host runs/purge, deleted only when the Host confirms; ADR-005, 06:92); commitment_closed: a Commitment 365 days after it was closed (fulfilled, cancelled or breached), object and ledger deleted (D4); claim_superseded: the structured value and remaining text of a superseded Claim 365 days after it was superseded, ids and the correction chain kept (D2). v4 (NX-029 completion): the remaining expiry classes of the design table - plan_closed: a plan 365 days after its last status became closed or invalidated, all versions, steps, events, Runs, outcomes and its unshared strategies deleted (D3); effect_text: the frozen request text of a terminal effect intent 90 days after it was created, states, receipts and digests kept (follows message_text; ADR-021); job_records: 365 days after a task became terminal its events, inbox and outbox rows and old idempotency records are deleted, the task row too unless a Run binding still refers to it (then its payloads are erased); model_request_manifest: model request rows and their prompt rows 365 days after the request (D3; cost entries keep their own financial retention); read_audit: read audit rows 365 days after the read, folded into per-day counts (D6); governance_audit: audit events, superseded control events (the latest per scope stays), human requests, export requests and completed deletion requests whose ledger entries are gone, 365 days after the event (D6). records: ledger entries and tombstones leave after deletion_ledger_days / tombstone_days, never while a restore replay is open; both must exceed backup_max_retention_days. v5 (NX-038 E3, owner D2 2026-10-12): semantic_access - the append-only per-access records of the Context Engine''s semantic browse/resolve (term hashes, refs and counts, no text) are deleted 90 days after they were recorded; a Consumer erasure deletes the records of that Consumer''s Runs when purging begins. v6 (NX-039 V4, owner D39-12 2026-10-20): semantic_release - semantic release records (with their items and rollback reports) and evaluation reports are deleted 365 days after they were recorded; the current release version and the one before it are always kept, and so is a report that a kept release or a review link still names. v7 (NX-039 V5, owner D39-12 2026-10-20): evolution_records - evaluation reports with their review links and case references 365 days after they were recorded; the reports of the current semantic release and the one before it are kept (the release publication path names them). (v6 on its own line; v6 here is semantic_release.) v8 (NX-041 S2, owner D2 2026-10-20): sim_world_summary - when a simulation or shadow world is deleted as a whole, only a summary of counts and digests (no content) is kept, 30 days after the deletion completed; a world still being deleted is resumed by the same pass.","records":{"backup_max_retention_days":35,"deletion_ledger_days":365,"tombstone_days":365},"schema":"nexloop-retention/1","sweep":{"batch":200,"interval_seconds":3600,"max_passes":20},"version":8}','9d1d0ded44364e25125a296d9c0060e52f207ee4788b513b9c3a2fcb4a6d4fea','migration:nx041-s2');

-- Worlds still deleting are continued (a bounded pass each), then summaries whose deletion completed before the cutoff leave.
create function runtime.nexloop_retention_sweep_sim_worlds(p_tenant text,p_world text,p_class text,p_batch integer,p_ref text,p_by text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_cut timestamptz;x record;n_resumed integer:=0;n_summaries integer:=0;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 v_cut:=clock_timestamp()-make_interval(days=>runtime.nexloop_retention_days(p_class));
 if p_class<>'sim_world_summary' or v_cut is null then raise exception 'retention class unavailable' using errcode='22023';end if;
 for x in select world from control.nexloop_sim_worlds where tenant_id=p_tenant and status='deleting' order by created_at limit 10 loop
  perform runtime.nexloop_sim_world_purge(p_tenant,x.world,p_batch);n_resumed:=n_resumed+1;
  perform set_config('eios.tenant_id',p_tenant,true);
 end loop;
 perform set_config('nexloop.sim_summary_retention','on',true);
 delete from control.nexloop_sim_world_summaries s where s.tenant_id=p_tenant and s.completed_at<v_cut
  and s.world in (select world from control.nexloop_sim_worlds where tenant_id=p_tenant and status='deleted' order by deleted_at limit p_batch);
 get diagnostics n_summaries=row_count;
 perform set_config('nexloop.sim_summary_retention','',true);
 if n_summaries>0 then
  perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,'retention_expiry',p_ref,p_class,'batch',p_class,'deleted',
   jsonb_build_object('summaries',n_summaries),p_by);
 end if;
 return jsonb_build_object('item_class',p_class,'processed',n_summaries,'more',n_summaries>=p_batch,
  'counts',jsonb_build_object('summaries',n_summaries,'worlds_resumed',n_resumed),'cutoff',runtime.nexloop_commercial_ts(v_cut));
end $$;
alter function runtime.nexloop_retention_sweep_sim_worlds(text,text,text,integer,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_retention_sweep_sim_worlds(text,text,text,integer,text,text) from public;

-- The hold wrapper (0250 body, replaced in place) dispatches the new class; holds are checked first as before.
create or replace function runtime.nexloop_retention_sweep(p_tenant text,p_world text,p_class text,p_batch integer,p_ref text,p_by text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if runtime.nexloop_retention_held(p_tenant,p_world,'item_class',p_class) then
  perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,'retention_expiry',p_ref,p_class,'batch',p_class,'skipped_hold','{}'::jsonb,p_by);
  return jsonb_build_object('item_class',p_class,'processed',0,'more',false,'counts','{}'::jsonb,'held',true);
 end if;
 if p_class='sim_world_summary' then
  return runtime.nexloop_retention_sweep_sim_worlds(p_tenant,p_world,p_class,p_batch,p_ref,p_by);
 end if;
 if p_class='evolution_records' then
  return runtime.nexloop_retention_sweep_evolution(p_tenant,p_world,p_class,p_batch,p_ref,p_by);
 end if;
 if p_class='semantic_release' then
  return runtime.nexloop_retention_sweep_semantic_release(p_tenant,p_world,p_class,p_batch,p_ref,p_by);
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

-- 5. Metrics export (S1 left the registered worlds to S2) ---------------------------------------------------------------------------
create function runtime.nexloop_sim_worlds_active(p_tenant text) returns setof text
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 return query select w.world from control.nexloop_sim_worlds w where w.tenant_id=p_tenant and w.status='active' order by w.world;
end $$;
alter function runtime.nexloop_sim_worlds_active(text) owner to nexloop_owner;
revoke all on function runtime.nexloop_sim_worlds_active(text) from public;
create or replace function authz.nexloop_metrics_export() returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if session_user is distinct from 'nexloop_metrics' then raise exception 'metrics export unavailable' using errcode='42501';end if;
 return coalesce((select jsonb_agg(runtime.nexloop_metrics_snapshot(x.tenant_id,x.world) order by x.tenant_id,x.world) from (
   select t.tenant_id,w.world from control.nexloop_tenants t cross join (values ('real'),('test')) w(world) where t.status='active'
   union all
   select t.tenant_id,s.world from control.nexloop_tenants t cross join lateral runtime.nexloop_sim_worlds_active(t.tenant_id) s(world) where t.status='active') x),'[]'::jsonb);
end $$;
