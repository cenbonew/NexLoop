-- Unpublished NX018 v3 history draft; published 0058 remains immutable.

create function authz.nexloop_read_assessment_history(p_digest text,p_world text,p_text text,p_signature text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;k bytea;row ontology.nexloop_assessment_revisions%rowtype;p jsonb;proof jsonb;field text;target text;m ontology.objects%rowtype;item jsonb;formal boolean;prefix text;
begin
 if octet_length(p_text)>1048576 or a->>'protocol' is distinct from 'nexloop-assessment-history-v1' or a->>'object_id' !~ '^[a-f0-9]{64}$'
  or a->>'resource_id' is distinct from 'eios:object:RelationshipAssessment/'||(a->>'object_id') or a->>'target_resource' is distinct from a->>'resource_id'
  or a->>'expires_at' is null or a->>'known_at' is null or a->>'valid_at' is null
  or a->>'known_at' !~ '(Z|[+-][0-9]{2}:[0-9]{2})$' or a->>'valid_at' !~ '(Z|[+-][0-9]{2}:[0-9]{2})$'
  or jsonb_typeof(a->'property_authorities') is distinct from 'array' or jsonb_array_length(a->'property_authorities')<>14
 then raise exception 'assessment history command rejected' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-assessment-history-v1:'||p_text,'UTF8'),k,'sha256'),'hex') then raise exception 'assessment history signature rejected' using errcode='42501';end if;
 perform authz.nexloop_assert_read_authority(p_digest,p_world,a);
 for field in select unnest(array['relation_name','relation_version','source_type','source_id','target_type','target_id','conclusion','epistemic_kind','resolution_state','valid_from','valid_to','evidence_message_id','evidence_content_hash','corrects_revision']) loop
  select value into proof from jsonb_array_elements(a->'property_authorities') where value->>'target_resource'='eios:property:RelationshipAssessment/'||(a->>'object_id')||'/'||field;
  if not found or proof->>'expires_at' is null then raise exception 'assessment history field authority missing' using errcode='42501';end if;
  perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
 end loop;
 perform 1 from ontology.objects where tenant_id=a->>'tenant_id' and world=p_world and type_name='RelationshipAssessment' and object_id=a->>'object_id' for share;
 if not found then raise exception 'assessment history object unavailable' using errcode='42501';end if;
 select * into row from ontology.nexloop_assessment_revisions where tenant_id=a->>'tenant_id' and world=p_world and assessment_id=a->>'object_id'
  and recorded_at<=(a->>'known_at')::timestamptz order by recorded_at desc,revision desc limit 1 for share;
 if not found then
  perform authz.nexloop_assert_read_authority(p_digest,p_world,a);
  for proof in select value from jsonb_array_elements(a->'property_authorities') union all select value from jsonb_array_elements(a->'assessment_authorities') loop
   if proof->>'expires_at' is null then raise exception 'empty history proof expiry missing' using errcode='42501';end if;
   perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
  end loop;
  if (a->>'expires_at')::timestamptz<=clock_timestamp()
   or exists(select 1 from jsonb_array_elements(a->'property_authorities') q where q->>'expires_at' is null or (q->>'expires_at')::timestamptz<=clock_timestamp())
   or exists(select 1 from jsonb_array_elements(a->'assessment_authorities') q where q->>'expires_at' is null or (q->>'expires_at')::timestamptz<=clock_timestamp())
   or not exists(select 1 from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active and key_material=k)
  then raise exception 'empty history expired at return' using errcode='42501';end if;
  return jsonb_build_object('formal','[]'::jsonb,'evidence','[]'::jsonb);
 end if;
 p:=row.new_properties;
 perform authz.nexloop_assert_assessment_relation(p_digest,p_world,a,p);
 for prefix in select unnest(array['source','target']) loop
  target:='eios:object:'||(p->>(prefix||'_type'))||'/'||(p->>(prefix||'_id'));
  select value into proof from jsonb_array_elements(a->'assessment_authorities') where value->>'target_resource'=target and value->>'operation'='read';
  if not found or proof->>'expires_at' is null then raise exception 'history endpoint authority missing' using errcode='42501';end if;
  perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
  perform 1 from ontology.objects where tenant_id=a->>'tenant_id' and world=p_world and type_name=p->>(prefix||'_type') and object_id=p->>(prefix||'_id') for share;
  if not found then raise exception 'history endpoint unavailable' using errcode='42501';end if;
 end loop;
 if p->>'epistemic_kind'='user_statement' then
  for field in select unnest(array['','/actor','/body']) loop
   target:=case when field='' then 'eios:object:' else 'eios:property:' end||'Message/'||(p->>'evidence_message_id')||field;
   select value into proof from jsonb_array_elements(a->'assessment_authorities') where value->>'target_resource'=target and value->>'operation'='read';
   if not found or proof->>'expires_at' is null then raise exception 'history source authority missing' using errcode='42501';end if;
   perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
  end loop;
  select * into m from ontology.objects where tenant_id=a->>'tenant_id' and world=p_world and type_name='Message' and object_id=p->>'evidence_message_id' for share;
  if not found or m.properties->>'body' is distinct from p->>'conclusion' or encode(sha256(convert_to(m.properties->>'body','UTF8')),'hex') is distinct from p->>'evidence_content_hash'
   or not exists(select 1 from runtime.nexloop_conversation_messages cm where cm.tenant_id=m.tenant_id and cm.world=m.world and cm.message_id=m.object_id and cm.record->>'body'=m.properties->>'body' and cm.record->>'actor'=m.properties->>'actor')
   or not exists(select 1 from control.nexloop_browser_memberships b join control.nexloop_browser_subjects s on s.subject_id=b.subject_id where b.tenant_id=m.tenant_id and b.principal_id=m.properties->>'actor' and s.payload->>'kind'='human') then raise exception 'history source provenance unavailable' using errcode='42501';end if;
 end if;
 -- No stale permission or expiry is accepted after history/source/definition locks.
 perform authz.nexloop_assert_read_authority(p_digest,p_world,a);
 for proof in select value from jsonb_array_elements(a->'property_authorities') union all select value from jsonb_array_elements(a->'assessment_authorities') loop
  if proof->>'expires_at' is null then raise exception 'history read proof expiry missing' using errcode='42501';end if;
  perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
 end loop;
 perform authz.nexloop_assert_assessment_relation(p_digest,p_world,a,p);
 if a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp()
  or exists(select 1 from jsonb_array_elements(a->'property_authorities') q where q->>'expires_at' is null or (q->>'expires_at')::timestamptz<=clock_timestamp())
  or exists(select 1 from jsonb_array_elements(a->'assessment_authorities') q where q->>'expires_at' is null or (q->>'expires_at')::timestamptz<=clock_timestamp())
  or not exists(select 1 from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active and key_material=k) then raise exception 'history read expired at return' using errcode='42501';end if;
 formal:=(p->>'relation_version')::integer>0 and p->>'epistemic_kind'='user_statement' and p->>'resolution_state'='resolved'
  and (p->>'valid_from')::timestamptz<=(a->>'valid_at')::timestamptz and (p->>'valid_to'='' or (a->>'valid_at')::timestamptz<(p->>'valid_to')::timestamptz);
 item:=jsonb_build_object('assessment_id',row.assessment_id,'revision',row.revision,'recorded_at',row.recorded_at,'epistemic_kind',p->>'epistemic_kind','resolution_state',p->>'resolution_state','properties',p);
 return jsonb_build_object('formal',case when formal then jsonb_build_array(item) else '[]'::jsonb end,'evidence',case when formal then '[]'::jsonb else jsonb_build_array(item) end);
end $$;
alter function authz.nexloop_read_assessment_history(text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_read_assessment_history(text,text,text,text) from public;
grant execute on function authz.nexloop_read_assessment_history(text,text,text,text) to nexloop_api,nexloop_domain_worker,nexloop_action_worker;
