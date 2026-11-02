-- K5 (docs/design/k5-consumer-evidence-views.md v2, L6 PASS; owner decisions Q1-Q5 of 2026-11-02): the raw reads behind three
-- read-only workbench views. Temporary number; the dispatcher renumbers.
-- authz.nexloop_workbench_observe_read gains the verbs 'consumer_evidence', 'message_trace' and 'conversation_metrics' (every other
-- verb unchanged, as 0370 added 'experiments'). Unlike 'experiments' they are served to every workbench role holding
-- nexloop.workbench.read; the role is returned and the per-node filtering (ADR-025 object and property READ, the owner's restricted
-- groups, the reviewer-only quote, owner-only outreach, actions and cost) is applied by the caller through the existing audited
-- reads, which write the read audit with the purposes consumer_evidence, message_trace and conversation_metrics.
-- World real only (another world: 42501 'k5: real_world_only'); an erasing Consumer: null (404). Identifiers, codes and times; the
-- one text returned is a Claim's quote and predicate, which the caller passes on only under the rules above (never a structured
-- Claim value: AT-064 - nothing here is projected into business state, and nothing here writes).
set local timezone = 'UTC';

-- Claims of one consumer or one conversation with their latest match conclusion (the matched type and property refs).
create function runtime.nexloop_k5_claims(p_tenant text,p_consumer text,p_conversation text,p_message text) returns jsonb
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select coalesce(jsonb_agg(jsonb_build_object('claim_id',c.claim_id,'consumer_id',c.consumer_id,'conversation_id',c.conversation_id,
   'source_message_id',c.source_message_id,'sequence',c.source_sequence,'speaker',c.speaker,'predicate',c.predicate,
   'quote',nullif(c.quote,''),'epistemic_kind',c.epistemic_kind,'resolution_state',c.resolution_state,'confidence',c.confidence,
   'corrects_claim_id',c.corrects_claim_id,'erased',c.value ? 'erased','recorded_at',c.recorded_at,
   'match',(select jsonb_build_object('outcome',m.outcome,'reason',m.reason,'type_ref',m.decision->'type'->>'ref',
      'property_ref',m.decision->'property'->>'ref','proposal_id',m.proposal_id,'candidate_id',m.candidate_id,'recorded_at',m.recorded_at)
     from ontology.nexloop_claim_matches m where m.tenant_id=p_tenant and m.world='real' and m.claim_id=c.claim_id
     order by m.recorded_at desc,m.matcher_version desc limit 1))
   order by c.recorded_at,c.claim_id),'[]'::jsonb)
 from ontology.nexloop_claims c
 where c.tenant_id=p_tenant and c.world='real' and (p_consumer is null or c.consumer_id=p_consumer)
  and (p_conversation is null or c.conversation_id=p_conversation) and (p_message is null or c.source_message_id=p_message) $$;

