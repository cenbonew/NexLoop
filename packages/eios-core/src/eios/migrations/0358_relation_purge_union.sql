-- NX-054 P4c (docs/design/relation-read-p4.md v2 §1; ships with P4a, 0360, on real - design §3 R4): the erasure purge finds the
-- Consumer's formal objects through relations as well as through properties (ADR-038 §2.3: the purge uses the union for good).
-- runtime.nexloop_relation_consumer_objects(tenant, world, consumer): the objects whose ownership relations reach the Consumer, walked
-- backwards at most the published longest chain (a Consumer source ends a path, as the forward walk 0319:271 does).
-- L6 review C-1: the purge itself cuts those chains (the messages phase deletes the relations of each erased Message, 0132; the
-- references phase deletes a Conversation, and with it message_in, before the RelationshipAssessment behind it), so the walk is done
-- once, when the purge starts, and frozen: control.nexloop_erasure_relation_targets holds the request's targets (owner only) until
-- its consumer phase ends. runtime.nexloop_erasure_step is 0227's body with these additions only (registered in the copy guard):
-- the targets frozen before the first purge phase runs (an erasure already purging when this ships freezes at its next step); the
-- references phase also deletes the frozen targets (same type exclusions: Consumer, Commitment, CommercialRecord, Message keep their
-- own phases); the commitments phase also pseudonymizes a frozen Commitment (linked only by commitment_to); the consumer phase drops
-- the request's frozen rows. Nothing the property rule found is dropped. CommercialRecord (commercial phase, consumer_ref) and Message
-- (messages phase, by conversation) keep their own resolutions: both are complete without relations (a CommercialRecord always
-- carries consumer_ref; a Message belongs to its conversation). Temporary number; the dispatcher renumbers.

create function runtime.nexloop_relation_consumer_objects(p_tenant text,p_world text,p_consumer text) returns table(type_name text,object_id text)
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_limit integer;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select c.longest_ownership_chain into v_limit from control.nexloop_relation_catalogs c where c.tenant_id=p_tenant order by c.version desc limit 1;
 return query with recursive walk(t,id,depth) as (
   select 'Consumer'::text,p_consumer,0
   union
   select r.source_type,r.source_object_id,w.depth+1 from walk w
    join ontology.relations r on r.tenant_id=p_tenant and r.world=p_world and r.target_type=w.t and r.target_object_id=w.id
    join control.nexloop_relation_type_profiles p on p.tenant_id=r.tenant_id and p.relation_name=r.relation_name and p.version=r.schema_version
     and p.consumer_ownership is not null
    where w.depth<coalesce(v_limit,0) and (w.depth=0 or w.t<>'Consumer'))
  select distinct w.t,w.id from walk w where w.depth>0;
end $$;
alter function runtime.nexloop_relation_consumer_objects(text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_relation_consumer_objects(text,text,text) from public;

create table control.nexloop_erasure_relation_targets (
 tenant_id text not null,world text not null,request_id text not null,type_name text not null,object_id text not null,
 primary key(tenant_id,world,request_id,type_name,object_id),
 foreign key(tenant_id,world,request_id) references control.nexloop_consumer_erasures(tenant_id,world,request_id) on delete cascade);
create table control.nexloop_erasure_relation_frozen (
 tenant_id text not null,world text not null,request_id text not null,frozen_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,request_id),
 foreign key(tenant_id,world,request_id) references control.nexloop_consumer_erasures(tenant_id,world,request_id) on delete cascade);
do $$declare t text;begin
 foreach t in array array['control.nexloop_erasure_relation_targets','control.nexloop_erasure_relation_frozen'] loop
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy tenant_boundary on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on %s from public',t);
 end loop;
end $$;

-- Once per request: the objects the ownership relations reach now, before any purge phase cuts a chain.
create function runtime.nexloop_erasure_freeze_relation_targets(p_tenant text,p_world text,p_request text,p_consumer text) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 insert into control.nexloop_erasure_relation_frozen(tenant_id,world,request_id) values(p_tenant,p_world,p_request) on conflict do nothing;
 if found then
  insert into control.nexloop_erasure_relation_targets(tenant_id,world,request_id,type_name,object_id)
   select p_tenant,p_world,p_request,r.type_name,r.object_id from runtime.nexloop_relation_consumer_objects(p_tenant,p_world,p_consumer) r
   on conflict do nothing;
 end if;
