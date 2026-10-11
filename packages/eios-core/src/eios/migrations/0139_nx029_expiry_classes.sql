-- NX-029 final, part 2 (temporary number 0141): the three expiry classes AT-026 still lacked, executed by the retention
-- keeper through its existing sweep port. docs/implementation/NX-029-design.md §3 table, owner D2/D4, dispatcher 2026-10-11.
--
-- 1. Settings v3 (retention.v1.json): run_files 30 days after the Run is terminal, commitment_closed 365 days after
--    closing, claim_superseded 365 days after supersession. The settings check accepts the three names.
-- 2. run_files: Runs whose task is terminal (or role Runs past their binding expiry with no live task) before the cutoff
--    are queued in runtime.nexloop_run_purge_items (reason retention_expiry); the keeper hands them to the Agent Host
--    runs/purge exactly as for an erasure, and only a confirmed purge (purged/absent) writes the tombstone - now with the
--    item's own reason, so an expiry purge enters the deletion ledger as class run_files (erasure_command body replaced).
-- 3. commitment_closed (D4): a Commitment closed (fulfilled, cancelled, breached) before the cutoff - closing time is its
--    last status event - leaves with its registry row, events, evidence, exceptions, commercial bindings, monitor feed
--    item, object events and relations; one tombstone each. The 0111/0132 guards admit only a registered erasure.
-- 4. claim_superseded (D2): ontology.nexloop_claims.superseded_at is stamped when a Claim becomes superseded (existing
--    superseded Claims get the migration time: their year starts now). Past the cutoff the structured value becomes
--    erased and the remaining text is cleared; the row, its ids and the correction chain stay (no row deletion: proposals,
--    matches, reviews and extraction runs refer to it).
-- Holds apply as for every class (the 0132 wrapper checks them first). Published migrations are not edited.

-- 1. Settings v3 ----------------------------------------------------------------------------------------------------------
create or replace function control.nexloop_retention_settings_check() returns trigger
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare k text;v jsonb;
begin
 for k,v in select * from jsonb_each(new.definition->'classes') loop
  if k not in ('message_text','claim_quote','prompt_text','job_input','commercial_receipt','metric_observation','run_files','commitment_closed','claim_superseded')
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
 values(3,'{"classes":{"claim_quote":{"days":90,"source":"message_text"},"claim_superseded":{"days":365,"source":null},"commercial_receipt":{"days":90,"source":null},"commitment_closed":{"days":365,"source":null},"job_input":{"days":30,"source":"message_text"},"message_text":{"days":90,"source":null},"metric_observation":{"days":365,"source":null},"prompt_text":{"days":30,"source":"message_text"},"run_files":{"days":30,"source":null}},"decision":"NX-029 slice 1 (design docs/implementation/NX-029-design.md §3, owner decisions D1–D7 ''all as recommended'' 2026-10-10, dispatcher D8/D9): tenant-level retention (D9) for the classes the retention keeper executes in slice 1. Product defaults, not legal periods (03:96, 13:31); enterprises adjust by a new version. A derived class never outlives its source (prompt text, Claim quotes and job inputs follow the message text). Financial records and cost entries use financial_retention_days of the commercial settings (NX-027 D6), not a second value here. Expired message text, Claim quotes, prompt text and job inputs are redacted in place (rows, digests and order stay); commercial receipts, metric observations, commercial records and cost entries are deleted; every pass leaves a tombstone without content. v2 (NX-029 slice 4, NX-035 D4 confirmed by the owner 2026-10-10): records — deletion ledger and tombstones are kept 365 days, longer than the 35-day maximum backup retention, so every deletion still in a backup can be replayed after a restore (NX-035 §5, §6.2); a version with backup_max_retention_days not below both is refused. v3 (NX-029 final, AT-026): three expiry classes the keeper now executes - run_files: a Pi Run directory 30 days after its Run is terminal (queued for the Agent Host runs/purge, deleted only when the Host confirms; ADR-005, 06:92); commitment_closed: a Commitment 365 days after it was closed (fulfilled, cancelled or breached), object and ledger deleted (D4); claim_superseded: the structured value and remaining text of a superseded Claim 365 days after it was superseded, ids and the correction chain kept (D2).","records":{"backup_max_retention_days":35,"deletion_ledger_days":365,"tombstone_days":365},"schema":"nexloop-retention/1","sweep":{"batch":200,"interval_seconds":3600,"max_passes":20},"version":3}',
  '84e6fd5aa5cfec22823f6406feb4dd945ba51f9b44e991cc0243d2deffc7c920','migration:0141');

