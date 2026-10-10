-- NX-025 / ADR-023 (temporary number 0109): explicit refusal of contact is a hard stop at dispatch;
-- every accepted inbound consumer message gets a reply (bound reply allowed under a restriction,
-- pending-reply registry, settlement, escalation).
--
-- 1. Versioned, append-only rule and policy rows (global; v1 = deploy/configuration/contact-refusal-rules.v1.json
--    and reply-guarantee.v1.json, byte-equal canonical JSON, checked by tests). No model call anywhere here.
-- 2. In the same transaction that persists an inbound consumer Message (0046 commit_message inserts the
--    conversation row), a row trigger runs the deterministic rules (exclusions removed first, then refuse,
--    then uncertain). A hit, an uncertain hit, a missing rule set or a matching error all restrict: through the
--    NX-022 control writer (control.nexloop_nx022_bump) a consumer-scope 'contact_restricted' control event
--    advances the control revision, and the restriction row records message, rule version, rule id and the
--    matched text. The message itself is always persisted (a refusal never loses the inbound message).
-- 3. Dispatch: the existing intent check (0097) still runs nexloop_assert_dispatch_controls first (so intents
--    queued before the restriction are refused as stale, NXC02); then, while the consumer is restricted, only an
--    Agent reply bound to an inbound message passes: the intent's NX-047 outbound record (trigger_message_id is
--    derived server-side from the message Run, never chosen by the model), same conversation and consumer, inside
--    the configured reply window, and no other reply to that message dispatched or accepted. Anything else: NXC05.
-- 4. Release is only the governed human owner Action (capability goals.contact.release) through the NX-022
--    governed entry; Agents, Runs, services, extraction and model output have no path. A later constraint Claim
--    only adds evidence.
-- 5. Query port (nexloop.contact.read): active restrictions with reason and evidence, reply escalations.
-- 6. Pending reply: every inbound consumer message registers a 'reply-due' work-feed item due at accepted_at +
--    fallback start_after_seconds (within the reply window); an Agent reply bound to it that the channel accepted settles it (deleted in that transaction).
--    The reply worker escalates what is still unsettled (the fallback reply Run is attached by the same worker).

-- 1. Versioned rules and reply policy -------------------------------------------------------------------------
create table control.nexloop_contact_refusal_rules (
 version integer primary key check(version>=1),definition jsonb not null check(jsonb_typeof(definition)='object'),
 definition_digest text not null check(definition_digest~'^[0-9a-f]{64}$'),published_by text not null,
 published_at timestamptz not null default clock_timestamp(),check((definition->>'version')::integer=version)
);
create table control.nexloop_reply_policies (
 version integer primary key check(version>=1),definition jsonb not null check(jsonb_typeof(definition)='object'),
 definition_digest text not null check(definition_digest~'^[0-9a-f]{64}$'),published_by text not null,
 published_at timestamptz not null default clock_timestamp(),check((definition->>'version')::integer=version),
 check(jsonb_typeof(definition->'reply_window_seconds')='number' and (definition->>'reply_window_seconds')::integer between 60 and 604800),
 -- The fallback must start, and its Run must end, inside the reply window (ADR-023 §2.6/§2.7).
 check(jsonb_typeof(definition->'fallback'->'start_after_seconds')='number'
  and (definition->'fallback'->>'start_after_seconds')::integer>=1
  and (definition->'fallback'->>'start_after_seconds')::integer+greatest((definition->'fallback'->>'run_ttl_seconds')::integer,
   (definition->'fallback'->'run_budget'->>'active_timeout_seconds')::integer)<(definition->>'reply_window_seconds')::integer)
);
alter table control.nexloop_contact_refusal_rules owner to nexloop_owner;
alter table control.nexloop_reply_policies owner to nexloop_owner;
create trigger nx025_append_only before update or delete on control.nexloop_contact_refusal_rules for each row execute function control.nexloop_nx022_append_only();
create trigger nx025_append_only before update or delete on control.nexloop_reply_policies for each row execute function control.nexloop_nx022_append_only();
revoke all on control.nexloop_contact_refusal_rules,control.nexloop_reply_policies
 from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator;