create function runtime.nexloop_k5_consumer_evidence(p_tenant text,p_consumer text,p_role text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);v jsonb;
begin
 if p_consumer !~ '^[a-f0-9]{64}$' then raise exception 'k5: invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 if not exists(select 1 from runtime.nexloop_conversations x where x.tenant_id=p_tenant and x.world='real' and x.consumer_id=p_consumer)
  and not exists(select 1 from ontology.objects o where o.tenant_id=p_tenant and o.world='real' and o.type_name='Consumer' and o.object_id=p_consumer)
  or runtime.nexloop_consumer_erasing(p_tenant,'real',p_consumer) then
  perform set_config('eios.tenant_id',coalesce(v_prior,''),true);return null;end if;
 v:=jsonb_build_object('role',p_role,'claims',runtime.nexloop_k5_claims(p_tenant,p_consumer,null,null),
  'commitments',(select coalesce(jsonb_agg(jsonb_build_object('commitment_id',r.commitment_id,'origin',r.origin,'claim_id',r.claim_id,
     'replaces_commitment_id',r.supersedes,'intent_id',r.intent_id,'prepared_at',r.prepared_at) order by r.prepared_at,r.commitment_id),'[]'::jsonb)
    from runtime.nexloop_commitments r where r.tenant_id=p_tenant and r.world='real' and r.consumer_id=p_consumer),
  'actions',(select coalesce(jsonb_agg(jsonb_build_object('intent_id',i.intent_id,'action_name',i.action_name,'state',i.state,
     'created_at',i.created_at) order by i.created_at,i.intent_id),'[]'::jsonb)
    from runtime.nexloop_effect_intents i where i.tenant_id=p_tenant and i.world='real' and i.consumer_id=p_consumer),
  -- Q1: outreach is the owner's; another role gets its count only.
  'outreach_total',(select count(*) from runtime.nexloop_outreach_sends s where s.tenant_id=p_tenant and s.world='real' and s.consumer_id=p_consumer),
  'outreach',case when p_role='owner' then (select coalesce(jsonb_agg(jsonb_build_object('intent_id',s.intent_id,'purpose',s.purpose_id,
     'state',s.state,'created_at',s.created_at) order by s.created_at,s.intent_id),'[]'::jsonb)
    from runtime.nexloop_outreach_sends s where s.tenant_id=p_tenant and s.world='real' and s.consumer_id=p_consumer) else '[]'::jsonb end,
  'candidates',(select coalesce(jsonb_agg(x.v order by x.created_at,x.id),'[]'::jsonb) from (
     select p.proposal_id id,p.created_at,jsonb_build_object('candidate_kind','proposal','candidate_id',p.proposal_id,'status',p.status,
       'match_decision',p.outcome,'match_reason_code',nullif(p.reason,''),'dependent_claim_ids',jsonb_build_array(p.claim_id),
       'type_name',p.type_name,'target_ref',p.target_ref,'property_name',p.property_name) v
      from ontology.nexloop_mutation_proposals p join ontology.nexloop_claims c on c.tenant_id=p.tenant_id and c.world=p.world and c.claim_id=p.claim_id
      where p.tenant_id=p_tenant and p.world='real' and c.consumer_id=p_consumer
     union all
     select d.candidate_id,d.created_at,jsonb_build_object('candidate_kind','definition','candidate_id',d.candidate_id,'status',d.status,
       'match_decision','no_match','match_reason_code',null,'dependent_claim_ids',to_jsonb(d.dependent_claims))
      from ontology.nexloop_candidate_definitions d where d.tenant_id=p_tenant and d.world='real'
       and exists(select 1 from ontology.nexloop_claims c where c.tenant_id=d.tenant_id and c.world=d.world and c.claim_id=any(d.dependent_claims)
        and c.consumer_id=p_consumer)) x));
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return v;
end $$;

create function runtime.nexloop_k5_message_trace(p_tenant text,p_message text,p_role text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);m runtime.nexloop_conversation_messages;cv runtime.nexloop_conversations;
 iss authz.nexloop_message_run_issuances;v jsonb;
begin
 if p_message is null or length(p_message) not between 1 and 160 then raise exception 'k5: invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into m from runtime.nexloop_conversation_messages x where x.tenant_id=p_tenant and x.world='real' and x.message_id=p_message limit 1;
 if found then select * into cv from runtime.nexloop_conversations x where x.tenant_id=p_tenant and x.world='real' and x.conversation_id=m.conversation_id;end if;
 if m.message_id is null or runtime.nexloop_consumer_erasing(p_tenant,'real',cv.consumer_id) then
  perform set_config('eios.tenant_id',coalesce(v_prior,''),true);return null;end if;
 select * into iss from authz.nexloop_message_run_issuances x where x.tenant_id=p_tenant and x.world='real' and x.message_id=p_message;
 v:=jsonb_build_object('role',p_role,'message_id',m.message_id,'conversation_id',m.conversation_id,'consumer_id',cv.consumer_id,
  'direction',m.record->>'direction','accepted_at',m.record->>'accepted_at',
  'relay',jsonb_build_object('leased',exists(select 1 from runtime.nexloop_message_relay_leases l where l.tenant_id=p_tenant and l.world='real' and l.message_id=p_message),
    'handoff',(select jsonb_build_object('reason',h.reason,'at',h.handed_at) from runtime.nexloop_relay_handoffs h where h.tenant_id=p_tenant and h.world='real' and h.message_id=p_message),
    'attempts',(select jsonb_build_object('attempts',a.attempts,'at',a.last_failure_at) from runtime.nexloop_relay_attempts a where a.tenant_id=p_tenant and a.world='real' and a.message_id=p_message)),
  'run',case when iss.run_id is null then null else jsonb_build_object('run_id',iss.run_id,'issued_at',iss.issued_at) end,
  'contexts',(select coalesce(jsonb_agg(jsonb_build_object('context_id',p.context_id,'at',p.created_at) order by p.created_at,p.context_id),'[]'::jsonb)
    from runtime.nexloop_context_packs p where p.tenant_id=p_tenant and p.world='real' and p.run_id=iss.run_id),
  'models',(select coalesce(jsonb_agg(jsonb_build_object('call',r.call_sequence,'status',r.result_status,'at',r.requested_at,'finished_at',r.completed_at)
     order by r.call_sequence),'[]'::jsonb) from runtime.nexloop_model_requests r where r.tenant_id=p_tenant and r.world='real' and r.run_id=iss.run_id),
  'intents',(select coalesce(jsonb_agg(jsonb_build_object('intent_id',i.intent_id,'receipt_id',i.receipt_id,'state',i.state,'at',i.created_at)
     order by i.created_at,i.intent_id),'[]'::jsonb)
    from runtime.nexloop_effect_intents i where i.tenant_id=p_tenant and i.world='real'
     and (i.origin_run_id=iss.run_id or exists(select 1 from runtime.nexloop_effect_submissions s where s.intent_id=i.intent_id and s.run_id=iss.run_id))),
  'extraction',(select jsonb_build_object('at',min(e.recorded_at),'runs',count(*)) from ontology.nexloop_extraction_runs e
    where e.tenant_id=p_tenant and e.world='real' and e.conversation_id=m.conversation_id and e.message_refs @> jsonb_build_array(p_message)),
  'claims',runtime.nexloop_k5_claims(p_tenant,null,null,p_message));
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return v;
end $$;