-- 4a. When a Claim was superseded --------------------------------------------------------------------------------------
alter table ontology.nexloop_claims add column superseded_at timestamptz;
create function ontology.nexloop_claim_superseded_at() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if new.resolution_state='superseded' and old.resolution_state is distinct from 'superseded' then new.superseded_at:=clock_timestamp();
 elsif new.resolution_state<>'superseded' then new.superseded_at:=null;end if;
 return new;
end $$;
alter function ontology.nexloop_claim_superseded_at() owner to nexloop_owner;
revoke all on function ontology.nexloop_claim_superseded_at() from public;
create trigger nx029_claim_superseded_at before update of resolution_state on ontology.nexloop_claims for each row execute function ontology.nexloop_claim_superseded_at();
update ontology.nexloop_claims set superseded_at=clock_timestamp() where resolution_state='superseded' and superseded_at is null;
create index nexloop_claims_superseded on ontology.nexloop_claims(tenant_id,world,superseded_at) where resolution_state='superseded';

-- 3a. Commitment guards admit a registered erasure (0132 / 0111 bodies, one branch each) ---------------------------------
-- The object guard runs as the owner: the transaction-local, owner-only erasure scope is what admits a deletion.
create or replace function ontology.nexloop_commitment_guard() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);r runtime.nexloop_commitments;patch jsonb;
begin
 if tg_op='DELETE' then
  if old.type_name='Commitment' and runtime.nexloop_erasure_permits('nexloop_owner','ontology.objects','DELETE',to_jsonb(old),null) then return old;end if;
  if old.type_name='Commitment' then raise exception 'a Commitment is never deleted' using errcode='42501';end if;
  return old;
 end if;
 if new.type_name<>'Commitment' and (tg_op='INSERT' or old.type_name<>'Commitment') then return new;end if;
 if tg_op='UPDATE' and (old.type_name<>'Commitment' or new.type_name<>'Commitment' or new.object_id<>old.object_id
   or new.tenant_id<>old.tenant_id or new.world<>old.world) then raise exception 'commitment identity is immutable' using errcode='42501';end if;
 if tg_op='UPDATE' and exists(select 1 from control.nexloop_erasure_scope s where s.txid=txid_current() and s.table_name='ontology.objects' and 'commitment_erasure'=any(s.columns))
  and (new.properties-'made_to'-'condition')=(old.properties-'made_to'-'condition') and new.properties->>'made_to'~'^erasure:[A-Za-z0-9._:-]{1,190}$'
  and coalesce(new.properties->>'condition','')='' then
  return new;
 end if;
 perform set_config('eios.tenant_id',new.tenant_id,true);
 if tg_op='INSERT' then
  select * into r from runtime.nexloop_commitments c where c.tenant_id=new.tenant_id and c.world=new.world and c.commitment_id=new.object_id;
  if not found or r.intent_id is distinct from new.source_ref or r.properties is distinct from new.properties or r.registered_at is not null then
   raise exception 'commitment creation was not prepared from its evidence' using errcode='42501';end if;
 elsif new.properties is distinct from old.properties then
  patch:=runtime.nexloop_commitment_target(new.tenant_id,new.world,new.object_id,old.properties,clock_timestamp());
  if patch='{}'::jsonb or new.properties is distinct from old.properties||patch then
   raise exception 'commitment transition is not justified by its evidence' using errcode='23514';end if;
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return new;
end $$;

create or replace function runtime.nexloop_commitment_registry_guard() returns trigger
 language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if tg_op='DELETE' and runtime.nexloop_erasure_permits(current_user::text,'runtime.nexloop_commitments','DELETE',to_jsonb(old),null) then return old;end if;
 if tg_op='DELETE' or old.registered_at is not null or new.registered_at is null
  or (to_jsonb(new)-'registered_at') is distinct from (to_jsonb(old)-'registered_at') then
  raise exception 'commitment registry is immutable' using errcode='42501';end if;
 return new;
end $$;