insert into control.nexloop_contact_refusal_rules(version,definition,definition_digest,published_by)
 values(1,'{"decision":"ADR-023 §2.1/§3: deterministic protective check on each inbound consumer message, in the same transaction as its persistence; no model call. A refuse or uncertain rule hit restricts contact (uncertain is restricted too: when in doubt, restrict). Exclusions first remove clearly non-contact phrases (wrong goods, shipping, discounts, change) from the text; the rules are matched on what remains. Patterns are PostgreSQL ARE, case-insensitive. A new version is a new file and a new published row; versions are never edited.","exclusions":[{"id":"goods-and-shipping","pattern":"(发|寄)\\s*(错|漏|顺丰|快递|货|过来|到)"},{"id":"discount-and-payment","pattern":"打\\s*(折|包|开|印|款|钱|车)"},{"id":"change","pattern":"找\\s*(零|钱|补)"}],"refuse":[{"id":"stop-contact","pattern":"(不要|别|不用|勿|请勿|不必|甭)\\s*(再|在|继续)?\\s*(给我|跟我|向我|和我|对我|找我)?\\s*(发|打|推送|推|联系|打扰|骚扰|找|call|text)"},{"id":"unsubscribe","pattern":"(退订|取消订阅|unsubscribe|拉黑|屏蔽你们|删除我的(号码|手机号|联系方式))"},{"id":"threat-if-contacted","pattern":"(再|继续)\\s*(发|打|联系|骚扰|推送).{0,8}(投诉|报警|举报|起诉)"},{"id":"sms-stop-keyword","pattern":"^\\s*(stop|td)\\s*[。.!！]?\\s*$"}],"schema":"nexloop-contact-refusal-rules/1","uncertain":[{"id":"less-contact","pattern":"(少|别老|别总|不要老|不要总|别一直|不要一直)\\s*(给我)?\\s*(发|打|联系|推)"},{"id":"annoyed","pattern":"(烦死了|很烦|太烦了|别烦我|不胜其烦)"}],"version":1}'::jsonb,'03efe14ff76b74c85c49dfd509c92176bc3bf8a4683761c08620079145e2bd03','deploy/configuration/contact-refusal-rules.v1.json');
insert into control.nexloop_reply_policies(version,definition,definition_digest,published_by)
 values(1,'{"decision":"ADR-023 §2.6/§2.7 (dispatcher defaults 2026-10-10): every accepted inbound consumer message gets a reply. A reply is an Agent outbound bound (server-derived trigger_message_id) to that message, same conversation and consumer. Under a contact restriction only such a bound reply may dispatch, at most one per inbound message, within reply_window_seconds of its acceptance. A message still unsettled start_after_seconds after its acceptance starts at most one fallback reply Run (LLM-generated, single bound-reply tool, fixed small budget); if that fails too, the owner is escalated with evidence. Loading refuses a policy where start_after_seconds + the fallback Run''s longest duration is not below reply_window_seconds. Initial values; owner-adjustable by a new version.","fallback":{"batch":10,"context_strategy":"recent_plus_required","lease_seconds":120,"max_attempts":1,"run_budget":{"active_timeout_seconds":60,"currency":"USD","maximum_cost":"0.20","maximum_model_turns":2,"maximum_tool_calls":2},"run_ttl_seconds":120,"runtime_profile":"deepseek-flash","start_after_seconds":300},"reply_window_seconds":1800,"schema":"nexloop-reply-guarantee/1","version":1}'::jsonb,'8db7dfd7c3270d406a56c3ca69e59f949dcaccf1b8526661aca2d0801bb67c6c','deploy/configuration/reply-guarantee.v1.json');

create function control.nexloop_reply_window_seconds() returns integer
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select (definition->>'reply_window_seconds')::integer from control.nexloop_reply_policies order by version desc limit 1
$$;
create function control.nexloop_reply_fallback_after_seconds() returns integer
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select (definition->'fallback'->>'start_after_seconds')::integer from control.nexloop_reply_policies order by version desc limit 1
$$;
alter function control.nexloop_reply_window_seconds() owner to nexloop_owner;
alter function control.nexloop_reply_fallback_after_seconds() owner to nexloop_owner;
revoke all on function control.nexloop_reply_window_seconds(),control.nexloop_reply_fallback_after_seconds() from public;

