-- NX-029 final, part 1 (temporary number 0140): erasure propagation (AT-026 "deletion reaches every copy").
-- docs/implementation/NX-029-design.md §4.3 (formal objects, plans, effect ledger), dispatcher assignment 2026-10-11.
--
-- 1. Erasure values may also be an empty array (plan step prerequisites are free text in an array).
-- 2. Plans and strategies (§4.3 "清除自由文本；行保留至其保留期"): the NX-024 append-only trigger asks the erasure scope
--    first (body replaced in place, triggers unchanged); free-text columns may hold '' (the signed ports still require
--    1..N characters). The Consumer erasure clears strategy content, step expected results and prerequisites, Run
--    outcomes and free-text event reasons; status chains, ids and digests stay. A conditional Claim may lose its condition
--    text once its value is erased (0132 claims phase failed on such a Claim).
-- 3. Formal objects that refer to the Consumer (§4.3: RelationshipAssessment, ConsumerRoleLink and the others carrying
--    its consumer_id: Goal, PlanStep, EffectControl, ConsumerServiceOffering, ...) are deleted with their events,
--    relations and assessment revisions; the 0058 assessment guards let only a registered erasure through. Commitment
--    (D4) and CommercialRecord (NX-027 D6) keep their own handling. One tombstone per deleted object, ids only.
-- 4. Effect ledger (§4.3 "清除 frozen_request 中的正文"): the frozen request of the Consumer's intents (and, on
--    Message.erase, of the intent that sent that message) becomes {"erased":true}; states, receipts and digests stay.
--    An intent still dispatching or unknown is never touched (the erasure waits for reconciliation first).
-- 5. Erased messages leave the reads that assemble or show conversations: the WebChat history and event stream (0046
--    query, body replaced in place: the conversation-message contract requires a body, and paging stays dense) and the
--    Claim evidence view used by Context assembly (0073: a statement whose source text was erased or expired is not
--    returned; claim.schema.json requires a quote).
-- Published migrations are not edited.

-- 1. Empty array as an erasure value (0132 body, one value added) -----------------------------------------------------
create or replace function runtime.nexloop_erasure_permits(p_user text,p_table text,p_op text,p_old jsonb,p_new jsonb) returns boolean
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp as $$
declare s control.nexloop_erasure_scope;k text;
begin
 if p_user is distinct from 'nexloop_owner' then return false;end if;
 select * into s from control.nexloop_erasure_scope where txid=txid_current() and table_name=p_table;
 if not found then return false;end if;
 if p_op='DELETE' then return s.allow_delete;end if;
 if p_op<>'UPDATE' or p_new is null then return false;end if;
 for k in select key from jsonb_each(p_new) loop
  if p_old->k is distinct from p_new->k then
   if not (k=any(s.columns)) or not (p_new->k='null'::jsonb or p_new->k='""'::jsonb
     or p_new->k='[]'::jsonb or (jsonb_typeof(p_new->k)='object' and p_new->k->>'erased'='true')
     or (jsonb_typeof(p_new->k)='string' and (p_new->>k)~'^pseudonym:[0-9a-f]{32}$')) then return false;end if;
  end if;
 end loop;
 return true;
end $$;


-- 2. Plans and strategies -----------------------------------------------------------------------------------------------
create or replace function runtime.nexloop_plan_append_only() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if runtime.nexloop_erasure_permits(current_user::text,tg_table_schema||'.'||tg_table_name,tg_op,to_jsonb(old),case when tg_op='UPDATE' then to_jsonb(new) end) then
  if tg_op='DELETE' then return old;end if;
  return new;
 end if;
 raise exception 'plan records are append-only' using errcode='22023';
end $$;
alter table runtime.nexloop_strategies drop constraint nexloop_strategies_content_check,add constraint nexloop_strategies_content_check check(length(content)<=8192);
alter table runtime.nexloop_plan_steps drop constraint nexloop_plan_steps_expected_result_check,add constraint nexloop_plan_steps_expected_result_check check(length(expected_result)<=2000);
alter table runtime.nexloop_plan_events drop constraint nexloop_plan_events_reason_check,add constraint nexloop_plan_events_reason_check check(length(reason)<=500);
-- A conditional Claim keeps its condition while it lives; erased (value marked erased) it may hold none. Without this the
-- Consumer erasure stopped at its claims phase for any Consumer with a conditional Claim (0132 bug, found here).
alter table ontology.nexloop_claims drop constraint nexloop_claims_check4,
 add constraint nexloop_claims_check4 check(modality<>'conditional' or length(condition_text)>0 or value ? 'erased');

-- 3. The 0058 assessment guards let a registered erasure delete (bodies replaced in place; the head guard runs as the owner,
-- so the transaction-local erasure scope - owner-only - is what admits it, as for the NX-027 CommercialRecord guard).
create or replace function authz.nexloop_assessment_audit_immutable() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if runtime.nexloop_erasure_permits(current_user::text,'ontology.nexloop_assessment_revisions',tg_op,to_jsonb(old),null) and tg_op='DELETE' then return old;end if;
 raise exception 'assessment audit immutable' using errcode='42501';end $$;
