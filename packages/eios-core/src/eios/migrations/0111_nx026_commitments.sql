-- NX-026 (temporary number 0111): commitments and value fulfilment (M18), docs/implementation/NX-026-design.md.
--
-- 1. Registration. Recording a commitment Claim (0066) marks it in the 'commitment-register' work feed in the same
--    transaction. The commitment keeper (service, nexloop.commitment.keep) prepares the registration in SQL from the
--    Claim and the delivered outbound Message (NX-047) only, deterministically (no model): made_to, made_by,
--    content_ref, promised_at, due_at (resolved end / latest bound of an ambiguous window / none), condition,
--    related_goal_ref. Then it creates the Commitment object through the governed Commitment.create Action; the
--    object guard accepts a created Commitment only with exactly the prepared properties. Same message + same
--    correlation_key = same commitment (replay, re-extraction, new extractor version). The same promise in a later
--    delivered message reaffirms it, or supersedes it when its due date differs.
-- 2. Status only moves on ledger evidence. Every edit of a Commitment must equal the transition SQL derives from the
--    current object and the append-only evidence/event ledger (runtime.nexloop_commitment_target); content fields
--    never change; nothing deletes a Commitment. A delivered message, a customer's own words and an Agent's report
--    never fulfil a commitment (AT-040); an effect receipt of a non-contact service delivery, a verified commercial
--    event or a human attestation do; a delivered message bound to the commitment does only after a human marked the
--    commitment as a communication commitment, and only for messages delivered after that marking (D3).
-- 3. Due. lead_seconds before due_at the Consumer's active plans are marked (NX-024 T7, cause external); at due_at an
--    open / in-progress commitment without qualifying evidence is breached, an exception is raised for the owner and
--    the plans are marked again. A paused Consumer does not hide a breach. Conditional commitments are never breached:
--    an exception asks for the owner's decision. Late evidence turns a breach into fulfilled (late=true); the breach
--    stays in the history.
-- 4. Human-only Actions through the NX-022 governed entry: cancel, extend (a new Commitment supersedes the old one,
--    which is cancelled unless already breached), attest, condition met, mark as communication commitment.
-- 5. Ports: keeper (prepare / registered / exception / evaluate / settle) and read (open commitments, one
--    commitment with its four evidence columns, exceptions). A deleted source Message is never quoted (D7).
-- Published migrations are not edited; the work feed port and the governed goal entry are replaced in place by
-- their latest bodies (0109) with only the stated additions.

-- 0. Versioned settings (deploy/configuration/commitments.v1.json, byte-equal canonical JSON) ----------------------
-- Deployment configuration without a tenant dimension (same kind as 0109 control.nexloop_reply_policies): control
-- schema, owner-only, append-only, no application role access; read only through the owner's setting function.
create table control.nexloop_commitment_settings (
 version integer primary key check(version>=1),definition jsonb not null check(jsonb_typeof(definition)='object'),
 definition_digest text not null check(definition_digest~'^[0-9a-f]{64}$'),published_by text not null,
 published_at timestamptz not null default clock_timestamp(),check((definition->>'version')::integer=version),
 check(jsonb_typeof(definition->'lead_seconds')='number' and (definition->>'lead_seconds')::integer between 0 and 2592000),
 check(jsonb_typeof(definition->'unspecified_max_open_seconds')='number' and (definition->>'unspecified_max_open_seconds')::integer between 60 and 31536000)
);
alter table control.nexloop_commitment_settings owner to nexloop_owner;
create trigger nx026_append_only before update or delete on control.nexloop_commitment_settings for each row execute function control.nexloop_nx022_append_only();
revoke all on control.nexloop_commitment_settings from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;
insert into control.nexloop_commitment_settings(version,definition,definition_digest,published_by)
 values(1,'{"decision":"NX-026 (M18, dispatcher rulings 2026-10-10): an enterprise commitment Claim from a delivered outbound Message is registered as a governed Commitment object; its status only moves on ledger evidence. lead_seconds before due_at the Consumer''s active plans are marked for reevaluation (NX-024 T7); at due_at an open or in-progress commitment without qualifying evidence is breached, an owner-visible exception is raised and the plans are marked again. A commitment without a determinable due date marks the plans at registration (clarify) and raises no_due_date after unspecified_max_open_seconds. Initial values; owner-adjustable by a new version.","lead_seconds":3600,"schema":"nexloop-commitments/1","unspecified_max_open_seconds":604800,"version":1,"worker":{"batch":20,"lease_seconds":120,"max_attempts":8,"retry_base_seconds":30}}'::jsonb,
  'c75d92952119b23a0b70a9c663e481961327d451850879ca23fff2cc056c10fa','deploy/configuration/commitments.v1.json');

create function runtime.nexloop_commitment_setting(p_key text) returns integer
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select (definition->>p_key)::integer from control.nexloop_commitment_settings order by version desc limit 1
$$;
alter function runtime.nexloop_commitment_setting(text) owner to nexloop_owner;
revoke all on function runtime.nexloop_commitment_setting(text) from public;

-- 1. Registry, events, evidence, exceptions ---------------------------------------------------------------------
-- Registry: one row per Commitment object, written before its governed creation (the object guard checks it).
create table runtime.nexloop_commitments (
 tenant_id text not null,world text not null,commitment_id text not null check(commitment_id~'^[0-9a-f]{64}$'),
 dedupe_key text not null check(char_length(dedupe_key) between 1 and 200),
 origin text not null check(origin in ('claim','extend')),claim_id text,message_id text,
 consumer_id text not null check(consumer_id~'^[a-f0-9]{64}$'),correlation_key text,
 supersedes text check(supersedes~'^[0-9a-f]{64}$'),intent_id text not null check(char_length(intent_id) between 1 and 200),
 properties jsonb not null check(jsonb_typeof(properties)='object'),
 prepared_at timestamptz not null default clock_timestamp(),registered_at timestamptz,
 primary key(tenant_id,world,commitment_id),unique(tenant_id,world,dedupe_key),unique(tenant_id,world,intent_id),
 check((origin='claim')=(claim_id is not null) and (claim_id is null)=(message_id is null) and (claim_id is null)=(correlation_key is null)),
 check(origin<>'extend' or supersedes is not null)
);
create index nexloop_commitments_consumer on runtime.nexloop_commitments(tenant_id,world,consumer_id);
create index nexloop_commitments_claim on runtime.nexloop_commitments(tenant_id,world,claim_id);
create index nexloop_commitments_message on runtime.nexloop_commitments(tenant_id,world,message_id);
-- Lifecycle events (registration, reaffirmation, human requests, status changes, due stages).
create table runtime.nexloop_commitment_events (
 tenant_id text not null,world text not null,event_number bigint generated always as identity primary key,
 subject_ref text not null check(subject_ref~'^(commitment|claim):[0-9a-f]{64}$'),
 kind text not null check(kind in ('registered','reaffirmed','skipped','superseded','cancel_requested','extend_requested','condition_met',
  'marked_communication','status','due_soon')),
 ref text,detail jsonb not null default '{}'::jsonb check(jsonb_typeof(detail)='object'),principal_id text,
 recorded_at timestamptz not null default clock_timestamp()
);
create index nexloop_commitment_events_subject on runtime.nexloop_commitment_events(tenant_id,world,subject_ref,event_number);
-- Evidence ledger. Four columns are shown separately (request, delivery, customer confirmation, problem resolution).
create table runtime.nexloop_commitment_evidence (
 tenant_id text not null,world text not null,evidence_number bigint generated always as identity primary key,
 commitment_id text not null check(commitment_id~'^[0-9a-f]{64}$'),
 kind text not null check(kind in ('requested','effect_fulfilled','delivered_message','consumer_confirmation','problem_resolution',
  'commercial_event','operator_attestation','agent_report')),
 ref text not null check(char_length(ref) between 1 and 300),occurred_at timestamptz not null,recorded_by text not null,
 detail jsonb not null default '{}'::jsonb check(jsonb_typeof(detail)='object'),recorded_at timestamptz not null default clock_timestamp(),
 unique(tenant_id,world,commitment_id,kind,ref)
);
-- Owner-visible exceptions, at most one per (subject, reason).
create table runtime.nexloop_commitment_exceptions (
 tenant_id text not null,world text not null,subject_ref text not null check(subject_ref~'^(commitment|claim):[0-9a-f]{64}$'),
 consumer_id text,reason text not null check(reason~'^[a-z_]{1,64}$'),detail jsonb not null check(jsonb_typeof(detail)='object'),
 raised_at timestamptz not null default clock_timestamp(),primary key(tenant_id,world,subject_ref,reason)
);
do $tables$
declare t text;
begin
 foreach t in array array['nexloop_commitments','nexloop_commitment_events','nexloop_commitment_evidence','nexloop_commitment_exceptions'] loop
  execute format('alter table runtime.%I owner to nexloop_owner',t);
  execute format('alter table runtime.%I enable row level security',t);
  execute format('alter table runtime.%I force row level security',t);
  execute format('create policy tenant_boundary on runtime.%I to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on runtime.%I from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator',t);
 end loop;