-- Deterministic match. Exclusions remove clearly non-contact phrases first; refuse, then uncertain rules.
-- No rule set, or an error, is uncertain: restricted (ADR-023 §3, when in doubt restrict).
create function control.nexloop_contact_refusal_match(p_body text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp as $$
declare r control.nexloop_contact_refusal_rules;x jsonb;t text:=coalesce(p_body,'');m text[];kind text;
begin
 select * into r from control.nexloop_contact_refusal_rules order by version desc limit 1;
 if not found then return jsonb_build_object('matched',true,'certainty','uncertain','rule_version',0,'rule_id','rules-unavailable','matched_text','');end if;
 begin
  for x in select value from jsonb_array_elements(r.definition->'exclusions') loop t:=regexp_replace(t,x->>'pattern','','gi');end loop;
  foreach kind in array array['refuse','uncertain'] loop
   for x in select value from jsonb_array_elements(r.definition->kind) loop
    m:=regexp_match(t,'('||(x->>'pattern')||')','i');
    if m is not null then
     return jsonb_build_object('matched',true,'certainty',kind,'rule_version',r.version,'rule_id',x->>'id','matched_text',left(m[1],200));
    end if;
   end loop;
  end loop;
 exception when others then
  return jsonb_build_object('matched',true,'certainty','uncertain','rule_version',r.version,'rule_id','match-error','matched_text','');
 end;
 return jsonb_build_object('matched',false,'rule_version',r.version);
end $$;
alter function control.nexloop_contact_refusal_match(text) owner to nexloop_owner;
revoke all on function control.nexloop_contact_refusal_match(text) from public;

-- 2. Restriction state, evidence, escalations -----------------------------------------------------------------
alter table control.nexloop_control_events drop constraint nexloop_control_events_event_kind_check;
alter table control.nexloop_control_events add constraint nexloop_control_events_event_kind_check
 check(event_kind in ('pause','resume','budget','goal_version','agent_goal','metric','contact_restricted','contact_released'));
create table control.nexloop_contact_restrictions (
 tenant_id text not null,world text not null,consumer_id text not null check(consumer_id~'^[a-f0-9]{64}$'),
 active boolean not null,control_revision bigint not null check(control_revision>=1),
 message_id text not null,conversation_id text not null,rule_version integer not null,rule_id text not null,
 certainty text not null check(certainty in ('refuse','uncertain')),matched_text text not null,restricted_at timestamptz not null,
 released_at timestamptz,released_by text,release_intent text,release_reason text,
 primary key(tenant_id,world,consumer_id),
 check(active=(released_at is null) and (released_at is null)=(released_by is null))
);
create table control.nexloop_contact_refusal_hits (
 tenant_id text not null,world text not null,message_id text not null,consumer_id text not null,conversation_id text not null,
 rule_version integer not null,rule_id text not null,certainty text not null,matched_text text not null,
 control_revision bigint not null,recorded_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,message_id)
);
create table control.nexloop_reply_escalations (
 tenant_id text not null,world text not null,message_id text not null,conversation_id text not null,consumer_id text not null,
 reason text not null check(reason~'^[a-z_]{1,64}$'),detail jsonb not null check(jsonb_typeof(detail)='object'),
 escalated_at timestamptz not null default clock_timestamp(),primary key(tenant_id,world,message_id)
);
alter table control.nexloop_contact_restrictions owner to nexloop_owner;
alter table control.nexloop_contact_refusal_hits owner to nexloop_owner;
alter table control.nexloop_reply_escalations owner to nexloop_owner;
create trigger nx025_append_only before update or delete on control.nexloop_contact_refusal_hits for each row execute function control.nexloop_nx022_append_only();
create trigger nx025_append_only before update or delete on control.nexloop_reply_escalations for each row execute function control.nexloop_nx022_append_only();
create trigger nx025_no_delete before delete on control.nexloop_contact_restrictions for each row execute function control.nexloop_nx022_append_only();
do $rls$
declare t text;
begin
 foreach t in array array['nexloop_contact_restrictions','nexloop_contact_refusal_hits','nexloop_reply_escalations'] loop
  execute format('alter table control.%I enable row level security',t);
  execute format('alter table control.%I force row level security',t);
  execute format('create policy tenant_boundary on control.%I to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on control.%I from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator',t);
 end loop;
