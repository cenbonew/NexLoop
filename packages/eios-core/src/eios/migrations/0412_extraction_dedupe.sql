-- S-DUP (docs/implementation/S-DUP-extraction-dedupe-design.md §2, §3): the recorder writes the same statement once, a correction may point
-- at an earlier current Claim of the Conversation, and no Claim may cite a context Message. The migration number lives only in the file name
-- and the catalog; nothing below repeats it.
-- 1. ontology.nexloop_claim_value_key(value): the normalized value of the dedupe key, as conversation_extraction.normalize_value_key:
--    type plus, for a string, the text without any whitespace and lower-cased; any other value as its JSON (so "3" and 3 differ).
-- 2. authz.nexloop_record_claim_extraction_v0066 (0066 body, changes marked "S-DUP", registered in tests/support/s_dup_edits.py): a Claim with
--    the same Message span, kind, polarity, modality, condition presence and normalized value as a current Claim (not superseded) is not
--    written again; the run links the earlier Claim. Claims cited by another Claim of the same run (derivation, correction) are always
--    written. claim_id is unchanged.
-- 3. authz.nexloop_record_claim_extraction (0115 wrapper renamed to ..._v0115; this one repeats it with two changes marked "S-DUP"):
--    a Claim citing a Message marked context in the payload is refused; a correction may also point at a current explicit Claim of the
--    same Conversation outside the window, from an earlier Message (receipt order), not erased or superseded.

create function ontology.nexloop_claim_value_key(p_value jsonb) returns text language sql immutable set search_path=pg_catalog,pg_temp as $$
 select coalesce(p_value->>'type','')||':'||case when jsonb_typeof(p_value->'value')='string' then lower(regexp_replace(p_value->>'value','\s+','','g'))
  else coalesce((p_value->'value')::text,'null') end
$$;
alter function ontology.nexloop_claim_value_key(jsonb) owner to nexloop_owner;
create index nexloop_claims_statement on ontology.nexloop_claims(tenant_id,world,source_message_id,span_start,span_end,epistemic_kind);

