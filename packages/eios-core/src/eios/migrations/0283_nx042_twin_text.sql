-- NX-042 T6 (design §2, D1, D5, D6): the text layer of the consumer twin. The migration number lives only in the file name and the
-- catalog; nothing below repeats it.
-- 1. runtime.nexloop_twin_render_text(generator, base, step, behaviour, kind): the step's simulated message text from the twin version's
--    generator (the controlled list, T1). twin-template/1, the default (D1): a fixed template chosen by the seeded draw
--    (runtime.nexloop_sim_draw over seed | definition digest | consumer, label text:<step>:<behaviour>) among the templates of the
--    event kind - message, refuse_contact, payment_intent; a visit has no text. The same seed and twin version give the same text byte
--    for byte. Every template starts with the simulation marker 【模拟消息】, names no person, contact or account and quotes no
--    conversation (D5: the real-content guard holds for every template); the event's attributes carry text_marker simulated, the
--    template id and the generator version.
-- 2. A generator that calls a model is an interface only: rendering with it is refused (model_budget_required) - D1: a real model needs
--    an experiment's own approved budget, which no slice provides yet. An unlisted generator is refused (generator_unknown).
-- 3. authz.nexloop_twin_act (T3 body, replaced in place) takes its text from the renderer instead of the fixed placeholder and adds the
--    three text attributes; everything else is T3's (registered in the function copy guard).

create function runtime.nexloop_twin_text_templates() returns jsonb language sql immutable set search_path=pg_catalog,pg_temp as $$
 select '{"message":[
   {"id":"message.1","text":"【模拟消息】收到，我晚点看一下。"},
   {"id":"message.2","text":"【模拟消息】好的，谢谢告知。"},
   {"id":"message.3","text":"【模拟消息】能再说明一下具体安排吗？"},
   {"id":"message.4","text":"【模拟消息】我考虑一下，稍后回复。"}],
  "refuse_contact":[
   {"id":"refuse_contact.1","text":"【模拟消息】请不要再给我发消息了。"},
   {"id":"refuse_contact.2","text":"【模拟消息】以后不用联系我了，谢谢。"}],
  "payment_intent":[
   {"id":"payment_intent.1","text":"【模拟消息】我想下单，怎么付款？"},
   {"id":"payment_intent.2","text":"【模拟消息】价格合适的话，我这周就付款。"}]}'::jsonb
$$;
alter function runtime.nexloop_twin_text_templates() owner to nexloop_owner;

