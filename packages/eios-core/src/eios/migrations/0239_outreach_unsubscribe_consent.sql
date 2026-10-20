-- Proactive outreach, slice 7: unsubscribe and consent (docs/implementation/outreach-impl-plan.md; design §4.4/§4.5, owner decisions
-- P8 and O4). The migration number lives only in the file name and the catalog; nothing below repeats it. Contact refusal rules v1 are
-- unchanged: an inbound message still runs them first, and their 退订 / TD / unsubscribe entries still restrict all contact (the owner
-- keeps the stricter behaviour; a v2 is an open owner decision, plan §7).
-- 1. control.nexloop_unsubscribe_rules: versioned, append-only trusted configuration without a tenant dimension (as the refusal
--    rules); v1 = deploy/configuration/unsubscribe-rules.v1.json, the same content (tests). runtime.nexloop_unsubscribe_match(body):
--    normalized text (trim, NFKC, lower case, leading/trailing punctuation removed), exclusions first, then the entries.
-- 2. runtime.nexloop_marketing_optouts: append-only marketing opt-out events per consumer ('unsubscribed' | 'resumed', source
--    'unsubscribe_rule' | 'owner'), with evidence (message, rule version and id, matched text, the marketing send it answered).
-- 3. In the inbound message's own transaction (a row trigger beside the refusal check): when the latest admitted proactive message to
--    the consumer was marketing and within window_days, an unsubscribe hit records 'unsubscribed' (topic marketing). Otherwise
--    nothing here (the refusal rules decide, as today).
-- 4. runtime.nexloop_marketing_consents: append-only marketing consent events ('granted' | 'withdrawn'), each with its source
--    (the consumer's own inbound message, or an owner-recorded offline evidence reference) and time.
-- 5. Governed owner Action marketing_consent.record / record_marketing_consent: consent granted or withdrawn, or marketing resumed
--    after an unsubscribe, always with a reason and evidence. Resuming by the customer's own words is a separate word list, drafted in
--    the plan (appendix B) for the owner; nothing resumes automatically.
-- 6. Gate 4 (outreach_gates, latest body copied, changes marked "Outreach slice 7"): opt-out tenants refuse a consumer whose latest
--    opt-out event is 'unsubscribed' (marketing_opted_out); opt-in tenants, or a purpose with requires_consent, need the latest consent
--    event to be 'granted' (marketing_consent_missing).

-- 1. Rules -------------------------------------------------------------------------------------------------------------------------
create table control.nexloop_unsubscribe_rules (
 version integer primary key check(version>=1),definition jsonb not null check(jsonb_typeof(definition)='object'),
 definition_digest text not null check(definition_digest~'^[0-9a-f]{64}$'),published_by text not null,
 published_at timestamptz not null default clock_timestamp(),check((definition->>'version')::integer=version),
 check(definition->>'schema'='nexloop-unsubscribe-rules/1' and jsonb_typeof(definition->'unsubscribe')='array' and jsonb_typeof(definition->'exclusions')='array')
);
alter table control.nexloop_unsubscribe_rules owner to nexloop_owner;
create trigger unsubscribe_rules_append_only before update or delete on control.nexloop_unsubscribe_rules for each row execute function control.nexloop_nx022_append_only();
revoke all on control.nexloop_unsubscribe_rules from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;
insert into control.nexloop_unsubscribe_rules(version,definition,definition_digest,published_by)
 values(1,'{"schema":"nexloop-unsubscribe-rules/1","version":1,"decision":"Outreach design §4.4 and plan O4 (owner approved 2026-10-20): deterministic check of an inbound consumer message, only when the latest proactive message to that consumer was a marketing purpose within window_days; no model call. A hit writes a marketing topic refusal (unsubscribe record), never a general contact restriction; when the latest proactive message was not marketing, the general refusal rules decide (fallback: contact restriction). Text is normalized first (trim, NFKC full-width to half-width, lower case, leading/trailing punctuation removed); exclusions are checked first and a hit there means no unsubscribe (questions about unsubscribing, negation, refund and order wording go to the normal reply). Patterns are PostgreSQL ARE, case-insensitive; match is exact (the whole normalized message) or contains. A new version is a new file and a new published row; versions are only extended, never edited or reduced.","window_days":30,"topic":"marketing","unsubscribe":[{"id":"unsubscribe-word","match":"exact","text":"退订","pattern":"^退订$"},{"id":"sms-td","match":"exact","text":"TD","pattern":"^td$"},{"id":"unsub-marketing","match":"contains","text":"退订营销","pattern":"退订营销"},{"id":"cancel-subscription","match":"contains","text":"取消订阅","pattern":"取消订阅"},{"id":"stop-pushing","match":"contains","text":"不要再推了","pattern":"不要再推了"},{"id":"stop-pushing-2","match":"contains","text":"别再推了","pattern":"别再推了"},{"id":"no-push","match":"contains","text":"不要推送","pattern":"不要推送"},{"id":"dont-push-me","match":"contains","text":"别给我推","pattern":"别给我推"},{"id":"no-ads","match":"contains","text":"不要发广告","pattern":"不要发广告"},{"id":"no-ads-2","match":"contains","text":"别发广告","pattern":"别发广告"},{"id":"no-campaigns","match":"contains","text":"不要发活动","pattern":"不要发活动"},{"id":"no-campaigns-2","match":"contains","text":"别发活动","pattern":"别发活动"},{"id":"no-campaign-messages","match":"contains","text":"不想收到活动","pattern":"不想收到活动"},{"id":"no-promotion","match":"contains","text":"不想再收到推广","pattern":"不想再收到推广"},{"id":"no-offers","match":"contains","text":"不需要优惠信息","pattern":"不需要优惠信息"},{"id":"close-marketing","match":"contains","text":"关闭营销消息","pattern":"关闭营销消息"},{"id":"no-marketing","match":"contains","text":"不接收营销","pattern":"不接收营销"}],"exclusions":[{"id":"how-to-unsubscribe","match":"contains","text":"怎么退订","pattern":"怎么退订"},{"id":"how-to-unsubscribe-2","match":"contains","text":"如何退订","pattern":"如何退订"},{"id":"unsubscribed-yet","match":"contains","text":"退订了吗","pattern":"退订了吗"},{"id":"do-not-unsubscribe","match":"contains","text":"不要退订","pattern":"不要退订"},{"id":"refund","match":"contains","text":"退款","pattern":"退款"},{"id":"return-order","match":"contains","text":"退订单","pattern":"退订单"}]}'::jsonb,encode(sha256(convert_to(runtime.nexloop_canonical_json('{"schema":"nexloop-unsubscribe-rules/1","version":1,"decision":"Outreach design §4.4 and plan O4 (owner approved 2026-10-20): deterministic check of an inbound consumer message, only when the latest proactive message to that consumer was a marketing purpose within window_days; no model call. A hit writes a marketing topic refusal (unsubscribe record), never a general contact restriction; when the latest proactive message was not marketing, the general refusal rules decide (fallback: contact restriction). Text is normalized first (trim, NFKC full-width to half-width, lower case, leading/trailing punctuation removed); exclusions are checked first and a hit there means no unsubscribe (questions about unsubscribing, negation, refund and order wording go to the normal reply). Patterns are PostgreSQL ARE, case-insensitive; match is exact (the whole normalized message) or contains. A new version is a new file and a new published row; versions are only extended, never edited or reduced.","window_days":30,"topic":"marketing","unsubscribe":[{"id":"unsubscribe-word","match":"exact","text":"退订","pattern":"^退订$"},{"id":"sms-td","match":"exact","text":"TD","pattern":"^td$"},{"id":"unsub-marketing","match":"contains","text":"退订营销","pattern":"退订营销"},{"id":"cancel-subscription","match":"contains","text":"取消订阅","pattern":"取消订阅"},{"id":"stop-pushing","match":"contains","text":"不要再推了","pattern":"不要再推了"},{"id":"stop-pushing-2","match":"contains","text":"别再推了","pattern":"别再推了"},{"id":"no-push","match":"contains","text":"不要推送","pattern":"不要推送"},{"id":"dont-push-me","match":"contains","text":"别给我推","pattern":"别给我推"},{"id":"no-ads","match":"contains","text":"不要发广告","pattern":"不要发广告"},{"id":"no-ads-2","match":"contains","text":"别发广告","pattern":"别发广告"},{"id":"no-campaigns","match":"contains","text":"不要发活动","pattern":"不要发活动"},{"id":"no-campaigns-2","match":"contains","text":"别发活动","pattern":"别发活动"},{"id":"no-campaign-messages","match":"contains","text":"不想收到活动","pattern":"不想收到活动"},{"id":"no-promotion","match":"contains","text":"不想再收到推广","pattern":"不想再收到推广"},{"id":"no-offers","match":"contains","text":"不需要优惠信息","pattern":"不需要优惠信息"},{"id":"close-marketing","match":"contains","text":"关闭营销消息","pattern":"关闭营销消息"},{"id":"no-marketing","match":"contains","text":"不接收营销","pattern":"不接收营销"}],"exclusions":[{"id":"how-to-unsubscribe","match":"contains","text":"怎么退订","pattern":"怎么退订"},{"id":"how-to-unsubscribe-2","match":"contains","text":"如何退订","pattern":"如何退订"},{"id":"unsubscribed-yet","match":"contains","text":"退订了吗","pattern":"退订了吗"},{"id":"do-not-unsubscribe","match":"contains","text":"不要退订","pattern":"不要退订"},{"id":"refund","match":"contains","text":"退款","pattern":"退款"},{"id":"return-order","match":"contains","text":"退订单","pattern":"退订单"}]}'::jsonb),'UTF8')),'hex'),'migration:unsubscribe-rules.v1');


-- Deterministic match of one inbound body against the current unsubscribe rules; no model call. A missing rule set never matches
-- (the refusal rules remain the protective default).
create function runtime.nexloop_unsubscribe_match(p_body text) returns jsonb
 language plpgsql stable set search_path=pg_catalog,pg_temp as $$
declare r control.nexloop_unsubscribe_rules;x jsonb;t text;
begin
 select * into r from control.nexloop_unsubscribe_rules order by version desc limit 1;
 if not found then return jsonb_build_object('matched',false,'rule_version',0);end if;
 t:=lower(normalize(coalesce(p_body,''),NFKC));
 t:=regexp_replace(t,'^[[:space:][:punct:]。，！？、；：…～·“”‘’（）【】《》]+|[[:space:][:punct:]。，！？、；：…～·“”‘’（）【】《》]+$','','g');
 for x in select value from jsonb_array_elements(r.definition->'exclusions') loop
  if t ~* (x->>'pattern') then return jsonb_build_object('matched',false,'rule_version',r.version,'excluded_by',x->>'id');end if;
 end loop;
 for x in select value from jsonb_array_elements(r.definition->'unsubscribe') loop
  if t ~* (x->>'pattern') then
   return jsonb_build_object('matched',true,'rule_version',r.version,'rule_id',x->>'id','matched_text',left(x->>'text',200),
    'window_days',(r.definition->>'window_days')::integer,'topic',r.definition->>'topic');
  end if;
 end loop;
 return jsonb_build_object('matched',false,'rule_version',r.version);
end $$;
alter function runtime.nexloop_unsubscribe_match(text) owner to nexloop_owner;
revoke all on function runtime.nexloop_unsubscribe_match(text) from public;

-- 2/4. Opt-out and consent events (append-only) ----------------------------------------------------------------------------------
create table runtime.nexloop_marketing_optouts (
 event_id bigint generated always as identity primary key,tenant_id text not null,world text not null,
 consumer_id text not null check(consumer_id~'^[a-f0-9]{64}$'),kind text not null check(kind in ('unsubscribed','resumed')),
 source text not null check(source in ('unsubscribe_rule','owner')),topic text not null default 'marketing' check(topic~'^marketing(\.[a-z0-9_]+)*$'),
 message_id text,rule_version integer,rule_id text,matched_text text check(char_length(matched_text)<=200),outreach_intent text,
 recorded_by text not null,request_intent text,reason text check(char_length(reason)<=500),recorded_at timestamptz not null default clock_timestamp(),
 foreign key(tenant_id) references control.nexloop_tenants(tenant_id),
 check((source='unsubscribe_rule')=(kind='unsubscribed' and message_id is not null and rule_version is not null and rule_id is not null and outreach_intent is not null and reason is null)),
 check(source<>'owner' or (request_intent is not null and reason is not null))
);
create unique index nexloop_marketing_optouts_message on runtime.nexloop_marketing_optouts(tenant_id,world,message_id) where source='unsubscribe_rule';
create index nexloop_marketing_optouts_consumer on runtime.nexloop_marketing_optouts(tenant_id,world,consumer_id,event_id);
create table runtime.nexloop_marketing_consents (
 event_id bigint generated always as identity primary key,tenant_id text not null,world text not null,
 consumer_id text not null check(consumer_id~'^[a-f0-9]{64}$'),kind text not null check(kind in ('granted','withdrawn')),
 source text not null check(source in ('consumer_message','offline')),message_id text,evidence_ref text check(evidence_ref~'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),
 recorded_by text not null,request_intent text not null,reason text not null check(char_length(reason) between 1 and 500),
 recorded_at timestamptz not null default clock_timestamp(),foreign key(tenant_id) references control.nexloop_tenants(tenant_id),
 check((source='consumer_message')=(message_id is not null) and (source='offline')=(evidence_ref is not null))
);
create index nexloop_marketing_consents_consumer on runtime.nexloop_marketing_consents(tenant_id,world,consumer_id,event_id);
create function runtime.nexloop_marketing_events_append_only() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin raise exception 'marketing consent and opt-out events are append-only' using errcode='42501';end $$;
alter function runtime.nexloop_marketing_events_append_only() owner to nexloop_owner;
do $tables$
declare t text;
begin
 foreach t in array array['runtime.nexloop_marketing_optouts','runtime.nexloop_marketing_consents'] loop
  execute 'alter table '||t||' owner to nexloop_owner';execute 'alter table '||t||' enable row level security';
  execute 'alter table '||t||' force row level security';
  execute 'revoke all on '||t||' from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator';
  execute format('create policy tenant_boundary on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('create trigger marketing_events_append_only before update or delete on %s for each row execute function runtime.nexloop_marketing_events_append_only()',t);
 end loop;
end $tables$;

-- Current state: the latest event decides (no event: not opted out, no consent).
create function runtime.nexloop_marketing_state(p_tenant text,p_world text,p_consumer text) returns jsonb
 language sql stable set search_path=pg_catalog,pg_temp as $$
 select jsonb_build_object(
  'opted_out',coalesce((select kind='unsubscribed' from runtime.nexloop_marketing_optouts where tenant_id=p_tenant and world=p_world and consumer_id=p_consumer
    order by event_id desc limit 1),false),
  'consent',coalesce((select kind='granted' from runtime.nexloop_marketing_consents where tenant_id=p_tenant and world=p_world and consumer_id=p_consumer
    order by event_id desc limit 1),false))
$$;
alter function runtime.nexloop_marketing_state(text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_marketing_state(text,text,text) from public;

-- 3. Inbound unsubscribe, in the inbound Message's own transaction -----------------------------------------------------------------
create function runtime.nexloop_inbound_unsubscribe() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v_consumer text;hit jsonb;last runtime.nexloop_outreach_sends;
begin
 -- As the refusal check: Agent replies are not inbound consumer messages.
 if new.record ? 'direction' or new.idempotency_key like 'agent-%' then return null;end if;
 perform set_config('eios.tenant_id',new.tenant_id,true);
 select consumer_id into v_consumer from runtime.nexloop_conversations where tenant_id=new.tenant_id and world=new.world and conversation_id=new.conversation_id;
 -- Only after marketing: the latest proactive message that reached (or may have reached) the customer.
 select * into last from runtime.nexloop_outreach_sends where tenant_id=new.tenant_id and world=new.world and consumer_id=v_consumer
   and state in ('dispatching','unknown','fulfilled') order by admitted_at desc limit 1;
 if found and last.frequency_class='marketing' then
  hit:=runtime.nexloop_unsubscribe_match(new.record->>'body');
  if (hit->>'matched')::boolean and last.admitted_at>clock_timestamp()-make_interval(days=>(hit->>'window_days')::integer) then
   insert into runtime.nexloop_marketing_optouts(tenant_id,world,consumer_id,kind,source,topic,message_id,rule_version,rule_id,matched_text,outreach_intent,recorded_by)
    values(new.tenant_id,new.world,v_consumer,'unsubscribed','unsubscribe_rule',hit->>'topic',new.message_id,(hit->>'rule_version')::integer,hit->>'rule_id',
     hit->>'matched_text',last.intent_id,'system:unsubscribe-rules')
    on conflict do nothing;
  end if;
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return null;
end $$;
alter function runtime.nexloop_inbound_unsubscribe() owner to nexloop_owner;
revoke all on function runtime.nexloop_inbound_unsubscribe() from public;
create trigger outreach_inbound_unsubscribe after insert on runtime.nexloop_conversation_messages
 for each row execute function runtime.nexloop_inbound_unsubscribe();

-- 5. Governed owner Action -----------------------------------------------------------------------------------------------------------
create function control.nexloop_governed_record_marketing_consent(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_consumer text:=body->>'consumer_id';v_kind text:=body->>'kind';v_source text:=body->>'source';v_state jsonb;
begin
 if jsonb_typeof(body) is distinct from 'object'
  or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','consumer_id','kind','source','message_id','evidence_ref','reason'))
  or coalesce(v_consumer,'')!~'^[a-f0-9]{64}$' or v_kind is null or v_kind not in ('consent_granted','consent_withdrawn','marketing_resumed')
  or v_source is null or v_source not in ('consumer_message','offline') or length(coalesce(body->>'reason','')) not between 1 and 500
  or (v_source='consumer_message') is distinct from (body->>'message_id' is not null) or (v_source='offline') is distinct from (body->>'evidence_ref' is not null)
  or coalesce(body->>'evidence_ref','x')!~'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$' or coalesce(body->>'message_id','0'||repeat('0',63))!~'^[a-f0-9]{64}$' then
  raise exception 'marketing consent: invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 if not exists(select 1 from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='Consumer' and object_id=v_consumer) then
  raise exception 'marketing consent: consumer_unavailable' using errcode='22023';end if;
 -- The customer's own words: an inbound message of a conversation of this consumer.
 if v_source='consumer_message' and not exists(select 1 from runtime.nexloop_conversation_messages m join runtime.nexloop_conversations cv
   on cv.tenant_id=m.tenant_id and cv.world=m.world and cv.conversation_id=m.conversation_id
  where m.tenant_id=p_tenant and m.world=p_world and m.message_id=body->>'message_id' and cv.consumer_id=v_consumer and not (m.record ? 'direction')) then
  raise exception 'marketing consent: evidence_unavailable' using errcode='22023';end if;
 -- Idempotent per governed request.
 if exists(select 1 from runtime.nexloop_marketing_consents where tenant_id=p_tenant and world=p_world and request_intent=p_intent)
  or exists(select 1 from runtime.nexloop_marketing_optouts where tenant_id=p_tenant and world=p_world and request_intent=p_intent) then
  return jsonb_build_object('consumer_id',v_consumer,'replayed',true)||runtime.nexloop_marketing_state(p_tenant,p_world,v_consumer);end if;
 if v_kind='marketing_resumed' then
  if not (runtime.nexloop_marketing_state(p_tenant,p_world,v_consumer)->>'opted_out')::boolean then
   raise exception 'marketing consent: not_opted_out' using errcode='22023';end if;
  insert into runtime.nexloop_marketing_optouts(tenant_id,world,consumer_id,kind,source,message_id,recorded_by,request_intent,reason)
   values(p_tenant,p_world,v_consumer,'resumed','owner',body->>'message_id',p_principal,p_intent,body->>'reason');
 else
  insert into runtime.nexloop_marketing_consents(tenant_id,world,consumer_id,kind,source,message_id,evidence_ref,recorded_by,request_intent,reason)
   values(p_tenant,p_world,v_consumer,case v_kind when 'consent_granted' then 'granted' else 'withdrawn' end,v_source,body->>'message_id',body->>'evidence_ref',
    p_principal,p_intent,body->>'reason');
 end if;
 return jsonb_build_object('consumer_id',v_consumer,'replayed',false)||runtime.nexloop_marketing_state(p_tenant,p_world,v_consumer);
end $$;
alter function control.nexloop_governed_record_marketing_consent(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_governed_record_marketing_consent(text,text,text,text,text,jsonb) from public;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('marketing_consent.record','record_marketing_consent','human','control.nexloop_governed_record_marketing_consent(text,text,text,text,text,jsonb)','NX-022');

-- 6. Gate 4 reads the records (latest outreach_gates body copied; changes marked "Outreach slice 7") ------------------------------
create or replace function runtime.nexloop_outreach_gates(p_tenant text,p_world text,p_consumer text,p_purpose text,p_at timestamptz) returns jsonb
 language plpgsql stable set search_path=pg_catalog,pg_temp as $$
declare d jsonb:=runtime.nexloop_outreach_settings(p_tenant);g jsonb:='[]'::jsonb;v_local time:=(p_at at time zone (d->>'time_zone'))::time;
 v_from time:=split_part(d->>'quiet_hours','-',1)::time;v_to time:=split_part(d->>'quiet_hours','-',2)::time;v_n24 bigint;v_n7 bigint;v_code text;v_layer text;v_purpose jsonb;
 v_marketing boolean;v_m7 bigint;lim control.nexloop_budget_limits;v_used numeric;v_state jsonb;
begin
 g:=g||jsonb_build_object('gate','contact_restricted','result',case when exists(select 1 from control.nexloop_contact_restrictions r where r.tenant_id=p_tenant
   and r.world=p_world and r.consumer_id=p_consumer and r.active) then 'refuse' else 'pass' end);
 g:=g||jsonb_build_object('gate','control_paused','result',case when exists(select 1 from control.nexloop_control_scopes s where s.tenant_id=p_tenant and s.world=p_world and s.paused
   and ((s.scope_kind='tenant' and s.scope_ref='*') or (s.scope_kind='consumer' and s.scope_ref in (p_consumer,'consumer:'||p_consumer)))) then 'refuse' else 'pass' end);
 -- Outreach slice 3: the purpose must be in the current purpose list and enabled for the tenant; approval is slice 6.
 v_purpose:=runtime.nexloop_outreach_purpose(p_tenant,p_purpose);
 g:=g||jsonb_build_object('gate','purpose','result',case when not (v_purpose->>'listed')::boolean or not (v_purpose->>'enabled')::boolean then 'refuse'
   when (v_purpose->'definition'->>'requires_approval')::boolean then 'not_wired' else 'pass' end,
  'detail',case when not (v_purpose->>'listed')::boolean then 'purpose_not_allowed' when not (v_purpose->>'enabled')::boolean then 'purpose_disabled'
   when (v_purpose->'definition'->>'requires_approval')::boolean then 'approval: slice 6' else 'list v'||(v_purpose->>'list_version') end);
 -- Outreach slice 4, gate 4: marketing needs the tenant's marketing switch; opt-in (tenant or purpose) needs a consent record, and
 -- consent records arrive with slice 7, so it refuses until then. Opt-out refusals are gates 1 (restriction) and 8 (topic).
 v_marketing:=coalesce(v_purpose->'definition'->>'frequency_class','service')='marketing';
 -- Outreach slice 7: opt-out refuses after an unsubscribe; opt-in (tenant or purpose) needs the latest consent event to be granted.
 v_state:=runtime.nexloop_marketing_state(p_tenant,p_world,p_consumer);
 g:=g||jsonb_build_object('gate','marketing','result',case when not v_marketing then 'pass' when not (d->'marketing'->>'enabled')::boolean then 'refuse'
   when d->'marketing'->>'consent_mode'='opt_in' or coalesce((v_purpose->'definition'->>'requires_consent')::boolean,false) then
    case when (v_state->>'consent')::boolean then 'pass' else 'refuse' end
   when (v_state->>'opted_out')::boolean then 'refuse' else 'pass' end,
  'detail',case when not v_marketing then 'not_marketing' when not (d->'marketing'->>'enabled')::boolean then 'marketing_disabled'
   when d->'marketing'->>'consent_mode'='opt_in' or coalesce((v_purpose->'definition'->>'requires_consent')::boolean,false) then
    case when (v_state->>'consent')::boolean then 'consent_granted' else 'marketing_consent_missing' end
   when (v_state->>'opted_out')::boolean then 'marketing_opted_out' else 'opt_out' end);
 g:=g||jsonb_build_object('gate','persona_excluded','result',case when runtime.nexloop_consumer_persona(p_tenant,p_world,p_consumer)<>'real' then 'refuse' else 'pass' end,
   'detail',runtime.nexloop_consumer_persona(p_tenant,p_world,p_consumer));
 g:=g||jsonb_build_object('gate','quiet_hours','result',case when (v_from<=v_to and v_local>=v_from and v_local<v_to) or (v_from>v_to and (v_local>=v_from or v_local<v_to))
   then 'refuse' else 'pass' end,'detail',d->>'quiet_hours');
 v_n24:=runtime.nexloop_outreach_contacts_since(p_tenant,p_world,p_consumer,p_at-interval '24 hours');
 v_n7:=runtime.nexloop_outreach_contacts_since(p_tenant,p_world,p_consumer,p_at-interval '7 days');
 -- Outreach slice 4: the marketing allowance (P8) counts admitted marketing sends.
 select count(*) into v_m7 from runtime.nexloop_outreach_sends s where s.tenant_id=p_tenant and s.world=p_world and s.consumer_id=p_consumer
  and s.frequency_class='marketing' and s.admitted_at>p_at-interval '7 days' and s.state in ('dispatching','unknown','fulfilled');
 g:=g||jsonb_build_object('gate','frequency','result',case when v_n24>=(d->>'per_24h')::integer or v_n7>=(d->>'per_7d')::integer
   or (v_marketing and v_m7>=(d->'marketing'->>'per_7d')::integer) then 'refuse' else 'pass' end,
   'detail',jsonb_build_object('last_24h',v_n24,'last_7d',v_n7,'marketing_7d',v_m7));
 v_code:=runtime.nexloop_contact_preference_refusal(p_tenant,p_world,p_consumer,'native.webchat',coalesce(v_purpose->'definition'->>'topic',p_purpose),gen_random_uuid(),p_at);
 g:=g||jsonb_build_object('gate','consumer_preference','result',case when v_code is null then 'pass' else 'refuse' end,'detail',v_code);
 v_layer:=control.nexloop_real_dispatch_closed_layer(p_tenant,p_world,'eios:action:nexloop.outreach.send:1');
 g:=g||jsonb_build_object('gate','real_dispatch','result',case when v_layer is null then 'pass' else 'refuse' end,'detail',coalesce(v_layer,'open'));
 -- Outreach slice 4, gate 10: the tenant model budget must be configured, in its period and not used up.
 select * into lim from control.nexloop_budget_limits where tenant_id=p_tenant and world=p_world and budget_kind='model';
 if found then
  select coalesce(sum(amount),0) into v_used from control.nexloop_budget_consumption where tenant_id=p_tenant and world=p_world and budget_kind='model'
   and recorded_at>=lim.period_start and recorded_at<lim.period_end;
 end if;
 g:=g||jsonb_build_object('gate','budget','result',case when lim.tenant_id is null or p_at<lim.period_start or p_at>=lim.period_end or v_used>=lim.limit_amount
   then 'refuse' else 'pass' end,'detail',case when lim.tenant_id is null then 'budget_unconfigured'
   when p_at<lim.period_start or p_at>=lim.period_end or v_used>=lim.limit_amount then 'budget_exhausted' else 'model' end);
 return g;
end $$;