end $rls$;

-- 3. Inbound protective check + pending reply, in the inbound Message's own transaction ---------------------------
create function runtime.nexloop_inbound_message_protect() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v_consumer text;hit jsonb;rev bigint;cur control.nexloop_contact_restrictions;v_at timestamptz;
begin
 -- Agent replies (0079 materialization) are not inbound consumer messages.
 if new.record ? 'direction' or new.idempotency_key like 'agent-%' then return null;end if;
 perform set_config('eios.tenant_id',new.tenant_id,true);
 select consumer_id into v_consumer from runtime.nexloop_conversations where tenant_id=new.tenant_id and world=new.world and conversation_id=new.conversation_id;
 hit:=control.nexloop_contact_refusal_match(new.record->>'body');
 if (hit->>'matched')::boolean then
  select * into cur from control.nexloop_contact_restrictions where tenant_id=new.tenant_id and world=new.world and consumer_id=v_consumer for update;
  if found and cur.active then
   rev:=cur.control_revision;
  else
   rev:=control.nexloop_nx022_bump(new.tenant_id,new.world,'contact_restricted','consumer',v_consumer,
    jsonb_build_object('message_id',new.message_id,'rule_version',(hit->>'rule_version')::integer,'rule_id',hit->>'rule_id','certainty',hit->>'certainty'),
    'system:contact-refusal-rules','contact-refusal:'||new.message_id);
   insert into control.nexloop_contact_restrictions(tenant_id,world,consumer_id,active,control_revision,message_id,conversation_id,rule_version,rule_id,certainty,matched_text,restricted_at)
    values(new.tenant_id,new.world,v_consumer,true,rev,new.message_id,new.conversation_id,(hit->>'rule_version')::integer,hit->>'rule_id',hit->>'certainty',hit->>'matched_text',clock_timestamp())
    on conflict(tenant_id,world,consumer_id) do update set active=true,control_revision=excluded.control_revision,message_id=excluded.message_id,
     conversation_id=excluded.conversation_id,rule_version=excluded.rule_version,rule_id=excluded.rule_id,certainty=excluded.certainty,
     matched_text=excluded.matched_text,restricted_at=excluded.restricted_at,released_at=null,released_by=null,release_intent=null,release_reason=null;
  end if;
  insert into control.nexloop_contact_refusal_hits(tenant_id,world,message_id,consumer_id,conversation_id,rule_version,rule_id,certainty,matched_text,control_revision)
   values(new.tenant_id,new.world,new.message_id,v_consumer,new.conversation_id,(hit->>'rule_version')::integer,hit->>'rule_id',hit->>'certainty',hit->>'matched_text',rev)
   on conflict do nothing;
 end if;
 -- ADR-023 §2.7: every accepted inbound message is a pending reply, due when the fallback may start.
 v_at:=coalesce((new.record->>'accepted_at')::timestamptz,clock_timestamp());
 insert into runtime.nexloop_work_feed(tenant_id,world,feed,item_key,payload,available_at)
  values(new.tenant_id,new.world,'reply-due','reply:'||new.message_id,
   jsonb_build_object('message_id',new.message_id,'conversation_id',new.conversation_id,'consumer_id',v_consumer),
   v_at+make_interval(secs=>control.nexloop_reply_fallback_after_seconds()))
  on conflict(tenant_id,world,feed,item_key) do nothing;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return null;
end $$;
alter function runtime.nexloop_inbound_message_protect() owner to nexloop_owner;
revoke all on function runtime.nexloop_inbound_message_protect() from public;
create trigger nx025_inbound_message_protect after insert on runtime.nexloop_conversation_messages
 for each row execute function runtime.nexloop_inbound_message_protect();

alter table runtime.nexloop_work_feed drop constraint nexloop_work_feed_feed_check;
alter table runtime.nexloop_work_feed add constraint nexloop_work_feed_feed_check check(feed in ('recall-instance','claim-match','plan-reevaluate','reply-due'));

