-- claim.schema.json wire shape for the Conversation evidence view (statements/hypotheses).
-- Same authorization as 0066; only the item projection changes from flat table columns
-- to the canonical nested contract. Storage, RLS and recorder are untouched.
create function authz.nexloop_claim_contract(x ontology.nexloop_claims) returns jsonb
 language sql stable set search_path=pg_catalog as $$
 select jsonb_build_object('claim_id',x.claim_id,'tenant_id',x.tenant_id,'world_id',x.world,'conversation_id',x.conversation_id,
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
create or replace function authz.nexloop_read_conversation_claims(p_digest text,p_world text,p_text text,p_signature text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;k bytea;v_tenant text:=a->>'tenant_id';v_id text:=a->>'conversation_id';v_result jsonb;
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker') or octet_length(p_text)>1048576 or a->>'protocol' is distinct from 'nexloop-claim-read-v1'
  or v_id!~'^[0-9a-f]{64}$' or v_id is null or a->>'target_resource' is distinct from 'eios:object:Conversation/'||v_id
  or a->>'resource_id' is distinct from a->>'target_resource' or (a->>'input_digest' is not null and a->>'input_digest'!~'^[0-9a-f]{64}$') then
  raise exception 'claim read unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-claim-read-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'claim read unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_read_authority(p_digest,p_world,a);
 perform 1 from runtime.nexloop_conversations where tenant_id=v_tenant and world=p_world and conversation_id=v_id for share;
 if not found then raise exception 'claim read unavailable' using errcode='42501';end if;
 select jsonb_build_object(
  'runs',(select coalesce(jsonb_agg(jsonb_build_object('input_digest',r.input_digest,'extractor_version',r.extractor_version,'prompt_version',r.prompt_version,
     'provider',r.provider,'model_id',r.model_id,'timezone',r.timezone,'message_refs',r.message_refs,'rejected',r.rejected,
     'topics',(select coalesce(jsonb_agg(jsonb_build_object('topic_key',t.topic_key,'topic',t.topic,'conversation_summary',t.conversation_summary,
        'first_sequence',t.first_sequence,'last_sequence',t.last_sequence,'message_ids',t.message_ids) order by t.first_sequence,t.topic_key),'[]'::jsonb)
       from ontology.nexloop_conversation_topics t where t.tenant_id=r.tenant_id and t.world=r.world and t.input_digest=r.input_digest)) order by r.recorded_at,r.input_digest),'[]'::jsonb)
   from ontology.nexloop_extraction_runs r where r.tenant_id=v_tenant and r.world=p_world and r.conversation_id=v_id
    and (a->>'input_digest' is null or r.input_digest=a->>'input_digest')),
  'statements',(select coalesce(jsonb_agg(authz.nexloop_claim_contract(x) order by x.source_sequence,x.span_start,x.claim_id),'[]'::jsonb) from ontology.nexloop_claims x
   where x.tenant_id=v_tenant and x.world=p_world and x.conversation_id=v_id and x.epistemic_kind<>'hypothesis'
    and (a->>'input_digest' is null or exists(select 1 from ontology.nexloop_extraction_run_claims rc where rc.tenant_id=x.tenant_id and rc.world=x.world and rc.claim_id=x.claim_id and rc.input_digest=a->>'input_digest'))),
  'hypotheses',(select coalesce(jsonb_agg(authz.nexloop_claim_contract(x) order by x.recorded_at,x.claim_id),'[]'::jsonb) from ontology.nexloop_claims x
   where x.tenant_id=v_tenant and x.world=p_world and x.conversation_id=v_id and x.epistemic_kind='hypothesis'
    and (a->>'input_digest' is null or exists(select 1 from ontology.nexloop_extraction_run_claims rc where rc.tenant_id=x.tenant_id and rc.world=x.world and rc.claim_id=x.claim_id and rc.input_digest=a->>'input_digest')))
 ) into v_result;
 perform authz.nexloop_assert_read_authority(p_digest,p_world,a);
 return v_result;
end $$;
alter function authz.nexloop_read_conversation_claims(text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_read_conversation_claims(text,text,text,text) from public,nexloop_action_worker,nexloop_scheduler,nexloop_identity;
grant execute on function authz.nexloop_read_conversation_claims(text,text,text,text) to nexloop_api,nexloop_domain_worker;
