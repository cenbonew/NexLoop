-- NX-042 T2 (design §2, §5, D1, D4, D6, D7): the decision layer and seeded population sampling of a consumer twin in a simulation
-- world. The migration number lives only in the file name and the catalog; nothing below repeats it.
-- 1. authz.nexloop_twin_sample(digest, world, twin, version, seed, count): the simulation intake (nexloop_sim_api, the world's api
--    credential; real and test refused before any lookup) samples `count` twin instances of one published, not retired twin version
--    (T1) over the world's synthetic Consumers (S3). For every instance and behaviour the decision layer gives the version's interval
--    at its coverage (D4: never a single probability) and one simulated outcome: a probability drawn inside the interval and an
--    outcome drawn against it, both from sha256(seed | twin digest | label) - the drawn probability is internal and never stored or
--    returned. The same seed and twin version give byte-identical samples (NX-041 S3's draw); a repeated call returns the same run.
-- 2. D6: T2 runs the deterministic template generator only (generator twin-template/N); a twin whose generator needs a model is
--    refused (model_budget_required) - the simulation model budget (NX-041 D6) is checked by the slice that adds a model.
-- 3. D7: an uncalibrated twin runs; every run and sample carries the twin's calibration status at sampling time (from its latest
--    calibration event, T1; uncalibrated until T5), and NX-043
--    selects only through runtime.nexloop_consumer_twin_selectable (T1).
-- 4. Output lives in runtime.nexloop_twin_sample_runs / runtime.nexloop_twin_samples (sim:/shadow: only; data_mode simulation,
--    output_kind simulated_output, origin twin; append-only; they leave with the world, NX-041 D2). Never a Claim or a fact.

create table runtime.nexloop_twin_sample_runs (
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 world text not null check(coalesce(runtime.nexloop_world_kind(world),'') in ('simulation','shadow')),
 run_id text not null check(run_id~'^[0-9a-f]{64}$'),
 twin_id text not null,twin_version integer not null,twin_digest text not null check(twin_digest~'^[0-9a-f]{64}$'),
 seed text not null check(seed~'^[A-Za-z0-9_.-]{1,64}$'),
 instances integer not null check(instances between 1 and 1000),
 generator_version text not null,
 calibration_status text not null check(calibration_status in ('uncalibrated','calibrated')),
 output_digest text not null check(output_digest~'^[0-9a-f]{64}$'),
 data_mode text not null default 'simulation' check(data_mode='simulation'),
 requested_by text not null check(char_length(requested_by) between 1 and 200),
 created_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,run_id));
create table runtime.nexloop_twin_samples (
 tenant_id text not null,
 world text not null check(coalesce(runtime.nexloop_world_kind(world),'') in ('simulation','shadow')),
 run_id text not null,instance integer not null check(instance>=1),
 consumer_id text not null check(consumer_id~'^[0-9a-f]{64}$'),
 behaviors jsonb not null check(jsonb_typeof(behaviors)='object'),
 calibration_status text not null check(calibration_status in ('uncalibrated','calibrated')),
 data_mode text not null default 'simulation' check(data_mode='simulation'),
 output_kind text not null default 'simulated_output' check(output_kind='simulated_output'),
 origin text not null default 'twin' check(origin='twin'),
 primary key(tenant_id,world,run_id,instance),
 foreign key(tenant_id,world,run_id) references runtime.nexloop_twin_sample_runs(tenant_id,world,run_id));
do $$declare t text;begin
 foreach t in array array['runtime.nexloop_twin_sample_runs','runtime.nexloop_twin_samples'] loop
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy tenant_boundary on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on %s from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_stand_in_worker,nexloop_twin_port,nexloop_sim_worker,nexloop_sim_guard,nexloop_sim_api',t);
  -- Append-only; only the S2 whole deletion (its erasure scope) removes rows (S4 runtime.nexloop_sim_ledger_guard).
  execute format('create trigger nx042_twin_sample_guard before update or delete on %s for each row execute function runtime.nexloop_sim_ledger_guard()',t);
  execute format('create trigger nx042_twin_sample_no_truncate before truncate on %s for each statement execute function runtime.nexloop_sim_ledger_guard()',t);
 end loop;
end $$;

-- The decision layer of one instance: every behaviour's interval and one simulated outcome (the drawn probability stays inside).
create function runtime.nexloop_twin_decide(p_base text,p_instance integer,p_behaviors jsonb) returns jsonb
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select coalesce(jsonb_object_agg(b.key,jsonb_build_object('low',b.value->'low','high',b.value->'high','coverage',b.value->'coverage',
   'simulated_outcome',runtime.nexloop_sim_draw(p_base,'outcome:'||p_instance||':'||b.key)
     <(b.value->>'low')::numeric+((b.value->>'high')::numeric-(b.value->>'low')::numeric)*runtime.nexloop_sim_draw(p_base,'p:'||p_instance||':'||b.key)::numeric)),'{}'::jsonb)
 from jsonb_each(p_behaviors) b
$$;
alter function runtime.nexloop_twin_decide(text,integer,jsonb) owner to nexloop_owner;
revoke all on function runtime.nexloop_twin_decide(text,integer,jsonb) from public;

