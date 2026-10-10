-- NX-025 (temporary number 0108): plans in Context v6 (G4) and governed changes of snapshot objects wake their plans (G5).
--
-- G4 (dispatcher decision 3): a reevaluation Run must see the plan it reevaluates. The Consumer's active
-- plans are open work: the open_work read returns them, each as a read-only item
--   ref nexloop:plan:<plan_id>@<version>, subsection 'plan', evidence_kind 'policy', revision = version,
--   content = runtime.nexloop_plan_context (goal and strategy refs, strategy text, steps)
-- re-derived by the v6 item verifier at bind (current active version, the Consumer's own plan, under the
-- caller's current Consumer READ) and recorded in the source manifest with its content hash. Every active
-- plan of the Consumer is pinned: present in the pack, or open work declared unreadable. Plans stay in
-- runtime (not ontology); this is a projection, never a second store. The 0090/0105 open_work read, the
-- 0094 item verifier and section check are otherwise unchanged (bodies copied, plans added).

-- The formal open-work zone may now carry read-only plan policy items (0089 constraint widened, nothing else).
alter table runtime.nexloop_context_sources drop constraint nexloop_context_sources_check2;
alter table runtime.nexloop_context_sources add constraint nexloop_context_sources_open_work_kind
 check(section<>'open_work' or evidence_kind in ('formal_object','execution_state','policy'));

create function runtime.nexloop_plan_context(p_tenant text,p_world text,p_plan uuid,p_version integer) returns jsonb
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select jsonb_build_object('plan_ref','plan:'||p.plan_id||'@'||p.version,'goal_version_ref',p.goal_version_ref,'strategy_ref',p.strategy_ref,
  'strategy',(select s.content from runtime.nexloop_strategies s where s.tenant_id=p.tenant_id and s.world=p.world
    and p.strategy_ref is not null and s.strategy_id=split_part(substr(p.strategy_ref,10),'@',1)::uuid and s.version=split_part(p.strategy_ref,'@',2)::integer),
  'context_strategy_ref',p.context_strategy_ref,
  'steps',coalesce((select jsonb_agg(jsonb_build_object('step_key',x.step_key,'prerequisites',x.prerequisites,'expected_result',x.expected_result,
     'stop_if',x.stop_if,'reassess_at',x.reassess_at,'budget',x.budget,'intent_ref',x.intent_ref) order by x.step_key)
   from runtime.nexloop_plan_steps x where x.tenant_id=p.tenant_id and x.world=p.world and x.plan_id=p.plan_id and x.version=p.version),'[]'::jsonb))
 from runtime.nexloop_plans p where p.tenant_id=p_tenant and p.world=p_world and p.plan_id=p_plan and p.version=p_version
$$;
alter function runtime.nexloop_plan_context(text,text,uuid,integer) owner to nexloop_owner;
revoke all on function runtime.nexloop_plan_context(text,text,uuid,integer) from public;

create or replace function authz.nexloop_context_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;v_verb text:=c->>'verb';ident jsonb;v_tenant text;
 v_result jsonb;v_row control.nexloop_context_strategies%rowtype;proof jsonb;ref text;gate text;v_type text;v_prop text;v_value text;
 v_def jsonb;v_version integer;v_items jsonb:='[]'::jsonb;v_props jsonb;v_consumer text;
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker') or octet_length(p_text)>4194304 or octet_length(p_payload)>65536
  or a->>'protocol' is distinct from 'nexloop-context-read-v1' or v_verb not in ('strategy','definitions','open_work') or v_verb is null
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'context read unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-context-read-v1:'||p_text,'UTF8'),k,'sha256'),'hex') then
  raise exception 'context read unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);v_tenant:=ident->'binding'->>'tenant_id';
 if a->>'tenant_id' is distinct from v_tenant or a->>'world' is distinct from p_world then raise exception 'context read unavailable' using errcode='42501';end if;
 if v_verb='strategy' then
  if a->>'action_resource' is distinct from 'eios:action:nexloop.context.assemble:1' or c->>'strategy_id' !~ '^[a-z][a-z0-9_]{0,63}$'
   or (c ? 'version' and jsonb_typeof(c->'version') is distinct from 'number') then raise exception 'context read unavailable' using errcode='42501';end if;
  perform authz.nexloop_assert_context_assemble(p_digest,p_world,a,null);
  select * into v_row from control.nexloop_context_strategies where tenant_id=v_tenant and world=p_world and strategy_id=c->>'strategy_id'
   and (not c ? 'version' or version=(c->>'version')::integer) order by version desc limit 1;
  if not found then v_result:=null;else
   v_result:=jsonb_build_object('strategy_ref','context-strategy:'||v_row.strategy_id||'@'||v_row.version,'definition',v_row.definition,
    'definition_digest',v_row.definition_digest,'published_at',v_row.published_at);end if;
  perform authz.nexloop_assert_context_assemble(p_digest,p_world,a,null);
  return v_result;
 end if;
 if jsonb_typeof(a->'read_proofs') is distinct from 'array' or jsonb_array_length(a->'read_proofs')>256 then raise exception 'context read unavailable' using errcode='42501';end if;
 -- Every proof must be the caller's own, current, and for exactly the resource it names.
 for proof in select value from jsonb_array_elements(a->'read_proofs') loop
  if proof->>'tenant_id' is distinct from v_tenant or proof->>'principal_id' is distinct from ident->'binding'->>'subject_principal_id'
   or proof->>'operation' is distinct from 'read' or proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp() then
   raise exception 'context read proof invalid' using errcode='42501';end if;
  perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
 end loop;
 perform set_config('eios.tenant_id',v_tenant,true);
 if v_verb='definitions' then
  if jsonb_typeof(c->'refs') is distinct from 'array' or jsonb_array_length(c->'refs') not between 1 and 64 then raise exception 'context read unavailable' using errcode='42501';end if;
  for ref in select value#>>'{}' from jsonb_array_elements(c->'refs') loop
   v_type:=null;v_prop:=null;v_value:=null;v_props:=null;
   if ref ~ '^eios:object_type:[A-Za-z][A-Za-z0-9_]{0,63}$' then v_type:=substring(ref from 18);
   elsif ref ~ '^eios:property:[A-Za-z][A-Za-z0-9_]{0,63}/[A-Za-z][A-Za-z0-9_]{0,63}$' then
    v_type:=split_part(substring(ref from 15),'/',1);v_prop:=split_part(substring(ref from 15),'/',2);
   elsif ref ~ '^nexloop:vocabulary:[A-Za-z][A-Za-z0-9_]{0,63}/[A-Za-z][A-Za-z0-9_]{0,63}/.+$' then
    v_type:=split_part(substring(ref from 20),'/',1);v_prop:=split_part(substring(ref from 20),'/',2);
    v_value:=substring(substring(ref from 20) from length(v_type)+length(v_prop)+3);
   else raise exception 'context definition ref invalid' using errcode='22023';end if;
   -- Gates: the type always; the property for property/vocabulary refs (same as NX-021 recall gates).
   foreach gate in array array_remove(array['eios:object_type:'||v_type,case when v_prop is null then null else 'eios:property:'||v_type||'/'||v_prop end],null) loop
    if not exists(select 1 from jsonb_array_elements(a->'read_proofs') q where q->>'target_resource'=gate and q->>'resource_id'=gate) then
     raise exception 'context definition READ required' using errcode='42501';end if;
   end loop;
   select definition,version into v_def,v_version from ontology.object_type_versions where tenant_id=v_tenant and type_name=v_type order by version desc limit 1;
   if not found then raise exception 'context definition unavailable' using errcode='42501';end if;
   if v_prop is null then
    -- A type view lists only the properties the caller may READ.
    select coalesce(jsonb_agg(p->>'property_name' order by p->>'property_name'),'[]'::jsonb) into v_props from jsonb_array_elements(v_def->'properties') p
     where exists(select 1 from jsonb_array_elements(a->'read_proofs') q where q->>'target_resource'='eios:property:'||v_type||'/'||(p->>'property_name'));
    v_items:=v_items||jsonb_build_array(jsonb_build_object('ref',ref,'kind','object_type','schema_version',v_version,
     'display_name',v_def->>'display_name','description',coalesce(v_def->>'description',''),'readable_properties',v_props,
     'aliases',(select coalesce(jsonb_agg(x.alias_text order by x.alias_text),'[]'::jsonb) from ontology.nexloop_definition_aliases x where x.tenant_id=v_tenant and x.world=p_world and x.canonical_ref=ref)));
   else
    select value into v_props from jsonb_array_elements(v_def->'properties') where value->>'property_name'=v_prop;
    if v_props is null then raise exception 'context definition unavailable' using errcode='42501';end if;
    if v_value is not null and not coalesce(v_props->'type_descriptor'->'enum','[]'::jsonb) ? replace(replace(replace(replace(replace(replace(replace(v_value,'%20',' '),'%09',chr(9)),'%0A',chr(10)),'%0D',chr(13)),'%0C',chr(12)),'%0B',chr(11)),'%25','%') then
     raise exception 'context definition unavailable' using errcode='42501';end if;
    v_items:=v_items||jsonb_build_array(jsonb_build_object('ref',ref,'kind',case when v_value is null then 'property' else 'vocabulary_value' end,
     'schema_version',v_version,'property_ref','eios:property:'||v_type||'/'||v_prop,'display_name',v_props->>'display_name','description',coalesce(v_props->>'description',''),
     'value_type',v_props->>'value_type','closed_vocabulary',coalesce(v_props->'type_descriptor'->'enum','[]'::jsonb),
     'group',(select g->>'group_name' from jsonb_array_elements(coalesce(v_def->'property_groups','[]'::jsonb)) g where g->'property_names' ? v_prop limit 1),
     'aliases',(select coalesce(jsonb_agg(x.alias_text order by x.alias_text),'[]'::jsonb) from ontology.nexloop_definition_aliases x where x.tenant_id=v_tenant and x.world=p_world and x.canonical_ref=ref)));
   end if;
  end loop;
  v_result:=jsonb_build_object('items',v_items);
 else
  v_consumer:=c->>'consumer_id';
  if v_consumer !~ '^[0-9a-f]{64}$' or v_consumer is null or not exists(select 1 from jsonb_array_elements(a->'read_proofs') q
    where q->>'target_resource'='eios:object:Consumer/'||v_consumer and q->>'resource_id'='eios:object:Consumer/'||v_consumer) then
   raise exception 'context open work requires Consumer READ' using errcode='42501';end if;
  if not exists(select 1 from ontology.objects where tenant_id=v_tenant and world=p_world and type_name='Consumer' and object_id=v_consumer) then
   raise exception 'context consumer unavailable' using errcode='42501';end if;
  v_result:=jsonb_build_object(
   'intents',(select coalesce(jsonb_agg(jsonb_build_object('intent_id',i.intent_id::text,'action',i.action_name||':'||i.action_version,'state',i.state,
      'created_at',i.created_at) order by i.created_at,i.intent_id),'[]'::jsonb) from runtime.nexloop_effect_intents i
     where i.tenant_id=v_tenant and i.world=p_world and i.consumer_id=v_consumer and i.state in ('accepted','dispatching','unknown')),
   'outbound',(select coalesce(jsonb_agg(jsonb_build_object('intent_id',o.intent_id::text,'conversation_id',o.conversation_id,'delivery_state',o.delivery_state,
      'persisted_at',o.persisted_at,'delivery_changed_at',o.delivery_changed_at,'message_id',o.message_id,'body_digest',o.body_digest)
      order by o.persisted_at,o.intent_id),'[]'::jsonb) from runtime.nexloop_outbound_messages o join runtime.nexloop_conversations cv
      on cv.tenant_id=o.tenant_id and cv.world=o.world and cv.conversation_id=o.conversation_id
     where o.tenant_id=v_tenant and o.world=p_world and cv.consumer_id=v_consumer),
   -- NX-025 (G4): the Consumer's active plans at their current version, read-only, exactly as the v6 bind re-derives them.
   'plans',(select coalesce(jsonb_agg(jsonb_build_object('plan_id',pl.plan_id::text,'version',pl.version,'created_at',pl.created_at,
      'content',runtime.nexloop_plan_context(pl.tenant_id,pl.world,pl.plan_id,pl.version)) order by pl.created_at,pl.plan_id),'[]'::jsonb)
     from runtime.nexloop_active_plans(v_tenant,p_world) pl where pl.consumer_id=v_consumer));
 end if;
 for proof in select value from jsonb_array_elements(a->'read_proofs') loop
  perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
 end loop;
 return v_result;
