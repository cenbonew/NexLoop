-- Contact refusal rules v3 and the governed history backfill (ADR-023 §4; owner decision 2026-11-02, plan
-- docs/implementation/claim-to-consumer-property-plan.md §9.6, §10 item 4, §11). The migration number lives only in the file name and the
-- catalog; nothing below repeats it.
-- 1. v3 = deploy/configuration/contact-refusal-rules.v3.json, published as a new row (v1, v2 stay; append-only); the switch from v2 to v3
--    is recorded in control.nexloop_contact_refusal_rule_switches. control.nexloop_contact_refusal_match and the inbound trigger are
--    unchanged: they always use the latest row, so every inbound message from now on is checked with v3.
-- 2. authz.nexloop_contact_refusal_backfill (eios:action:nexloop.contact.refusal_backfill:1, protocol nexloop-contact-refusal-backfill-v1):
--    the reply guarantor service re-checks the tenant's earlier inbound consumer messages with the current rules and restricts contact
--    through the same control writer as the inbound trigger (control.nexloop_nx022_bump, idempotency key contact-refusal:<message_id>,
--    then the restriction and hit rows). Tenant from the service credential, world from its binding, caller role nexloop_domain_worker
--    only (authz.nexloop_plan_port_tenant). Skipped: Consumers with any erasure request, persona simulated_agent or synthetic, a
--    restriction the owner released (the owner's decision stands), messages that already have a hit, marketing-only hits (the marketing
--    window at the time cannot be judged afterwards). Consumers already restricted get evidence only, no new control revision.
--    Repeatable: the second run finds the hits of the first. verb dry_run writes nothing; both verbs return counts only.

