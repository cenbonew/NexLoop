-- An erased Consumer stays erased under its pseudonym (I28, after R60; temporary number, renumbered at merge).
-- When the NX-029 erasure completes, 0305/0306/0307 and the erasure step rewrite the Consumer id of the kept rows (conversations,
-- plans, outreach sends, effect intents, role run bindings, data exports and others) to a fresh 'pseudonym:<hex>', while
-- control.nexloop_consumer_erasures keeps the original id. Every reader that takes the id from such a row and asks
-- runtime.nexloop_consumer_erasing got false and treated the erased Consumer as not erased: evolution trace runs and twin calibration
-- counted it, the remediation list and evidence showed it, an accepted outreach send could pass the admit check, a data export
-- stayed open. Only the erasure writes that prefix, so a 'pseudonym:' id is erasing. The outreach candidates (dry run and metrics),
-- which had no erasure check, skip an erasing Consumer. Nothing else changes.
create or replace function runtime.nexloop_consumer_erasing(p_tenant text,p_world text,p_consumer text) returns boolean
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v boolean;
begin
 if p_consumer like 'pseudonym:%' then return true;end if;
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 v:=exists(select 1 from control.nexloop_consumer_erasures e where e.tenant_id=p_tenant and e.world=p_world and e.consumer_id=p_consumer
  and e.state<>'pending_confirmation');
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;

create or replace function runtime.nexloop_outreach_candidates(p_tenant text,p_world text,p_at timestamptz) returns jsonb
 language plpgsql stable set search_path=pg_catalog,pg_temp as $$
declare v_lead integer:=coalesce((select (definition->>'lead_seconds')::integer from control.nexloop_commitment_settings order by version desc limit 1),3600);
 x record;out jsonb:='[]'::jsonb;gates jsonb;v_lever jsonb;
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
   and coalesce((select max(e.created_at) from runtime.nexloop_effect_events e where e.tenant_id=i.tenant_id and e.world=i.world and e.intent_id=i.intent_id),i.created_at)>p_at-interval '336 hours'
  order by 5,3
 loop
  continue when runtime.nexloop_consumer_erasing(p_tenant,p_world,x.consumer_id);
  -- NX-043 X4 wiring: a service follow-up of a candidate-group Consumer comes after the strategy's follow_up_delay_hours (1-336 h);
  -- everyone else keeps the 24 hours window.
  if x.purpose='service_followup' then
   v_lever:=runtime.nexloop_experiment_levers(p_tenant,p_world,x.consumer_id)->'levers';
   if v_lever ? 'follow_up_delay_hours' then
    continue when x.at_time+make_interval(hours=>(v_lever->>'follow_up_delay_hours')::integer)>p_at;
   else
    continue when x.at_time<=p_at-interval '24 hours';
   end if;
  end if;
  gates:=runtime.nexloop_outreach_gates(p_tenant,p_world,x.consumer_id,x.purpose,p_at);
  out:=out||jsonb_build_object('consumer_id',x.consumer_id,'purpose',x.purpose,'origin_ref',x.origin_ref,'dedupe_key',x.dedupe_key,'at',x.at_time,'gates',gates,
   'would_dispatch',not exists(select 1 from jsonb_array_elements(gates) y where y->>'result'<>'pass'),
   'blocked_by',(select coalesce(jsonb_agg(y->>'gate'),'[]'::jsonb) from jsonb_array_elements(gates) y where y->>'result'='refuse'));
 end loop;
 return jsonb_build_object('at',p_at,'settings',runtime.nexloop_outreach_settings(p_tenant),'candidates',out,
  'note','dry run: no outreach Action is published yet (slice 4); nothing is sent');
end $$;
