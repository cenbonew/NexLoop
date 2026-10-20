-- Proactive outreach, slice 1: candidates and a dispatch dry run, read only (docs/implementation/outreach-impl-plan.md §3;
-- design docs/design/proactive-outreach.md, owner decisions P1-P8, 2026-10-19). Nothing here writes a row, creates an intent,
-- sends anything or calls a model; there is no outreach Action yet (slice 4), so no candidate can be dispatched.
-- 1. runtime.nexloop_outreach_defaults(): the owner's P3 defaults until slice 2 brings tenant configuration: quiet hours
--    21:00-09:00 in Asia/Shanghai (plan O1: no tenant time zone yet), at most 1 contact per 24 h and 2 per 7 days per customer.
-- 2. runtime.nexloop_outreach_candidates(tenant, world, at): from the existing triggers only
--    commitment_followup  an open or in-progress Commitment whose due_at is within lead_seconds (current commitment settings)
--    service_followup     a service intent that is not a bound reply and became fulfilled / confirmed / failed in the last 24 h
--    each with a dedupe key and the gates of the design (§3.2) in order: control_paused, contact_restricted, purpose (not wired:
--    slice 3), persona_excluded (ADR-034), quiet_hours, frequency (plan O2: every contact that is not a bound reply counts),
--    consumer_preference (the operator persona chain's check, channel native WebChat, topic = purpose), real_dispatch
--    (state of nexloop.outreach.send:1, not published yet, so closed), budget (not wired: slice 4). would_dispatch is true
--    only when every gate passes and none is unwired (never, in this slice).
-- 3. Workbench observe verb outreach_candidates (owner only): ids, purposes, times and gate results; no message text, no name.

create function runtime.nexloop_outreach_defaults() returns jsonb
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select jsonb_build_object('time_zone','Asia/Shanghai','quiet_hours','21:00-09:00','per_24h',1,'per_7d',2,'source','owner P3 default (tenant configuration from slice 2)')
$$;

-- Contacts that count against the frequency limits: intents that reach the customer and are not a reply bound to an inbound message.
create function runtime.nexloop_outreach_contacts_since(p_tenant text,p_world text,p_consumer text,p_since timestamptz) returns bigint
 language sql stable set search_path=pg_catalog,pg_temp as $$
 select count(*) from runtime.nexloop_effect_intents i where i.tenant_id=p_tenant and i.world=p_world and i.consumer_id=p_consumer and i.created_at>p_since
  and i.state in ('dispatching','unknown','fulfilled','observed_fulfilled','confirmed')
  and not exists(select 1 from runtime.nexloop_outbound_messages o where o.tenant_id=i.tenant_id and o.world=i.world and o.intent_id=i.intent_id and o.trigger_message_id is not null)
  and coalesce((control.nexloop_intent_effect_category(p_tenant,p_world,i.intent_id)->>'category')='customer_contact'
   or (control.nexloop_intent_effect_category(p_tenant,p_world,i.intent_id)->>'notification')::boolean,true)
$$;

create function runtime.nexloop_outreach_gates(p_tenant text,p_world text,p_consumer text,p_purpose text,p_at timestamptz) returns jsonb
 language plpgsql stable set search_path=pg_catalog,pg_temp as $$
declare d jsonb:=runtime.nexloop_outreach_defaults();g jsonb:='[]'::jsonb;v_local time:=(p_at at time zone (d->>'time_zone'))::time;
 v_from time:=split_part(d->>'quiet_hours','-',1)::time;v_to time:=split_part(d->>'quiet_hours','-',2)::time;v_n24 bigint;v_n7 bigint;v_code text;v_layer text;
begin
 g:=g||jsonb_build_object('gate','control_paused','result',case when exists(select 1 from control.nexloop_control_scopes s where s.tenant_id=p_tenant and s.world=p_world and s.paused
   and ((s.scope_kind='tenant' and s.scope_ref='*') or (s.scope_kind='consumer' and s.scope_ref in (p_consumer,'consumer:'||p_consumer)))) then 'refuse' else 'pass' end);
 g:=g||jsonb_build_object('gate','contact_restricted','result',case when exists(select 1 from control.nexloop_contact_restrictions r where r.tenant_id=p_tenant
   and r.world=p_world and r.consumer_id=p_consumer and r.active) then 'refuse' else 'pass' end);
 g:=g||jsonb_build_object('gate','purpose','result','not_wired','detail','purpose list: slice 3');
 g:=g||jsonb_build_object('gate','persona_excluded','result',case when runtime.nexloop_consumer_persona(p_tenant,p_world,p_consumer)<>'real' then 'refuse' else 'pass' end,
   'detail',runtime.nexloop_consumer_persona(p_tenant,p_world,p_consumer));
 g:=g||jsonb_build_object('gate','quiet_hours','result',case when (v_from<=v_to and v_local>=v_from and v_local<v_to) or (v_from>v_to and (v_local>=v_from or v_local<v_to))
   then 'refuse' else 'pass' end,'detail',d->>'quiet_hours');
 v_n24:=runtime.nexloop_outreach_contacts_since(p_tenant,p_world,p_consumer,p_at-interval '24 hours');
 v_n7:=runtime.nexloop_outreach_contacts_since(p_tenant,p_world,p_consumer,p_at-interval '7 days');
 g:=g||jsonb_build_object('gate','frequency','result',case when v_n24>=(d->>'per_24h')::integer or v_n7>=(d->>'per_7d')::integer then 'refuse' else 'pass' end,
   'detail',jsonb_build_object('last_24h',v_n24,'last_7d',v_n7));
 v_code:=runtime.nexloop_contact_preference_refusal(p_tenant,p_world,p_consumer,'native.webchat',p_purpose,gen_random_uuid(),p_at);
 g:=g||jsonb_build_object('gate','consumer_preference','result',case when v_code is null then 'pass' else 'refuse' end,'detail',v_code);
 v_layer:=control.nexloop_real_dispatch_closed_layer(p_tenant,p_world,'eios:action:nexloop.outreach.send:1');
 g:=g||jsonb_build_object('gate','real_dispatch','result',case when v_layer is null then 'pass' else 'refuse' end,'detail',coalesce(v_layer,'open'));
 g:=g||jsonb_build_object('gate','budget','result','not_wired','detail','outreach Action and budget: slice 4');
 return g;
end $$;

create function runtime.nexloop_outreach_candidates(p_tenant text,p_world text,p_at timestamptz) returns jsonb
 language plpgsql stable set search_path=pg_catalog,pg_temp as $$
declare v_lead integer:=coalesce((select (definition->>'lead_seconds')::integer from control.nexloop_commitment_settings order by version desc limit 1),3600);
 x record;out jsonb:='[]'::jsonb;gates jsonb;
begin
 for x in
  select c.consumer_id,'commitment_followup' purpose,'commitment:'||c.commitment_id origin_ref,c.commitment_id||':due_soon' dedupe_key,
   (o.properties->>'due_at')::timestamptz at_time
  from runtime.nexloop_commitments c join ontology.objects o on o.tenant_id=c.tenant_id and o.world=c.world and o.type_name='Commitment' and o.object_id=c.commitment_id
  where c.tenant_id=p_tenant and c.world=p_world and o.properties->>'status' in ('open','in_progress') and o.properties->>'due_at' is not null
   and (o.properties->>'due_at')::timestamptz>p_at and (o.properties->>'due_at')::timestamptz-make_interval(secs=>v_lead)<=p_at
  union all
  select i.consumer_id,'service_followup','intent:'||i.intent_id::text,i.intent_id::text||':'||i.state,
   coalesce((select max(e.created_at) from runtime.nexloop_effect_events e where e.tenant_id=i.tenant_id and e.world=i.world and e.intent_id=i.intent_id),i.created_at)
  from runtime.nexloop_effect_intents i
  where i.tenant_id=p_tenant and i.world=p_world and i.state in ('fulfilled','confirmed','failed')
   and not exists(select 1 from runtime.nexloop_outbound_messages m where m.tenant_id=i.tenant_id and m.world=i.world and m.intent_id=i.intent_id)
   and coalesce((select max(e.created_at) from runtime.nexloop_effect_events e where e.tenant_id=i.tenant_id and e.world=i.world and e.intent_id=i.intent_id),i.created_at)>p_at-interval '24 hours'
  order by 5,3
 loop
  gates:=runtime.nexloop_outreach_gates(p_tenant,p_world,x.consumer_id,x.purpose,p_at);
  out:=out||jsonb_build_object('consumer_id',x.consumer_id,'purpose',x.purpose,'origin_ref',x.origin_ref,'dedupe_key',x.dedupe_key,'at',x.at_time,'gates',gates,
   'would_dispatch',not exists(select 1 from jsonb_array_elements(gates) y where y->>'result'<>'pass'),
   'blocked_by',(select coalesce(jsonb_agg(y->>'gate'),'[]'::jsonb) from jsonb_array_elements(gates) y where y->>'result'='refuse'));
 end loop;
 return jsonb_build_object('at',p_at,'defaults',runtime.nexloop_outreach_defaults(),'candidates',out,
  'note','dry run: no outreach Action is published yet (slice 4); nothing is sent');
end $$;

do $owners$
declare f text;
begin
 foreach f in array array['runtime.nexloop_outreach_defaults()','runtime.nexloop_outreach_contacts_since(text,text,text,timestamp with time zone)',
   'runtime.nexloop_outreach_gates(text,text,text,text,timestamp with time zone)','runtime.nexloop_outreach_candidates(text,text,timestamp with time zone)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';execute 'revoke all on function '||f||' from public';
 end loop;
end $owners$;

-- 3. The owner's read (workbench observe; every other verb unchanged) -------------------------------------------------------
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) rename to nexloop_workbench_observe_read_before_outreach_candidates_v0231;
revoke all on function authz.nexloop_workbench_observe_read_before_outreach_candidates_v0231(text,text,text,text,text) from public,nexloop_api;
create function authz.nexloop_workbench_observe_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;w jsonb;cfg control.nexloop_workbench_configurations;v_tenant text;
 v_action text:='eios:action:nexloop.workbench.read:1';v_consumer text;
begin
 if c->>'verb' is distinct from 'outreach_candidates' then
  return authz.nexloop_workbench_observe_read_before_outreach_candidates_v0231(p_digest,p_world,p_text,p_signature,p_payload);end if;
 -- The 0133 verification, for this verb (as 0210/0220/0227).
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
 if (select count(*) from jsonb_object_keys(c))<>1 then raise exception 'workbench observe invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 return runtime.nexloop_outreach_candidates(v_tenant,p_world,clock_timestamp());
end $$;
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_observe_read(text,text,text,text,text) from public;
grant execute on function authz.nexloop_workbench_observe_read(text,text,text,text,text) to nexloop_api;
