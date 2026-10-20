-- Proactive outreach, slice 8: metrics, the evidence chain and the owner's view (docs/implementation/outreach-impl-plan.md; design §5,
-- P7). The migration number lives only in the file name and the catalog; nothing below repeats it. Read only: counts, ratios, times,
-- ids, digests and codes; never a message body, Claim value or payload.
-- 1. runtime.nexloop_outreach_metrics(tenant, world): over the last 7 days (and current states): candidates of the dry run, sends
--    accepted, pending approval, approvals by decision, sent (admitted), fulfilled, refusals by gate code, per purpose, marketing
--    (sent, unsubscribes, unsubscribe rate when at least 20 marketing sends, the tenant allowance and consumers at it), consent
--    (granted / withdrawn events, consumers whose latest consent is granted, consent rate), frequency refusals.
-- 2. The metrics snapshot gains the block 'outreach' (metrics export, alert evaluator, owner metrics read); alert rules v7 add
--    outreach_unsubscribe_rate_high and outreach_approvals_waiting.
-- 3. runtime.nexloop_outreach_evidence(tenant, world, intent): the chain of one proactive send: trigger (origin), the frozen request
--    (purpose, list version, topic, channel, body digest and length, initiator, Run), approval, every gate's conclusion (the gate
--    results recorded at refusal; an admitted send passed all of them at its admission time), dispatch (state, fence, provider
--    reference, times), audit events, and the consumer's unsubscribe / resume / consent records after the request.
--    runtime.nexloop_outreach_evidence_list: summaries, newest first, optionally one consumer or synthetic / test personas only (the
--    owner's demonstration view: synthetic data only, real dispatch stays closed).
-- 4. Workbench observe verbs outreach_metrics and outreach_evidence, owner only.

create function runtime.nexloop_outreach_metrics(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare n timestamptz:=clock_timestamp();since timestamptz:=n-interval '7 days';s jsonb;
 v_candidates integer;v_msent bigint;v_unsub bigint;v_cap integer;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 -- The tenant's settings under its own row security (read after the tenant context is set).
 s:=runtime.nexloop_outreach_settings(p_tenant);v_cap:=(s->'marketing'->>'per_7d')::integer;
 v_candidates:=jsonb_array_length(coalesce(runtime.nexloop_outreach_candidates(p_tenant,p_world,n)->'candidates','[]'::jsonb));
 perform set_config('eios.tenant_id',p_tenant,true);
 select count(*) into v_msent from runtime.nexloop_outreach_sends where tenant_id=p_tenant and world=p_world and frequency_class='marketing'
  and admitted_at>since and state in ('dispatching','unknown','fulfilled','failed');
 select count(*) into v_unsub from runtime.nexloop_marketing_optouts where tenant_id=p_tenant and world=p_world and kind='unsubscribed' and recorded_at>since;
 return jsonb_build_object('computed_at',n,'window_days',7,'candidates',v_candidates,
  'accepted_7d',(select count(*) from runtime.nexloop_outreach_sends where tenant_id=p_tenant and world=p_world and created_at>since),
  'pending_approval',(select count(*) from runtime.nexloop_outreach_sends where tenant_id=p_tenant and world=p_world and state='pending_approval'),
  'approvals_7d',coalesce((select jsonb_object_agg(coalesce(decision,'pending'),c) from (select decision,count(*) c from runtime.nexloop_outreach_approvals
    where tenant_id=p_tenant and world=p_world and requested_at>since group by decision) q),'{}'::jsonb),
  'sent_7d',(select count(*) from runtime.nexloop_outreach_sends where tenant_id=p_tenant and world=p_world and admitted_at>since
    and state in ('dispatching','unknown','fulfilled','failed')),
  'fulfilled_7d',(select count(*) from runtime.nexloop_outreach_sends where tenant_id=p_tenant and world=p_world and state='fulfilled' and finished_at>since),
  'refused_7d',coalesce((select jsonb_object_agg(refusal_code,c) from (select refusal_code,count(*) c from runtime.nexloop_outreach_sends
    where tenant_id=p_tenant and world=p_world and state='refused' and finished_at>since group by refusal_code) q),'{}'::jsonb),
  'by_purpose_7d',coalesce((select jsonb_object_agg(purpose_id,jsonb_build_object('accepted',a,'sent',sn,'refused',rf)) from (select purpose_id,count(*) a,
    count(*) filter (where admitted_at is not null and state in ('dispatching','unknown','fulfilled','failed')) sn,count(*) filter (where state='refused') rf
    from runtime.nexloop_outreach_sends where tenant_id=p_tenant and world=p_world and created_at>since group by purpose_id) q),'{}'::jsonb),
  'frequency_capped_7d',(select count(*) from runtime.nexloop_outreach_sends where tenant_id=p_tenant and world=p_world and state='refused'
    and refusal_code='frequency_capped' and finished_at>since),
  'marketing',jsonb_build_object('enabled',(s->'marketing'->>'enabled')::boolean,'consent_mode',s->'marketing'->>'consent_mode','sent_7d',v_msent,
    'unsubscribes_7d',v_unsub,'min_volume',20,'unsubscribe_rate_7d',case when v_msent>=20 then round(v_unsub::numeric/v_msent,4) end,
    'allowance_per_7d',v_cap,
    'consumers_at_allowance',(select count(*) from (select consumer_id from runtime.nexloop_outreach_sends where tenant_id=p_tenant and world=p_world
      and frequency_class='marketing' and admitted_at>since and state in ('dispatching','unknown','fulfilled') group by consumer_id having count(*)>=greatest(v_cap,1)) q)),
  'consent',jsonb_build_object(
    'granted_7d',(select count(*) from runtime.nexloop_marketing_consents where tenant_id=p_tenant and world=p_world and kind='granted' and recorded_at>since),
    'withdrawn_7d',(select count(*) from runtime.nexloop_marketing_consents where tenant_id=p_tenant and world=p_world and kind='withdrawn' and recorded_at>since),
    'consumers_with_record',(select count(distinct consumer_id) from runtime.nexloop_marketing_consents where tenant_id=p_tenant and world=p_world),
    'consumers_consented',(select count(*) from (select distinct on (consumer_id) consumer_id,kind from runtime.nexloop_marketing_consents
      where tenant_id=p_tenant and world=p_world order by consumer_id,event_id desc) q where kind='granted'),
    'consent_rate',(select case when count(*)>0 then round((count(*) filter (where kind='granted'))::numeric/count(*),4) end from (select distinct on (consumer_id) kind
      from runtime.nexloop_marketing_consents where tenant_id=p_tenant and world=p_world order by consumer_id,event_id desc) q)));
end $$;
alter function runtime.nexloop_outreach_metrics(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_outreach_metrics(text,text) from public;

-- 2. Snapshot block ------------------------------------------------------------------------------------------------------------------
alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_outreach;
revoke all on function runtime.nexloop_metrics_snapshot_before_outreach(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 return runtime.nexloop_metrics_snapshot_before_outreach(p_tenant,p_world)||jsonb_build_object('outreach',runtime.nexloop_outreach_metrics(p_tenant,p_world));
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;

-- 3. Evidence ------------------------------------------------------------------------------------------------------------------------
create function runtime.nexloop_outreach_evidence(p_tenant text,p_world text,p_intent text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare s runtime.nexloop_outreach_sends;ap runtime.nexloop_outreach_approvals;v_gates jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into s from runtime.nexloop_outreach_sends where tenant_id=p_tenant and world=p_world and intent_id=p_intent;
 if not found then return null;end if;
 select * into ap from runtime.nexloop_outreach_approvals where tenant_id=p_tenant and world=p_world and intent_id=p_intent;
 -- Gates: what admission recorded on refusal; an admitted send passed every gate at its admission time; nothing yet otherwise.
 v_gates:=case when s.state='refused' then jsonb_build_object('stage','refused','code',s.refusal_code,'at',s.finished_at,
    'gates',coalesce((select jsonb_agg(jsonb_build_object('gate',g->>'gate','result',g->>'result','detail',case when jsonb_typeof(g->'detail')='string' then g->'detail'
      when jsonb_typeof(g->'detail')='object' then g->'detail' end)) from jsonb_array_elements(s.refusal_gates) g),'[]'::jsonb))
  when s.admitted_at is not null then jsonb_build_object('stage','admitted','code',null,'at',s.admitted_at,'gates','all_passed')
  else jsonb_build_object('stage','not_yet','code',null,'at',null,'gates','[]'::jsonb) end;
 return jsonb_build_object('found',true,
  'trigger',jsonb_build_object('origin_kind',s.origin_kind,'origin',split_part(s.origin_ref,':',1),'origin_ref',s.origin_ref,'initiator',s.initiator,'run_id',s.run_id),
  'request',jsonb_build_object('intent_id',s.intent_id,'consumer_id',s.consumer_id,'persona',runtime.nexloop_consumer_persona(p_tenant,p_world,s.consumer_id),
    'purpose_id',s.purpose_id,'purpose_version',s.purpose_version,'topic',s.topic,'frequency_class',s.frequency_class,'channel',s.channel,
    'body_digest',s.body_digest,'body_chars',char_length(s.body),'conversation_id',s.conversation_id,'requested_at',s.created_at),
  'approval',case when ap.intent_id is null then null else jsonb_build_object('requested_at',ap.requested_at,'expires_at',ap.expires_at,'decision',ap.decision,
    'decided_at',ap.decided_at,'decided_by',ap.decided_by,'requested_by',ap.requested_by) end,
  'gates',v_gates,
  'dispatch',jsonb_build_object('state',s.state,'fence',s.fence,'admitted_at',s.admitted_at,'finished_at',s.finished_at,'provider_reference',s.provider_reference,
    'provider','loopback'),
  'events',coalesce((select jsonb_agg(jsonb_build_object('action',e.action,'at',e.created_at) order by e.created_at,e.event_id) from runtime.audit_events e
    where e.tenant_id=p_tenant and e.trace_id='outreach' and (e.request_id=s.intent_id or e.event_id='outreach-send:'||s.intent_id)),'[]'::jsonb),
  'responses',jsonb_build_object(
    'optouts',coalesce((select jsonb_agg(jsonb_build_object('kind',o.kind,'source',o.source,'rule_version',o.rule_version,'rule_id',o.rule_id,
      'answers_this_send',o.outreach_intent=s.intent_id,'at',o.recorded_at) order by o.event_id) from runtime.nexloop_marketing_optouts o
      where o.tenant_id=p_tenant and o.world=p_world and o.consumer_id=s.consumer_id and o.recorded_at>=s.created_at),'[]'::jsonb),
    'consents',coalesce((select jsonb_agg(jsonb_build_object('kind',k.kind,'source',k.source,'at',k.recorded_at) order by k.event_id) from runtime.nexloop_marketing_consents k
      where k.tenant_id=p_tenant and k.world=p_world and k.consumer_id=s.consumer_id),'[]'::jsonb),
    'contact_restricted',exists(select 1 from control.nexloop_contact_restrictions r where r.tenant_id=p_tenant and r.world=p_world and r.consumer_id=s.consumer_id and r.active)));
end $$;
alter function runtime.nexloop_outreach_evidence(text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_outreach_evidence(text,text,text) from public;

create function runtime.nexloop_outreach_evidence_list(p_tenant text,p_world text,p_consumer text,p_synthetic_only boolean,p_limit integer) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 return jsonb_build_object('synthetic_only',p_synthetic_only,'items',coalesce((select jsonb_agg(x order by x->>'requested_at' desc,x->>'intent_id') from (
  select jsonb_build_object('intent_id',s.intent_id,'consumer_id',s.consumer_id,'persona',runtime.nexloop_consumer_persona(p_tenant,p_world,s.consumer_id),
    'purpose_id',s.purpose_id,'initiator',s.initiator,'origin',split_part(s.origin_ref,':',1),'state',s.state,'refusal_code',s.refusal_code,
    'requested_at',s.created_at,'finished_at',s.finished_at) x
  from runtime.nexloop_outreach_sends s where s.tenant_id=p_tenant and s.world=p_world and (p_consumer is null or s.consumer_id=p_consumer)
   and (not p_synthetic_only or runtime.nexloop_consumer_persona(p_tenant,p_world,s.consumer_id)<>'real')
  order by s.created_at desc,s.intent_id limit p_limit) q),'[]'::jsonb));
end $$;
alter function runtime.nexloop_outreach_evidence_list(text,text,text,boolean,integer) owner to nexloop_owner;
revoke all on function runtime.nexloop_outreach_evidence_list(text,text,text,boolean,integer) from public;

-- 4. Workbench observe verbs (owner only; every other verb unchanged) -----------------------------------------------------------------
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) rename to nexloop_workbench_observe_read_before_outreach_evidence;
revoke all on function authz.nexloop_workbench_observe_read_before_outreach_evidence(text,text,text,text,text) from public,nexloop_api;
create function authz.nexloop_workbench_observe_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;w jsonb;cfg control.nexloop_workbench_configurations;v_tenant text;
 v_action text:='eios:action:nexloop.workbench.read:1';
begin
 if c->>'verb' is null or c->>'verb' not in ('outreach_metrics','outreach_evidence') then
  return authz.nexloop_workbench_observe_read_before_outreach_evidence(p_digest,p_world,p_text,p_signature,p_payload);end if;
 -- The 0133 verification, for this verb (as the earlier verbs).
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
  or w->>'role'<>'owner' then
  raise exception 'workbench observe forbidden' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 -- outreach_metrics: the verb only. outreach_evidence: one intent's chain (intent_id) or a list (optional consumer_id, synthetic_only, limit).
 if (c->>'verb'='outreach_metrics' and (select count(*) from jsonb_object_keys(c))<>1)
  or exists(select 1 from jsonb_object_keys(c) q(key) where q.key not in ('verb','intent_id','consumer_id','synthetic_only','limit'))
  or (c ? 'intent_id' and coalesce(c->>'intent_id','')!~'^outreach:[0-9a-f]{64}$') or (c ? 'consumer_id' and coalesce(c->>'consumer_id','')!~'^[a-f0-9]{64}$')
  or (c ? 'synthetic_only' and jsonb_typeof(c->'synthetic_only') is distinct from 'boolean')
  or (c ? 'limit' and (jsonb_typeof(c->'limit') is distinct from 'number' or (c->>'limit')::numeric not between 1 and 200)) then
  raise exception 'workbench observe invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 if c->>'verb'='outreach_metrics' then return runtime.nexloop_outreach_metrics(v_tenant,p_world);end if;
 if c ? 'intent_id' then
  return coalesce(runtime.nexloop_outreach_evidence(v_tenant,p_world,c->>'intent_id'),jsonb_build_object('found',false));end if;
 return runtime.nexloop_outreach_evidence_list(v_tenant,p_world,c->>'consumer_id',coalesce((c->>'synthetic_only')::boolean,false),coalesce((c->>'limit')::integer,50));
end $$;
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_observe_read(text,text,text,text,text) from public;
grant execute on function authz.nexloop_workbench_observe_read(text,text,text,text,text) to nexloop_api;