create or replace function authz.nexloop_assessment_head_guard() returns trigger language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if TG_OP='DELETE' and old.type_name='RelationshipAssessment' and runtime.nexloop_erasure_permits('nexloop_owner','ontology.objects','DELETE',to_jsonb(old),null) then return old;end if;
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

-- 4. Message erasure also clears the sending intent (0132 body, one statement added) ------------------------------------
create or replace function runtime.nexloop_erase_message(p_tenant text,p_world text,p_message text,p_ref text,p_reason text,p_by text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare n_intents integer;n_stream integer;n_outbox integer;n_claims integer;n_object integer;n_prompts integer;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 update runtime.nexloop_conversation_messages set record=record||'{"body":"","erased":true}'::jsonb
  where tenant_id=p_tenant and world=p_world and message_id=p_message and record->>'body'<>'';
 get diagnostics n_stream=row_count;
 update runtime.nexloop_message_outbox set record=record||'{"body":"","erased":true}'::jsonb
  where tenant_id=p_tenant and world=p_world and message_id=p_message and record->>'body'<>'';
 get diagnostics n_outbox=row_count;
 -- The intent that sent this message (outbound) keeps its states and receipts, not the text.
 update runtime.nexloop_effect_intents i set frozen_request='{"erased":true}'::jsonb from runtime.nexloop_conversation_messages m
  where m.tenant_id=p_tenant and m.world=p_world and m.message_id=p_message and i.tenant_id=m.tenant_id and i.world=m.world
   and i.intent_id::text=m.record->>'intent_id' and i.state not in ('dispatching','unknown') and not (i.frozen_request ? 'erased');
 get diagnostics n_intents=row_count;
 update ontology.nexloop_claims set quote='' where tenant_id=p_tenant and world=p_world and source_message_id=p_message and quote<>'';
 get diagnostics n_claims=row_count;
 perform runtime.nexloop_erasure_open('runtime.nexloop_model_request_prompts',array['request_text'],false,p_ref);
 update runtime.nexloop_model_request_prompts p set request_text='' from runtime.nexloop_context_packs k
  where p.tenant_id=p_tenant and p.world=p_world and k.tenant_id=p.tenant_id and k.world=p.world and k.run_id=p.run_id and p.request_text<>''
   and exists(select 1 from runtime.nexloop_context_sources s where s.tenant_id=k.tenant_id and s.world=k.world and s.context_id=k.context_id
    and s.ref like '%'||p_message||'%');
 get diagnostics n_prompts=row_count;
 delete from ontology.events where tenant_id=p_tenant and object_type='Message' and object_id=p_message;
 delete from ontology.relations where tenant_id=p_tenant and ((source_type='Message' and source_object_id=p_message) or (target_type='Message' and target_object_id=p_message));
 delete from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='Message' and object_id=p_message;
 get diagnostics n_object=row_count;
 perform runtime.nexloop_erasure_close();
 perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,p_reason,p_ref,'message','message',p_message,'redacted',
  jsonb_build_object('stream',n_stream,'outbox',n_outbox,'intents',n_intents,'claims',n_claims,'prompts',n_prompts,'object',n_object),p_by);
 return jsonb_build_object('message_id',p_message,'stream',n_stream,'outbox',n_outbox,'intents',n_intents,'claims',n_claims,'prompts',n_prompts,'object',n_object);
end $$;
alter function runtime.nexloop_erase_message(text,text,text,text,text,text) owner to nexloop_owner;

