-- One clock reading per issue (L6 S1 after CI seq 186; the same fix as control.nexloop_approval_open): these functions took a second
-- clock_timestamp() (or a column default) for the expiry or due time, so it lay its validity plus the microseconds between the readings
-- after the issue time. Each now reads the clock once and derives the rest from it. Nothing else changes. Copy guard.
--   control.nexloop_governed_invite_workbench_member (0371), control.nexloop_governed_issue_workbench_owner_reset and
--   control.nexloop_governed_invite_workbench_owner (0348), control.nexloop_governed_issue_workbench_reset (0333): issued_at,
--   expires_at = issued_at + validity and the audit time;
--   control.nexloop_workbench_oidc_begin (0365): created_at and expires_at = created_at + 5 minutes;
--   authz.nexloop_remediation_plan_command (0338): created_at (and updated_at), due_by = created_at + due_after_seconds and the
--   proposed event's recorded_at (they came from column defaults).
-- The migration number is temporary; it lives only in the file name and the catalog.


create or replace function control.nexloop_governed_invite_workbench_member(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r control.nexloop_workbench_invitations;v_hours integer;v_now timestamptz;
begin
 if jsonb_typeof(body) is distinct from 'object' or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','username','role','token_hmac','issuer_source','expires_in_hours','sso_provider'))
  or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200
  or coalesce(body->>'username','')!~'^[a-z0-9][a-z0-9._-]{2,63}$' or coalesce(body->>'token_hmac','')!~'^[0-9a-f]{64}$'
  or coalesce(body->>'issuer_source','')!~'^[0-9a-f]{64}$' or jsonb_typeof(body->'expires_in_hours') is distinct from 'number'
  or (body->>'expires_in_hours')!~'^[0-9]{1,2}$' or (body->>'expires_in_hours')::integer not between 1 and 72
  or (body ? 'sso_provider' and (jsonb_typeof(body->'sso_provider') is distinct from 'string' or body->>'sso_provider'!~'^[a-z][a-z0-9_-]{0,31}$')) then
  raise exception 'workbench member: invalid' using errcode='22023';end if;
 -- invite is for operators and reviewers; an owner is invited by invite_owner, with approval (slice C, design §1b).
 if body->>'role' is null or body->>'role' not in ('operator','reviewer') then raise exception 'workbench member: role_not_allowed' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'workbench member: owner_world_only' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform control.nexloop_workbench_member_lock(p_tenant);
 select * into r from control.nexloop_workbench_invitations where tenant_id=p_tenant and request_id=body->>'request_id';
 if found then
  if (r.username,r.role,r.token_hmac,r.sso_provider) is distinct from (body->>'username',body->>'role',body->>'token_hmac',body->>'sso_provider') then
   raise exception 'workbench member: request_reused' using errcode='22023';end if;
  return jsonb_build_object('invitation_id',r.invitation_id,'username',r.username,'role',r.role,'expires_at',r.expires_at,'sso_provider',r.sso_provider,'replayed',true);
 end if;
 if not control.nexloop_workbench_member_active(p_tenant,p_principal,true) then raise exception 'workbench member: issuer_not_owner' using errcode='22023';end if;
 if (select count(*) from control.nexloop_workbench_invitations where tenant_id=p_tenant and issued_by_principal=p_principal
     and issued_at>clock_timestamp()-interval '15 minutes')>=10 then raise exception 'workbench member: rate_limited' using errcode='22023';end if;
 if exists(select 1 from control.nexloop_browser_accounts a where a.tenant_id=p_tenant and lower(a.username)=body->>'username') then
  raise exception 'workbench member: username_taken' using errcode='22023';end if;
 update control.nexloop_workbench_invitations set state='revoked',revoked_reason='reissued' where tenant_id=p_tenant and username=body->>'username' and state='issued';
 v_hours:=(body->>'expires_in_hours')::integer;
 v_now:=clock_timestamp();  -- one reading: the issue time, the expiry and the audit time are the same instant
 insert into control.nexloop_workbench_invitations(tenant_id,request_id,username,role,token_hmac,issued_by_principal,issued_by_subject,issued_at,expires_at,issuer_source,state,sso_provider)
  values(p_tenant,body->>'request_id',body->>'username',body->>'role',body->>'token_hmac',p_principal,p_subject,v_now,
   v_now+make_interval(hours=>v_hours),body->>'issuer_source','issued',body->>'sso_provider') returning * into r;
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('workbench-invite:'||r.invitation_id,'nexloop.workbench_member.invite',p_tenant,p_principal,'workbench_member',p_intent,
   jsonb_build_object('invitation_id',r.invitation_id,'role',r.role,'expires_at',r.expires_at,'sso_provider',r.sso_provider),v_now);
 return jsonb_build_object('invitation_id',r.invitation_id,'username',r.username,'role',r.role,'expires_at',r.expires_at,'sso_provider',r.sso_provider,'replayed',false);
end $$;

create or replace function control.nexloop_governed_issue_workbench_owner_reset(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r control.nexloop_workbench_reset_codes;a control.nexloop_browser_accounts;target_subject text;v_approval uuid;v_now timestamptz;
begin
 if jsonb_typeof(body) is distinct from 'object'
  or (select array_agg(k order by k) from jsonb_object_keys(body) k) is distinct from array['code_hmac','issuer_source_hmac','operation','request_id','target_principal']
  or body->>'operation' is distinct from 'issue_workbench_owner_reset'
  or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200
  or not control.nexloop_workbench_member_id_valid(body->>'target_principal')
  or coalesce(body->>'code_hmac','')!~'^[0-9a-f]{64}$' or coalesce(body->>'issuer_source_hmac','')!~'^[0-9a-f]{64}$' then
  raise exception 'workbench member: invalid' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'workbench member: owner_world_only' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform control.nexloop_workbench_member_lock(p_tenant);
 select * into r from control.nexloop_workbench_reset_codes where tenant_id=p_tenant and request_id=body->>'request_id';
 if found then
  -- A replay of this handler's own earlier execution in the same request (the governed entry replays a finished claim before it
  -- gets here; this guards the handler on its own): the same code only, never a second one.
  if r.target_principal is distinct from body->>'target_principal' or r.code_hmac is distinct from body->>'code_hmac' then
   raise exception 'workbench member: request_reused' using errcode='22023';end if;
  return jsonb_build_object('reset_id',r.reset_id,'target_principal',r.target_principal,'expires_at',r.expires_at,'replayed',true);
 end if;
 v_approval:=control.nexloop_workbench_owner_approval_v2(p_tenant,p_world,'workbench_member.issue_owner_reset',p_principal,body,'target_principal');
 if not control.nexloop_workbench_member_active(p_tenant,p_principal,true) then raise exception 'workbench member: issuer_not_owner' using errcode='22023';end if;
 -- The issuing owner's limit, shared with issue_reset (design §5).
 if (select count(*) from control.nexloop_workbench_reset_codes where tenant_id=p_tenant and issued_by_principal=p_principal
     and issued_at>clock_timestamp()-interval '15 minutes')>=10 then raise exception 'workbench member: rate_limited' using errcode='22023';end if;
 if body->>'target_principal'=p_principal then raise exception 'workbench member: target_is_requester' using errcode='22023';end if;
 -- The target, rechecked under the lock: an active owner (an operator or reviewer is issue_reset's).
 if control.nexloop_workbench_role(p_tenant,body->>'target_principal') is distinct from 'owner' then
  raise exception 'workbench member: target_role_changed' using errcode='22023';end if;
 if not control.nexloop_workbench_member_active(p_tenant,body->>'target_principal',true) then
  raise exception 'workbench member: target_not_active' using errcode='22023';end if;
 a:=control.nexloop_workbench_member_account(p_tenant,body->>'target_principal');
 if a.local_account_id is null or a.payload->>'status' is distinct from 'active' then raise exception 'workbench member: target_not_active' using errcode='22023';end if;
 target_subject:=a.subject_id;
 perform control.nexloop_workbench_reset_revoke(p_tenant,target_subject,'reissued');
 v_now:=clock_timestamp();  -- one reading: the issue time, the expiry and the audit time are the same instant
 insert into control.nexloop_workbench_reset_codes(tenant_id,request_id,target_principal,target_subject,code_hmac,issued_by_principal,issued_by_subject,
   issued_at,expires_at,issuer_source,state,approval_id)
  values(p_tenant,body->>'request_id',body->>'target_principal',target_subject,body->>'code_hmac',p_principal,p_subject,v_now,
   v_now+interval '30 minutes',body->>'issuer_source_hmac','issued',v_approval) returning * into r;
 -- R5: only now (the approval consumed by this execution) do the target's sessions end.
 perform control.nexloop_workbench_kick(p_tenant,a);
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('workbench-reset-issue:'||r.reset_id,'nexloop.workbench_member.issue_owner_reset',p_tenant,p_principal,'workbench_member',p_intent,
   jsonb_build_object('reset_id',r.reset_id,'target_principal',r.target_principal,'expires_at',r.expires_at,
    'approval_id',current_setting('nexloop.approval_in_flight',true)),v_now);
 return jsonb_build_object('reset_id',r.reset_id,'target_principal',r.target_principal,'expires_at',r.expires_at,'replayed',false);
end $$;

create or replace function control.nexloop_governed_invite_workbench_owner(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r control.nexloop_workbench_invitations;v_hours integer;v_approval uuid;v_now timestamptz;
begin
 if jsonb_typeof(body) is distinct from 'object'
  or (select array_agg(k order by k) from jsonb_object_keys(body) k) is distinct from array['expires_in_hours','issuer_source_hmac','operation','request_id','token_hmac','username']
  or body->>'operation' is distinct from 'invite_workbench_owner'
  or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200
  or coalesce(body->>'username','')!~'^[a-z0-9][a-z0-9._-]{2,63}$' or coalesce(body->>'token_hmac','')!~'^[0-9a-f]{64}$'
  or coalesce(body->>'issuer_source_hmac','')!~'^[0-9a-f]{64}$' or jsonb_typeof(body->'expires_in_hours') is distinct from 'number'
  or (body->>'expires_in_hours')!~'^[0-9]{1,2}$' or (body->>'expires_in_hours')::integer not between 1 and 72 then
  raise exception 'workbench member: invalid' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'workbench member: owner_world_only' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform control.nexloop_workbench_member_lock(p_tenant);
 select * into r from control.nexloop_workbench_invitations where tenant_id=p_tenant and request_id=body->>'request_id';
 if found then
  -- A replay of this handler's own earlier execution (as above): the same invitation, never a second token.
  if (r.username,r.role,r.token_hmac) is distinct from (body->>'username','owner',body->>'token_hmac') then raise exception 'workbench member: request_reused' using errcode='22023';end if;
  return jsonb_build_object('invitation_id',r.invitation_id,'username',r.username,'role',r.role,'expires_at',r.expires_at,'replayed',true);
 end if;
 v_approval:=control.nexloop_workbench_owner_approval_v2(p_tenant,p_world,'workbench_member.invite_owner',p_principal,body,null);
 if not control.nexloop_workbench_member_active(p_tenant,p_principal,true) then raise exception 'workbench member: issuer_not_owner' using errcode='22023';end if;
 if (select count(*) from control.nexloop_workbench_invitations where tenant_id=p_tenant and issued_by_principal=p_principal
     and issued_at>clock_timestamp()-interval '15 minutes')>=10 then raise exception 'workbench member: rate_limited' using errcode='22023';end if;
 if exists(select 1 from control.nexloop_browser_accounts a where a.tenant_id=p_tenant and lower(a.username)=body->>'username') then
  raise exception 'workbench member: username_taken' using errcode='22023';end if;
 update control.nexloop_workbench_invitations set state='revoked',revoked_reason='reissued' where tenant_id=p_tenant and username=body->>'username' and state='issued';
 v_hours:=(body->>'expires_in_hours')::integer;
 v_now:=clock_timestamp();  -- one reading: the issue time, the expiry and the audit time are the same instant
 insert into control.nexloop_workbench_invitations(tenant_id,request_id,username,role,token_hmac,issued_by_principal,issued_by_subject,issued_at,expires_at,issuer_source,state,approval_id)
  values(p_tenant,body->>'request_id',body->>'username','owner',body->>'token_hmac',p_principal,p_subject,v_now,
   v_now+make_interval(hours=>v_hours),body->>'issuer_source_hmac','issued',v_approval) returning * into r;
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('workbench-invite:'||r.invitation_id,'nexloop.workbench_member.invite_owner',p_tenant,p_principal,'workbench_member',p_intent,
   jsonb_build_object('invitation_id',r.invitation_id,'role',r.role,'expires_at',r.expires_at,'approval_id',v_approval),v_now);
 return jsonb_build_object('invitation_id',r.invitation_id,'username',r.username,'role',r.role,'expires_at',r.expires_at,'replayed',false);
end $$;

create or replace function control.nexloop_governed_issue_workbench_reset(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r control.nexloop_workbench_reset_codes;a control.nexloop_browser_accounts;target_subject text;v_now timestamptz;
begin
 if jsonb_typeof(body) is distinct from 'object' or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','target_principal','code_hmac','issuer_source'))
  or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200
  or jsonb_typeof(body->'target_principal') is distinct from 'string' or char_length(body->>'target_principal') not between 1 and 320
  or coalesce(body->>'code_hmac','')!~'^[0-9a-f]{64}$' or (body ? 'issuer_source' and coalesce(body->>'issuer_source','')!~'^[0-9a-f]{64}$') then
  raise exception 'workbench member: invalid' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'workbench member: owner_world_only' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform control.nexloop_workbench_member_lock(p_tenant);
 select * into r from control.nexloop_workbench_reset_codes where tenant_id=p_tenant and request_id=body->>'request_id';
 if found then
  if r.target_principal is distinct from body->>'target_principal' or r.code_hmac is distinct from body->>'code_hmac' then
   raise exception 'workbench member: request_reused' using errcode='22023';end if;
  return jsonb_build_object('reset_id',r.reset_id,'target_principal',r.target_principal,'expires_at',r.expires_at,'replayed',true);
 end if;
 -- The issuer, rechecked under the lock (S-1): still an active owner of the tenant's workbench.
 if not control.nexloop_workbench_member_active(p_tenant,p_principal,true) then raise exception 'workbench member: issuer_not_owner' using errcode='22023';end if;
 -- Per issuing owner (design §4, I-1): at most 10 codes in 15 minutes, so a stolen owner session cannot keep ending a member's sessions.
 if (select count(*) from control.nexloop_workbench_reset_codes where tenant_id=p_tenant and issued_by_principal=p_principal
     and issued_at>clock_timestamp()-interval '15 minutes')>=10 then raise exception 'workbench member: rate_limited' using errcode='22023';end if;
 if body->>'target_principal'=p_principal then raise exception 'workbench member: target_is_requester' using errcode='22023';end if;
 -- The target's current role and status, rechecked under the lock (§1b, V1/Y5): an owner target is issue_owner_reset's.
 if control.nexloop_workbench_role(p_tenant,body->>'target_principal')='owner' then raise exception 'workbench member: target_role_changed' using errcode='22023';end if;
 if not control.nexloop_workbench_member_active(p_tenant,body->>'target_principal',false) then
  raise exception 'workbench member: target_not_active' using errcode='22023';end if;
 a:=control.nexloop_workbench_member_account(p_tenant,body->>'target_principal');
 if a.local_account_id is null or a.payload->>'status' is distinct from 'active' then raise exception 'workbench member: target_not_active' using errcode='22023';end if;
 target_subject:=a.subject_id;
 perform control.nexloop_workbench_reset_revoke(p_tenant,target_subject,'reissued');
 v_now:=clock_timestamp();  -- one reading: the issue time, the expiry and the audit time are the same instant
 insert into control.nexloop_workbench_reset_codes(tenant_id,request_id,target_principal,target_subject,code_hmac,issued_by_principal,issued_by_subject,
   issued_at,expires_at,issuer_source,state)
  values(p_tenant,body->>'request_id',body->>'target_principal',target_subject,body->>'code_hmac',p_principal,p_subject,v_now,
   v_now+interval '30 minutes',body->>'issuer_source','issued') returning * into r;
 perform control.nexloop_workbench_kick(p_tenant,a);
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('workbench-reset-issue:'||r.reset_id,'nexloop.workbench_member.issue_reset',p_tenant,p_principal,'workbench_member',p_intent,
   jsonb_build_object('reset_id',r.reset_id,'target_principal',r.target_principal,'expires_at',r.expires_at),v_now);
 return jsonb_build_object('reset_id',r.reset_id,'target_principal',r.target_principal,'expires_at',r.expires_at,'replayed',false);
end $$;

-- The completion's lookup (nexloop_identity): the target account's current hash and history (the caller checks reuse with them, as
-- the 0198 tool does), or null for any code that cannot complete now - one answer for every refusal.

create or replace function control.nexloop_workbench_oidc_begin(p_tenant text,p_provider text,p_state_hmac text,p_binding_hmac text) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_now timestamptz:=clock_timestamp();  -- one reading: created_at and expires_at are the same instant
begin
 if session_user<>'nexloop_identity' or coalesce(p_state_hmac,'')!~'^[0-9a-f]{64}$' or coalesce(p_binding_hmac,'')!~'^[0-9a-f]{64}$'
  or coalesce(p_provider,'')!~'^[a-z][a-z0-9_-]{0,31}$' then raise exception 'identity unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 delete from control.nexloop_workbench_oidc_states where tenant_id=p_tenant and expires_at<v_now-interval '1 day';
 insert into control.nexloop_workbench_oidc_states(state_hmac,tenant_id,provider_id,binding_hmac,created_at,expires_at)
  values(p_state_hmac,p_tenant,p_provider,p_binding_hmac,v_now,v_now+interval '5 minutes');
end $$;


create or replace function authz.nexloop_remediation_plan_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-remediation-plan-v1',
  'eios:action:nexloop.remediation.plan:1',array['nexloop_domain_worker']);
 c jsonb:=p_payload::jsonb;v_limit integer;v_closed text;m control.nexloop_remediation_rules;f runtime.nexloop_effect_failures;
 i runtime.nexloop_effect_intents;r jsonb;v_decision text;v_rules integer;v_id uuid;v_recent integer;v_consumer_recent integer;
 v_code text;v_expired integer:=0;v_hour timestamptz;counts jsonb:='{}'::jsonb;x record;v_now timestamptz;v_actor text:=(p_text::jsonb)->>'principal_id';v_origin uuid;v_held text[];
begin
 -- Design §6.1 (S-3): lock_timeout 1 s here (read at each lock wait); the 5 s statement_timeout is set by the port before this call
 -- (a setting made inside the statement would not bound it, L6 R1 review 1).
 perform set_config('lock_timeout','1000',true);
 perform set_config('eios.tenant_id',t,true);
 if c->>'verb' is distinct from 'round' or (select array_agg(k order by k) from jsonb_object_keys(c) k) is distinct from array['limit','verb']
  or coalesce(c->>'limit','')!~'^[1-9][0-9]?$' or (c->>'limit')::integer>50 then
  raise exception 'remediation plan invalid' using errcode='22023';end if;
 v_limit:=(c->>'limit')::integer;
 if not pg_try_advisory_xact_lock(hashtext('nexloop-remediation-plan:'||t||':'||p_world)) then
  return jsonb_build_object('status','busy');end if;
 -- Due proposals expire whatever the switches (design §9.4).
 for x in select q.remediation_id from runtime.nexloop_remediation_intents q where q.tenant_id=t and q.world=p_world and q.state='proposed'
   and q.due_by<=clock_timestamp() order by q.due_by limit 200 for update skip locked loop
  perform runtime.nexloop_remediation_move(x.remediation_id,'expired','due_by_passed','service',v_actor,null);
  v_expired:=v_expired+1;
 end loop;
 -- The switches, read at the start of the round (design §9.4): the deployment, the tenant switch, the tenant control pause.
 v_closed:=case when coalesce(current_setting('nexloop.remediation_enabled',true),'') is distinct from 'true' then 'deployment'
  when runtime.nexloop_remediation_paused(t,p_world) then 'tenant'
  when exists(select 1 from control.nexloop_control_scopes s where s.tenant_id=t and s.world=p_world and s.paused and s.scope_kind='tenant' and s.scope_ref='*') then 'control'
  end;
 select * into m from control.nexloop_remediation_rules q where q.tenant_id=t and q.world=p_world order by q.manifest_version desc limit 1;
 for f in select ff.* from runtime.nexloop_effect_failures ff where ff.tenant_id=t and ff.world=p_world
   and not exists(select 1 from runtime.nexloop_remediation_scans s where s.failure_id=ff.failure_id)
   order by ff.recorded_at,ff.failure_id limit v_limit for update of ff skip locked loop
  v_decision:=null;v_rules:=0;
  if v_closed is not null then v_decision:='switch_closed';
  elsif p_world<>'real' then v_decision:='not_real';
  elsif f.reason_code not in ('origin_expired','confirmed_not_delivered') then v_decision:='not_eligible';
  else
   select * into i from runtime.nexloop_effect_intents q where q.intent_id=f.intent_id and q.tenant_id=t and q.world=p_world for share;
   if not found or i.state<>'failed' then v_decision:='not_failed';end if;
  end if;
  -- R2 (design §9.1): an intent submitted by a remediation Run (its Run consumed a wake) only escalates, whatever the rule says.
  if v_decision is null then v_origin:=runtime.nexloop_remediation_origin(t,p_world,i.intent_id);end if;
  if v_decision is null then
   for r in select value from jsonb_array_elements(coalesce(m.manifest->'rules','[]'::jsonb)) loop
    continue when r->'source'->>'stable_name' is distinct from i.action_name or (r->'source'->>'version')::integer<>i.action_version
     or not (r->'failure_kinds' ? f.reason_code);
    -- R2 (design §9.3): a held rule generates nothing until the owner releases it.
    if runtime.nexloop_remediation_rule_held(t,p_world,r->>'rule_id') then v_decision:=coalesce(v_decision,'rule_held');continue;end if;
    if r->'exclude_bound_replies'='true'::jsonb and exists(select 1 from runtime.nexloop_outbound_messages o where o.tenant_id=t and o.world=p_world
      and o.intent_id=i.intent_id and o.trigger_message_id is not null) then
     v_decision:=coalesce(v_decision,'bound_reply');continue;end if;
    -- The tenant rate limit (S-2): past it, one aggregated escalation per hour carries the count; nothing per failure.
    select count(*) into v_recent from runtime.nexloop_remediation_intents q where q.tenant_id=t and q.world=p_world
     and q.created_at>clock_timestamp()-interval '1 hour';
    if v_recent>=(m.manifest->'limits'->>'tenant_per_hour')::integer then
     v_hour:=date_trunc('hour',clock_timestamp());
     insert into runtime.nexloop_remediation_rate_caps as rc(tenant_id,world,hour,over_count) values(t,p_world,v_hour,1)
      on conflict(tenant_id,world,hour) do update set over_count=rc.over_count+1,updated_at=clock_timestamp();
     v_decision:='rate_capped';exit;
    end if;
    -- The Consumer chain cap (design §9.2): past it the remediation is an escalation only, coded chain_capped.
    select count(*) into v_consumer_recent from runtime.nexloop_remediation_intents q join runtime.nexloop_effect_intents e
      on e.intent_id=q.source_intent_id and e.tenant_id=q.tenant_id and e.world=q.world
     where q.tenant_id=t and q.world=p_world and e.consumer_id=i.consumer_id and q.created_at>clock_timestamp()-interval '24 hours';
    v_code:=case when v_origin is not null then 'remediation_origin' when v_consumer_recent>=(m.manifest->'limits'->>'consumer_per_day')::integer then 'chain_capped' else 'proposed' end;
    v_now:=clock_timestamp();  -- one reading: created_at, due_by and the proposed event are the same instant
    insert into runtime.nexloop_remediation_intents(tenant_id,world,source_intent_id,source_action,source_action_version,failure_kind,failure_id,
      rule_id,rule_version,rule_digest,kind,mode,state,created_at,updated_at,due_by,last_code,origin_remediation_id)
     values(t,p_world,i.intent_id,i.action_name,i.action_version,f.reason_code,f.failure_id,r->>'rule_id',m.manifest_version,m.digest,
      case when v_code in ('chain_capped','remediation_origin') then 'escalate' else r->>'kind' end,r->>'mode','proposed',
      v_now,v_now,v_now+make_interval(secs=>(r->>'due_after_seconds')::integer),v_code,v_origin)
     on conflict(tenant_id,world,source_intent_id,rule_id) do nothing returning remediation_id into v_id;
    if v_id is not null then
     insert into runtime.nexloop_remediation_events(remediation_id,tenant_id,world,from_state,to_state,code,actor_kind,actor,recorded_at)
      values(v_id,t,p_world,null,'proposed',v_code,'service',v_actor,v_now);
     -- An automatic escalation goes to the owner at once; a manual one waits for the owner's resolution. An automatic reevaluate
     -- wakes the waiting plan now (R2); a capped or remediation-origin one is an escalation only.
     if v_code in ('chain_capped','remediation_origin') or (r->>'mode'='automatic' and r->>'kind'='escalate') then
      perform runtime.nexloop_remediation_move(v_id,'escalated',case when v_code='proposed' then 'escalated_automatic' else v_code end,
       'service',v_actor,null);
     elsif r->>'mode'='automatic' and r->>'kind'='reevaluate' then
      perform runtime.nexloop_remediation_wake(t,p_world,v_id,'service',v_actor,null);
     end if;
     v_rules:=v_rules+1;
    end if;
   end loop;
   v_decision:=case when v_rules>0 then 'generated' else coalesce(v_decision,'no_rule') end;
  end if;
  insert into runtime.nexloop_remediation_scans(failure_id,tenant_id,world,decision,rules) values(f.failure_id,t,p_world,v_decision,v_rules);
  counts:=counts||jsonb_build_object(v_decision,coalesce((counts->>v_decision)::integer,0)+1);
 end loop;
 -- R2 (design §9.3): the breaker holds a rule past its limit in 24 hours; the owner releases it.
 v_held:=runtime.nexloop_remediation_breaker(t,p_world);
 return jsonb_build_object('status',coalesce('closed:'||v_closed,'open'),'expired',v_expired,'decisions',counts,'held',to_jsonb(v_held));
end $$;


do $$ declare f text; begin
 foreach f in array array['control.nexloop_governed_invite_workbench_member(text,text,text,text,text,jsonb)','control.nexloop_governed_issue_workbench_owner_reset(text,text,text,text,text,jsonb)','control.nexloop_governed_invite_workbench_owner(text,text,text,text,text,jsonb)','control.nexloop_governed_issue_workbench_reset(text,text,text,text,text,jsonb)','control.nexloop_workbench_oidc_begin(text,text,text,text)','authz.nexloop_remediation_plan_command(text,text,text,text,text)'] loop
  execute format('alter function %s owner to nexloop_owner',f);
  execute format('revoke all on function %s from public',f);
 end loop;
end $$;