create function runtime.nexloop_k5_conversation_metrics(p_tenant text,p_conversation text,p_role text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);cv runtime.nexloop_conversations;v jsonb;
begin
 if p_conversation !~ '^[a-f0-9]{64}$' then raise exception 'k5: invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into cv from runtime.nexloop_conversations x where x.tenant_id=p_tenant and x.world='real' and x.conversation_id=p_conversation;
 if not found or runtime.nexloop_consumer_erasing(p_tenant,'real',cv.consumer_id) then
  perform set_config('eios.tenant_id',coalesce(v_prior,''),true);return null;end if;
 with msg as (select m.message_id,m.sequence,m.record->>'direction' direction,(m.record->>'accepted_at')::timestamptz accepted_at
   from runtime.nexloop_conversation_messages m where m.tenant_id=p_tenant and m.world='real' and m.conversation_id=p_conversation),
 lat as (select (extract(epoch from (select min(o.accepted_at) from msg o where o.direction='outbound' and o.sequence>i.sequence)-i.accepted_at)*1000)::bigint ms
   from msg i where i.direction='inbound'),
 runs as (select r.run_id from authz.nexloop_message_run_issuances r join msg on msg.message_id=r.message_id where r.tenant_id=p_tenant and r.world='real'),
 gates as (select 'scope_denied'::text code from runtime.nexloop_scope_denials d join msg on msg.message_id=d.message_id where d.tenant_id=p_tenant and d.world='real'
   union all select 'contact_'||c.certainty from control.nexloop_contact_restrictions c where c.tenant_id=p_tenant and c.world='real' and c.conversation_id=p_conversation
   union all select 'dispatch_'||f.code from runtime.nexloop_dispatch_refusals f where f.tenant_id=p_tenant and f.world='real' and f.intent_id in
     (select i.intent_id from runtime.nexloop_effect_intents i where i.tenant_id=p_tenant and i.world='real' and (i.origin_run_id in (select run_id from runs)
       or exists(select 1 from runtime.nexloop_effect_submissions s where s.intent_id=i.intent_id and s.run_id in (select run_id from runs)))))
 select jsonb_build_object('role',p_role,'consumer_id',cv.consumer_id,
  'response_ms',(select case when count(ms)=0 then null else jsonb_build_object('p50',percentile_disc(0.5) within group (order by ms),
     'p90',percentile_disc(0.9) within group (order by ms),'samples',count(ms)) end from lat where ms is not null and ms>=0),
  -- Q3: the model cost is the owner's (as /metrics); nothing of it leaves this function for another role.
  'model_cost',case when p_role='owner' then jsonb_build_object('runs',(select count(*) from runs),
     'totals',(select coalesce(jsonb_agg(jsonb_build_object('currency',t.currency,'amount',t.amount::text,'amount_unit',t.amount_unit,'entries',t.n)
       order by t.currency,t.amount_unit),'[]'::jsonb) from (select e.currency,e.amount_unit,sum(e.amount) amount,count(*) n
       from runtime.nexloop_cost_entries e where e.tenant_id=p_tenant and e.world='real' and e.cost_kind='model' and e.amount is not null
        and e.run_id in (select run_id from runs) group by 1,2 limit 8) t),
     'unpriced_entries',(select count(*) from runtime.nexloop_cost_entries e where e.tenant_id=p_tenant and e.world='real' and e.cost_kind='model'
       and e.amount is null and e.run_id in (select run_id from runs))) end,
  'gate_blocks',(select jsonb_build_object('total',count(*),'by_code',coalesce((select jsonb_agg(jsonb_build_object('code',g.code,'count',g.n) order by g.n desc,g.code)
     from (select code,count(*) n from gates group by 1 order by 2 desc,1 limit 20) g),'[]'::jsonb)) from gates),
  'claims',runtime.nexloop_k5_claims(p_tenant,null,p_conversation,null),
  'commitments',(select coalesce(jsonb_agg(jsonb_build_object('commitment_id',r.commitment_id,'claim_id',r.claim_id) order by r.commitment_id),'[]'::jsonb)
    from runtime.nexloop_commitments r join msg on msg.message_id=r.message_id where r.tenant_id=p_tenant and r.world='real'))
 into v;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return v;