-- Settlement: an Agent reply bound to the message that the channel accepted ends the pending reply.
create function runtime.nexloop_reply_settle() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);
begin
 if new.delivery_state not in ('provider_accepted','delivered') or old.delivery_state in ('provider_accepted','delivered') then return null;end if;
 perform set_config('eios.tenant_id',new.tenant_id,true);
 delete from runtime.nexloop_work_feed where tenant_id=new.tenant_id and world=new.world and feed='reply-due' and item_key='reply:'||new.trigger_message_id;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return null;
end $$;
alter function runtime.nexloop_reply_settle() owner to nexloop_owner;
revoke all on function runtime.nexloop_reply_settle() from public;
create trigger nx025_reply_settle after update of delivery_state on runtime.nexloop_outbound_messages
 for each row execute function runtime.nexloop_reply_settle();

-- 4. Dispatch: bound replies only, while restricted -------------------------------------------------------------
create function control.nexloop_contact_assert_intent(p_tenant text,p_world text,p_intent uuid) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);i runtime.nexloop_effect_intents;o runtime.nexloop_outbound_messages;
 m runtime.nexloop_conversation_messages;v_consumer text;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into i from runtime.nexloop_effect_intents where intent_id=p_intent and tenant_id=p_tenant and world=p_world;
 if not found then raise exception 'intent unavailable' using errcode='42501';end if;
 if exists(select 1 from control.nexloop_contact_restrictions r where r.tenant_id=p_tenant and r.world=p_world and r.consumer_id=i.consumer_id and r.active) then
  select * into o from runtime.nexloop_outbound_messages where tenant_id=p_tenant and world=p_world and intent_id=p_intent;
  if not found then raise exception 'contact restricted: outreach is not a reply to an inbound message' using errcode='NXC05';end if;
  select cm.* into m from runtime.nexloop_conversation_messages cm join runtime.nexloop_conversations cv
   on cv.tenant_id=cm.tenant_id and cv.world=cm.world and cv.conversation_id=cm.conversation_id
   where cm.tenant_id=p_tenant and cm.world=p_world and cm.conversation_id=o.conversation_id and cm.message_id=o.trigger_message_id
    and cv.consumer_id=i.consumer_id and not (cm.record ? 'direction') and cm.idempotency_key not like 'agent-%';
  if not found then raise exception 'contact restricted: reply not bound to an inbound message of this consumer' using errcode='NXC05';end if;
  if (m.record->>'accepted_at')::timestamptz+make_interval(secs=>control.nexloop_reply_window_seconds())<=clock_timestamp() then
   raise exception 'contact restricted: reply window closed' using errcode='NXC05';end if;
  -- One reply per inbound message: serialize the replies to this message, refuse a second one in flight or accepted.
  perform pg_advisory_xact_lock(hashtextextended(p_tenant||':'||p_world||':reply:'||m.message_id,0));
  if exists(select 1 from runtime.nexloop_outbound_messages x where x.tenant_id=p_tenant and x.world=p_world and x.trigger_message_id=m.message_id
    and x.intent_id<>p_intent and x.delivery_state in ('dispatching','provider_accepted','delivered','unknown')) then
   raise exception 'contact restricted: inbound message already answered' using errcode='NXC05';end if;
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
end $$;
alter function control.nexloop_contact_assert_intent(text,text,uuid) owner to nexloop_owner;
revoke all on function control.nexloop_contact_assert_intent(text,text,uuid) from public;

create or replace function authz.nexloop_assert_intent_dispatch_controls(p_digest text,p_world text,p_intent uuid) returns jsonb
language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;v_snapshot jsonb;v_result jsonb;
begin
 ident:=control.nexloop_nx022_identity(p_digest,p_world,true);
 select c.snapshot into v_snapshot from runtime.nexloop_effect_dispatch_controls c
  where c.intent_id=p_intent and c.tenant_id=ident->'binding'->>'tenant_id' and c.world=p_world order by c.captured_at desc,c.run_id limit 1;
 if not found then raise exception 'control snapshot missing' using errcode='NXC02';end if;
 v_result:=authz.nexloop_assert_dispatch_controls(p_digest,p_world,v_snapshot);
 -- ADR-023: a contact restriction lets only a reply bound to an inbound message through (NXC05 otherwise).
 perform control.nexloop_contact_assert_intent(ident->'binding'->>'tenant_id',p_world,p_intent);
 return v_result;
