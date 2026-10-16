-- Conversation turns in the message Context (temporary number 0201; 10/16 stage: "我刚才和你说我要的是什么？" got "我这边无法读取到我们之前的对话记录").
-- The v6 pack of a message Run carried the current statement only; earlier turns of the conversation reached the model only
-- as extracted Claims (and not at all before extraction ran). Now:
-- 1. authz.nexloop_read_conversation_turns: the latest turns of one conversation (accepted inbound messages and materialized
--    channel-accepted Agent replies, erased ones excluded), read under the reader's current Conversation READ decision
--    (configured, or derived through its message_read_rule: 0077/0102), verified as authz.nexloop_read_conversation_claims does.
-- 2. authz.nexloop_context_v6_source_in (latest body: 0145, copied verbatim) also verifies an evidence item
--    eios:object:Message/<id> (subsection conversation): the message is in the Run's conversation, not erased, an outbound one
--    channel-accepted, the decision targets that Conversation, and the content equals {speaker, body, sequence, accepted_at}.
-- Untrusted data, never formal state; nothing is written.
create function authz.nexloop_read_conversation_turns(p_digest text,p_world text,p_text text,p_signature text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;k bytea;v_tenant text:=a->>'tenant_id';v_id text:=a->>'conversation_id';v_limit integer;v_result jsonb;
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker') or octet_length(p_text)>1048576 or a->>'protocol' is distinct from 'nexloop-turn-read-v1'
  or v_id is null or v_id!~'^[0-9a-f]{64}$' or a->>'target_resource' is distinct from 'eios:object:Conversation/'||v_id
  or a->>'resource_id' is distinct from a->>'target_resource' or jsonb_typeof(a->'limit') is distinct from 'number' or (a->>'limit')!~'^[0-9]{1,2}$' then
  raise exception 'turn read unavailable' using errcode='42501';end if;
 v_limit:=(a->>'limit')::integer;
 if v_limit<1 or v_limit>64 then raise exception 'turn read unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-turn-read-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'turn read unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_read_authority(p_digest,p_world,a);
 perform 1 from runtime.nexloop_conversations where tenant_id=v_tenant and world=p_world and conversation_id=v_id for share;
 if not found then raise exception 'turn read unavailable' using errcode='42501';end if;
 select coalesce(jsonb_agg(t.record order by t.sequence),'[]'::jsonb) into v_result from (
  select m.sequence,jsonb_build_object('id',m.message_id,'sequence',m.sequence,'body',m.record->>'body','accepted_at',m.record->>'accepted_at',
   'direction',coalesce(m.record->>'direction','inbound')) record
  from runtime.nexloop_conversation_messages m where m.tenant_id=v_tenant and m.world=p_world and m.conversation_id=v_id
   and coalesce(m.record->>'erased','false')<>'true' and (m.record->>'direction' is distinct from 'outbound' or m.record->>'status'='accepted')
  order by m.sequence desc limit v_limit) t;
 perform authz.nexloop_assert_read_authority(p_digest,p_world,a);
 return jsonb_build_object('turns',v_result);
end $$;
alter function authz.nexloop_read_conversation_turns(text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_read_conversation_turns(text,text,text,text) from public;
grant execute on function authz.nexloop_read_conversation_turns(text,text,text,text) to nexloop_api,nexloop_domain_worker;

create or replace function authz.nexloop_context_v6_source_in(p_tenant text,p_world text,p_conversations text[],p_consumer text,p_section text,p_item jsonb,p_proofs jsonb)
returns jsonb language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ref text:=p_item->>'ref';pl runtime.nexloop_plans;cm ontology.objects;cur record;proof jsonb;x ontology.nexloop_claims;i runtime.nexloop_effect_intents;o runtime.nexloop_outbound_messages;
 obj ontology.objects;expected jsonb;kind text;sub text;revision text;tags text[]:='{}';prop text;msg runtime.nexloop_conversation_messages;
begin
 if jsonb_typeof(p_item) is distinct from 'object' or (select count(*) from jsonb_object_keys(p_item))<>10
  or not p_item ?& array['subsection','ref','revision','content','content_hash','evidence_kind','access_decision_ref','relevance_permille','at','tags']
  or jsonb_typeof(p_item->'relevance_permille') is distinct from 'number' or (p_item->>'relevance_permille')!~'^[0-9]{1,4}$' or (p_item->>'relevance_permille')::integer>1000
  or jsonb_typeof(p_item->'at') is distinct from 'string' or length(p_item->>'at')>64 or jsonb_typeof(p_item->'tags') is distinct from 'array'
  or jsonb_typeof(p_item->'revision') is distinct from 'string' then raise exception 'context source shape invalid' using errcode='42501';end if;
 if p_item->>'content_hash' is distinct from runtime.nexloop_context_hash(p_item->'content') then raise exception 'context source hash mismatch' using errcode='42501';end if;
 proof:=p_proofs->(p_item->>'access_decision_ref');
 if proof is null then raise exception 'context source decision unknown' using errcode='42501';end if;
 if ref ~ '^claim:[0-9a-f]{64}$' then
  -- AT-064: a Claim is evidence only; never formal state, never a planning fact.
  if p_section<>'evidence' then raise exception 'context Claim outside evidence' using errcode='42501';end if;
  select * into x from ontology.nexloop_claims where tenant_id=p_tenant and world=p_world and claim_id=substr(ref,7) and conversation_id=any(p_conversations);
  if not found then raise exception 'context Claim unavailable' using errcode='42501';end if;
  if proof->>'target_resource' is distinct from 'eios:object:Conversation/'||x.conversation_id then raise exception 'context source decision mismatch' using errcode='42501';end if;
  if x.epistemic_kind='hypothesis' then
   -- Real-world strategies cannot include hypotheses (0088); nothing else may carry one.
   raise exception 'context hypothesis excluded' using errcode='42501';
  end if;
  if x.source_message_id is null or x.resolution_state not in ('unresolved','needs_resolution','awaiting_definition','resolved') then
   raise exception 'context Claim not admissible evidence' using errcode='42501';end if;
  sub:='claim_evidence';revision:=x.extractor_version;kind:=case when x.speaker='consumer' then 'user_statement' else 'conversation' end;
  -- Original text only: no structured value can act as a formal property.
  expected:=jsonb_build_object('quote',x.quote,'message_ref','eios:object:Message/'||x.source_message_id,'span',jsonb_build_array(x.span_start,x.span_end),
   'speaker',x.speaker,'epistemic_kind',x.epistemic_kind,'resolution_state',x.resolution_state,'modality',x.modality,'condition',x.condition_text);
  if x.polarity='negated' then tags:=tags||'negation'::text;end if;
  if x.epistemic_kind='constraint' then tags:=tags||'contact_limit'::text;end if;
 elsif ref ~ '^nexloop:intent:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' then
  if p_section<>'open_work' then raise exception 'context execution state outside open work' using errcode='42501';end if;
  select * into i from runtime.nexloop_effect_intents where intent_id=substr(ref,16)::uuid and tenant_id=p_tenant and world=p_world and consumer_id=p_consumer;
  if not found or i.state not in ('accepted','dispatching','unknown') then raise exception 'context intent unavailable' using errcode='42501';end if;
  if proof->>'target_resource' is distinct from 'eios:object:Consumer/'||p_consumer then raise exception 'context source decision mismatch' using errcode='42501';end if;
  sub:='intents';revision:=i.state;kind:='execution_state';tags:=array['unconfirmed'];
  expected:=jsonb_build_object('action',i.action_name||':'||i.action_version,'state',i.state,'since',i.created_at);
 elsif ref ~ '^nexloop:outbound:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' then
  if p_section<>'open_work' then raise exception 'context execution state outside open work' using errcode='42501';end if;
  select o2.* into o from runtime.nexloop_outbound_messages o2 join runtime.nexloop_conversations cv on cv.tenant_id=o2.tenant_id and cv.world=o2.world and cv.conversation_id=o2.conversation_id
   where o2.tenant_id=p_tenant and o2.world=p_world and o2.intent_id=substr(ref,18)::uuid and cv.consumer_id=p_consumer;
  -- Channel-accepted replies are conversation, never unconfirmed execution (ADR-020 §2).
  if not found or o.delivery_state in ('provider_accepted','delivered') then raise exception 'context outbound unavailable' using errcode='42501';end if;
  if proof->>'target_resource' is distinct from 'eios:object:Consumer/'||p_consumer then raise exception 'context source decision mismatch' using errcode='42501';end if;
  sub:='outbound';revision:=o.delivery_state;kind:='execution_state';
  if o.delivery_state in ('persisted','dispatching','unknown') then tags:=array['unconfirmed'];end if;
  expected:=jsonb_build_object('delivery_state',o.delivery_state,'since',o.delivery_changed_at,'note','not confirmed as delivered to the consumer');
 elsif ref ~ '^nexloop:plan:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}@[1-9][0-9]{0,5}$' then
  -- NX-025 (G4): a plan is open work of its Consumer; only its current active version, read-only.
  if p_section<>'open_work' then raise exception 'context plan outside open work' using errcode='42501';end if;
  select * into pl from runtime.nexloop_plans where tenant_id=p_tenant and world=p_world and plan_id=split_part(substr(ref,14),'@',1)::uuid
   and version=split_part(ref,'@',2)::integer and consumer_id=p_consumer;
  if not found then raise exception 'context plan unavailable' using errcode='42501';end if;
  select * into cur from runtime.nexloop_plan_current(p_tenant,p_world,pl.plan_id);
  if cur.status is distinct from 'active' or cur.version is distinct from pl.version then raise exception 'context plan not current' using errcode='42501';end if;
  if proof->>'target_resource' is distinct from 'eios:object:Consumer/'||p_consumer then raise exception 'context source decision mismatch' using errcode='42501';end if;
  sub:='plan';revision:=pl.version::text;kind:='policy';
  expected:=runtime.nexloop_plan_context(p_tenant,p_world,pl.plan_id,pl.version);
 elsif ref ~ '^eios:object:Commitment/[0-9a-f]{64}$' then
  -- NX-026: a live commitment made to this Consumer is open work; its current revision, read-only.
  if p_section<>'open_work' then raise exception 'context commitment outside open work' using errcode='42501';end if;
  select * into cm from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='Commitment' and object_id=substr(ref,24)
   and properties->>'made_to'=p_consumer and properties->>'status' in ('conditional','open','in_progress','breached');
  if not found then raise exception 'context commitment unavailable' using errcode='42501';end if;
  if proof->>'target_resource' is distinct from 'eios:object:Consumer/'||p_consumer then raise exception 'context source decision mismatch' using errcode='42501';end if;
  sub:='commitment';revision:=cm.nexloop_revision::text;kind:='formal_object';
  expected:=runtime.nexloop_commitment_context(p_tenant,p_world,cm.object_id);
 elsif ref ~ '^eios:object:IncentiveOffering/[0-9a-f]{64}$' then
  -- AT-043 D8: an incentive this Consumer may be issued is open work; re-derived now (catalog and issuable range).
  if p_section<>'open_work' then raise exception 'context incentive outside open work' using errcode='42501';end if;
  if proof->>'target_resource' is distinct from 'eios:object:Consumer/'||p_consumer then raise exception 'context source decision mismatch' using errcode='42501';end if;
  select ic.revision,ic.content into revision,expected from runtime.nexloop_incentive_catalog_for(p_tenant,p_world,p_consumer) ic where ic.offering_id=substr(ref,31);
  if not found then raise exception 'context incentive unavailable' using errcode='42501';end if;
  sub:='incentive';kind:='formal_object';
 elsif ref ~ '^eios:object:Consumer/[0-9a-f]{64}$' then
  if p_section not in ('constraints','consumer_state') or substr(ref,22) is distinct from p_consumer then raise exception 'context formal object outside formal state' using errcode='42501';end if;
  select * into obj from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='Consumer' and object_id=p_consumer;
  if not found or proof->>'target_resource' is distinct from 'eios:object:Consumer/'||p_consumer then raise exception 'context source decision mismatch' using errcode='42501';end if;
  if jsonb_typeof(p_item->'content'->'properties') is distinct from 'object' then raise exception 'context formal projection invalid' using errcode='42501';end if;
  -- Projection only: every shown property is current and was itself READ-authorized.
  for prop in select jsonb_object_keys(p_item->'content'->'properties') loop
   if not obj.properties ? prop or not exists(select 1 from jsonb_each(p_proofs) q where q.value->>'target_resource'='eios:property:Consumer/'||p_consumer||'/'||prop) then
    raise exception 'context formal property unreadable' using errcode='42501';end if;
  end loop;
  sub:='Consumer';revision:=obj.nexloop_revision::text;kind:='formal_object';
  expected:=jsonb_build_object('type','Consumer','properties',(select coalesce(jsonb_object_agg(k,obj.properties->k),'{}'::jsonb) from jsonb_object_keys(p_item->'content'->'properties') k));
 elsif ref ~ '^eios:object:Message/[0-9a-f]{64}$' then
  -- 0201: a conversation turn (accepted inbound, or a materialized channel-accepted Agent reply) of the Run's own conversation,
  -- read under that Conversation's READ decision; erased messages never enter.
  if p_section<>'evidence' then raise exception 'context conversation turn outside evidence' using errcode='42501';end if;
  select * into msg from runtime.nexloop_conversation_messages where tenant_id=p_tenant and world=p_world and message_id=substr(ref,21)
   and conversation_id=any(p_conversations);
  if not found or coalesce(msg.record->>'erased','false')='true' or (msg.record->>'direction'='outbound' and msg.record->>'status' is distinct from 'accepted') then
   raise exception 'context conversation turn unavailable' using errcode='42501';end if;
  if proof->>'target_resource' is distinct from 'eios:object:Conversation/'||msg.conversation_id then raise exception 'context source decision mismatch' using errcode='42501';end if;
  sub:='conversation';kind:='conversation';revision:=msg.sequence::text;
  expected:=jsonb_build_object('speaker',case when msg.record->>'direction'='outbound' then 'agent' else 'consumer' end,'body',msg.record->>'body',
   'sequence',msg.sequence,'accepted_at',msg.record->>'accepted_at');
 else
  raise exception 'context source kind unsupported' using errcode='42501';
 end if;
 if p_item->>'subsection' is distinct from sub or p_item->>'evidence_kind' is distinct from kind or p_item->>'revision' is distinct from revision
  or p_item->'content' is distinct from expected then raise exception 'context source content mismatch' using errcode='42501';end if;
 return to_jsonb(array(select t from unnest(tags) t order by t));
end $$;
