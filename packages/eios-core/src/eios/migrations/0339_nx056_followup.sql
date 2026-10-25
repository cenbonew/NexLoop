-- NX-056 follow-up (L6 reviews 11-14; dispatcher 2026-10-25). Temporary number; the dispatcher renumbers. Published migrations are not
-- edited; the functions below are re-created with only these changes (tests/test_function_copy_guard.py).
-- 1. Lock order when the session's tenant is missing or another tenant (L6 review 11 follow-up): a row-level BEFORE trigger on member
--    and workbench configuration rows. An insert takes the target tenant's lock before any row exists (the right order). An update or
--    delete of a row of another tenant than the session's (or without one) cannot reach the row for an application role (forced RLS
--    on memberships; every application writer goes through owner functions that set eios.tenant_id: 0050, 0147, 0333-0335) and is
--    refused here besides, for any role but a superuser; a superuser (tests, maintenance) takes the target tenant's lock there, after
--    the row (the one documented exception to the order).
-- 2. Checked, no change: the batch's tenant reference on the eligibility timeline (0332) cannot fail a member write - memberships and
--    workbench configurations reference the tenant themselves (0158), so a timeline row is only written for a registered tenant.
-- 3. control.nexloop_approval_eligibility_trigger: several tenant locks of one subject change are taken in tenant order.
-- 4. runtime.nexloop_approval_view_row: the list's approval count leaves out a self vote that does not count now (the mode off or not in
--    effect), as the consumption does.

create function control.nexloop_approval_row_lock() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
declare t text:=case when tg_op='DELETE' then old.tenant_id else new.tenant_id end;s text:=nullif(current_setting('eios.tenant_id',true),'');
begin
 if tg_op='INSERT' then
  perform control.nexloop_approval_tenant_lock(t);  -- no row is locked yet
 elsif s is distinct from t or (tg_op='UPDATE' and old.tenant_id<>new.tenant_id) then
  if not coalesce((select rolsuper from pg_roles where rolname=session_user),false) then
   raise exception 'approval: member change outside the session tenant' using errcode='42501';end if;
  perform control.nexloop_approval_tenant_lock(t);
  if tg_op='UPDATE' and old.tenant_id<>new.tenant_id then perform control.nexloop_approval_tenant_lock(old.tenant_id);end if;
 end if;  -- the session's own tenant: the statement trigger (0332) has taken its lock before any row
 return case when tg_op='DELETE' then old else new end;
end $$;
alter function control.nexloop_approval_row_lock() owner to nexloop_owner;
revoke all on function control.nexloop_approval_row_lock() from public;
create trigger nx056_approval_row_lock before insert or update or delete on control.nexloop_browser_memberships
 for each row execute function control.nexloop_approval_row_lock();
create trigger nx056_approval_row_lock before insert on control.nexloop_workbench_configurations
 for each row execute function control.nexloop_approval_row_lock();

create or replace function control.nexloop_approval_eligibility_trigger() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text;prior text:=current_setting('eios.tenant_id',true);
begin
 if tg_table_name='nexloop_browser_memberships' then
  perform control.nexloop_approval_eligibility_record(case when tg_op='DELETE' then old.tenant_id else new.tenant_id end);
  if tg_op='UPDATE' and old.tenant_id<>new.tenant_id then perform control.nexloop_approval_eligibility_record(old.tenant_id);end if;
 elsif tg_table_name='nexloop_browser_subjects' then
  -- A subject has no tenant: every tenant it is a member of.
  for t in select distinct m.tenant_id from control.nexloop_browser_memberships m where m.subject_id=coalesce(new.subject_id,old.subject_id)
    order by m.tenant_id loop  -- one order for several tenant locks
   perform control.nexloop_approval_eligibility_record(t);
  end loop;
 elsif tg_table_name='nexloop_workbench_configurations' then
  perform control.nexloop_approval_eligibility_record(new.tenant_id);
 else  -- a new policy version: every tenant (its approver roles may differ)
  for t in select tenant_id from control.nexloop_tenants order by tenant_id loop
   perform control.nexloop_approval_eligibility_record(t);
  end loop;
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return null;
end $$;

create or replace function runtime.nexloop_approval_view_row(r control.nexloop_approval_requests,pol control.nexloop_approval_policies) returns jsonb
 language sql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select jsonb_build_object('approval_id',r.approval_id,'capability',r.capability,'operation',r.body->>'operation','action',r.action_resource,
  'requested_by',r.requester_principal,'requested_at',r.requested_at,'due_by',r.due_by,'status',r.status,'digest',left(r.request_digest,12),
  'policy_version',r.policy_version,'quorum',pol.quorum,
  'approvals',(select count(distinct d.actor_subject) from control.nexloop_approval_decisions d where d.tenant_id=r.tenant_id and d.world=r.world
    and d.approval_id=r.approval_id and d.outcome='approved' and (d.actor_subject<>r.requester_subject
     -- a self vote counts in the list only while the single-person mode takes effect (as at consumption)
     or (d.self_approved and control.nexloop_approval_solo_effective(r.tenant_id,r.world,r.capability,r.policy_version,r.requester_subject)))
    and d.actor_subject is distinct from control.nexloop_approval_target_subject(r.tenant_id,r.body,pol.target_subject_field)),
  'summary',coalesce((select jsonb_object_agg(f,r.body->f) from unnest(pol.summary_fields) f
    where r.body ? f and jsonb_typeof(r.body->f) in ('string','number','boolean','null')),'{}'::jsonb))
$$;
