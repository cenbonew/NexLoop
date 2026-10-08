-- NX-018 governed relationship assessments; existing migration checksums preserved.
create table ontology.nexloop_assessment_revisions (
 tenant_id text not null,world text not null,assessment_id text not null,revision bigint not null,
 prior_properties jsonb,new_properties jsonb not null,recorded_at timestamptz not null default clock_timestamp(),
 principal_id text not null,action_resource text not null,intent_id text not null,transaction_id bigint not null,
 primary key(tenant_id,world,assessment_id,revision));
alter table ontology.nexloop_assessment_revisions owner to nexloop_owner;
alter table ontology.nexloop_assessment_revisions enable row level security;
alter table ontology.nexloop_assessment_revisions force row level security;
create policy assessment_revision_tenant on ontology.nexloop_assessment_revisions to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on ontology.nexloop_assessment_revisions from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler;
create function authz.nexloop_assessment_audit_immutable() returns trigger language plpgsql set search_path=pg_catalog as $$
begin raise exception 'assessment audit immutable' using errcode='42501';end $$;
create trigger assessment_audit_immutable before update or delete on ontology.nexloop_assessment_revisions
 for each row execute function authz.nexloop_assessment_audit_immutable();
create function authz.nexloop_assessment_head_guard() returns trigger language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
begin
 if (TG_OP='DELETE' and old.type_name='RelationshipAssessment') then raise exception 'assessment deletion unsupported' using errcode='42501';end if;
 if TG_OP='DELETE' then return old;end if;
 if new.type_name='RelationshipAssessment' or (TG_OP='UPDATE' and old.type_name='RelationshipAssessment') then
  if TG_OP='UPDATE' and (new.tenant_id,new.world,new.type_name,new.object_id,new.schema_version) is distinct from (old.tenant_id,old.world,old.type_name,old.object_id,old.schema_version) then
    raise exception 'assessment binding immutable' using errcode='42501';end if;
  if not exists(select 1 from ontology.nexloop_assessment_revisions r where r.tenant_id=new.tenant_id and r.world=new.world
    and r.assessment_id=new.object_id and r.revision=new.nexloop_revision and r.transaction_id=txid_current()
    and r.new_properties=new.properties and ((TG_OP='INSERT' and r.prior_properties is null and new.nexloop_revision=1)
       or (TG_OP='UPDATE' and r.prior_properties=old.properties and new.nexloop_revision=old.nexloop_revision+1))) then
    raise exception 'assessment requires atomic revision provenance' using errcode='42501';end if;
 end if;
 return new;
end $$;
alter function authz.nexloop_assessment_head_guard() owner to nexloop_owner;
revoke all on function authz.nexloop_assessment_head_guard() from public;
create trigger assessment_head_guard before insert or update or delete on ontology.objects
 for each row execute function authz.nexloop_assessment_head_guard();


create function authz.nexloop_assert_assessment_relation(p_digest text,p_world text,a jsonb,p jsonb) returns void
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare s jsonb;proof jsonb;prefix text;
begin
 if jsonb_typeof(p->'relation_version') is distinct from 'number' or p->>'relation_version' !~ '^[0-9]+$' then raise exception 'assessment relation version rejected' using errcode='42501';end if;
 if (p->>'relation_version')::integer=0 then
  if p->>'epistemic_kind' is distinct from 'hypothesis' and p->>'resolution_state' is distinct from 'awaiting_definition' then raise exception 'unknown relation cannot be formal' using errcode='42501';end if;
  return;
 end if;
 select value into proof from jsonb_array_elements(a->'assessment_authorities') where value->>'target_resource'='eios:link_type:'||(p->>'relation_name') and value->>'operation'='read';
 if not found or proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp()
  or proof->>'tenant_id' is distinct from a->>'tenant_id' or proof->>'principal_id' is distinct from a->>'principal_id' then raise exception 'relation definition read authority missing' using errcode='42501';end if;
 perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
 select definition into s from ontology.relation_type_versions where tenant_id=a->>'tenant_id' and relation_name=p->>'relation_name' and version=(p->>'relation_version')::integer for share;
 if not found or jsonb_typeof(s) is distinct from 'object' or s->>'relation_name' is distinct from p->>'relation_name'
  or s->>'version' is distinct from p->>'relation_version' or s->>'source_type' is distinct from p->>'source_type'
  or s->>'target_type' is distinct from p->>'target_type' or s->>'cardinality' is null
  or s->>'cardinality' not in ('one_to_one','one_to_many','many_to_one','many_to_many') then raise exception 'registered relation definition unavailable' using errcode='42501';end if;
 for prefix in select unnest(array['source','target']) loop
  if not exists(select 1 from ontology.objects o join ontology.object_type_versions t on t.tenant_id=o.tenant_id and t.type_name=o.type_name and t.version=o.schema_version
   where o.tenant_id=a->>'tenant_id' and o.world=p_world and o.type_name=p->>(prefix||'_type') and o.object_id=p->>(prefix||'_id')
   and t.definition->>'type_name'=o.type_name and t.definition->>'version'=o.schema_version::text) then raise exception 'relation endpoint schema lineage unavailable' using errcode='42501';end if;
 end loop;
