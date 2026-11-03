-- K5 display folding (owner decision 2026-11-02: duplicate Claims recorded before write-time dedupe stay in the data and are folded
-- only when shown). runtime.nexloop_k5_claims returns each Claim's statement key from the one source, 0412's
-- ontology.nexloop_claim_statement_key; the caller folds visible Claims with an equal key and state. Temporary number; the
-- dispatcher renumbers. Read-only; every other field, the order and the owner are unchanged (create or replace keeps 0406's grants).
set local timezone = 'UTC';

create or replace function runtime.nexloop_k5_claims(p_tenant text,p_consumer text,p_conversation text,p_message text) returns jsonb
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select coalesce(jsonb_agg(jsonb_build_object('claim_id',c.claim_id,'consumer_id',c.consumer_id,'conversation_id',c.conversation_id,
   'source_message_id',c.source_message_id,'sequence',c.source_sequence,'speaker',c.speaker,'predicate',c.predicate,
   'quote',nullif(c.quote,''),'epistemic_kind',c.epistemic_kind,'resolution_state',c.resolution_state,'confidence',c.confidence,
   'corrects_claim_id',c.corrects_claim_id,'erased',c.value ? 'erased','recorded_at',c.recorded_at,
   'statement_key',case when c.value ? 'erased' then null else ontology.nexloop_claim_statement_key(c.source_message_id,c.span_start,
      c.span_end,c.epistemic_kind,c.polarity,c.modality,c.condition_text,c.value) end,
   'match',(select jsonb_build_object('outcome',m.outcome,'reason',m.reason,'type_ref',m.decision->'type'->>'ref',
      'property_ref',m.decision->'property'->>'ref','proposal_id',m.proposal_id,'candidate_id',m.candidate_id,'recorded_at',m.recorded_at)
     from ontology.nexloop_claim_matches m where m.tenant_id=p_tenant and m.world='real' and m.claim_id=c.claim_id
     order by m.recorded_at desc,m.matcher_version desc limit 1))
   order by c.recorded_at,c.claim_id),'[]'::jsonb)
 from ontology.nexloop_claims c
 where c.tenant_id=p_tenant and c.world='real' and (p_consumer is null or c.consumer_id=p_consumer)
  and (p_conversation is null or c.conversation_id=p_conversation) and (p_message is null or c.source_message_id=p_message) $$;
