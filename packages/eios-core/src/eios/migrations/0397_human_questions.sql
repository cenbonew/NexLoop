-- K4b (owner 2026-11-01; docs/design/dsh-superagent-idea-intake.md v2 §3.3, Q-2, N3; K4 design D1-D6, all as recommended): a plan
-- reevaluation Run may ask one person one structured question (run-outcome 1.1 `question`, K4a contract), and a workbench member answers
-- it through a governed Action. Nothing here mutates a business object or causes an external effect.
-- 1. control.nexloop_human_question_kinds: trusted configuration (deploy/configuration/human-question-kinds.v1.json), versioned and
--    append-only: which workbench roles may answer each question_kind, and the expiry. The Run's audience never decides who answers.
-- 2. runtime.nexloop_human_questions (one per Run, D5; foreign key to its plan outcome, deleted with it by the plan_closed retention
--    class: a question never outlives the outcome that asked it) and runtime.nexloop_human_answers (append-only revisions: an answer,
--    a correction or a withdrawal). States open / answered / withdrawn / expired / superseded.
-- 3. authz.nexloop_record_plan_outcome (0108 body, three edits registered with the copy guard): accepts 1.1, checks the question with
--    the contract's own rules (runtime.nexloop_run_question_valid) and opens it in the same transaction.
-- 4. nexloop.human_question.answer:1 (governed entry handler, human only): answer or withdraw. The handler reads the answerer's current
--    workbench role and refuses a kind that role may not answer (question_role_forbidden); owner-only kinds are always withdrawable (D3).
--    Every answer, withdrawal and expiry wakes the plan once (plan-reevaluate feed); the answer is a person's statement, never a fact.
-- 5. Expiry (D2): authz.nexloop_retention_command (the 0392 entry_tickets body, one more verb questions_expire) moves open questions past due_by to
--    expired and wakes their plan; nothing is asked again by itself. A plan version that is superseded, invalidated or closed
--    supersedes its open questions (a trigger on the append-only plan events).
-- 6. Erasure: when the Consumer erasure blanks a plan outcome ({"erased":true}), the question text, options and subject and the
--    answer texts of that Run are blanked in the same transaction (runtime.nexloop_human_question_erasure). Answerers are staff (the
--    member's principal is kept for audit like other staff actions).

-- 1. Trusted configuration ----------------------------------------------------------------------------------------------------------
create table control.nexloop_human_question_kinds (
 version integer primary key check(version>=1),definition jsonb not null check(jsonb_typeof(definition)='object'),
 definition_digest text not null check(definition_digest~'^[0-9a-f]{64}$'),published_by text not null,
 published_at timestamptz not null default clock_timestamp(),check((definition->>'version')::integer=version),
 check(definition->>'schema'='nexloop-human-question-kinds/1' and jsonb_typeof(definition->'kinds')='object'
  and (definition->>'expire_after_seconds')::integer between 3600 and 2592000)
);
alter table control.nexloop_human_question_kinds owner to nexloop_owner;
create trigger human_question_kinds_append_only before update or delete on control.nexloop_human_question_kinds for each row execute function control.nexloop_nx022_append_only();
revoke all on control.nexloop_human_question_kinds from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;
insert into control.nexloop_human_question_kinds(version,definition,definition_digest,published_by)
 values(1,'{"schema":"nexloop-human-question-kinds/1","version":1,"decision":"K4b (owner 2026-11-01, dsh intake v2 §3.3, Q-2 and K4 D1-D6, all as recommended): who may answer a question a plan reevaluation Run asks (run-outcome 1.1). The server decides from question_kind and the answerer''s current workbench role; the Run''s audience never decides it. Operators answer operational questions (schedule changes, fulfilment confirmations, customer facts); budget, releasing a contact restriction, erasure and anything unclassified (other) are the owner''s, matching the owner-only set of workbench-roles (a test keeps the two from drifting). A question of an owner-only kind is always withdrawable (D3). An open question expires after expire_after_seconds (D2, 7 days): it wakes the plan once and is never asked again by itself. Reviewers answer nothing (D6). Trusted configuration: a change is a new version of this file and a new migration; versions are only added.","expire_after_seconds":604800,"kinds":{"schedule_change":["owner","operator"],"fulfilment_confirm":["owner","operator"],"customer_fact":["owner","operator"],"budget":["owner"],"contact_release":["owner"],"erasure":["owner"],"other":["owner"]}}'::jsonb,encode(sha256(convert_to(runtime.nexloop_canonical_json('{"schema":"nexloop-human-question-kinds/1","version":1,"decision":"K4b (owner 2026-11-01, dsh intake v2 §3.3, Q-2 and K4 D1-D6, all as recommended): who may answer a question a plan reevaluation Run asks (run-outcome 1.1). The server decides from question_kind and the answerer''s current workbench role; the Run''s audience never decides it. Operators answer operational questions (schedule changes, fulfilment confirmations, customer facts); budget, releasing a contact restriction, erasure and anything unclassified (other) are the owner''s, matching the owner-only set of workbench-roles (a test keeps the two from drifting). A question of an owner-only kind is always withdrawable (D3). An open question expires after expire_after_seconds (D2, 7 days): it wakes the plan once and is never asked again by itself. Reviewers answer nothing (D6). Trusted configuration: a change is a new version of this file and a new migration; versions are only added.","expire_after_seconds":604800,"kinds":{"schedule_change":["owner","operator"],"fulfilment_confirm":["owner","operator"],"customer_fact":["owner","operator"],"budget":["owner"],"contact_release":["owner"],"erasure":["owner"],"other":["owner"]}}'::jsonb),'UTF8')),'hex'),'migration:human-question-kinds.v1');

-- The current version (the highest).
create function runtime.nexloop_human_question_kinds_current() returns jsonb
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select definition from control.nexloop_human_question_kinds order by version desc limit 1 $$;
alter function runtime.nexloop_human_question_kinds_current() owner to nexloop_owner;
revoke all on function runtime.nexloop_human_question_kinds_current() from public;

-- 2. Questions and answers ----------------------------------------------------------------------------------------------------------
create table runtime.nexloop_human_questions (
 tenant_id text not null,world text not null,question_id uuid not null default gen_random_uuid(),
 run_id uuid not null,plan_id uuid not null,plan_version integer not null,
 audience text not null check(audience in ('owner','operator')),
 question_kind text not null check(question_kind~'^[a-z][a-z_]{0,31}$'),
 text text not null check(char_length(text)<=300),options jsonb not null check(jsonb_typeof(options)='array' and jsonb_array_length(options)<=4),
 subject_ref text check(subject_ref is null or char_length(subject_ref) between 3 and 512),withdrawable boolean not null,
 kinds_version integer not null references control.nexloop_human_question_kinds(version),
 state text not null default 'open' check(state in ('open','answered','withdrawn','expired','superseded')),
 revision integer not null default 1 check(revision>=1),
 created_at timestamptz not null default clock_timestamp(),due_by timestamptz not null,updated_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,question_id),unique(tenant_id,world,run_id),check(due_by>created_at),
 foreign key(tenant_id,world,run_id) references runtime.nexloop_plan_outcomes(tenant_id,world,run_id) on delete cascade
);
create index nexloop_human_questions_open on runtime.nexloop_human_questions(tenant_id,world,state,due_by) where state='open';
create index nexloop_human_questions_plan on runtime.nexloop_human_questions(tenant_id,world,plan_id,plan_version);
create table runtime.nexloop_human_answers (
 answer_id bigint generated always as identity primary key,tenant_id text not null,world text not null,question_id uuid not null,
 revision integer not null check(revision>=2),answer_kind text not null check(answer_kind in ('option','text','withdraw')),
 option_index integer check(option_index between 0 and 3),text text check(text is null or char_length(text)<=500),
 principal_id text not null,role text not null check(role in ('owner','operator')),request_intent text not null,
 recorded_at timestamptz not null default clock_timestamp(),unique(tenant_id,world,question_id,revision),
 check((answer_kind='option')=(option_index is not null)),check(answer_kind<>'withdraw' or text is null),
 foreign key(tenant_id,world,question_id) references runtime.nexloop_human_questions(tenant_id,world,question_id) on delete cascade
);
-- Answers are append-only: a correction or a withdrawal is a new revision. Only the erasure (registered columns blanked) or the
-- deletion of the plan outcome that asked (the plan_closed retention class, cascading) changes them.
create function runtime.nexloop_human_answers_guard() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if tg_op='DELETE' and (runtime.nexloop_erasure_permits(current_user,'runtime.nexloop_human_answers','DELETE',to_jsonb(old),null)
   or runtime.nexloop_erasure_permits(current_user,'runtime.nexloop_plan_outcomes','DELETE',null,null)) then return old;end if;
 if tg_op='UPDATE' and runtime.nexloop_erasure_permits(current_user,'runtime.nexloop_human_answers','UPDATE',to_jsonb(old),to_jsonb(new)) then return new;end if;
 raise exception 'human answers are append-only' using errcode='22023';
