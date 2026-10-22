-- NX-056 A4 (docs/design/unified-approvals.md v5 §8): the workbench's approvals view. Temporary number; the dispatcher renumbers.
--
-- authz.nexloop_workbench_observe_read gains the verb 'approvals' (every other verb unchanged; the 0256 verification), for every
-- workbench member holding the read Action - an operator or reviewer can be an approver. Three columns, all of the caller's tenant
-- and world, the caller identified by their subject (any of their principals):
--   to_decide   waiting, before due_by, not requested by the caller, the caller an eligible approver now (the decision's own check)
--               and without a vote of theirs yet;
--   mine        the caller's own requests of the last 30 days (status, result when consumed), newest first;
--   to_execute  the caller's approved, unconsumed requests before due_by, with the inputs to resubmit (the frozen body without
--               request_id and operation: the requester's own body, shown only to them).
-- Each row: approval id, capability, operation, Action, requester principal, times, status, counted approvals and quorum, the digest's
-- first 12 hex digits and the policy's summary fields (scalar values from the body, server side). The bodies of others' requests are
-- never returned. At most 50 rows per column.

create function runtime.nexloop_approval_view_row(r control.nexloop_approval_requests,pol control.nexloop_approval_policies) returns jsonb
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select jsonb_build_object('approval_id',r.approval_id,'capability',r.capability,'operation',r.body->>'operation','action',r.action_resource,
  'requested_by',r.requester_principal,'requested_at',r.requested_at,'due_by',r.due_by,'status',r.status,'digest',left(r.request_digest,12),
  'policy_version',r.policy_version,'quorum',pol.quorum,
  'approvals',(select count(distinct d.actor_subject) from control.nexloop_approval_decisions d where d.tenant_id=r.tenant_id and d.world=r.world
    and d.approval_id=r.approval_id and d.outcome='approved' and d.actor_subject<>r.requester_subject),
  'summary',coalesce((select jsonb_object_agg(f,r.body->f) from unnest(pol.summary_fields) f
    where r.body ? f and jsonb_typeof(r.body->f) in ('string','number','boolean','null')),'{}'::jsonb))
$$;
alter function runtime.nexloop_approval_view_row(control.nexloop_approval_requests,control.nexloop_approval_policies) owner to nexloop_owner;
revoke all on function runtime.nexloop_approval_view_row(control.nexloop_approval_requests,control.nexloop_approval_policies) from public;

create function runtime.nexloop_approval_view(p_tenant text,p_world text,p_principal text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_subject text:=control.nexloop_approval_subject_of(p_tenant,p_principal);v_decide jsonb;v_mine jsonb;v_execute jsonb;
begin
 if v_subject is null then raise exception 'workbench observe forbidden' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select coalesce(jsonb_agg(x.v order by x.due_by,x.approval_id),'[]'::jsonb) into v_decide from (
  select runtime.nexloop_approval_view_row(r,pol) v,r.due_by,r.approval_id from control.nexloop_approval_requests r
   join control.nexloop_approval_policies pol on pol.capability=r.capability and pol.version=r.policy_version
  where r.tenant_id=p_tenant and r.world=p_world and r.status='waiting_approval' and r.due_by>clock_timestamp() and r.requester_subject<>v_subject
   and not exists(select 1 from control.nexloop_approval_decisions d where d.tenant_id=r.tenant_id and d.world=r.world and d.approval_id=r.approval_id
    and d.actor_subject=v_subject)
   and control.nexloop_approval_approver_eligible(p_tenant,p_principal,pol.approver_roles)
  order by r.due_by,r.approval_id limit 50) x;
 perform set_config('eios.tenant_id',p_tenant,true);
 select coalesce(jsonb_agg(x.v order by x.requested_at desc,x.approval_id),'[]'::jsonb) into v_mine from (
  select runtime.nexloop_approval_view_row(r,pol)||jsonb_build_object('result',k.result) v,r.requested_at,r.approval_id from control.nexloop_approval_requests r
   join control.nexloop_approval_policies pol on pol.capability=r.capability and pol.version=r.policy_version
   left join control.nexloop_approval_consumptions k on k.tenant_id=r.tenant_id and k.world=r.world and k.approval_id=r.approval_id
  where r.tenant_id=p_tenant and r.world=p_world and r.requester_subject=v_subject and r.requested_at>clock_timestamp()-interval '30 days'
  order by r.requested_at desc,r.approval_id limit 50) x;
 select coalesce(jsonb_agg(x.v order by x.due_by,x.approval_id),'[]'::jsonb) into v_execute from (
  select runtime.nexloop_approval_view_row(r,pol)||jsonb_build_object('inputs',r.body-'request_id'-'operation') v,r.due_by,r.approval_id
  from control.nexloop_approval_requests r join control.nexloop_approval_policies pol on pol.capability=r.capability and pol.version=r.policy_version
  where r.tenant_id=p_tenant and r.world=p_world and r.requester_subject=v_subject and r.status='approved' and r.due_by>clock_timestamp()
   and not exists(select 1 from control.nexloop_approval_consumptions k where k.tenant_id=r.tenant_id and k.world=r.world and k.approval_id=r.approval_id)
  order by r.due_by,r.approval_id limit 50) x;
 return jsonb_build_object('to_decide',v_decide,'mine',v_mine,'to_execute',v_execute);
end $$;
alter function runtime.nexloop_approval_view(text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_approval_view(text,text,text) from public;

alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) rename to nexloop_workbench_observe_read_before_approvals;
revoke all on function authz.nexloop_workbench_observe_read_before_approvals(text,text,text,text,text) from public,nexloop_api;
create function authz.nexloop_workbench_observe_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;w jsonb;cfg control.nexloop_workbench_configurations;v_tenant text;
 v_action text:='eios:action:nexloop.workbench.read:1';
begin
 if c->>'verb' is distinct from 'approvals' then
  return authz.nexloop_workbench_observe_read_before_approvals(p_digest,p_world,p_text,p_signature,p_payload);end if;
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
 -- Every workbench member holding the read Action (an approver may be an operator or reviewer); the rows are the caller's own.
 w:=authz.nexloop_workbench_member(p_digest,p_world);v_tenant:=w->>'tenant_id';cfg:=control.nexloop_workbench_current(v_tenant);
 if a->>'tenant_id' is distinct from v_tenant or a->>'principal_id' is distinct from w->>'principal_id' or not (cfg.roles->(w->>'role') ? v_action) then
  raise exception 'workbench observe forbidden' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 if (select count(*) from jsonb_object_keys(c))<>1 then raise exception 'workbench observe invalid' using errcode='22023';end if;
 return runtime.nexloop_approval_view(v_tenant,p_world,w->>'principal_id');
end $$;
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_observe_read(text,text,text,text,text) from public;
grant execute on function authz.nexloop_workbench_observe_read(text,text,text,text,text) to nexloop_api;