end $$;

-- 5. Release: governed human owner Action only ------------------------------------------------------------------
create function control.nexloop_contact_release(p_tenant text,p_world text,p_principal text,p_intent text,body jsonb)
 returns jsonb language plpgsql set search_path=pg_catalog,pg_temp as $$
declare cur control.nexloop_contact_restrictions;rev bigint;
begin
 if body->>'operation' is distinct from 'release_contact_restriction' or coalesce(body->>'consumer_id','')!~'^[a-f0-9]{64}$'
  or length(coalesce(body->>'reason','')) not between 1 and 500 then raise exception 'contact release invalid' using errcode='22023';end if;
 select * into cur from control.nexloop_contact_restrictions where tenant_id=p_tenant and world=p_world and consumer_id=body->>'consumer_id' for update;
 if not found or not cur.active then raise exception 'no active contact restriction' using errcode='22023';end if;
 rev:=control.nexloop_nx022_bump(p_tenant,p_world,'contact_released','consumer',body->>'consumer_id',
  jsonb_build_object('reason',body->>'reason','restricted_by_message',cur.message_id),p_principal,p_intent);
 update control.nexloop_contact_restrictions set active=false,control_revision=rev,released_at=clock_timestamp(),released_by=p_principal,
  release_intent=p_intent,release_reason=body->>'reason' where tenant_id=p_tenant and world=p_world and consumer_id=body->>'consumer_id';
 return jsonb_build_object('consumer_id',body->>'consumer_id','released',true,'control_revision',rev);
end $$;
alter function control.nexloop_contact_release(text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_contact_release(text,text,text,text,jsonb) from public;

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
     when 'goals.contact.release' then 'release_contact_restriction' end)
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

-- 6. Query port (before NX-028): restrictions with their evidence, and reply escalations ------------------------
create function authz.nexloop_contact_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-contact-read-v1','eios:action:nexloop.contact.read:1',
  array['nexloop_api','nexloop_domain_worker']);c jsonb:=p_payload::jsonb;
begin
 perform set_config('eios.tenant_id',t,true);
 if c->>'verb'='restrictions' then
  if c ? 'consumer_id' and coalesce(c->>'consumer_id','')!~'^[a-f0-9]{64}$' then raise exception 'contact read invalid' using errcode='22023';end if;
  return jsonb_build_object('restrictions',coalesce((select jsonb_agg(jsonb_build_object('consumer_id',r.consumer_id,'active',r.active,'control_revision',r.control_revision,
    'message_id',r.message_id,'conversation_id',r.conversation_id,'rule_version',r.rule_version,'rule_id',r.rule_id,'certainty',r.certainty,
    'matched_text',r.matched_text,'restricted_at',r.restricted_at,'released_at',r.released_at,'released_by',r.released_by,'release_reason',r.release_reason,
    'hits',(select coalesce(jsonb_agg(jsonb_build_object('message_id',h.message_id,'rule_version',h.rule_version,'rule_id',h.rule_id,'certainty',h.certainty,
      'matched_text',h.matched_text,'recorded_at',h.recorded_at) order by h.recorded_at),'[]'::jsonb) from control.nexloop_contact_refusal_hits h
      where h.tenant_id=r.tenant_id and h.world=r.world and h.consumer_id=r.consumer_id)) order by r.restricted_at)
   from control.nexloop_contact_restrictions r where r.tenant_id=t and r.world=p_world
    and ((c ? 'consumer_id' and r.consumer_id=c->>'consumer_id') or (not c ? 'consumer_id' and r.active))),'[]'::jsonb));
 elsif c->>'verb'='escalations' then
  return jsonb_build_object('escalations',coalesce((select jsonb_agg(jsonb_build_object('message_id',e.message_id,'conversation_id',e.conversation_id,
    'consumer_id',e.consumer_id,'reason',e.reason,'detail',e.detail,'escalated_at',e.escalated_at) order by e.escalated_at)
   from control.nexloop_reply_escalations e where e.tenant_id=t and e.world=p_world),'[]'::jsonb));
 end if;
 raise exception 'contact read invalid' using errcode='22023';
