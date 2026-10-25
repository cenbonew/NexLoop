-- NX-058 slice C, the part without approval (docs/design/account-self-service.md v7 §1b, §1c, §4, §7; dispatcher 2026-10-23: owner-only
-- Actions wait for NX-056), and the L6 slice A/B review suggestions E-1..E-4, A-7..A-10. The migration number is temporary; it lives
-- only in the file name and the catalog. 0333-0335 are released (24th batch) and are not changed: new behaviour comes as *_v2 functions that
-- wrap them (the callers move to the v2 names; the old entry points lose their grants).
--
-- * Member changes (owner, no approval; the target an operator or reviewer, its current role and status rechecked under the member lock;
--   an owner target is refused with target_role_changed - the owner-only Actions come with NX-056):
--   change_role (operator <-> reviewer), suspend, resume, revoke. Each in one transaction: the old role's revoke form, the configuration
--   version (or, for suspend and resume, the membership status), the new role's grants, the role history, every session of the member
--   ended, every unused reset code revoked (member_changed), an audit event.
-- * Configurator path: control.nexloop_workbench_governed_drift lists the members whose role or presence a governed change (invitation:*,
--   action:*) set since the last configurator version and the member file would undo (X1; named revocations exempt, Z2);
--   control.nexloop_workbench_role_history_sync writes the role history of configurator changes and ends the sessions of a member
--   removed or moved.
-- * Every instantiation writes an audit event that names its instance record (A-8, D-3), template rewrites included.
-- * Acceptance (v2): the acceptor's ids have exactly the shapes the identity process draws (E-3), the timestamps are not in the future and
--   the membership kind is home (A-7); its audit names the instance and the configuration version (A-8).
-- * Template publication (v2): roles_manifest_version must be a positive integer (E-1, 22023); a stale expected revision is a business
--   conflict P0001 'expected_revision_mismatch', never the retryable 40001 (E-2).
-- * A-9 (documented, docs/implementation/NX-058-slice-C.md): a template carries the tenant-level resource_graph facts of the role's
--   Actions, the same bytes every grant path writes for them; acceptance rewrites them only when they differ.
set local timezone = 'UTC';

-- A-8 / D-3: one audit event per instance record written after p_after (its id, role, direction, source, digests).
create function control.nexloop_workbench_instances_audit(p_tenant text,p_after bigint,p_actor text,p_request text) returns void
 language sql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
 select 'workbench-grants:'||g.instance_id||':'||md5(p_tenant),'nexloop.workbench_member.grants_written',p_tenant,p_actor,'workbench_member',p_request,
   jsonb_build_object('instance_id',g.instance_id,'principal_id',g.principal_id,'role',g.role,'direction',g.direction,'template_version',g.template_version,
    'template_digest',g.template_digest,'facts_digest',g.facts_digest,'source',g.source),clock_timestamp()
 from control.nexloop_workbench_grant_instances g where g.tenant_id=p_tenant and g.instance_id>p_after $$;

create function control.nexloop_workbench_last_instance(p_tenant text) returns bigint language sql stable security definer
 set search_path=pg_catalog,pg_temp set row_security=on as $$
 select coalesce(max(instance_id),0) from control.nexloop_workbench_grant_instances where tenant_id=p_tenant $$;

-- The internal write with its audit (every caller below uses this one).
create function control.nexloop_workbench_grants_write_v2(p_tenant text,p_principal text,p_subject text,p_role text,p_direction text,p_source text,
  p_actor text,p_request text) returns text
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare before bigint;v text;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 before:=control.nexloop_workbench_last_instance(p_tenant);
 v:=control.nexloop_workbench_grants_write(p_tenant,p_principal,p_subject,p_role,p_direction,p_source);
 perform control.nexloop_workbench_instances_audit(p_tenant,before,p_actor,p_request);
 return v;
end $$;

