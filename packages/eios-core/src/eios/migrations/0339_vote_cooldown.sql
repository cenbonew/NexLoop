-- NX-058 slice D §2 (docs/design/nx058-owner-actions.md v2.5; ADR-039 R1 §6.7, account-self-service design §7 X2/Z4, AA6): the
-- 24-hour vote cooldown for every NX-056 policy. The migration number is temporary; it lives only in the file name and the catalog.
--
-- * control.nexloop_workbench_vote_eligible_from(tenant, world, subject, roles): from control.nexloop_workbench_role_history (0335), the
--   start of the subject's current continuous holding of any of the roles (over all its principals, in the order of recorded_at and
--   entry_id, one transaction's rows as one step: recorded_txid) plus 24 hours; null when it holds none of them now. A change within the union keeps the clock (operator -> owner when
--   both approve); stopping all of them (a suspension included) and starting again restarts it. world: the workbench's (real) only.
-- * control.nexloop_approval_approver_eligible (0326) also requires that time to have passed. The decision (decide refuses the vote) and
--   the consumption (valid_votes does not count it) both go through it, so a vote of an approver demoted or suspended after deciding,
--   or of one who joined less than 24 hours ago, never counts. Registered in tests/test_function_copy_guard.py.
-- * Deployment (design §2, Y6): a configured active member without an open holding of its role gets a start now, so every policy waits
--   24 hours after this migration (the runbook says so); members configured later start when the configurator or a governed change
--   records them.
set local timezone = 'UTC';

-- The transaction that wrote a history row: a handler's stop of one role and start of another (operator -> owner) are one change, so
-- the union is judged after each transaction's rows, never between them. Rows before this migration have none (each its own step).
-- The default is set after the column is added, so existing rows stay null instead of taking this migration's transaction.
alter table control.nexloop_workbench_role_history add column recorded_txid bigint;
alter table control.nexloop_workbench_role_history alter column recorded_txid set default txid_current();

create function control.nexloop_workbench_vote_eligible_from(p_tenant text,p_world text,p_subject text,p_roles text[]) returns timestamptz
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare h record;open_pairs text[]:='{}';since timestamptz;pair text;step_open boolean:=false;step_start timestamptz;step_tx bigint;first boolean:=true;
 prior text:=current_setting('eios.tenant_id',true);
begin
 if p_world is distinct from 'real' or p_subject is null or p_roles is null then return null;end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 for h in select r.principal_id,r.role,r.event,r.recorded_at,r.recorded_txid from control.nexloop_workbench_role_history r
   where r.tenant_id=p_tenant and r.world=p_world and r.subject_id=p_subject and r.role=any(p_roles) order by r.recorded_at,r.entry_id loop
  -- A new step (another transaction, or a row without one): settle the previous step first.
  if not first and (h.recorded_txid is null or h.recorded_txid is distinct from step_tx) then
   if cardinality(open_pairs)=0 then since:=null;elsif not step_open then since:=step_start;end if;
   first:=true;
  end if;
  if first then step_open:=cardinality(open_pairs)>0;step_start:=h.recorded_at;step_tx:=h.recorded_txid;first:=false;end if;
  pair:=h.principal_id||chr(31)||h.role;
  if h.event='start' then
   if not pair=any(open_pairs) then open_pairs:=open_pairs||pair;end if;
  else
   open_pairs:=array_remove(open_pairs,pair);
  end if;
 end loop;
 if not first then
  if cardinality(open_pairs)=0 then since:=null;elsif not step_open then since:=step_start;end if;
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return since+interval '24 hours';
end $$;
alter function control.nexloop_workbench_vote_eligible_from(text,text,text,text[]) owner to nexloop_owner;
revoke all on function control.nexloop_workbench_vote_eligible_from(text,text,text,text[]) from public;

create or replace function control.nexloop_approval_approver_eligible(p_tenant text,p_principal text,p_roles text[]) returns boolean
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare cfg control.nexloop_workbench_configurations;prior text:=current_setting('eios.tenant_id',true);v boolean;
begin
 cfg:=control.nexloop_workbench_current(p_tenant);
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 v:=cfg.tenant_id is not null and exists(select 1 from control.nexloop_browser_memberships m join control.nexloop_browser_subjects b on b.subject_id=m.subject_id
   where m.tenant_id=p_tenant and m.principal_id=p_principal and m.payload->>'status'='active'
    and b.payload->>'kind'='human' and b.payload->>'status'='active') and coalesce(cfg.members->>p_principal,'')=any(p_roles)
  and coalesce(control.nexloop_workbench_vote_eligible_from(p_tenant,'real',control.nexloop_approval_subject_of(p_tenant,p_principal),p_roles)<=clock_timestamp(),false);
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;

do $$ declare t text;m record; begin
 for t in select tenant_id from control.nexloop_tenants order by tenant_id loop
  perform set_config('eios.tenant_id',t,true);
  for m in select c.key principal_id,c.value role,bm.subject_id
    from jsonb_each_text(coalesce((select w.members from control.nexloop_workbench_configurations w where w.tenant_id=t order by w.manifest_version desc limit 1),'{}')) c
    join control.nexloop_browser_memberships bm on bm.tenant_id=t and bm.principal_id=c.key and bm.payload->>'status'='active' loop
   if coalesce((select r.event from control.nexloop_workbench_role_history r where r.tenant_id=t and r.principal_id=m.principal_id and r.role=m.role
       order by r.entry_id desc limit 1),'stop')<>'start' then
    insert into control.nexloop_workbench_role_history(tenant_id,subject_id,principal_id,role,event,source) values(t,m.subject_id,m.principal_id,m.role,'start','deployment');
   end if;
  end loop;
 end loop;
 perform set_config('eios.tenant_id','',true);
end $$;
