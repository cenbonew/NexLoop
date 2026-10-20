-- Contact refusal rules v2 (ADR-023 §4; owner decision 2026-10-20 "拒绝规则 v2 选 B"; plan §7). The migration number lives only in the
-- file name and the catalog; nothing below repeats it.
-- 1. v2 = deploy/configuration/contact-refusal-rules.v2.json, published as a new row (v1 stays, append-only); the switch from v1 to v2
--    is recorded in control.nexloop_contact_refusal_rule_switches (from, to, decision, reference).
-- 2. control.nexloop_contact_refusal_match (0109 body, change marked): the entries named in marketing_only (unsubscribe,
--    sms-stop-keyword) are kept aside while matching; any other refuse or uncertain hit in the same message is returned first (full
--    stop). Only when nothing else hits is the marketing-only hit returned, flagged, with the marketing window. v1 (no
--    marketing_only) matches exactly as before.
-- 3. runtime.nexloop_inbound_message_protect (0109 body, change marked): a marketing-only hit when the latest dispatched proactive
--    message was marketing within the window records a marketing unsubscribe (source refusal_rule, with the rule and the marketing
--    send) and no contact restriction; otherwise the hit restricts all contact as in v1. Same transaction as the inbound message.
-- 4. The slice 7 unsubscribe trigger (runs after the refusal check, same transaction) does nothing for a message the refusal check
--    already recorded.

-- 1. Rules and switch evidence ------------------------------------------------------------------------------------------------------
insert into control.nexloop_contact_refusal_rules(version,definition,definition_digest,published_by)
 values(2,'{"schema":"nexloop-contact-refusal-rules/1","version":2,"decision":"ADR-023 §4 (owner 2026-10-20, refusal rules v2 option B): the v1 rules and their order, with one narrow exception. The refuse entries listed in marketing_only (unsubscribe: 退订 / 取消订阅 / unsubscribe; sms-stop-keyword: a whole message TD or stop) stop only marketing (a marketing unsubscribe record, no contact restriction) when, and only when, the latest dispatched proactive message to that consumer was marketing and was admitted within window_days. Otherwise they restrict all contact as in v1. Every other refuse entry and every uncertain entry restricts all contact; a message that also hits any of them restricts all contact (a full-stop hit always wins). v1''s unsubscribe entry is split: 拉黑 / 屏蔽你们 / 删除我的号码 move to block-or-erase, which always restricts. Deterministic, no model call; exclusions first as in v1. A customer who wants everything stopped says so again (for example 别联系我) and is restricted.","refuse":[{"id":"stop-contact","pattern":"(不要|别|不用|勿|请勿|不必|甭)\\s*(再|在|继续)?\\s*(给我|跟我|向我|和我|对我|找我)?\\s*(发|打|推送|推|联系|打扰|骚扰|找|call|text)"},{"id":"unsubscribe","pattern":"(退订|取消订阅|unsubscribe)"},{"id":"block-or-erase","pattern":"(拉黑|屏蔽你们|删除我的(号码|手机号|联系方式))"},{"id":"threat-if-contacted","pattern":"(再|继续)\\s*(发|打|联系|骚扰|推送).{0,8}(投诉|报警|举报|起诉)"},{"id":"sms-stop-keyword","pattern":"^\\s*(stop|td)\\s*[。.!！]?\\s*$"}],"uncertain":[{"id":"less-contact","pattern":"(少|别老|别总|不要老|不要总|别一直|不要一直)\\s*(给我)?\\s*(发|打|联系|推)"},{"id":"annoyed","pattern":"(烦死了|很烦|太烦了|别烦我|不胜其烦)"}],"exclusions":[{"id":"goods-and-shipping","pattern":"(发|寄)\\s*(错|漏|顺丰|快递|货|过来|到)"},{"id":"discount-and-payment","pattern":"打\\s*(折|包|开|印|款|钱|车)"},{"id":"change","pattern":"找\\s*(零|钱|补)"}],"marketing_only":{"rule_ids":["unsubscribe","sms-stop-keyword"],"window_days":30}}'::jsonb,encode(sha256(convert_to(runtime.nexloop_canonical_json('{"schema":"nexloop-contact-refusal-rules/1","version":2,"decision":"ADR-023 §4 (owner 2026-10-20, refusal rules v2 option B): the v1 rules and their order, with one narrow exception. The refuse entries listed in marketing_only (unsubscribe: 退订 / 取消订阅 / unsubscribe; sms-stop-keyword: a whole message TD or stop) stop only marketing (a marketing unsubscribe record, no contact restriction) when, and only when, the latest dispatched proactive message to that consumer was marketing and was admitted within window_days. Otherwise they restrict all contact as in v1. Every other refuse entry and every uncertain entry restricts all contact; a message that also hits any of them restricts all contact (a full-stop hit always wins). v1''s unsubscribe entry is split: 拉黑 / 屏蔽你们 / 删除我的号码 move to block-or-erase, which always restricts. Deterministic, no model call; exclusions first as in v1. A customer who wants everything stopped says so again (for example 别联系我) and is restricted.","refuse":[{"id":"stop-contact","pattern":"(不要|别|不用|勿|请勿|不必|甭)\\s*(再|在|继续)?\\s*(给我|跟我|向我|和我|对我|找我)?\\s*(发|打|推送|推|联系|打扰|骚扰|找|call|text)"},{"id":"unsubscribe","pattern":"(退订|取消订阅|unsubscribe)"},{"id":"block-or-erase","pattern":"(拉黑|屏蔽你们|删除我的(号码|手机号|联系方式))"},{"id":"threat-if-contacted","pattern":"(再|继续)\\s*(发|打|联系|骚扰|推送).{0,8}(投诉|报警|举报|起诉)"},{"id":"sms-stop-keyword","pattern":"^\\s*(stop|td)\\s*[。.!！]?\\s*$"}],"uncertain":[{"id":"less-contact","pattern":"(少|别老|别总|不要老|不要总|别一直|不要一直)\\s*(给我)?\\s*(发|打|联系|推)"},{"id":"annoyed","pattern":"(烦死了|很烦|太烦了|别烦我|不胜其烦)"}],"exclusions":[{"id":"goods-and-shipping","pattern":"(发|寄)\\s*(错|漏|顺丰|快递|货|过来|到)"},{"id":"discount-and-payment","pattern":"打\\s*(折|包|开|印|款|钱|车)"},{"id":"change","pattern":"找\\s*(零|钱|补)"}],"marketing_only":{"rule_ids":["unsubscribe","sms-stop-keyword"],"window_days":30}}'::jsonb),'UTF8')),'hex'),'migration:contact-refusal-rules.v2');