end $$;
alter function authz.nexloop_assert_assessment_relation(text,text,jsonb,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_assert_assessment_relation(text,text,jsonb,jsonb) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker;

create function authz.nexloop_assert_assessment_provenance(p_digest text,p_world text,a jsonb,p jsonb) returns void
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare prefix text;op text;target text;proof jsonb;m ontology.objects%rowtype;v_field text;
begin
 if jsonb_typeof(p) is distinct from 'object' or (select count(*) from jsonb_object_keys(p))<>14
  or exists(select 1 from unnest(array['relation_name','source_type','source_id','target_type','target_id','conclusion','epistemic_kind','resolution_state','valid_from','valid_to','evidence_message_id','evidence_content_hash']) f where jsonb_typeof(p->f) is distinct from 'string')
  or p->>'relation_name' !~ '^[A-Za-z][A-Za-z0-9_]*$'
  or jsonb_typeof(p->'relation_version') is distinct from 'number' or p->>'relation_version' !~ '^[0-9]+$'
  or p->>'corrects_revision' !~ '^[0-9]+$' then raise exception 'assessment typed contract rejected' using errcode='42501';end if;
 if p->>'epistemic_kind' not in ('hypothesis','user_statement') or p->>'resolution_state' not in ('resolved','awaiting_definition','unresolved')
  or p->>'epistemic_kind' is null or p->>'resolution_state' is null
  or jsonb_typeof(p->'corrects_revision') is distinct from 'number' or p->>'valid_from' !~ '(Z|[+-][0-9]{2}:[0-9]{2})$'
  or (p->>'valid_to'<>'' and (p->>'valid_to' !~ '(Z|[+-][0-9]{2}:[0-9]{2})$' or (p->>'valid_to')::timestamptz<=(p->>'valid_from')::timestamptz))
  or length(p->>'conclusion') not between 1 and 8192 then raise exception 'assessment semantics rejected' using errcode='42501';end if;
 perform authz.nexloop_assert_assessment_relation(p_digest,p_world,a,p);
 for prefix in select unnest(array['source','target']) loop
  if p->>(prefix||'_type') !~ '^[A-Za-z][A-Za-z0-9_]*$' or p->>(prefix||'_id') !~ '^[a-f0-9]{64}$' then raise exception 'assessment endpoint invalid' using errcode='42501';end if;
  target:='eios:object:'||(p->>(prefix||'_type'))||'/'||(p->>(prefix||'_id'));
  for op in select unnest(array['read','edit']) loop
   select value into proof from jsonb_array_elements(a->'assessment_authorities') where value->>'target_resource'=target and value->>'operation'=op;
   if not found or proof->>'tenant_id' is distinct from a->>'tenant_id' or proof->>'principal_id' is distinct from a->>'principal_id'
      or proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'assessment endpoint authority missing' using errcode='42501';end if;
   if op='read' then perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);else perform authz.nexloop_assert_edit_authority(p_digest,p_world,proof);end if;
  end loop;
  perform 1 from ontology.objects where tenant_id=a->>'tenant_id' and world=p_world and type_name=p->>(prefix||'_type') and object_id=p->>(prefix||'_id') for share;
  if not found then raise exception 'assessment endpoint unavailable' using errcode='42501';end if;
 end loop;
 if p->>'epistemic_kind'='user_statement' then
  for v_field in select unnest(array['','/actor','/body']) loop
   target:=case when v_field='' then 'eios:object:' else 'eios:property:' end||'Message/'||(p->>'evidence_message_id')||v_field;
   select value into proof from jsonb_array_elements(a->'assessment_authorities') where value->>'target_resource'=target and value->>'operation'='read';
   if not found or proof->>'tenant_id' is distinct from a->>'tenant_id' or proof->>'principal_id' is distinct from a->>'principal_id'
    or proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'assessment evidence authority missing' using errcode='42501';end if;
   perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
  end loop;
  select * into m from ontology.objects where tenant_id=a->>'tenant_id' and world=p_world and type_name='Message' and object_id=p->>'evidence_message_id' for share;
  if not found or encode(sha256(convert_to(m.properties->>'body','UTF8')),'hex') is distinct from p->>'evidence_content_hash'
    or m.properties->>'body' is distinct from p->>'conclusion'
    or not exists(select 1 from runtime.nexloop_conversation_messages cm where cm.tenant_id=m.tenant_id and cm.world=m.world and cm.message_id=m.object_id and cm.record->>'body'=m.properties->>'body' and cm.record->>'actor'=m.properties->>'actor')
    or not exists(select 1 from control.nexloop_browser_memberships b join control.nexloop_browser_subjects s on s.subject_id=b.subject_id where b.tenant_id=m.tenant_id and b.principal_id=m.properties->>'actor' and s.payload->>'kind'='human') then
    raise exception 'assessment human Message provenance unavailable' using errcode='42501';end if;
 elsif p->>'evidence_message_id' is distinct from '' or p->>'evidence_content_hash' is distinct from '' then
  raise exception 'hypothesis cannot assert human evidence' using errcode='42501';
 end if;