end $tables$;
create trigger nx026_append_only before update or delete on runtime.nexloop_commitment_events for each row execute function control.nexloop_nx022_append_only();
create trigger nx026_append_only before update or delete on runtime.nexloop_commitment_evidence for each row execute function control.nexloop_nx022_append_only();
create trigger nx026_append_only before update or delete on runtime.nexloop_commitment_exceptions for each row execute function control.nexloop_nx022_append_only();
-- The registry row is immutable except its one registration time.
create function runtime.nexloop_commitment_registry_guard() returns trigger
 language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if tg_op='DELETE' or old.registered_at is not null or new.registered_at is null
  or (to_jsonb(new)-'registered_at') is distinct from (to_jsonb(old)-'registered_at') then
  raise exception 'commitment registry is immutable' using errcode='42501';end if;
 return new;
end $$;
alter function runtime.nexloop_commitment_registry_guard() owner to nexloop_owner;
revoke all on function runtime.nexloop_commitment_registry_guard() from public;
create trigger nx026_registry_guard before update or delete on runtime.nexloop_commitments for each row execute function runtime.nexloop_commitment_registry_guard();

-- 2. Helpers ------------------------------------------------------------------------------------------------------
create function runtime.nexloop_commitment_ts(p timestamptz) returns text
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select to_char(p at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
$$;
alter function runtime.nexloop_commitment_ts(timestamptz) owner to nexloop_owner;

-- Mark one commitment for the monitor at p_at (never later than an earlier pending mark).
create function runtime.nexloop_commitment_touch(p_tenant text,p_world text,p_commitment text,p_at timestamptz) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);due timestamptz:=greatest(coalesce(p_at,clock_timestamp()),clock_timestamp());
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 insert into runtime.nexloop_work_feed as f(tenant_id,world,feed,item_key,payload,available_at)
  values(p_tenant,p_world,'commitment-monitor','commitment:'||p_commitment,jsonb_build_object('commitment_id',p_commitment),due)
 on conflict(tenant_id,world,feed,item_key) do update set change_seq=f.change_seq+1,changed_at=clock_timestamp(),
  available_at=case when f.status='pending' then least(f.available_at,due) else due end,attempts=0,status='pending',last_code=null;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
end $$;
alter function runtime.nexloop_commitment_touch(text,text,text,timestamptz) owner to nexloop_owner;
revoke all on function runtime.nexloop_commitment_touch(text,text,text,timestamptz) from public;

create function runtime.nexloop_commitment_raise(p_tenant text,p_world text,p_subject text,p_consumer text,p_reason text,p_detail jsonb) returns boolean
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);n integer;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 insert into runtime.nexloop_commitment_exceptions(tenant_id,world,subject_ref,consumer_id,reason,detail)
  values(p_tenant,p_world,p_subject,p_consumer,p_reason,coalesce(p_detail,'{}'::jsonb)) on conflict do nothing;
 get diagnostics n=row_count;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return n>0;
