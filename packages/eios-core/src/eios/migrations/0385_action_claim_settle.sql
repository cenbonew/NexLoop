-- Stale active action claims (stage 2026-10-31, dispatcher; L2 plan rev 3, approved): fifteen world-real claims stayed active with long
-- expired leases and nothing noticed them. Temporary number; the dispatcher renumbers (after L4 I31). Published migrations are not edited.
--
-- Diagnosis (read-only, DATA_HOST): every one is a settled fact, not an unknown result.
-- * 13 have adapter nexloop.core / target system postgres: a governed create (0010) or a goal-governed Action (0348) writes its business
--   rows and turns its claim terminal in one transaction, so a claim still active means that transaction rolled back and wrote nothing.
--   They come from the container clock (fixed by R59 ce46926a: leases were already past on the database clock) and the 10-22 relay stall.
-- * 2 are nexloop.service.request effects the owner confirmed not delivered on 10-18 (0210, on a not_found provider observation; the
--   intent and its outbox failed). 0325's failure rows came later without a backfill, and 0345 closes a claim only on a new failure row,
--   so these claims were never closed.
--
-- 1. runtime.nexloop_action_claim_settlements: one append-only record per settled claim - its category, the claim revision it ended, the
--    evidence ids, the reason, the deciding owner and the governed intent. Forced RLS, owner only.
-- 2. runtime.nexloop_action_claim_settle_check(tenant, world, action, intent, category): one code for whether a claim may be settled now
--    (eligible, not_found, not_active, not_stale, wrong_class, object_exists, intent_not_failed, no_determination, provider_observed,
--    outbound_delivered), used by the Action. Every case needs a world-real active claim whose lease expired more than an hour ago.
--    * uncommitted: binding adapter_id nexloop.core and target_system postgres, and no object written under the intent (source_ref = intent,
--      which also covers 0010's object id sha256(tenant, world, type, intent)).
--    * confirmed_not_delivered: an effect claim (not postgres) whose effect intent is failed with a confirmed_not_delivered determination
--      on a not_found observation, no accepted or fulfilled observation of it, and no outbound reply materialized or delivered (0079).
--    An effect claim in any other state is refused: an unknown result goes to the effect reconciliation, never here.
-- 3. Governed human capability action_claim.settle (operation settle_action_claim; owner only by the workbench role manifest): takes 0007's
--    advisory lock and the claim row, checks again, and ends the claim terminal permanent_failure - the frozen claim contract has no other
--    non-success status (succeeded / permanent_failure / compensated) and 0345 counts no permanent_failure claim anywhere. The outcome id is
--    the settlement id. A later reserve of the same intent replays that outcome and never runs again. No effect failure row is written:
--    one would wake the remediation scan (0338) and could resend a reply weeks later, and would count as a fresh defect (0345). One
--    settlement per claim; the same category replays.
-- 4. runtime.nexloop_metrics_snapshot is renamed once and wrapped (world real): action_claims {stale_active, stale_active_oldest_age_seconds,
--    stale_active_by_class {postgres, effect}} - active claims whose lease expired more than 5 minutes ago on the database clock; counts
--    only, zeros when there are none. Alert rules v26 (on v24; L4 I31 publishes v25) read it.

create table runtime.nexloop_action_claim_settlements (
 settlement_id uuid primary key default gen_random_uuid(),
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null check(world='real'),
 action_name text not null,intent_id text not null,
 category text not null check(category in ('uncommitted','confirmed_not_delivered')),
 claim_revision bigint not null check(claim_revision>0),evidence jsonb not null check(jsonb_typeof(evidence)='object'),
 reason text not null check(char_length(btrim(reason)) between 1 and 500),actor_principal text not null,request_intent text not null,
 settled_at timestamptz not null default clock_timestamp(),
 unique(tenant_id,world,action_name,intent_id)
);
alter table runtime.nexloop_action_claim_settlements owner to nexloop_owner;
alter table runtime.nexloop_action_claim_settlements enable row level security;
alter table runtime.nexloop_action_claim_settlements force row level security;
create policy tenant_boundary on runtime.nexloop_action_claim_settlements to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_action_claim_settlements from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;
create function runtime.nexloop_action_claim_settlements_append_only() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin raise exception 'action claim settlements are append-only' using errcode='42501';end $$;
alter function runtime.nexloop_action_claim_settlements_append_only() owner to nexloop_owner;
revoke all on function runtime.nexloop_action_claim_settlements_append_only() from public;
create trigger action_claim_settlements_append_only before update or delete or truncate on runtime.nexloop_action_claim_settlements
 for each statement execute function runtime.nexloop_action_claim_settlements_append_only();

-- 2. The check ------------------------------------------------------------------------------------------------------------------------
create function runtime.nexloop_action_claim_settle_check(p_tenant text,p_world text,p_action text,p_intent text,p_category text) returns text
 language plpgsql stable set search_path=pg_catalog,pg_temp as $$
declare c runtime.nexloop_action_claims;i runtime.nexloop_effect_intents;d runtime.nexloop_effect_determinations;
begin
 select * into c from runtime.nexloop_action_claims where tenant_id=p_tenant and world=p_world and action_name=p_action and intent_id=p_intent;
 if not found or p_world<>'real' then return 'not_found';end if;
 if c.claim->>'state' is distinct from 'active' then return 'not_active';end if;
 if (c.claim->>'lease_expires_at')::timestamptz>clock_timestamp()-interval '1 hour' then return 'not_stale';end if;
 if p_category='uncommitted' then
  if c.claim->'binding'->>'adapter_id' is distinct from 'nexloop.core' or c.claim->'binding'->>'target_system' is distinct from 'postgres' then
   return 'wrong_class';end if;
  if exists(select 1 from ontology.objects o where o.tenant_id=p_tenant and o.world=p_world and o.source_ref=p_intent) then return 'object_exists';end if;
  return 'eligible';
 end if;
 if p_category<>'confirmed_not_delivered' or c.claim->'binding'->>'target_system' is not distinct from 'postgres'
  or p_intent!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' then return 'wrong_class';end if;
 select * into i from runtime.nexloop_effect_intents where tenant_id=p_tenant and world=p_world and intent_id=p_intent::uuid and action_name=p_action;
 if not found or i.state<>'failed' then return 'intent_not_failed';end if;
 select * into d from runtime.nexloop_effect_determinations where tenant_id=p_tenant and world=p_world and intent_id=i.intent_id and kind='confirmed_not_delivered';
 if not found or not exists(select 1 from runtime.nexloop_effect_observations o where o.observation_id=d.observation_id and o.provider_state='not_found') then
  return 'no_determination';end if;
 if exists(select 1 from runtime.nexloop_effect_observations o where o.tenant_id=p_tenant and o.world=p_world and o.intent_id=i.intent_id
   and o.provider_state in ('accepted','fulfilled')) then return 'provider_observed';end if;
 if exists(select 1 from runtime.nexloop_outbound_messages m where m.tenant_id=p_tenant and m.world=p_world and m.intent_id=i.intent_id
   and (m.message_id is not null or m.delivery_state in ('provider_accepted','delivered'))) then return 'outbound_delivered';end if;
 return 'eligible';
end $$;
alter function runtime.nexloop_action_claim_settle_check(text,text,text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_action_claim_settle_check(text,text,text,text,text) from public;

-- 3. The governed Action --------------------------------------------------------------------------------------------------------------
create function runtime.nexloop_governed_settle_action_claim(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare c runtime.nexloop_action_claims;prior runtime.nexloop_action_claim_settlements;code text;v_id uuid;v_evidence jsonb;v_det uuid;v_obs uuid;
begin
 if jsonb_typeof(body) is distinct from 'object' or body->>'operation' is distinct from 'settle_action_claim'
  or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','action_name','intent_id','category','reason'))
  or jsonb_typeof(body->'action_name') is distinct from 'string' or body->>'action_name'!~'^[A-Za-z][A-Za-z0-9_.]{0,159}$'
  or jsonb_typeof(body->'intent_id') is distinct from 'string' or char_length(body->>'intent_id') not between 1 and 200
  or coalesce(body->>'category','') not in ('uncommitted','confirmed_not_delivered')
  or jsonb_typeof(body->'reason') is distinct from 'string' or char_length(btrim(body->>'reason')) not between 1 and 500 then
  raise exception 'action claim settle: invalid' using errcode='22023';end if;
 if control.nexloop_workbench_role(p_tenant,p_principal) is distinct from 'owner' then raise exception 'action claim settlement is owner only' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 -- 0007's order: the claim command's advisory lock, then the claim row; a concurrent reserve, finalize or 0345 close waits or wins.
 perform pg_advisory_xact_lock(hashtextextended('nexloop-action:'||p_tenant||':'||p_world||':'||(body->>'action_name')||':'||(body->>'intent_id'),0));
 select * into c from runtime.nexloop_action_claims where tenant_id=p_tenant and world=p_world and action_name=body->>'action_name'
  and intent_id=body->>'intent_id' for update;
 select * into prior from runtime.nexloop_action_claim_settlements where tenant_id=p_tenant and world=p_world and action_name=body->>'action_name'
  and intent_id=body->>'intent_id';
 if found then
  if prior.category<>body->>'category' then raise exception 'action claim settle: already_settled' using errcode='22023';end if;
  return jsonb_build_object('settlement_id',prior.settlement_id,'action_name',prior.action_name,'intent_id',prior.intent_id,'category',prior.category,
   'claim_revision',prior.claim_revision,'replayed',true);
 end if;
 code:=runtime.nexloop_action_claim_settle_check(p_tenant,p_world,body->>'action_name',body->>'intent_id',body->>'category');
 if code<>'eligible' then raise exception 'action claim settle: %',code using errcode='22023';end if;
 if body->>'category'='uncommitted' then
  v_evidence:=jsonb_build_object('adapter_id',c.claim->'binding'->>'adapter_id','target_system',c.claim->'binding'->>'target_system',
   'lease_expires_at',c.claim->'lease_expires_at','objects_under_intent',0);
 else
  select d.determination_id,d.observation_id into v_det,v_obs from runtime.nexloop_effect_determinations d
   where d.tenant_id=p_tenant and d.world=p_world and d.intent_id=(body->>'intent_id')::uuid and d.kind='confirmed_not_delivered';
  v_evidence:=jsonb_build_object('determination_id',v_det,'observation_id',v_obs,'intent_state','failed','lease_expires_at',c.claim->'lease_expires_at');
 end if;
 v_id:=pg_catalog.gen_random_uuid();
 insert into runtime.nexloop_action_claim_settlements(settlement_id,tenant_id,world,action_name,intent_id,category,claim_revision,evidence,reason,actor_principal,request_intent)
  values(v_id,p_tenant,p_world,c.action_name,c.intent_id,body->>'category',(c.claim->>'claim_revision')::bigint,v_evidence,btrim(body->>'reason'),p_principal,p_intent);
 -- claim_revision and the fence stay (the frozen claim contract forbids extra fields); the outcome id names the settlement.
 update runtime.nexloop_action_claims set claim=claim||jsonb_build_object('state','terminal','terminal_outcome',jsonb_build_object(
    'outcome_id',v_id::text,'outcome_revision',1,'status','permanent_failure',
    'outcome_digest',encode(sha256(convert_to(jsonb_build_object('settlement_id',v_id,'category',body->>'category','evidence',v_evidence)::text,'UTF8')),'hex'),
    'finalized_at',to_jsonb(clock_timestamp())))
  where tenant_id=c.tenant_id and world=c.world and action_name=c.action_name and intent_id=c.intent_id;
 return jsonb_build_object('settlement_id',v_id,'action_name',c.action_name,'intent_id',c.intent_id,'category',body->>'category',
  'claim_revision',(c.claim->>'claim_revision')::bigint,'replayed',false);
end $$;
alter function runtime.nexloop_governed_settle_action_claim(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function runtime.nexloop_governed_settle_action_claim(text,text,text,text,text,jsonb) from public;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('action_claim.settle','settle_action_claim','human','runtime.nexloop_governed_settle_action_claim(text,text,text,text,text,jsonb)','NX-057');

-- 4. Metrics --------------------------------------------------------------------------------------------------------------------------
create function runtime.nexloop_action_claim_health(p_tenant text,p_world text,p_at timestamptz) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);r jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select jsonb_build_object('stale_active',count(*),
   'stale_active_oldest_age_seconds',coalesce(floor(extract(epoch from p_at-min((c.claim->>'lease_expires_at')::timestamptz)))::bigint,0),
   'stale_active_by_class',jsonb_build_object(
    'postgres',count(*) filter(where c.claim->'binding'->>'target_system'='postgres'),
    'effect',count(*) filter(where c.claim->'binding'->>'target_system' is distinct from 'postgres')))
  into r from runtime.nexloop_action_claims c
  where c.tenant_id=p_tenant and c.world=p_world and c.claim->>'state'='active'
   and (c.claim->>'lease_expires_at')::timestamptz<p_at-interval '5 minutes';
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return r;
end $$;
alter function runtime.nexloop_action_claim_health(text,text,timestamptz) owner to nexloop_owner;
revoke all on function runtime.nexloop_action_claim_health(text,text,timestamptz) from public;

alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_action_claims;
revoke all on function runtime.nexloop_metrics_snapshot_before_action_claims(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp as $$
declare v jsonb:=runtime.nexloop_metrics_snapshot_before_action_claims(p_tenant,p_world);
begin
 if p_world is distinct from 'real' then return v;end if;
 return v||jsonb_build_object('action_claims',runtime.nexloop_action_claim_health(p_tenant,p_world,clock_timestamp()));
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;
