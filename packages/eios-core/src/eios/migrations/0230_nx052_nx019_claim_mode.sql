-- NX-052: the claim.schema.json wire object gains mode, derived from the world exactly as runtime.nexloop_cost_data_mode
-- (real -> real; simulation and shadow -> simulation; any other world -> test), so a Claim never carries shadow mode.
-- Replaces the 0073 item projection with the field added; storage, RLS, recorder and the read authorization are untouched.
create or replace function authz.nexloop_claim_contract(x ontology.nexloop_claims) returns jsonb
 language sql stable set search_path=pg_catalog,pg_temp as $$
 select jsonb_build_object('claim_id',x.claim_id,'tenant_id',x.tenant_id,'world_id',x.world,
  'mode',case when x.world='real' then 'real' when x.world in ('simulation','shadow') then 'simulation' else 'test' end,
  'conversation_id',x.conversation_id,
  'consumer_id',x.consumer_id,'topic_key',x.topic_key,
  'subject',jsonb_build_object('kind',x.subject_kind,'ref',x.subject_ref,'text',x.subject_text),
  'predicate',x.predicate,'value',x.value,'speaker',x.speaker,'polarity',x.polarity,'modality',x.modality,'condition',x.condition_text,
  'time_expression',x.time_expression,'valid_time',x.valid_time,
  'source',case when x.source_message_id is null then 'null'::jsonb else jsonb_build_object('message_id',x.source_message_id,'sequence',x.source_sequence,
    'span_start',x.span_start,'span_end',x.span_end,'content_hash',x.source_content_hash,'quote',x.quote) end,
  'derived_from',x.derived_from,'corrects_claim_id',x.corrects_claim_id,'extractor_version',x.extractor_version,
  'confidence',x.confidence,'epistemic_kind',x.epistemic_kind,'resolution_state',x.resolution_state,'correlation_key',x.correlation_key,
  'guard_flags',x.guard_flags,'recorded_at',to_char(x.recorded_at at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))
$$;
alter function authz.nexloop_claim_contract(ontology.nexloop_claims) owner to nexloop_owner;
revoke all on function authz.nexloop_claim_contract(ontology.nexloop_claims) from public;
