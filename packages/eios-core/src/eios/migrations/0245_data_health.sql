-- Data health (D-G5, owner decision as recommended; docs/implementation/eios-capability-gap.md D-G5): the health of NexLoop's own data
-- flows, not the upstream pipeline health. The migration number lives only in the file name and the catalog; nothing below repeats it.
-- 1. runtime.nexloop_data_health(tenant, world): one block per flow, each with counts, ratios, ages in seconds and a status
--    ('ok' | 'attention') with reason codes. Read only; never a message body, Claim value, recall text, embedding or payload.
--      extraction  the claim-extraction feed (not yet enqueued, oldest age), the claim-extraction queue by status, failures and
--                  dead letters, the failure ratio over the last 24 hours;
--      recall      the active embedding profile, entries, entries without an embedding under it (backfill needed), vectors under
--                  other profiles (profile drift), coverage, the last backfill;
--      reflow      review decisions whose Claims still wait for reflow (pending / waiting), the oldest age;
--      ingestion   message outbox not yet delivered; signed commercial receipts of the last 24 hours by outcome, rejected ratio,
--                  late events and the largest receive latency;
--      artifacts   available Artifacts referenced by no Context binding, Role Context or erasure record and older than a day
--                  (orphans), available Artifacts past retention, Artifacts stuck in 'deleting' for more than an hour;
--      erasure     Consumer erasures not finished, the oldest age, those waiting for unknown effects, purge items retried three
--                  times or more, erasures completed with holds.
-- 2. The metrics snapshot gains the block 'data_health' (the metrics export, the alert evaluator and the owner's metrics read see it);
--    alert rules v6 (deploy/configuration/alert-rules.v6.json) add thresholds on it.
-- 3. Workbench observe verb data_health (owner only), GET /api/v1/workbench/data-health.

create function runtime.nexloop_data_health(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare n timestamptz:=clock_timestamp();x jsonb;r jsonb;ig jsonb;a jsonb;e jsonb;rf jsonb;v_profile text;
 v_pending bigint;v_oldest numeric;v_jobs jsonb;v_failed24 bigint;v_done24 bigint;v_dead bigint;
 v_entries bigint;v_missing bigint;v_other bigint;v_last timestamptz;
 v_receipts jsonb;v_total24 bigint;v_rejected24 bigint;v_late24 bigint;v_latency numeric;v_outbox bigint;
 v_orphans bigint;v_expired bigint;v_stuck bigint;v_open bigint;v_open_oldest numeric;v_unknown bigint;v_retry bigint;v_holds bigint;
 v_rpending bigint;v_rwaiting bigint;v_roldest numeric;
 reasons text[];
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 -- Extraction.
 select count(*),extract(epoch from n-min(created_at)) into v_pending,v_oldest from runtime.nexloop_claim_extraction_feed
  where tenant_id=p_tenant and world=p_world and task_id is null;
 select coalesce(jsonb_object_agg(status,c),'{}'::jsonb) into v_jobs from (select status,count(*) c from runtime.jobs
  where tenant_id=p_tenant and world=p_world and queue='claim-extraction' group by status) q;
 select count(*) filter (where status in ('failed','dead_lettered')),count(*) filter (where status in ('succeeded','failed','dead_lettered'))
  into v_failed24,v_done24 from runtime.jobs where tenant_id=p_tenant and world=p_world and queue='claim-extraction' and updated_at>n-interval '24 hours';
 v_dead:=coalesce((v_jobs->>'dead_lettered')::bigint,0);
 reasons:=array[]::text[];
 if v_dead>0 then reasons:=reasons||'dead_letters'::text;end if;
 if coalesce((v_jobs->>'failed')::bigint,0)>0 then reasons:=reasons||'failed_jobs'::text;end if;
 if v_oldest>3600 then reasons:=reasons||'backlog_stale'::text;end if;
 if v_done24>=5 and v_failed24::numeric/v_done24>0.2 then reasons:=reasons||'failure_ratio_high'::text;end if;
 x:=jsonb_build_object('feed_pending',v_pending,'oldest_pending_seconds',floor(coalesce(v_oldest,0))::bigint,'jobs',v_jobs,'dead_lettered',v_dead,
  'failed_24h',v_failed24,'finished_24h',v_done24,'failure_ratio_24h',case when v_done24>0 then round(v_failed24::numeric/v_done24,4) end,
  'status',case when cardinality(reasons)=0 then 'ok' else 'attention' end,'reasons',to_jsonb(reasons));
 -- Recall index.
 select active_profile_id into v_profile from ontology.nexloop_recall_settings where tenant_id=p_tenant;
 select count(*),count(*) filter (where v_profile is null or not exists(select 1 from ontology.nexloop_recall_embeddings m where m.entry_id=e2.entry_id and m.profile_id=v_profile))
  into v_entries,v_missing from ontology.nexloop_recall_entries e2 where e2.tenant_id=p_tenant and (e2.world is null or e2.world=p_world);
 select count(*) into v_other from ontology.nexloop_recall_embeddings m join ontology.nexloop_recall_entries e3 on e3.entry_id=m.entry_id
  where v_profile is not null and m.tenant_id=p_tenant and (e3.world is null or e3.world=p_world) and m.profile_id<>v_profile;
 select max(last_at) into v_last from ontology.nexloop_recall_backfills where tenant_id=p_tenant and world=p_world;
 reasons:=array[]::text[];
 if v_entries>0 and v_profile is null then reasons:=reasons||'no_active_profile'::text;end if;
 if v_missing>0 then reasons:=reasons||'missing_embeddings'::text;end if;
 if v_other>0 then reasons:=reasons||'profile_drift'::text;end if;
 r:=jsonb_build_object('active_profile',v_profile,'entries',v_entries,'missing_embeddings',v_missing,'other_profile_vectors',v_other,
  'coverage_ratio',case when v_entries>0 then round((v_entries-v_missing)::numeric/v_entries,4) end,'last_backfill_at',v_last,
  'status',case when cardinality(reasons)=0 then 'ok' else 'attention' end,'reasons',to_jsonb(reasons));
 -- Review reflow.
 select count(*) filter (where reflow_status='pending'),count(*) filter (where reflow_status='waiting'),extract(epoch from n-min(decided_at))
  into v_rpending,v_rwaiting,v_roldest from ontology.nexloop_review_decisions where tenant_id=p_tenant and world=p_world and reflow_status in ('pending','waiting');
 reasons:=array[]::text[];
 if v_roldest>3600 then reasons:=reasons||'reflow_stale'::text;end if;
 rf:=jsonb_build_object('pending',v_rpending,'waiting',v_rwaiting,'oldest_seconds',floor(coalesce(v_roldest,0))::bigint,
  'status',case when cardinality(reasons)=0 then 'ok' else 'attention' end,'reasons',to_jsonb(reasons));
 -- Ingestion.
 select count(*) into v_outbox from runtime.nexloop_message_outbox where tenant_id=p_tenant and world=p_world and status='pending';
 select coalesce(jsonb_object_agg(outcome,c),'{}'::jsonb),coalesce(sum(c),0),coalesce(sum(c) filter (where outcome in ('rejected_signature','rejected_schema','too_large','replay_window')),0)
  into v_receipts,v_total24,v_rejected24 from (select outcome,count(*) c from runtime.nexloop_commercial_receipts
  where tenant_id=p_tenant and world=p_world and received_at>n-interval '24 hours' group by outcome) q;
 select count(*) filter (where disposition='late'),max(extract(epoch from received_at-occurred_at)) into v_late24,v_latency
  from runtime.nexloop_commercial_events where tenant_id=p_tenant and world=p_world and received_at>n-interval '24 hours';
 reasons:=array[]::text[];
 if coalesce((v_receipts->>'rejected_signature')::bigint,0)>0 then reasons:=reasons||'signature_rejections'::text;end if;
 if v_total24>=5 and v_rejected24::numeric/v_total24>0.1 then reasons:=reasons||'rejected_ratio_high'::text;end if;
 if v_outbox>50 then reasons:=reasons||'outbox_backlog'::text;end if;
 ig:=jsonb_build_object('outbox_pending',v_outbox,'commercial_receipts_24h',v_receipts,'commercial_rejected_24h',v_rejected24,
  'commercial_rejected_ratio_24h',case when v_total24>0 then round(v_rejected24::numeric/v_total24,4) end,'commercial_late_24h',v_late24,
  'commercial_max_latency_seconds_24h',floor(greatest(coalesce(v_latency,0),0))::bigint,
  'status',case when cardinality(reasons)=0 then 'ok' else 'attention' end,'reasons',to_jsonb(reasons));
 -- Artifacts.
 select count(*) filter (where l.status='available' and l.created_at<n-interval '1 day'
    and not exists(select 1 from runtime.nexloop_context_artifact_bindings b where b.tenant_id=l.tenant_id and b.world=l.world and b.artifact_id=l.artifact_id)
    and not exists(select 1 from runtime.nexloop_fallback_context_bindings b where b.tenant_id=l.tenant_id and b.world=l.world and b.artifact_id=l.artifact_id)
    and not exists(select 1 from runtime.nexloop_role_context_artifacts b where b.tenant_id=l.tenant_id and b.world=l.world and b.artifact_id=l.artifact_id)
    and not exists(select 1 from runtime.nexloop_erasure_artifacts b where b.tenant_id=l.tenant_id and b.world=l.world and b.artifact_id=l.artifact_id)),
  count(*) filter (where l.status='available' and l.retention_until<n),
  count(*) filter (where l.status='deleting' and l.updated_at<n-interval '1 hour')
  into v_orphans,v_expired,v_stuck from runtime.nexloop_local_artifacts l where l.tenant_id=p_tenant and l.world=p_world;
 reasons:=array[]::text[];
 if v_orphans>0 then reasons:=reasons||'orphans'::text;end if;
 if v_expired>0 then reasons:=reasons||'retention_passed'::text;end if;
 if v_stuck>0 then reasons:=reasons||'stuck_deleting'::text;end if;
 a:=jsonb_build_object('orphans',v_orphans,'retention_passed',v_expired,'stuck_deleting',v_stuck,
  'status',case when cardinality(reasons)=0 then 'ok' else 'attention' end,'reasons',to_jsonb(reasons));
 -- Erasure propagation.
 select count(*) filter (where state not in ('completed','completed_with_holds')),extract(epoch from n-min(created_at) filter (where state not in ('completed','completed_with_holds'))),
  count(*) filter (where state='waiting_unknown_effects'),count(*) filter (where state='completed_with_holds')
  into v_open,v_open_oldest,v_unknown,v_holds from control.nexloop_consumer_erasures where tenant_id=p_tenant and world=p_world;
 select count(*) into v_retry from runtime.nexloop_run_purge_items where tenant_id=p_tenant and world=p_world and state='pending' and attempts>=3;
 reasons:=array[]::text[];
 if v_open_oldest>86400 then reasons:=reasons||'erasure_stalled'::text;end if;
 if v_unknown>0 then reasons:=reasons||'waiting_unknown_effects'::text;end if;
 if v_retry>0 then reasons:=reasons||'purge_retrying'::text;end if;
 e:=jsonb_build_object('open',v_open,'oldest_open_seconds',floor(coalesce(v_open_oldest,0))::bigint,'waiting_unknown_effects',v_unknown,
  'purge_items_retrying',v_retry,'completed_with_holds',v_holds,
  'status',case when cardinality(reasons)=0 then 'ok' else 'attention' end,'reasons',to_jsonb(reasons));
 return jsonb_build_object('computed_at',n,'extraction',x,'recall',r,'reflow',rf,'ingestion',ig,'artifacts',a,'erasure',e,
  'attention',(select count(*) from jsonb_array_elements(jsonb_build_array(x,r,rf,ig,a,e)) s where s->>'status'='attention'));
end $$;
alter function runtime.nexloop_data_health(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_data_health(text,text) from public;

-- 2. The metrics snapshot (latest wrapper kept, one block added) ---------------------------------------------------------------------
alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_data_health;
revoke all on function runtime.nexloop_metrics_snapshot_before_data_health(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 return runtime.nexloop_metrics_snapshot_before_data_health(p_tenant,p_world)||jsonb_build_object('data_health',runtime.nexloop_data_health(p_tenant,p_world));
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;

-- 3. Workbench observe verb data_health (owner only; every other verb unchanged) -------------------------------------------------------
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) rename to nexloop_workbench_observe_read_before_data_health;
revoke all on function authz.nexloop_workbench_observe_read_before_data_health(text,text,text,text,text) from public,nexloop_api;
create function authz.nexloop_workbench_observe_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;w jsonb;cfg control.nexloop_workbench_configurations;v_tenant text;
 v_action text:='eios:action:nexloop.workbench.read:1';
begin
 if c->>'verb' is distinct from 'data_health' then
  return authz.nexloop_workbench_observe_read_before_data_health(p_digest,p_world,p_text,p_signature,p_payload);end if;
 -- The 0133 verification, for this verb (as the earlier verbs).
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
 if (select count(*) from jsonb_object_keys(c))<>1 then raise exception 'workbench observe invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 -- Counts, ratios, times and codes only (runtime.nexloop_data_health).
 return runtime.nexloop_data_health(v_tenant,p_world);
end $$;
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_observe_read(text,text,text,text,text) from public;
grant execute on function authz.nexloop_workbench_observe_read(text,text,text,text,text) to nexloop_api;