create function runtime.nexloop_twin_render_text(p_generator text,p_base text,p_step integer,p_behavior text,p_kind text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp as $$
declare g control.nexloop_twin_generators;v_list jsonb;v_pick jsonb;
begin
 select * into g from control.nexloop_twin_generators where generator_version=p_generator;
 if not found then raise exception 'twin text: generator_unknown' using errcode='22023';end if;
 -- The model path is an interface only (D1): an experiment's approved budget is required and none exists.
 if g.uses_model then raise exception 'twin text: model_budget_required' using errcode='22023';end if;
 if p_generator<>'twin-template/1' then raise exception 'twin text: generator_unknown' using errcode='22023';end if;
 if p_kind is null or p_kind='visit' then
  return jsonb_build_object('text',null,'template_id',null,'generator_version',p_generator);end if;
 v_list:=runtime.nexloop_twin_text_templates()->p_kind;
 if v_list is null then raise exception 'twin text: kind_unknown' using errcode='22023';end if;
 v_pick:=v_list->(floor(runtime.nexloop_sim_draw(p_base,'text:'||p_step||':'||coalesce(p_behavior,''))*jsonb_array_length(v_list))::integer);
 return jsonb_build_object('text',v_pick->>'text','template_id',v_pick->>'id','generator_version',p_generator);
end $$;
alter function runtime.nexloop_twin_render_text(text,text,integer,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_twin_render_text(text,text,integer,text,text) from public;

create or replace function authz.nexloop_twin_act(p_digest text,p_world text,p_binding text,p_consumer text,p_step integer) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text;b runtime.nexloop_twin_bindings;o runtime.nexloop_twin_observations;p runtime.nexloop_twin_event_provenance;t control.nexloop_consumer_twins;
 v_behaviors jsonb;v_choice record;v_cal text;v_event jsonb;v_result jsonb;v_text jsonb;
begin
 if session_user is distinct from 'nexloop_twin_port' then raise exception 'twin port: unavailable' using errcode='42501';end if;
 v_tenant:=authz.nexloop_sim_effect_tenant(p_digest,p_world);
 if coalesce(p_binding,'')!~'^[0-9a-f]{64}$' or coalesce(p_consumer,'')!~'^[0-9a-f]{64}$' or p_step is null or p_step not between 1 and 1000000 then
  raise exception 'twin port: invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 b:=runtime.nexloop_twin_binding(v_tenant,p_world,p_binding);
 perform pg_advisory_xact_lock(hashtextextended('nexloop:twin-observe:'||p_world||':'||p_binding||':'||p_consumer,0));
 select * into o from runtime.nexloop_twin_observations where tenant_id=v_tenant and world=p_world and binding_id=p_binding and consumer_id=p_consumer and step=p_step;
 if not found then raise exception 'twin port: step_not_observed' using errcode='22023';end if;
 select * into p from runtime.nexloop_twin_event_provenance where tenant_id=v_tenant and world=p_world and binding_id=p_binding and consumer_id=p_consumer and step=p_step;
 if not found then
  select * into t from control.nexloop_consumer_twins where tenant_id=v_tenant and twin_id=b.twin_id and version=b.twin_version;
  if runtime.nexloop_consumer_twin_status(v_tenant,b.twin_id,b.twin_version) is distinct from 'published' then raise exception 'twin port: twin_retired' using errcode='22023';end if;
  v_cal:=runtime.nexloop_consumer_twin_calibration(v_tenant,b.twin_id,b.twin_version);
  -- The decision layer (T2): seed | definition digest | consumer; the step is the instance. It never reads the observation.
  v_behaviors:=runtime.nexloop_twin_decide(b.seed||'|'||b.twin_digest||'|'||p_consumer,p_step,t.definition->'parameters'->'behaviors');
  select x.key behavior,runtime.nexloop_twin_behavior_kind(x.key) kind into v_choice from jsonb_each(v_behaviors) x
   where (x.value->>'simulated_outcome')::boolean
   order by array_position(array['refuse_contact','payment_intent','message','visit'],runtime.nexloop_twin_behavior_kind(x.key)),x.key limit 1;
  if v_choice.behavior is not null then
   -- T6 text layer (D1): the version's generator renders the step's text; the deterministic template by default, a model generator refused.
   v_text:=runtime.nexloop_twin_render_text(t.generator_version,b.seed||'|'||b.twin_digest||'|'||p_consumer,p_step,v_choice.behavior,v_choice.kind);
   v_event:=jsonb_build_object('consumer_id',p_consumer,'event_kind',v_choice.kind,
    'content',(case when v_text->>'text' is not null then jsonb_build_object('text',v_text->>'text') else '{}'::jsonb end)
     ||jsonb_build_object('attributes',jsonb_build_object('behavior',v_choice.behavior,'twin_id',b.twin_id,'twin_version',b.twin_version,
       'twin_seed',b.seed,'twin_step',p_step,'calibration_status',v_cal,'origin','twin')
      ||case when v_text->>'text' is not null then jsonb_build_object('text_template',v_text->>'template_id','text_generator',v_text->>'generator_version',
        'text_marker','simulated') else '{}'::jsonb end),
    'twin',jsonb_build_object('version',b.port_version,'seed',b.seed,'step',p_step));
   -- The S7 port entry: the world's event ceiling, the sequence and the world time are its own.
   v_result:=authz.nexloop_sim_consumer_act(p_digest,p_world,v_event);
   perform set_config('eios.tenant_id',v_tenant,true);
  end if;
  insert into runtime.nexloop_twin_event_provenance(tenant_id,world,binding_id,consumer_id,step,event_id,behaviors,behavior,event_kind,twin_id,twin_version,
    twin_digest,seed,calibration_status,observation_digest)
   values(v_tenant,p_world,p_binding,p_consumer,p_step,v_result->'frozen'->>'event_id',v_behaviors,v_choice.behavior,v_choice.kind,b.twin_id,b.twin_version,
    b.twin_digest,b.seed,v_cal,o.observation_digest)
   returning * into p;
 end if;
 return jsonb_build_object('binding_id',p.binding_id,'consumer_id',p.consumer_id,'step',p.step,'behaviors',p.behaviors,'behavior',p.behavior,
  'event_kind',p.event_kind,'event_id',p.event_id,
  'event',(select jsonb_build_object('sequence',e.sequence,'content',e.content,'content_digest',e.content_digest,'port_version',e.twin_version,
     'sim_time',runtime.nexloop_commercial_ts(e.sim_time),'data_mode',e.data_mode,'output_kind',e.output_kind,'origin',e.origin)
    from runtime.nexloop_sim_consumer_events e where e.tenant_id=v_tenant and e.world=p_world and e.event_id=p.event_id),
  'provenance',jsonb_build_object('twin_id',p.twin_id,'twin_version',p.twin_version,'twin_digest',p.twin_digest,'seed',p.seed,'step',p.step,
    'calibration_status',p.calibration_status,'observation_digest',p.observation_digest,'data_mode',p.data_mode,'output_kind',p.output_kind,'origin',p.origin));
end $$;
