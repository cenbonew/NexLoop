-- Recall vectors for objects that existed before the embedding profile (temporary number 0217; owner 2026-10-18 option B:
-- vector recall and candidate merge on stage). 0093 queued every existing object once, at migration time, when stage had no
-- embedding profile: those objects were indexed for FTS and n-gram only, and activating a profile (0067) re-queues nothing,
-- so a full vector index never happened. control.nexloop_recall_backfill_profile: once per tenant, world and profile, the
-- recall-index service (its own credential; tenant server side) re-queues every object of the instance types it indexes into
-- the recall-instance feed (op upsert, the same item the 0093 triggers write). Only for the tenant's active profile; the
-- record says when and how many. Nothing is indexed here and no business fact changes.
create table ontology.nexloop_recall_backfills(
 tenant_id text not null,world text not null,profile_id text not null,items integer not null check(items>=0),
 backfilled_at timestamptz not null default clock_timestamp(),primary key(tenant_id,world,profile_id)
);
alter table ontology.nexloop_recall_backfills owner to nexloop_owner;
alter table ontology.nexloop_recall_backfills enable row level security;
alter table ontology.nexloop_recall_backfills force row level security;
create policy recall_backfill_tenant on ontology.nexloop_recall_backfills to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on ontology.nexloop_recall_backfills from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;

create function control.nexloop_recall_backfill_profile(p_digest text,p_world text,p_profile text,p_types text[]) returns integer
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=ontology.nexloop_recall_tenant(p_digest,p_world);n integer:=0;o record;
begin
 if session_user is distinct from 'nexloop_domain_worker' or p_types is null or cardinality(p_types) not between 1 and 64
  or exists(select 1 from unnest(p_types) x where x is null or x !~ '^[A-Za-z][A-Za-z0-9_]{0,63}$') then
  raise exception 'recall backfill invalid' using errcode='22023';end if;
 if not exists(select 1 from ontology.nexloop_recall_settings s where s.tenant_id=t and s.active_profile_id=p_profile) then
  raise exception 'recall backfill needs the active profile' using errcode='42501';end if;
 perform 1 from ontology.nexloop_recall_backfills where tenant_id=t and world=p_world and profile_id=p_profile;
 if found then return 0;end if;
 for o in select type_name,object_id from ontology.objects where tenant_id=t and world=p_world and type_name=any(p_types) loop
  perform authz.nexloop_work_feed_touch(t,p_world,'recall-instance',o.type_name||'/'||o.object_id,
   jsonb_build_object('type_name',o.type_name,'object_id',o.object_id,'op','upsert'));
  n:=n+1;
 end loop;
 perform set_config('eios.tenant_id',t,true);
 insert into ontology.nexloop_recall_backfills(tenant_id,world,profile_id,items) values(t,p_world,p_profile,n);
 return n;
end $$;
alter function control.nexloop_recall_backfill_profile(text,text,text,text[]) owner to nexloop_owner;
revoke all on function control.nexloop_recall_backfill_profile(text,text,text,text[]) from public;
grant execute on function control.nexloop_recall_backfill_profile(text,text,text,text[]) to nexloop_domain_worker;