end $$;
alter function runtime.nexloop_erasure_freeze_relation_targets(text,text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_erasure_freeze_relation_targets(text,text,text,text) from public;

create or replace function runtime.nexloop_erasure_step(p_tenant text,p_world text,p_request text,p_batch integer,p_by text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare e control.nexloop_consumer_erasures;n2 integer:=0;n3 integer:=0;v_ref text;n integer:=0;m integer:=0;x record;v_phases text[]:=array['messages','claims','runs_text','effects','commercial',
 'commitments','incentives','plans','references','relay','runs_files','artifacts','consumer'];v_next text;v_pseudonym text;v_counts jsonb:='{}'::jsonb;k bytea;v_held boolean;
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
 -- NX-054 P4c (L6 C-1): the relation targets frozen before the first phase cuts a chain.
 perform runtime.nexloop_erasure_freeze_relation_targets(p_tenant,p_world,e.request_id,e.consumer_id);
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
     or (o.type_name,o.object_id) in (select r.type_name,r.object_id from control.nexloop_erasure_relation_targets r
      where r.tenant_id=p_tenant and r.world=p_world and r.request_id=e.request_id)
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
 elsif e.phase='relay' then
  -- Relay multi-consumer (0160–0163): the consumer's relay enrollments (withdrawn by the erasure itself: the relay already
  -- skipped it while erasing) are cleared with its rotation cursor and claim records; one tombstone, counts only.
  perform runtime.nexloop_erasure_open('control.nexloop_relay_enrollments',null,true,v_ref);
  delete from control.nexloop_relay_enrollments where tenant_id=p_tenant and world=p_world and consumer_id=e.consumer_id;
  get diagnostics n2=row_count;
  perform runtime.nexloop_erasure_close();
  delete from runtime.nexloop_relay_consumer_cursors where tenant_id=p_tenant and world=p_world and consumer_id=e.consumer_id;
  get diagnostics n3=row_count;
  delete from runtime.nexloop_relay_claims where tenant_id=p_tenant and world=p_world and consumer_id=e.consumer_id;
  get diagnostics m=row_count;
  perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,'consumer_erasure',v_ref,'relay_enrollment','control.nexloop_relay_enrollments',e.consumer_id,'deleted',
   jsonb_build_object('relay_enrollments',n2,'relay_cursors',n3,'relay_claims',m),p_by);
  v_counts:=jsonb_build_object('relay_enrollments',n2,'relay_cursors',n3,'relay_claims',m);n2:=0;n3:=0;m:=0;
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
 elsif e.phase='incentives' then
  -- AT-043: an issue whose provider answer is outstanding is settled first (queried, never re-sent); an accepted one is
  -- refused; then the rows keep amounts, states and ids but not the person: a random pseudonym for the Consumer, no
  -- notification text, no reply binding, no control snapshot (it names the Consumer).
  if exists(select 1 from runtime.nexloop_incentive_issues where tenant_id=p_tenant and world=p_world and consumer_id=e.consumer_id
    and state in ('dispatching','unknown')) then
   return jsonb_build_object('request_id',p_request,'state','purging','phase','incentives','progress',false,'waiting','incentive_results');
  end if;
  update runtime.nexloop_incentive_issues set state='refused',refusal_code='consumer_erasing',finished_at=clock_timestamp(),updated_at=clock_timestamp()
   where tenant_id=p_tenant and world=p_world and consumer_id=e.consumer_id and state='accepted';
  get diagnostics m=row_count;
  perform runtime.nexloop_erasure_open('runtime.nexloop_incentive_issues',array['consumer_id','notification_text','reply_to_message_id','control_snapshot'],false,v_ref);
  update runtime.nexloop_incentive_issues set consumer_id='pseudonym:'||encode(extensions.gen_random_bytes(16),'hex'),notification_text='',
   reply_to_message_id=null,control_snapshot='{"erased":true}'::jsonb where tenant_id=p_tenant and world=p_world and consumer_id=e.consumer_id;
  get diagnostics n2=row_count;
  perform runtime.nexloop_erasure_close();
  v_counts:=jsonb_build_object('incentive_issues',n2,'incentive_refused',m);
 elsif e.phase='commitments' then
  perform runtime.nexloop_erasure_open('ontology.objects',array['commitment_erasure'],false,v_ref);
  update ontology.objects set properties=properties||jsonb_build_object('made_to','erasure:'||e.request_id,'condition',null),updated_at=clock_timestamp()
   where tenant_id=p_tenant and world=p_world and type_name='Commitment' and (properties->>'made_to'=e.consumer_id
    or object_id in (select r.object_id from control.nexloop_erasure_relation_targets r
     where r.tenant_id=p_tenant and r.world=p_world and r.request_id=e.request_id and r.type_name='Commitment'));
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
  delete from control.nexloop_erasure_relation_targets where tenant_id=p_tenant and world=p_world and request_id=e.request_id;
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
  -- 0227 (Codex review, PR 13): the operator persona assignment leaves with the Consumer: the per-Consumer rows are deleted, the
  -- append-only assignment events keep persona ids and times under a random pseudonym, without request or reason. ADR-034's
  -- persona marker (0220) likewise: the row goes, its events keep values and times under the same pseudonym, reason cleared.
  v_pseudonym:='pseudonym:'||encode(extensions.gen_random_bytes(16),'hex');
  delete from control.nexloop_operator_persona_assignments where tenant_id=p_tenant and world=p_world and consumer_id=e.consumer_id;
  delete from control.nexloop_operator_persona_current where tenant_id=p_tenant and world=p_world and consumer_id=e.consumer_id;
  perform runtime.nexloop_erasure_open('control.nexloop_operator_persona_assignment_events',array['consumer_id','request_intent','reason'],false,v_ref);
  update control.nexloop_operator_persona_assignment_events set consumer_id=v_pseudonym,request_intent=null,reason=null
   where tenant_id=p_tenant and world=p_world and consumer_id=e.consumer_id;
  get diagnostics n2=row_count;
  perform runtime.nexloop_erasure_close();
  delete from control.nexloop_consumer_personas where tenant_id=p_tenant and world=p_world and consumer_id=e.consumer_id;
  perform runtime.nexloop_erasure_open('control.nexloop_consumer_persona_events',array['consumer_id','request_intent','reason'],false,v_ref);
  update control.nexloop_consumer_persona_events set consumer_id=v_pseudonym,request_intent='',reason=''
   where tenant_id=p_tenant and world=p_world and consumer_id=e.consumer_id;
  get diagnostics n3=row_count;
  perform runtime.nexloop_erasure_close();
  delete from ontology.events where tenant_id=p_tenant and object_type='Consumer' and object_id=e.consumer_id;
  delete from ontology.relations where tenant_id=p_tenant and ((source_type='Consumer' and source_object_id=e.consumer_id) or (target_type='Consumer' and target_object_id=e.consumer_id));
  delete from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='Consumer' and object_id=e.consumer_id;
  get diagnostics n=row_count;
  perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,'consumer_erasure',v_ref,'consumer','consumer',e.consumer_id,'deleted',
   e.counts||jsonb_build_object('consumer_object',n,'contact_keys',m,'operator_persona_events',n2,'persona_events',n3),p_by);
  update control.nexloop_consumer_erasures set state='completed',phase=null,completed_at=clock_timestamp(),updated_at=clock_timestamp(),
   counts=counts||jsonb_build_object('consumer_object',n,'contact_keys',m,'operator_persona_events',n2,'persona_events',n3) where tenant_id=p_tenant and world=p_world and request_id=p_request;
  return jsonb_build_object('request_id',p_request,'state','completed','progress',true);
 end if;
 -- A phase is done when its batch was not full; then the next phase.
 v_next:=case when n>=p_batch then e.phase else v_phases[array_position(v_phases,e.phase)+1] end;
 update control.nexloop_consumer_erasures set phase=v_next,updated_at=clock_timestamp(),
  counts=counts||(select coalesce(jsonb_object_agg(key,coalesce((counts->>key)::bigint,0)+(value#>>'{}')::bigint),'{}'::jsonb) from jsonb_each(v_counts))
  where tenant_id=p_tenant and world=p_world and request_id=p_request;
 return jsonb_build_object('request_id',p_request,'state','purging','phase',v_next,'progress',true,'counts',v_counts);
end $$;