end $$;

create or replace function authz.nexloop_context_v6_source_in(p_tenant text,p_world text,p_conversations text[],p_consumer text,p_section text,p_item jsonb,p_proofs jsonb)
returns jsonb language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ref text:=p_item->>'ref';pl runtime.nexloop_plans;cur record;proof jsonb;x ontology.nexloop_claims;i runtime.nexloop_effect_intents;o runtime.nexloop_outbound_messages;
 obj ontology.objects;expected jsonb;kind text;sub text;revision text;tags text[]:='{}';prop text;
begin
 if jsonb_typeof(p_item) is distinct from 'object' or (select count(*) from jsonb_object_keys(p_item))<>10
  or not p_item ?& array['subsection','ref','revision','content','content_hash','evidence_kind','access_decision_ref','relevance_permille','at','tags']
  or jsonb_typeof(p_item->'relevance_permille') is distinct from 'number' or (p_item->>'relevance_permille')!~'^[0-9]{1,4}$' or (p_item->>'relevance_permille')::integer>1000
  or jsonb_typeof(p_item->'at') is distinct from 'string' or length(p_item->>'at')>64 or jsonb_typeof(p_item->'tags') is distinct from 'array'
  or jsonb_typeof(p_item->'revision') is distinct from 'string' then raise exception 'context source shape invalid' using errcode='42501';end if;
 if p_item->>'content_hash' is distinct from runtime.nexloop_context_hash(p_item->'content') then raise exception 'context source hash mismatch' using errcode='42501';end if;
 proof:=p_proofs->(p_item->>'access_decision_ref');
 if proof is null then raise exception 'context source decision unknown' using errcode='42501';end if;
 if ref ~ '^claim:[0-9a-f]{64}$' then
  -- AT-064: a Claim is evidence only; never formal state, never a planning fact.
  if p_section<>'evidence' then raise exception 'context Claim outside evidence' using errcode='42501';end if;
  select * into x from ontology.nexloop_claims where tenant_id=p_tenant and world=p_world and claim_id=substr(ref,7) and conversation_id=any(p_conversations);
  if not found then raise exception 'context Claim unavailable' using errcode='42501';end if;
  if proof->>'target_resource' is distinct from 'eios:object:Conversation/'||x.conversation_id then raise exception 'context source decision mismatch' using errcode='42501';end if;
  if x.epistemic_kind='hypothesis' then
   -- Real-world strategies cannot include hypotheses (0088); nothing else may carry one.
   raise exception 'context hypothesis excluded' using errcode='42501';
  end if;
  if x.source_message_id is null or x.resolution_state not in ('unresolved','needs_resolution','awaiting_definition','resolved') then
   raise exception 'context Claim not admissible evidence' using errcode='42501';end if;
  sub:='claim_evidence';revision:=x.extractor_version;kind:=case when x.speaker='consumer' then 'user_statement' else 'conversation' end;
  -- Original text only: no structured value can act as a formal property.
  expected:=jsonb_build_object('quote',x.quote,'message_ref','eios:object:Message/'||x.source_message_id,'span',jsonb_build_array(x.span_start,x.span_end),
   'speaker',x.speaker,'epistemic_kind',x.epistemic_kind,'resolution_state',x.resolution_state,'modality',x.modality,'condition',x.condition_text);
  if x.polarity='negated' then tags:=tags||'negation'::text;end if;
  if x.epistemic_kind='constraint' then tags:=tags||'contact_limit'::text;end if;
 elsif ref ~ '^nexloop:intent:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' then
  if p_section<>'open_work' then raise exception 'context execution state outside open work' using errcode='42501';end if;
  select * into i from runtime.nexloop_effect_intents where intent_id=substr(ref,16)::uuid and tenant_id=p_tenant and world=p_world and consumer_id=p_consumer;
  if not found or i.state not in ('accepted','dispatching','unknown') then raise exception 'context intent unavailable' using errcode='42501';end if;
  if proof->>'target_resource' is distinct from 'eios:object:Consumer/'||p_consumer then raise exception 'context source decision mismatch' using errcode='42501';end if;
  sub:='intents';revision:=i.state;kind:='execution_state';tags:=array['unconfirmed'];
  expected:=jsonb_build_object('action',i.action_name||':'||i.action_version,'state',i.state,'since',i.created_at);
 elsif ref ~ '^nexloop:outbound:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' then
  if p_section<>'open_work' then raise exception 'context execution state outside open work' using errcode='42501';end if;
  select o2.* into o from runtime.nexloop_outbound_messages o2 join runtime.nexloop_conversations cv on cv.tenant_id=o2.tenant_id and cv.world=o2.world and cv.conversation_id=o2.conversation_id
   where o2.tenant_id=p_tenant and o2.world=p_world and o2.intent_id=substr(ref,18)::uuid and cv.consumer_id=p_consumer;
  -- Channel-accepted replies are conversation, never unconfirmed execution (ADR-020 §2).
  if not found or o.delivery_state in ('provider_accepted','delivered') then raise exception 'context outbound unavailable' using errcode='42501';end if;
  if proof->>'target_resource' is distinct from 'eios:object:Consumer/'||p_consumer then raise exception 'context source decision mismatch' using errcode='42501';end if;
  sub:='outbound';revision:=o.delivery_state;kind:='execution_state';
  if o.delivery_state in ('persisted','dispatching','unknown') then tags:=array['unconfirmed'];end if;
  expected:=jsonb_build_object('delivery_state',o.delivery_state,'since',o.delivery_changed_at,'note','not confirmed as delivered to the consumer');
 elsif ref ~ '^nexloop:plan:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}@[1-9][0-9]{0,5}$' then
  -- NX-025 (G4): a plan is open work of its Consumer; only its current active version, read-only.
  if p_section<>'open_work' then raise exception 'context plan outside open work' using errcode='42501';end if;
  select * into pl from runtime.nexloop_plans where tenant_id=p_tenant and world=p_world and plan_id=split_part(substr(ref,14),'@',1)::uuid
   and version=split_part(ref,'@',2)::integer and consumer_id=p_consumer;
  if not found then raise exception 'context plan unavailable' using errcode='42501';end if;
  select * into cur from runtime.nexloop_plan_current(p_tenant,p_world,pl.plan_id);
  if cur.status is distinct from 'active' or cur.version is distinct from pl.version then raise exception 'context plan not current' using errcode='42501';end if;
  if proof->>'target_resource' is distinct from 'eios:object:Consumer/'||p_consumer then raise exception 'context source decision mismatch' using errcode='42501';end if;
  sub:='plan';revision:=pl.version::text;kind:='policy';
  expected:=runtime.nexloop_plan_context(p_tenant,p_world,pl.plan_id,pl.version);
 elsif ref ~ '^eios:object:Consumer/[0-9a-f]{64}$' then
  if p_section not in ('constraints','consumer_state') or substr(ref,22) is distinct from p_consumer then raise exception 'context formal object outside formal state' using errcode='42501';end if;
  select * into obj from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='Consumer' and object_id=p_consumer;
  if not found or proof->>'target_resource' is distinct from 'eios:object:Consumer/'||p_consumer then raise exception 'context source decision mismatch' using errcode='42501';end if;
  if jsonb_typeof(p_item->'content'->'properties') is distinct from 'object' then raise exception 'context formal projection invalid' using errcode='42501';end if;
  -- Projection only: every shown property is current and was itself READ-authorized.
  for prop in select jsonb_object_keys(p_item->'content'->'properties') loop
   if not obj.properties ? prop or not exists(select 1 from jsonb_each(p_proofs) q where q.value->>'target_resource'='eios:property:Consumer/'||p_consumer||'/'||prop) then
    raise exception 'context formal property unreadable' using errcode='42501';end if;
  end loop;
  sub:='Consumer';revision:=obj.nexloop_revision::text;kind:='formal_object';
  expected:=jsonb_build_object('type','Consumer','properties',(select coalesce(jsonb_object_agg(k,obj.properties->k),'{}'::jsonb) from jsonb_object_keys(p_item->'content'->'properties') k));
 else
  raise exception 'context source kind unsupported' using errcode='42501';
 end if;
 if p_item->>'subsection' is distinct from sub or p_item->>'evidence_kind' is distinct from kind or p_item->>'revision' is distinct from revision
  or p_item->'content' is distinct from expected then raise exception 'context source content mismatch' using errcode='42501';end if;
 return to_jsonb(array(select t from unnest(tags) t order by t));