end $$;
alter function authz.nexloop_assert_assessment_provenance(text,text,jsonb,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_assert_assessment_provenance(text,text,jsonb,jsonb) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker;
create function authz.nexloop_create_assessment_action(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;body jsonb:=p_payload::jsonb;k bytea;stored runtime.nexloop_action_claims%rowtype;
 permit jsonb:=a->'permit';v_claim jsonb:=permit->'claim';ref jsonb:=v_claim->'binding'->'action_reference';d jsonb;s jsonb;v_id text;v_outcome jsonb;
begin
 if octet_length(p_text)>1048576 or octet_length(p_payload)>262144 then raise exception 'instance command too large' using errcode='22023';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if a->>'expires_at' is null or permit->>'expires_at' is null or v_claim->>'lease_expires_at' is null then raise exception 'assessment command expiry required' using errcode='42501';end if;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-assessment-create-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'protocol' is distinct from 'nexloop-assessment-create-v1'
  or a->>'resource_id' is distinct from 'eios:action:'||(ref->>'stable_name')||':'||(ref->>'version')
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds'
  or v_claim->'binding'->>'request_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'instance permit rejected' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 select definition into d from control.nexloop_action_definitions where tenant_id=a->>'tenant_id' and world=p_world and resource_id=a->>'resource_id' and active;
 if not found or d is distinct from a->'definition' or d->'governance'->>'approval_mode'<>'none'
  or d->'governance'->>'risk_level'<>'low' or jsonb_array_length(d->'governance'->'policy_refs')<>0
  or d->'capability_binding'->>'capability_name' <> 'ontology.relationship_assessment.create'
  or not (d->'governance'->'change_scope'->'target_systems' @> '["postgres"]'::jsonb)
  or jsonb_array_length(d->'governance'->'change_scope'->'properties')<>0
 then raise exception 'instance Action contract unavailable' using errcode='42501';end if;
 if not exists(select 1 from jsonb_array_elements(d->'governance'->'change_scope'->'object_types') r
  where r->>'tenant_id'=a->>'tenant_id' and r->>'stable_name'=body->>'type_name' and (r->>'version')::integer=(a->'schema'->>'version')::integer) then
  raise exception 'instance type outside Action scope' using errcode='42501';end if;
 select definition into s from ontology.object_type_versions where tenant_id=a->>'tenant_id' and type_name=body->>'type_name' and version=(a->'schema'->>'version')::integer;
 if not found or s is distinct from a->'schema' or s->>'only_edit_via_actions'<>'true' then
  raise exception 'instance schema stale' using errcode='42501';end if;
 select * into stored from runtime.nexloop_action_claims where tenant_id=a->>'tenant_id' and world=p_world
  and action_name=v_claim->'key'->>'action_stable_name' and intent_id=v_claim->'key'->>'idempotency_key' for update;
 if not found or stored.principal_id is distinct from a->>'principal_id' or stored.claim is distinct from v_claim
  or v_claim->>'state'<>'active' or (v_claim->>'lease_expires_at')::timestamptz<=clock_timestamp()
  or (permit->>'expires_at')::timestamptz<=clock_timestamp() then
  raise exception 'instance claim fenced' using errcode='40001';end if;
 if body->>'type_name' is distinct from 'RelationshipAssessment' or body->'properties'->>'corrects_revision' is distinct from '0' then raise exception 'assessment type/chain invalid' using errcode='42501';end if;
 perform authz.nexloop_assert_assessment_provenance(p_digest,p_world,a,body->'properties');
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 v_id:=encode(sha256(convert_to(jsonb_build_array(a->>'tenant_id',p_world,body->>'type_name',stored.intent_id)::text,'UTF8')),'hex');
 insert into ontology.nexloop_assessment_revisions(tenant_id,world,assessment_id,revision,prior_properties,new_properties,principal_id,action_resource,intent_id,transaction_id)
 values(a->>'tenant_id',p_world,v_id,1,null,body->'properties',a->>'principal_id',a->>'resource_id',stored.intent_id,txid_current());
 insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
  values(a->>'tenant_id',p_world,body->>'type_name',v_id,(s->>'version')::integer,body->'properties','nexloop-action',stored.intent_id,clock_timestamp(),clock_timestamp());
 v_outcome:=jsonb_build_object('outcome_id',v_id,'outcome_revision',1,'status','succeeded','outcome_digest',v_claim->'binding'->>'request_digest','finalized_at',clock_timestamp());
 update runtime.nexloop_action_claims set claim=stored.claim||jsonb_build_object('state','terminal','terminal_outcome',v_outcome)
  where tenant_id=stored.tenant_id and world=p_world and action_name=stored.action_name and intent_id=stored.intent_id;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 perform authz.nexloop_assert_assessment_provenance(p_digest,p_world,a,body->'properties');
 if a->>'expires_at' is null or permit->>'expires_at' is null or v_claim->>'lease_expires_at' is null
  or (a->>'expires_at')::timestamptz<=clock_timestamp() or (permit->>'expires_at')::timestamptz<=clock_timestamp()
  or (v_claim->>'lease_expires_at')::timestamptz<=clock_timestamp()
  or not exists(select 1 from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active and key_material=k) then
  raise exception 'assessment creation authority expired at commit' using errcode='42501';end if;
 select definition into d from control.nexloop_action_definitions where tenant_id=a->>'tenant_id' and world=p_world and resource_id=a->>'resource_id' and active for share;
 if not found or d is distinct from a->'definition' then raise exception 'assessment Action changed' using errcode='42501';end if;
 select definition into s from ontology.object_type_versions where tenant_id=a->>'tenant_id' and type_name='RelationshipAssessment' and version=(a->'schema'->>'version')::integer for share;
 if not found or s is distinct from a->'schema' then raise exception 'assessment schema changed' using errcode='42501';end if;
 if a->>'expires_at' is null or permit->>'expires_at' is null or v_claim->>'lease_expires_at' is null
  or (a->>'expires_at')::timestamptz<=clock_timestamp() or (permit->>'expires_at')::timestamptz<=clock_timestamp()
  or (v_claim->>'lease_expires_at')::timestamptz<=clock_timestamp()
  or exists(select 1 from jsonb_array_elements(a->'assessment_authorities') q where q->>'expires_at' is null or (q->>'expires_at')::timestamptz<=clock_timestamp())
  or exists(select 1 from jsonb_array_elements(a->'property_authorities') q where q->>'expires_at' is null or (q->>'expires_at')::timestamptz<=clock_timestamp())
  or not exists(select 1 from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active and key_material=k)
 then raise exception 'assessment final fence expired' using errcode='42501';end if;
 return jsonb_build_object('object_id',v_id,'type_name',body->>'type_name','world',p_world);
end $$;
alter function authz.nexloop_create_assessment_action(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_create_assessment_action(text,text,text,text,text) from public;
grant execute on function authz.nexloop_create_assessment_action(text,text,text,text,text) to nexloop_api,nexloop_domain_worker,nexloop_action_worker;

-- Candidate append-only commit-tail protection; published 0015/0053 immutable.
create function authz.nexloop_correct_assessment_action(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;body jsonb:=p_payload::jsonb;k bytea;stored runtime.nexloop_action_claims%rowtype;
 permit jsonb:=a->'permit';v_claim jsonb:=permit->'claim';ref jsonb:=v_claim->'binding'->'action_reference';d jsonb;s jsonb;v_id text;v_outcome jsonb;v_object ontology.objects%rowtype;field text;proof jsonb;
begin
 if octet_length(p_text)>1048576 or octet_length(p_payload)>262144 then raise exception 'instance command too large' using errcode='22023';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if a->>'expires_at' is null or permit->>'expires_at' is null or v_claim->>'lease_expires_at' is null then raise exception 'assessment command expiry required' using errcode='42501';end if;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-assessment-correct-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'protocol' is distinct from 'nexloop-assessment-correct-v1'
  or a->>'resource_id' is distinct from 'eios:action:'||(ref->>'stable_name')||':'||(ref->>'version')
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds'
  or v_claim->'binding'->>'request_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'instance permit rejected' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 select definition into d from control.nexloop_action_definitions where tenant_id=a->>'tenant_id' and world=p_world and resource_id=a->>'resource_id' and active;
 if not found or d is distinct from a->'definition' or d->'governance'->>'approval_mode'<>'none'
  or d->'governance'->>'risk_level'<>'low' or jsonb_array_length(d->'governance'->'policy_refs')<>0
  or d->'capability_binding'->>'capability_name' <> 'ontology.relationship_assessment.correct'
  or not (d->'governance'->'change_scope'->'target_systems' @> '["postgres"]'::jsonb)
  or jsonb_array_length(d->'governance'->'change_scope'->'properties')<>0
 then raise exception 'instance Action contract unavailable' using errcode='42501';end if;
 if not exists(select 1 from jsonb_array_elements(d->'governance'->'change_scope'->'object_types') r
  where r->>'tenant_id'=a->>'tenant_id' and r->>'stable_name'=body->>'type_name' and (r->>'version')::integer=(a->'schema'->>'version')::integer) then
  raise exception 'instance type outside Action scope' using errcode='42501';end if;
 select definition into s from ontology.object_type_versions where tenant_id=a->>'tenant_id' and type_name=body->>'type_name' and version=(a->'schema'->>'version')::integer;
 if not found or s is distinct from a->'schema' or s->>'only_edit_via_actions'<>'true' then
  raise exception 'instance schema stale' using errcode='42501';end if;
 select * into stored from runtime.nexloop_action_claims where tenant_id=a->>'tenant_id' and world=p_world
  and action_name=v_claim->'key'->>'action_stable_name' and intent_id=v_claim->'key'->>'idempotency_key' for update;
 if not found or stored.principal_id is distinct from a->>'principal_id' or stored.claim is distinct from v_claim
  or v_claim->>'state'<>'active' or (v_claim->>'lease_expires_at')::timestamptz<=clock_timestamp()
  or (permit->>'expires_at')::timestamptz<=clock_timestamp() then
  raise exception 'instance claim fenced' using errcode='40001';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 v_id:=body->>'object_id';
 if v_id !~ '^[a-f0-9]{64}$' or jsonb_typeof(body->'properties') is distinct from 'object'
  or (body->>'expected_revision')::bigint<1 or body->'properties'='{}'::jsonb
  or a->'object_authority'->>'target_resource' is distinct from 'eios:object:'||(body->>'type_name')||'/'||v_id then
  raise exception 'object edit target invalid' using errcode='42501';end if;
 perform authz.nexloop_assert_edit_authority(p_digest,p_world,a->'object_authority');
 for field in select jsonb_object_keys(body->'properties') order by 1 loop
  select value into proof from jsonb_array_elements(a->'property_authorities')
   where value->>'target_resource'='eios:property:'||(body->>'type_name')||'/'||v_id||'/'||field;
  if not found then raise exception 'property edit proof missing' using errcode='42501';end if;
  perform authz.nexloop_assert_edit_authority(p_digest,p_world,proof);
 end loop;
 select * into v_object from ontology.objects where tenant_id=a->>'tenant_id' and world=p_world
  and type_name=body->>'type_name' and object_id=v_id for update;
 if not found then raise exception 'object unavailable' using errcode='42501';end if;
 if v_object.schema_version<>(s->>'version')::integer or v_object.nexloop_revision<>(body->>'expected_revision')::bigint then
  raise exception 'object revision changed' using errcode='40001';end if;
 if body->>'type_name' is distinct from 'RelationshipAssessment' or body->'properties'->>'corrects_revision' is distinct from body->>'expected_revision'
  or ((v_object.properties->>'relation_version')::integer>0 and v_object.properties->>'relation_version' is distinct from body->'properties'->>'relation_version')
  or exists(select 1 from unnest(array['relation_name','source_type','source_id','target_type','target_id']) f where v_object.properties->>f is distinct from body->'properties'->>f) then
  raise exception 'assessment binding/chain invalid' using errcode='42501';end if;
 perform authz.nexloop_assert_assessment_provenance(p_digest,p_world,a,body->'properties');
 insert into ontology.nexloop_assessment_revisions(tenant_id,world,assessment_id,revision,prior_properties,new_properties,principal_id,action_resource,intent_id,transaction_id)
 values(a->>'tenant_id',p_world,v_id,v_object.nexloop_revision+1,v_object.properties,v_object.properties||body->'properties',a->>'principal_id',a->>'resource_id',stored.intent_id,txid_current());
 update ontology.objects set properties=properties||(body->'properties'),nexloop_revision=nexloop_revision+1,updated_at=clock_timestamp()
  where tenant_id=a->>'tenant_id' and world=p_world and type_name=body->>'type_name' and object_id=v_id;
 v_outcome:=jsonb_build_object('outcome_id',v_id,'outcome_revision',v_object.nexloop_revision+1,'status','succeeded','outcome_digest',v_claim->'binding'->>'request_digest','finalized_at',clock_timestamp());
 update runtime.nexloop_action_claims set claim=stored.claim||jsonb_build_object('state','terminal','terminal_outcome',v_outcome)
  where tenant_id=stored.tenant_id and world=p_world and action_name=stored.action_name and intent_id=stored.intent_id;
 -- Commit-tail validation: the UPDATE and terminal claim are provisional until all
 -- current proofs, original permit/lease and published contracts remain valid.
 if a->>'expires_at' is null or permit->>'expires_at' is null
  or v_claim->>'lease_expires_at' is null
  or (a->>'expires_at')::timestamptz<=clock_timestamp()
  or (permit->>'expires_at')::timestamptz<=clock_timestamp()
  or (v_claim->>'lease_expires_at')::timestamptz<=clock_timestamp()
 then raise exception 'edit authority expired at commit' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 perform authz.nexloop_assert_edit_authority(p_digest,p_world,a->'object_authority');
 for field in select jsonb_object_keys(body->'properties') order by 1 loop
  select value into proof from jsonb_array_elements(a->'property_authorities')
   where value->>'target_resource'='eios:property:'||(body->>'type_name')||'/'||v_id||'/'||field;
  if not found or proof->>'expires_at' is null then raise exception 'edit property proof missing' using errcode='42501';end if;
  perform authz.nexloop_assert_edit_authority(p_digest,p_world,proof);
 end loop;
 select definition into d from control.nexloop_action_definitions where tenant_id=a->>'tenant_id'
  and world=p_world and resource_id=a->>'resource_id' and active for share;
 if not found or d is distinct from a->'definition' then raise exception 'edit Action contract changed' using errcode='42501';end if;
 select definition into s from ontology.object_type_versions where tenant_id=a->>'tenant_id'
  and type_name=body->>'type_name' and version=(a->'schema'->>'version')::integer for share;
 if not found or s is distinct from a->'schema' then raise exception 'edit schema changed' using errcode='42501';end if;
 if not exists(select 1 from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active and key_material=k)
  or a->'object_authority'->>'expires_at' is null
  or (a->'object_authority'->>'expires_at')::timestamptz<=clock_timestamp()
  or exists(select 1 from jsonb_array_elements(a->'property_authorities') p
     where p->>'expires_at' is null or (p->>'expires_at')::timestamptz<=clock_timestamp())
  or (a->>'expires_at')::timestamptz<=clock_timestamp()
  or (permit->>'expires_at')::timestamptz<=clock_timestamp()
  or (v_claim->>'lease_expires_at')::timestamptz<=clock_timestamp()
 then raise exception 'edit authority expired at commit' using errcode='42501';end if;
 perform authz.nexloop_assert_assessment_provenance(p_digest,p_world,a,body->'properties');
 if a->>'expires_at' is null or permit->>'expires_at' is null or v_claim->>'lease_expires_at' is null
  or (a->>'expires_at')::timestamptz<=clock_timestamp() or (permit->>'expires_at')::timestamptz<=clock_timestamp()
  or (v_claim->>'lease_expires_at')::timestamptz<=clock_timestamp()
  or exists(select 1 from jsonb_array_elements(a->'assessment_authorities') q where q->>'expires_at' is null or (q->>'expires_at')::timestamptz<=clock_timestamp())
  or exists(select 1 from jsonb_array_elements(a->'property_authorities') q where q->>'expires_at' is null or (q->>'expires_at')::timestamptz<=clock_timestamp())
  or not exists(select 1 from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active and key_material=k)
 then raise exception 'assessment final fence expired' using errcode='42501';end if;
 return jsonb_build_object('object_id',v_id,'type_name',body->>'type_name','world',p_world,'revision',v_object.nexloop_revision+1);
end $$;

alter function authz.nexloop_correct_assessment_action(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_correct_assessment_action(text,text,text,text,text) from public;
grant execute on function authz.nexloop_correct_assessment_action(text,text,text,text,text) to nexloop_api,nexloop_domain_worker,nexloop_action_worker;

create function authz.nexloop_read_assessment_object(p_digest text,p_world text,p_text text,p_signature text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;r jsonb;proof jsonb;
begin
 if a->>'type_name' is distinct from 'RelationshipAssessment' or a->>'expires_at' is null then raise exception 'assessment read rejected' using errcode='42501';end if;
 r:=authz.nexloop_read_object(p_digest,p_world,p_text,p_signature);
 if not (r->'properties' ?& array['relation_name','relation_version','source_type','source_id','target_type','target_id','epistemic_kind','resolution_state']) then raise exception 'assessment typed read required' using errcode='42501';end if;
 proof:=a->'relation_authority';
 perform authz.nexloop_assert_assessment_relation(p_digest,p_world,a||jsonb_build_object('assessment_authorities',jsonb_build_array(proof)),r->'properties');
 -- Recheck every live read proof after definition locks and projected data reads.
 perform authz.nexloop_assert_read_authority(p_digest,p_world,a);
 for proof in select value from jsonb_array_elements(a->'property_authorities') loop
  if proof->>'expires_at' is null then raise exception 'assessment property expiry required' using errcode='42501';end if;
  perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
 end loop;
 perform authz.nexloop_assert_assessment_relation(p_digest,p_world,a||jsonb_build_object('assessment_authorities',jsonb_build_array(a->'relation_authority')),r->'properties');
 if a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp()
  or exists(select 1 from jsonb_array_elements(a->'property_authorities') q where q->>'expires_at' is null or (q->>'expires_at')::timestamptz<=clock_timestamp())
  or ((r->'properties'->>'relation_version')::integer>0 and (a->'relation_authority'->>'expires_at' is null or (a->'relation_authority'->>'expires_at')::timestamptz<=clock_timestamp()))
 then raise exception 'assessment read expired at return' using errcode='42501';end if;
 return r;
end $$;
alter function authz.nexloop_read_assessment_object(text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_read_assessment_object(text,text,text,text) from public;
grant execute on function authz.nexloop_read_assessment_object(text,text,text,text) to nexloop_api,nexloop_domain_worker,nexloop_action_worker;