-- 1. Rules and switch evidence ------------------------------------------------------------------------------------------------------
insert into control.nexloop_contact_refusal_rules(version,definition,definition_digest,published_by)
 values(3,'{"schema":"nexloop-contact-refusal-rules/1","version":3,"decision":"ADR-023 §4 (owner 2026-11-02, plan claim-to-consumer-property §9.6/§10/§11): v2 and its marketing_only exception unchanged, extended. stop-contact accepts one adverb between the negation and the verb (主动 / 随便 / 没事: 不要再主动联系我) and is not a refusal when the object is someone else (不要联系我同事). stop-contact-en (new, case-insensitive) covers English refusals (don''t (proactively) contact me, stop messaging me), not when ''me'' is followed by a form of address, a time, a channel object or a preposition (don''t call me sir / at night / before 9; don''t email me the invoice). self-initiated-only (new, uncertain, restricts as well): the customer says they will reach out themselves. New exclusions remove time-limited phrases (晚上不要打电话: quiet hours, a 0228 preference) and channel-only phrases (不要电话和短信: a 0228 channel preference) before matching. Deterministic, no model call.","refuse":[{"id":"stop-contact","pattern":"(不要|别|不用|勿|请勿|不必|甭)\\s*(再|在|继续)?\\s*(主动|随便|没事)?\\s*(给我|跟我|向我|和我|对我|找我)?\\s*(发|打|推送|推|联系|打扰|骚扰|找|call|text)(?!\\s*我?\\s*的?\\s*(同事|家人|朋友|老婆|老公|爱人|妈妈|爸爸|孩子|领导|老板|助理|秘书))"},{"id":"stop-contact-en","pattern":"(don[’'']?t|do not|never|stop|quit)\\s+(ever\\s+)?(proactively\\s+|again\\s+)?(contact|contacting|message|messaging|text|texting|call|calling|email|emailing|ping|pinging)\\s+me\\M(?!\\s*(sir|madam|ma[’'']?am|mister|miss|mr|mrs|ms|dear|buddy|by|at|before|after|on|in|during|until|till|about|the|that|this|with|via|over|through|[0-9]))"},{"id":"unsubscribe","pattern":"(退订|取消订阅|unsubscribe)"},{"id":"block-or-erase","pattern":"(拉黑|屏蔽你们|删除我的(号码|手机号|联系方式))"},{"id":"threat-if-contacted","pattern":"(再|继续)\\s*(发|打|联系|骚扰|推送).{0,8}(投诉|报警|举报|起诉)"},{"id":"sms-stop-keyword","pattern":"^\\s*(stop|td)\\s*[。.!！]?\\s*$"}],"uncertain":[{"id":"less-contact","pattern":"(少|别老|别总|不要老|不要总|别一直|不要一直)\\s*(给我)?\\s*(发|打|联系|推)"},{"id":"annoyed","pattern":"(烦死了|很烦|太烦了|别烦我|不胜其烦)"},{"id":"self-initiated-only","pattern":"(有(需要|事|问题)\\s*(的话)?\\s*我?\\s*(会)?\\s*自己\\s*(来)?\\s*(问|找|联系|咨询))|(I[’'']?ll\\s+(reach out|contact you|get in touch)\\s+(myself|when|if))"}],"exclusions":[{"id":"goods-and-shipping","pattern":"(发|寄)\\s*(错|漏|顺丰|快递|货|过来|到)"},{"id":"discount-and-payment","pattern":"打\\s*(折|包|开|印|款|钱|车)"},{"id":"change","pattern":"找\\s*(零|钱|补)"},{"id":"time-limited","pattern":"(晚上|夜里|夜间|深夜|半夜|早上|早晨|上午|中午|下午|周末|节假日|休息日|上班时间|工作时间|午休|[0-9一二三四五六七八九十]+\\s*点\\s*(以前|之前|以后|之后|前|后)?)\\s*(的时候)?\\s*(请)?\\s*(不要|别|勿|请勿)\\s*(给我)?\\s*(发|打|联系|打扰|推送|推)[^，。,.!！?？;；]*"},{"id":"time-limited-after","pattern":"(不要|别|勿|请勿)\\s*在\\s*(晚上|夜里|夜间|深夜|半夜|早上|早晨|上午|中午|下午|周末|节假日|休息日|上班时间|工作时间|午休|[0-9一二三四五六七八九十]+\\s*点\\s*(以前|之前|以后|之后|前|后)?)[^，。,.!！?？;；]*"},{"id":"channel-only","pattern":"(不要|别|不用)\\s*(打)?\\s*(电话|短信|邮件|微信)(\\s*(和|或|、|跟)\\s*(电话|短信|邮件|微信))*"}],"marketing_only":{"rule_ids":["unsubscribe","sms-stop-keyword"],"window_days":30}}'::jsonb,encode(sha256(convert_to(runtime.nexloop_canonical_json('{"schema":"nexloop-contact-refusal-rules/1","version":3,"decision":"ADR-023 §4 (owner 2026-11-02, plan claim-to-consumer-property §9.6/§10/§11): v2 and its marketing_only exception unchanged, extended. stop-contact accepts one adverb between the negation and the verb (主动 / 随便 / 没事: 不要再主动联系我) and is not a refusal when the object is someone else (不要联系我同事). stop-contact-en (new, case-insensitive) covers English refusals (don''t (proactively) contact me, stop messaging me), not when ''me'' is followed by a form of address, a time, a channel object or a preposition (don''t call me sir / at night / before 9; don''t email me the invoice). self-initiated-only (new, uncertain, restricts as well): the customer says they will reach out themselves. New exclusions remove time-limited phrases (晚上不要打电话: quiet hours, a 0228 preference) and channel-only phrases (不要电话和短信: a 0228 channel preference) before matching. Deterministic, no model call.","refuse":[{"id":"stop-contact","pattern":"(不要|别|不用|勿|请勿|不必|甭)\\s*(再|在|继续)?\\s*(主动|随便|没事)?\\s*(给我|跟我|向我|和我|对我|找我)?\\s*(发|打|推送|推|联系|打扰|骚扰|找|call|text)(?!\\s*我?\\s*的?\\s*(同事|家人|朋友|老婆|老公|爱人|妈妈|爸爸|孩子|领导|老板|助理|秘书))"},{"id":"stop-contact-en","pattern":"(don[’'']?t|do not|never|stop|quit)\\s+(ever\\s+)?(proactively\\s+|again\\s+)?(contact|contacting|message|messaging|text|texting|call|calling|email|emailing|ping|pinging)\\s+me\\M(?!\\s*(sir|madam|ma[’'']?am|mister|miss|mr|mrs|ms|dear|buddy|by|at|before|after|on|in|during|until|till|about|the|that|this|with|via|over|through|[0-9]))"},{"id":"unsubscribe","pattern":"(退订|取消订阅|unsubscribe)"},{"id":"block-or-erase","pattern":"(拉黑|屏蔽你们|删除我的(号码|手机号|联系方式))"},{"id":"threat-if-contacted","pattern":"(再|继续)\\s*(发|打|联系|骚扰|推送).{0,8}(投诉|报警|举报|起诉)"},{"id":"sms-stop-keyword","pattern":"^\\s*(stop|td)\\s*[。.!！]?\\s*$"}],"uncertain":[{"id":"less-contact","pattern":"(少|别老|别总|不要老|不要总|别一直|不要一直)\\s*(给我)?\\s*(发|打|联系|推)"},{"id":"annoyed","pattern":"(烦死了|很烦|太烦了|别烦我|不胜其烦)"},{"id":"self-initiated-only","pattern":"(有(需要|事|问题)\\s*(的话)?\\s*我?\\s*(会)?\\s*自己\\s*(来)?\\s*(问|找|联系|咨询))|(I[’'']?ll\\s+(reach out|contact you|get in touch)\\s+(myself|when|if))"}],"exclusions":[{"id":"goods-and-shipping","pattern":"(发|寄)\\s*(错|漏|顺丰|快递|货|过来|到)"},{"id":"discount-and-payment","pattern":"打\\s*(折|包|开|印|款|钱|车)"},{"id":"change","pattern":"找\\s*(零|钱|补)"},{"id":"time-limited","pattern":"(晚上|夜里|夜间|深夜|半夜|早上|早晨|上午|中午|下午|周末|节假日|休息日|上班时间|工作时间|午休|[0-9一二三四五六七八九十]+\\s*点\\s*(以前|之前|以后|之后|前|后)?)\\s*(的时候)?\\s*(请)?\\s*(不要|别|勿|请勿)\\s*(给我)?\\s*(发|打|联系|打扰|推送|推)[^，。,.!！?？;；]*"},{"id":"time-limited-after","pattern":"(不要|别|勿|请勿)\\s*在\\s*(晚上|夜里|夜间|深夜|半夜|早上|早晨|上午|中午|下午|周末|节假日|休息日|上班时间|工作时间|午休|[0-9一二三四五六七八九十]+\\s*点\\s*(以前|之前|以后|之后|前|后)?)[^，。,.!！?？;；]*"},{"id":"channel-only","pattern":"(不要|别|不用)\\s*(打)?\\s*(电话|短信|邮件|微信)(\\s*(和|或|、|跟)\\s*(电话|短信|邮件|微信))*"}],"marketing_only":{"rule_ids":["unsubscribe","sms-stop-keyword"],"window_days":30}}'::jsonb),'UTF8')),'hex'),'migration:contact-refusal-rules.v3');