end $$;

create or replace function authz.nexloop_context_v6_sections(p_digest text,p_world text,p_tenant text,p_principal text,a jsonb,p jsonb,pack jsonb,
 p_conversations text[],p_consumer text,p_run uuid,p_context uuid,p_core_sources jsonb) returns void
language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare strat control.nexloop_context_strategies;goal_fact jsonb;control jsonb;proofs jsonb:='{}'::jsonb;proof jsonb;item jsonb;section text;
 ordinal integer:=0;tags jsonb;omitted jsonb;n integer;required record;
 sections constant text[]:=array['constraints','consumer_state','open_work','evidence','semantics','experience'];
begin
 -- Strategy: the current published version only.
 if pack->>'strategy_ref' !~ '^context-strategy:[a-z][a-z0-9_]{0,63}@[1-9][0-9]{0,6}$' then raise exception 'context v6 strategy invalid' using errcode='42501';end if;
 select * into strat from control.nexloop_context_strategies where tenant_id=p_tenant and world=p_world and strategy_id=split_part(substr(pack->>'strategy_ref',18),'@',1)
  and version=split_part(pack->>'strategy_ref','@',2)::integer;
 if not found or exists(select 1 from control.nexloop_context_strategies s where s.tenant_id=p_tenant and s.world=p_world and s.strategy_id=strat.strategy_id and s.version>strat.version)
  or strat.definition->'include_hypotheses' is distinct from 'false'::jsonb then raise exception 'context v6 strategy not current' using errcode='42501';end if;
 if jsonb_typeof(pack->'budget_report') is distinct from 'object'
  or (pack->'budget_report'->'input_token_budget',pack->'budget_report'->'output_reserve',pack->'budget_report'->'framing_reserve')
   is distinct from (strat.definition->'input_token_budget',strat.definition->'output_reserve',strat.definition->'framing_reserve') then
  raise exception 'context v6 budget mismatch' using errcode='42501';end if;
 -- Goal: the bound formal Goal revision; control snapshot recomputed now (NX-022), or declared insufficient.
 select value into goal_fact from jsonb_array_elements(pack->'formal_facts') where value->>'type'='Goal';
 if jsonb_typeof(pack->'goal') is distinct from 'object' or (select count(*) from jsonb_object_keys(pack->'goal'))<>2
  or pack->'goal'->'goal_version_refs' is distinct from jsonb_build_array('goal:'||(goal_fact->>'id')||'@'||(goal_fact->>'revision')) then
  raise exception 'context v6 goal mismatch' using errcode='42501';end if;
 control:=pack->'goal'->'control_snapshot';
 if control is null or control='null'::jsonb then
  if not exists(select 1 from jsonb_array_elements(pack->'insufficient') x where x->>'code' in ('control_paused','goal_not_current')) then
   raise exception 'context v6 control snapshot required' using errcode='42501';end if;
 elsif control is distinct from authz.nexloop_control_snapshot(p_digest,p_world,jsonb_build_object('scopes',jsonb_build_array(jsonb_build_object('kind','consumer','ref','consumer:'||p_consumer)),
   'goals','[]'::jsonb,'objects',jsonb_build_array(jsonb_build_object('type_name','Consumer','object_id',p_consumer)),'budgets',jsonb_build_array('model'))) then
  raise exception 'context v6 control snapshot stale' using errcode='42501';
 end if;
 -- Read proofs: caller's own, current, each addressed by the hash the items cite.
 if jsonb_typeof(a->'read_proofs') is distinct from 'array' or jsonb_array_length(a->'read_proofs')>256 then raise exception 'context v6 unavailable' using errcode='42501';end if;
 for proof in select value from jsonb_array_elements(a->'read_proofs') loop
  if proof->>'tenant_id' is distinct from p_tenant or proof->>'principal_id' is distinct from p_principal or proof->>'operation' is distinct from 'read'
   or proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'context read proof invalid' using errcode='42501';end if;
  perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
  proofs:=proofs||jsonb_build_object('decision:'||runtime.nexloop_context_hash(proof),proof);
 end loop;
 insert into runtime.nexloop_context_packs(tenant_id,world,context_id,run_id,protocol,strategy_ref,pack_digest,artifact_id,budget_report,insufficient)
  values(p_tenant,p_world,p_context,p_run,'nexloop.context-pack.v6',pack->>'strategy_ref',p->>'pack_digest',pack->'bindings'->>'artifact_id',pack->'budget_report',pack->'insufficient');
 -- Core sources, then strategy and control.
 for item in select value from jsonb_array_elements(p_core_sources||jsonb_build_array(
   jsonb_build_object('section','bindings','ref',pack->>'strategy_ref','revision',strat.version::text,'hash',strat.definition_digest,'kind','policy','decision',runtime.nexloop_context_hash(a)),
   jsonb_build_object('section','goal','ref','nexloop:control:'||coalesce(control->>'control_revision','unavailable'),'revision',coalesce(control->>'control_revision','0'),
    'hash',runtime.nexloop_context_hash(coalesce(control,'null'::jsonb)),'kind','policy','decision',runtime.nexloop_context_hash(a)))) loop
  insert into runtime.nexloop_context_sources values(p_tenant,p_world,p_context,ordinal,item->>'section',item->>'ref',item->>'revision',item->>'hash',item->>'kind','decision:'||(item->>'decision'),'included',null);
  ordinal:=ordinal+1;
 end loop;
 -- Item sections: every item re-verified against its source; tags are what the source requires.
 foreach section in array sections loop
  if jsonb_typeof(pack->section) is distinct from 'array' or jsonb_array_length(pack->section)>256 then raise exception 'context v6 pack invalid' using errcode='42501';end if;
  if (pack->'budget_report'->'sections'->section->>'included')::integer is distinct from jsonb_array_length(pack->section) then raise exception 'context v6 budget report mismatch' using errcode='42501';end if;
  for item in select value from jsonb_array_elements(pack->section) loop
   tags:=authz.nexloop_context_v6_source_in(p_tenant,p_world,p_conversations,p_consumer,section,item,proofs);
   if item->'tags' is distinct from tags then raise exception 'context v6 tags mismatch' using errcode='42501';end if;
   insert into runtime.nexloop_context_sources values(p_tenant,p_world,p_context,ordinal,section,item->>'ref',item->>'revision',item->>'content_hash',item->>'evidence_kind',item->>'access_decision_ref','included',null);
   ordinal:=ordinal+1;
  end loop;
 end loop;
 -- Omitted items are real sources too, and none of them may be pinned (AT-028).
 select count(*) into n from jsonb_array_elements(pack->'budget_report'->'omitted');
 if jsonb_typeof(p->'omitted_items') is distinct from 'array' or n<>jsonb_array_length(p->'omitted_items') then raise exception 'context v6 omission report mismatch' using errcode='42501';end if;
 for omitted in select value from jsonb_array_elements(p->'omitted_items') loop
  if not exists(select 1 from jsonb_array_elements(pack->'budget_report'->'omitted') r where r=jsonb_build_object('section',omitted->>'section','subsection',omitted->'item'->>'subsection','ref',omitted->'item'->>'ref','reason',omitted->>'reason'))
   or not (omitted->>'section')=any(sections) or exists(select 1 from jsonb_array_elements(pack->(omitted->>'section')) r where r->>'ref'=omitted->'item'->>'ref') then
   raise exception 'context v6 omission report mismatch' using errcode='42501';end if;
  tags:=authz.nexloop_context_v6_source_in(p_tenant,p_world,p_conversations,p_consumer,omitted->>'section',omitted->'item',proofs);
  if tags<>'[]'::jsonb or omitted->'item'->'tags' is distinct from tags then raise exception 'context v6 pinned source omitted' using errcode='42501';end if;
  insert into runtime.nexloop_context_sources values(p_tenant,p_world,p_context,ordinal,omitted->>'section',omitted->'item'->>'ref',omitted->'item'->>'revision',
   omitted->'item'->>'content_hash',omitted->'item'->>'evidence_kind',omitted->'item'->>'access_decision_ref','omitted',omitted->>'reason');
  ordinal:=ordinal+1;
 end loop;
 -- Every pinned source the database knows about is present, or its section is declared insufficient.
 for required in
  select 'evidence' section,'claim:'||x.claim_id ref from ontology.nexloop_claims x where x.tenant_id=p_tenant and x.world=p_world and x.conversation_id=any(p_conversations)
   and x.epistemic_kind<>'hypothesis' and x.source_message_id is not null and x.resolution_state in ('unresolved','needs_resolution','awaiting_definition','resolved')
   and (x.polarity='negated' or x.epistemic_kind='constraint')
  union all select 'open_work','nexloop:intent:'||i.intent_id from runtime.nexloop_effect_intents i where i.tenant_id=p_tenant and i.world=p_world and i.consumer_id=p_consumer and i.state in ('accepted','dispatching','unknown')
  union all select 'open_work','nexloop:outbound:'||o.intent_id from runtime.nexloop_outbound_messages o join runtime.nexloop_conversations cv
   on cv.tenant_id=o.tenant_id and cv.world=o.world and cv.conversation_id=o.conversation_id where o.tenant_id=p_tenant and o.world=p_world and cv.consumer_id=p_consumer and o.delivery_state in ('persisted','dispatching','unknown')
  union all select 'open_work','nexloop:plan:'||pl.plan_id||'@'||pl.version from runtime.nexloop_active_plans(p_tenant,p_world) pl where pl.consumer_id=p_consumer
 loop
  if not exists(select 1 from jsonb_array_elements(pack->required.section) r where r->>'ref'=required.ref)
   and not exists(select 1 from jsonb_array_elements(pack->'insufficient') x where x->>'code'='required_source_unreadable' and x->>'section'=required.section) then
   raise exception 'context v6 pinned source missing' using errcode='42501';end if;
 end loop;
