-- NX-058 workbench UI (docs/design/account-self-service.md v7 §8 slice D, the workbench pages): the owner's members view. Temporary number;
-- the dispatcher renumbers.
-- authz.nexloop_workbench_observe_read gains the verb 'members' (every other verb unchanged, as 0329 added 'approvals'), for the owner only.
-- runtime.nexloop_workbench_members_view(tenant): the tenant's workbench members (from the latest configuration and the role history), with
-- the display label (p- and 8 hex of sha256(principal), ADR-039 §6.2), username, role, membership and account status, the vote cooldown's
-- end, whether the subject was erased and whether every membership of that person is revoked; the issued, unexpired invitations; and the reset codes of the last 90 days (target, issuer,
-- times, state, owner or member code). Never a token, code, HMAC, source digest, email or password.
set local timezone = 'UTC';

create function runtime.nexloop_workbench_members_view(p_tenant text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare cfg control.nexloop_workbench_configurations:=control.nexloop_workbench_current(p_tenant);v_members jsonb;v_invitations jsonb;v_resets jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select coalesce(jsonb_agg(x.v order by x.sort,x.principal_id),'[]'::jsonb) into v_members from (
  select m.principal_id,case m.payload->>'status' when 'active' then 0 when 'suspended' then 1 when 'invited' then 2 else 3 end sort,
   jsonb_build_object('principal_id',m.principal_id,'label','p-'||left(encode(sha256(convert_to(m.principal_id,'UTF8')),'hex'),8),
    'username',a.username,'role',cfg.members->>m.principal_id,'member_status',m.payload->>'status','account_status',a.payload->>'status',
    'vote_eligible_from',runtime.nexloop_commercial_ts(control.nexloop_workbench_vote_eligible_from(p_tenant,'real',m.subject_id,array['owner'])),
    'erased',exists(select 1 from control.nexloop_workbench_member_erasures e where e.tenant_id=p_tenant and e.subject_id=m.subject_id),
    -- the staff erasure needs every membership of the person revoked (slice E, C1): the page offers it on that condition
    'person_all_revoked',not exists(select 1 from control.nexloop_browser_memberships o where o.tenant_id=p_tenant and o.subject_id=m.subject_id
      and o.payload->>'status'<>'revoked')) v
  from control.nexloop_browser_memberships m
  left join lateral (select x.username,x.payload from control.nexloop_browser_accounts x where x.tenant_id=p_tenant and x.subject_id=m.subject_id
    order by array_position(array['active','invited','disabled'],x.payload->>'status'),x.local_account_id limit 1) a on true
  where m.tenant_id=p_tenant and (cfg.members ? m.principal_id
    or exists(select 1 from control.nexloop_workbench_role_history h where h.tenant_id=p_tenant and h.principal_id=m.principal_id))
  order by 2,1 limit 500) x;
 select coalesce(jsonb_agg(jsonb_build_object('invitation_id',i.invitation_id,'username',i.username,'role',i.role,'issued_by',i.issued_by_principal,
   'issued_at',runtime.nexloop_commercial_ts(i.issued_at),'expires_at',runtime.nexloop_commercial_ts(i.expires_at)) order by i.issued_at desc),'[]'::jsonb)
  into v_invitations from control.nexloop_workbench_invitations i where i.tenant_id=p_tenant and i.state='issued' and i.expires_at>clock_timestamp();
 select coalesce(jsonb_agg(jsonb_build_object('reset_id',r.reset_id,'target_principal',r.target_principal,'issued_by',r.issued_by_principal,
   'issued_at',runtime.nexloop_commercial_ts(r.issued_at),'expires_at',runtime.nexloop_commercial_ts(r.expires_at),'state',r.state,
   'completed_at',runtime.nexloop_commercial_ts(r.completed_at),'kind',case when r.approval_id is null then 'member' else 'owner' end) order by r.issued_at desc),'[]'::jsonb)
  into v_resets from (select * from control.nexloop_workbench_reset_codes where tenant_id=p_tenant and issued_at>clock_timestamp()-interval '90 days'
   order by issued_at desc limit 200) r;
 return jsonb_build_object('members',v_members,'invitations',v_invitations,'resets',v_resets);
end $$;
alter function runtime.nexloop_workbench_members_view(text) owner to nexloop_owner;
revoke all on function runtime.nexloop_workbench_members_view(text) from public;

alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) rename to nexloop_workbench_observe_read_before_members;
revoke all on function authz.nexloop_workbench_observe_read_before_members(text,text,text,text,text) from public,nexloop_api;
create function authz.nexloop_workbench_observe_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;w jsonb;cfg control.nexloop_workbench_configurations;v_tenant text;
 v_action text:='eios:action:nexloop.workbench.read:1';
begin
 if c->>'verb' is distinct from 'members' then
  return authz.nexloop_workbench_observe_read_before_members(p_digest,p_world,p_text,p_signature,p_payload);end if;
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
 -- The owner only (members, invitations and resets are the owner's to manage, design §1b).
 w:=authz.nexloop_workbench_member(p_digest,p_world);v_tenant:=w->>'tenant_id';cfg:=control.nexloop_workbench_current(v_tenant);
 if a->>'tenant_id' is distinct from v_tenant or a->>'principal_id' is distinct from w->>'principal_id' or w->>'role' is distinct from 'owner'
   or not (cfg.roles->(w->>'role') ? v_action) then
  raise exception 'workbench observe forbidden' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 if (select count(*) from jsonb_object_keys(c))<>1 then raise exception 'workbench observe invalid' using errcode='22023';end if;
 return runtime.nexloop_workbench_members_view(v_tenant);
end $$;
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_observe_read(text,text,text,text,text) from public;
grant execute on function authz.nexloop_workbench_observe_read(text,text,text,text,text) to nexloop_api;
