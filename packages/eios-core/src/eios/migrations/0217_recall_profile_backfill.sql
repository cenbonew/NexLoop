-- Recall vectors for what existed before the embedding profile, and embedding calls inside the tenant model budget (temporary
-- number 0217; owner 2026-10-18 option B: vector recall and candidate merge on stage; PR #8 review).
-- 1. 0093 queued every existing object once, at migration time, when stage had no embedding profile: objects and published
--    definitions were indexed for FTS and n-gram only, and activating a profile (0067) re-queues nothing.
--    control.nexloop_recall_backfill_profile (recall-index service, its own credential, tenant server side; active profile only),
--    called at every service start, re-queues into the recall-instance feed (op upsert, the item the 0093 triggers write) every
--    object of the indexed types that still lacks a vector of the profile: on the first call also those never indexed. A touch
--    revives a dead-lettered item (attempts 0), so a provider outage during the backfill is healed by the next start; nothing is
--    marked done for good. It also names the published object types whose definition entries lack vectors (all of them on the
--    first call): the service re-indexes those definitions (types, properties, vocabulary values) through index_object_type.
--    ontology.nexloop_recall_backfills records each call (first and last, counts). No business fact changes.
-- 2. authz.nexloop_budget_open(tenant model budget): configured, and open now (inside its period and not exhausted). The
--    embedding services check it before a tick claims any work: a budget outside its period or used up claims nothing.
create table ontology.nexloop_recall_backfills(
 tenant_id text not null,world text not null,profile_id text not null,
 first_at timestamptz not null default clock_timestamp(),last_at timestamptz not null default clock_timestamp(),
 runs integer not null default 1 check(runs>=1),last_queued integer not null check(last_queued>=0),
 last_definition_types integer not null check(last_definition_types>=0),primary key(tenant_id,world,profile_id)
);
alter table ontology.nexloop_recall_backfills owner to nexloop_owner;
alter table ontology.nexloop_recall_backfills enable row level security;
alter table ontology.nexloop_recall_backfills force row level security;
create policy recall_backfill_tenant on ontology.nexloop_recall_backfills to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on ontology.nexloop_recall_backfills from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;

create function control.nexloop_recall_backfill_profile(p_digest text,p_world text,p_profile text,p_types text[]) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=ontology.nexloop_recall_tenant(p_digest,p_world);n integer:=0;o record;first boolean;defs text[];
begin
 if session_user is distinct from 'nexloop_domain_worker' or p_types is null or cardinality(p_types) not between 1 and 64
  or exists(select 1 from unnest(p_types) x where x is null or x !~ '^[A-Za-z][A-Za-z0-9_]{0,63}$') then
  raise exception 'recall backfill invalid' using errcode='22023';end if;
 if not exists(select 1 from ontology.nexloop_recall_settings s where s.tenant_id=t and s.active_profile_id=p_profile) then
  raise exception 'recall backfill needs the active profile' using errcode='42501';end if;
 first:=not exists(select 1 from ontology.nexloop_recall_backfills where tenant_id=t and world=p_world and profile_id=p_profile);
 for o in select x.type_name,x.object_id from ontology.objects x where x.tenant_id=t and x.world=p_world and x.type_name=any(p_types)
   and (exists(select 1 from ontology.nexloop_recall_entries e where e.tenant_id=t and e.index_kind='instance' and e.world=p_world
          and e.source_key='object:'||p_world||'/'||x.type_name||'/'||x.object_id and e.field<>'strong_id'
          and not exists(select 1 from ontology.nexloop_recall_embeddings v where v.entry_id=e.entry_id and v.profile_id=p_profile))
        or (first and not exists(select 1 from ontology.nexloop_recall_entries e where e.tenant_id=t and e.index_kind='instance' and e.world=p_world
          and e.source_key='object:'||p_world||'/'||x.type_name||'/'||x.object_id))) loop
  perform authz.nexloop_work_feed_touch(t,p_world,'recall-instance',o.type_name||'/'||o.object_id,
   jsonb_build_object('type_name',o.type_name,'object_id',o.object_id,'op','upsert'));
  n:=n+1;
 end loop;
 select coalesce(array_agg(distinct v.type_name order by v.type_name),'{}') into defs from ontology.object_type_versions v where v.tenant_id=t
  and (first or exists(select 1 from ontology.nexloop_recall_entries e where e.tenant_id=t and e.index_kind='definition' and e.source_key='type:'||v.type_name
        and not exists(select 1 from ontology.nexloop_recall_embeddings w where w.entry_id=e.entry_id and w.profile_id=p_profile)));
 perform set_config('eios.tenant_id',t,true);
 insert into ontology.nexloop_recall_backfills(tenant_id,world,profile_id,last_queued,last_definition_types) values(t,p_world,p_profile,n,cardinality(defs))
  on conflict(tenant_id,world,profile_id) do update set last_at=clock_timestamp(),runs=ontology.nexloop_recall_backfills.runs+1,
   last_queued=excluded.last_queued,last_definition_types=excluded.last_definition_types;
 return jsonb_build_object('queued',n,'definition_types',to_jsonb(defs),'first',first);
end $$;
alter function control.nexloop_recall_backfill_profile(text,text,text,text[]) owner to nexloop_owner;
revoke all on function control.nexloop_recall_backfill_profile(text,text,text,text[]) from public;
grant execute on function control.nexloop_recall_backfill_profile(text,text,text,text[]) to nexloop_domain_worker;

create function authz.nexloop_budget_open(p_digest text,p_world text,p_kind text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;v_tenant text;lim control.nexloop_budget_limits%rowtype;v_used numeric;
begin
 ident:=control.nexloop_nx022_identity(p_digest,p_world,false);v_tenant:=ident->'binding'->>'tenant_id';
 select * into lim from control.nexloop_budget_limits where tenant_id=v_tenant and world=p_world and budget_kind=p_kind;
 if not found then return jsonb_build_object('budget_kind',p_kind,'configured',false,'open',false);end if;
 select coalesce(sum(amount),0) into v_used from control.nexloop_budget_consumption where tenant_id=v_tenant and world=p_world and budget_kind=p_kind
  and recorded_at>=lim.period_start and recorded_at<lim.period_end;
 return jsonb_build_object('budget_kind',p_kind,'configured',true,'unit',lim.unit,
  'open',clock_timestamp()>=lim.period_start and clock_timestamp()<lim.period_end and v_used<lim.limit_amount,
  'in_period',clock_timestamp()>=lim.period_start and clock_timestamp()<lim.period_end,'used',v_used::text,'limit_amount',lim.limit_amount::text);
end $$;
alter function authz.nexloop_budget_open(text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_budget_open(text,text,text) from public;
grant execute on function authz.nexloop_budget_open(text,text,text) to nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler;