end $$;

do $owners$
declare f text;
begin
 foreach f in array array['runtime.nexloop_k5_claims(text,text,text,text)','runtime.nexloop_k5_consumer_evidence(text,text,text)',
   'runtime.nexloop_k5_message_trace(text,text,text)','runtime.nexloop_k5_conversation_metrics(text,text,text)'] loop
  execute format('alter function %s owner to nexloop_owner',f);
  execute format('revoke all on function %s from public',f);
 end loop;
end $owners$;

alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) rename to nexloop_workbench_observe_read_before_k5;
revoke all on function authz.nexloop_workbench_observe_read_before_k5(text,text,text,text,text) from public,nexloop_api;
create function authz.nexloop_workbench_observe_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;w jsonb;cfg control.nexloop_workbench_configurations;v_tenant text;
 v_action text:='eios:action:nexloop.workbench.read:1';
begin
 if c->>'verb' is null or c->>'verb' not in ('consumer_evidence','message_trace','conversation_metrics') then
  return authz.nexloop_workbench_observe_read_before_k5(p_digest,p_world,p_text,p_signature,p_payload);end if;
 -- The 0133 verification, for these verbs (as 0370 for experiments).
 if octet_length(p_text)>1048576 or octet_length(p_payload)>4096
  or a->>'protocol' is distinct from 'nexloop-workbench-observe-v1' or a->>'operation' is distinct from 'execute'
  or a->>'action_resource' is distinct from v_action or a->>'resource_id' is distinct from v_action
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'workbench observe unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-workbench-observe-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'workbench observe unavailable' using errcode='42501';end if;
 if p_world is distinct from 'real' then raise exception 'k5: real_world_only' using errcode='42501';end if;
 -- Every workbench role holding the read; the role decides the filtering (owner-only parts are left out here already).
 w:=authz.nexloop_workbench_member(p_digest,p_world);v_tenant:=w->>'tenant_id';cfg:=control.nexloop_workbench_current(v_tenant);
 if a->>'tenant_id' is distinct from v_tenant or a->>'principal_id' is distinct from w->>'principal_id'
   or not coalesce(cfg.roles->(w->>'role') ? v_action,false) then
  raise exception 'workbench observe forbidden' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 if (select count(*) from jsonb_object_keys(c))<>2 then raise exception 'workbench observe invalid' using errcode='22023';end if;
 if c->>'verb'='consumer_evidence' then return runtime.nexloop_k5_consumer_evidence(v_tenant,c->>'consumer_id',w->>'role');end if;
 if c->>'verb'='message_trace' then return runtime.nexloop_k5_message_trace(v_tenant,c->>'message_id',w->>'role');end if;
 return runtime.nexloop_k5_conversation_metrics(v_tenant,c->>'conversation_id',w->>'role');
end $$;
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_observe_read(text,text,text,text,text) from public;
grant execute on function authz.nexloop_workbench_observe_read(text,text,text,text,text) to nexloop_api;