end $$;
alter function runtime.nexloop_commitment_raise(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function runtime.nexloop_commitment_raise(text,text,text,text,text,jsonb) from public;

-- NX-024 T7: the Consumer's active plans are marked (cause external); none is an exception for the owner.
create function runtime.nexloop_commitment_mark_plans(p_tenant text,p_world text,p_commitment text,p_consumer text,p_due timestamptz,p_stage text) returns integer
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare p record;n integer:=0;
begin
 for p in select a.plan_id from runtime.nexloop_active_plans(p_tenant,p_world) a where a.consumer_id=p_consumer order by a.plan_id loop
  perform runtime.nexloop_plan_commitment_due(p_tenant,p_world,p.plan_id,'commitment:'||p_commitment,clock_timestamp());n:=n+1;
 end loop;
 if n=0 then perform runtime.nexloop_commitment_raise(p_tenant,p_world,'commitment:'||p_commitment,p_consumer,'no_active_plan',
  jsonb_build_object('stage',p_stage,'due_at',p_due));end if;
 return n;
end $$;
alter function runtime.nexloop_commitment_mark_plans(text,text,text,text,timestamptz,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_commitment_mark_plans(text,text,text,text,timestamptz,text) from public;

-- Kind of a fulfilled effect bound to a commitment: a customer-reaching effect (an Agent reply, or any effect not
-- declared as non-contact service delivery, 0112) only ever proves a delivered message.
create function runtime.nexloop_commitment_effect_kind(p_tenant text,p_world text,p_intent uuid) returns text
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select case when exists(select 1 from runtime.nexloop_outbound_messages o where o.tenant_id=p_tenant and o.world=p_world and o.intent_id=p_intent)
  then 'delivered_message' else 'effect_fulfilled' end
$$;
alter function runtime.nexloop_commitment_effect_kind(text,text,uuid) owner to nexloop_owner;
revoke all on function runtime.nexloop_commitment_effect_kind(text,text,uuid) from public;

-- 3. The only transition a Commitment edit may carry -------------------------------------------------------------
-- Returns the property patch justified now by the ledger for the current properties (empty object: none).
create function runtime.nexloop_commitment_target(p_tenant text,p_world text,p_commitment text,p_props jsonb,p_at timestamptz) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v_status text:=p_props->>'status';v_basis text:=p_props->>'fulfillment_basis';
 patch jsonb:='{}'::jsonb;v_marked timestamptz;v_promised timestamptz:=(p_props->>'promised_at')::timestamptz;
 v_due timestamptz:=(p_props->>'due_at')::timestamptz;v_first timestamptz;v_evidence jsonb;v_subject text:='commitment:'||p_commitment;
begin
 if v_status in ('fulfilled','cancelled') then return patch;end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 -- D3: a human marking turns the commitment into a communication commitment (never back).
 select min(e.recorded_at) into v_marked from runtime.nexloop_commitment_events e
  where e.tenant_id=p_tenant and e.world=p_world and e.subject_ref=v_subject and e.kind='marked_communication';
 if v_basis='undetermined' and v_marked is not null then v_basis:='communication';patch:=patch||jsonb_build_object('fulfillment_basis','communication');end if;
 if v_status in ('open','in_progress','breached') then
  select min(x.occurred_at),coalesce(jsonb_agg(jsonb_build_object('kind',x.kind,'ref',x.ref,'occurred_at',runtime.nexloop_commitment_ts(x.occurred_at))
    order by x.occurred_at,x.evidence_number),'[]'::jsonb) into v_first,v_evidence
   from runtime.nexloop_commitment_evidence x where x.tenant_id=p_tenant and x.world=p_world and x.commitment_id=p_commitment
    and x.occurred_at>=v_promised and x.occurred_at<=p_at
    and (x.kind in ('effect_fulfilled','commercial_event','operator_attestation')
     or (x.kind='delivered_message' and v_basis='communication' and x.occurred_at>=v_marked));
  if v_first is not null then
   perform set_config('eios.tenant_id',coalesce(prior,''),true);
   return patch||jsonb_build_object('status','fulfilled','late',v_status='breached' or (v_due is not null and v_first>v_due),'fulfillment_evidence',v_evidence);
  end if;
 end if;
 if v_status in ('conditional','open','in_progress') and exists(select 1 from runtime.nexloop_commitment_events e
   where e.tenant_id=p_tenant and e.world=p_world and e.subject_ref=v_subject and e.kind in ('cancel_requested','superseded')) then
  perform set_config('eios.tenant_id',coalesce(prior,''),true);
  return patch||jsonb_build_object('status','cancelled');
 end if;
 if v_status='conditional' and exists(select 1 from runtime.nexloop_commitment_events e
   where e.tenant_id=p_tenant and e.world=p_world and e.subject_ref=v_subject and e.kind='condition_met') then
  v_status:='open';patch:=patch||jsonb_build_object('status','open');
 end if;
 if v_status='open' and exists(select 1 from runtime.nexloop_commitment_evidence x
   where x.tenant_id=p_tenant and x.world=p_world and x.commitment_id=p_commitment and x.kind='requested') then
  v_status:='in_progress';patch:=patch||jsonb_build_object('status','in_progress');
 end if;
 if v_status in ('open','in_progress') and v_due is not null and v_due<=p_at then patch:=patch||jsonb_build_object('status','breached');end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return patch;
end $$;
alter function runtime.nexloop_commitment_target(text,text,text,jsonb,timestamptz) owner to nexloop_owner;
revoke all on function runtime.nexloop_commitment_target(text,text,text,jsonb,timestamptz) from public;

-- 4. Object guard: prepared creation only, ledger-justified transitions only, no deletion -------------------------
create function ontology.nexloop_commitment_guard() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);r runtime.nexloop_commitments;patch jsonb;
begin
 if tg_op='DELETE' then
  if old.type_name='Commitment' then raise exception 'a Commitment is never deleted' using errcode='42501';end if;
  return old;
 end if;
 if new.type_name<>'Commitment' and (tg_op='INSERT' or old.type_name<>'Commitment') then return new;end if;
 if tg_op='UPDATE' and (old.type_name<>'Commitment' or new.type_name<>'Commitment' or new.object_id<>old.object_id
   or new.tenant_id<>old.tenant_id or new.world<>old.world) then raise exception 'commitment identity is immutable' using errcode='42501';end if;
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
alter function ontology.nexloop_commitment_guard() owner to nexloop_owner;
revoke all on function ontology.nexloop_commitment_guard() from public;
create trigger nx026_commitment_guard before insert or update or delete on ontology.objects
 for each row execute function ontology.nexloop_commitment_guard();

-- 5. Same-transaction marks: Claims, corrections, deleted source Messages, contact restrictions -----------------
create function ontology.nexloop_commitment_on_claim() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if new.epistemic_kind='commitment' then
  perform authz.nexloop_work_feed_touch(new.tenant_id,new.world,'commitment-register','claim:'||new.claim_id,jsonb_build_object('claim_id',new.claim_id));
 end if;
 return null;
end $$;
alter function ontology.nexloop_commitment_on_claim() owner to nexloop_owner;
revoke all on function ontology.nexloop_commitment_on_claim() from public;
create trigger nx026_commitment_claim after insert on ontology.nexloop_claims for each row execute function ontology.nexloop_commitment_on_claim();

-- A correction superseding a registered commitment's Claim never cancels it (the words were delivered): owner decides.
create function ontology.nexloop_commitment_on_claim_superseded() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);r record;
begin
 if new.resolution_state<>'superseded' or old.resolution_state='superseded' then return null;end if;
 perform set_config('eios.tenant_id',new.tenant_id,true);
 for r in select c.commitment_id,c.consumer_id from runtime.nexloop_commitments c where c.tenant_id=new.tenant_id and c.world=new.world and c.claim_id=new.claim_id loop
  perform runtime.nexloop_commitment_raise(new.tenant_id,new.world,'commitment:'||r.commitment_id,r.consumer_id,'source_superseded',jsonb_build_object('claim_id',new.claim_id));
 end loop;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return null;
end $$;
alter function ontology.nexloop_commitment_on_claim_superseded() owner to nexloop_owner;
revoke all on function ontology.nexloop_commitment_on_claim_superseded() from public;
create trigger nx026_commitment_claim_superseded after update of resolution_state on ontology.nexloop_claims
 for each row execute function ontology.nexloop_commitment_on_claim_superseded();

-- D7: a deleted source Message leaves the commitment's status and audit; its words are no longer shown (read port).
create function ontology.nexloop_commitment_on_message_delete() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);r record;
begin
 if old.type_name<>'Message' then return null;end if;
 perform set_config('eios.tenant_id',old.tenant_id,true);
 for r in select c.commitment_id,c.consumer_id from runtime.nexloop_commitments c where c.tenant_id=old.tenant_id and c.world=old.world and c.message_id=old.object_id loop
  perform runtime.nexloop_commitment_raise(old.tenant_id,old.world,'commitment:'||r.commitment_id,r.consumer_id,'source_deleted',jsonb_build_object('message_id',old.object_id));
 end loop;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return null;
end $$;
alter function ontology.nexloop_commitment_on_message_delete() owner to nexloop_owner;
revoke all on function ontology.nexloop_commitment_on_message_delete() from public;
create trigger nx026_commitment_message_delete after delete on ontology.objects for each row execute function ontology.nexloop_commitment_on_message_delete();

-- A new contact restriction is looked at by the monitor for the Consumer's live commitments (blocked fulfilment).
create function control.nexloop_commitment_on_restriction() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);r record;
begin
 perform set_config('eios.tenant_id',new.tenant_id,true);
 for r in select c.commitment_id from runtime.nexloop_commitments c join ontology.objects o on o.tenant_id=c.tenant_id and o.world=c.world
   and o.type_name='Commitment' and o.object_id=c.commitment_id
  where c.tenant_id=new.tenant_id and c.world=new.world and c.consumer_id=new.consumer_id and o.properties->>'status' in ('conditional','open','in_progress') loop
  perform runtime.nexloop_commitment_touch(new.tenant_id,new.world,r.commitment_id,clock_timestamp());
 end loop;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return null;
end $$;
alter function control.nexloop_commitment_on_restriction() owner to nexloop_owner;
revoke all on function control.nexloop_commitment_on_restriction() from public;
create trigger nx026_commitment_restriction after insert or update of active on control.nexloop_contact_restrictions
 for each row execute function control.nexloop_commitment_on_restriction();