create or replace function authz.nexloop_record_claim_extraction_v0066(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;v_tenant text:=a->>'tenant_id';
 v_conversation runtime.nexloop_conversations%rowtype;v_row runtime.nexloop_conversation_messages%rowtype;v_obj ontology.objects%rowtype;
 v_existing ontology.nexloop_extraction_runs%rowtype;m jsonb;t jsonb;cl jsonb;src jsonb;v_field text;v_body text;v_speaker text;
 v_previous bigint:=0;v_messages jsonb:='{}'::jsonb;v_topics jsonb:='{}'::jsonb;v_claims jsonb:='{}'::jsonb;v_kind text;v_ids jsonb;v_ref text;
 v_start integer;v_end integer;v_dup text;v_refs jsonb;
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker') or p_world is null
  or octet_length(p_text)>8388608 or octet_length(p_payload)>4194304
  or a->>'protocol' is distinct from 'nexloop-claim-extraction-v1' or a->>'operation' is distinct from 'execute'
  or a->>'action_resource' is distinct from 'eios:action:nexloop.claim.extract:1'
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or c->>'verb' is distinct from 'record' or c->>'input_digest' !~ '^[0-9a-f]{64}$' or c->>'input_digest' is null
  or jsonb_typeof(c->'messages') is distinct from 'array' or jsonb_array_length(c->'messages') not between 1 and 200
  or jsonb_typeof(c->'topics') is distinct from 'array' or jsonb_typeof(c->'claims') is distinct from 'array' or jsonb_typeof(c->'rejected') is distinct from 'array'
  or jsonb_array_length(c->'claims')>256 or jsonb_typeof(a->'source_authorities') is distinct from 'array' then
  raise exception 'claim extraction unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-claim-extraction-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'claim extraction unavailable' using errcode='42501';end if;
 -- Background extraction is a governed service, never a consumer browser session.
 if exists(select 1 from authz.nexloop_browser_token_realms where token_digest=p_digest)
  or not exists(select 1 from authz.nexloop_service_credentials t where t.token_digest=p_digest and t.tenant_id=v_tenant
     and t.binding->>'subject_kind'='service' and t.status='active') then
  raise exception 'claim extraction unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 select * into v_conversation from runtime.nexloop_conversations where tenant_id=v_tenant and world=p_world and conversation_id=c->>'conversation_id' for share;
 if not found then raise exception 'claim conversation unavailable' using errcode='42501';end if;
 for v_field in select unnest(array['','/consumer_id','/owner_principal']) loop
  perform authz.nexloop_assert_claim_source_read(p_digest,p_world,a,case when v_field='' then 'eios:object:' else 'eios:property:' end||'Conversation/'||v_conversation.conversation_id||v_field);
 end loop;
 if not exists(select 1 from ontology.objects o where o.tenant_id=v_tenant and o.world=p_world and o.type_name='Conversation' and o.object_id=v_conversation.conversation_id
   and o.properties=jsonb_build_object('consumer_id',v_conversation.consumer_id,'owner_principal',v_conversation.principal_id)) then
  raise exception 'claim conversation unavailable' using errcode='42501';end if;
 -- Every cited Message: current per-field READ, same conversation, receipt order, exact content hash.
 for m in select value from jsonb_array_elements(c->'messages') loop
  if jsonb_typeof(m->'sequence') is distinct from 'number' or m->>'sequence'!~'^[1-9][0-9]{0,17}$' or m->>'message_id'!~'^[0-9a-f]{64}$'
   or m->>'content_hash'!~'^[0-9a-f]{64}$' or (m->>'sequence')::bigint<=v_previous then
   raise exception 'claim source window invalid' using errcode='42501';end if;
  v_previous:=(m->>'sequence')::bigint;
  for v_field in select unnest(array['','/accepted_at','/actor','/body','/conversation_id','/sequence']) loop
   perform authz.nexloop_assert_claim_source_read(p_digest,p_world,a,case when v_field='' then 'eios:object:' else 'eios:property:' end||'Message/'||(m->>'message_id')||v_field);
  end loop;
  select * into v_row from runtime.nexloop_conversation_messages where tenant_id=v_tenant and world=p_world and conversation_id=v_conversation.conversation_id
   and message_id=m->>'message_id' for share;
  if not found or v_row.sequence<>(m->>'sequence')::bigint
   or encode(sha256(convert_to(v_row.record->>'body','UTF8')),'hex') is distinct from m->>'content_hash' then
   raise exception 'claim source evidence mismatch' using errcode='42501';end if;
  select * into v_obj from ontology.objects where tenant_id=v_tenant and world=p_world and type_name='Message' and object_id=v_row.message_id for share;
  if not found or v_obj.properties->>'body' is distinct from v_row.record->>'body' or v_obj.properties->>'actor' is distinct from v_row.record->>'actor'
   or v_obj.properties->>'conversation_id' is distinct from v_conversation.conversation_id then
   raise exception 'claim source evidence mismatch' using errcode='42501';end if;
  v_speaker:=case when v_row.record->>'actor'=v_conversation.principal_id then 'consumer' else 'agent' end;
  if m->>'speaker' is distinct from v_speaker then raise exception 'claim speaker mismatch' using errcode='42501';end if;
  v_messages:=v_messages||jsonb_build_object(v_row.message_id,jsonb_build_object('sequence',v_row.sequence,'speaker',v_speaker,'body',v_row.record->>'body','hash',m->>'content_hash'));
 end loop;
 perform pg_advisory_xact_lock(hashtextextended(v_tenant||':'||p_world||':claim-extraction:'||(c->>'input_digest'),19));
 select * into v_existing from ontology.nexloop_extraction_runs where tenant_id=v_tenant and world=p_world and input_digest=c->>'input_digest';
 if found then
  -- Same input version: idempotent replay, nothing new is written.
  if v_existing.conversation_id is distinct from v_conversation.conversation_id or v_existing.extractor_version is distinct from c->>'extractor_version' then
   raise exception 'claim_extraction_conflict' using errcode='P0001';end if;
  perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
  return jsonb_build_object('replay',true,'input_digest',v_existing.input_digest,
   'claim_ids',(select coalesce(jsonb_agg(r.claim_id order by r.claim_id),'[]'::jsonb) from ontology.nexloop_extraction_run_claims r
     where r.tenant_id=v_tenant and r.world=p_world and r.input_digest=v_existing.input_digest));
 end if;
 if length(c->>'extractor_version') not between 1 and 80 or length(c->>'prompt_version') not between 1 and 80 or length(c->>'provider') not between 1 and 40
  or length(c->>'model_id') not between 1 and 80 or length(c->>'timezone') not between 1 and 64 then
  raise exception 'claim extraction unavailable' using errcode='42501';end if;
 insert into ontology.nexloop_extraction_runs values(v_tenant,p_world,c->>'input_digest',v_conversation.conversation_id,v_conversation.consumer_id,a->>'principal_id',
  c->>'extractor_version',c->>'prompt_version',c->>'provider',c->>'model_id',c->>'timezone',c->'messages',c->'rejected',default);
 for t in select value from jsonb_array_elements(c->'topics') loop
  if t->>'topic_key'!~'^[0-9a-f]{64}$' or t->'user_valid_reply' is distinct from 'true'::jsonb or jsonb_typeof(t->'message_ids') is distinct from 'array'
   or length(t->>'topic') not between 1 and 200 or length(t->>'conversation_summary') not between 1 and 2000
   or (t->>'first_sequence')::bigint>(t->>'last_sequence')::bigint
   or not exists(select 1 from jsonb_each(v_messages) e where (e.value->>'sequence')::bigint between (t->>'first_sequence')::bigint and (t->>'last_sequence')::bigint and e.value->>'speaker'='consumer')
   or exists(select 1 from jsonb_array_elements_text(t->'message_ids') x where not v_messages ? x.value) then
   raise exception 'claim topic invalid' using errcode='42501';end if;
  insert into ontology.nexloop_conversation_topics values(v_tenant,p_world,c->>'input_digest',t->>'topic_key',v_conversation.conversation_id,t->>'topic',
   t->>'conversation_summary',true,(t->>'first_sequence')::bigint,(t->>'last_sequence')::bigint,t->'message_ids');
  v_topics:=v_topics||jsonb_build_object(t->>'topic_key',jsonb_build_array((t->>'first_sequence')::bigint,(t->>'last_sequence')::bigint));
 end loop;
 -- S-DUP: a Claim another Claim of this run cites (derivation, correction) is always written as it is.
 select coalesce(jsonb_object_agg(r.x,true),'{}'::jsonb) into v_refs from (
   select jsonb_array_elements_text(value->'derived_from') x from jsonb_array_elements(c->'claims')
   union select value->>'corrects_claim_id' from jsonb_array_elements(c->'claims') where jsonb_typeof(value->'corrects_claim_id')='string') r;
 for cl in select value from jsonb_array_elements(c->'claims') loop
  v_kind:=cl->>'epistemic_kind';
  if v_kind='verified_fact' then raise exception 'extractor cannot produce verified_fact' using errcode='42501';end if;
  if cl->>'claim_id'!~'^[0-9a-f]{64}$' or cl->>'claim_id' is null or cl->>'extractor_version' is distinct from c->>'extractor_version'
   or cl->>'resolution_state' is distinct from (case when v_kind='hypothesis' then 'hypothesis_only' else 'unresolved' end)
   or not v_topics ? (cl->>'topic_key') or jsonb_typeof(cl->'confidence') is distinct from 'number'
   or jsonb_typeof(cl->'derived_from') is distinct from 'array' or jsonb_typeof(cl->'guard_flags') is distinct from 'array'
   or (cl->>'subject_kind'='consumer' and cl->>'subject_ref' is distinct from v_conversation.consumer_id)
   or (cl->>'subject_kind'<>'consumer' and cl->>'subject_ref' is distinct from '') then
   raise exception 'claim invalid' using errcode='42501';end if;
  src:=null;v_start:=null;v_end:=null;
  if jsonb_typeof(cl->'source_message_id')='string' then
   src:=v_messages->(cl->>'source_message_id');
   if src is null then raise exception 'claim source outside window' using errcode='42501';end if;
   v_body:=src->>'body';v_start:=(cl->>'span_start')::integer;v_end:=(cl->>'span_end')::integer;
   -- Server-side evidence check: span must reproduce the cited quote exactly.
   if v_start is null or v_end is null or v_start<0 or v_end<=v_start or v_end>length(v_body)
    or substr(v_body,v_start+1,v_end-v_start) is distinct from cl->>'quote'
    or cl->>'source_content_hash' is distinct from src->>'hash' or (cl->>'source_sequence')::bigint is distinct from (src->>'sequence')::bigint
    or cl->>'speaker' is distinct from src->>'speaker'
    or (src->>'sequence')::bigint not between (v_topics->(cl->>'topic_key')->>0)::bigint and (v_topics->(cl->>'topic_key')->>1)::bigint then
    raise exception 'claim evidence span mismatch' using errcode='42501';end if;
  elsif v_kind<>'hypothesis' then raise exception 'explicit claim requires Message evidence' using errcode='42501';
  else
   -- Derived hypotheses cite only explicit Claims recorded in this same run.
   if jsonb_array_length(cl->'derived_from')=0 or cl->>'quote'<>'' or exists(select 1 from jsonb_array_elements_text(cl->'derived_from') d
      where v_claims->>d.value is distinct from 'explicit') then
    raise exception 'hypothesis derivation invalid' using errcode='42501';end if;
  end if;
  if (cl->>'speaker'='agent' and v_kind not in ('commitment','hypothesis')) or (cl->>'speaker'='consumer' and v_kind='commitment') then
   raise exception 'claim speaker kind invalid' using errcode='42501';end if;
  -- S-DUP: the same statement again (same Message span, kind, polarity, modality, condition presence and normalized value, still current)
  -- is not written a second time; this run links the Claim already recorded.
  v_dup:=null;
  if src is not null and not v_refs ? (cl->>'claim_id') then
   select x.claim_id into v_dup from ontology.nexloop_claims x where x.tenant_id=v_tenant and x.world=p_world and x.source_message_id=cl->>'source_message_id'
     and x.span_start=v_start and x.span_end=v_end and x.epistemic_kind=v_kind and x.polarity=cl->>'polarity' and x.modality=cl->>'modality'
     and (x.condition_text<>'')=(coalesce(cl->>'condition','')<>'') and x.claim_id<>cl->>'claim_id'
     and ontology.nexloop_claim_value_key(x.value)=ontology.nexloop_claim_value_key(cl->'value')
     and x.superseded_at is null and x.resolution_state<>'superseded'
    order by x.recorded_at,x.claim_id limit 1;
  end if;
  if v_dup is null then
  insert into ontology.nexloop_claims values(v_tenant,p_world,cl->>'claim_id',v_conversation.conversation_id,v_conversation.consumer_id,c->>'input_digest',cl->>'topic_key',
   cl->>'subject_kind',cl->>'subject_ref',cl->>'subject_text',cl->>'predicate',cl->'value',cl->>'speaker',cl->>'polarity',cl->>'modality',cl->>'condition',
   cl->>'time_expression',cl->'valid_time',cl->>'source_message_id',(cl->>'source_sequence')::bigint,v_start,v_end,cl->>'source_content_hash',cl->>'quote',
   cl->>'extractor_version',(cl->>'confidence')::numeric,v_kind,cl->>'resolution_state',cl->'derived_from',cl->>'correlation_key',cl->'guard_flags',default)
   on conflict(tenant_id,world,claim_id) do nothing;
  end if;
  insert into ontology.nexloop_extraction_run_claims values(v_tenant,p_world,c->>'input_digest',coalesce(v_dup,cl->>'claim_id')) on conflict do nothing;
  v_claims:=v_claims||jsonb_build_object(coalesce(v_dup,cl->>'claim_id'),case when v_kind='hypothesis' then 'hypothesis' else 'explicit' end);
 end loop;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 return jsonb_build_object('replay',false,'input_digest',c->>'input_digest',
  'claim_ids',(select coalesce(jsonb_agg(x order by x),'[]'::jsonb) from jsonb_object_keys(v_claims) x));
end $$;

alter function authz.nexloop_record_claim_extraction(text,text,text,text,text) rename to nexloop_record_claim_extraction_v0115;
revoke all on function authz.nexloop_record_claim_extraction_v0115(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;

create function authz.nexloop_record_claim_extraction(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare c jsonb:=p_payload::jsonb;cl jsonb;v_seen jsonb:='{}'::jsonb;v_result jsonb;v_target jsonb;v_rank jsonb:='{}'::jsonb;
 prior text:=current_setting('eios.tenant_id',true);v_tenant text:=(p_text::jsonb)->>'tenant_id';v_trusted boolean;v_context jsonb;v_old record;
begin
 if jsonb_typeof(c->'claims') is distinct from 'array' or jsonb_typeof(c->'messages') is distinct from 'array' then
  raise exception 'claim extraction unavailable' using errcode='42501';end if;
 -- Order evidence only; authority and every window/evidence check stay in the 0066 recorder below (same transaction).
 perform set_config('eios.tenant_id',coalesce(v_tenant,''),true);
 select count(*)>0 and count(*)=count(f.message_id) and bool_and(f.trust='signed' and not f.skewed and f.provider_sequence is not null)
   and count(distinct f.provider_namespace)=1 and count(distinct f.provider_sequence)=count(*)
  into v_trusted
  from jsonb_array_elements(c->'messages') w
  left join runtime.nexloop_message_provider_facts f on f.tenant_id=v_tenant and f.world=p_world and f.message_id=w.value->>'message_id';
 select coalesce(jsonb_object_agg(w.value->>'sequence',case when v_trusted then f.provider_sequence else (w.value->>'sequence')::bigint end),'{}'::jsonb) into v_rank
  from jsonb_array_elements(c->'messages') w
  left join runtime.nexloop_message_provider_facts f on f.tenant_id=v_tenant and f.world=p_world and f.message_id=w.value->>'message_id';
 -- S-DUP: context Messages are shown to the model only; no Claim may cite one.
 select coalesce(jsonb_object_agg(w.value->>'message_id',true),'{}'::jsonb) into v_context from jsonb_array_elements(c->'messages') w where w.value->'context'='true'::jsonb;
 if exists(select 1 from jsonb_array_elements(c->'claims') x where jsonb_typeof(x.value->'source_message_id')='string' and v_context ? (x.value->>'source_message_id')) then
  perform set_config('eios.tenant_id',coalesce(prior,''),true);
  raise exception 'claim source is context' using errcode='42501';end if;
 -- A correction may only point at an explicit Claim recorded earlier in the same run, from a Message that is the
 -- same or earlier in the effective order.
 for cl in select value from jsonb_array_elements(c->'claims') loop
  if jsonb_typeof(cl->'corrects_claim_id')='string' then
   v_target:=v_seen->(cl->>'corrects_claim_id');
   -- S-DUP: or at a current Claim recorded earlier for this Conversation (outside the window): explicit, not erased or superseded,
   -- from an earlier Message (receipt order: the earlier Message is not in this window's effective order).
   if v_target is null then
    select x.epistemic_kind,x.source_sequence,x.conversation_id,x.value->>'type' vtype,x.superseded_at,x.resolution_state into v_old
     from ontology.nexloop_claims x where x.tenant_id=v_tenant and x.world=p_world and x.claim_id=cl->>'corrects_claim_id';
    if found and v_old.conversation_id=c->>'conversation_id' and v_old.epistemic_kind<>'hypothesis' and v_old.vtype<>'erased'
     and v_old.superseded_at is null and v_old.resolution_state<>'superseded' and v_old.source_sequence is not null
     and cl->>'source_sequence' is not null and v_old.source_sequence<(cl->>'source_sequence')::bigint and cl->>'epistemic_kind'='correction' then
     v_seen:=v_seen||jsonb_build_object(cl->>'claim_id',jsonb_build_object('kind',cl->>'epistemic_kind','sequence',cl->'source_sequence'));
     continue;
    end if;
   end if;
   if cl->>'epistemic_kind' is distinct from 'correction' or v_target is null or v_target->>'kind'='hypothesis'
    or cl->>'source_sequence' is null or v_target->>'sequence' is null
    or not v_rank ? (v_target->>'sequence') or not v_rank ? (cl->>'source_sequence')
    or (v_rank->>(v_target->>'sequence'))::bigint>(v_rank->>(cl->>'source_sequence'))::bigint then
    raise exception 'claim correction target invalid' using errcode='42501';end if;
  elsif cl ? 'corrects_claim_id' and jsonb_typeof(cl->'corrects_claim_id') is distinct from 'null' then
   raise exception 'claim correction target invalid' using errcode='42501';end if;
  v_seen:=v_seen||jsonb_build_object(cl->>'claim_id',jsonb_build_object('kind',cl->>'epistemic_kind','sequence',cl->'source_sequence'));
 end loop;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 v_result:=authz.nexloop_record_claim_extraction_v0066(p_digest,p_world,p_text,p_signature,p_payload);
 if v_result->'replay'='true'::jsonb then return v_result;end if;
 -- Same transaction; RLS tenant context was set by the verified authority above.
 for cl in select value from jsonb_array_elements(c->'claims') where jsonb_typeof(value->'corrects_claim_id')='string' loop
  update ontology.nexloop_claims set corrects_claim_id=cl->>'corrects_claim_id'
   where tenant_id=current_setting('eios.tenant_id',true) and world=p_world and claim_id=cl->>'claim_id' and corrects_claim_id is null;
 end loop;
 return v_result;
end $$;

alter function authz.nexloop_record_claim_extraction(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_record_claim_extraction(text,text,text,text,text) from public,nexloop_action_worker,nexloop_scheduler,nexloop_identity;
grant execute on function authz.nexloop_record_claim_extraction(text,text,text,text,text) to nexloop_api,nexloop_domain_worker;