create function authz.nexloop_twin_sample(p_digest text,p_world text,p_twin text,p_version integer,p_seed text,p_count integer) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_cal text;v_tenant text;t control.nexloop_consumer_twins;r runtime.nexloop_twin_sample_runs;v_run text;v_base text;v_consumers text[];v_principal text;v_samples jsonb;
begin
 -- The simulation intake itself, for its own world (S6c app_role; real and test refused before any lookup by the tenant check).
 if session_user is distinct from 'nexloop_sim_api' or authz.nexloop_app_role(p_world) is distinct from 'nexloop_api' then
  raise exception 'twin sample: unavailable' using errcode='42501';end if;
 if coalesce(p_twin,'')!~'^[a-z][a-z0-9_.-]{0,63}$' or p_version is null or p_version<1 or coalesce(p_seed,'')!~'^[A-Za-z0-9_.-]{1,64}$'
  or p_count is null or p_count not between 1 and 1000 then raise exception 'twin sample: invalid' using errcode='22023';end if;
 v_tenant:=authz.nexloop_sim_effect_tenant(p_digest,p_world);
 perform set_config('eios.tenant_id',v_tenant,true);
 select * into t from control.nexloop_consumer_twins where tenant_id=v_tenant and twin_id=p_twin and version=p_version;
 if not found then raise exception 'twin sample: twin_unknown' using errcode='22023';end if;
 if runtime.nexloop_consumer_twin_status(v_tenant,p_twin,p_version) is distinct from 'published' then
  raise exception 'twin sample: twin_retired' using errcode='22023';end if;
 -- D6: no model in T2 - the deterministic template generator only.
 if t.generator_version!~'^twin-template/[0-9]+$' then raise exception 'twin sample: model_budget_required' using errcode='22023';end if;
 v_run:=encode(sha256(convert_to(p_world||'|'||p_twin||'|'||p_version||'|'||t.definition_digest||'|'||p_seed||'|'||p_count,'UTF8')),'hex');
 perform pg_advisory_xact_lock(hashtextextended('nexloop:twin-sample:'||v_run,0));
 select * into r from runtime.nexloop_twin_sample_runs where tenant_id=v_tenant and world=p_world and run_id=v_run;
 if not found then
  select array_agg(o.object_id order by o.object_id) into v_consumers from runtime.nexloop_sim_objects o
   where o.tenant_id=v_tenant and o.world=p_world and o.type_name='Consumer';
  if coalesce(cardinality(v_consumers),0)=0 then raise exception 'twin sample: no_synthetic_consumers' using errcode='22023';end if;
  -- The draws depend on the seed and the frozen twin definition only (not on the world): the same input reproduces byte for byte.
  v_base:=p_seed||'|'||t.definition_digest;
  v_cal:=runtime.nexloop_consumer_twin_calibration(v_tenant,p_twin,p_version);
  v_principal:=authz.nexloop_service_identity(p_digest,p_world)->'binding'->>'principal_id';
  -- The samples first (the run row is append-only, so its output digest is known before it is written). The digest covers what the
  -- samples say (instance, Consumer, behaviours, calibration), not the world or the run id.
  select jsonb_agg(jsonb_build_object('instance',i,'consumer_id',v_consumers[1+((i-1) % cardinality(v_consumers))],
    'behaviors',runtime.nexloop_twin_decide(v_base,i,t.definition->'parameters'->'behaviors')) order by i)
   into v_samples from generate_series(1,p_count) i;
  insert into runtime.nexloop_twin_sample_runs(tenant_id,world,run_id,twin_id,twin_version,twin_digest,seed,instances,generator_version,calibration_status,output_digest,requested_by)
   values(v_tenant,p_world,v_run,p_twin,p_version,t.definition_digest,p_seed,p_count,t.generator_version,v_cal,
    encode(sha256(convert_to((select string_agg((x->>'instance')||'|'||(x->>'consumer_id')||'|'||(x->'behaviors')::text||'|'||v_cal,',' order by (x->>'instance')::integer)
      from jsonb_array_elements(v_samples) x),'UTF8')),'hex'),coalesce(v_principal,'service'))
   returning * into r;
  insert into runtime.nexloop_twin_samples(tenant_id,world,run_id,instance,consumer_id,behaviors,calibration_status)
   select v_tenant,p_world,v_run,(x->>'instance')::integer,x->>'consumer_id',x->'behaviors',v_cal from jsonb_array_elements(v_samples) x;
 end if;
 return jsonb_build_object('run_id',r.run_id,'world',r.world,'twin_id',r.twin_id,'twin_version',r.twin_version,'seed',r.seed,'instances',r.instances,
  'calibration_status',r.calibration_status,'output_digest',r.output_digest,'data_mode','simulation',
  'samples',(select jsonb_agg(jsonb_build_object('schema','nexloop-twin-sample/1','world',s.world,'run_id',s.run_id,'twin_id',r.twin_id,'twin_version',r.twin_version,
     'seed',r.seed,'instance',s.instance,'consumer_id',s.consumer_id,'behaviors',s.behaviors,'calibration_status',s.calibration_status,
     'data_mode',s.data_mode,'output_kind',s.output_kind,'origin',s.origin) order by s.instance)
    from runtime.nexloop_twin_samples s where s.tenant_id=v_tenant and s.world=p_world and s.run_id=r.run_id));
end $$;
alter function authz.nexloop_twin_sample(text,text,text,integer,text,integer) owner to nexloop_owner;
revoke all on function authz.nexloop_twin_sample(text,text,text,integer,text,integer) from public;
grant execute on function authz.nexloop_twin_sample(text,text,text,integer,text,integer) to nexloop_sim_api;
