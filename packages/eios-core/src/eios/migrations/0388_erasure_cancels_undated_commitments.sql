-- I36 (owner 2026-10-31; L6 option A with conditions a-d): consumer erasure cancels the Consumer's undated commitments.
-- D4 (0132) keeps an erased Consumer's commitment in its state until its own expiry; a commitment without due_at never expires, and
-- the erasure's completion (0306) deletes the registry rows, after which the keeper can no longer move the commitment (stage: 44 open
-- or conditional commitments of an erased Consumer). Temporary number 0388.
--
-- a. One transition, shared: runtime.nexloop_commitment_cancel_apply(tenant, world, commitment, source) applies the ledger-derived
--    patch (runtime.nexloop_commitment_target, the same function the keeper's evaluate uses; the object guard checks it again) with
--    revision+1, and records the status event through runtime.nexloop_commitment_status_event, which the keeper's settle now calls
--    too (authz.nexloop_commitment_command is 0263's body with that call in place of its inline insert; registered in the copy guard).
--    The event carries source consumer_erasure; the keeper's carries none (unchanged). The object write is not the keeper's signed
--    Commitment.edit (the erasure holds no keeper permit): no action claim / outcome row, as the erasure's own made_to write (0132).
-- b. Only conditional / open / in_progress commitments that are undated or already carry a cancel request; fulfilled, breached,
--    cancelled are left alone. Idempotent: no second cancel request, no second status event (the event is written only when the last
--    recorded status differs); a keeper that wakes later finds an empty patch and an equal last status.
-- c. In the completion trigger control.nexloop_consumer_reference_erasure (0306's body with these additions only; copy guard), in
--    the same transaction and before the registry rows are deleted; a retention hold returns before either, as before.
-- I36-c: a dated, not yet ended commitment keeps its registry row under the erasure pseudonym (the guard admits that update), so
--    the keeper breaches it at its due time (D4) and commitment_closed retention removes it; counted as commitments_kept.
-- d. Existing stock: the commitments of Consumers whose erasure already completed and whose registry rows remain (erased before
--    0306; made_to 'erasure:<request id>') are handled once below, with the same rule and the same functions.

create function runtime.nexloop_commitment_cancel_requested(p_tenant text,p_world text,p_commitment text) returns boolean
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select exists(select 1 from runtime.nexloop_commitment_events e where e.tenant_id=p_tenant and e.world=p_world
  and e.subject_ref='commitment:'||p_commitment and e.kind='cancel_requested')
$$;
alter function runtime.nexloop_commitment_cancel_requested(text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_commitment_cancel_requested(text,text,text) from public;

-- The status event of a commitment (the keeper's settle and the erasure): written when the last recorded status differs.
create function runtime.nexloop_commitment_status_event(p_tenant text,p_world text,o ontology.objects,p_last text,p_source text) returns boolean
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if p_last is not distinct from o.properties->>'status' then return false;end if;
 insert into runtime.nexloop_commitment_events(tenant_id,world,subject_ref,kind,detail)
  values(p_tenant,p_world,'commitment:'||o.object_id,'status',jsonb_build_object('status',o.properties->>'status','from',p_last,'late',o.properties->'late',
   'revision',o.nexloop_revision,'fulfillment_evidence',o.properties->'fulfillment_evidence')
   ||case when p_source is null then '{}'::jsonb else jsonb_build_object('source',p_source) end);
 return true;
end $$;
alter function runtime.nexloop_commitment_status_event(text,text,ontology.objects,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_commitment_status_event(text,text,ontology.objects,text,text) from public;

-- The keeper's cancellation transition outside the keeper (the erasure): the ledger patch, revision+1, the status event.
create function runtime.nexloop_commitment_cancel_apply(p_tenant text,p_world text,p_commitment text,p_source text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);o ontology.objects;v_patch jsonb;v_last text;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into o from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='Commitment' and object_id=p_commitment for update;
 if not found or o.properties->>'status' not in ('conditional','open','in_progress') then
  perform set_config('eios.tenant_id',coalesce(prior,''),true);return jsonb_build_object('applied',false,'status',o.properties->>'status');end if;
 v_patch:=runtime.nexloop_commitment_target(p_tenant,p_world,p_commitment,o.properties,clock_timestamp());
 if v_patch->>'status' is distinct from 'cancelled' then
  perform set_config('eios.tenant_id',coalesce(prior,''),true);return jsonb_build_object('applied',false,'status',o.properties->>'status');end if;
 select e.detail->>'status' into v_last from runtime.nexloop_commitment_events e where e.tenant_id=p_tenant and e.world=p_world
  and e.subject_ref='commitment:'||p_commitment and e.kind in ('status','registered') order by e.event_number desc limit 1;
 update ontology.objects set properties=properties||v_patch,nexloop_revision=nexloop_revision+1,updated_at=clock_timestamp()
  where tenant_id=p_tenant and world=p_world and type_name='Commitment' and object_id=p_commitment returning * into o;
 perform runtime.nexloop_commitment_status_event(p_tenant,p_world,o,v_last,p_source);
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return jsonb_build_object('applied',true,'status',o.properties->>'status','revision',o.nexloop_revision);
end $$;
alter function runtime.nexloop_commitment_cancel_apply(text,text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_commitment_cancel_apply(text,text,text,text) from public;

-- I36-c (owner 2026-10-31, option one): the registry keeps a dated, not yet ended commitment of an erased Consumer under the
-- erasure's pseudonym (as 0306 does for the other tables) with its prepared properties cleared ({"erased":true}); its guard admits that one erasure update (and the erasure delete, as before).
alter table runtime.nexloop_commitments drop constraint nexloop_commitments_consumer_id_check,
 add constraint nexloop_commitments_consumer_id_check check(consumer_id~'^[a-f0-9]{64}$' or consumer_id~'^pseudonym:[0-9a-f]{32}$');
-- L6 I36-c a: a kept row holds nothing that identifies the customer - claim, message and correlation references go (null) and the
-- dedupe key becomes 'erased:<commitment id>'; the claim triple check admits exactly that shape for a pseudonym row.
do $c$
declare k text;
begin
 select conname into strict k from pg_constraint where conrelid='runtime.nexloop_commitments'::regclass and contype='c'
  and pg_get_constraintdef(oid) like '%correlation_key IS NULL%';
 execute format('alter table runtime.nexloop_commitments drop constraint %I',k);
end $c$;
alter table runtime.nexloop_commitments add constraint nexloop_commitments_claim_triple_check check(
 (consumer_id~'^pseudonym:[0-9a-f]{32}$' and claim_id is null and message_id is null and correlation_key is null)
 or ((origin='claim')=(claim_id is not null) and (claim_id is null)=(message_id is null) and (claim_id is null)=(correlation_key is null)));
create or replace function runtime.nexloop_commitment_registry_guard() returns trigger
 language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if tg_op='DELETE' and runtime.nexloop_erasure_permits(current_user::text,'runtime.nexloop_commitments','DELETE',to_jsonb(old),null) then return old;end if;
 -- I36-c: inside a consumer erasure that registered this table, a registered row may become the kept shape and nothing else.
 if tg_op='UPDATE' and current_user='nexloop_owner' and exists(select 1 from control.nexloop_erasure_scope s where s.txid=txid_current()
   and s.table_name='runtime.nexloop_commitments' and 'consumer_id'=any(s.columns))
  and old.registered_at is not null and new.consumer_id~'^pseudonym:[0-9a-f]{32}$' and new.properties='{"erased":true}'::jsonb
  and new.claim_id is null and new.message_id is null and new.correlation_key is null and new.dedupe_key='erased:'||old.commitment_id
  and (to_jsonb(new)-array['consumer_id','properties','claim_id','message_id','correlation_key','dedupe_key'])
   =(to_jsonb(old)-array['consumer_id','properties','claim_id','message_id','correlation_key','dedupe_key']) then return new;end if;
 if tg_op='DELETE' or old.registered_at is not null or new.registered_at is null
  or (to_jsonb(new)-'registered_at') is distinct from (to_jsonb(old)-'registered_at') then
  raise exception 'commitment registry is immutable' using errcode='42501';end if;
 return new;
end $$;

-- L6 I36-c b: no follow-up for an erasing or erased Consumer is generated at all - the plan marks (due soon, breached, condition at due,
-- clarify) are skipped, and no no_active_plan exception is raised for it (0111 body with this first line only; copy guard).
create or replace function runtime.nexloop_commitment_mark_plans(p_tenant text,p_world text,p_commitment text,p_consumer text,p_due timestamptz,p_stage text) returns integer
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare p record;n integer:=0;
begin
 if runtime.nexloop_consumer_erasing(p_tenant,p_world,p_consumer) then return 0;end if;
 for p in select a.plan_id from runtime.nexloop_active_plans(p_tenant,p_world) a where a.consumer_id=p_consumer order by a.plan_id loop
  perform runtime.nexloop_plan_commitment_due(p_tenant,p_world,p.plan_id,'commitment:'||p_commitment,clock_timestamp());n:=n+1;
 end loop;
 if n=0 then perform runtime.nexloop_commitment_raise(p_tenant,p_world,'commitment:'||p_commitment,p_consumer,'no_active_plan',
  jsonb_build_object('stage',p_stage,'due_at',p_due));end if;
 return n;
end $$;

-- I36-d (L6): every orphaned commitment the keeper meets is recorded (one row per commitment, last seen time) and counted in the
-- metrics snapshot; a tenant-context or RLS fault that hides registry rows would show up here instead of passing silently.
create table runtime.nexloop_commitment_orphans (
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null,commitment_id text not null check(commitment_id~'^[0-9a-f]{64}$'),
 first_seen_at timestamptz not null default clock_timestamp(),last_seen_at timestamptz not null default clock_timestamp(),
 seen integer not null default 1 check(seen>=1),primary key(tenant_id,world,commitment_id)
);
alter table runtime.nexloop_commitment_orphans owner to nexloop_owner;
alter table runtime.nexloop_commitment_orphans enable row level security;
alter table runtime.nexloop_commitment_orphans force row level security;
create policy tenant_boundary on runtime.nexloop_commitment_orphans to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_commitment_orphans from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;
create function runtime.nexloop_commitment_orphan(p_tenant text,p_world text,p_commitment text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 insert into runtime.nexloop_commitment_orphans(tenant_id,world,commitment_id) values(p_tenant,p_world,p_commitment)
  on conflict(tenant_id,world,commitment_id) do update set last_seen_at=clock_timestamp(),seen=runtime.nexloop_commitment_orphans.seen+1;
 return jsonb_build_object('commitment_id',p_commitment,'orphaned',true);
end $$;
alter function runtime.nexloop_commitment_orphan(text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_commitment_orphan(text,text,text) from public;

-- I38 (L6): the keeper's commitment command tells a missing registry row (recorded and returned as orphaned; terminal: the keeper
-- completes the item) from an unavailable port (42501, retried); before, a missing row was retried for good.
-- a. The keeper's settle records its status event through the shared function (0263 body otherwise unchanged).
CREATE OR REPLACE FUNCTION authz.nexloop_commitment_command(p_digest text, p_world text, p_text text, p_signature text, p_payload text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-commitment-keep-v1','eios:action:nexloop.commitment.keep:1',
  array['nexloop_domain_worker']);c jsonb:=p_payload::jsonb;r runtime.nexloop_commitments;o ontology.objects;v_patch jsonb;v_status text;v_last text;
 v_due timestamptz;v_lead integer;v_next timestamptz;v_now timestamptz:=runtime.nexloop_world_now(t,p_world);v_subject text;v_restricted boolean;f runtime.nexloop_work_feed;
begin
 perform set_config('eios.tenant_id',t,true);
 if c->>'verb'='prepare' then
  if c ? 'claim_id' then
   if coalesce(c->>'claim_id','')!~'^[0-9a-f]{64}$' then raise exception 'commitment command invalid' using errcode='22023';end if;
   return runtime.nexloop_commitment_prepare_claim(t,p_world,c->>'claim_id');
  end if;
  if coalesce(c->>'commitment_id','')!~'^[0-9a-f]{64}$' then raise exception 'commitment command invalid' using errcode='22023';end if;
  select * into r from runtime.nexloop_commitments where tenant_id=t and world=p_world and commitment_id=c->>'commitment_id';
  if not found then raise exception 'commitment unavailable' using errcode='42501';end if;
  if r.registered_at is not null then return jsonb_build_object('action','registered','commitment_id',r.commitment_id);end if;
  return jsonb_build_object('action','create','commitment_id',r.commitment_id,'intent_id',r.intent_id,'properties',r.properties);
 end if;
 if coalesce(c->>'commitment_id','')!~'^[0-9a-f]{64}$' then raise exception 'commitment command invalid' using errcode='22023';end if;
 select * into r from runtime.nexloop_commitments where tenant_id=t and world=p_world and commitment_id=c->>'commitment_id' for update;
 if not found then
  -- I38 / I36-d: orphaned only when this tenant still holds the Commitment object (its registry row went with a completed erasure);
  -- anything else (another tenant's commitment, an unknown id) is refused as before.
  if exists(select 1 from ontology.objects x where x.tenant_id=t and x.world=p_world and x.type_name='Commitment' and x.object_id=c->>'commitment_id') then
   return runtime.nexloop_commitment_orphan(t,p_world,c->>'commitment_id');end if;
  raise exception 'commitment unavailable' using errcode='42501';
 end if;
 v_subject:='commitment:'||r.commitment_id;
 select * into o from ontology.objects where tenant_id=t and world=p_world and type_name='Commitment' and object_id=r.commitment_id for share;
 if c->>'verb'='registered' then
  if o.object_id is null or o.properties is distinct from r.properties then raise exception 'commitment not created as prepared' using errcode='40001';end if;
  if r.registered_at is not null then return jsonb_build_object('commitment_id',r.commitment_id,'replay',true);end if;
  update runtime.nexloop_commitments set registered_at=v_now where tenant_id=t and world=p_world and commitment_id=r.commitment_id;
  insert into runtime.nexloop_commitment_events(tenant_id,world,subject_ref,kind,ref,detail)
   values(t,p_world,v_subject,'registered',coalesce('claim:'||r.claim_id,'commitment:'||r.supersedes),jsonb_build_object('origin',r.origin,'status',r.properties->>'status'));
  if r.claim_id is not null then
   update ontology.nexloop_claims set resolution_state='resolved' where tenant_id=t and world=p_world and claim_id=r.claim_id and resolution_state in ('unresolved','needs_resolution');
  end if;
  if r.supersedes is not null then
   insert into runtime.nexloop_commitment_events(tenant_id,world,subject_ref,kind,ref)
    values(t,p_world,'commitment:'||r.supersedes,'superseded',v_subject);
   perform runtime.nexloop_commitment_touch(t,p_world,r.supersedes,v_now);
  end if;
  if exists(select 1 from control.nexloop_contact_restrictions x where x.tenant_id=t and x.world=p_world and x.consumer_id=r.consumer_id and x.active) and r.origin='claim' then
   perform runtime.nexloop_commitment_raise(t,p_world,v_subject,r.consumer_id,'made_under_contact_restriction',jsonb_build_object('message_id',r.message_id));
  end if;
  if r.claim_id is not null and exists(select 1 from ontology.nexloop_claims cl where cl.tenant_id=t and cl.world=p_world and cl.claim_id=r.claim_id
    and cl.valid_time->>'kind'='past_reference') then
   perform runtime.nexloop_commitment_raise(t,p_world,v_subject,r.consumer_id,'due_in_past',jsonb_build_object('claim_id',r.claim_id));
  end if;
  -- Without a due date the plans are marked now: the business rule is to clarify it (docs/04 §8).
  if r.properties->>'due_at' is null then perform runtime.nexloop_commitment_mark_plans(t,p_world,r.commitment_id,r.consumer_id,null,'clarify_due');end if;
  perform runtime.nexloop_commitment_touch(t,p_world,r.commitment_id,v_now);
  return jsonb_build_object('commitment_id',r.commitment_id,'replay',false);
 elsif c->>'verb'='exception' then
  if coalesce(c->>'reason','')!~'^[a-z_]{1,64}$' or jsonb_typeof(c->'detail') is distinct from 'object' then raise exception 'commitment command invalid' using errcode='22023';end if;
  return jsonb_build_object('raised',runtime.nexloop_commitment_raise(t,p_world,v_subject,r.consumer_id,c->>'reason',c->'detail'));
 end if;
 if o.object_id is null or r.registered_at is null then raise exception 'commitment not registered' using errcode='40001';end if;
 if c->>'verb'='evaluate' then
  v_patch:=runtime.nexloop_commitment_target(t,p_world,r.commitment_id,o.properties,v_now);
  return jsonb_build_object('commitment_id',r.commitment_id,'revision',o.nexloop_revision,'status',o.properties->>'status','patch',v_patch);
 elsif c->>'verb'='settle' then
  v_status:=o.properties->>'status';
  select e.detail->>'status' into v_last from runtime.nexloop_commitment_events e where e.tenant_id=t and e.world=p_world and e.subject_ref=v_subject
   and e.kind in ('status','registered') order by e.event_number desc limit 1;
  if v_last is distinct from v_status then
   perform runtime.nexloop_commitment_status_event(t,p_world,o,v_last,null);
   if v_status='breached' then
    v_restricted:=exists(select 1 from control.nexloop_contact_restrictions x where x.tenant_id=t and x.world=p_world and x.consumer_id=r.consumer_id and x.active);
    perform runtime.nexloop_commitment_raise(t,p_world,v_subject,r.consumer_id,'breached',jsonb_build_object('due_at',o.properties->>'due_at',
     'contact_restricted',v_restricted,'paused',exists(select 1 from control.nexloop_control_scopes s where s.tenant_id=t and s.world=p_world and s.paused
      and ((s.scope_kind='consumer' and s.scope_ref=r.consumer_id) or s.scope_kind='tenant'))));
    perform runtime.nexloop_commitment_mark_plans(t,p_world,r.commitment_id,r.consumer_id,(o.properties->>'due_at')::timestamptz,'breached');
   end if;
  end if;
  v_due:=(o.properties->>'due_at')::timestamptz;v_lead:=runtime.nexloop_commitment_setting('lead_seconds');
  if v_status in ('open','in_progress') then
   -- A fulfilment intent that cannot dispatch to a restricted Consumer (ADR-023): owner-visible.
   if exists(select 1 from control.nexloop_contact_restrictions x where x.tenant_id=t and x.world=p_world and x.consumer_id=r.consumer_id and x.active)
    and exists(select 1 from runtime.nexloop_commitment_evidence q join runtime.nexloop_effect_intents i on 'intent:'||i.intent_id=q.ref
      and i.tenant_id=q.tenant_id and i.world=q.world
     where q.tenant_id=t and q.world=p_world and q.commitment_id=r.commitment_id and q.kind='requested' and i.state not in ('fulfilled','confirmed')
      and runtime.nexloop_commitment_effect_kind(t,p_world,i.intent_id)='delivered_message') then
    perform runtime.nexloop_commitment_raise(t,p_world,v_subject,r.consumer_id,'blocked_by_contact_restriction',jsonb_build_object('due_at',o.properties->>'due_at'));
   end if;
   if v_due is not null then
    if v_now>=v_due-make_interval(secs=>v_lead) and not exists(select 1 from runtime.nexloop_commitment_events e
      where e.tenant_id=t and e.world=p_world and e.subject_ref=v_subject and e.kind='due_soon') then
     insert into runtime.nexloop_commitment_events(tenant_id,world,subject_ref,kind,detail) values(t,p_world,v_subject,'due_soon',jsonb_build_object('due_at',o.properties->>'due_at'));
     perform runtime.nexloop_commitment_mark_plans(t,p_world,r.commitment_id,r.consumer_id,v_due,'due_soon');
    end if;
    v_next:=case when v_now<v_due-make_interval(secs=>v_lead) then v_due-make_interval(secs=>v_lead) else v_due end;
   end if;
  elsif v_status='conditional' and v_due is not null then
   if v_now>=v_due then
    if runtime.nexloop_commitment_raise(t,p_world,v_subject,r.consumer_id,'condition_unresolved_at_due',jsonb_build_object('due_at',o.properties->>'due_at')) then
     perform runtime.nexloop_commitment_mark_plans(t,p_world,r.commitment_id,r.consumer_id,v_due,'condition_unresolved');
    end if;
   else v_next:=v_due;end if;
  end if;
  if v_status in ('conditional','open','in_progress') and v_due is null then
   if v_now>=r.registered_at+make_interval(secs=>runtime.nexloop_commitment_setting('unspecified_max_open_seconds')) then
    perform runtime.nexloop_commitment_raise(t,p_world,v_subject,r.consumer_id,'no_due_date',jsonb_build_object('registered_at',r.registered_at));
   else v_next:=r.registered_at+make_interval(secs=>runtime.nexloop_commitment_setting('unspecified_max_open_seconds'));end if;
  end if;
  -- Next stage of this commitment's monitor item; an item changed while being worked on stays due now.
  if v_next is not null then
   select * into f from runtime.nexloop_work_feed where tenant_id=t and world=p_world and feed='commitment-monitor' and item_key=v_subject for update;
   if not found then
    insert into runtime.nexloop_work_feed(tenant_id,world,feed,item_key,payload,available_at)
     values(t,p_world,'commitment-monitor',v_subject,jsonb_build_object('commitment_id',r.commitment_id),runtime.nexloop_world_wall_at(t,p_world,v_next));
   elsif f.lease_seq is not distinct from (c->>'fence')::bigint and f.change_seq=f.lease_seq then
    update runtime.nexloop_work_feed set available_at=runtime.nexloop_world_wall_at(t,p_world,v_next),change_seq=change_seq+1,changed_at=clock_timestamp()
     where tenant_id=t and world=p_world and feed='commitment-monitor' and item_key=v_subject;
   end if;
  end if;
  return jsonb_build_object('commitment_id',r.commitment_id,'status',v_status,'next_at',v_next);
 end if;
 raise exception 'commitment command invalid' using errcode='22023';
end $function$;

-- c. The completion trigger cancels before the registry rows go (0306 body otherwise unchanged).
create or replace function control.nexloop_consumer_reference_erasure() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);v_p text:='pseudonym:'||encode(extensions.gen_random_bytes(16),'hex');
 v_ref text:='consumer-erasure:'||new.request_id;n jsonb:='{}'::jsonb;c integer;t text;w text:=new.world;k text:=new.consumer_id;tn text:=new.tenant_id;x record;
begin
 if new.state<>'completed' or old.state='completed' then return null;end if;
 perform set_config('eios.tenant_id',tn,true);
 if runtime.nexloop_retention_held(tn,w,'consumer',k) then perform set_config('eios.tenant_id',coalesce(v_prior,''),true);return null;end if;
 -- Deletions.
 delete from control.nexloop_consumer_owners where tenant_id=tn and world=w and consumer_id=k;
 get diagnostics c=row_count;n:=n||jsonb_build_object('consumer_owners',c);
 delete from control.nexloop_relationship_context_recipes where tenant_id=tn and world=w and consumer_id=k;
 get diagnostics c=row_count;n:=n||jsonb_build_object('relationship_context_recipes',c);
 delete from runtime.nexloop_evo_patch_sources where tenant_id=tn and world=w and consumer_id=k;
 get diagnostics c=row_count;n:=n||jsonb_build_object('evo_patch_sources',c);
 delete from runtime.nexloop_message_relay_leases where tenant_id=tn and world=w and consumer_id=k;
 get diagnostics c=row_count;n:=n||jsonb_build_object('message_relay_leases',c);
 -- I36: before the registry rows go, the Consumer's undated (or already cancel-requested) conditional / open / in_progress
 -- commitments are cancelled on their ledger through the keeper's transition (one cancel request each, never a second).
 c:=0;
 for x in select r.commitment_id from runtime.nexloop_commitments r join ontology.objects o on o.tenant_id=r.tenant_id and o.world=r.world
   and o.type_name='Commitment' and o.object_id=r.commitment_id
  where r.tenant_id=tn and r.world=w and r.consumer_id=k and o.properties->>'status' in ('conditional','open','in_progress')
   and (o.properties->>'due_at' is null or runtime.nexloop_commitment_cancel_requested(tn,w,r.commitment_id))
  order by r.commitment_id for update of o loop
  if not runtime.nexloop_commitment_cancel_requested(tn,w,x.commitment_id) then
   insert into runtime.nexloop_commitment_events(tenant_id,world,subject_ref,kind,ref,detail,principal_id)
    values(tn,w,'commitment:'||x.commitment_id,'cancel_requested','erasure:'||new.request_id,jsonb_build_object('reason','consumer_erasure'),'erasure:'||new.request_id);
  end if;
  if (runtime.nexloop_commitment_cancel_apply(tn,w,x.commitment_id,'consumer_erasure')->>'applied')::boolean then c:=c+1;end if;
 end loop;
 n:=n||jsonb_build_object('commitments_cancelled',c);
 -- I36-c (owner): a dated, not yet ended commitment keeps its registry row under this step's pseudonym, so the keeper still moves it
 -- (breached at its due time, D4) and commitment_closed retention removes it later; every other registry row goes.
 -- Nothing identifying stays (L6 I36-c a): the prepared properties, claim / message / correlation references and the dedupe key go.
 perform runtime.nexloop_erasure_open('runtime.nexloop_commitments',array['consumer_id'],true,v_ref);
 update runtime.nexloop_commitments r set consumer_id=v_p,properties='{"erased":true}'::jsonb,claim_id=null,message_id=null,correlation_key=null,
  dedupe_key='erased:'||r.commitment_id from ontology.objects o
  where r.tenant_id=tn and r.world=w and r.consumer_id=k and o.tenant_id=r.tenant_id and o.world=r.world and o.type_name='Commitment'
   and o.object_id=r.commitment_id and r.registered_at is not null and o.properties->>'status' in ('conditional','open','in_progress') and o.properties->>'due_at' is not null;
 get diagnostics c=row_count;n:=n||jsonb_build_object('commitments_kept',c);
 delete from runtime.nexloop_commitments where tenant_id=tn and world=w and consumer_id=k;
 get diagnostics c=row_count;n:=n||jsonb_build_object('commitments',c);
 -- Pseudonyms (no guard on these tables).
 update authz.nexloop_role_run_bindings set consumer_id=v_p where tenant_id=tn and world=w and consumer_id=k;
 get diagnostics c=row_count;n:=n||jsonb_build_object('role_run_bindings',c);
 update control.nexloop_consumer_identity_link_receipts set identity_ref='pseudonym:'||encode(extensions.gen_random_bytes(16),'hex'),
   consumer_id=v_p,subject_id=v_p,account_id=v_p,request_payload='{"erased":true}'::jsonb where tenant_id=tn and world=w and consumer_id=k;
 get diagnostics c=row_count;n:=n||jsonb_build_object('identity_link_receipts',c);
 update control.nexloop_data_exports set consumer_id=v_p,reason='' where tenant_id=tn and world=w and consumer_id=k;
 get diagnostics c=row_count;n:=n||jsonb_build_object('data_exports',c);
 update control.nexloop_effect_control_ledger set consumer_id=v_p where tenant_id=tn and world=w and consumer_id=k;
 get diagnostics c=row_count;n:=n||jsonb_build_object('effect_control_ledger',c);
 update ontology.nexloop_extraction_runs set consumer_id=v_p,principal_id=v_p,rejected='[]'::jsonb where tenant_id=tn and world=w and consumer_id=k;
 get diagnostics c=row_count;n:=n||jsonb_build_object('extraction_runs',c);
 update runtime.nexloop_native_web_events set consumer_id=v_p,principal_id=v_p where tenant_id=tn and world=w and consumer_id=k;
 get diagnostics c=row_count;n:=n||jsonb_build_object('native_web_events',c);
 -- Pseudonyms through the erasure scope (guarded tables).
 perform runtime.nexloop_erasure_open('control.nexloop_effect_contexts',array['consumer_id','expected_versions'],false,v_ref);
 update control.nexloop_effect_contexts set consumer_id=v_p,expected_versions='{"erased":true}'::jsonb where tenant_id=tn and world=w and consumer_id=k;
 get diagnostics c=row_count;n:=n||jsonb_build_object('effect_contexts',c);
 perform runtime.nexloop_erasure_open('runtime.nexloop_commitment_exceptions',array['consumer_id','detail'],false,v_ref);
 update runtime.nexloop_commitment_exceptions set consumer_id=v_p,detail='{"erased":true}'::jsonb where tenant_id=tn and world=w and consumer_id=k;
 get diagnostics c=row_count;n:=n||jsonb_build_object('commitment_exceptions',c);
 perform runtime.nexloop_erasure_open('runtime.nexloop_relay_handoffs',array['consumer_id'],false,v_ref);
 update runtime.nexloop_relay_handoffs set consumer_id=v_p where tenant_id=tn and world=w and consumer_id=k;
 get diagnostics c=row_count;n:=n||jsonb_build_object('relay_handoffs',c);
 perform runtime.nexloop_erasure_open('runtime.nexloop_scope_denials',array['consumer_id','scope','request_scope'],false,v_ref);
 update runtime.nexloop_scope_denials set consumer_id=v_p,scope='{"erased":true}'::jsonb,request_scope='{"erased":true}'::jsonb where tenant_id=tn and world=w and consumer_id=k;
 get diagnostics c=row_count;n:=n||jsonb_build_object('scope_denials',c);
 -- Only the scopes opened here are closed: the erasure step may hold others open in the same transaction.
 delete from control.nexloop_erasure_scope where txid=txid_current() and table_name in ('runtime.nexloop_commitments','control.nexloop_effect_contexts',
  'runtime.nexloop_commitment_exceptions','runtime.nexloop_relay_handoffs','runtime.nexloop_scope_denials');
 perform runtime.nexloop_erasure_tombstone(tn,w,'consumer_erasure',v_ref,'consumer_references','consumer_references',k,'pseudonymized',n,'nexloop_owner');
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return null;
end $$;

-- d. Existing stock: erased Consumers' commitments whose registry rows remain (erasure completed before 0306).
do $stock$
declare x record;
begin
 for x in select o.tenant_id,o.world,o.object_id,o.properties->>'made_to' made_to,o.properties->>'due_at' due_at from ontology.objects o
   join runtime.nexloop_commitments r on r.tenant_id=o.tenant_id and r.world=o.world and r.commitment_id=o.object_id
  where o.type_name='Commitment' and o.properties->>'made_to' ~ '^erasure:[A-Za-z0-9._:-]{1,190}$'
   and o.properties->>'status' in ('conditional','open','in_progress')
  order by o.tenant_id,o.world,o.object_id loop
  perform set_config('eios.tenant_id',x.tenant_id,true);
  continue when x.due_at is not null and not runtime.nexloop_commitment_cancel_requested(x.tenant_id,x.world,x.object_id);
  if not runtime.nexloop_commitment_cancel_requested(x.tenant_id,x.world,x.object_id) then
   insert into runtime.nexloop_commitment_events(tenant_id,world,subject_ref,kind,ref,detail,principal_id)
    values(x.tenant_id,x.world,'commitment:'||x.object_id,'cancel_requested',x.made_to,jsonb_build_object('reason','consumer_erasure'),x.made_to);
  end if;
  perform runtime.nexloop_commitment_cancel_apply(x.tenant_id,x.world,x.object_id,'consumer_erasure');
 end loop;
 perform set_config('eios.tenant_id','',true);
end $stock$;

-- I36-d: runtime.nexloop_metrics_snapshot is renamed once and wrapped (world real): commitment_orphans {total, seen_last_24h}.
alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_commitment_orphans;
revoke all on function runtime.nexloop_metrics_snapshot_before_commitment_orphans(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp as $$
declare v jsonb:=runtime.nexloop_metrics_snapshot_before_commitment_orphans(p_tenant,p_world);prior text:=current_setting('eios.tenant_id',true);o jsonb;
begin
 if p_world is distinct from 'real' then return v;end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select jsonb_build_object('total',count(*),'seen_last_24h',count(*) filter(where last_seen_at>clock_timestamp()-interval '24 hours')) into o
  from runtime.nexloop_commitment_orphans where tenant_id=p_tenant and world=p_world;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v||jsonb_build_object('commitment_orphans',o);
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;