insert into control.nexloop_contact_refusal_rule_switches(from_version,to_version,decision,decision_ref,switched_by)
 values(2,3,'Owner 2026-11-02: refusal rules v3 (adverbs such as 主动, English refusals, self-initiated contact as uncertain; time-limited and channel-only phrases excluded as 0228 preferences)',
  'adr/ADR-023-explicit-refusal-hard-stop.md §4; docs/implementation/claim-to-consumer-property-plan.md §9.6','migration:contact-refusal-rules.v3');

-- 2. History backfill ---------------------------------------------------------------------------------------------------------------
create function authz.nexloop_contact_refusal_backfill(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-contact-refusal-backfill-v1',
  'eios:action:nexloop.contact.refusal_backfill:1',array['nexloop_domain_worker']);
 c jsonb:=p_payload::jsonb;apply boolean;v_rules integer;m record;hit jsonb;cur control.nexloop_contact_restrictions;rev bigint;
 n jsonb:=jsonb_build_object('scanned',0,'matched',0,'restricted',0,'evidence_only',0,'already_hit',0,'skipped_erasure',0,'skipped_persona',0,
  'skipped_released',0,'skipped_marketing_only',0);
 by_rule jsonb:='{}'::jsonb;restricted text[]:='{}';
begin
 if jsonb_typeof(c) is distinct from 'object' or (select count(*) from jsonb_object_keys(c))<>2 or c->>'verb' not in ('dry_run','apply')
  or jsonb_typeof(c->'rule_version') is distinct from 'number' then
  raise exception 'contact refusal backfill invalid' using errcode='22023';end if;
 apply:=c->>'verb'='apply';
 -- The caller names the rule version it reviewed; a newer published set refuses the run (re-review first).
 select max(version) into v_rules from control.nexloop_contact_refusal_rules;
 if v_rules is distinct from (c->>'rule_version')::integer then raise exception 'contact refusal rules changed' using errcode='40001';end if;
 for m in select cm.message_id,cm.conversation_id,cm.record->>'body' body,cv.consumer_id
   from runtime.nexloop_conversation_messages cm
   join runtime.nexloop_conversations cv on cv.tenant_id=cm.tenant_id and cv.world=cm.world and cv.conversation_id=cm.conversation_id
  where cm.tenant_id=t and cm.world=p_world and not (cm.record ? 'direction') and cm.idempotency_key not like 'agent-%'
  order by cm.conversation_id,cm.sequence loop
  n:=jsonb_set(n,'{scanned}',to_jsonb((n->>'scanned')::integer+1));
  if exists(select 1 from control.nexloop_contact_refusal_hits h where h.tenant_id=t and h.world=p_world and h.message_id=m.message_id) then
   n:=jsonb_set(n,'{already_hit}',to_jsonb((n->>'already_hit')::integer+1));continue;end if;
  hit:=control.nexloop_contact_refusal_match(m.body);
  if not coalesce((hit->>'matched')::boolean,false) then continue;end if;
  n:=jsonb_set(n,'{matched}',to_jsonb((n->>'matched')::integer+1));
  if exists(select 1 from control.nexloop_consumer_erasures e where e.tenant_id=t and e.world=p_world and e.consumer_id=m.consumer_id) then
   n:=jsonb_set(n,'{skipped_erasure}',to_jsonb((n->>'skipped_erasure')::integer+1));continue;end if;
  if runtime.nexloop_consumer_persona(t,p_world,m.consumer_id)<>'real' then
   n:=jsonb_set(n,'{skipped_persona}',to_jsonb((n->>'skipped_persona')::integer+1));continue;end if;
  if coalesce((hit->>'marketing_only')::boolean,false) then
   n:=jsonb_set(n,'{skipped_marketing_only}',to_jsonb((n->>'skipped_marketing_only')::integer+1));continue;end if;
  select * into cur from control.nexloop_contact_restrictions where tenant_id=t and world=p_world and consumer_id=m.consumer_id for update;
  if found and cur.released_at is not null then
   n:=jsonb_set(n,'{skipped_released}',to_jsonb((n->>'skipped_released')::integer+1));continue;end if;
  by_rule:=jsonb_set(by_rule,array[hit->>'rule_id'],to_jsonb(coalesce((by_rule->>(hit->>'rule_id'))::integer,0)+1));
  if (found and cur.active) or m.consumer_id=any(restricted) then
   n:=jsonb_set(n,'{evidence_only}',to_jsonb((n->>'evidence_only')::integer+1));
   if apply then
    select control_revision into rev from control.nexloop_contact_restrictions where tenant_id=t and world=p_world and consumer_id=m.consumer_id;
   end if;
  else
   n:=jsonb_set(n,'{restricted}',to_jsonb((n->>'restricted')::integer+1));restricted:=restricted||m.consumer_id;
   if apply then
    -- Same control writer and idempotency key as the inbound trigger (0109/0244).
    rev:=control.nexloop_nx022_bump(t,p_world,'contact_restricted','consumer',m.consumer_id,
     jsonb_build_object('message_id',m.message_id,'rule_version',(hit->>'rule_version')::integer,'rule_id',hit->>'rule_id','certainty',hit->>'certainty','backfill',true),
     'system:contact-refusal-backfill','contact-refusal:'||m.message_id);
    insert into control.nexloop_contact_restrictions(tenant_id,world,consumer_id,active,control_revision,message_id,conversation_id,rule_version,rule_id,certainty,matched_text,restricted_at)
     values(t,p_world,m.consumer_id,true,rev,m.message_id,m.conversation_id,(hit->>'rule_version')::integer,hit->>'rule_id',hit->>'certainty',hit->>'matched_text',clock_timestamp());
   end if;
  end if;
  if apply then
   insert into control.nexloop_contact_refusal_hits(tenant_id,world,message_id,consumer_id,conversation_id,rule_version,rule_id,certainty,matched_text,control_revision)
    values(t,p_world,m.message_id,m.consumer_id,m.conversation_id,(hit->>'rule_version')::integer,hit->>'rule_id',hit->>'certainty',hit->>'matched_text',rev)
    on conflict do nothing;
  end if;
 end loop;
 return jsonb_build_object('verb',c->>'verb','rule_version',v_rules,'counts',n,'by_rule',by_rule,'consumers_restricted',cardinality(restricted));
end $$;
alter function authz.nexloop_contact_refusal_backfill(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_contact_refusal_backfill(text,text,text,text,text) from public;
grant execute on function authz.nexloop_contact_refusal_backfill(text,text,text,text,text) to nexloop_domain_worker;