-- 2-4. The three classes (one bounded batch per call; the caller is the keeper's sweep port) -----------------------------
create function runtime.nexloop_retention_sweep_expiry(p_tenant text,p_world text,p_class text,p_batch integer,p_ref text,p_by text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_cut timestamptz:=clock_timestamp()-make_interval(days=>runtime.nexloop_retention_days(p_class));n integer:=0;m integer:=0;x record;
 v_counts jsonb:='{}'::jsonb;v_terminal text[]:=array['succeeded','failed','dead_lettered','cancelled'];
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 if v_cut is null then raise exception 'retention class unavailable' using errcode='22023';end if;
 if p_class='run_files' then
  -- Queued only; the directory is gone when the Host confirms (erasure_command run_purged writes the tombstone).
  insert into runtime.nexloop_run_purge_items(tenant_id,world,run_id,request_ref,reason)
   select p_tenant,p_world,d.run_id,p_ref,'retention_expiry' from (
     select b.run_id from authz.nexloop_runtime_run_bindings b join runtime.jobs j on j.tenant_id=b.tenant_id and j.job_id=b.task_id
      where b.tenant_id=p_tenant and b.world=p_world and j.status=any(v_terminal) and j.updated_at<v_cut
     union
     select r.run_id from authz.nexloop_role_run_bindings r where r.tenant_id=p_tenant and r.world=p_world and r.expires_at<v_cut
      and not exists(select 1 from authz.nexloop_runtime_run_bindings b join runtime.jobs j on j.tenant_id=b.tenant_id and j.job_id=b.task_id
       where b.run_id=r.run_id and j.status<>all(v_terminal))) d
   where not exists(select 1 from runtime.nexloop_run_purge_items i where i.tenant_id=p_tenant and i.world=p_world and i.run_id=d.run_id)
   order by d.run_id limit p_batch on conflict do nothing;
  get diagnostics n=row_count;v_counts:=jsonb_build_object('runs_queued',n);
 elsif p_class='commitment_closed' then
  perform runtime.nexloop_erasure_open('ontology.objects',null,true,p_ref);
  perform runtime.nexloop_erasure_open('runtime.nexloop_commitments',null,true,p_ref);
  perform runtime.nexloop_erasure_open('runtime.nexloop_commitment_events',null,true,p_ref);
  perform runtime.nexloop_erasure_open('runtime.nexloop_commitment_evidence',null,true,p_ref);
  perform runtime.nexloop_erasure_open('runtime.nexloop_commitment_exceptions',null,true,p_ref);
  perform runtime.nexloop_erasure_open('runtime.nexloop_commercial_commitment_bindings',null,true,p_ref);
  for x in select o.object_id from ontology.objects o where o.tenant_id=p_tenant and o.world=p_world and o.type_name='Commitment'
    and o.properties->>'status' in ('fulfilled','cancelled','breached')
    and coalesce((select max(v.recorded_at) from runtime.nexloop_commitment_events v where v.tenant_id=o.tenant_id and v.world=o.world
      and v.subject_ref='commitment:'||o.object_id and v.kind='status'),o.updated_at)<v_cut
    order by o.object_id limit p_batch for update of o skip locked loop
   delete from runtime.nexloop_commitment_events where tenant_id=p_tenant and world=p_world and subject_ref='commitment:'||x.object_id;
   get diagnostics m=row_count;
   delete from runtime.nexloop_commitment_evidence where tenant_id=p_tenant and world=p_world and commitment_id=x.object_id;
   delete from runtime.nexloop_commitment_exceptions where tenant_id=p_tenant and world=p_world and subject_ref='commitment:'||x.object_id;
   delete from runtime.nexloop_commercial_commitment_bindings where tenant_id=p_tenant and world=p_world and commitment_id=x.object_id;
   delete from runtime.nexloop_work_feed where tenant_id=p_tenant and world=p_world and feed='commitment-monitor' and item_key='commitment:'||x.object_id;
   delete from ontology.events where tenant_id=p_tenant and object_type='Commitment' and object_id=x.object_id;
   delete from ontology.relations where tenant_id=p_tenant and ((source_type='Commitment' and source_object_id=x.object_id) or (target_type='Commitment' and target_object_id=x.object_id));
   delete from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='Commitment' and object_id=x.object_id;
   delete from runtime.nexloop_commitments where tenant_id=p_tenant and world=p_world and commitment_id=x.object_id;
   perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,'retention_expiry',p_ref,'commitment_closed','commitment',x.object_id,'deleted',
    jsonb_build_object('events',m),p_by);
   n:=n+1;
  end loop;
  v_counts:=jsonb_build_object('commitments',n);
 elsif p_class='claim_superseded' then
  with due as (select claim_id from ontology.nexloop_claims where tenant_id=p_tenant and world=p_world and resolution_state='superseded'
    and superseded_at<v_cut and not (value ? 'erased') order by superseded_at,claim_id limit p_batch for update skip locked)
  update ontology.nexloop_claims c set quote='',subject_text='',condition_text='',time_expression='',value='{"type":"erased","value":null,"erased":true}'::jsonb
   from due where c.tenant_id=p_tenant and c.world=p_world and c.claim_id=due.claim_id;
  get diagnostics n=row_count;v_counts:=jsonb_build_object('claims',n);
  if n>0 then
   perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,'retention_expiry',p_ref,'claim_superseded','batch','claim_superseded','redacted',v_counts,p_by);
  end if;
 else
  raise exception 'retention class unavailable' using errcode='22023';
 end if;
 perform runtime.nexloop_erasure_close();
 return jsonb_build_object('item_class',p_class,'processed',n,'more',n>=p_batch,'counts',v_counts,'cutoff',runtime.nexloop_commercial_ts(v_cut));
