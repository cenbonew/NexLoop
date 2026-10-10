-- NX-028 slice 1 (design §3, §15.2): the owner workbench's human read port.
--
-- authz.nexloop_workbench_read: protocol nexloop-workbench-read-v1, an actual browser Human session of the tenant's workbench
-- application only (the opposite of the 0109/0111 service ports), a current workbench member whose role carries the verb's read
-- Action, never a customer principal, and the caller's current EXECUTE on that Action (assert_action_authority). Each verb needs
-- one read Action:
--   nexloop.workbench.read:1  goals, consumers, consumer, conversation, plans, actions, takeovers, settings (owner role only)
--   nexloop.commitment.read:1 commitments, commitment
--   nexloop.contact.read:1    contact
-- Message content (bodies, actors, the promised words, a refusal hit's matched text) is NOT a workbench read: the API returns it only
-- where the caller also holds that Message's READ (the existing 0077/0086 derivation or configured grants), so the port hands out the
-- message references and the caller's backend reads the content through AuthorizedObjectReader.
-- Reuses runtime.nexloop_commitment_view (0111), the 0109 restriction tables and the NX-051 projection (0114) without copying them.
-- The dispatch prediction is slice 2's control.nexloop_intent_dispatch_prediction(text,text,uuid) (0116); until it exists every
-- intent's prediction is reported unavailable, never guessed.

create function runtime.nexloop_workbench_prediction(p_tenant text,p_world text,p_intent uuid) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v jsonb;
begin
 if to_regprocedure('control.nexloop_intent_dispatch_prediction(text,text,uuid)') is null then
  return jsonb_build_object('status','unavailable');end if;
 execute 'select control.nexloop_intent_dispatch_prediction($1,$2,$3)' into v using p_tenant,p_world,p_intent;
 return jsonb_build_object('status','ok','prediction',v);
exception when others then return jsonb_build_object('status','unavailable');
end $$;
alter function runtime.nexloop_workbench_prediction(text,text,uuid) owner to nexloop_owner;
revoke all on function runtime.nexloop_workbench_prediction(text,text,uuid) from public;

create function authz.nexloop_workbench_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;ident jsonb;cfg control.nexloop_workbench_configurations;
 v_verb text:=c->>'verb';v_action text;v_tenant text;v_principal text;v_role text;v_limit integer;v_consumer text;v_conversation text;
begin
 v_action:=case when v_verb in ('goals','consumers','consumer','conversation','plans','actions','takeovers','settings') then 'eios:action:nexloop.workbench.read:1'
  when v_verb in ('commitments','commitment') then 'eios:action:nexloop.commitment.read:1'
  when v_verb='contact' then 'eios:action:nexloop.contact.read:1' end;
 if session_user is distinct from 'nexloop_api' or p_world is distinct from 'real' or v_action is null
  or octet_length(p_text)>1048576 or octet_length(p_payload)>8192
  or a->>'protocol' is distinct from 'nexloop-workbench-read-v1' or a->>'operation' is distinct from 'execute'
  or a->>'action_resource' is distinct from v_action or a->>'resource_id' is distinct from v_action
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'workbench read unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-workbench-read-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'workbench read unavailable' using errcode='42501';end if;
 -- An actual browser Human session, never a service or Run credential.
 if not exists(select 1 from authz.nexloop_browser_token_realms where token_digest=p_digest) then
  raise exception 'workbench read unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_browser_identity_snapshot(p_digest,p_world);
 v_tenant:=ident->'binding'->>'tenant_id';v_principal:=ident->'binding'->>'subject_principal_id';
 if a->>'tenant_id' is distinct from v_tenant or a->>'principal_id' is distinct from v_principal or ident->'binding'->>'subject_kind' is distinct from 'human' then
  raise exception 'workbench read unavailable' using errcode='42501';end if;
 cfg:=control.nexloop_workbench_current(v_tenant);v_role:=cfg.members->>v_principal;
 if cfg.tenant_id is null or ident->>'browser_application_id' is distinct from cfg.application_id or v_role is null
  or not (cfg.roles->v_role ? v_action) or (v_verb='settings' and v_role<>'owner')
  or control.nexloop_is_customer_principal(v_tenant,v_principal) then
  raise exception 'workbench read forbidden' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 perform set_config('eios.tenant_id',v_tenant,true);
 v_limit:=coalesce((c->>'limit')::integer,50);
 if v_limit not between 1 and 200 then raise exception 'workbench read invalid' using errcode='22023';end if;
 if c ? 'consumer_id' then v_consumer:=c->>'consumer_id';if coalesce(v_consumer,'')!~'^[a-f0-9]{64}$' then raise exception 'workbench read invalid' using errcode='22023';end if;end if;

 if v_verb='goals' then
  return jsonb_build_object(
   'goals',coalesce((select jsonb_agg(jsonb_build_object('goal_id',g.goal_id,'goal_kind',g.goal_kind,'parent_goal_id',g.parent_goal_id,'owner_principal_id',g.owner_principal_id,
     'current_version',g.current_version,'objective',v.objective,'period_start',v.period_start,'period_end',v.period_end,'priority',v.priority,'budget',v.budget,
     'constraints',v.constraints,'role_ref',v.role_ref,'publisher_kind',v.publisher_kind,'change_summary',v.change_summary,'published_at',v.published_at,
     'control_revision',v.control_revision,
     'key_results',(select coalesce(jsonb_agg(jsonb_build_object('kr_key',kr.kr_key,'metric_id',kr.metric_id,'metric_version',kr.metric_version,'target',kr.target::text,
        'direction',kr.direction,'window_start',kr.window_start,'window_end',kr.window_end) order by kr.kr_key),'[]'::jsonb)
       from control.nexloop_key_results kr where kr.tenant_id=g.tenant_id and kr.world=g.world and kr.goal_id=g.goal_id and kr.goal_version=g.current_version))
     order by g.goal_kind,g.goal_id)
    from control.nexloop_goals g join control.nexloop_goal_versions v on v.tenant_id=g.tenant_id and v.world=g.world and v.goal_id=g.goal_id and v.version=g.current_version
    where g.tenant_id=v_tenant and g.world=p_world),'[]'::jsonb),
   'metrics',coalesce((select jsonb_agg(jsonb_build_object('metric_id',m.metric_id,'version',m.version,'name',m.name,'aggregation',m.aggregation,'unit',m.unit,
     'currency',m.currency,'maturity_seconds',m.maturity_seconds,'refund_rule',m.refund_rule,'approved_at',m.approved_at) order by m.metric_id,m.version)
    from control.nexloop_metric_definitions m where m.tenant_id=v_tenant and m.world=p_world),'[]'::jsonb),
   'control',jsonb_build_object('revision',coalesce((select h.revision from control.nexloop_control_heads h where h.tenant_id=v_tenant and h.world=p_world),0),
    'scopes',coalesce((select jsonb_agg(jsonb_build_object('scope_kind',s.scope_kind,'scope_ref',s.scope_ref,'paused',s.paused,'revision',s.revision,'reason',s.reason,
      'updated_at',s.updated_at) order by s.scope_kind,s.scope_ref) from control.nexloop_control_scopes s where s.tenant_id=v_tenant and s.world=p_world),'[]'::jsonb),
    'events',coalesce((select jsonb_agg(e order by (e->>'revision')::bigint desc) from (select jsonb_build_object('revision',e.revision,'event_kind',e.event_kind,
      'scope_kind',e.scope_kind,'scope_ref',e.scope_ref,'detail',e.detail,'principal_id',e.principal_id,'recorded_at',e.recorded_at) e
      from control.nexloop_control_events e where e.tenant_id=v_tenant and e.world=p_world order by e.revision desc limit v_limit) x(e)),'[]'::jsonb)),
   'budgets',coalesce((select jsonb_agg(jsonb_build_object('budget_kind',b.budget_kind,'unit',b.unit,'limit_amount',b.limit_amount::text,'period_start',b.period_start,
     'period_end',b.period_end,'revision',b.revision,'consumed',(select coalesce(sum(u.amount),0)::text from control.nexloop_budget_consumption u
       where u.tenant_id=b.tenant_id and u.world=b.world and u.budget_kind=b.budget_kind and u.recorded_at>=b.period_start and u.recorded_at<b.period_end))
     order by b.budget_kind) from control.nexloop_budget_limits b where b.tenant_id=v_tenant and b.world=p_world),'[]'::jsonb));

 elsif v_verb='consumers' then
  if c ? 'after' and coalesce(c->>'after','')!~'^[a-f0-9]{64}$' then raise exception 'workbench read invalid' using errcode='22023';end if;
  return jsonb_build_object('consumers',coalesce((select jsonb_agg(x.v order by x.id) from (
    select o.object_id id,jsonb_build_object('consumer_id',o.object_id,'revision',o.nexloop_revision,
     'restricted',exists(select 1 from control.nexloop_contact_restrictions r where r.tenant_id=o.tenant_id and r.world=o.world and r.consumer_id=o.object_id and r.active),
     'open_commitments',(select count(*) from runtime.nexloop_commitments cm join ontology.objects co on co.tenant_id=cm.tenant_id and co.world=cm.world
        and co.type_name='Commitment' and co.object_id=cm.commitment_id
       where cm.tenant_id=o.tenant_id and cm.world=o.world and cm.consumer_id=o.object_id and co.properties->>'status' in ('conditional','open','in_progress','breached')),
     'active_plans',(select count(*) from runtime.nexloop_active_plans(o.tenant_id,o.world) p where p.consumer_id=o.object_id),
     'conversations',(select count(*) from runtime.nexloop_conversations cv where cv.tenant_id=o.tenant_id and cv.world=o.world and cv.consumer_id=o.object_id)) v
    from ontology.objects o where o.tenant_id=v_tenant and o.world=p_world and o.type_name='Consumer' and (not c ? 'after' or o.object_id>c->>'after')
    order by o.object_id limit v_limit) x),'[]'::jsonb));

 elsif v_verb='consumer' then
  if v_consumer is null then raise exception 'workbench read invalid' using errcode='22023';end if;
  if not exists(select 1 from ontology.objects o where o.tenant_id=v_tenant and o.world=p_world and o.type_name='Consumer' and o.object_id=v_consumer) then return null;end if;
  return jsonb_build_object('consumer_id',v_consumer,
   'restriction',(select jsonb_build_object('active',r.active,'control_revision',r.control_revision,'rule_id',r.rule_id,'rule_version',r.rule_version,
     'certainty',r.certainty,'restricted_at',r.restricted_at,'released_at',r.released_at,'released_by',r.released_by)
    from control.nexloop_contact_restrictions r where r.tenant_id=v_tenant and r.world=p_world and r.consumer_id=v_consumer),
   'paused',exists(select 1 from control.nexloop_control_scopes s where s.tenant_id=v_tenant and s.world=p_world and s.scope_kind='consumer' and s.scope_ref=v_consumer and s.paused),
   'conversations',coalesce((select jsonb_agg(jsonb_build_object('conversation_id',cv.conversation_id,'last_sequence',cv.last_sequence) order by cv.conversation_id)
     from runtime.nexloop_conversations cv where cv.tenant_id=v_tenant and cv.world=p_world and cv.consumer_id=v_consumer),'[]'::jsonb),
   'commitments',coalesce((select jsonb_agg(cm.commitment_id order by cm.prepared_at,cm.commitment_id) from runtime.nexloop_commitments cm
     where cm.tenant_id=v_tenant and cm.world=p_world and cm.consumer_id=v_consumer and cm.registered_at is not null),'[]'::jsonb));

 elsif v_verb='conversation' then
  v_conversation:=c->>'conversation_id';
  if coalesce(v_conversation,'')!~'^[a-f0-9]{64}$' then raise exception 'workbench read invalid' using errcode='22023';end if;
  if not exists(select 1 from runtime.nexloop_conversations cv where cv.tenant_id=v_tenant and cv.world=p_world and cv.conversation_id=v_conversation) then return null;end if;
  -- Order, direction, references and provider evidence (NX-051); bodies and actors are Message content (READ, see header).
  return jsonb_build_object('conversation_id',v_conversation,
   'consumer_id',(select cv.consumer_id from runtime.nexloop_conversations cv where cv.tenant_id=v_tenant and cv.world=p_world and cv.conversation_id=v_conversation),
   'messages',coalesce((select jsonb_agg(jsonb_build_object('id',m.message_id,'sequence',m.sequence,'accepted_at',m.record->>'accepted_at',
     'direction',coalesce(m.record->>'direction','inbound'),'sender_kind',coalesce(m.record->>'sender_kind','consumer'),'intent_id',m.record->>'intent_id',
     'trigger_message_id',m.record->>'trigger_message_id','reply_to_message_id',x->'reply'->'reply_to_message_id','provider',x->'provider') order by m.sequence)
    from runtime.nexloop_conversation_messages m cross join lateral runtime.nexloop_message_projection(v_tenant,p_world,m.message_id) x
    where m.tenant_id=v_tenant and m.world=p_world and m.conversation_id=v_conversation),'[]'::jsonb));

 elsif v_verb='plans' then
  return jsonb_build_object('plans',coalesce((select jsonb_agg(jsonb_build_object('plan_id',p.plan_id,'version',p.version,'consumer_id',p.consumer_id,
    'goal_version_ref',p.goal_version_ref,'strategy_ref',p.strategy_ref,'created_by',p.created_by,'created_at',p.created_at,
    'status',(runtime.nexloop_plan_current(p.tenant_id,p.world,p.plan_id)).status,
    'steps',(select coalesce(jsonb_agg(jsonb_build_object('step_key',s.step_key,'expected_result',s.expected_result,'reassess_at',s.reassess_at,'intent_ref',s.intent_ref)
      order by s.step_key),'[]'::jsonb) from runtime.nexloop_plan_steps s where s.tenant_id=p.tenant_id and s.world=p.world and s.plan_id=p.plan_id and s.version=p.version),
    'outcomes',(select coalesce(jsonb_agg(jsonb_build_object('run_id',o.run_id,'kind',o.kind,'recorded_at',o.recorded_at) order by o.recorded_at desc),'[]'::jsonb)
      from runtime.nexloop_plan_outcomes o where o.tenant_id=p.tenant_id and o.world=p.world and o.plan_id=p.plan_id)) order by p.created_at desc)
   from runtime.nexloop_plans p where p.tenant_id=v_tenant and p.world=p_world and (v_consumer is null or p.consumer_id=v_consumer)
    and p.version=(runtime.nexloop_plan_current(p.tenant_id,p.world,p.plan_id)).version),'[]'::jsonb));

 elsif v_verb='actions' then
  if c ? 'state' and c->>'state' not in ('accepted','dispatching','unknown','fulfilled','failed','confirmed') then raise exception 'workbench read invalid' using errcode='22023';end if;
  return jsonb_build_object('intents',coalesce((select jsonb_agg(x.v order by x.created_at desc,x.id) from (
    select i.created_at,i.intent_id id,jsonb_build_object('intent_id',i.intent_id,'receipt_id',i.receipt_id,'consumer_id',i.consumer_id,'action_name',i.action_name,
     'action_version',i.action_version,'state',i.state,'created_at',i.created_at,'control_revision',i.control_revision,
     'attempts',(select count(*) from runtime.nexloop_effect_attempts t where t.intent_id=i.intent_id and t.tenant_id=i.tenant_id and t.world=i.world),
     'observation',(select jsonb_build_object('provider_state',ob.provider_state,'provider_reference',ob.provider_reference,'observed_at',ob.observed_at)
       from runtime.nexloop_effect_observations ob where ob.intent_id=i.intent_id and ob.tenant_id=i.tenant_id and ob.world=i.world order by ob.observed_at desc limit 1),
     'age_seconds',floor(extract(epoch from clock_timestamp()-i.created_at))::bigint,
     'dispatch',runtime.nexloop_workbench_prediction(i.tenant_id,i.world,i.intent_id)) v
    from runtime.nexloop_effect_intents i where i.tenant_id=v_tenant and i.world=p_world and (not c ? 'state' or i.state=c->>'state')
     and (v_consumer is null or i.consumer_id=v_consumer) order by i.created_at desc limit v_limit) x),'[]'::jsonb),
   'unknown',(select jsonb_build_object('count',count(*),'oldest_age_seconds',floor(extract(epoch from clock_timestamp()-min(i.created_at)))::bigint)
     from runtime.nexloop_effect_intents i where i.tenant_id=v_tenant and i.world=p_world and i.state='unknown'));

 elsif v_verb='takeovers' then
  -- Human takeover is slice 3 (0118); until then this section is unavailable, never an empty success.
  if to_regclass('control.nexloop_takeovers') is null then return jsonb_build_object('status','unavailable','takeovers','[]'::jsonb);end if;
  return jsonb_build_object('status','unavailable','takeovers','[]'::jsonb);

 elsif v_verb='settings' then
  return jsonb_build_object('application_id',cfg.application_id,'manifest_version',cfg.manifest_version,'roles_version',cfg.roles_version,'roles',cfg.roles,
   'members',coalesce((select jsonb_agg(jsonb_build_object('principal_id',m.key,'role',m.value#>>'{}') order by m.key) from jsonb_each(cfg.members) m),'[]'::jsonb),
   'budgets',coalesce((select jsonb_agg(jsonb_build_object('budget_kind',b.budget_kind,'unit',b.unit,'limit_amount',b.limit_amount::text,'revision',b.revision) order by b.budget_kind)
     from control.nexloop_budget_limits b where b.tenant_id=v_tenant and b.world=p_world),'[]'::jsonb));

 elsif v_verb='commitments' then
  return jsonb_build_object('commitments',coalesce((select jsonb_agg(runtime.nexloop_commitment_view(v_tenant,p_world,r.commitment_id)
     ||jsonb_build_object('consumer_id',r.consumer_id,'source_message_id',r.message_id) order by r.prepared_at,r.commitment_id)
   from runtime.nexloop_commitments r join ontology.objects o on o.tenant_id=r.tenant_id and o.world=r.world and o.type_name='Commitment' and o.object_id=r.commitment_id
   where r.tenant_id=v_tenant and r.world=p_world and (v_consumer is null or r.consumer_id=v_consumer)
    and (coalesce((c->>'all')::boolean,false) or o.properties->>'status' in ('conditional','open','in_progress','breached'))),'[]'::jsonb),
   'exceptions',coalesce((select jsonb_agg(jsonb_build_object('subject_ref',x.subject_ref,'consumer_id',x.consumer_id,'reason',x.reason,'detail',x.detail,
     'raised_at',x.raised_at) order by x.raised_at,x.subject_ref,x.reason)
    from runtime.nexloop_commitment_exceptions x where x.tenant_id=v_tenant and x.world=p_world and (v_consumer is null or x.consumer_id=v_consumer)),'[]'::jsonb));

 elsif v_verb='commitment' then
  if coalesce(c->>'commitment_id','')!~'^[0-9a-f]{64}$' then raise exception 'workbench read invalid' using errcode='22023';end if;
  return (select runtime.nexloop_commitment_view(v_tenant,p_world,r.commitment_id)||jsonb_build_object('consumer_id',r.consumer_id,'source_message_id',r.message_id)
   from runtime.nexloop_commitments r where r.tenant_id=v_tenant and r.world=p_world and r.commitment_id=c->>'commitment_id');

 elsif v_verb='contact' then
  return jsonb_build_object('restrictions',coalesce((select jsonb_agg(jsonb_build_object('consumer_id',r.consumer_id,'active',r.active,'control_revision',r.control_revision,
    'message_id',r.message_id,'conversation_id',r.conversation_id,'rule_version',r.rule_version,'rule_id',r.rule_id,'certainty',r.certainty,
    'matched_text',r.matched_text,'restricted_at',r.restricted_at,'released_at',r.released_at,'released_by',r.released_by,'release_reason',r.release_reason,
    'hits',(select coalesce(jsonb_agg(jsonb_build_object('message_id',h.message_id,'rule_version',h.rule_version,'rule_id',h.rule_id,'certainty',h.certainty,
      'matched_text',h.matched_text,'recorded_at',h.recorded_at) order by h.recorded_at),'[]'::jsonb) from control.nexloop_contact_refusal_hits h
      where h.tenant_id=r.tenant_id and h.world=r.world and h.consumer_id=r.consumer_id)) order by r.restricted_at)
   from control.nexloop_contact_restrictions r where r.tenant_id=v_tenant and r.world=p_world
    and ((v_consumer is not null and r.consumer_id=v_consumer) or (v_consumer is null and (r.active or coalesce((c->>'all')::boolean,false))))),'[]'::jsonb),
   'escalations',coalesce((select jsonb_agg(jsonb_build_object('message_id',e.message_id,'conversation_id',e.conversation_id,'consumer_id',e.consumer_id,
     'reason',e.reason,'detail',e.detail,'escalated_at',e.escalated_at) order by e.escalated_at)
    from control.nexloop_reply_escalations e where e.tenant_id=v_tenant and e.world=p_world and (v_consumer is null or e.consumer_id=v_consumer)),'[]'::jsonb));
 end if;
 raise exception 'workbench read invalid' using errcode='22023';
end $$;
alter function authz.nexloop_workbench_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_read(text,text,text,text,text) from public;
grant execute on function authz.nexloop_workbench_read(text,text,text,text,text) to nexloop_api;