-- 6. Effects bound to a commitment (service.request parameter commitment_ref, when the Action declares it) --------
-- The reference must name a live Commitment made to the intent's own Consumer; otherwise the intent is refused.
create function runtime.nexloop_commitment_on_intent() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v_ref text;v_id text;o ontology.objects;v_kind text;
begin
 if tg_op='INSERT' then
  if not (new.frozen_request->'parameters' ? 'commitment_ref') then return new;end if;
 elsif not (new.frozen_request->'parameters' ? 'commitment_ref') or new.state not in ('fulfilled','confirmed') or old.state in ('fulfilled','confirmed') then
  return new;
 end if;
 v_ref:=new.frozen_request->'parameters'->>'commitment_ref';
 v_id:=substring(coalesce(v_ref,'') from '^commitment:([0-9a-f]{64})$');
 perform set_config('eios.tenant_id',new.tenant_id,true);
 select * into o from ontology.objects where tenant_id=new.tenant_id and world=new.world and type_name='Commitment' and object_id=v_id;
 if tg_op='INSERT' then
  if v_id is null or not found or o.properties->>'made_to' is distinct from new.consumer_id or o.properties->>'status' not in ('open','in_progress','breached') then
   raise exception 'commitment reference rejected' using errcode='22023';end if;
  insert into runtime.nexloop_commitment_evidence(tenant_id,world,commitment_id,kind,ref,occurred_at,recorded_by,detail)
   values(new.tenant_id,new.world,v_id,'requested','intent:'||new.intent_id,clock_timestamp(),'effect-intent',jsonb_build_object('action_name',new.action_name,'action_version',new.action_version))
   on conflict do nothing;
 elsif found then
  v_kind:=runtime.nexloop_commitment_effect_kind(new.tenant_id,new.world,new.intent_id);
  insert into runtime.nexloop_commitment_evidence(tenant_id,world,commitment_id,kind,ref,occurred_at,recorded_by,detail)
   values(new.tenant_id,new.world,v_id,v_kind,'intent:'||new.intent_id,clock_timestamp(),'effect-ledger',jsonb_build_object('state',new.state))
   on conflict do nothing;
 end if;
 if v_id is not null then perform runtime.nexloop_commitment_touch(new.tenant_id,new.world,v_id,clock_timestamp());end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return new;
end $$;
alter function runtime.nexloop_commitment_on_intent() owner to nexloop_owner;
revoke all on function runtime.nexloop_commitment_on_intent() from public;
create trigger nx026_commitment_intent before insert on runtime.nexloop_effect_intents for each row execute function runtime.nexloop_commitment_on_intent();
create trigger nx026_commitment_intent_state after update of state on runtime.nexloop_effect_intents for each row execute function runtime.nexloop_commitment_on_intent();

-- A delivered Agent reply bound to a commitment is a delivered message (NX-047 ledger).
create function runtime.nexloop_commitment_on_outbound() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);i runtime.nexloop_effect_intents;v_id text;
begin
 if new.delivery_state<>'delivered' or old.delivery_state='delivered' then return null;end if;
 perform set_config('eios.tenant_id',new.tenant_id,true);
 select * into i from runtime.nexloop_effect_intents where intent_id=new.intent_id and tenant_id=new.tenant_id and world=new.world;
 v_id:=substring(coalesce(i.frozen_request->'parameters'->>'commitment_ref','') from '^commitment:([0-9a-f]{64})$');
 if v_id is not null and exists(select 1 from runtime.nexloop_commitments c where c.tenant_id=new.tenant_id and c.world=new.world and c.commitment_id=v_id) then
  insert into runtime.nexloop_commitment_evidence(tenant_id,world,commitment_id,kind,ref,occurred_at,recorded_by,detail)
   values(new.tenant_id,new.world,v_id,'delivered_message','intent:'||new.intent_id,clock_timestamp(),'outbound-ledger',jsonb_build_object('trigger_message_id',new.trigger_message_id))
   on conflict do nothing;
  perform runtime.nexloop_commitment_touch(new.tenant_id,new.world,v_id,clock_timestamp());
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return null;
end $$;
alter function runtime.nexloop_commitment_on_outbound() owner to nexloop_owner;
revoke all on function runtime.nexloop_commitment_on_outbound() from public;
create trigger nx026_commitment_outbound after update of delivery_state on runtime.nexloop_outbound_messages
 for each row execute function runtime.nexloop_commitment_on_outbound();

-- The fallback reply Run (ADR-023 §2.7) may only answer the inbound message: it never carries a commitment reference.
create function runtime.nexloop_commitment_on_submission() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if authz.nexloop_is_fallback_run(new.run_id) and exists(select 1 from runtime.nexloop_effect_intents i
   where i.intent_id=new.intent_id and i.frozen_request->'parameters' ? 'commitment_ref') then
  raise exception 'fallback reply Run cannot act on a commitment' using errcode='42501';end if;
 return null;
end $$;
alter function runtime.nexloop_commitment_on_submission() owner to nexloop_owner;
revoke all on function runtime.nexloop_commitment_on_submission() from public;
create trigger nx026_commitment_submission after insert on runtime.nexloop_effect_submissions
 for each row execute function runtime.nexloop_commitment_on_submission();

-- 7. Work feeds -------------------------------------------------------------------------------------------------
alter table runtime.nexloop_work_feed drop constraint nexloop_work_feed_feed_check;
alter table runtime.nexloop_work_feed add constraint nexloop_work_feed_feed_check
 check(feed in ('recall-instance','claim-match','plan-reevaluate','reply-due','commitment-register','commitment-monitor'));

