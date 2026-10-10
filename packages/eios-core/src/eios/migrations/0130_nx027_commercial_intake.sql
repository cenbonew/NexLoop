-- NX-027 (temporary number 0130): commercial events intake (M08), docs/implementation/NX-027-design.md §3, §8.
--
-- 1. A connector (deployment configuration through nexloop_configurator) is bound to one tenant, world and data mode;
--    a test connector only ever writes a non-real world and a real connector needs the owner's confirmation (D1, D2).
--    Its HMAC key is held by nexloop_owner; no application role can read it.
-- 2. POST /api/v1/webhooks/commercial/{connector} (nexloop_api) calls authz.nexloop_commercial_ingest with the raw
--    body, timestamp and signature. Tenant, world and data mode come only from the connector. Every arrival leaves a
--    receipt (verified / duplicate / conflict / rejected_* / replay_window / disabled / too_large); only a verified
--    first arrival of (connector, event_id) is stored, with its record key, and marks the record in the
--    'commercial-record' work feed in the same transaction (persist before ACK).
-- 3. The record keeper (service commercial_recorder, domain worker) prepares the one CommercialRecord state SQL
--    derives from all verified events of the record, the related refunds and the customer link, and applies it
--    through the governed CommercialRecord.create / CommercialRecord.edit Actions; the object guard accepts exactly
--    that state. The derivation is order independent: a late older event never overwrites newer state, a provider
--    correction of the amount is the canonically latest amount, a refund is its own record linked to the original.
--    Conflicts (currency change, refund without payment, refund over the amount) are owner-visible exceptions.
-- 4. A customer's own words never create or change a CommercialRecord (AT-011): the only writer is this chain.
-- 5. D6: deleting a Consumer pseudonymizes its customer references (random, not derived, no mapping kept); amounts,
--    currency, times and the correction/refund chains stay. Retention: financial_retention_days (settings).
-- Published migrations are not edited; the work feed port is replaced by its latest body (0111) plus one feed.

-- 0. Versioned settings (deploy/configuration/commercial.v1.json, byte-equal canonical JSON) -----------------------
-- Deployment configuration without a tenant dimension (as 0109 control.nexloop_reply_policies and 0111
-- control.nexloop_commitment_settings): control schema, owner-only, append-only, read only through the setting function.
create table control.nexloop_commercial_settings (
 version integer primary key check(version>=1),definition jsonb not null check(jsonb_typeof(definition)='object'),
 definition_digest text not null check(definition_digest~'^[0-9a-f]{64}$'),published_by text not null,
 published_at timestamptz not null default clock_timestamp(),check((definition->>'version')::integer=version),
 check(jsonb_typeof(definition->'event_types')='object' and jsonb_typeof(definition->'currency_exponents')='object'),
 check((definition->>'replay_window_seconds')::integer between 30 and 3600 and (definition->>'max_payload_bytes')::integer between 1024 and 262144),
 check((definition->>'financial_retention_days')::integer between 1 and 36500)
);
alter table control.nexloop_commercial_settings owner to nexloop_owner;
create trigger nx027_append_only before update or delete on control.nexloop_commercial_settings for each row execute function control.nexloop_nx022_append_only();
revoke all on control.nexloop_commercial_settings from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;
insert into control.nexloop_commercial_settings(version,definition,definition_digest,published_by)
 values(1,'{"channel_unit_rates":[],"currency_exponents":{"CNY":2,"EUR":2,"GBP":2,"HKD":2,"JPY":0,"KRW":0,"SGD":2,"USD":2},"decision":"NX-027 (M08/M19, dispatcher rulings 2026-10-10, owner decision D6): verified signed commercial events from a configured connector are the only source of CommercialRecord objects; a customer''s own words never are. A connector is bound to one world and data mode (test connectors only write the test world, D1). Event types map to a record kind and status; money is an integer amount in minor units with an ISO 4217 currency (exponents below); currencies are never added together. CommercialRecord objects and cost entries are kept financial_retention_days (default 365, owner decision D6, enterprise-configurable by a new version); raw events follow the event and message retention. Channel cost is recorded in units; an amount is computed only for actions with a unit rate here (D7). Initial values; owner-adjustable by a new version.","event_types":{"order.cancelled":{"kind":"order","status":"cancelled"},"order.created":{"kind":"order","status":"pending"},"order.paid":{"kind":"order","status":"paid"},"payment.failed":{"kind":"payment","status":"failed"},"payment.pending":{"kind":"payment","status":"pending"},"payment.succeeded":{"kind":"payment","status":"succeeded"},"refund.succeeded":{"kind":"refund","status":"succeeded"},"subscription.renewed":{"kind":"renewal","status":"succeeded"}},"financial_retention_days":365,"max_future_skew_seconds":300,"max_payload_bytes":65536,"replay_window_seconds":300,"schema":"nexloop-commercial/1","version":1,"worker":{"batch":20,"lease_seconds":120,"max_attempts":8,"retry_base_seconds":30}}'::jsonb,
  'c079d5b3cdc717b0a0c719babc935eff248e0a1e8dce2a0632a182ca29b76de5','deploy/configuration/commercial.v1.json');

create function runtime.nexloop_commercial_settings_current() returns jsonb
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select definition from control.nexloop_commercial_settings order by version desc limit 1
$$;
alter function runtime.nexloop_commercial_settings_current() owner to nexloop_owner;
revoke all on function runtime.nexloop_commercial_settings_current() from public;