end $$;
create trigger nexloop_human_answers_append_only before update or delete on runtime.nexloop_human_answers for each row execute function runtime.nexloop_human_answers_guard();
do $tables$
declare t text;
begin
 foreach t in array array['nexloop_human_questions','nexloop_human_answers'] loop
  execute format('alter table runtime.%I owner to nexloop_owner',t);
  execute format('alter table runtime.%I enable row level security',t);
  execute format('alter table runtime.%I force row level security',t);
  execute format('create policy tenant_boundary on runtime.%I to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on runtime.%I from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator',t);
 end loop;
end $tables$;
alter function runtime.nexloop_human_answers_guard() owner to nexloop_owner;

-- The run-outcome 1.1 question, by the contract's rules (packages/contracts/run-outcome.schema.json): only on 1.1, only for escalate and
-- needs_information, exactly six fields, a known kind of the current configuration.
create function runtime.nexloop_run_question_valid(o jsonb) returns boolean
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp as $$
declare q jsonb:=o->'question';
begin
 return coalesce(o->>'schema_version','')='1.1' and coalesce(o->>'kind','') in ('escalate','needs_information') and jsonb_typeof(q)='object'
  and (select array_agg(k order by k) from jsonb_object_keys(q) k)=array['audience','options','question_kind','subject_ref','text','withdrawable']
  and coalesce(q->>'audience','') in ('owner','operator')
  and jsonb_typeof(q->'question_kind')='string' and runtime.nexloop_human_question_kinds_current()->'kinds' ? (q->>'question_kind')
  and q->>'question_kind' in ('schedule_change','fulfilment_confirm','customer_fact','budget','contact_release','erasure','other')
  and jsonb_typeof(q->'text')='string' and char_length(q->>'text') between 1 and 300
  and jsonb_typeof(q->'options')='array' and jsonb_array_length(q->'options')<=4
  and not exists(select 1 from jsonb_array_elements(q->'options') x where jsonb_typeof(x)<>'string' or char_length(x#>>'{}') not between 1 and 60)
  and (select count(distinct x) from jsonb_array_elements(q->'options') x)=jsonb_array_length(q->'options')
  and (jsonb_typeof(q->'subject_ref')='null' or (jsonb_typeof(q->'subject_ref')='string' and char_length(q->>'subject_ref') between 3 and 512
   and (q->>'subject_ref')~'^[A-Za-z][A-Za-z0-9_.-]*:[^\s]+$'))
  and jsonb_typeof(q->'withdrawable')='boolean';
end $$;
alter function runtime.nexloop_run_question_valid(jsonb) owner to nexloop_owner;
revoke all on function runtime.nexloop_run_question_valid(jsonb) from public;

-- Opens the question of a recorded outcome (called by the outcome port in its transaction). D3: an owner-only kind is always withdrawable.
create function runtime.nexloop_human_question_open(p_tenant text,p_world text,p_run uuid,p_plan uuid,p_version integer,q jsonb) returns uuid
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);k jsonb;v integer;roles jsonb;v_id uuid;
begin
 select version,definition into v,k from control.nexloop_human_question_kinds order by version desc limit 1;
 roles:=k->'kinds'->(q->>'question_kind');
 if roles is null then raise exception 'human question: kind_unavailable' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 insert into runtime.nexloop_human_questions(tenant_id,world,run_id,plan_id,plan_version,audience,question_kind,text,options,subject_ref,
   withdrawable,kinds_version,due_by)
  values(p_tenant,p_world,p_run,p_plan,p_version,q->>'audience',q->>'question_kind',q->>'text',q->'options',q->>'subject_ref',
   (q->>'withdrawable')::boolean or roles=jsonb_build_array('owner'),v,
   clock_timestamp()+make_interval(secs=>(k->>'expire_after_seconds')::integer))
  returning question_id into v_id;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v_id;
end $$;
alter function runtime.nexloop_human_question_open(text,text,uuid,uuid,integer,jsonb) owner to nexloop_owner;
revoke all on function runtime.nexloop_human_question_open(text,text,uuid,uuid,integer,jsonb) from public;

-- Wakes the question's plan once, if that plan is still active (cause external: a person or the clock moved the question).
create function runtime.nexloop_human_question_wake(p_tenant text,p_world text,p_plan uuid,p_question uuid,p_kind text) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare cur record;
begin
 select * into cur from runtime.nexloop_plan_current(p_tenant,p_world,p_plan);
 if found and cur.status='active' then
  perform authz.nexloop_plan_feed_touch(p_tenant,p_world,p_plan,jsonb_build_object('kind',p_kind,'cause','external','question_id',p_question,'at',clock_timestamp()),null);
 end if;
end $$;
alter function runtime.nexloop_human_question_wake(text,text,uuid,uuid,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_human_question_wake(text,text,uuid,uuid,text) from public;

-- 3. The outcome port accepts 1.1 (0108 body; the three edits are in tests/support/human_questions_edits.py).
create or replace function authz.nexloop_record_plan_outcome(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-plan-outcome-v1','eios:action:nexloop.plan.outcome:1',
  array['nexloop_scheduler','nexloop_domain_worker']);
 c jsonb:=p_payload::jsonb;o jsonb:=c->'outcome';act authz.nexloop_runtime_activations%rowtype;j runtime.jobs%rowtype;b authz.nexloop_runtime_run_bindings%rowtype;
 pr runtime.nexloop_plan_runs%rowtype;cur record;p runtime.nexloop_plans%rowtype;prior runtime.nexloop_plan_outcomes%rowtype;digest text;
 v_new integer;v_strategy text;v_goal_id text;v_current integer;snap jsonb;due timestamptz;
begin
 select * into act from authz.nexloop_runtime_activations where activation_ref=c->>'activation_ref' and tenant_id=t and world=p_world;
 if not found then raise exception 'plan outcome activation unavailable' using errcode='42501';end if;
 select * into j from runtime.jobs where tenant_id=t and job_id=act.task_id for share;
 if not found or j.status<>'running' or j.lease_until<=clock_timestamp() or j.lease_credential is distinct from p_digest
  or act.lease_credential is distinct from p_digest or act.fence is distinct from j.fencing_token then
  raise exception 'plan outcome lease unavailable' using errcode='42501';end if;
 select * into b from authz.nexloop_runtime_run_bindings where run_id=act.run_id;
 if b.command_digest is distinct from encode(sha256(convert_to(c->>'command_text','UTF8')),'hex')
  or (c->>'command_text')::jsonb->>'run_id' is distinct from act.run_id::text then raise exception 'plan outcome command mismatch' using errcode='42501';end if;
 select * into pr from runtime.nexloop_plan_runs where tenant_id=t and world=p_world and run_id=act.run_id;
 if not found then raise exception 'not a plan reevaluation Run' using errcode='42501';end if;
 -- Contract run-outcome 1.0 or 1.1 (packages/contracts/run-outcome.schema.json), exact; 1.1 may carry one question (K4b).
 if jsonb_typeof(o) is distinct from 'object'
  or (select array_agg(k order by k) from jsonb_object_keys(o-'question') k) is distinct from array['evidence_refs','intent_ref','kind','plan_update','reasons','reassess_at','schema_version']
  or coalesce(o->>'schema_version','') not in ('1.0','1.1') or (o ? 'question' and not runtime.nexloop_run_question_valid(o)) or o->>'kind' not in ('no_action','needs_information','waiting_external','escalate','plan_update','action_intent')
  or jsonb_typeof(o->'reasons') is distinct from 'array' or jsonb_array_length(o->'reasons')>8
  or exists(select 1 from jsonb_array_elements(o->'reasons') r where jsonb_typeof(r)<>'string' or length(r#>>'{}') not between 1 and 500)
  or jsonb_typeof(o->'evidence_refs') is distinct from 'array' or jsonb_array_length(o->'evidence_refs')>32
  or exists(select 1 from jsonb_array_elements(o->'evidence_refs') r where jsonb_typeof(r)<>'string' or (r#>>'{}')!~'^[A-Za-z][A-Za-z0-9_.-]*:\S+$' or length(r#>>'{}') not between 3 and 512)
  or (jsonb_typeof(o->'reassess_at')<>'null' and (jsonb_typeof(o->'reassess_at')<>'string' or (o->>'reassess_at')!~'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|\+00:00)$'
     or (o->>'reassess_at')::timestamptz>clock_timestamp()+interval '366 days'))
  or (jsonb_typeof(o->'intent_ref')<>'null' and coalesce(o->>'intent_ref','')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
  or ((o->>'kind')='plan_update')<>(jsonb_typeof(o->'plan_update')='object')
  or (jsonb_typeof(o->'plan_update')='object' and ((select array_agg(k order by k) from jsonb_object_keys(o->'plan_update') k) is distinct from array['steps','strategy']
     or not runtime.nexloop_plan_steps_valid(o->'plan_update'->'steps')
     or (jsonb_typeof(o->'plan_update'->'strategy')<>'null' and (jsonb_typeof(o->'plan_update'->'strategy')<>'string' or length(o->'plan_update'->>'strategy') not between 1 and 8192))))
  or ((o->>'kind')='action_intent' and jsonb_typeof(o->'intent_ref')<>'string')
  or ((o->>'kind')='waiting_external' and jsonb_typeof(o->'reassess_at')<>'string' and jsonb_typeof(o->'intent_ref')<>'string') then
  raise exception 'run outcome invalid' using errcode='22023';end if;
 -- An intent named by the outcome must have been submitted by this very Run.
 if jsonb_typeof(o->'intent_ref')='string' and not exists(select 1 from runtime.nexloop_effect_submissions s where s.intent_id=(o->>'intent_ref')::uuid and s.run_id=act.run_id) then
  raise exception 'outcome intent not submitted by this Run' using errcode='42501';end if;
 digest:=encode(sha256(convert_to(o::text,'UTF8')),'hex');
 select * into prior from runtime.nexloop_plan_outcomes where tenant_id=t and world=p_world and run_id=act.run_id;
 if found then
  if prior.outcome_digest<>digest then raise exception 'run outcome already recorded' using errcode='40001';end if;
  return jsonb_build_object('recorded',true,'replay',true,'plan_id',prior.plan_id,'version',prior.version,'kind',prior.kind,'new_version',prior.new_version);
 end if;
 select * into cur from runtime.nexloop_plan_current(t,p_world,pr.plan_id);
 if cur.status<>'active' or cur.version<>pr.version then raise exception 'plan version not current' using errcode='40001';end if;
 -- NX-025: a goal in the plan's chain published a new version after this Run was linked: the Run reasoned on a goal
 -- that is no longer current; its outcome is refused (the goal_version_stale trigger already queued a fresh reevaluation).
 if exists(select 1 from runtime.nexloop_plans p2 cross join lateral jsonb_array_elements_text(p2.recipe->'goal_chain') g(goal_id)
   join control.nexloop_goal_versions gv on gv.tenant_id=p2.tenant_id and gv.world=p2.world and gv.goal_id=g.goal_id
   where p2.tenant_id=t and p2.world=p_world and p2.plan_id=pr.plan_id and p2.version=pr.version and gv.published_at>pr.created_at) then
  raise exception 'goal changed since the Run was linked' using errcode='40001';end if;
 select * into p from runtime.nexloop_plans where tenant_id=t and world=p_world and plan_id=pr.plan_id and version=pr.version;
 if o->>'kind'='plan_update' then
  v_new:=p.version+1;v_goal_id:=split_part(substr(p.goal_version_ref,6),'@',1);
  select g.current_version into v_current from control.nexloop_goals g where g.tenant_id=t and g.world=p_world and g.goal_id=v_goal_id;
  snap:=runtime.nexloop_plan_snapshot(p_digest,p_world,p.consumer_id,v_goal_id);
  v_strategy:=p.strategy_ref;
  if jsonb_typeof(o->'plan_update'->'strategy')='string' then
   insert into runtime.nexloop_strategies(tenant_id,world,strategy_id,version,consumer_id,goal_version_ref,content,created_by,created_by_run)
    select t,p_world,coalesce(split_part(substr(p.strategy_ref,10),'@',1)::uuid,p.plan_id),
     coalesce((select max(x.version) from runtime.nexloop_strategies x where x.tenant_id=t and x.world=p_world and x.strategy_id=coalesce(split_part(substr(p.strategy_ref,10),'@',1)::uuid,p.plan_id)),0)+1,
     p.consumer_id,'goal:'||v_goal_id||'@'||v_current,o->'plan_update'->>'strategy','run',act.run_id
    returning 'strategy:'||strategy_id||'@'||version into v_strategy;
  end if;
  insert into runtime.nexloop_plans(tenant_id,world,plan_id,version,consumer_id,goal_version_ref,strategy_ref,context_strategy_ref,recipe,settings,control_snapshot,created_by,created_by_run)
   values(t,p_world,p.plan_id,v_new,p.consumer_id,'goal:'||v_goal_id||'@'||v_current,v_strategy,p.context_strategy_ref,
    p.recipe||jsonb_build_object('goal_chain',to_jsonb(control.nexloop_nx022_goal_chain(t,p_world,v_goal_id,v_current))),p.settings,snap,'run',act.run_id);
  perform runtime.nexloop_plan_insert_steps(t,p_world,p.plan_id,v_new,o->'plan_update'->'steps');
  insert into runtime.nexloop_plan_events(tenant_id,world,plan_id,version,status,reason,run_id) values(t,p_world,p.plan_id,p.version,'superseded','plan_update',act.run_id);
  insert into runtime.nexloop_plan_events(tenant_id,world,plan_id,version,status,reason,run_id) values(t,p_world,p.plan_id,v_new,'active','plan_update',act.run_id);
 end if;
 insert into runtime.nexloop_plan_outcomes(tenant_id,world,run_id,plan_id,version,kind,outcome,outcome_digest,intent_ref,new_version)
  values(t,p_world,act.run_id,p.plan_id,p.version,o->>'kind',o,digest,(o->>'intent_ref')::uuid,v_new);
 if o ? 'question' then perform runtime.nexloop_human_question_open(t,p_world,act.run_id,p.plan_id,p.version,o->'question');end if;
 if v_new is not null then perform runtime.nexloop_plan_mark_due(t,p_world,p.plan_id,v_new,'self');
 elsif jsonb_typeof(o->'reassess_at')='string' then
  due:=greatest((o->>'reassess_at')::timestamptz,clock_timestamp()+make_interval(secs=>(p.settings->>'min_reassess_seconds')::integer));
  perform authz.nexloop_plan_feed_touch(t,p_world,p.plan_id,jsonb_build_object('kind','reassess_due','cause','self','due',due,'run_id',act.run_id,'at',clock_timestamp()),due);
 end if;
 return jsonb_build_object('recorded',true,'replay',false,'plan_id',p.plan_id,'version',p.version,'kind',o->>'kind','new_version',v_new);
end $$;

-- 4. The answer Action (governed entry handler) ----------------------------------------------------------------------------------
-- answer: option_index (when the question offered options) or text (when it offered none); a later answer is a correction (a new
-- revision). withdraw: only an answered, withdrawable question. Who may answer: the answerer's current workbench role must be listed
-- for the question's kind in the configuration the question was opened with; the Run's audience is never read here.
create function runtime.nexloop_governed_answer_human_question(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare q runtime.nexloop_human_questions;v_role text;v_roles jsonb;v_kind text;v_to text;
begin
 if body->>'operation' is distinct from 'answer_human_question'
  or coalesce(body->>'question_id','')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
  or coalesce(body->>'answer_kind','') not in ('answer','withdraw')
  or jsonb_typeof(coalesce(body->'option_index','null'::jsonb)) not in ('null','number')
  or jsonb_typeof(coalesce(body->'text','null'::jsonb)) not in ('null','string') then
  raise exception 'human question: invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into q from runtime.nexloop_human_questions where tenant_id=p_tenant and world=p_world and question_id=(body->>'question_id')::uuid for update;
 if not found then raise exception 'human question: not_found' using errcode='22023';end if;
 v_role:=control.nexloop_workbench_role(p_tenant,p_principal);
 select definition->'kinds'->q.question_kind into v_roles from control.nexloop_human_question_kinds where version=q.kinds_version;
 perform set_config('eios.tenant_id',p_tenant,true);
 if v_role is null or v_roles is null or not (v_roles ? v_role) then
  raise exception 'human question: question_role_forbidden' using errcode='42501';end if;
 if body->>'answer_kind'='withdraw' then
  if jsonb_typeof(coalesce(body->'option_index','null'::jsonb))<>'null' or jsonb_typeof(coalesce(body->'text','null'::jsonb))<>'null' then
   raise exception 'human question: invalid' using errcode='22023';end if;
  if q.state<>'answered' then raise exception 'human question: not_answered' using errcode='22023';end if;
  if not q.withdrawable then raise exception 'human question: not_withdrawable' using errcode='22023';end if;
  v_kind:='withdraw';v_to:='withdrawn';
 else
  if q.state not in ('open','answered','withdrawn') then raise exception 'human question: not_open' using errcode='22023';end if;
  if q.state='open' and q.due_by<=clock_timestamp() then raise exception 'human question: expired' using errcode='22023';end if;
  if jsonb_array_length(q.options)>0 then
   if jsonb_typeof(coalesce(body->'option_index','null'::jsonb))<>'number' or jsonb_typeof(coalesce(body->'text','null'::jsonb))<>'null'
    or (body->>'option_index')!~'^[0-3]$' or (body->>'option_index')::integer>=jsonb_array_length(q.options) then
    raise exception 'human question: invalid_answer' using errcode='22023';end if;
   v_kind:='option';
  else
   if jsonb_typeof(coalesce(body->'option_index','null'::jsonb))<>'null' or jsonb_typeof(coalesce(body->'text','null'::jsonb))<>'string'
    or char_length(body->>'text') not between 1 and 500 then
    raise exception 'human question: invalid_answer' using errcode='22023';end if;
   v_kind:='text';
  end if;
  v_to:='answered';
 end if;
 insert into runtime.nexloop_human_answers(tenant_id,world,question_id,revision,answer_kind,option_index,text,principal_id,role,request_intent)
  values(p_tenant,p_world,q.question_id,q.revision+1,v_kind,case when v_kind='option' then (body->>'option_index')::integer end,
   case when v_kind='text' then body->>'text' end,p_principal,v_role,p_intent);
 update runtime.nexloop_human_questions set state=v_to,revision=q.revision+1,updated_at=clock_timestamp()
  where tenant_id=p_tenant and world=p_world and question_id=q.question_id;
 perform runtime.nexloop_human_question_wake(p_tenant,p_world,q.plan_id,q.question_id,case v_to when 'withdrawn' then 'question_withdrawn' else 'question_answered' end);
 return jsonb_build_object('question_id',q.question_id,'state',v_to,'revision',q.revision+1);
end $$;
alter function runtime.nexloop_governed_answer_human_question(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function runtime.nexloop_governed_answer_human_question(text,text,text,text,text,jsonb) from public;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('human_question.answer','answer_human_question','human','runtime.nexloop_governed_answer_human_question(text,text,text,text,text,jsonb)','NX-024');

-- 5. Expiry and supersession ------------------------------------------------------------------------------------------------------
create function runtime.nexloop_human_questions_expire(p_tenant text,p_world text,p_batch integer) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare x record;n integer:=0;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 for x in select question_id,plan_id,revision from runtime.nexloop_human_questions where tenant_id=p_tenant and world=p_world and state='open'
   and due_by<=clock_timestamp() order by due_by limit greatest(1,least(coalesce(p_batch,100),1000)) for update skip locked loop
  update runtime.nexloop_human_questions set state='expired',revision=x.revision+1,updated_at=clock_timestamp()
   where tenant_id=p_tenant and world=p_world and question_id=x.question_id;
  perform runtime.nexloop_human_question_wake(p_tenant,p_world,x.plan_id,x.question_id,'question_expired');
  perform set_config('eios.tenant_id',p_tenant,true);
  n:=n+1;
 end loop;
 return jsonb_build_object('expired',n);
end $$;
alter function runtime.nexloop_human_questions_expire(text,text,integer) owner to nexloop_owner;
revoke all on function runtime.nexloop_human_questions_expire(text,text,integer) from public;

-- A plan version superseded, invalidated or closed takes its open questions with it (answered ones keep their history).
create function runtime.nexloop_human_questions_on_plan_event() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);
begin
 perform set_config('eios.tenant_id',new.tenant_id,true);
 update runtime.nexloop_human_questions set state='superseded',revision=revision+1,updated_at=clock_timestamp()
  where tenant_id=new.tenant_id and world=new.world and plan_id=new.plan_id and plan_version=new.version and state='open';
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return null;
end $$;
alter function runtime.nexloop_human_questions_on_plan_event() owner to nexloop_owner;
revoke all on function runtime.nexloop_human_questions_on_plan_event() from public;
create trigger nexloop_human_questions_supersede after insert on runtime.nexloop_plan_events for each row
 when (new.status in ('superseded','invalidated','closed')) execute function runtime.nexloop_human_questions_on_plan_event();

create or replace function authz.nexloop_retention_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-retention-v1',
  'eios:action:nexloop.retention.execute:1',array['nexloop_domain_worker']);c jsonb:=p_payload::jsonb;s jsonb:=runtime.nexloop_retention_settings_current();
 v jsonb;v_principal text:=(p_text::jsonb)->>'principal_id';v_interval integer:=(s->'sweep'->>'interval_seconds')::integer;
begin
 perform set_config('eios.tenant_id',t,true);
 if c->>'verb'='due' then
  select coalesce(jsonb_agg(k order by k),'[]'::jsonb) into v from unnest(runtime.nexloop_retention_classes()) k
   where not exists(select 1 from runtime.nexloop_retention_sweeps w where w.tenant_id=t and w.world=p_world and w.item_class=k
     and w.last_completed_at is not null and w.last_completed_at>=coalesce(w.last_started_at,w.last_completed_at)
     and w.last_completed_at>clock_timestamp()-make_interval(secs=>v_interval));
  return jsonb_build_object('due',v,'batch',(s->'sweep'->>'batch')::integer,'max_passes',(s->'sweep'->>'max_passes')::integer,'settings_version',(s->>'version')::integer);
 elsif c->>'verb'='sweep' then
  if not (c->>'item_class'=any(runtime.nexloop_retention_classes())) or coalesce(c->>'sweep_ref','')!~'^sweep:[0-9a-f-]{36}$' then
   raise exception 'retention sweep invalid' using errcode='22023';end if;
  insert into runtime.nexloop_retention_sweeps(tenant_id,world,item_class,last_started_at) values(t,p_world,c->>'item_class',clock_timestamp())
   on conflict(tenant_id,world,item_class) do update set last_started_at=clock_timestamp();
  v:=runtime.nexloop_retention_sweep(t,p_world,c->>'item_class',(s->'sweep'->>'batch')::integer,(c->>'sweep_ref')||':'||(c->>'item_class'),'service:'||v_principal);
  perform set_config('eios.tenant_id',t,true);
  if not (v->>'more')::boolean then
   update runtime.nexloop_retention_sweeps set last_completed_at=clock_timestamp(),last_counts=v->'counts'
    where tenant_id=t and world=p_world and item_class=c->>'item_class';
  end if;
  return v;
 elsif c->>'verb'='approvals_expire' then
  -- NX-056 A3: end waiting approvals past due_by (never executes one).
  return control.nexloop_approval_expire_due(t,p_world,(s->'sweep'->>'batch')::integer)
   ||control.nexloop_approval_redact_due(t,p_world,(s->'sweep'->>'batch')::integer);  -- A5 (0331): frozen bodies past retention
 elsif c->>'verb'='member_login_clear' then
  -- NX-058 slice E2 (design §2.5, E6 = A): the failed-login snapshots of members erased 90 days ago or more.
  return control.nexloop_workbench_member_login_clear(t,(s->'sweep'->>'batch')::integer);
 elsif c->>'verb'='entry_tickets_clear' then
  -- S81 (0392, ADR-043): entry tickets and their events older than 30 days.
  return control.nexloop_entry_tickets_clear(t,(s->'sweep'->>'batch')::integer);
 elsif c->>'verb'='questions_expire' then
  -- K4b (D2): open human questions past due_by expire and wake their plan once; nothing is asked again.
  return runtime.nexloop_human_questions_expire(t,p_world,(s->'sweep'->>'batch')::integer);
 elsif c->>'verb'='status' then
  select coalesce(jsonb_agg(jsonb_build_object('item_class',k.item_class,'action',k.action,'tombstones',k.n) order by k.item_class,k.action),'[]'::jsonb) into v
   from (select item_class,action,count(*) n from runtime.nexloop_erasure_tombstones where tenant_id=t and world=p_world group by item_class,action) k;
  return jsonb_build_object('world',p_world,'settings_version',(s->>'version')::integer,'tombstones',v,
   'sweeps',(select coalesce(jsonb_agg(jsonb_build_object('item_class',w.item_class,'last_completed_at',runtime.nexloop_commercial_ts(w.last_completed_at),
     'last_counts',w.last_counts) order by w.item_class),'[]'::jsonb) from runtime.nexloop_retention_sweeps w where w.tenant_id=t and w.world=p_world),
   'warnings',runtime.nexloop_retention_warnings(t,p_world));
 end if;
 raise exception 'retention command invalid' using errcode='22023';
end $$;

-- 6. Erasure propagation: a blanked plan outcome blanks its question and the answer texts.
create function runtime.nexloop_human_question_erasure() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);
begin
 perform set_config('eios.tenant_id',new.tenant_id,true);
 perform runtime.nexloop_erasure_open('runtime.nexloop_human_answers',array['text'],false,'erasure:plan_outcome:'||new.run_id);
 update runtime.nexloop_human_questions set text='',options='[]'::jsonb,subject_ref=null
  where tenant_id=new.tenant_id and world=new.world and run_id=new.run_id and (text<>'' or options<>'[]'::jsonb or subject_ref is not null);
 update runtime.nexloop_human_answers a set text='' from runtime.nexloop_human_questions q
  where q.tenant_id=new.tenant_id and q.world=new.world and q.run_id=new.run_id
   and a.tenant_id=q.tenant_id and a.world=q.world and a.question_id=q.question_id and coalesce(a.text,'')<>'';
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return null;
end $$;
alter function runtime.nexloop_human_question_erasure() owner to nexloop_owner;
revoke all on function runtime.nexloop_human_question_erasure() from public;
create trigger nexloop_human_question_erasure after update of outcome on runtime.nexloop_plan_outcomes for each row
 when (new.outcome ? 'erased' and not (old.outcome ? 'erased')) execute function runtime.nexloop_human_question_erasure();
