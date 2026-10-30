-- Relay dependency expiry (L6 S43/S45, dispatcher 2026-10-30; temporary number, renumbered at merge). On 10-22T16:00Z every
-- object the stage relay enrollment depends on expired together (EffectControl and its ledger row, the offering, the role objects,
-- the Goal); runtime.nexloop_relay_profile turned null, claim_next skipped the consumer, the relay idled without a word and no
-- alert fired for eight days. The metrics snapshot (world real) gains relay_readiness for alert rules v23:
--   active_enrollments                 consumers whose current enrollment is active
--   no_active_enrollment               1 when there is none (a relay with nothing to serve is visible, never a silent zero), else 0
--   unserviceable_enrollments          active enrollments whose relay profile is null now (claim_next would skip them)
--   dependencies_expired               dependencies of active enrollments whose valid_until has passed (or is missing/not a time)
--   dependencies_expiring_7d           dependencies whose valid_until falls in the next 7 days
--   earliest_valid_until               the earliest valid_until over all dependencies (null when there is none)
--   stalled_consumers                  enrolled, not erasing, not taken over consumers whose oldest pending message waited more than
--                                      300 seconds with no relay lease in that time: the relay ticks idle past them (S45)
-- Dependencies of one enrollment: EffectControl (object and ledger row), ServiceOffering, ConsumerServiceOffering, the role triple
-- RoleDefinition / ConsumerRoleLink / PlanStep with the step's Goal, the role's RoleExecutionCeiling (ceiling_ref) and the link's
-- RoleAssignmentScope (scope), and the enterprise slot's control (object and ledger), offering and binding. A dependency that is
-- named but missing counts as expired. Objects without a valid_until property (bindings, steps) are not counted.
create function runtime.nexloop_relay_readiness(p_tenant text,p_world text,p_now timestamptz) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 with e as (select * from (select distinct on (x.consumer_id) x.* from control.nexloop_relay_enrollments x where x.tenant_id=p_tenant and x.world=p_world
    order by x.consumer_id,x.enrollment_revision desc) z where z.state='active'),
  named as (
   select e.consumer_id,d.kind,d.type_name,d.object_id from e cross join lateral (values
    ('control','EffectControl',e.control_id),('offering','ServiceOffering',e.offering_id),('binding','ConsumerServiceOffering',e.binding_id),
    ('role','RoleDefinition',e.role_id),('link','ConsumerRoleLink',e.link_id),('step','PlanStep',e.step_id),
    ('goal','Goal',(select s.properties->>'goal_id' from ontology.objects s where s.tenant_id=p_tenant and s.world=p_world and s.object_id=e.step_id and s.type_name='PlanStep')),
    ('ceiling','RoleExecutionCeiling',(select r.properties->>'ceiling_ref' from ontology.objects r where r.tenant_id=p_tenant and r.world=p_world and r.object_id=e.role_id and r.type_name='RoleDefinition')),
    ('scope','RoleAssignmentScope',(select l.properties->>'scope' from ontology.objects l where l.tenant_id=p_tenant and l.world=p_world and l.object_id=e.link_id and l.type_name='ConsumerRoleLink')),
    ('enterprise_control','EffectControl',e.enterprise_control_id),('enterprise_offering','ServiceOffering',e.enterprise_offering_id),
    ('enterprise_binding','ConsumerServiceOffering',e.enterprise_binding_id)) d(kind,type_name,object_id)
   where d.object_id is not null or d.kind in ('goal','ceiling','scope') and (case d.kind when 'goal' then e.step_id when 'ceiling' then e.role_id else e.link_id end) is not null),
  deps as (
   select n.kind,case when o.object_id is null then '-infinity'::timestamptz
     when o.properties ? 'valid_until' then case when jsonb_typeof(o.properties->'valid_until')='string' and (o.properties->>'valid_until')~'^[0-9]{4}-[0-9]{2}-[0-9]{2}T'
      then (o.properties->>'valid_until')::timestamptz else '-infinity'::timestamptz end end until
   from named n left join ontology.objects o on o.tenant_id=p_tenant and o.world=p_world and o.object_id=n.object_id and o.type_name=n.type_name
   union all
   select 'ledger',coalesce(l.valid_until,'-infinity'::timestamptz) from e cross join lateral unnest(array[e.control_id,e.enterprise_control_id]) c(id)
    left join control.nexloop_effect_control_ledger l on l.tenant_id=p_tenant and l.world=p_world and l.control_id=c.id where c.id is not null),
  stalled as (
   select e.consumer_id from e cross join lateral (select b.conversation_id,(b.record->>'accepted_at')::timestamptz accepted from runtime.nexloop_message_outbox b
     join runtime.nexloop_conversations cv on cv.tenant_id=b.tenant_id and cv.world=b.world and cv.conversation_id=b.conversation_id
     where b.tenant_id=p_tenant and b.world=p_world and b.status='pending' and cv.consumer_id=e.consumer_id
     order by (b.record->>'accepted_at')::timestamptz limit 1) h
   where h.accepted<p_now-interval '300 seconds' and not runtime.nexloop_consumer_erasing(p_tenant,p_world,e.consumer_id)
    and control.nexloop_takeover_active(p_tenant,p_world,e.consumer_id,h.conversation_id) is null
    and not exists(select 1 from runtime.nexloop_message_relay_leases l where l.tenant_id=p_tenant and l.world=p_world and l.consumer_id=e.consumer_id
     and l.lease_until>p_now-interval '300 seconds'))
 select jsonb_build_object('active_enrollments',(select count(*) from e),'no_active_enrollment',case when exists(select 1 from e) then 0 else 1 end,
  'unserviceable_enrollments',(select count(*) from e where runtime.nexloop_relay_profile(p_tenant,p_world,e.consumer_id,null) is null),
  'dependencies_expired',(select count(*) from deps where until<=p_now),
  'dependencies_expiring_7d',(select count(*) from deps where until>p_now and until<=p_now+interval '7 days'),
  'earliest_valid_until',(select case when min(until)='-infinity' then 'missing' else to_char(min(until) at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"') end from deps where until is not null),
  'stalled_consumers',(select count(*) from stalled)) into v;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
alter function runtime.nexloop_relay_readiness(text,text,timestamptz) owner to nexloop_owner;
revoke all on function runtime.nexloop_relay_readiness(text,text,timestamptz) from public;

alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_relay_readiness;
revoke all on function runtime.nexloop_metrics_snapshot_before_relay_readiness(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp as $$
declare v jsonb:=runtime.nexloop_metrics_snapshot_before_relay_readiness(p_tenant,p_world);
begin
 if p_world is distinct from 'real' then return v;end if;
 return v||jsonb_build_object('relay_readiness',runtime.nexloop_relay_readiness(p_tenant,p_world,clock_timestamp()));
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;