end $$;
alter function runtime.nexloop_retention_sweep_expiry(text,text,text,integer,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_retention_sweep_expiry(text,text,text,integer,text,text) from public;
-- The 0132 hold wrapper dispatches them (body replaced in place; holds checked first as before).
create or replace function runtime.nexloop_retention_sweep(p_tenant text,p_world text,p_class text,p_batch integer,p_ref text,p_by text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if runtime.nexloop_retention_held(p_tenant,p_world,'item_class',p_class) then
  perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,'retention_expiry',p_ref,p_class,'batch',p_class,'skipped_hold','{}'::jsonb,p_by);
  return jsonb_build_object('item_class',p_class,'processed',0,'more',false,'counts','{}'::jsonb,'held',true);
 end if;
 if p_class in ('run_files','commitment_closed','claim_superseded') then
  return runtime.nexloop_retention_sweep_expiry(p_tenant,p_world,p_class,p_batch,p_ref,p_by);
 end if;
 return runtime.nexloop_retention_sweep_before_holds_v0142(p_tenant,p_world,p_class,p_batch,p_ref,p_by);
end $$;

-- 2b. A confirmed purge records the item's own reason (0132 body; an expiry purge is class run_files in the ledger) ------
create or replace function authz.nexloop_erasure_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-erasure-v1',
  'eios:action:nexloop.erasure.execute:1',array['nexloop_domain_worker']);c jsonb:=p_payload::jsonb;v jsonb;v_by text:='service:'||((p_text::jsonb)->>'principal_id');
 art runtime.nexloop_local_artifacts;n integer;v_limit integer;v_ref text;v_reason text;