end $$;

-- G5 (dispatcher decision 4): a governed change of an object that an active plan's control snapshot
-- names wakes that plan, in the writer's transaction: the 0093 recall-instance marker on ontology.objects
-- (every governed create/edit/erasure already passes it) also touches plan-reevaluate for exactly those
-- plans. The wake is a trigger, not a decision: precheck re-derives everything (NXC04 object changed,
-- stop_if) and only then may one bounded Run start. Only snapshot objects (not every object) wake a plan.
create or replace function authz.nexloop_work_feed_on_object() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare o record;r record;
begin
 if tg_op='DELETE' then o:=old;else o:=new;end if;
 if tg_op='UPDATE' and (old.tenant_id,old.world,old.type_name,old.object_id) is distinct from (new.tenant_id,new.world,new.type_name,new.object_id) then
  perform authz.nexloop_work_feed_touch(old.tenant_id,old.world,'recall-instance',old.type_name||'/'||old.object_id,
   jsonb_build_object('type_name',old.type_name,'object_id',old.object_id,'op','delete'));
 end if;
 perform authz.nexloop_work_feed_touch(o.tenant_id,o.world,'recall-instance',o.type_name||'/'||o.object_id,
  jsonb_build_object('type_name',o.type_name,'object_id',o.object_id,'op',case when tg_op='DELETE' then 'delete' else 'upsert' end));
 if tg_op<>'INSERT' then
  for r in select p.plan_id from runtime.nexloop_active_plans(o.tenant_id,o.world) p
   where p.control_snapshot->'objects' @> jsonb_build_array(jsonb_build_object('type_name',o.type_name,'object_id',o.object_id)) loop
   perform authz.nexloop_plan_feed_touch(o.tenant_id,o.world,r.plan_id,jsonb_build_object('kind','object_changed','cause','external',
    'ref',o.type_name||'/'||o.object_id,'revision',case when tg_op='DELETE' then null else new.nexloop_revision end,'at',clock_timestamp()),null);
  end loop;
 end if;
 return null;
end $$;
create index nexloop_plans_snapshot_objects on runtime.nexloop_plans using gin ((control_snapshot->'objects') jsonb_path_ops);