-- Work feed port: 0109 body, 'commitment-register' and 'commitment-monitor' added.
create or replace function authz.nexloop_work_feed(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;v_tenant text:=a->>'tenant_id';v_feed text:=c->>'feed';v_verb text:=c->>'verb';
 v_result jsonb;v_limit integer;v_lease integer;v_delay integer;v_max integer;r runtime.nexloop_work_feed%rowtype;ident jsonb;
begin
 if session_user<>'nexloop_domain_worker' or p_world is null or octet_length(p_text)>1048576 or octet_length(p_payload)>65536
  or v_feed is null or v_feed not in ('recall-instance','claim-match','plan-reevaluate','reply-due','commitment-register','commitment-monitor')
  or v_verb is null or v_verb not in ('claim','complete','retry','backlog')
  or a->>'protocol' is distinct from 'nexloop-work-feed-v1' or a->>'operation' is distinct from 'execute'
  or a->>'action_resource' is distinct from 'eios:action:NexLoop.feed.'||v_feed||':1' or a->>'resource_id' is distinct from a->>'action_resource'
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'work feed unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-work-feed-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'work feed unavailable' using errcode='42501';end if;
 if exists(select 1 from authz.nexloop_browser_token_realms where token_digest=p_digest)
  or not exists(select 1 from authz.nexloop_service_credentials t where t.token_digest=p_digest and t.tenant_id=v_tenant
     and t.binding->>'subject_kind'='service' and t.status='active') then
  raise exception 'work feed unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ident->'run_context' is not null and ident->'run_context'<>'null'::jsonb then raise exception 'work feed unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 perform set_config('eios.tenant_id',v_tenant,true);
 if v_verb='claim' then
  v_limit:=(c->>'limit')::integer;v_lease:=(c->>'lease_seconds')::integer;
  if v_limit is null or v_limit not between 1 and 50 or v_lease is null or v_lease not between 1 and 600 then raise exception 'work feed input invalid' using errcode='22023';end if;
  with due as (
   select f.item_key from runtime.nexloop_work_feed f where f.tenant_id=v_tenant and f.world=p_world and f.feed=v_feed and f.status='pending'
    and f.available_at<=clock_timestamp() and (f.lease_until is null or f.lease_until<=clock_timestamp())
   order by f.available_at,f.changed_at,f.item_key limit v_limit for update skip locked),
  leased as (
   update runtime.nexloop_work_feed f set lease_until=clock_timestamp()+make_interval(secs=>v_lease),lease_seq=f.change_seq,attempts=f.attempts+1
   from due where f.tenant_id=v_tenant and f.world=p_world and f.feed=v_feed and f.item_key=due.item_key
   returning f.item_key,f.payload,f.change_seq,f.attempts,f.available_at)
  select coalesce(jsonb_agg(jsonb_build_object('item_key',l.item_key,'payload',l.payload,'fence',l.change_seq,'attempts',l.attempts)
   order by l.available_at,l.item_key),'[]'::jsonb) into v_result from leased l;
 elsif v_verb in ('complete','retry') then
  select * into r from runtime.nexloop_work_feed where tenant_id=v_tenant and world=p_world and feed=v_feed and item_key=c->>'item_key' for update;
  -- A lost or replaced lease never completes or retries someone else's work.
  if not found or r.lease_seq is distinct from (c->>'fence')::bigint or r.lease_until<=clock_timestamp() then
   v_result:=jsonb_build_object('status','lease_lost');
  elsif v_verb='complete' then
   if r.change_seq=r.lease_seq then
    delete from runtime.nexloop_work_feed where tenant_id=v_tenant and world=p_world and feed=v_feed and item_key=r.item_key;
    v_result:=jsonb_build_object('status','completed');
   else
    -- A newer change arrived while working: keep it pending (it is processed again from scratch).
    update runtime.nexloop_work_feed set lease_until=null,lease_seq=null,attempts=0
     where tenant_id=v_tenant and world=p_world and feed=v_feed and item_key=r.item_key;
    v_result:=jsonb_build_object('status','changed');
   end if;
  else
   v_delay:=(c->>'delay_seconds')::integer;v_max:=(c->>'max_attempts')::integer;
   if v_delay is null or v_delay not between 0 and 3600 or v_max is null or v_max not between 1 and 20 or coalesce(c->>'code','')!~'^[a-z0-9_]{1,64}$' then
    raise exception 'work feed input invalid' using errcode='22023';end if;
   update runtime.nexloop_work_feed set lease_until=null,lease_seq=null,last_code=c->>'code',
    status=case when r.change_seq=r.lease_seq and r.attempts>=v_max then 'dead_lettered' else 'pending' end,
    available_at=case when r.change_seq=r.lease_seq then clock_timestamp()+make_interval(secs=>v_delay) else clock_timestamp() end,
    attempts=case when r.change_seq=r.lease_seq then r.attempts else 0 end
   where tenant_id=v_tenant and world=p_world and feed=v_feed and item_key=r.item_key
   returning jsonb_build_object('status',status) into v_result;
  end if;
 else
  select jsonb_build_object(
   'pending',(select count(*) from runtime.nexloop_work_feed f where f.tenant_id=v_tenant and f.world=p_world and f.feed=v_feed and f.status='pending'),
   'oldest_pending_seconds',(select floor(extract(epoch from clock_timestamp()-min(f.changed_at)))::bigint from runtime.nexloop_work_feed f
     where f.tenant_id=v_tenant and f.world=p_world and f.feed=v_feed and f.status='pending'),
   'dead_lettered',(select coalesce(jsonb_agg(jsonb_build_object('item_key',x.item_key,'attempts',x.attempts,'code',x.last_code) order by x.changed_at desc),'[]'::jsonb)
     from (select * from runtime.nexloop_work_feed f where f.tenant_id=v_tenant and f.world=p_world and f.feed=v_feed and f.status='dead_lettered'
      order by f.changed_at desc limit 20) x)) into v_result;
 end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 return v_result;
end $$;

-- 8. Registration derivation (no model, no caller-chosen value) ----------------------------------------------------
create function runtime.nexloop_commitment_prepare_claim(p_tenant text,p_world text,p_claim text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare cl ontology.nexloop_claims;om runtime.nexloop_outbound_messages;cm runtime.nexloop_conversation_messages;r runtime.nexloop_commitments;
 x runtime.nexloop_commitments;vt jsonb;v_due timestamptz;v_precision text;v_promised timestamptz;v_goal text;v_dedupe text;v_intent text;v_id text;
 v_props jsonb;v_supersedes text;v_existing jsonb;
begin
 select * into cl from ontology.nexloop_claims where tenant_id=p_tenant and world=p_world and claim_id=p_claim for update;
 if not found or cl.epistemic_kind<>'commitment' then raise exception 'commitment claim unavailable' using errcode='42501';end if;
 select * into r from runtime.nexloop_commitments c where c.tenant_id=p_tenant and c.world=p_world and c.claim_id=p_claim;
 if found then
  if r.registered_at is not null then return jsonb_build_object('action','registered','commitment_id',r.commitment_id);end if;
  return jsonb_build_object('action','create','commitment_id',r.commitment_id,'intent_id',r.intent_id,'properties',r.properties);
 end if;
 if exists(select 1 from runtime.nexloop_commitment_events e where e.tenant_id=p_tenant and e.world=p_world and e.subject_ref='claim:'||p_claim
   and e.kind in ('reaffirmed','skipped')) then return jsonb_build_object('action','done');end if;
 -- Only an enterprise's asserted or conditional promise from a delivered outbound Message is a commitment.
 if cl.speaker<>'agent' or cl.polarity<>'affirmed' or cl.modality not in ('asserted','conditional') or cl.resolution_state not in ('unresolved','needs_resolution') then
  insert into runtime.nexloop_commitment_events(tenant_id,world,subject_ref,kind,detail)
   values(p_tenant,p_world,'claim:'||p_claim,'skipped',jsonb_build_object('reason','not_a_commitment','speaker',cl.speaker,'polarity',cl.polarity,
    'modality',cl.modality,'resolution_state',cl.resolution_state));
  return jsonb_build_object('action','skipped','reason','not_a_commitment');
 end if;
 select * into om from runtime.nexloop_outbound_messages o where o.tenant_id=p_tenant and o.world=p_world and o.message_id=cl.source_message_id
  and o.delivery_state in ('provider_accepted','delivered');
 select * into cm from runtime.nexloop_conversation_messages m where m.tenant_id=p_tenant and m.world=p_world and m.message_id=cl.source_message_id
  and m.record->>'direction'='outbound';
 if om.intent_id is null or cm.message_id is null then
  insert into runtime.nexloop_commitment_events(tenant_id,world,subject_ref,kind,detail)
   values(p_tenant,p_world,'claim:'||p_claim,'skipped',jsonb_build_object('reason','source_not_delivered_outbound'));
  return jsonb_build_object('action','skipped','reason','source_not_delivered_outbound');
 end if;
 vt:=cl.valid_time;
 if vt->>'kind' in ('point','deadline','interval') and vt->>'status'='resolved' then
  v_due:=coalesce((case when vt->>'kind'='point' then vt->>'start' else vt->>'end' end)::timestamptz,(vt->>'start')::timestamptz);v_precision:='exact';
 elsif vt->>'kind' in ('point','deadline','interval') and vt->>'status'='ambiguous' then
  v_due:=coalesce((vt->'latest_bound_window'->>1)::timestamptz,(vt->>'end')::timestamptz);v_precision:='latest_bound';
 end if;
 if v_due is null then v_precision:='unspecified';end if;
 v_promised:=(cm.record->>'accepted_at')::timestamptz;
 select 'goal:'||b.goal_id||'@'||b.goal_version into v_goal from control.nexloop_run_goal_bindings b
  where b.tenant_id=p_tenant and b.world=p_world and b.run_id=om.run_id order by b.bound_at desc,b.goal_id limit 1;
 -- The same promise again in a later delivered message: reaffirmed, or a new commitment when the due date differs.
 select c.* into x from runtime.nexloop_commitments c join ontology.objects o on o.tenant_id=c.tenant_id and o.world=c.world
   and o.type_name='Commitment' and o.object_id=c.commitment_id
  where c.tenant_id=p_tenant and c.world=p_world and c.consumer_id=cl.consumer_id and c.correlation_key=cl.correlation_key
   and c.message_id<>cl.source_message_id and o.properties->>'status' in ('conditional','open','in_progress')
  order by c.prepared_at desc limit 1;
 if found then
  if (x.properties->>'due_at')::timestamptz is not distinct from v_due then
   insert into runtime.nexloop_commitment_events(tenant_id,world,subject_ref,kind,ref,detail)
    values(p_tenant,p_world,'commitment:'||x.commitment_id,'reaffirmed','claim:'||p_claim,jsonb_build_object('message_id',cl.source_message_id));
   insert into runtime.nexloop_commitment_events(tenant_id,world,subject_ref,kind,ref)
    values(p_tenant,p_world,'claim:'||p_claim,'reaffirmed','commitment:'||x.commitment_id);
   update ontology.nexloop_claims set resolution_state='resolved' where tenant_id=p_tenant and world=p_world and claim_id=p_claim;
   return jsonb_build_object('action','reaffirmed','commitment_id',x.commitment_id);
  end if;
  v_supersedes:=x.commitment_id;
 end if;
 v_dedupe:=encode(sha256(convert_to(p_tenant||'|'||p_world||'|'||cl.source_message_id||'|'||cl.correlation_key,'UTF8')),'hex');
 select * into r from runtime.nexloop_commitments c where c.tenant_id=p_tenant and c.world=p_world and c.dedupe_key=v_dedupe;
 if found then
  -- Another Claim of the same message and promise (re-extraction): the same commitment.
  if r.registered_at is not null then
   update ontology.nexloop_claims set resolution_state='resolved' where tenant_id=p_tenant and world=p_world and claim_id=p_claim and resolution_state in ('unresolved','needs_resolution');
   insert into runtime.nexloop_commitment_events(tenant_id,world,subject_ref,kind,ref)
    values(p_tenant,p_world,'claim:'||p_claim,'reaffirmed','commitment:'||r.commitment_id);
   return jsonb_build_object('action','reaffirmed','commitment_id',r.commitment_id);
  end if;
  return jsonb_build_object('action','create','commitment_id',r.commitment_id,'intent_id',r.intent_id,'properties',r.properties);
 end if;
 v_intent:='commitment-'||v_dedupe;
 v_id:=encode(sha256(convert_to(jsonb_build_array(p_tenant,p_world,'Commitment',v_intent)::text,'UTF8')),'hex');
 v_props:=jsonb_build_object('made_to',cl.consumer_id,
  'made_by',jsonb_build_object('sender_kind',om.sender_kind,'sender_principal',om.sender_principal,'role_ref',om.role_ref,'run_id',om.run_id::text,'intent_id',om.intent_id::text),
  'content_ref',jsonb_build_object('claim_id',cl.claim_id,'message_id',cl.source_message_id,'span',jsonb_build_array(cl.span_start,cl.span_end),'content_hash',cl.source_content_hash),
  'promised_at',runtime.nexloop_commitment_ts(v_promised),'due_at',case when v_due is null then null else runtime.nexloop_commitment_ts(v_due) end,
  'due_precision',v_precision,'condition',case when cl.modality='conditional' then cl.condition_text end,
  'status',case when cl.modality='conditional' then 'conditional' else 'open' end,'late',false,
  'fulfillment_basis','undetermined','fulfillment_evidence','[]'::jsonb,'related_goal_ref',v_goal,'supersedes',v_supersedes);
 insert into runtime.nexloop_commitments(tenant_id,world,commitment_id,dedupe_key,origin,claim_id,message_id,consumer_id,correlation_key,supersedes,intent_id,properties)
  values(p_tenant,p_world,v_id,v_dedupe,'claim',p_claim,cl.source_message_id,cl.consumer_id,cl.correlation_key,v_supersedes,v_intent,v_props);
 return jsonb_build_object('action','create','commitment_id',v_id,'intent_id',v_intent,'properties',v_props);
end $$;
alter function runtime.nexloop_commitment_prepare_claim(text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_commitment_prepare_claim(text,text,text) from public;

-- 9. Keeper port (nexloop_domain_worker; eios:action:nexloop.commitment.keep:1) -------------------------------------
create function authz.nexloop_commitment_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-commitment-keep-v1','eios:action:nexloop.commitment.keep:1',
  array['nexloop_domain_worker']);c jsonb:=p_payload::jsonb;r runtime.nexloop_commitments;o ontology.objects;v_patch jsonb;v_status text;v_last text;
 v_due timestamptz;v_lead integer;v_next timestamptz;v_now timestamptz:=clock_timestamp();v_subject text;v_restricted boolean;f runtime.nexloop_work_feed;
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
 if not found then raise exception 'commitment unavailable' using errcode='42501';end if;
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
   insert into runtime.nexloop_commitment_events(tenant_id,world,subject_ref,kind,detail)
    values(t,p_world,v_subject,'status',jsonb_build_object('status',v_status,'from',v_last,'late',o.properties->'late','revision',o.nexloop_revision,
     'fulfillment_evidence',o.properties->'fulfillment_evidence'));
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
     values(t,p_world,'commitment-monitor',v_subject,jsonb_build_object('commitment_id',r.commitment_id),v_next);
   elsif f.lease_seq is not distinct from (c->>'fence')::bigint and f.change_seq=f.lease_seq then
    update runtime.nexloop_work_feed set available_at=v_next,change_seq=change_seq+1,changed_at=clock_timestamp()
     where tenant_id=t and world=p_world and feed='commitment-monitor' and item_key=v_subject;
   end if;
  end if;
  return jsonb_build_object('commitment_id',r.commitment_id,'status',v_status,'next_at',v_next);
 end if;
 raise exception 'commitment command invalid' using errcode='22023';
end $$;
alter function authz.nexloop_commitment_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_commitment_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_commitment_command(text,text,text,text,text) to nexloop_domain_worker;

-- 10. Read port (eios:action:nexloop.commitment.read:1): owner query port before NX-028 ---------------------------
create function runtime.nexloop_commitment_view(p_tenant text,p_world text,p_commitment text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);r runtime.nexloop_commitments;o ontology.objects;v_quote text;v_result jsonb;v_subject text:='commitment:'||p_commitment;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into r from runtime.nexloop_commitments where tenant_id=p_tenant and world=p_world and commitment_id=p_commitment;
 select * into o from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='Commitment' and object_id=p_commitment;
 if r.commitment_id is null or o.object_id is null then perform set_config('eios.tenant_id',coalesce(prior,''),true);return null;end if;
 -- D7: the promised words are shown only while their Message exists.
 if r.message_id is not null and exists(select 1 from ontology.objects m where m.tenant_id=p_tenant and m.world=p_world and m.type_name='Message' and m.object_id=r.message_id) then
  select cl.quote into v_quote from ontology.nexloop_claims cl where cl.tenant_id=p_tenant and cl.world=p_world and cl.claim_id=r.claim_id;
 end if;
 v_result:=jsonb_build_object('commitment_id',p_commitment,'revision',o.nexloop_revision,'properties',o.properties,'quote',v_quote,
  'source_available',v_quote is not null or r.message_id is null,'origin',r.origin,'registered_at',r.registered_at,
  'evidence',jsonb_build_object(
   'requested',(select coalesce(jsonb_agg(jsonb_build_object('kind',x.kind,'ref',x.ref,'occurred_at',x.occurred_at) order by x.evidence_number),'[]'::jsonb)
     from runtime.nexloop_commitment_evidence x where x.tenant_id=p_tenant and x.world=p_world and x.commitment_id=p_commitment and x.kind='requested'),
   'delivered',(select coalesce(jsonb_agg(jsonb_build_object('kind',x.kind,'ref',x.ref,'occurred_at',x.occurred_at) order by x.evidence_number),'[]'::jsonb)
     from runtime.nexloop_commitment_evidence x where x.tenant_id=p_tenant and x.world=p_world and x.commitment_id=p_commitment
      and x.kind in ('effect_fulfilled','delivered_message','commercial_event','operator_attestation','agent_report')),
   'customer_confirmed',(select coalesce(jsonb_agg(jsonb_build_object('kind',x.kind,'ref',x.ref,'occurred_at',x.occurred_at) order by x.evidence_number),'[]'::jsonb)
     from runtime.nexloop_commitment_evidence x where x.tenant_id=p_tenant and x.world=p_world and x.commitment_id=p_commitment and x.kind='consumer_confirmation'),
   -- D5: Problem objects are not part of NX-026; this column is unavailable, never inferred.
   'problem_resolved','unavailable'),
  'events',(select coalesce(jsonb_agg(jsonb_build_object('kind',e.kind,'ref',e.ref,'detail',e.detail,'principal_id',e.principal_id,'recorded_at',e.recorded_at)
    order by e.event_number),'[]'::jsonb) from runtime.nexloop_commitment_events e where e.tenant_id=p_tenant and e.world=p_world and e.subject_ref=v_subject),
  'exceptions',(select coalesce(jsonb_agg(jsonb_build_object('reason',x.reason,'detail',x.detail,'raised_at',x.raised_at) order by x.raised_at,x.reason),'[]'::jsonb)
    from runtime.nexloop_commitment_exceptions x where x.tenant_id=p_tenant and x.world=p_world and x.subject_ref=v_subject));
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v_result;
end $$;
alter function runtime.nexloop_commitment_view(text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_commitment_view(text,text,text) from public;

create function authz.nexloop_commitment_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-commitment-read-v1','eios:action:nexloop.commitment.read:1',
  array['nexloop_api','nexloop_domain_worker']);c jsonb:=p_payload::jsonb;
begin
 perform set_config('eios.tenant_id',t,true);
 if c->>'verb'='commitments' then
  if c ? 'consumer_id' and coalesce(c->>'consumer_id','')!~'^[a-f0-9]{64}$' then raise exception 'commitment read invalid' using errcode='22023';end if;
  return jsonb_build_object('commitments',coalesce((select jsonb_agg(runtime.nexloop_commitment_view(t,p_world,r.commitment_id) order by r.prepared_at,r.commitment_id)
   from runtime.nexloop_commitments r join ontology.objects o on o.tenant_id=r.tenant_id and o.world=r.world and o.type_name='Commitment' and o.object_id=r.commitment_id
   where r.tenant_id=t and r.world=p_world and (not c ? 'consumer_id' or r.consumer_id=c->>'consumer_id')
    and (coalesce((c->>'all')::boolean,false) or o.properties->>'status' in ('conditional','open','in_progress','breached'))),'[]'::jsonb));
 elsif c->>'verb'='commitment' then
  if coalesce(c->>'commitment_id','')!~'^[0-9a-f]{64}$' then raise exception 'commitment read invalid' using errcode='22023';end if;
  return jsonb_build_object('commitment',runtime.nexloop_commitment_view(t,p_world,c->>'commitment_id'));
 elsif c->>'verb'='exceptions' then
  return jsonb_build_object('exceptions',coalesce((select jsonb_agg(jsonb_build_object('subject_ref',x.subject_ref,'consumer_id',x.consumer_id,'reason',x.reason,
    'detail',x.detail,'raised_at',x.raised_at) order by x.raised_at,x.subject_ref,x.reason)
   from runtime.nexloop_commitment_exceptions x where x.tenant_id=t and x.world=p_world),'[]'::jsonb));
 end if;
 raise exception 'commitment read invalid' using errcode='22023';
end $$;
alter function authz.nexloop_commitment_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_commitment_read(text,text,text,text,text) from public;
grant execute on function authz.nexloop_commitment_read(text,text,text,text,text) to nexloop_api,nexloop_domain_worker;

-- 11. Human-only commitment Actions (D4, D8; through the NX-022 governed entry) ------------------------------------
create function runtime.nexloop_commitment_human(p_tenant text,p_world text,p_principal text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare r runtime.nexloop_commitments;o ontology.objects;v_op text:=body->>'operation';v_subject text;v_status text;v_due timestamptz;
 v_props jsonb;v_intent text;v_id text;v_dedupe text;
begin
 if coalesce(body->>'commitment_id','')!~'^[0-9a-f]{64}$' or length(coalesce(body->>'reason','')) not between 1 and 500 then
  raise exception 'commitment action invalid' using errcode='22023';end if;
 select * into r from runtime.nexloop_commitments where tenant_id=p_tenant and world=p_world and commitment_id=body->>'commitment_id' for update;
 select * into o from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='Commitment' and object_id=body->>'commitment_id' for share;
 if r.commitment_id is null or o.object_id is null or r.registered_at is null then raise exception 'commitment unavailable' using errcode='22023';end if;
 v_subject:='commitment:'||r.commitment_id;v_status:=o.properties->>'status';
 if v_op='cancel_commitment' then
  if v_status not in ('conditional','open','in_progress') then raise exception 'commitment cannot be cancelled in state %',v_status using errcode='22023';end if;
  insert into runtime.nexloop_commitment_events(tenant_id,world,subject_ref,kind,ref,detail,principal_id)
   values(p_tenant,p_world,v_subject,'cancel_requested','intent:'||p_intent,jsonb_build_object('reason',body->>'reason'),p_principal);
 elsif v_op='commitment_condition_met' then
  if v_status<>'conditional' then raise exception 'commitment is not conditional' using errcode='22023';end if;
  insert into runtime.nexloop_commitment_events(tenant_id,world,subject_ref,kind,ref,detail,principal_id)
   values(p_tenant,p_world,v_subject,'condition_met','intent:'||p_intent,jsonb_build_object('reason',body->>'reason'),p_principal);
 elsif v_op='mark_commitment_communication' then
  if v_status not in ('conditional','open','in_progress','breached') or o.properties->>'fulfillment_basis'<>'undetermined' then
   raise exception 'commitment cannot be marked as communication' using errcode='22023';end if;
  insert into runtime.nexloop_commitment_events(tenant_id,world,subject_ref,kind,ref,detail,principal_id)
   values(p_tenant,p_world,v_subject,'marked_communication','intent:'||p_intent,jsonb_build_object('reason',body->>'reason'),p_principal);
 elsif v_op='attest_commitment' then
  if v_status not in ('open','in_progress','breached') then raise exception 'commitment cannot be attested in state %',v_status using errcode='22023';end if;
  if body->>'occurred_at' is null or (body->>'occurred_at')::timestamptz>clock_timestamp() then raise exception 'attested time required' using errcode='22023';end if;
  insert into runtime.nexloop_commitment_evidence(tenant_id,world,commitment_id,kind,ref,occurred_at,recorded_by,detail)
   values(p_tenant,p_world,r.commitment_id,'operator_attestation','intent:'||p_intent,(body->>'occurred_at')::timestamptz,p_principal,
    jsonb_build_object('reason',body->>'reason')) on conflict do nothing;
 elsif v_op='extend_commitment' then
  if v_status not in ('conditional','open','in_progress') then raise exception 'commitment cannot be extended in state %',v_status using errcode='22023';end if;
  v_due:=(body->>'due_at')::timestamptz;
  if v_due is null or v_due<=clock_timestamp() then raise exception 'new due date must be in the future' using errcode='22023';end if;
  v_dedupe:='extend:'||p_intent;v_intent:='commitment-'||encode(sha256(convert_to(p_tenant||'|'||p_world||'|'||v_dedupe,'UTF8')),'hex');
  v_id:=encode(sha256(convert_to(jsonb_build_array(p_tenant,p_world,'Commitment',v_intent)::text,'UTF8')),'hex');
  v_props:=o.properties||jsonb_build_object('due_at',runtime.nexloop_commitment_ts(v_due),'due_precision','exact',
   'status',case when v_status='conditional' then 'conditional' else 'open' end,'late',false,'fulfillment_evidence','[]'::jsonb,'supersedes',r.commitment_id);
  insert into runtime.nexloop_commitments(tenant_id,world,commitment_id,dedupe_key,origin,consumer_id,supersedes,intent_id,properties)
   values(p_tenant,p_world,v_id,v_dedupe,'extend',r.consumer_id,r.commitment_id,v_intent,v_props);
  insert into runtime.nexloop_commitment_events(tenant_id,world,subject_ref,kind,ref,detail,principal_id)
   values(p_tenant,p_world,v_subject,'extend_requested','intent:'||p_intent,jsonb_build_object('reason',body->>'reason','due_at',body->>'due_at','new_commitment_id',v_id),p_principal);
  perform authz.nexloop_work_feed_touch(p_tenant,p_world,'commitment-register','commitment:'||v_id,jsonb_build_object('commitment_id',v_id));
  return jsonb_build_object('commitment_id',r.commitment_id,'new_commitment_id',v_id);
 else raise exception 'commitment action invalid' using errcode='22023';
 end if;
 perform runtime.nexloop_commitment_touch(p_tenant,p_world,r.commitment_id,clock_timestamp());
 return jsonb_build_object('commitment_id',r.commitment_id);
end $$;
alter function runtime.nexloop_commitment_human(text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function runtime.nexloop_commitment_human(text,text,text,text,jsonb) from public;

-- NX-022 governed entry: 0109 body; commitment.* capabilities added (human subject only, like every owner change).
create or replace function authz.nexloop_goal_governed_action(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;body jsonb:=p_payload::jsonb;k bytea;stored runtime.nexloop_action_claims%rowtype;
 permit jsonb:=a->'permit';v_claim jsonb:=permit->'claim';ref jsonb:=v_claim->'binding'->'action_reference';d jsonb;
 ident jsonb;v_subject text;v_cap text;v_tenant text:=a->>'tenant_id';v_principal text:=a->>'principal_id';v_id text;v_outcome jsonb;v_result jsonb;
begin
 if octet_length(p_text)>1048576 or octet_length(p_payload)>262144 then raise exception 'goal command too large' using errcode='22023';end if;
 if a->>'expires_at' is null or permit->>'expires_at' is null or v_claim->>'lease_expires_at' is null then
  raise exception 'goal command expiry required' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-goal-governed-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'protocol' is distinct from 'nexloop-goal-governed-v1'
  or a->>'resource_id' is distinct from 'eios:action:'||(ref->>'stable_name')||':'||(ref->>'version')
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds'
  or v_claim->'binding'->>'request_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'goal permit rejected' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 select definition into d from control.nexloop_action_definitions where tenant_id=v_tenant and world=p_world and resource_id=a->>'resource_id' and active;
 v_cap:=d->'capability_binding'->>'capability_name';
 if d is null or d is distinct from a->'definition' or d->'governance'->>'approval_mode'<>'none'
  or jsonb_array_length(d->'governance'->'policy_refs')<>0
  or not (d->'governance'->'change_scope'->'target_systems' @> '["postgres"]'::jsonb)
  or body->>'operation' is distinct from (case v_cap when 'goals.metric.approve' then 'approve_metric' when 'goals.version.publish' then 'publish_goal'
     when 'goals.agent.propose' then 'propose_agent_goal' when 'goals.control.set' then 'set_control' when 'goals.budget.set' then 'set_budget'
     when 'goals.contact.release' then 'release_contact_restriction'
     when 'commitment.cancel' then 'cancel_commitment' when 'commitment.extend' then 'extend_commitment' when 'commitment.attest' then 'attest_commitment'
     when 'commitment.condition_met' then 'commitment_condition_met' when 'commitment.mark_communication' then 'mark_commitment_communication' end)
 then raise exception 'goal Action contract unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);v_subject:=ident->'binding'->>'subject_kind';
 if ident->'binding'->>'tenant_id' is distinct from v_tenant or ident->'binding'->>'subject_principal_id' is distinct from v_principal then
  raise exception 'goal identity binding mismatch' using errcode='42501';end if;
 if v_cap='goals.agent.propose' then
  if v_subject not in ('agent','human') then raise exception 'goal proposal requires an agent or human author' using errcode='42501';end if;
 elsif v_subject is distinct from 'human' then
  -- AT-005: only a human owner with a current EXECUTE grant can change
  -- owner goals, statistical definitions, controls or budgets.
  raise exception 'human goal authority required' using errcode='42501';
 end if;
 select * into stored from runtime.nexloop_action_claims where tenant_id=v_tenant and world=p_world
  and action_name=v_claim->'key'->>'action_stable_name' and intent_id=v_claim->'key'->>'idempotency_key' for update;
 if not found or stored.principal_id is distinct from v_principal or stored.claim is distinct from v_claim
  or v_claim->>'state'<>'active' or (v_claim->>'lease_expires_at')::timestamptz<=clock_timestamp()
  or (permit->>'expires_at')::timestamptz<=clock_timestamp() or body->>'request_id' is distinct from stored.intent_id then
  raise exception 'goal claim fenced' using errcode='40001';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 if v_cap in ('goals.version.publish','goals.agent.propose') then
  v_result:=control.nexloop_nx022_write_goal(v_tenant,p_world,v_principal,v_subject,stored.intent_id,body,v_cap='goals.agent.propose');
 elsif v_cap like 'commitment.%' then
  -- NX-026: commitment cancel / extend / attest / condition met / communication marking are human Actions only.
  v_result:=runtime.nexloop_commitment_human(v_tenant,p_world,v_principal,stored.intent_id,body);
 else
  -- ADR-023 §2.4: releasing a contact restriction is a human owner Action like any owner control change.
  if v_cap='goals.contact.release' then v_result:=control.nexloop_contact_release(v_tenant,p_world,v_principal,stored.intent_id,body);
  else v_result:=control.nexloop_nx022_owner_change(v_tenant,p_world,v_principal,stored.intent_id,body);end if;
 end if;
 v_id:=encode(sha256(convert_to(jsonb_build_array(v_tenant,p_world,body->>'operation',stored.intent_id)::text,'UTF8')),'hex');
 v_outcome:=jsonb_build_object('outcome_id',v_id,'outcome_revision',1,'status','succeeded','outcome_digest',v_claim->'binding'->>'request_digest','finalized_at',clock_timestamp());
 update runtime.nexloop_action_claims set claim=stored.claim||jsonb_build_object('state','terminal','terminal_outcome',v_outcome)
  where tenant_id=stored.tenant_id and world=p_world and action_name=stored.action_name and intent_id=stored.intent_id;
 -- Commit tail: writes stay provisional until authority, contract and fences are still current.
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 select definition into d from control.nexloop_action_definitions where tenant_id=v_tenant and world=p_world and resource_id=a->>'resource_id' and active for share;
 if not found or d is distinct from a->'definition' then raise exception 'goal Action changed at commit' using errcode='42501';end if;
 if (a->>'expires_at')::timestamptz<=clock_timestamp() or (permit->>'expires_at')::timestamptz<=clock_timestamp()
  or (v_claim->>'lease_expires_at')::timestamptz<=clock_timestamp()
  or not exists(select 1 from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active and key_material=k) then
  raise exception 'goal authority expired at commit' using errcode='42501';end if;
 return v_result||jsonb_build_object('outcome_id',v_id,'operation',body->>'operation');
end $$;