-- Publication v2 (E-1, E-2, A-8): the 0335 publication underneath.
create function control.nexloop_publish_workbench_role_templates_v2(p_tenant text,p_text text,p_expected_revision integer) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v jsonb;r jsonb;before bigint;
begin
 if session_user<>'nexloop_configurator' then raise exception 'workbench role templates are trusted configuration only' using errcode='42501';end if;
 begin v:=p_text::jsonb;exception when others then raise exception 'workbench role templates invalid' using errcode='22023';end;
 if jsonb_typeof(v) is distinct from 'object'
  or exists(select 1 from jsonb_each(v) x where jsonb_typeof(x.value->'roles_manifest_version') is distinct from 'number'
            or (x.value->>'roles_manifest_version')!~'^[1-9][0-9]{0,8}$') then
  raise exception 'workbench role templates invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 before:=control.nexloop_workbench_last_instance(p_tenant);
 begin
  r:=control.nexloop_publish_workbench_role_templates(p_tenant,p_text,p_expected_revision);
 exception when serialization_failure then
  -- E-2: a stale expected revision is the caller's to re-read, never a generic retry of the same call.
  raise exception 'workbench role templates: expected_revision_mismatch' using errcode='P0001';
 end;
 perform control.nexloop_workbench_instances_audit(p_tenant,before,session_user,'workbench-templates:'||(r->>'roles_manifest_version'));
 return r;
end $$;

create function control.nexloop_workbench_templates_settle_v2(p_tenant text) returns integer
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare before bigint;n integer;
begin
 if session_user<>'nexloop_configurator' then raise exception 'workbench role templates are trusted configuration only' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 before:=control.nexloop_workbench_last_instance(p_tenant);
 n:=control.nexloop_workbench_templates_settle(p_tenant);
 perform control.nexloop_workbench_instances_audit(p_tenant,before,session_user,'workbench-templates:settle');
 return n;
end $$;