end $$;
alter function authz.nexloop_contact_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_contact_read(text,text,text,text,text) from public;
grant execute on function authz.nexloop_contact_read(text,text,text,text,text) to nexloop_api,nexloop_domain_worker;

-- 7. Reply guarantee port (reply worker): state of one pending reply, and escalation with evidence --------------
create function authz.nexloop_reply_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-reply-guarantee-v1','eios:action:nexloop.reply.guarantee:1',
  array['nexloop_domain_worker']);c jsonb:=p_payload::jsonb;m runtime.nexloop_conversation_messages;v_consumer text;
begin
 perform set_config('eios.tenant_id',t,true);
 select cm.* into m from runtime.nexloop_conversation_messages cm where cm.tenant_id=t and cm.world=p_world and cm.message_id=c->>'message_id'
  and not (cm.record ? 'direction') and cm.idempotency_key not like 'agent-%';
 if not found then raise exception 'reply message unavailable' using errcode='42501';end if;
 select consumer_id into v_consumer from runtime.nexloop_conversations where tenant_id=t and world=p_world and conversation_id=m.conversation_id;
 if c->>'verb'='state' then
  return jsonb_build_object('message_id',m.message_id,'conversation_id',m.conversation_id,'consumer_id',v_consumer,
   'accepted_at',m.record->>'accepted_at',
   'settled',exists(select 1 from runtime.nexloop_outbound_messages o where o.tenant_id=t and o.world=p_world and o.trigger_message_id=m.message_id
     and o.delivery_state in ('provider_accepted','delivered')),
   'in_flight',exists(select 1 from runtime.nexloop_outbound_messages o where o.tenant_id=t and o.world=p_world and o.trigger_message_id=m.message_id
     and o.delivery_state in ('persisted','dispatching','unknown')),
   'restricted',exists(select 1 from control.nexloop_contact_restrictions r where r.tenant_id=t and r.world=p_world and r.consumer_id=v_consumer and r.active),
   'escalated',exists(select 1 from control.nexloop_reply_escalations e where e.tenant_id=t and e.world=p_world and e.message_id=m.message_id));
 elsif c->>'verb'='escalate' then
  if coalesce(c->>'reason','')!~'^[a-z_]{1,64}$' or jsonb_typeof(c->'detail') is distinct from 'object' then raise exception 'reply escalation invalid' using errcode='22023';end if;
  -- Evidence only for a message still unanswered: a settled message is never escalated.
  if exists(select 1 from runtime.nexloop_outbound_messages o where o.tenant_id=t and o.world=p_world and o.trigger_message_id=m.message_id
    and o.delivery_state in ('provider_accepted','delivered')) then raise exception 'reply already settled' using errcode='40001';end if;
  insert into control.nexloop_reply_escalations(tenant_id,world,message_id,conversation_id,consumer_id,reason,detail)
   values(t,p_world,m.message_id,m.conversation_id,v_consumer,c->>'reason',c->'detail') on conflict do nothing;
  return jsonb_build_object('message_id',m.message_id,'escalated',true);
 end if;
 raise exception 'reply command invalid' using errcode='22023';
end $$;
alter function authz.nexloop_reply_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_reply_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_reply_command(text,text,text,text,text) to nexloop_domain_worker;

-- Work feed port: 0106 body, 'reply-due' added.
create or replace function authz.nexloop_work_feed(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;v_tenant text:=a->>'tenant_id';v_feed text:=c->>'feed';v_verb text:=c->>'verb';
 v_result jsonb;v_limit integer;v_lease integer;v_delay integer;v_max integer;r runtime.nexloop_work_feed%rowtype;ident jsonb;
begin
 if session_user<>'nexloop_domain_worker' or p_world is null or octet_length(p_text)>1048576 or octet_length(p_payload)>65536
  or v_feed is null or v_feed not in ('recall-instance','claim-match','plan-reevaluate','reply-due') or v_verb is null or v_verb not in ('claim','complete','retry','backlog')
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