create table control.nexloop_contact_refusal_rule_switches (
 switch_id bigint generated always as identity primary key,from_version integer not null references control.nexloop_contact_refusal_rules(version),
 to_version integer not null references control.nexloop_contact_refusal_rules(version),decision text not null check(char_length(decision) between 1 and 500),
 decision_ref text not null check(char_length(decision_ref) between 1 and 200),switched_by text not null,switched_at timestamptz not null default clock_timestamp(),
 check(to_version>from_version)
);
alter table control.nexloop_contact_refusal_rule_switches owner to nexloop_owner;
create trigger refusal_rule_switches_append_only before update or delete on control.nexloop_contact_refusal_rule_switches for each row execute function control.nexloop_nx022_append_only();
revoke all on control.nexloop_contact_refusal_rule_switches from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;
insert into control.nexloop_contact_refusal_rule_switches(from_version,to_version,decision,decision_ref,switched_by)
 values(1,2,'Owner 2026-10-20: refusal rules v2 option B (unsubscribe and TD/stop right after marketing within 30 days stop marketing only; everything else stops all contact)',
  'adr/ADR-023-explicit-refusal-hard-stop.md §4; docs/implementation/outreach-impl-plan.md §7','migration:contact-refusal-rules.v2');

-- Marketing unsubscribes written by the refusal check.
alter table runtime.nexloop_marketing_optouts drop constraint nexloop_marketing_optouts_source_check,
 add constraint nexloop_marketing_optouts_source_check check(source in ('unsubscribe_rule','refusal_rule','owner'));
alter table runtime.nexloop_marketing_optouts drop constraint nexloop_marketing_optouts_check,
 add constraint nexloop_marketing_optouts_check check((source in ('unsubscribe_rule','refusal_rule'))=(kind='unsubscribed' and message_id is not null and rule_version is not null
  and rule_id is not null and outreach_intent is not null and reason is null));
create unique index nexloop_marketing_optouts_refusal_message on runtime.nexloop_marketing_optouts(tenant_id,world,message_id) where source='refusal_rule';

-- The latest dispatched proactive message to the consumer, when it is marketing and admitted within the window (its intent id).
create function runtime.nexloop_latest_marketing_send(p_tenant text,p_world text,p_consumer text,p_days integer) returns text
 language sql stable set search_path=pg_catalog,pg_temp as $$
 select case when s.frequency_class='marketing' and s.admitted_at>clock_timestamp()-make_interval(days=>p_days) then s.intent_id end
  from runtime.nexloop_outreach_sends s where s.tenant_id=p_tenant and s.world=p_world and s.consumer_id=p_consumer and s.state in ('dispatching','unknown','fulfilled')
  order by s.admitted_at desc limit 1
