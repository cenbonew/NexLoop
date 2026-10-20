-- Workbench persona pages (temporary number 0232; operator persona UI slice, scheduler 2026-10-19): the operator persona overview
-- (workbench observe verb operator_personas, 0230) becomes readable by every workbench member, read only (non-owners see the
-- configuration; every write stays an owner Action), and each version carries its full document for the editor. The body is
-- 0230's verb body (now under the 0231 rename), with the owner-only condition removed and the document added. The consumer
-- communication preferences (0231 verb contact_preferences) stay owner only: they come from the consumer's own statements.
create or replace function authz.nexloop_workbench_observe_read_before_contact_preferences_v0231(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;w jsonb;cfg control.nexloop_workbench_configurations;v_tenant text;
 v_action text:='eios:action:nexloop.workbench.read:1';v_consumer text;
begin
 if c->>'verb' is distinct from 'operator_personas' then
  return authz.nexloop_workbench_observe_read_before_operator_persona_v0230(p_digest,p_world,p_text,p_signature,p_payload);end if;
 -- The 0133 verification, for this verb (as 0210/0220).
 if octet_length(p_text)>1048576 or octet_length(p_payload)>4096
  or a->>'protocol' is distinct from 'nexloop-workbench-observe-v1' or a->>'operation' is distinct from 'execute'
  or a->>'action_resource' is distinct from v_action or a->>'resource_id' is distinct from v_action
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'workbench observe unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-workbench-observe-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'workbench observe unavailable' using errcode='42501';end if;
 w:=authz.nexloop_workbench_member(p_digest,p_world);v_tenant:=w->>'tenant_id';cfg:=control.nexloop_workbench_current(v_tenant);
 if a->>'tenant_id' is distinct from v_tenant or a->>'principal_id' is distinct from w->>'principal_id' or not (cfg.roles->(w->>'role') ? v_action)
  -- 0232: every workbench member reads the operator persona configuration (tenant configuration, no customer data); writes stay the owner's.
  then raise exception 'workbench observe forbidden' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 v_consumer:=c->>'consumer_id';
 if (select count(*) from jsonb_object_keys(c))-(c ? 'consumer_id')::integer<>1 or (c ? 'consumer_id' and coalesce(v_consumer,'')!~'^[a-f0-9]{64}$') then
  raise exception 'workbench observe invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 return jsonb_build_object(
  'personas',coalesce((select jsonb_agg(jsonb_build_object('persona_id',x.persona_id,'version',x.version,'document_hash',x.document_hash,'created_at',x.created_at,
    'status',(select e.status from control.nexloop_operator_persona_status_events e where e.tenant_id=x.tenant_id and e.persona_id=x.persona_id and e.version=x.version order by e.event_id desc limit 1),
    'display_name',x.document->>'display_name','document',x.document) order by x.persona_id,x.version) from control.nexloop_operator_personas x where x.tenant_id=v_tenant),'[]'::jsonb),
  'rules',(select jsonb_build_object('version',r.version,'default_persona_id',r.default_persona_id,'rules',r.rules,'published_at',r.published_at)
    from control.nexloop_operator_persona_rules r where r.tenant_id=v_tenant order by r.version desc limit 1),
  'ai_disclosure',runtime.nexloop_ai_disclosure_setting(v_tenant),
  'consumer',case when v_consumer is null then null else jsonb_build_object('consumer_id',v_consumer,
    'desired',(select to_jsonb(d) from runtime.nexloop_operator_persona_desired(v_tenant,p_world,v_consumer) d),
    'settled',(select jsonb_build_object('persona_id',cur.persona_id,'source',cur.source,'rules_version',cur.rules_version,'event_id',cur.event_id,'updated_at',cur.updated_at)
      from control.nexloop_operator_persona_current cur where cur.tenant_id=v_tenant and cur.world=p_world and cur.consumer_id=v_consumer),
    'events',coalesce((select jsonb_agg(jsonb_build_object('event_id',e.event_id,'previous',e.previous,'persona_id',e.persona_id,'source',e.source,'rules_version',e.rules_version,
      'recorded_at',e.recorded_at) order by e.event_id) from control.nexloop_operator_persona_assignment_events e where e.tenant_id=v_tenant and e.world=p_world and e.consumer_id=v_consumer),'[]'::jsonb)) end);
end $$;
alter function authz.nexloop_workbench_observe_read_before_contact_preferences_v0231(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_observe_read_before_contact_preferences_v0231(text,text,text,text,text) from public,nexloop_api;