-- Acceptance v2 (E-3, A-7, A-8): the 0335 acceptance underneath, in the same transaction.
create function control.nexloop_accept_workbench_invitation_v2(p_tenant text,p_token_hmac text,p_subject jsonb,p_membership jsonb,p_account jsonb,
  p_hash text,p_source text,p_trace text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare before bigint;r jsonb;v_version integer;inst bigint;now_at timestamptz:=clock_timestamp();
begin
 if session_user<>'nexloop_identity' or jsonb_typeof(p_subject) is distinct from 'object' or jsonb_typeof(p_membership) is distinct from 'object'
  or jsonb_typeof(p_account) is distinct from 'object' then raise exception 'identity unavailable' using errcode='42501';end if;
 -- E-3: the shapes browser_http draws; A-7: as 0050's initial identity, no timestamp after now, a home membership.
 if coalesce(p_subject->>'subject_id','')!~'^workbench-subject-[0-9a-f]{32}$'
  or coalesce(p_membership->>'principal_id','')!~'^workbench-[0-9a-f]{32}$' or coalesce(p_account->>'local_account_id','')!~'^workbench-account-[0-9a-f]{32}$'
  or substr(p_subject->>'subject_id',19) is distinct from substr(p_membership->>'principal_id',11)
  or substr(p_account->>'local_account_id',19) is distinct from substr(p_membership->>'principal_id',11)
  or p_membership->>'kind' is distinct from 'home'
  or p_subject->>'created_at' is null or (p_subject->>'created_at')::timestamptz>now_at or (p_subject->>'updated_at')::timestamptz>now_at
  or p_account->>'created_at' is null or p_account->>'updated_at' is null
  or (p_account->>'created_at')::timestamptz>now_at or (p_account->>'updated_at')::timestamptz>now_at then
  raise exception 'identity unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 before:=control.nexloop_workbench_last_instance(p_tenant);
 r:=control.nexloop_accept_workbench_invitation(p_tenant,p_token_hmac,p_subject,p_membership,p_account,p_hash,p_source,p_trace);
 select manifest_version into v_version from control.nexloop_workbench_configurations where tenant_id=p_tenant order by manifest_version desc limit 1;
 select max(instance_id) into inst from control.nexloop_workbench_grant_instances where tenant_id=p_tenant and instance_id>before;
 perform control.nexloop_workbench_instances_audit(p_tenant,before,r->>'principal_id','workbench-invite:'||(r->>'invitation_id'));
 -- A separate event (audit rows are never updated: the NX-059 chain holds their digests).
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('workbench-invite-accept-detail:'||(r->>'invitation_id'),'nexloop.workbench_member.invitation_accepted_detail',p_tenant,r->>'principal_id','workbench_member',
   'workbench-invite:'||(r->>'invitation_id'),jsonb_build_object('invitation_id',r->>'invitation_id','instance_id',inst,'configuration_version',v_version),clock_timestamp());
 return r||jsonb_build_object('instance_id',inst,'configuration_version',v_version);
end $$;

-- ---- slice C: member changes without approval --------------------------------------------------------------------------------------

-- The member a change acts on, rechecked under the lock: its membership, current role and account (AA2: the target is the body's
-- target_principal only).
create function control.nexloop_workbench_change_target(p_tenant text,p_principal text,p_requester text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare m control.nexloop_browser_memberships;a control.nexloop_browser_accounts;v_role text;
begin
 if not control.nexloop_workbench_member_active(p_tenant,p_requester,true) then raise exception 'workbench member: issuer_not_owner' using errcode='22023';end if;
 if p_principal is null or p_principal=p_requester then raise exception 'workbench member: target_is_requester' using errcode='22023';end if;
 select * into m from control.nexloop_browser_memberships where tenant_id=p_tenant and principal_id=p_principal for update;
 v_role:=control.nexloop_workbench_role(p_tenant,p_principal);
 if not found or v_role is null or m.payload->>'status'='revoked' then raise exception 'workbench member: target_not_active' using errcode='22023';end if;
 -- An owner target (suspended owners included, Y5) is an owner-only Action's, with approval (NX-056).
 if v_role='owner' then raise exception 'workbench member: target_role_changed' using errcode='22023';end if;
 select * into a from control.nexloop_browser_accounts where tenant_id=p_tenant and subject_id=m.subject_id for update;
 return jsonb_build_object('principal',p_principal,'subject',m.subject_id,'role',v_role,'status',m.payload->>'status','account',to_jsonb(a));
end $$;

-- Membership status (suspended, active, revoked): the identity directory's own row, revision + 1.
create function control.nexloop_workbench_membership_status(p_tenant text,p_principal text,p_status text) returns void
 language sql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 update control.nexloop_browser_memberships set payload=payload||jsonb_build_object('status',p_status,'revision',(payload->>'revision')::bigint+1)
  where tenant_id=p_tenant and principal_id=p_principal $$;

create function control.nexloop_workbench_member_settle(p_tenant text,t jsonb,p_reason text) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 -- Every session ends; every unused reset code ends (design §4, I4).
 if t->'account'->>'local_account_id' is not null then
  perform control.nexloop_workbench_kick(p_tenant,jsonb_populate_record(null::control.nexloop_browser_accounts,t->'account'));
 end if;
 perform control.nexloop_workbench_reset_revoke(p_tenant,t->>'subject',p_reason);
end $$;

create function control.nexloop_governed_change_workbench_member(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t jsonb;op text:=body->>'operation';cur control.nexloop_workbench_configurations;v_source text:='action:'||p_intent;
 new_role text;v_version integer;keys text[];
begin
 keys:=case op when 'change_workbench_member_role' then array['new_role','operation','request_id','target_principal'] else array['operation','reason','request_id','target_principal'] end;
 if jsonb_typeof(body) is distinct from 'object' or (select array_agg(k order by k) from jsonb_object_keys(body) k) is distinct from keys
  or op not in ('change_workbench_member_role','suspend_workbench_member','resume_workbench_member','revoke_workbench_member')
  or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200
  or not control.nexloop_workbench_member_id_valid(body->>'target_principal')
  or (op='change_workbench_member_role' and coalesce(body->>'new_role','') not in ('operator','reviewer'))
  or (op<>'change_workbench_member_role' and (jsonb_typeof(body->'reason') is distinct from 'string' or char_length(btrim(body->>'reason')) not between 1 and 500)) then
  raise exception 'workbench member: invalid' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'workbench member: owner_world_only' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform control.nexloop_workbench_member_lock(p_tenant);
 -- A replay of this intent: its audit event exists; nothing again.
 if exists(select 1 from runtime.audit_events where event_id='workbench-change:'||p_intent) then
  return jsonb_build_object('target_principal',body->>'target_principal','operation',op,'replayed',true);
 end if;
 t:=control.nexloop_workbench_change_target(p_tenant,body->>'target_principal',p_principal);
 -- Before the first authority write: the governed entry's recheck of the requester keeps its revision (0338).
 perform control.nexloop_authority_handler_mark(p_tenant);
 select * into cur from control.nexloop_workbench_configurations where tenant_id=p_tenant order by manifest_version desc limit 1;
 if op='change_workbench_member_role' then
  new_role:=body->>'new_role';
  if t->>'status'<>'active' then raise exception 'workbench member: target_not_active' using errcode='22023';end if;
  if new_role=t->>'role' then raise exception 'workbench member: role_unchanged' using errcode='22023';end if;
  -- Z0: the old role's grants first, then the configuration, then the new role's grants (grant_guard checks the configured role).
  perform control.nexloop_workbench_grants_write_v2(p_tenant,t->>'principal',t->>'subject',t->>'role','revoke',v_source,p_principal,p_intent);
  v_version:=control.nexloop_workbench_configuration_append(p_tenant,cur.members||jsonb_build_object(t->>'principal',new_role),v_source);
  perform control.nexloop_workbench_grants_write_v2(p_tenant,t->>'principal',t->>'subject',new_role,'grant',v_source,p_principal,p_intent);
  insert into control.nexloop_workbench_role_history(tenant_id,subject_id,principal_id,role,event,source) values
   (p_tenant,t->>'subject',t->>'principal',t->>'role','stop',v_source),(p_tenant,t->>'subject',t->>'principal',new_role,'start',v_source);
 elsif op='suspend_workbench_member' then
  if t->>'status'<>'active' then raise exception 'workbench member: target_not_active' using errcode='22023';end if;
  perform control.nexloop_workbench_grants_write_v2(p_tenant,t->>'principal',t->>'subject',t->>'role','revoke',v_source,p_principal,p_intent);
  perform control.nexloop_workbench_membership_status(p_tenant,t->>'principal','suspended');
  insert into control.nexloop_workbench_role_history(tenant_id,subject_id,principal_id,role,event,source) values(p_tenant,t->>'subject',t->>'principal',t->>'role','stop',v_source);
 elsif op='resume_workbench_member' then
  -- AA1: a suspension revoked the grants; resuming grants the role's current template again.
  if t->>'status'<>'suspended' then raise exception 'workbench member: target_not_suspended' using errcode='22023';end if;
  perform control.nexloop_workbench_membership_status(p_tenant,t->>'principal','active');
  perform control.nexloop_workbench_grants_write_v2(p_tenant,t->>'principal',t->>'subject',t->>'role','grant',v_source,p_principal,p_intent);
  insert into control.nexloop_workbench_role_history(tenant_id,subject_id,principal_id,role,event,source) values(p_tenant,t->>'subject',t->>'principal',t->>'role','start',v_source);
 else  -- revoke
  if t->>'status'='active' then
   perform control.nexloop_workbench_grants_write_v2(p_tenant,t->>'principal',t->>'subject',t->>'role','revoke',v_source,p_principal,p_intent);
   insert into control.nexloop_workbench_role_history(tenant_id,subject_id,principal_id,role,event,source) values(p_tenant,t->>'subject',t->>'principal',t->>'role','stop',v_source);
  end if;
  v_version:=control.nexloop_workbench_configuration_append(p_tenant,cur.members-(t->>'principal'),v_source);
  perform control.nexloop_workbench_membership_status(p_tenant,t->>'principal','revoked');
 end if;
 if op<>'resume_workbench_member' then perform control.nexloop_workbench_member_settle(p_tenant,t,'member_changed');end if;
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('workbench-change:'||p_intent,'nexloop.workbench_member.'||replace(op,'_workbench_member',''),p_tenant,p_principal,
   'workbench_member',p_intent,jsonb_build_object('target_principal',t->>'principal','from_role',t->>'role','to_role',coalesce(new_role,t->>'role'),
    'operation',op,'configuration_version',v_version,'reason',body->>'reason'),clock_timestamp());
 return jsonb_build_object('target_principal',t->>'principal','operation',op,'role',coalesce(new_role,t->>'role'),'configuration_version',v_version,'replayed',false);
end $$;

-- ---- configurator path ---------------------------------------------------------------------------------------------------------------

-- X1: the members whose role or presence a governed change set since the last configurator version, and that the member file would set
-- differently. p_named: the principals --revoke-member names (an explicit removal, Z2).
create function control.nexloop_workbench_governed_drift(p_tenant text,p_members jsonb,p_named text[]) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare last_conf integer;cur control.nexloop_workbench_configurations;base jsonb;p text;out jsonb:='[]';prior text:=current_setting('eios.tenant_id',true);
begin
 if session_user<>'nexloop_configurator' then raise exception 'workbench members are trusted configuration only' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into cur from control.nexloop_workbench_configurations where tenant_id=p_tenant order by manifest_version desc limit 1;
 select max(manifest_version) into last_conf from control.nexloop_workbench_configurations where tenant_id=p_tenant and recorded_by='nexloop_configurator';
 if cur.tenant_id is null or last_conf is null or last_conf=cur.manifest_version then perform set_config('eios.tenant_id',coalesce(prior,''),true);return out;end if;
 select members into base from control.nexloop_workbench_configurations where tenant_id=p_tenant and manifest_version=last_conf;
 for p in select k from (select jsonb_object_keys(cur.members) k union select jsonb_object_keys(base) k) x loop
  if (cur.members->>p) is distinct from (base->>p) and (p_members->>p) is distinct from (cur.members->>p) and not (p=any(coalesce(p_named,'{}'))) then
   out:=out||jsonb_build_array(jsonb_build_object('principal_id',p,'role',cur.members->>p));
  end if;
 end loop;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return out;
end $$;

-- After a configurator member configuration: the role history of what it changed (start for a member now holding a role, stop for one
-- no longer holding it), and every session of a member removed or moved ends (S4).
create function control.nexloop_workbench_role_history_sync(p_tenant text) returns integer
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare cur control.nexloop_workbench_configurations;h record;m record;n integer:=0;a control.nexloop_browser_accounts;prior text:=current_setting('eios.tenant_id',true);
begin
 if session_user<>'nexloop_configurator' then raise exception 'workbench members are trusted configuration only' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform control.nexloop_workbench_member_lock(p_tenant);
 select * into cur from control.nexloop_workbench_configurations where tenant_id=p_tenant order by manifest_version desc limit 1;
 -- Open holdings that the configuration no longer has: stop, and the member's sessions end.
 for h in select distinct on (r.principal_id,r.role) r.principal_id,r.subject_id,r.role,r.event from control.nexloop_workbench_role_history r
   where r.tenant_id=p_tenant order by r.principal_id,r.role,r.entry_id desc loop
  if h.event='start' and (cur.members->>h.principal_id) is distinct from h.role then
   insert into control.nexloop_workbench_role_history(tenant_id,subject_id,principal_id,role,event,source) values(p_tenant,h.subject_id,h.principal_id,h.role,'stop','configurator');
   select * into a from control.nexloop_browser_accounts where tenant_id=p_tenant and subject_id=h.subject_id;
   if found then perform control.nexloop_workbench_kick(p_tenant,a);end if;
   perform control.nexloop_workbench_reset_revoke(p_tenant,h.subject_id,'member_changed');
   n:=n+1;
  end if;
 end loop;
 -- Configured active members without an open holding of their role: start.
 for m in select c.key principal_id,c.value role,bm.subject_id from jsonb_each_text(cur.members) c
   join control.nexloop_browser_memberships bm on bm.tenant_id=p_tenant and bm.principal_id=c.key and bm.payload->>'status'='active' loop
  if coalesce((select r.event from control.nexloop_workbench_role_history r where r.tenant_id=p_tenant and r.principal_id=m.principal_id and r.role=m.role
      order by r.entry_id desc limit 1),'stop')<>'start' then
   insert into control.nexloop_workbench_role_history(tenant_id,subject_id,principal_id,role,event,source) values(p_tenant,m.subject_id,m.principal_id,m.role,'start','configurator');
   n:=n+1;
  end if;
 end loop;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return n;
end $$;

do $$ declare f text; begin
 foreach f in array array['control.nexloop_workbench_instances_audit(text,bigint,text,text)','control.nexloop_workbench_last_instance(text)',
   'control.nexloop_workbench_grants_write_v2(text,text,text,text,text,text,text,text)','control.nexloop_publish_workbench_role_templates_v2(text,text,integer)',
   'control.nexloop_workbench_templates_settle_v2(text)',
   'control.nexloop_accept_workbench_invitation_v2(text,text,jsonb,jsonb,jsonb,text,text,text)',
   'control.nexloop_workbench_change_target(text,text,text)','control.nexloop_workbench_membership_status(text,text,text)',
   'control.nexloop_workbench_member_settle(text,jsonb,text)','control.nexloop_governed_change_workbench_member(text,text,text,text,text,jsonb)',
   'control.nexloop_workbench_governed_drift(text,jsonb,text[])','control.nexloop_workbench_role_history_sync(text)'] loop
  execute format('alter function %s owner to nexloop_owner',f);
  execute format('revoke all on function %s from public',f);
 end loop;
end $$;
-- The callers move to v2; the 0335 entry points keep running only underneath them.
revoke execute on function control.nexloop_publish_workbench_role_templates(text,text,integer),control.nexloop_workbench_templates_settle(text) from nexloop_configurator;
revoke execute on function control.nexloop_accept_workbench_invitation(text,text,jsonb,jsonb,jsonb,text,text,text) from nexloop_identity;
grant execute on function control.nexloop_publish_workbench_role_templates_v2(text,text,integer),control.nexloop_workbench_templates_settle_v2(text),
 control.nexloop_workbench_governed_drift(text,jsonb,text[]),control.nexloop_workbench_role_history_sync(text) to nexloop_configurator;
grant execute on function control.nexloop_accept_workbench_invitation_v2(text,text,jsonb,jsonb,jsonb,text,text,text) to nexloop_identity;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('workbench_member.change_role','change_workbench_member_role','human','control.nexloop_governed_change_workbench_member(text,text,text,text,text,jsonb)','NX-058'),
 ('workbench_member.suspend','suspend_workbench_member','human','control.nexloop_governed_change_workbench_member(text,text,text,text,text,jsonb)','NX-058'),
 ('workbench_member.resume','resume_workbench_member','human','control.nexloop_governed_change_workbench_member(text,text,text,text,text,jsonb)','NX-058'),
 ('workbench_member.revoke','revoke_workbench_member','human','control.nexloop_governed_change_workbench_member(text,text,text,text,text,jsonb)','NX-058');