create function runtime.nexloop_commercial_ts(p timestamptz) returns text
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select to_char(p at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
$$;
alter function runtime.nexloop_commercial_ts(timestamptz) owner to nexloop_owner;

-- Record key and object identity: one CommercialRecord per (tenant, world, connector, kind, external id). The object id
-- is the governed creation's id of the intent 'commercial-<key>' (same derivation as the object create path).
create function runtime.nexloop_commercial_key(p_tenant text,p_world text,p_connector text,p_kind text,p_external text) returns text
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select encode(sha256(convert_to(jsonb_build_array('commercial',p_tenant,p_world,p_connector,p_kind,p_external)::text,'UTF8')),'hex')
$$;
alter function runtime.nexloop_commercial_key(text,text,text,text,text) owner to nexloop_owner;
create function runtime.nexloop_commercial_object_id(p_tenant text,p_world text,p_key text) returns text
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select encode(sha256(convert_to(jsonb_build_array(p_tenant,p_world,'CommercialRecord','commercial-'||p_key)::text,'UTF8')),'hex')
$$;
alter function runtime.nexloop_commercial_object_id(text,text,text) owner to nexloop_owner;

-- 1. Connectors and customer links (deployment configuration, nexloop_configurator) ----------------------------------
create table control.nexloop_commercial_connectors (
 connector_id text primary key check(connector_id~'^[a-z0-9][a-z0-9-]{2,62}$'),
 tenant_id text not null,world text not null,data_mode text not null check(data_mode in ('real','test')),
 -- D1: test connectors only write a non-real world; real ones only the real world, with the owner's confirmation (D2).
 check((data_mode='real')=(world='real')),owner_confirmation text,check(data_mode<>'real' or char_length(owner_confirmation) between 1 and 500),
 accepted_types text[] not null check(cardinality(accepted_types) between 1 and 32),
 currencies text[] not null check(cardinality(currencies) between 1 and 32),
 key_id text not null check(key_id~'^[A-Za-z0-9._-]{1,64}$'),key_material bytea not null check(octet_length(key_material) between 32 and 128),
 previous_key_id text,previous_key_material bytea,previous_valid_until timestamptz,
 check((previous_key_id is null)=(previous_key_material is null) and (previous_key_id is null)=(previous_valid_until is null)),
 status text not null default 'active' check(status in ('active','disabled')),revision integer not null default 1 check(revision>=1),
 configured_at timestamptz not null default clock_timestamp()
);
create table control.nexloop_commercial_customer_links (
 connector_id text not null references control.nexloop_commercial_connectors(connector_id),
 customer_ref text not null check(char_length(customer_ref) between 1 and 200),
 tenant_id text not null,world text not null,consumer_id text not null check(consumer_id~'^[a-f0-9]{64}$'),
 linked_at timestamptz not null default clock_timestamp(),primary key(connector_id,customer_ref)
);
create index nexloop_commercial_links_consumer on control.nexloop_commercial_customer_links(tenant_id,world,consumer_id);

-- 2. Receipts, verified events, record registry, exceptions -------------------------------------------------------
-- One receipt per arrival (counts and audit; no payload, no signature).
create table runtime.nexloop_commercial_receipts (
 receipt_number bigint generated always as identity primary key,tenant_id text not null,world text not null,
 connector_id text not null,source_event_id text,payload_digest text check(payload_digest~'^[0-9a-f]{64}$'),
 outcome text not null check(outcome in ('verified','duplicate','conflict','rejected_signature','rejected_schema','replay_window','disabled','too_large')),
 reason text check(reason~'^[a-z_]{1,64}$'),received_at timestamptz not null default clock_timestamp()
);
create index nexloop_commercial_receipts_event on runtime.nexloop_commercial_receipts(tenant_id,world,connector_id,source_event_id);
-- Verified first arrivals only (evidence layer; never declares a payment by itself).
create table runtime.nexloop_commercial_events (
 tenant_id text not null,world text not null,data_mode text not null check(data_mode in ('real','test')),check((data_mode='real')=(world='real')),
 connector_id text not null,source_event_id text not null check(char_length(source_event_id) between 1 and 200),
 received_sequence bigint generated always as identity,provider_sequence bigint check(provider_sequence>=0),
 event_type text not null,record_kind text not null check(record_kind in ('order','payment','renewal','refund')),
 record_key text not null check(record_key~'^[0-9a-f]{64}$'),related_key text check(related_key~'^[0-9a-f]{64}$'),
 status text not null check(status~'^[a-z_]{1,32}$'),occurred_at timestamptz not null,received_at timestamptz not null default clock_timestamp(),
 amount_minor bigint not null check(amount_minor between 0 and 1000000000000000),currency text not null check(currency~'^[A-Z]{3}$'),
 customer_ref text not null check(char_length(customer_ref) between 1 and 200),payload_digest text not null check(payload_digest~'^[0-9a-f]{64}$'),
 signature_key_id text not null,disposition text check(disposition in ('applied','late')),recorded_at timestamptz,
 check((disposition is null)=(recorded_at is null)),
 primary key(tenant_id,world,connector_id,source_event_id),unique(received_sequence)
);
create index nexloop_commercial_events_record on runtime.nexloop_commercial_events(tenant_id,world,record_key);
create index nexloop_commercial_events_related on runtime.nexloop_commercial_events(tenant_id,world,related_key) where related_key is not null;
create index nexloop_commercial_events_customer on runtime.nexloop_commercial_events(connector_id,customer_ref);
-- One registry row per record (written at the first verified event).
create table runtime.nexloop_commercial_records (
 tenant_id text not null,world text not null,record_key text not null check(record_key~'^[0-9a-f]{64}$'),
 data_mode text not null check(data_mode in ('real','test')),check((data_mode='real')=(world='real')),
 connector_id text not null,record_kind text not null check(record_kind in ('order','payment','renewal','refund')),
 external_id text not null check(char_length(external_id) between 1 and 200),currency text not null check(currency~'^[A-Z]{3}$'),
 intent_id text not null,object_id text not null check(object_id~'^[0-9a-f]{64}$'),created_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,record_key),unique(tenant_id,world,object_id)
);
create table runtime.nexloop_commercial_exceptions (
 tenant_id text not null,world text not null,subject_ref text not null check(char_length(subject_ref) between 8 and 400 and subject_ref~'^(record:[0-9a-f]{64}|event:.+)$'),
 reason text not null check(reason~'^[a-z_]{1,64}$'),detail jsonb not null check(jsonb_typeof(detail)='object'),
 raised_at timestamptz not null default clock_timestamp(),primary key(tenant_id,world,subject_ref,reason)
);
do $tables$
declare t text;
begin
 foreach t in array array['runtime.nexloop_commercial_receipts','runtime.nexloop_commercial_events','runtime.nexloop_commercial_records',
   'runtime.nexloop_commercial_exceptions','control.nexloop_commercial_connectors','control.nexloop_commercial_customer_links'] loop
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('revoke all on %s from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator',t);
 end loop;
 foreach t in array array['runtime.nexloop_commercial_receipts','runtime.nexloop_commercial_events','runtime.nexloop_commercial_records',
   'runtime.nexloop_commercial_exceptions','control.nexloop_commercial_customer_links'] loop
  execute format('create policy tenant_boundary on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
 end loop;
end $tables$;
-- Connectors are looked up by their URL id before the tenant is known (owner functions only).
create policy connector_lookup on control.nexloop_commercial_connectors to nexloop_owner using(true) with check(true);
create trigger nx027_append_only before update or delete on runtime.nexloop_commercial_receipts for each row execute function control.nexloop_nx022_append_only();
create trigger nx027_append_only before update or delete on runtime.nexloop_commercial_records for each row execute function control.nexloop_nx022_append_only();
create trigger nx027_append_only before update or delete on runtime.nexloop_commercial_exceptions for each row execute function control.nexloop_nx022_append_only();
-- Events: the disposition is set once; D6 pseudonymization only replaces the customer reference.
create function runtime.nexloop_commercial_event_guard() returns trigger
 language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if tg_op='DELETE' then raise exception 'commercial events leave only by retention (NX-029)' using errcode='42501';end if;
 if (to_jsonb(new)-'disposition'-'recorded_at'-'customer_ref') is distinct from (to_jsonb(old)-'disposition'-'recorded_at'-'customer_ref')
  or (old.disposition is not null and (new.disposition is distinct from old.disposition or new.recorded_at is distinct from old.recorded_at))
  or (new.customer_ref is distinct from old.customer_ref and (new.customer_ref!~'^pseudonym:[0-9a-f]{32}$' or old.customer_ref~'^pseudonym:'))
 then raise exception 'commercial event is immutable' using errcode='42501';end if;
 return new;
end $$;
alter function runtime.nexloop_commercial_event_guard() owner to nexloop_owner;
create trigger nx027_event_guard before update or delete on runtime.nexloop_commercial_events for each row execute function runtime.nexloop_commercial_event_guard();
-- Customer links: the configurator links or unlinks; D6 removes the links of a deleted Consumer.
create trigger nx027_link_no_update before update on control.nexloop_commercial_customer_links for each row execute function control.nexloop_nx022_append_only();

-- 3. Feed mark ------------------------------------------------------------------------------------------------------
create function runtime.nexloop_commercial_touch(p_tenant text,p_world text,p_key text) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 insert into runtime.nexloop_work_feed as f(tenant_id,world,feed,item_key,payload,available_at)
  values(p_tenant,p_world,'commercial-record','record:'||p_key,jsonb_build_object('record_key',p_key),clock_timestamp())
 on conflict(tenant_id,world,feed,item_key) do update set change_seq=f.change_seq+1,changed_at=clock_timestamp(),
  available_at=clock_timestamp(),attempts=0,status='pending',last_code=null;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
end $$;
alter function runtime.nexloop_commercial_touch(text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_commercial_touch(text,text,text) from public;

create function runtime.nexloop_commercial_raise(p_tenant text,p_world text,p_subject text,p_reason text,p_detail jsonb) returns boolean
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v_new boolean;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 insert into runtime.nexloop_commercial_exceptions(tenant_id,world,subject_ref,reason,detail) values(p_tenant,p_world,p_subject,p_reason,p_detail)
  on conflict do nothing;
 v_new:=found;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v_new;
end $$;
alter function runtime.nexloop_commercial_raise(text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function runtime.nexloop_commercial_raise(text,text,text,text,jsonb) from public;

-- 4. The one record state its verified events justify (order independent) ------------------------------------------
-- Canonical order of a record's events: business time, then provider sequence, then arrival. The status is the
-- highest-ranked status, latest in that order (a late older event never moves it back); the amount is the canonically
-- latest amount in the record's first currency (provider corrections); refunds are refund records linked by key.
create function runtime.nexloop_commercial_target(p_tenant text,p_world text,p_key text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);r runtime.nexloop_commercial_records;v_currency text;v_first timestamptz;
 v_amount bigint;v_status text;v_status_at timestamptz;v_count integer;v_late integer;v_seq bigint;v_last bigint;v_customer text;
 v_consumer text;v_link text;v_refunded bigint:=0;v_refund_at timestamptz;v_other integer:=0;v_related_key text;v_related_keys integer;
 v_conflicts jsonb:='[]'::jsonb;v_props jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into r from runtime.nexloop_commercial_records where tenant_id=p_tenant and world=p_world and record_key=p_key;
 if not found then perform set_config('eios.tenant_id',coalesce(prior,''),true);return null;end if;
 -- The record's currency is fixed by its first arrival (registry); the earliest business time may move with late events.
 v_currency:=r.currency;
 select min(e.occurred_at) into v_first from runtime.nexloop_commercial_events e
  where e.tenant_id=p_tenant and e.world=p_world and e.record_key=p_key and e.currency=v_currency;
 if exists(select 1 from runtime.nexloop_commercial_events e where e.tenant_id=p_tenant and e.world=p_world and e.record_key=p_key and e.currency<>v_currency) then
  v_conflicts:=v_conflicts||jsonb_build_object('reason','currency_changed','currency',v_currency);end if;
 select e.amount_minor into v_amount from runtime.nexloop_commercial_events e
  where e.tenant_id=p_tenant and e.world=p_world and e.record_key=p_key and e.currency=v_currency
  order by e.occurred_at desc,coalesce(e.provider_sequence,-1) desc,e.received_sequence desc limit 1;
 select e.status,e.occurred_at into v_status,v_status_at from runtime.nexloop_commercial_events e
  where e.tenant_id=p_tenant and e.world=p_world and e.record_key=p_key and e.currency=v_currency
  order by case e.status when 'pending' then 0 else 1 end desc,e.occurred_at desc,coalesce(e.provider_sequence,-1) desc,e.received_sequence desc limit 1;
 select count(*),max(e.provider_sequence),max(e.received_sequence) into v_count,v_seq,v_last from runtime.nexloop_commercial_events e
  where e.tenant_id=p_tenant and e.world=p_world and e.record_key=p_key;
 -- Late: received after an event that is later in the canonical order.
 select count(*) into v_late from runtime.nexloop_commercial_events e where e.tenant_id=p_tenant and e.world=p_world and e.record_key=p_key
  and exists(select 1 from runtime.nexloop_commercial_events x where x.tenant_id=p_tenant and x.world=p_world and x.record_key=p_key
   and x.received_sequence<e.received_sequence
   and (x.occurred_at,coalesce(x.provider_sequence,-1),x.received_sequence)>(e.occurred_at,coalesce(e.provider_sequence,-1),e.received_sequence));
 select e.customer_ref into v_customer from runtime.nexloop_commercial_events e where e.tenant_id=p_tenant and e.world=p_world and e.record_key=p_key
  order by e.occurred_at desc,coalesce(e.provider_sequence,-1) desc,e.received_sequence desc limit 1;
 if v_customer~'^pseudonym:' then v_link:='pseudonymized';
 else
  select l.consumer_id into v_consumer from control.nexloop_commercial_customer_links l
   where l.connector_id=r.connector_id and l.customer_ref=v_customer and l.tenant_id=p_tenant and l.world=p_world;
  v_link:=case when v_consumer is null then 'unlinked' else 'linked' end;
 end if;
 if r.record_kind='refund' then
  select count(distinct e.related_key),min(e.related_key) into v_related_keys,v_related_key from runtime.nexloop_commercial_events e
   where e.tenant_id=p_tenant and e.world=p_world and e.record_key=p_key;
  if v_related_keys<>1 then v_conflicts:=v_conflicts||jsonb_build_object('reason','refund_target_changed');end if;
 else
  -- Each refund record counts with its own canonically latest amount; another currency is never added (conflict).
  with refunds as (
   select distinct on (e.record_key) e.record_key,e.amount_minor,e.currency,e.occurred_at,e.status from runtime.nexloop_commercial_events e
    where e.tenant_id=p_tenant and e.world=p_world and e.related_key=p_key and e.record_kind='refund'
    order by e.record_key,e.occurred_at desc,coalesce(e.provider_sequence,-1) desc,e.received_sequence desc)
  select coalesce(sum(amount_minor) filter (where currency=v_currency and status='succeeded'),0),count(*) filter (where currency<>v_currency),
   max(occurred_at) filter (where currency=v_currency and status='succeeded')
   into v_refunded,v_other,v_refund_at from refunds;
  if v_other>0 then v_conflicts:=v_conflicts||jsonb_build_object('reason','refund_currency_mismatch','currency',v_currency);end if;
  if v_refunded>0 then
   if v_status in ('paid','succeeded') then
    v_status:=case when v_refunded>=v_amount then 'refunded' else 'refunded_partial' end;v_status_at:=greatest(v_status_at,v_refund_at);
    -- Never truncated: the owner decides an over-refund.
    if v_refunded>v_amount then v_conflicts:=v_conflicts||jsonb_build_object('reason','refund_exceeds_amount','amount_minor',v_amount,'refunded_minor',v_refunded);end if;
   else
    v_conflicts:=v_conflicts||jsonb_build_object('reason','refund_without_payment','status',v_status);
   end if;
  end if;
 end if;
 v_props:=jsonb_build_object('kind',r.record_kind,'external_id',r.external_id,'connector_id',r.connector_id,'data_mode',r.data_mode,
  'currency',v_currency,'occurred_at',runtime.nexloop_commercial_ts(v_first),
  'related_record_id',case when v_related_key is null then null else runtime.nexloop_commercial_object_id(p_tenant,p_world,v_related_key) end,
  'verification_state','verified','status',v_status,'status_at',runtime.nexloop_commercial_ts(v_status_at),'amount_minor',v_amount,
  'refunded_minor',v_refunded,'consumer_ref',v_consumer,'link_state',v_link,'event_count',v_count,'late_event_count',v_late,
  'provider_sequence',v_seq,'last_event_ref','commercial-event:'||v_last);
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return jsonb_build_object('properties',v_props,'conflicts',v_conflicts);
end $$;
alter function runtime.nexloop_commercial_target(text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_commercial_target(text,text,text) from public;

-- 5. Object guard: CommercialRecord state is exactly the derived state; never deleted outside retention -------------
create function ontology.nexloop_commercial_record_guard() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);r runtime.nexloop_commercial_records;v jsonb;
begin
 if tg_op='DELETE' then
  if old.type_name='CommercialRecord' then raise exception 'a CommercialRecord leaves only by retention (NX-029)' using errcode='42501';end if;
  return old;
 end if;
 if new.type_name<>'CommercialRecord' and (tg_op='INSERT' or old.type_name<>'CommercialRecord') then return new;end if;
 if tg_op='UPDATE' and (old.type_name<>'CommercialRecord' or new.type_name<>'CommercialRecord' or new.object_id<>old.object_id
   or new.tenant_id<>old.tenant_id or new.world<>old.world) then raise exception 'commercial record identity is immutable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',new.tenant_id,true);
 select * into r from runtime.nexloop_commercial_records c where c.tenant_id=new.tenant_id and c.world=new.world and c.object_id=new.object_id;
 if not found or (tg_op='INSERT' and r.intent_id is distinct from new.source_ref) then
  raise exception 'a CommercialRecord comes only from verified commercial events' using errcode='42501';end if;
 v:=runtime.nexloop_commercial_target(new.tenant_id,new.world,r.record_key);
 if v is null or new.properties is distinct from v->'properties' then
  raise exception 'commercial record state is not derived from its verified events' using errcode='23514';end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return new;
end $$;
alter function ontology.nexloop_commercial_record_guard() owner to nexloop_owner;
revoke all on function ontology.nexloop_commercial_record_guard() from public;
create trigger nx027_commercial_record_guard before insert or update or delete on ontology.objects
 for each row execute function ontology.nexloop_commercial_record_guard();

-- 6. Signed intake (nexloop_api; POST /api/v1/webhooks/commercial/{connector}) -----------------------------------
create function runtime.nexloop_commercial_receipt(p_connector control.nexloop_commercial_connectors,p_event text,p_digest text,p_outcome text,p_reason text)
 returns void language sql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 insert into runtime.nexloop_commercial_receipts(tenant_id,world,connector_id,source_event_id,payload_digest,outcome,reason)
  values(p_connector.tenant_id,p_connector.world,p_connector.connector_id,p_event,p_digest,p_outcome,p_reason)
$$;
alter function runtime.nexloop_commercial_receipt(control.nexloop_commercial_connectors,text,text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_commercial_receipt(control.nexloop_commercial_connectors,text,text,text,text) from public;

create function authz.nexloop_commercial_ingest(p_connector text,p_timestamp text,p_signature text,p_body text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare cn control.nexloop_commercial_connectors;s jsonb:=runtime.nexloop_commercial_settings_current();v_now timestamptz:=clock_timestamp();
 v_digest text;v_given text;v_key_id text;body jsonb;v_type jsonb;v_event text;v_kind text;v_occ timestamptz;v_key text;v_related text;
 v_existing text;v_inserted bigint;v_reason text;
begin
 if session_user<>'nexloop_api' then raise exception 'commercial intake unavailable' using errcode='42501';end if;
 if p_connector is null or p_connector!~'^[a-z0-9][a-z0-9-]{2,62}$' then return jsonb_build_object('status',404,'outcome','unknown_connector');end if;
 select * into cn from control.nexloop_commercial_connectors where connector_id=p_connector;
 if not found then return jsonb_build_object('status',404,'outcome','unknown_connector');end if;
 perform set_config('eios.tenant_id',cn.tenant_id,true);
 if cn.status<>'active' then
  perform runtime.nexloop_commercial_receipt(cn,null,null,'disabled',null);return jsonb_build_object('status',403,'outcome','disabled');end if;
 if p_body is null or octet_length(p_body)>(s->>'max_payload_bytes')::integer then
  perform runtime.nexloop_commercial_receipt(cn,null,null,'too_large',null);return jsonb_build_object('status',413,'outcome','too_large');end if;
 v_digest:=encode(sha256(convert_to(p_body,'UTF8')),'hex');
 -- Replay window first: an out-of-window request is only counted (no evidence row).
 if coalesce(p_timestamp,'')!~'^[0-9]{1,12}$' or abs(extract(epoch from v_now)-p_timestamp::bigint)>(s->>'replay_window_seconds')::integer then
  perform runtime.nexloop_commercial_receipt(cn,null,null,'replay_window',null);return jsonb_build_object('status',401,'outcome','replay_window');end if;
 -- HMAC-SHA256 over '<timestamp>.<body>' with the current key, or the previous key during its rotation window.
 -- Both sides are hashed before the comparison (no prefix timing of the expected value).
 v_given:=case when coalesce(p_signature,'')~'^v1=[0-9a-f]{64}$' then substr(p_signature,4) end;
 if v_given is not null and sha256(convert_to(v_given,'UTF8'))=sha256(convert_to(encode(extensions.hmac(convert_to(p_timestamp||'.'||p_body,'UTF8'),cn.key_material,'sha256'),'hex'),'UTF8')) then
  v_key_id:=cn.key_id;
 elsif v_given is not null and cn.previous_key_material is not null and cn.previous_valid_until>v_now
  and sha256(convert_to(v_given,'UTF8'))=sha256(convert_to(encode(extensions.hmac(convert_to(p_timestamp||'.'||p_body,'UTF8'),cn.previous_key_material,'sha256'),'hex'),'UTF8')) then
  v_key_id:=cn.previous_key_id;
 else
  perform runtime.nexloop_commercial_receipt(cn,null,v_digest,'rejected_signature',null);return jsonb_build_object('status',401,'outcome','rejected_signature');
 end if;
 -- Schema (strict; no additional field; integer minor units; ISO currency with a known exponent).
 begin body:=p_body::jsonb;exception when others then body:=null;end;
 v_reason:=case
  when body is null or jsonb_typeof(body)<>'object' then 'not_an_object'
  when exists(select 1 from jsonb_object_keys(body) k where k not in ('event_id','type','external_id','occurred_at','amount_minor','currency',
    'customer_ref','provider_sequence','related_external_id','related_kind')) then 'unknown_field'
  when jsonb_typeof(body->'event_id') is distinct from 'string' or char_length(body->>'event_id') not between 1 and 200 then 'event_id'
  when jsonb_typeof(body->'type') is distinct from 'string' or not (s->'event_types' ? (body->>'type')) or not ((body->>'type')=any(cn.accepted_types)) then 'type'
  when jsonb_typeof(body->'external_id') is distinct from 'string' or char_length(body->>'external_id') not between 1 and 200 then 'external_id'
  when jsonb_typeof(body->'customer_ref') is distinct from 'string' or char_length(body->>'customer_ref') not between 1 and 200
   or (body->>'customer_ref')~'^pseudonym:' then 'customer_ref'
  when jsonb_typeof(body->'amount_minor') is distinct from 'number' or (body->>'amount_minor')!~'^[0-9]{1,16}$'
   or (body->>'amount_minor')::numeric>1000000000000000 then 'amount_minor'
  when jsonb_typeof(body->'currency') is distinct from 'string' or not (s->'currency_exponents' ? (body->>'currency')) or not ((body->>'currency')=any(cn.currencies)) then 'currency'
  when body ? 'provider_sequence' and (jsonb_typeof(body->'provider_sequence')<>'number' or (body->>'provider_sequence')!~'^[0-9]{1,18}$') then 'provider_sequence'
  when jsonb_typeof(body->'occurred_at') is distinct from 'string' or (body->>'occurred_at')!~'^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+(Z|[+-][0-9]{2}:?[0-9]{2})$' then 'occurred_at'
  else null end;
 if v_reason is null then
  v_type:=s->'event_types'->(body->>'type');v_kind:=v_type->>'kind';
  if v_kind='refund' and (jsonb_typeof(body->'related_external_id') is distinct from 'string' or char_length(body->>'related_external_id') not between 1 and 200
    or coalesce(body->>'related_kind','') not in ('order','payment','renewal')) then v_reason:='refund_target';
  elsif v_kind<>'refund' and (body ? 'related_external_id' or body ? 'related_kind') then v_reason:='refund_target';end if;
 end if;
 if v_reason is null then
  begin v_occ:=(body->>'occurred_at')::timestamptz;exception when others then v_reason:='occurred_at';end;
  if v_reason is null and v_occ>v_now+make_interval(secs=>(s->>'max_future_skew_seconds')::integer) then v_reason:='occurred_at_future';end if;
 end if;
 if v_reason is not null then
  perform runtime.nexloop_commercial_receipt(cn,case when jsonb_typeof(body->'event_id')='string' then left(body->>'event_id',200) end,v_digest,'rejected_schema',v_reason);
  return jsonb_build_object('status',400,'outcome','rejected_schema','reason',v_reason);
 end if;
 v_event:=body->>'event_id';
 v_key:=runtime.nexloop_commercial_key(cn.tenant_id,cn.world,cn.connector_id,v_kind,body->>'external_id');
 if v_kind='refund' then v_related:=runtime.nexloop_commercial_key(cn.tenant_id,cn.world,cn.connector_id,body->>'related_kind',body->>'related_external_id');end if;
 insert into runtime.nexloop_commercial_events(tenant_id,world,data_mode,connector_id,source_event_id,provider_sequence,event_type,record_kind,record_key,
   related_key,status,occurred_at,amount_minor,currency,customer_ref,payload_digest,signature_key_id)
  values(cn.tenant_id,cn.world,cn.data_mode,cn.connector_id,v_event,(body->>'provider_sequence')::bigint,body->>'type',v_kind,v_key,v_related,
   v_type->>'status',v_occ,(body->>'amount_minor')::bigint,body->>'currency',body->>'customer_ref',v_digest,v_key_id)
  on conflict(tenant_id,world,connector_id,source_event_id) do nothing returning received_sequence into v_inserted;
 if v_inserted is null then
  -- Same event id again (also a concurrent first arrival): same bytes = duplicate, different bytes = conflict (409).
  select payload_digest into v_existing from runtime.nexloop_commercial_events
   where tenant_id=cn.tenant_id and world=cn.world and connector_id=cn.connector_id and source_event_id=v_event;
  if v_existing=v_digest then
   perform runtime.nexloop_commercial_receipt(cn,v_event,v_digest,'duplicate',null);return jsonb_build_object('status',200,'outcome','duplicate');
  end if;
  perform runtime.nexloop_commercial_receipt(cn,v_event,v_digest,'conflict',null);
  perform runtime.nexloop_commercial_raise(cn.tenant_id,cn.world,'event:'||cn.connector_id||':'||v_event,'event_payload_conflict',
   jsonb_build_object('first_digest',v_existing,'conflicting_digest',v_digest));
  return jsonb_build_object('status',409,'outcome','conflict');
 end if;
 insert into runtime.nexloop_commercial_records(tenant_id,world,record_key,data_mode,connector_id,record_kind,external_id,currency,intent_id,object_id)
  values(cn.tenant_id,cn.world,v_key,cn.data_mode,cn.connector_id,v_kind,body->>'external_id',body->>'currency','commercial-'||v_key,
   runtime.nexloop_commercial_object_id(cn.tenant_id,cn.world,v_key)) on conflict do nothing;
 perform runtime.nexloop_commercial_receipt(cn,v_event,v_digest,'verified',null);
 perform runtime.nexloop_commercial_touch(cn.tenant_id,cn.world,v_key);
 if v_related is not null and exists(select 1 from runtime.nexloop_commercial_records where tenant_id=cn.tenant_id and world=cn.world and record_key=v_related) then
  perform runtime.nexloop_commercial_touch(cn.tenant_id,cn.world,v_related);end if;
 return jsonb_build_object('status',202,'outcome','accepted','event_ref','commercial-event:'||v_inserted);
end $$;
alter function authz.nexloop_commercial_ingest(text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_commercial_ingest(text,text,text,text) from public;
grant execute on function authz.nexloop_commercial_ingest(text,text,text,text) to nexloop_api;

-- 7. Deployment configuration (nexloop_configurator; keys from private files, never returned) -----------------------
create function control.nexloop_commercial_configure_connector(p_tenant text,p_world text,p_connector text,p_data_mode text,p_types text[],
  p_currencies text[],p_key_id text,p_key bytea,p_owner_confirmation text,p_rotation_seconds integer) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare s jsonb:=runtime.nexloop_commercial_settings_current();cur control.nexloop_commercial_connectors;v_revision integer;
begin
 if session_user<>'nexloop_configurator' then raise exception 'commercial configuration unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 if p_tenant is null or not exists(select 1 from control.nexloop_tenants where tenant_id=p_tenant and status='active')
  or p_world is null or p_world!~'^[a-z][a-z0-9_-]{0,63}$' or p_data_mode not in ('real','test') or (p_data_mode='real')<>(p_world='real')
  or (p_data_mode='real' and coalesce(char_length(btrim(p_owner_confirmation)),0) not between 1 and 500)
  or p_types is null or cardinality(p_types) not between 1 and 32 or exists(select 1 from unnest(p_types) x where not (s->'event_types' ? x))
  or p_currencies is null or cardinality(p_currencies) not between 1 and 32 or exists(select 1 from unnest(p_currencies) x where not (s->'currency_exponents' ? x))
  or p_rotation_seconds is null or p_rotation_seconds not between 0 and 604800 then
  raise exception 'commercial connector configuration invalid' using errcode='22023';end if;
 select * into cur from control.nexloop_commercial_connectors where connector_id=p_connector for update;
 if not found then
  insert into control.nexloop_commercial_connectors(connector_id,tenant_id,world,data_mode,owner_confirmation,accepted_types,currencies,key_id,key_material)
   values(p_connector,p_tenant,p_world,p_data_mode,nullif(btrim(p_owner_confirmation),''),p_types,p_currencies,p_key_id,p_key);
  v_revision:=1;
 else
  -- The binding of a connector (tenant, world, data mode) never changes; keys rotate with an overlap window.
  if cur.tenant_id<>p_tenant or cur.world<>p_world or cur.data_mode<>p_data_mode then
   raise exception 'commercial connector binding is immutable' using errcode='42501';end if;
  update control.nexloop_commercial_connectors set accepted_types=p_types,currencies=p_currencies,status='active',
   owner_confirmation=coalesce(nullif(btrim(p_owner_confirmation),''),owner_confirmation),
   key_id=p_key_id,key_material=p_key,
   previous_key_id=case when cur.key_id<>p_key_id and p_rotation_seconds>0 then cur.key_id end,
   previous_key_material=case when cur.key_id<>p_key_id and p_rotation_seconds>0 then cur.key_material end,
   previous_valid_until=case when cur.key_id<>p_key_id and p_rotation_seconds>0 then clock_timestamp()+make_interval(secs=>p_rotation_seconds) end,
   revision=revision+1,configured_at=clock_timestamp()
  where connector_id=p_connector returning revision into v_revision;
 end if;
 return jsonb_build_object('connector_id',p_connector,'world',p_world,'data_mode',p_data_mode,'revision',v_revision);
end $$;
alter function control.nexloop_commercial_configure_connector(text,text,text,text,text[],text[],text,bytea,text,integer) owner to nexloop_owner;
revoke all on function control.nexloop_commercial_configure_connector(text,text,text,text,text[],text[],text,bytea,text,integer) from public;
grant execute on function control.nexloop_commercial_configure_connector(text,text,text,text,text[],text[],text,bytea,text,integer) to nexloop_configurator;

create function control.nexloop_commercial_disable_connector(p_connector text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_revision integer;
begin
 if session_user<>'nexloop_configurator' then raise exception 'commercial configuration unavailable' using errcode='42501';end if;
 update control.nexloop_commercial_connectors set status='disabled',revision=revision+1,configured_at=clock_timestamp()
  where connector_id=p_connector returning revision into v_revision;
 if v_revision is null then raise exception 'commercial connector unavailable' using errcode='42501';end if;
 return jsonb_build_object('connector_id',p_connector,'status','disabled','revision',v_revision);
end $$;
alter function control.nexloop_commercial_disable_connector(text) owner to nexloop_owner;
revoke all on function control.nexloop_commercial_disable_connector(text) from public;
grant execute on function control.nexloop_commercial_disable_connector(text) to nexloop_configurator;

-- Records of one customer reference are marked when its link changes (their consumer_ref is re-derived).
create function runtime.nexloop_commercial_touch_customer(p_tenant text,p_world text,p_connector text,p_customer text) returns integer
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare k text;n integer:=0;prior text:=current_setting('eios.tenant_id',true);
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 for k in select distinct e.record_key from runtime.nexloop_commercial_events e
   where e.tenant_id=p_tenant and e.world=p_world and e.connector_id=p_connector and e.customer_ref=p_customer loop
  perform runtime.nexloop_commercial_touch(p_tenant,p_world,k);n:=n+1;
 end loop;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return n;
end $$;
alter function runtime.nexloop_commercial_touch_customer(text,text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_commercial_touch_customer(text,text,text,text) from public;

create function control.nexloop_commercial_link_customer(p_connector text,p_customer text,p_consumer text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare cn control.nexloop_commercial_connectors;v_current text;
begin
 if session_user<>'nexloop_configurator' then raise exception 'commercial configuration unavailable' using errcode='42501';end if;
 select * into cn from control.nexloop_commercial_connectors where connector_id=p_connector;
 if not found or coalesce(char_length(p_customer),0) not between 1 and 200 or p_customer~'^pseudonym:' or coalesce(p_consumer,'')!~'^[a-f0-9]{64}$' then
  raise exception 'commercial customer link invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',cn.tenant_id,true);
 if not exists(select 1 from ontology.objects o where o.tenant_id=cn.tenant_id and o.world=cn.world and o.type_name='Consumer' and o.object_id=p_consumer) then
  raise exception 'commercial customer link needs an existing Consumer of the connector''s world' using errcode='22023';end if;
 select consumer_id into v_current from control.nexloop_commercial_customer_links where connector_id=p_connector and customer_ref=p_customer;
 if v_current is not null and v_current<>p_consumer then raise exception 'customer reference is linked to another Consumer' using errcode='23505';end if;
 insert into control.nexloop_commercial_customer_links(connector_id,customer_ref,tenant_id,world,consumer_id)
  values(p_connector,p_customer,cn.tenant_id,cn.world,p_consumer) on conflict do nothing;
 return jsonb_build_object('linked',true,'records',runtime.nexloop_commercial_touch_customer(cn.tenant_id,cn.world,p_connector,p_customer));
end $$;
alter function control.nexloop_commercial_link_customer(text,text,text) owner to nexloop_owner;
revoke all on function control.nexloop_commercial_link_customer(text,text,text) from public;
grant execute on function control.nexloop_commercial_link_customer(text,text,text) to nexloop_configurator;

create function control.nexloop_commercial_unlink_customer(p_connector text,p_customer text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare cn control.nexloop_commercial_connectors;
begin
 if session_user<>'nexloop_configurator' then raise exception 'commercial configuration unavailable' using errcode='42501';end if;
 select * into cn from control.nexloop_commercial_connectors where connector_id=p_connector;
 if not found then raise exception 'commercial customer link invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',cn.tenant_id,true);
 delete from control.nexloop_commercial_customer_links where connector_id=p_connector and customer_ref=p_customer;
 return jsonb_build_object('linked',false,'records',runtime.nexloop_commercial_touch_customer(cn.tenant_id,cn.world,p_connector,p_customer));
end $$;
alter function control.nexloop_commercial_unlink_customer(text,text) owner to nexloop_owner;
revoke all on function control.nexloop_commercial_unlink_customer(text,text) from public;
grant execute on function control.nexloop_commercial_unlink_customer(text,text) to nexloop_configurator;

-- 8. D6: a deleted Consumer's customer references become one random pseudonym (not derived; no mapping kept). Called
-- by the Consumer deletion flow (NX-029); amounts, currency, times and the correction / refund chains stay.
create function runtime.nexloop_commercial_pseudonymize_consumer(p_tenant text,p_world text,p_consumer text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v_pseudonym text:='pseudonym:'||encode(extensions.gen_random_bytes(16),'hex');
 l record;n_events integer:=0;n integer;n_records integer:=0;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 for l in select * from control.nexloop_commercial_customer_links where tenant_id=p_tenant and world=p_world and consumer_id=p_consumer loop
  update runtime.nexloop_commercial_events set customer_ref=v_pseudonym
   where tenant_id=p_tenant and world=p_world and connector_id=l.connector_id and customer_ref=l.customer_ref;
  get diagnostics n=row_count;n_events:=n_events+n;
  delete from control.nexloop_commercial_customer_links where connector_id=l.connector_id and customer_ref=l.customer_ref;
  n_records:=n_records+runtime.nexloop_commercial_touch_customer(p_tenant,p_world,l.connector_id,v_pseudonym);
 end loop;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 -- The pseudonym itself is not returned: nothing outside the records links it back to the Consumer.
 return jsonb_build_object('events',n_events,'records',n_records);
end $$;
alter function runtime.nexloop_commercial_pseudonymize_consumer(text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_commercial_pseudonymize_consumer(text,text,text) from public;

-- 9. Downstream hook of a recorded record state (replaced by the NX-027 feeds migration: commitments, plans) -------
create function runtime.nexloop_commercial_on_recorded(p_tenant text,p_world text,p_key text,p_props jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
begin return '{}'::jsonb;end $$;
alter function runtime.nexloop_commercial_on_recorded(text,text,text,jsonb) owner to nexloop_owner;
revoke all on function runtime.nexloop_commercial_on_recorded(text,text,text,jsonb) from public;

-- 10. Record keeper port (nexloop_domain_worker; eios:action:nexloop.commercial.record:1) ---------------------------
create function authz.nexloop_commercial_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-commercial-record-v1',
  'eios:action:nexloop.commercial.record:1',array['nexloop_domain_worker']);c jsonb:=p_payload::jsonb;r runtime.nexloop_commercial_records;
 o ontology.objects;v jsonb;v_props jsonb;x jsonb;v_patch jsonb;n_applied integer;n_late integer;v_downstream jsonb;
begin
 perform set_config('eios.tenant_id',t,true);
 if c->>'verb' not in ('prepare','recorded') or coalesce(c->>'record_key','')!~'^[0-9a-f]{64}$' then
  raise exception 'commercial command invalid' using errcode='22023';end if;
 select * into r from runtime.nexloop_commercial_records where tenant_id=t and world=p_world and record_key=c->>'record_key' for update;
 if not found then raise exception 'commercial record unavailable' using errcode='42501';end if;
 v:=runtime.nexloop_commercial_target(t,p_world,r.record_key);v_props:=v->'properties';
 select * into o from ontology.objects where tenant_id=t and world=p_world and type_name='CommercialRecord' and object_id=r.object_id for share;
 if c->>'verb'='prepare' then
  for x in select * from jsonb_array_elements(v->'conflicts') loop
   perform runtime.nexloop_commercial_raise(t,p_world,'record:'||r.record_key,x->>'reason',x);
  end loop;
  if o.object_id is null then
   return jsonb_build_object('action','create','record_key',r.record_key,'object_id',r.object_id,'intent_id',r.intent_id,'properties',v_props,
    'conflicts',v->'conflicts');
  end if;
  select coalesce(jsonb_object_agg(e.key,e.value),'{}'::jsonb) into v_patch from jsonb_each(v_props) e where o.properties->e.key is distinct from e.value;
  if v_patch='{}'::jsonb then
   return jsonb_build_object('action','none','record_key',r.record_key,'object_id',r.object_id,'revision',o.nexloop_revision,'conflicts',v->'conflicts');
  end if;
  return jsonb_build_object('action','edit','record_key',r.record_key,'object_id',r.object_id,'revision',o.nexloop_revision,'patch',v_patch,
   'intent_id','commercial-'||r.record_key||'-r'||o.nexloop_revision,'conflicts',v->'conflicts');
 end if;
 -- recorded: the object carries exactly the derived state; events are dispositioned once (applied or late).
 if o.object_id is null or o.properties is distinct from v_props then raise exception 'commercial record not applied as derived' using errcode='40001';end if;
 with marked as (
  update runtime.nexloop_commercial_events e set recorded_at=clock_timestamp(),
   disposition=case when exists(select 1 from runtime.nexloop_commercial_events x where x.tenant_id=t and x.world=p_world and x.record_key=r.record_key
     and x.received_sequence<e.received_sequence
     and (x.occurred_at,coalesce(x.provider_sequence,-1),x.received_sequence)>(e.occurred_at,coalesce(e.provider_sequence,-1),e.received_sequence)) then 'late' else 'applied' end
  where e.tenant_id=t and e.world=p_world and e.record_key=r.record_key and e.disposition is null returning e.disposition)
 select count(*) filter (where disposition='applied'),count(*) filter (where disposition='late') into n_applied,n_late from marked;
 -- A refund changes its original's state: mark the original (when it exists).
 if r.record_kind='refund' then
  perform runtime.nexloop_commercial_touch(t,p_world,e.related_key) from (select distinct related_key from runtime.nexloop_commercial_events
   where tenant_id=t and world=p_world and record_key=r.record_key) e
   where exists(select 1 from runtime.nexloop_commercial_records x where x.tenant_id=t and x.world=p_world and x.record_key=e.related_key);
 end if;
 v_downstream:=runtime.nexloop_commercial_on_recorded(t,p_world,r.record_key,v_props);
 return jsonb_build_object('record_key',r.record_key,'object_id',r.object_id,'revision',o.nexloop_revision,'status',v_props->>'status',
  'applied',n_applied,'late',n_late,'downstream',v_downstream);
end $$;
alter function authz.nexloop_commercial_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_commercial_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_commercial_command(text,text,text,text,text) to nexloop_domain_worker;

-- 11. Read port (eios:action:nexloop.commercial.read:1): owner query port before NX-028 ------------------------------
create function authz.nexloop_commercial_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-commercial-read-v1',
  'eios:action:nexloop.commercial.read:1',array['nexloop_domain_worker','nexloop_api']);c jsonb:=p_payload::jsonb;v jsonb;r runtime.nexloop_commercial_records;
begin
 perform set_config('eios.tenant_id',t,true);
 if c->>'verb'='records' then
  if c ? 'consumer_id' and coalesce(c->>'consumer_id','')!~'^[a-f0-9]{64}$' then raise exception 'commercial read invalid' using errcode='22023';end if;
  select coalesce(jsonb_agg(jsonb_build_object('record_id',o.object_id,'revision',o.nexloop_revision,'properties',o.properties)
    order by o.properties->>'occurred_at',o.object_id),'[]'::jsonb) into v
   from ontology.objects o where o.tenant_id=t and o.world=p_world and o.type_name='CommercialRecord'
    and (not (c ? 'consumer_id') or o.properties->>'consumer_ref'=c->>'consumer_id');
  return jsonb_build_object('world',p_world,'records',v);
 elsif c->>'verb'='record' then
  select * into r from runtime.nexloop_commercial_records where tenant_id=t and world=p_world and object_id=c->>'record_id';
  if not found then raise exception 'commercial record unavailable' using errcode='42501';end if;
  select jsonb_build_object('record_id',r.object_id,'world',p_world,'data_mode',r.data_mode,
   'properties',(select o.properties from ontology.objects o where o.tenant_id=t and o.world=p_world and o.type_name='CommercialRecord' and o.object_id=r.object_id),
   'revision',(select o.nexloop_revision from ontology.objects o where o.tenant_id=t and o.world=p_world and o.type_name='CommercialRecord' and o.object_id=r.object_id),
   'events',(select coalesce(jsonb_agg(jsonb_build_object('event_ref','commercial-event:'||e.received_sequence,'event_type',e.event_type,'status',e.status,
      'amount_minor',e.amount_minor,'currency',e.currency,'occurred_at',runtime.nexloop_commercial_ts(e.occurred_at),
      'received_at',runtime.nexloop_commercial_ts(e.received_at),'provider_sequence',e.provider_sequence,'disposition',e.disposition)
     order by e.received_sequence),'[]'::jsonb) from runtime.nexloop_commercial_events e where e.tenant_id=t and e.world=p_world and e.record_key=r.record_key),
   'exceptions',(select coalesce(jsonb_agg(jsonb_build_object('reason',x.reason,'detail',x.detail,'raised_at',runtime.nexloop_commercial_ts(x.raised_at)) order by x.raised_at,x.reason),'[]'::jsonb)
     from runtime.nexloop_commercial_exceptions x where x.tenant_id=t and x.world=p_world and x.subject_ref='record:'||r.record_key)) into v;
  return v;
 elsif c->>'verb'='exceptions' then
  select coalesce(jsonb_agg(jsonb_build_object('subject_ref',x.subject_ref,'reason',x.reason,'detail',x.detail,'raised_at',runtime.nexloop_commercial_ts(x.raised_at))
   order by x.raised_at,x.subject_ref,x.reason),'[]'::jsonb) into v from runtime.nexloop_commercial_exceptions x where x.tenant_id=t and x.world=p_world;
  return jsonb_build_object('exceptions',v);
 elsif c->>'verb'='receipts' then
  select coalesce(jsonb_object_agg(k.outcome,k.n),'{}'::jsonb) into v from (select outcome,count(*) n from runtime.nexloop_commercial_receipts
   where tenant_id=t and world=p_world group by outcome) k;
  return jsonb_build_object('receipts',v);
 end if;
 raise exception 'commercial read invalid' using errcode='22023';
end $$;
alter function authz.nexloop_commercial_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_commercial_read(text,text,text,text,text) from public;
grant execute on function authz.nexloop_commercial_read(text,text,text,text,text) to nexloop_domain_worker,nexloop_api;

-- 12. Work feed: 'commercial-record' ----------------------------------------------------------------------------------
alter table runtime.nexloop_work_feed drop constraint nexloop_work_feed_feed_check;
alter table runtime.nexloop_work_feed add constraint nexloop_work_feed_feed_check
 check(feed in ('recall-instance','claim-match','plan-reevaluate','reply-due','commitment-register','commitment-monitor','commercial-record'));

-- Work feed port: 0111 body, 'commercial-record' added.
create or replace function authz.nexloop_work_feed(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;v_tenant text:=a->>'tenant_id';v_feed text:=c->>'feed';v_verb text:=c->>'verb';
 v_result jsonb;v_limit integer;v_lease integer;v_delay integer;v_max integer;r runtime.nexloop_work_feed%rowtype;ident jsonb;
begin
 if session_user<>'nexloop_domain_worker' or p_world is null or octet_length(p_text)>1048576 or octet_length(p_payload)>65536
  or v_feed is null or v_feed not in ('recall-instance','claim-match','plan-reevaluate','reply-due','commitment-register','commitment-monitor','commercial-record')
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