begin
 perform set_config('eios.tenant_id',t,true);
 v_limit:=coalesce((c->>'limit')::integer,50);
 if v_limit not between 1 and 200 then raise exception 'erasure command invalid' using errcode='22023';end if;
 if c->>'verb'='requests' then
  select coalesce(jsonb_agg(request_id order by created_at),'[]'::jsonb) into v from (select request_id,created_at from control.nexloop_consumer_erasures
   where tenant_id=t and world=p_world and state in ('blocking','settling','waiting_unknown_effects','purging') order by created_at limit v_limit) r;
  return jsonb_build_object('requests',v);
 elsif c->>'verb'='step' then
  if coalesce(c->>'request_id','')!~'^[A-Za-z0-9._:-]{1,190}$' then raise exception 'erasure command invalid' using errcode='22023';end if;
  return runtime.nexloop_erasure_step(t,p_world,c->>'request_id',(runtime.nexloop_retention_settings_current()->'sweep'->>'batch')::integer,v_by);
 elsif c->>'verb'='run_purges' then
  -- Only Runs whose task is terminal (or that have none); the Host refuses an active Run anyway.
  select coalesce(jsonb_agg(p.run_id order by p.created_at),'[]'::jsonb) into v from (select i.run_id,i.created_at from runtime.nexloop_run_purge_items i
   where i.tenant_id=t and i.world=p_world and i.state='pending'
    and not exists(select 1 from authz.nexloop_runtime_run_bindings b join runtime.jobs j on j.tenant_id=b.tenant_id and j.job_id=b.task_id
     where b.tenant_id=t and b.run_id=i.run_id and j.status not in ('succeeded','failed','dead_lettered','cancelled'))
   order by i.created_at limit v_limit) p;
  return jsonb_build_object('runs',v);
 elsif c->>'verb'='run_purged' then
  if coalesce(c->>'run_id','')!~'^[0-9a-f-]{36}$' or c->>'outcome' not in ('purged','absent','failed') then raise exception 'erasure command invalid' using errcode='22023';end if;
  if c->>'outcome'='failed' then
   update runtime.nexloop_run_purge_items set attempts=attempts+1,last_code='host_purge_failed' where tenant_id=t and world=p_world and run_id=(c->>'run_id')::uuid and state='pending';
   return jsonb_build_object('run_id',c->>'run_id','state','pending');
  end if;
  update runtime.nexloop_run_purge_items set state='purged',purged_at=clock_timestamp() where tenant_id=t and world=p_world and run_id=(c->>'run_id')::uuid and state='pending'
   returning request_ref,reason into v_ref,v_reason;
  if v_ref is not null then
   perform runtime.nexloop_erasure_tombstone(t,p_world,v_reason,v_ref,case when v_reason='retention_expiry' then 'run_files' else 'run_file' end,'pi_run',c->>'run_id','deleted',
    jsonb_build_object('outcome',c->>'outcome'),v_by);
   insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
    values('run-purge:'||(c->>'run_id'),'nexloop.erasure.run_purged',t,v_by,'erasure',v_ref,jsonb_build_object('run_id',c->>'run_id','outcome',c->>'outcome'),clock_timestamp())
    on conflict do nothing;
  end if;
  return jsonb_build_object('run_id',c->>'run_id','state','purged');
 elsif c->>'verb'='artifact_candidates' then
  select coalesce(jsonb_agg(x.artifact_id order by x.artifact_id),'[]'::jsonb) into v from (select a.artifact_id from runtime.nexloop_local_artifacts a
   where a.tenant_id=t and a.world=p_world and a.status in ('available','deleting') and runtime.nexloop_artifact_purgeable(t,p_world,a.artifact_id)
   order by a.artifact_id limit v_limit) x;
  return jsonb_build_object('artifacts',v);
 elsif c->>'verb'='artifact_claim' then
  if coalesce(c->>'artifact_id','')!~'^[a-f0-9]{32}$' or coalesce(c->>'worker','')!~'^[a-f0-9]{32}$' then raise exception 'erasure command invalid' using errcode='22023';end if;
  select * into art from runtime.nexloop_local_artifacts where tenant_id=t and world=p_world and artifact_id=c->>'artifact_id' for update;
  -- Re-verified here, whatever the keeper computed.
  if not found or not runtime.nexloop_artifact_purgeable(t,p_world,art.artifact_id) then raise exception 'artifact not purgeable' using errcode='42501';end if;
  update runtime.nexloop_local_artifacts set status='deleting',upload_worker=c->>'worker',upload_token=encode(extensions.gen_random_bytes(24),'hex'),
   upload_fence=upload_fence+1,lease_until=clock_timestamp()+interval '30 seconds',updated_at=clock_timestamp()
   where tenant_id=t and world=p_world and artifact_id=art.artifact_id returning * into art;
  return jsonb_build_object('artifact_id',art.artifact_id,'sha256',art.sha256,'size_bytes',art.size_bytes,'media_type',art.media_type,'fence',art.upload_fence);
 elsif c->>'verb'='artifact_deleted' then
  if coalesce(c->>'artifact_id','')!~'^[a-f0-9]{32}$' or coalesce(c->>'worker','')!~'^[a-f0-9]{32}$' then raise exception 'erasure command invalid' using errcode='22023';end if;
  select * into art from runtime.nexloop_local_artifacts where tenant_id=t and world=p_world and artifact_id=c->>'artifact_id' for update;
  if not found or art.status<>'deleting' or art.upload_worker is distinct from c->>'worker' or art.upload_fence is distinct from (c->>'fence')::bigint
   or art.lease_until<=clock_timestamp() then raise exception 'artifact deletion fenced' using errcode='40001';end if;
  update runtime.nexloop_local_artifacts set status='deleted',updated_at=clock_timestamp() where tenant_id=t and world=p_world and artifact_id=art.artifact_id;
  perform runtime.nexloop_erasure_tombstone(t,p_world,case when art.retention_until<=clock_timestamp() then 'retention_expiry' else 'consumer_erasure' end,
   'artifact:'||art.artifact_id,'artifact','local_artifact',art.artifact_id,'deleted',jsonb_build_object('size_bytes',art.size_bytes),v_by);
  insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
   values('artifact-purge:'||art.artifact_id,'nexloop.erasure.artifact_deleted',t,v_by,'erasure','artifact:'||art.artifact_id,
    jsonb_build_object('artifact_id',art.artifact_id,'principal_id',art.principal_id),clock_timestamp()) on conflict do nothing;
  return jsonb_build_object('artifact_id',art.artifact_id,'status','deleted');
 elsif c->>'verb'='status' then
  return jsonb_build_object('world',p_world,
   'requests',(select coalesce(jsonb_agg(jsonb_build_object('request_id',request_id,'state',state,'phase',phase,'counts',counts,'watermark',watermark) order by created_at),'[]'::jsonb)
     from control.nexloop_consumer_erasures where tenant_id=t and world=p_world),
   'run_purges_pending',(select count(*) from runtime.nexloop_run_purge_items where tenant_id=t and world=p_world and state='pending'));
 end if;
 raise exception 'erasure command invalid' using errcode='22023';
end $$;
