-- NX-019 evidence-bound conversation Claims (candidate knowledge, never formal objects).
-- Claims cite governed Message objects by id + character span + content hash. Only a
-- current service credential with Claim-extraction EXECUTE and per-field Message READ
-- may record them, through this definer; restricted roles have no table privileges.
create table ontology.nexloop_extraction_runs (
 tenant_id text not null,world text not null,input_digest text not null check(input_digest~'^[0-9a-f]{64}$'),
 conversation_id text not null,consumer_id text not null,principal_id text not null,
 extractor_version text not null,prompt_version text not null,provider text not null,model_id text not null,timezone text not null,
 message_refs jsonb not null check(jsonb_typeof(message_refs)='array'),rejected jsonb not null check(jsonb_typeof(rejected)='array'),
 recorded_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,input_digest),
 foreign key(tenant_id,world,conversation_id) references runtime.nexloop_conversations
);
create table ontology.nexloop_conversation_topics (
 tenant_id text not null,world text not null,input_digest text not null,topic_key text not null check(topic_key~'^[0-9a-f]{64}$'),
 conversation_id text not null,topic text not null,conversation_summary text not null,user_valid_reply boolean not null check(user_valid_reply),
 first_sequence bigint not null,last_sequence bigint not null,message_ids jsonb not null check(jsonb_typeof(message_ids)='array'),
 primary key(tenant_id,world,input_digest,topic_key),check(0<first_sequence and first_sequence<=last_sequence),
 foreign key(tenant_id,world,input_digest) references ontology.nexloop_extraction_runs
);
create table ontology.nexloop_claims (
 tenant_id text not null,world text not null,claim_id text not null check(claim_id~'^[0-9a-f]{64}$'),
 conversation_id text not null,consumer_id text not null,first_input_digest text not null,topic_key text not null,
 subject_kind text not null check(subject_kind in ('consumer','enterprise','entity')),subject_ref text not null,subject_text text not null,
 predicate text not null check(length(predicate) between 1 and 120),value jsonb not null check(jsonb_typeof(value)='object' and value ? 'type' and value ? 'value'),
 speaker text not null check(speaker in ('consumer','agent')),polarity text not null check(polarity in ('affirmed','negated')),
 modality text not null check(modality in ('asserted','conditional','tentative','requested')),condition_text text not null,
 time_expression text not null,valid_time jsonb not null check(jsonb_typeof(valid_time)='object'),
 source_message_id text,source_sequence bigint,span_start integer,span_end integer,source_content_hash text,quote text not null,
 extractor_version text not null,confidence numeric(4,3) not null check(confidence between 0 and 1),
 -- docs/04 §4 kinds; verified_fact is deliberately absent: extractors can never produce it.
 epistemic_kind text not null check(epistemic_kind in ('user_statement','preference','constraint','intent','need_problem','commitment','hypothesis','correction')),
 resolution_state text not null check(resolution_state in ('unresolved','hypothesis_only','needs_resolution','awaiting_definition','rejected_definition','resolved','superseded')),
 derived_from jsonb not null check(jsonb_typeof(derived_from)='array'),correlation_key text not null check(correlation_key~'^[0-9a-f]{64}$'),
 guard_flags jsonb not null check(jsonb_typeof(guard_flags)='array'),recorded_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,claim_id),
 check((source_message_id is null)=(span_start is null) and (span_start is null)=(span_end is null) and (span_end is null)=(source_content_hash is null) and (source_content_hash is null)=(source_sequence is null)),
 check(span_start is null or (0<=span_start and span_start<span_end)),
 check(epistemic_kind='hypothesis' or (source_message_id is not null and jsonb_array_length(derived_from)=0)),
 check(epistemic_kind<>'hypothesis' or source_message_id is not null or jsonb_array_length(derived_from)>0),
 check(modality<>'conditional' or length(condition_text)>0),
 foreign key(tenant_id,world,conversation_id) references runtime.nexloop_conversations
);
create index nexloop_claims_conversation on ontology.nexloop_claims(tenant_id,world,conversation_id,source_sequence);
create index nexloop_claims_correlation on ontology.nexloop_claims(tenant_id,world,consumer_id,correlation_key);
create table ontology.nexloop_extraction_run_claims (
 tenant_id text not null,world text not null,input_digest text not null,claim_id text not null,
 primary key(tenant_id,world,input_digest,claim_id),
 foreign key(tenant_id,world,input_digest) references ontology.nexloop_extraction_runs,
 foreign key(tenant_id,world,claim_id) references ontology.nexloop_claims
);
alter table ontology.nexloop_extraction_runs owner to nexloop_owner;
alter table ontology.nexloop_conversation_topics owner to nexloop_owner;
alter table ontology.nexloop_claims owner to nexloop_owner;
alter table ontology.nexloop_extraction_run_claims owner to nexloop_owner;
alter table ontology.nexloop_extraction_runs enable row level security;
alter table ontology.nexloop_extraction_runs force row level security;
alter table ontology.nexloop_conversation_topics enable row level security;
alter table ontology.nexloop_conversation_topics force row level security;
alter table ontology.nexloop_claims enable row level security;
alter table ontology.nexloop_claims force row level security;
alter table ontology.nexloop_extraction_run_claims enable row level security;
alter table ontology.nexloop_extraction_run_claims force row level security;
create policy extraction_run_tenant on ontology.nexloop_extraction_runs to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
create policy conversation_topic_tenant on ontology.nexloop_conversation_topics to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
create policy claim_tenant on ontology.nexloop_claims to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
create policy extraction_run_claim_tenant on ontology.nexloop_extraction_run_claims to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on ontology.nexloop_extraction_runs,ontology.nexloop_conversation_topics,ontology.nexloop_claims,ontology.nexloop_extraction_run_claims
 from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;