-- 5. Erasure steps with the new phases (0132 body; phases effects, plans and references added) -----------------------------
create or replace function runtime.nexloop_erasure_step(p_tenant text,p_world text,p_request text,p_batch integer,p_by text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare e control.nexloop_consumer_erasures;n2 integer:=0;n3 integer:=0;v_ref text;n integer:=0;m integer:=0;x record;v_phases text[]:=array['messages','claims','runs_text','effects','commercial',
 'commitments','plans','references','runs_files','artifacts','consumer'];v_next text;v_pseudonym text;v_counts jsonb:='{}'::jsonb;k bytea;v_held boolean;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into e from control.nexloop_consumer_erasures where tenant_id=p_tenant and world=p_world and request_id=p_request for update skip locked;
 if not found or e.state in ('pending_confirmation','completed','completed_with_holds') then return jsonb_build_object('request_id',p_request,'state',e.state,'progress',false);end if;
 v_ref:='erasure:'||e.request_id;
 v_held:=runtime.nexloop_retention_held(p_tenant,p_world,'consumer',e.consumer_id);
 if e.state in ('blocking','settling','waiting_unknown_effects') then
  -- Unknown external results are reconciled first (0065); never resent blindly.
  if exists(select 1 from runtime.nexloop_effect_intents i where i.tenant_id=p_tenant and i.world=p_world and i.consumer_id=e.consumer_id
    and i.state in ('dispatching','unknown')) then
   update control.nexloop_consumer_erasures set state='waiting_unknown_effects',updated_at=clock_timestamp() where tenant_id=p_tenant and world=p_world and request_id=p_request;
   return jsonb_build_object('request_id',p_request,'state','waiting_unknown_effects','progress',false);
  end if;
  update control.nexloop_consumer_erasures set state='purging',phase='messages',updated_at=clock_timestamp() where tenant_id=p_tenant and world=p_world and request_id=p_request
   returning * into e;
 end if;
 if v_held then
  -- A Consumer hold keeps everything; the request completes with the hold recorded and nothing purged.
  perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,'consumer_erasure',v_ref,'consumer','consumer',e.consumer_id,'skipped_hold','{}'::jsonb,p_by);
  update control.nexloop_consumer_erasures set state='completed_with_holds',phase=null,completed_at=clock_timestamp(),updated_at=clock_timestamp()
   where tenant_id=p_tenant and world=p_world and request_id=p_request;
  return jsonb_build_object('request_id',p_request,'state','completed_with_holds','progress',true);
 end if;
 if e.phase='messages' then
  for x in select m.message_id from runtime.nexloop_conversation_messages m join runtime.nexloop_conversations c on c.tenant_id=m.tenant_id and c.world=m.world
    and c.conversation_id=m.conversation_id where m.tenant_id=p_tenant and m.world=p_world and c.consumer_id=e.consumer_id
    and (m.record->>'body'<>'' or exists(select 1 from ontology.objects o where o.tenant_id=m.tenant_id and o.world=m.world and o.type_name='Message' and o.object_id=m.message_id))
    order by m.conversation_id,m.sequence limit p_batch loop
   perform runtime.nexloop_erase_message(p_tenant,p_world,x.message_id,v_ref,'consumer_erasure',p_by);n:=n+1;
  end loop;
  v_counts:=jsonb_build_object('messages',n);
 elsif e.phase='claims' then
  with due as (select claim_id from ontology.nexloop_claims where tenant_id=p_tenant and world=p_world and consumer_id=e.consumer_id
    and not (value ? 'erased') limit p_batch for update skip locked)
  update ontology.nexloop_claims c set quote='',subject_text='',condition_text='',time_expression='',value='{"type":"erased","value":null,"erased":true}'::jsonb
   from due where c.tenant_id=p_tenant and c.world=p_world and c.claim_id=due.claim_id;
  get diagnostics n=row_count;v_counts:=jsonb_build_object('claims',n);
 elsif e.phase='runs_text' then
  perform runtime.nexloop_erasure_open('runtime.nexloop_model_request_prompts',array['request_text'],false,v_ref);
  update runtime.nexloop_model_request_prompts set request_text='' where tenant_id=p_tenant and world=p_world and request_text<>''
   and run_id in (select runtime.nexloop_consumer_runs(p_tenant,p_world,e.consumer_id));
  get diagnostics n=row_count;
  update runtime.jobs j set normalized_input=j.normalized_input||'{"input":"","input_erased":true}'::jsonb from authz.nexloop_runtime_run_bindings b
   where b.tenant_id=p_tenant and b.world=p_world and j.tenant_id=b.tenant_id and j.job_id=b.task_id and coalesce(j.normalized_input->>'input','')<>''
    and b.run_id in (select runtime.nexloop_consumer_runs(p_tenant,p_world,e.consumer_id));
  get diagnostics m=row_count;
  perform runtime.nexloop_erasure_close();
  v_counts:=jsonb_build_object('prompts',n,'jobs',m);n:=0;
 elsif e.phase='effects' then
  -- Frozen requests carry the text sent or requested; dispatching/unknown were reconciled before purging began.
  update runtime.nexloop_effect_intents set frozen_request='{"erased":true}'::jsonb where tenant_id=p_tenant and world=p_world
   and consumer_id=e.consumer_id and state not in ('dispatching','unknown') and not (frozen_request ? 'erased');
  get diagnostics m=row_count;v_counts:=jsonb_build_object('effect_requests',m);
 elsif e.phase='plans' then
  perform runtime.nexloop_erasure_open('runtime.nexloop_strategies',array['content'],false,v_ref);
  perform runtime.nexloop_erasure_open('runtime.nexloop_plan_steps',array['expected_result','prerequisites'],false,v_ref);
  perform runtime.nexloop_erasure_open('runtime.nexloop_plan_events',array['reason'],false,v_ref);
  perform runtime.nexloop_erasure_open('runtime.nexloop_plan_outcomes',array['outcome'],false,v_ref);
  update runtime.nexloop_strategies set content='' where tenant_id=p_tenant and world=p_world and consumer_id=e.consumer_id and content<>'';
  get diagnostics m=row_count;
  update runtime.nexloop_plan_steps s set expected_result='',prerequisites='[]'::jsonb from runtime.nexloop_plans p
   where p.tenant_id=p_tenant and p.world=p_world and p.consumer_id=e.consumer_id and s.tenant_id=p.tenant_id and s.world=p.world
    and s.plan_id=p.plan_id and s.version=p.version and (s.expected_result<>'' or s.prerequisites<>'[]'::jsonb);
  get diagnostics n2=row_count;
  -- Reason codes (snake_case) stay: they are the status chain; free text goes.
  update runtime.nexloop_plan_events v set reason='' where v.tenant_id=p_tenant and v.world=p_world and v.reason<>'' and v.reason!~'^[a-z][a-z0-9_]{0,63}$'
   and exists(select 1 from runtime.nexloop_plans p where p.tenant_id=v.tenant_id and p.world=v.world and p.plan_id=v.plan_id and p.consumer_id=e.consumer_id);
  get diagnostics n3=row_count;
  update runtime.nexloop_plan_outcomes o set outcome='{"erased":true}'::jsonb where o.tenant_id=p_tenant and o.world=p_world and not (o.outcome ? 'erased')
   and exists(select 1 from runtime.nexloop_plans p where p.tenant_id=o.tenant_id and p.world=o.world and p.plan_id=o.plan_id and p.consumer_id=e.consumer_id);
  get diagnostics n=row_count;
  perform runtime.nexloop_erasure_close();
  v_counts:=jsonb_build_object('strategies',m,'plan_steps',n2,'plan_reasons',n3,'plan_outcomes',n);n:=0;
 elsif e.phase='references' then
  -- Formal objects that name the Consumer: deleted with their events, relations and (assessments) revisions.
  perform runtime.nexloop_erasure_open('ontology.objects',null,true,v_ref);
  perform runtime.nexloop_erasure_open('ontology.nexloop_assessment_revisions',null,true,v_ref);
  for x in select o.type_name,o.object_id from ontology.objects o where o.tenant_id=p_tenant and o.world=p_world
    and o.type_name not in ('Consumer','Commitment','CommercialRecord','Message')
    and (o.properties->>'consumer_id'=e.consumer_id
     or (o.type_name='RelationshipAssessment' and (e.consumer_id in (o.properties->>'source_id',o.properties->>'target_id')
      or exists(select 1 from runtime.nexloop_conversation_messages cm join runtime.nexloop_conversations cv on cv.tenant_id=cm.tenant_id and cv.world=cm.world
        and cv.conversation_id=cm.conversation_id where cm.tenant_id=p_tenant and cm.world=p_world and cm.message_id=o.properties->>'evidence_message_id'
        and cv.consumer_id=e.consumer_id))))
    order by o.type_name,o.object_id limit p_batch loop
   delete from ontology.events where tenant_id=p_tenant and object_type=x.type_name and object_id=x.object_id;
   delete from ontology.relations where tenant_id=p_tenant and ((source_type=x.type_name and source_object_id=x.object_id) or (target_type=x.type_name and target_object_id=x.object_id));
   if x.type_name='RelationshipAssessment' then
    delete from ontology.nexloop_assessment_revisions where tenant_id=p_tenant and world=p_world and assessment_id=x.object_id;
   end if;
   delete from ontology.objects where tenant_id=p_tenant and world=p_world and type_name=x.type_name and object_id=x.object_id;
   perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,'consumer_erasure',v_ref,'formal_object','object',x.type_name||'/'||x.object_id,'deleted','{}'::jsonb,p_by);
   n:=n+1;v_counts:=v_counts||jsonb_build_object('objects:'||x.type_name,coalesce((v_counts->>('objects:'||x.type_name))::integer,0)+1);
  end loop;
  perform runtime.nexloop_erasure_close();
 elsif e.phase='commercial' then
  v_counts:=runtime.nexloop_commercial_pseudonymize_consumer(p_tenant,p_world,e.consumer_id);
  perform set_config('eios.tenant_id',p_tenant,true);
  v_pseudonym:='pseudonym:'||encode(extensions.gen_random_bytes(16),'hex');
  perform runtime.nexloop_erasure_open('runtime.nexloop_cost_entries',array['consumer_ref'],false,v_ref);
  perform runtime.nexloop_erasure_open('control.nexloop_metric_cohorts',array['member_ref'],false,v_ref);
  update runtime.nexloop_cost_entries set consumer_ref=null where tenant_id=p_tenant and world=p_world and consumer_ref=e.consumer_id;
  get diagnostics n=row_count;
  update control.nexloop_metric_cohorts set member_ref=v_pseudonym where tenant_id=p_tenant and world=p_world and member_ref=e.consumer_id;
  get diagnostics m=row_count;
  perform runtime.nexloop_erasure_close();
  v_counts:=v_counts||jsonb_build_object('cost_entries',n,'cohort_members',m);n:=0;
 elsif e.phase='commitments' then
  perform runtime.nexloop_erasure_open('ontology.objects',array['commitment_erasure'],false,v_ref);
  update ontology.objects set properties=properties||jsonb_build_object('made_to','erasure:'||e.request_id,'condition',null),updated_at=clock_timestamp()
   where tenant_id=p_tenant and world=p_world and type_name='Commitment' and properties->>'made_to'=e.consumer_id;
  get diagnostics n=row_count;
  perform runtime.nexloop_erasure_close();
  v_counts:=jsonb_build_object('commitments',n);n:=0;
 elsif e.phase='runs_files' then
  insert into runtime.nexloop_run_purge_items(tenant_id,world,run_id,request_ref,reason)
   select p_tenant,p_world,r,v_ref,'consumer_erasure' from runtime.nexloop_consumer_runs(p_tenant,p_world,e.consumer_id) r on conflict do nothing;
  get diagnostics m=row_count;v_counts:=jsonb_build_object('run_purges_queued',m);
 elsif e.phase='artifacts' then
  insert into runtime.nexloop_erasure_artifacts(tenant_id,world,artifact_id,request_id)
   select p_tenant,p_world,k2.artifact_id,e.request_id from runtime.nexloop_context_packs k2 where k2.tenant_id=p_tenant and k2.world=p_world
    and k2.run_id in (select runtime.nexloop_consumer_runs(p_tenant,p_world,e.consumer_id))
   union select p_tenant,p_world,q.prompt_artifact_id,e.request_id from runtime.nexloop_model_requests q where q.tenant_id=p_tenant and q.world=p_world
    and q.run_id in (select runtime.nexloop_consumer_runs(p_tenant,p_world,e.consumer_id))
   on conflict do nothing;
  get diagnostics m=row_count;v_counts:=jsonb_build_object('artifacts_queued',m);
 elsif e.phase='consumer' then
  -- Files first: the request completes only when its Run files and Artifacts are gone (a failed purge keeps it open).
  if exists(select 1 from runtime.nexloop_run_purge_items where tenant_id=p_tenant and world=p_world and request_ref=v_ref and state='pending')
   or exists(select 1 from runtime.nexloop_erasure_artifacts a join runtime.nexloop_local_artifacts l on l.tenant_id=a.tenant_id and l.world=a.world
     and l.artifact_id=a.artifact_id where a.tenant_id=p_tenant and a.world=p_world and a.request_id=e.request_id and l.status<>'deleted') then
   return jsonb_build_object('request_id',p_request,'state','purging','phase','consumer','progress',false,'waiting','files');
  end if;
  -- D5: an identity-free key per external reference (salted per tenant, owner side) keeps a re-imported identity uncontacted.
  insert into control.nexloop_erasure_salts(tenant_id,salt) values(p_tenant,extensions.gen_random_bytes(32)) on conflict do nothing;
  select salt into k from control.nexloop_erasure_salts where tenant_id=p_tenant;
  insert into control.nexloop_erased_contact_keys(tenant_id,world,key_digest,request_id)
   select p_tenant,p_world,encode(extensions.hmac(convert_to(ref.value#>>'{}','UTF8'),k,'sha256'),'hex'),e.request_id
   from ontology.objects o,jsonb_array_elements(case when jsonb_typeof(o.properties->'external_id_refs')='array' then o.properties->'external_id_refs' else '[]'::jsonb end) ref
   where o.tenant_id=p_tenant and o.world=p_world and o.type_name='Consumer' and o.object_id=e.consumer_id on conflict do nothing;
  get diagnostics m=row_count;
  delete from ontology.events where tenant_id=p_tenant and object_type='Consumer' and object_id=e.consumer_id;
  delete from ontology.relations where tenant_id=p_tenant and ((source_type='Consumer' and source_object_id=e.consumer_id) or (target_type='Consumer' and target_object_id=e.consumer_id));
  delete from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='Consumer' and object_id=e.consumer_id;
  get diagnostics n=row_count;
  perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,'consumer_erasure',v_ref,'consumer','consumer',e.consumer_id,'deleted',
   e.counts||jsonb_build_object('consumer_object',n,'contact_keys',m),p_by);
  update control.nexloop_consumer_erasures set state='completed',phase=null,completed_at=clock_timestamp(),updated_at=clock_timestamp(),
   counts=counts||jsonb_build_object('consumer_object',n,'contact_keys',m) where tenant_id=p_tenant and world=p_world and request_id=p_request;
  return jsonb_build_object('request_id',p_request,'state','completed','progress',true);
 end if;
 -- A phase is done when its batch was not full; then the next phase.
 v_next:=case when n>=p_batch then e.phase else v_phases[array_position(v_phases,e.phase)+1] end;
 update control.nexloop_consumer_erasures set phase=v_next,updated_at=clock_timestamp(),
  counts=counts||(select coalesce(jsonb_object_agg(key,coalesce((counts->>key)::bigint,0)+(value#>>'{}')::bigint),'{}'::jsonb) from jsonb_each(v_counts))
  where tenant_id=p_tenant and world=p_world and request_id=p_request;
 return jsonb_build_object('request_id',p_request,'state','purging','phase',v_next,'progress',true,'counts',v_counts);
end $$;
alter function runtime.nexloop_erasure_step(text,text,text,integer,text) owner to nexloop_owner;

-- 6. Erased messages leave the Claim evidence view (0073 body, one condition added) ---------------------------------
create or replace function authz.nexloop_read_conversation_claims(p_digest text,p_world text,p_text text,p_signature text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
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
   where x.tenant_id=v_tenant and x.world=p_world and x.conversation_id=v_id and x.epistemic_kind<>'hypothesis' and x.quote<>''
    and (a->>'input_digest' is null or exists(select 1 from ontology.nexloop_extraction_run_claims rc where rc.tenant_id=x.tenant_id and rc.world=x.world and rc.claim_id=x.claim_id and rc.input_digest=a->>'input_digest'))),
  'hypotheses',(select coalesce(jsonb_agg(authz.nexloop_claim_contract(x) order by x.recorded_at,x.claim_id),'[]'::jsonb) from ontology.nexloop_claims x
   where x.tenant_id=v_tenant and x.world=p_world and x.conversation_id=v_id and x.epistemic_kind='hypothesis'
    and (a->>'input_digest' is null or exists(select 1 from ontology.nexloop_extraction_run_claims rc where rc.tenant_id=x.tenant_id and rc.world=x.world and rc.claim_id=x.claim_id and rc.input_digest=a->>'input_digest')))
 ) into v_result;
 perform authz.nexloop_assert_read_authority(p_digest,p_world,a);
 return v_result;
end $$;

-- 7. ... and the WebChat history and event stream (0046 body, one condition added) -------------------------------------
create or replace function authz.nexloop_conversation_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;v_verb text:=c->>'verb';v_tenant text:=a->>'tenant_id';
 v_principal text:=a->>'principal_id';v_owner control.nexloop_consumer_owners%rowtype;v_conversation runtime.nexloop_conversations%rowtype;
 v_message runtime.nexloop_conversation_messages%rowtype;v_body jsonb;v_properties jsonb;v_create jsonb:=c->'create';v_result jsonb;
 v_id text;v_key text:=c->>'idempotency_key';v_digest text;v_sequence bigint;v_items jsonb;v_after bigint;v_limit integer;v_definition jsonb;v_capability jsonb;
begin
 if session_user<>'nexloop_api' or p_world is distinct from 'real' or octet_length(p_text)>1048576 or octet_length(p_payload)>262144
  or a->>'protocol' is distinct from 'nexloop-conversation-v1' or a->>'operation' is distinct from 'execute'
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'conversation unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-conversation-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'conversation unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 select definition,capability into v_definition,v_capability from control.nexloop_action_definitions
  where tenant_id=v_tenant and world=p_world and resource_id=a->>'action_resource' and active for share;
 if not found or v_definition is distinct from a->'definition' or v_capability is distinct from a->'capability'
  or v_definition->'governance'->>'approval_mode' is distinct from 'none'
  or v_definition->'governance'->>'risk_level' is distinct from 'low'
  or jsonb_array_length(v_definition->'governance'->'policy_refs')<>0 then
  raise exception 'conversation unavailable' using errcode='42501';end if;
 if (v_verb in ('prepare_owner','commit_owner') and a->>'action_resource' is distinct from 'eios:action:ConsumerOwnership.create:1')
  or (v_verb in ('prepare_conversation','commit_conversation') and a->>'action_resource' is distinct from 'eios:action:Conversation.create:1')
  or (v_verb in ('prepare_message','commit_message') and a->>'action_resource' is distinct from 'eios:action:Message.create:1')
  or (v_verb in ('conversations','messages','events') and a->>'action_resource' is distinct from 'eios:action:nexloop.conversation.read:1') then
  raise exception 'conversation unavailable' using errcode='42501';end if;
 if v_verb in ('prepare_owner','commit_owner') then
  -- Registrar is a genuine governed service, never a consumer session.
  if exists(select 1 from control.nexloop_browser_sessions b where encode(b.session_token_digest,'hex')=p_digest)
   or not exists(select 1 from authz.nexloop_service_credentials t where t.token_digest=p_digest and t.tenant_id=v_tenant
      and t.binding->>'subject_kind'='service' and t.status='active')
   or not exists(select 1 from ontology.objects o where o.tenant_id=v_tenant and o.world=p_world and o.type_name='Consumer' and o.object_id=c->>'consumer_id')
   or not exists(select 1 from control.nexloop_browser_memberships m join control.nexloop_browser_subjects s on s.subject_id=m.subject_id
     where m.tenant_id=v_tenant and m.principal_id=c->>'principal_id' and m.payload->>'status'='active' and s.payload->>'kind'='human' and s.payload->>'status'='active'
      and (m.payload->>'valid_from')::timestamptz<=clock_timestamp() and (m.payload->>'valid_until' is null or (m.payload->>'valid_until')::timestamptz>clock_timestamp())) then
   raise exception 'conversation unavailable' using errcode='42501';end if;
  perform pg_advisory_xact_lock(hashtextextended(v_tenant||':'||p_world||':owner:'||(c->>'principal_id'),0));
  perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
  perform m.principal_id from control.nexloop_browser_memberships m join control.nexloop_browser_subjects s on s.subject_id=m.subject_id
   where m.tenant_id=v_tenant and m.principal_id=c->>'principal_id' and m.payload->>'status'='active' and s.payload->>'kind'='human' and s.payload->>'status'='active'
    and (m.payload->>'valid_from')::timestamptz<=clock_timestamp() and (m.payload->>'valid_until' is null or (m.payload->>'valid_until')::timestamptz>clock_timestamp()) for share of m,s;
  if not found then raise exception 'conversation unavailable' using errcode='42501';end if;
  select * into v_owner from control.nexloop_consumer_owners where tenant_id=v_tenant and world=p_world and principal_id=c->>'principal_id' for update;
  if found then
   if v_owner.consumer_id is distinct from c->>'consumer_id' or v_owner.idempotency_key is distinct from v_key then
    raise exception 'conversation_payload_conflict' using errcode='P0001';end if;
   perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
   return jsonb_build_object('replay',true,'result',jsonb_build_object('consumer_id',v_owner.consumer_id,'principal_id',v_owner.principal_id,'ownership_id',v_owner.ownership_id));
  end if;
  v_body=jsonb_build_object('request_id','consumer-owner-'||encode(sha256(convert_to(v_tenant||':'||(c->>'principal_id')||':'||v_key,'UTF8')),'hex'),
   'type_name','ConsumerOwnership','properties',jsonb_build_object('consumer_id',c->>'consumer_id','principal_id',c->>'principal_id'));
 elsif v_verb in ('prepare_conversation','commit_conversation','conversations','prepare_message','commit_message','messages','events') then
  -- Session digest must really be browser-backed; no SERVICE pretending Human.
  if not exists(select 1 from control.nexloop_browser_sessions b where encode(b.session_token_digest,'hex')=p_digest) then
   raise exception 'conversation unavailable' using errcode='42501';end if;
  select * into v_owner from control.nexloop_consumer_owners where tenant_id=v_tenant and world=p_world and principal_id=v_principal for share;
  if not found or not exists(select 1 from ontology.objects o where o.tenant_id=v_tenant and o.world=p_world and o.type_name='ConsumerOwnership'
    and o.object_id=v_owner.ownership_id and o.properties=jsonb_build_object('consumer_id',v_owner.consumer_id,'principal_id',v_principal))
   or not exists(select 1 from ontology.objects o where o.tenant_id=v_tenant and o.world=p_world and o.type_name='Consumer' and o.object_id=v_owner.consumer_id) then
   raise exception 'conversation unavailable' using errcode='42501';end if;
  if v_verb='conversations' then
   if jsonb_typeof(c->'limit') is distinct from 'number' or c->>'limit'!~'^[1-9][0-9]{0,2}$'
    or jsonb_typeof(c->'after') is distinct from 'string' or (c->>'after'<>'' and c->>'after'!~'^[0-9a-f]{64}$') then
    raise exception 'conversation unavailable' using errcode='42501';end if;
   v_limit=(c->>'limit')::integer;
   if v_limit not between 1 and 100 or c->>'after' is null then raise exception 'conversation unavailable' using errcode='42501';end if;
   select coalesce(jsonb_agg(x.record order by x.conversation_id),'[]'::jsonb) into v_items from
    (select t.conversation_id,jsonb_build_object('id',t.conversation_id,'consumer_id',t.consumer_id,'world_id',p_world,'revision',1) record
     from runtime.nexloop_conversations t where t.tenant_id=v_tenant and t.world=p_world and t.principal_id=v_principal
      and exists(select 1 from ontology.objects o where o.tenant_id=v_tenant and o.world=p_world and o.type_name='Conversation' and o.object_id=t.conversation_id
       and o.properties=jsonb_build_object('consumer_id',t.consumer_id,'owner_principal',v_principal))
      and t.consumer_id=v_owner.consumer_id and t.conversation_id>c->>'after' order by t.conversation_id limit v_limit) x;
   perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
   return jsonb_build_object('items',v_items,'next_cursor',case when jsonb_array_length(v_items)=v_limit then v_items->(v_limit-1)->>'id' else null end);
  end if;
  if v_verb in ('prepare_conversation','commit_conversation') then
   perform pg_advisory_xact_lock(hashtextextended(v_tenant||':'||p_world||':conversation:'||v_principal||':'||v_key,0));
   select * into v_conversation from runtime.nexloop_conversations where tenant_id=v_tenant and world=p_world and principal_id=v_principal and idempotency_key=v_key for update;
   if found then
    perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
    return jsonb_build_object('replay',true,'result',jsonb_build_object('id',v_conversation.conversation_id,'consumer_id',v_conversation.consumer_id,'world_id',p_world,'revision',1));
   end if;
   v_body=jsonb_build_object('request_id','conversation-'||encode(sha256(convert_to(v_principal||':'||v_key,'UTF8')),'hex'),
    'type_name','Conversation','properties',jsonb_build_object('consumer_id',v_owner.consumer_id,'owner_principal',v_principal));
  else
   select * into v_conversation from runtime.nexloop_conversations where tenant_id=v_tenant and world=p_world and conversation_id=c->>'conversation_id' for update;
   if not found or v_conversation.principal_id is distinct from v_principal or v_conversation.consumer_id is distinct from v_owner.consumer_id
    or not exists(select 1 from ontology.objects o where o.tenant_id=v_tenant and o.world=p_world and o.type_name='Conversation' and o.object_id=v_conversation.conversation_id
      and o.properties=jsonb_build_object('consumer_id',v_conversation.consumer_id,'owner_principal',v_principal)) then
    raise exception 'conversation unavailable' using errcode='42501';end if;
   perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
   if v_verb in ('messages','events') then
    if jsonb_typeof(c->'limit') is distinct from 'number' or c->>'limit'!~'^[1-9][0-9]{0,2}$'
     or jsonb_typeof(c->'after_sequence') is distinct from 'number' or c->>'after_sequence'!~'^(0|[1-9][0-9]{0,18})$'
     or (length(c->>'after_sequence')=19 and c->>'after_sequence'>'9223372036854775807') then
     raise exception 'conversation unavailable' using errcode='42501';end if;
    v_limit=(c->>'limit')::integer;v_after=(c->>'after_sequence')::bigint;
    if v_limit not between 1 and 100 or v_after<0 then raise exception 'conversation unavailable' using errcode='42501';end if;
    select coalesce(jsonb_agg(case when v_verb='events' then jsonb_build_object('id',x.sequence::text,'type','message.accepted','data',x.record) else x.record end order by x.sequence),'[]'::jsonb) into v_items
     from (select m.sequence,m.record from runtime.nexloop_conversation_messages m where m.tenant_id=v_tenant and m.world=p_world
       and m.conversation_id=v_conversation.conversation_id and m.sequence>v_after and m.record->>'erased' is distinct from 'true'
       order by m.sequence limit v_limit) x;
    perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
    return jsonb_build_object('items',v_items,'next_cursor',case when jsonb_array_length(v_items)=v_limit then
      case when v_verb='events' then v_items->(v_limit-1)->>'id' else v_items->(v_limit-1)->>'sequence' end else null end);
   end if;
   if jsonb_typeof(c->'body') is distinct from 'string' or length(c->>'body') not between 1 and 8192 then raise exception 'conversation unavailable' using errcode='42501';end if;
   v_digest=encode(sha256(convert_to(jsonb_build_object('body',c->>'body')::text,'UTF8')),'hex');
   select * into v_message from runtime.nexloop_conversation_messages where tenant_id=v_tenant and world=p_world and conversation_id=v_conversation.conversation_id and idempotency_key=v_key;
   if found then
    if v_message.payload_digest is distinct from v_digest then raise exception 'conversation_payload_conflict' using errcode='P0001';end if;
    perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
    return jsonb_build_object('replay',true,'result',jsonb_build_object('message',v_message.record,'created',false));
   end if;
   v_sequence=v_conversation.last_sequence+1;
   v_body=jsonb_build_object('request_id','message-'||encode(sha256(convert_to(v_conversation.conversation_id||':'||v_key,'UTF8')),'hex'),'type_name','Message',
    'properties',jsonb_build_object('conversation_id',v_conversation.conversation_id,'sequence',v_sequence,'actor',v_principal,'body',c->>'body','accepted_at',clock_timestamp()::text));
  end if;
 else raise exception 'conversation unavailable' using errcode='42501';end if;
 if v_key is null or length(v_key) not between 16 and 200 or v_key!~'^[A-Za-z0-9._~-]+$' then raise exception 'conversation unavailable' using errcode='42501';end if;
 if v_verb like 'prepare_%' then
  perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
  return jsonb_build_object('replay',false,'payload',v_body);
 end if;
 if jsonb_typeof(v_create) is distinct from 'object' or v_create->>'text' is null or v_create->>'signature' is null or v_create->>'payload' is null then
  raise exception 'conversation unavailable' using errcode='42501';end if;
 -- Message accepted_at was generated in prepare inside this transaction; no
 -- runtime/client date authorizes writing. All immutable binding fields match.
 if v_verb='commit_message' then
  v_properties=(v_create->>'payload')::jsonb->'properties';
  if v_properties-'accepted_at' is distinct from (v_body->'properties')-'accepted_at'
   or jsonb_typeof(v_properties->'accepted_at') is distinct from 'string'
   or (v_properties->>'accepted_at')::timestamptz>clock_timestamp()
   or (v_properties->>'accepted_at')::timestamptz<transaction_timestamp()-interval '30 seconds' then
   raise exception 'conversation unavailable' using errcode='42501';end if;
  v_body=jsonb_set(v_body,'{properties,accepted_at}',v_properties->'accepted_at');
 end if;
 if (v_create->>'payload')::jsonb is distinct from v_body then raise exception 'conversation unavailable' using errcode='42501';end if;
 v_result=authz.nexloop_create_object_action(p_digest,p_world,v_create->>'text',v_create->>'signature',v_create->>'payload');
 v_id=v_result->>'object_id';
 if v_verb='commit_owner' then
  insert into control.nexloop_consumer_owners values(v_tenant,p_world,c->>'principal_id',c->>'consumer_id',v_id,v_key);
  v_result=jsonb_build_object('consumer_id',c->>'consumer_id','principal_id',c->>'principal_id','ownership_id',v_id);
 elsif v_verb='commit_conversation' then
  insert into runtime.nexloop_conversations values(v_tenant,p_world,v_id,v_owner.consumer_id,v_principal,v_key,0);
  v_result=jsonb_build_object('id',v_id,'consumer_id',v_owner.consumer_id,'world_id',p_world,'revision',1);
 else
  v_result=jsonb_build_object('id',v_id,'conversation_id',v_conversation.conversation_id,'sequence',v_sequence,'actor',v_principal,
   'body',c->>'body','accepted_at',v_properties->>'accepted_at','status','accepted');
  insert into runtime.nexloop_conversation_messages values(v_tenant,p_world,v_conversation.conversation_id,v_sequence,v_id,v_key,v_digest,v_result);
  insert into runtime.nexloop_message_inbox values(v_tenant,p_world,v_id,v_conversation.conversation_id||':'||v_sequence);
  insert into runtime.nexloop_message_outbox values(v_tenant,p_world,v_conversation.conversation_id||':'||v_sequence,v_id,v_conversation.conversation_id,v_sequence,v_result,'pending');
  update runtime.nexloop_conversations set last_sequence=v_sequence where tenant_id=v_tenant and world=p_world and conversation_id=v_conversation.conversation_id;
  v_result=jsonb_build_object('message',v_result,'created',true);
 end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 return v_result;
end $$;