$$;
alter function runtime.nexloop_latest_marketing_send(text,text,text,integer) owner to nexloop_owner;
revoke all on function runtime.nexloop_latest_marketing_send(text,text,text,integer) from public;

-- 2. Matching (0109 body; change marked "ADR-023 §4") -----------------------------------------------------------------------------
create or replace function control.nexloop_contact_refusal_match(p_body text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp as $$
declare r control.nexloop_contact_refusal_rules;x jsonb;t text:=coalesce(p_body,'');m text[];kind text;narrow jsonb;v_only jsonb;
begin
 select * into r from control.nexloop_contact_refusal_rules order by version desc limit 1;
 if not found then return jsonb_build_object('matched',true,'certainty','uncertain','rule_version',0,'rule_id','rules-unavailable','matched_text','');end if;
 v_only:=coalesce(r.definition->'marketing_only'->'rule_ids','[]'::jsonb);
 begin
  for x in select value from jsonb_array_elements(r.definition->'exclusions') loop t:=regexp_replace(t,x->>'pattern','','gi');end loop;
  foreach kind in array array['refuse','uncertain'] loop
   for x in select value from jsonb_array_elements(r.definition->kind) loop
    m:=regexp_match(t,'('||(x->>'pattern')||')','i');
    if m is not null then
     -- ADR-023 §4: a marketing-only entry is kept aside; any other refuse or uncertain hit (full stop) wins.
     if kind='refuse' and v_only ? (x->>'id') then
      if narrow is null then narrow:=jsonb_build_object('matched',true,'certainty',kind,'rule_version',r.version,'rule_id',x->>'id','matched_text',left(m[1],200),
       'marketing_only',true,'window_days',(r.definition->'marketing_only'->>'window_days')::integer);end if;
     else
      return jsonb_build_object('matched',true,'certainty',kind,'rule_version',r.version,'rule_id',x->>'id','matched_text',left(m[1],200));
     end if;
    end if;
   end loop;
  end loop;
 exception when others then
  return jsonb_build_object('matched',true,'certainty','uncertain','rule_version',r.version,'rule_id','match-error','matched_text','');
 end;
 if narrow is not null then return narrow;end if;
 return jsonb_build_object('matched',false,'rule_version',r.version);
end $$;

-- 3. Inbound protective check (0109 body; change marked "ADR-023 §4") --------------------------------------------------------------
create or replace function runtime.nexloop_inbound_message_protect() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v_consumer text;hit jsonb;rev bigint;cur control.nexloop_contact_restrictions;v_at timestamptz;v_marketing text;
begin
 -- Agent replies (0079 materialization) are not inbound consumer messages.
 if new.record ? 'direction' or new.idempotency_key like 'agent-%' then return null;end if;
 perform set_config('eios.tenant_id',new.tenant_id,true);
 select consumer_id into v_consumer from runtime.nexloop_conversations where tenant_id=new.tenant_id and world=new.world and conversation_id=new.conversation_id;
 hit:=control.nexloop_contact_refusal_match(new.record->>'body');
 -- ADR-023 §4 (refusal rules v2, owner option B): a marketing-only hit right after marketing stops marketing only. Any full-stop
 -- hit in the same message was returned instead (the match prefers it), so it never reaches this branch.
 if (hit->>'matched')::boolean and coalesce((hit->>'marketing_only')::boolean,false) then
  v_marketing:=runtime.nexloop_latest_marketing_send(new.tenant_id,new.world,v_consumer,(hit->>'window_days')::integer);
  if v_marketing is not null then
   insert into runtime.nexloop_marketing_optouts(tenant_id,world,consumer_id,kind,source,topic,message_id,rule_version,rule_id,matched_text,outreach_intent,recorded_by)
    values(new.tenant_id,new.world,v_consumer,'unsubscribed','refusal_rule','marketing',new.message_id,(hit->>'rule_version')::integer,hit->>'rule_id',
     left(hit->>'matched_text',200),v_marketing,'system:contact-refusal-rules')
    on conflict do nothing;
   hit:=jsonb_build_object('matched',false,'rule_version',(hit->>'rule_version')::integer,'narrowed',true);
  end if;
 end if;
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

-- 4. The slice 7 unsubscribe trigger (latest body; change marked "Refusal rules v2") ------------------------------------------------
create or replace function runtime.nexloop_inbound_unsubscribe() returns trigger
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
 -- Refusal rules v2: when the refusal check already recorded this message as a marketing unsubscribe, nothing more here.
 if found and last.frequency_class='marketing' and not exists(select 1 from runtime.nexloop_marketing_optouts o
   where o.tenant_id=new.tenant_id and o.world=new.world and o.message_id=new.message_id) then
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
