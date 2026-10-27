-- NX-056 follow-up (day 1 drill, L6 S1): every row of the workbench approvals view says whether the approval was executed.
-- Temporary number; the dispatcher renumbers. Published migrations are not edited.
--
-- After the requester executes an approved request its status stays 'approved' (the state machine is unchanged: consumption is
-- its own append-only record, control.nexloop_approval_consumptions), so "mine" could not tell an executed request from one still
-- to execute. runtime.nexloop_approval_view_row (latest 0339) adds two read-only fields from that record:
--   consumed     true when the approval has a consumption row (executed, or execution refused - result in 'mine' tells which)
--   consumed_at  its time, or null
-- No other field changes meaning; to_execute keeps excluding consumed approvals. Registered in tests/test_function_copy_guard.py.

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
  -- whether it was executed, from the consumption record itself
  ||coalesce((select jsonb_build_object('consumed',true,'consumed_at',k.consumed_at) from control.nexloop_approval_consumptions k
    where k.tenant_id=r.tenant_id and k.world=r.world and k.approval_id=r.approval_id),jsonb_build_object('consumed',false,'consumed_at',null))
$$;
