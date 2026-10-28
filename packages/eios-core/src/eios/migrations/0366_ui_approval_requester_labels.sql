-- UI backend X1, X2 (L9 workbench UX audit, B8): the approvals view names the requester and labels the summary fields. Temporary
-- number; the dispatcher renumbers.
--
-- authz.nexloop_workbench_observe_read is renamed and wrapped (the 0329 / 0356 pattern: every verb and its checks unchanged in the
-- wrapped function). For the verb 'approvals' only, after the wrapped function verified the signed read, the member and the read Action
-- and answered, each row of to_decide, mine and to_execute gains two read-only fields; nothing else changes and no row is added or
-- dropped:
--   requested_by_username  the requester's workbench username (the account of the requester principal's subject in this tenant), or
--                          null when that person was erased as a member (NX-058 slice E), has no account, or the principal is not a
--                          membership of this tenant - never the erased placeholder name and never another tenant's account;
--   summary_labels         {field: Chinese label} for each key of `summary` (the fixed list below, the same fields as
--                          control.nexloop_approval_summary_allowed); a field without a label maps to itself.
-- The same members may see it who already see the row (any holder of nexloop.workbench.read:1 for to_decide; the requester for mine and
-- to_execute): a username is not more than the members page shows the owner, and an approver must know whose request they decide.
set local timezone = 'UTC';

create function control.nexloop_approval_summary_label(p_field text) returns text
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select case p_field
  when 'scope_kind' then '范围' when 'enabled' then '开关' when 'data_mode' then '数据模式' when 'action_type' then 'Action 类型'
  when 'amount_minor' then '金额（最小货币单位）' when 'currency' then '币种' when 'offering_revision' then '激励目录版本'
  when 'new_role' then '新角色' else p_field end
$$;
alter function control.nexloop_approval_summary_label(text) owner to nexloop_owner;
revoke all on function control.nexloop_approval_summary_label(text) from public;

create function runtime.nexloop_approval_requester_username(p_tenant text,p_principal text) returns text
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v text;
begin
 perform set_config('eios.tenant_id',p_tenant,true);  -- the membership and account rows of this tenant only (row security)
 select a.username into v from control.nexloop_browser_memberships m
  join lateral (select x.username from control.nexloop_browser_accounts x where x.tenant_id=p_tenant and x.subject_id=m.subject_id
    order by array_position(array['active','invited','disabled'],x.payload->>'status'),x.local_account_id limit 1) a on true
 where m.tenant_id=p_tenant and m.principal_id=p_principal
  and not exists(select 1 from control.nexloop_workbench_member_erasures e where e.tenant_id=p_tenant and e.subject_id=m.subject_id)
 limit 1;
 return v;
end $$;
alter function runtime.nexloop_approval_requester_username(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_approval_requester_username(text,text) from public;

create function runtime.nexloop_approval_rows_labelled(p_tenant text,p_rows jsonb) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare out jsonb;
begin
 if p_rows is null or jsonb_typeof(p_rows)<>'array' then return p_rows;end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select coalesce(jsonb_agg(r.v||jsonb_build_object('requested_by_username',runtime.nexloop_approval_requester_username(p_tenant,r.v->>'requested_by'),
   'summary_labels',coalesce((select jsonb_object_agg(k,control.nexloop_approval_summary_label(k)) from jsonb_object_keys(coalesce(r.v->'summary','{}'::jsonb)) k),'{}'::jsonb))
   order by r.n),'[]'::jsonb) into out from jsonb_array_elements(p_rows) with ordinality r(v,n);
 return out;
end $$;
alter function runtime.nexloop_approval_rows_labelled(text,jsonb) owner to nexloop_owner;
revoke all on function runtime.nexloop_approval_rows_labelled(text,jsonb) from public;

alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) rename to nexloop_workbench_observe_read_before_requester_labels;
revoke all on function authz.nexloop_workbench_observe_read_before_requester_labels(text,text,text,text,text) from public,nexloop_api;
create function authz.nexloop_workbench_observe_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare c jsonb:=p_payload::jsonb;v jsonb;v_tenant text;
begin
 v:=authz.nexloop_workbench_observe_read_before_requester_labels(p_digest,p_world,p_text,p_signature,p_payload);
 if c->>'verb' is distinct from 'approvals' or v is null then return v;end if;
 -- Verified by the wrapped function (signature, member, read Action) before it answered; the tenant is the member's.
 v_tenant:=authz.nexloop_workbench_member(p_digest,p_world)->>'tenant_id';
 return v||jsonb_build_object('to_decide',runtime.nexloop_approval_rows_labelled(v_tenant,v->'to_decide'),
  'mine',runtime.nexloop_approval_rows_labelled(v_tenant,v->'mine'),'to_execute',runtime.nexloop_approval_rows_labelled(v_tenant,v->'to_execute'));
end $$;
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_observe_read(text,text,text,text,text) from public;
grant execute on function authz.nexloop_workbench_observe_read(text,text,text,text,text) to nexloop_api;
