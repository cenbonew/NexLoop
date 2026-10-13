-- NX-037 (L3, synthetic five kinds): intents_open of the interaction observation (0172) also counts unknown intents, found
-- driving every interaction kind through the controlled interaction executor on the local synthetic stack.
-- An unknown intent (the provider's answer was not seen in time) is reconciled by a later query of the effect Worker; until then
-- the interaction is not settled. Before, the driver ended the interaction at the first unknown result and recorded it
-- unresolved, which pauses the trial (unresolved_unknown_effect): the first unknown result of a real trial paused it although
-- the query and reconcile were still to come. An unknown intent that never settles now ends in the driver's
-- interaction_timeout, which also pauses the trial. intents_open has no other reader (the RealDriver's completion condition).
-- Everything else is 0172 unchanged. Same signature, owner and grants (create or replace keeps them); the authz wrapper is
-- unchanged.
create or replace function runtime.nexloop_nx037_interaction_observation(p_tenant text,p_world text,p_since timestamptz) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v jsonb;m record;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select cm.message_id,cm.conversation_id,cv.consumer_id,(cm.record->>'accepted_at')::timestamptz accepted_at,
        r.run_id,r.task_id,r.command->>'consumer_ref' consumer_ref,j.status task_status
   into m
   from runtime.nexloop_conversation_messages cm
   join runtime.nexloop_conversations cv on cv.tenant_id=cm.tenant_id and cv.world=cm.world and cv.conversation_id=cm.conversation_id
   left join runtime.nexloop_message_routes r on r.tenant_id=cm.tenant_id and r.world=cm.world and r.message_id=cm.message_id
   left join runtime.jobs j on j.job_id=r.task_id
  where cm.tenant_id=p_tenant and cm.world=p_world and not (cm.record ? 'direction')
    and (cm.record->>'accepted_at')::timestamptz>=p_since
  order by (cm.record->>'accepted_at')::timestamptz,cm.message_id limit 1;
 if not found then
  perform set_config('eios.tenant_id',coalesce(prior,''),true);
  return jsonb_build_object('message_id',null,'server_time',clock_timestamp());
 end if;
 with outbound as (
   select o.message_id,o.conversation_id,o.record,(o.record->>'accepted_at')::timestamptz at from runtime.nexloop_conversation_messages o
    where o.tenant_id=p_tenant and o.world=p_world and o.record->>'direction'='outbound' and o.record->>'trigger_message_id'=m.message_id),
  intents as (
   select i.* from runtime.nexloop_effect_intents i where i.tenant_id=p_tenant and i.world=p_world and m.run_id is not null
     and (i.origin_run_id=m.run_id or exists(select 1 from runtime.nexloop_effect_dispatch_controls c
       where c.intent_id=i.intent_id and c.run_id=m.run_id and c.tenant_id=p_tenant and c.world=p_world))),
  q as (select q.context_id from runtime.nexloop_model_requests q where q.tenant_id=p_tenant and q.world=p_world and m.run_id is not null and q.run_id=m.run_id),
  cited as (select (regexp_matches(s.ref,'[0-9a-f]{64}','g'))[1] id from q join runtime.nexloop_context_sources s
    on s.tenant_id=p_tenant and s.world=p_world and s.context_id=q.context_id)
 select jsonb_build_object(
  'server_time',clock_timestamp(),'message_id',m.message_id,'accepted_at',m.accepted_at,'run_id',m.run_id,
  'task_status',m.task_status,'run_terminal',coalesce(m.task_status in ('succeeded','failed'),false),
  'intents',coalesce((select jsonb_object_agg(s,n) from (select state s,count(*) n from intents group by state) x),'{}'::jsonb),
  'intents_open',(select count(*) from intents where state in ('accepted','dispatching','observed_fulfilled','unknown')),
  'intent_ids',coalesce((select jsonb_agg(intent_id order by intent_id) from intents),'[]'::jsonb),
  'outbound_ids',coalesce((select jsonb_agg(message_id order by message_id) from outbound),'[]'::jsonb),
  'unauthorized_sends',(select count(*) from outbound o where o.record->>'sender_kind'='agent' and not exists(
      select 1 from runtime.nexloop_outbound_messages g where g.tenant_id=p_tenant and g.world=p_world and g.message_id=o.message_id
       and g.trigger_message_id=m.message_id)),
  'wrong_recipient_sends',(select count(*) from outbound o where o.conversation_id is distinct from m.conversation_id)
      +case when m.consumer_ref is not null and m.consumer_ref is distinct from 'consumer:'||m.consumer_id then 1 else 0 end,
  'contacts_after_refusal',(select count(*) from outbound o join control.nexloop_contact_restrictions c
      on c.tenant_id=p_tenant and c.world=p_world and c.consumer_id=m.consumer_id
     where o.record->>'sender_kind'='agent' and c.restricted_at<o.at and (c.released_at is null or c.released_at>o.at)),
  'duplicate_deliveries',(select coalesce(max(n),0) from (select count(*) n from runtime.nexloop_outbound_messages g where g.tenant_id=p_tenant
      and g.world=p_world and g.trigger_message_id=m.message_id and g.delivery_state in ('provider_accepted','delivered')) x),
  'duplicate_fulfilled',(select count(*) from intents i where i.state='fulfilled' and exists(select 1 from runtime.nexloop_effect_intents d
      where d.tenant_id=i.tenant_id and d.world=i.world and d.consumer_id=i.consumer_id and d.business_digest=i.business_digest
        and d.state='fulfilled' and d.intent_id<>i.intent_id)),
  'cross_consumer_reads',(select count(*) from cited c where
      exists(select 1 from ontology.objects o where o.tenant_id=p_tenant and o.world=p_world and o.type_name='Consumer' and o.object_id=c.id and o.object_id<>m.consumer_id)
      or exists(select 1 from runtime.nexloop_conversations cv where cv.tenant_id=p_tenant and cv.world=p_world and cv.conversation_id=c.id and cv.consumer_id<>m.consumer_id)
      or exists(select 1 from runtime.nexloop_conversation_messages x join runtime.nexloop_conversations cv on cv.tenant_id=x.tenant_id and cv.world=x.world
                and cv.conversation_id=x.conversation_id where x.tenant_id=p_tenant and x.world=p_world and x.message_id=c.id and cv.consumer_id<>m.consumer_id)),
  'critical_alerts',(select count(*) from control.nexloop_alert_events e where e.tenant_id=p_tenant and e.world=p_world and e.severity='critical'
      and e.kind='firing' and e.recorded_at>=p_since)) into v;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