-- Signed per-target READ proof must be present, bound to this principal and current.
create function authz.nexloop_assert_claim_source_read(p_digest text,p_world text,p_claims jsonb,p_target text)
 returns void language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare proof jsonb;
begin
 select value into proof from jsonb_array_elements(p_claims->'source_authorities') where value->>'target_resource'=p_target and value->>'operation'='read';
 if not found or proof->>'tenant_id' is distinct from p_claims->>'tenant_id' or proof->>'principal_id' is distinct from p_claims->>'principal_id'
  or proof->>'credential_id' is distinct from p_claims->>'credential_id'
  or proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp() then
  raise exception 'claim source READ authority missing' using errcode='42501';end if;
 perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
end $$;
alter function authz.nexloop_assert_claim_source_read(text,text,jsonb,text) owner to nexloop_owner;
revoke all on function authz.nexloop_assert_claim_source_read(text,text,jsonb,text) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;

create function authz.nexloop_record_claim_extraction(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;v_tenant text:=a->>'tenant_id';
 v_conversation runtime.nexloop_conversations%rowtype;v_row runtime.nexloop_conversation_messages%rowtype;v_obj ontology.objects%rowtype;
 v_existing ontology.nexloop_extraction_runs%rowtype;m jsonb;t jsonb;cl jsonb;src jsonb;v_field text;v_body text;v_speaker text;
 v_previous bigint:=0;v_messages jsonb:='{}'::jsonb;v_topics jsonb:='{}'::jsonb;v_claims jsonb:='{}'::jsonb;v_kind text;v_ids jsonb;v_ref text;
 v_start integer;v_end integer;
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
  insert into ontology.nexloop_claims values(v_tenant,p_world,cl->>'claim_id',v_conversation.conversation_id,v_conversation.consumer_id,c->>'input_digest',cl->>'topic_key',
   cl->>'subject_kind',cl->>'subject_ref',cl->>'subject_text',cl->>'predicate',cl->'value',cl->>'speaker',cl->>'polarity',cl->>'modality',cl->>'condition',
   cl->>'time_expression',cl->'valid_time',cl->>'source_message_id',(cl->>'source_sequence')::bigint,v_start,v_end,cl->>'source_content_hash',cl->>'quote',
   cl->>'extractor_version',(cl->>'confidence')::numeric,v_kind,cl->>'resolution_state',cl->'derived_from',cl->>'correlation_key',cl->'guard_flags',default)
   on conflict(tenant_id,world,claim_id) do nothing;
  insert into ontology.nexloop_extraction_run_claims values(v_tenant,p_world,c->>'input_digest',cl->>'claim_id') on conflict do nothing;
  v_claims:=v_claims||jsonb_build_object(cl->>'claim_id',case when v_kind='hypothesis' then 'hypothesis' else 'explicit' end);
 end loop;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 return jsonb_build_object('replay',false,'input_digest',c->>'input_digest',
  'claim_ids',(select coalesce(jsonb_agg(x order by x),'[]'::jsonb) from jsonb_object_keys(v_claims) x));
end $$;
alter function authz.nexloop_record_claim_extraction(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_record_claim_extraction(text,text,text,text,text) from public,nexloop_action_worker,nexloop_scheduler,nexloop_identity;
grant execute on function authz.nexloop_record_claim_extraction(text,text,text,text,text) to nexloop_api,nexloop_domain_worker;

-- Evidence view for one Conversation under current Conversation READ. Statements and
-- hypotheses are returned separately; there is deliberately no formal-property section.
create function authz.nexloop_read_conversation_claims(p_digest text,p_world text,p_text text,p_signature text)
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
  'statements',(select coalesce(jsonb_agg(to_jsonb(x)-'tenant_id' order by x.source_sequence,x.span_start,x.claim_id),'[]'::jsonb) from ontology.nexloop_claims x
   where x.tenant_id=v_tenant and x.world=p_world and x.conversation_id=v_id and x.epistemic_kind<>'hypothesis'
    and (a->>'input_digest' is null or exists(select 1 from ontology.nexloop_extraction_run_claims rc where rc.tenant_id=x.tenant_id and rc.world=x.world and rc.claim_id=x.claim_id and rc.input_digest=a->>'input_digest'))),
  'hypotheses',(select coalesce(jsonb_agg(to_jsonb(x)-'tenant_id' order by x.recorded_at,x.claim_id),'[]'::jsonb) from ontology.nexloop_claims x
   where x.tenant_id=v_tenant and x.world=p_world and x.conversation_id=v_id and x.epistemic_kind='hypothesis'
    and (a->>'input_digest' is null or exists(select 1 from ontology.nexloop_extraction_run_claims rc where rc.tenant_id=x.tenant_id and rc.world=x.world and rc.claim_id=x.claim_id and rc.input_digest=a->>'input_digest')))
 ) into v_result;
 perform authz.nexloop_assert_read_authority(p_digest,p_world,a);
 return v_result;
end $$;
alter function authz.nexloop_read_conversation_claims(text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_read_conversation_claims(text,text,text,text) from public,nexloop_action_worker,nexloop_scheduler,nexloop_identity;
grant execute on function authz.nexloop_read_conversation_claims(text,text,text,text) to nexloop_api,nexloop_domain_worker;
