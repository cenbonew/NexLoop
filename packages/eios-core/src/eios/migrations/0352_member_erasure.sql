-- NX-058 slice E1 (docs/design/nx058-member-erasure.md v2.4; ADR-039 R3 §8.1, §8.6, §8.7; owner Q-A "A+90天", E1 = A): staff
-- offboarding erasure under NX-056 approval. The migration number is temporary; it lives only in the file name and the catalog. The 90-day
-- clearing of login digests (retention service) follows in slice E2.
--
-- * control.nexloop_workbench_member_erasures: the staff erasure ledger - one row per erased subject of a tenant, permanent (never in a
--   retention sweep), append-only, owner only, forced RLS; ids and times only. erased, already_erased, the configurator's member_erased and
--   the approval view read it (design §2.3, I-E2).
-- * control.nexloop_deletion_ledger: kind workbench_member, its cutoff the erasure time (written by the ledger's insert trigger, as 0135
--   does for Consumers); runtime.nexloop_replay_deletions (0260) replays it idempotently with its original time and executor (C2, I-E4).
-- * control.nexloop_workbench_member_erase(tenant, subject, approval, actor, at): the one write, shared by the handler and the replay -
--   the account disabled, its password and history cleared, username and email an erased- pseudonym (column and payload); the subject
--   disabled; its sessions deleted; the invitations it accepted and the reset codes aimed at it deleted, the issuer source digests of what
--   it issued cleared; the frozen invite_owner bodies of its own invitation rewritten (finished requests only, ADR-039 R3 §8.7); the ledger.
-- * control.nexloop_governed_erase_workbench_member: capability workbench_member.erase_member, target statuses {revoked} (registered here
--   with the capability, R3 §8.3); fail closed like slice D; the subject's every membership revoked (C1, rechecked under the lock).
-- * The approval view's target_member (0342): erased by the ledger, and an invitee username with the erased- prefix (R3 §8.6). The request
--   guard (0326): the invite_owner username rewrite (R3 §8.7). Copy guard for the three.
-- * erased- is reserved: an invitation of such a username is refused (any path), and this migration refuses to run while one exists.
--   The configurator never configures a principal of an erased subject (member_erased).
set local timezone = 'UTC';

do $$ declare t text;n integer; begin
 -- The erased- prefix must be free before it means "erased" (design §2.4, S-E1): accounts, invitations and invite_owner bodies.
 for t in select tenant_id from control.nexloop_tenants loop
  perform set_config('eios.tenant_id',t,true);
  select (select count(*) from control.nexloop_browser_accounts where tenant_id=t and username like 'erased-%')
   +(select count(*) from control.nexloop_workbench_invitations where tenant_id=t and username like 'erased-%')
   +(select count(*) from control.nexloop_approval_requests where tenant_id=t and capability='workbench_member.invite_owner' and body->>'username' like 'erased-%') into n;
  if n>0 then raise exception 'NX-058 slice E: % username(s) of tenant % already start with erased-; rename them first',n,t;end if;
 end loop;
 perform set_config('eios.tenant_id','',true);
end $$;

create table control.nexloop_workbench_member_erasures (
 tenant_id text not null references control.nexloop_tenants(tenant_id) on delete restrict,subject_id text not null,
 principals text[] not null check(cardinality(principals) between 1 and 64),approval_id uuid,erased_by_principal text,
 erased_at timestamptz not null,world text not null default 'real' check(world='real'),
 primary key(tenant_id,subject_id)
);
alter table control.nexloop_workbench_member_erasures owner to nexloop_owner;
alter table control.nexloop_workbench_member_erasures enable row level security;
alter table control.nexloop_workbench_member_erasures force row level security;
create policy tenant_boundary on control.nexloop_workbench_member_erasures to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_workbench_member_erasures from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;
create trigger nx058_append_only before update or delete on control.nexloop_workbench_member_erasures for each row execute function control.nexloop_nx022_append_only();

-- The restore journal (C2): the deletion ledger learns the kind; its cutoff carries the erasure time (I-E4).
alter table control.nexloop_deletion_ledger drop constraint nexloop_deletion_ledger_kind_check;
alter table control.nexloop_deletion_ledger add constraint nexloop_deletion_ledger_kind_check check(kind in ('consumer','message','item_class','sim_world','workbench_member'));
do $$ declare c text; begin
 select conname into c from pg_constraint where conrelid='control.nexloop_deletion_ledger'::regclass and contype='c'
  and pg_get_constraintdef(oid) like '%item_class%cutoff%';
 execute format('alter table control.nexloop_deletion_ledger drop constraint %I',c);
end $$;
alter table control.nexloop_deletion_ledger add constraint nexloop_deletion_ledger_cutoff_check
 check(case when kind in ('item_class','workbench_member') then cutoff is not null else cutoff is null end);
-- L6 review of E1 (1): the executor travels with the entry - a backup older than the erasure has no consumption to look it up in.
alter table control.nexloop_deletion_ledger add column actor text check(actor is null or char_length(actor) between 1 and 320);
create or replace function control.nexloop_journal_on_deletion() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set timezone='UTC' as $$
begin
 perform control.nexloop_restore_journal_append('deletion',jsonb_build_object('subject','deletion_ledger','seq',new.seq,'tenant_id',new.tenant_id,
  'world',new.world,'kind',new.kind,'subject_ref',new.subject_ref,'request_ref',new.request_ref,'cutoff',new.cutoff,'actor',new.actor));
 return null;
end $$;

create function control.nexloop_workbench_member_erasure_ledger() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
begin
 insert into control.nexloop_deletion_ledger(seq,tenant_id,world,kind,subject_ref,request_ref,cutoff,actor)
  values(nextval('control.nexloop_deletion_watermark'),new.tenant_id,'real','workbench_member',new.subject_id,coalesce(new.approval_id::text,'replay'),
   new.erased_at,new.erased_by_principal) on conflict do nothing;
 return null;
end $$;
create trigger nx058_erasure_ledger after insert on control.nexloop_workbench_member_erasures
 for each row execute function control.nexloop_workbench_member_erasure_ledger();

-- The replay after a restore older than the revocation (L6 review of E1, 2): every membership of the subject revoked, its principals out of
-- the workbench configuration, their role grants revoked and their holdings stopped - before the erasure.
create function control.nexloop_workbench_member_revoke_for_replay(p_tenant text,p_subject text,p_source text) returns integer
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare m record;cur control.nexloop_workbench_configurations;n integer:=0;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 perform control.nexloop_workbench_member_lock(p_tenant);
 select * into cur from control.nexloop_workbench_configurations where tenant_id=p_tenant order by manifest_version desc limit 1;
 for m in select principal_id,payload->>'status' status from control.nexloop_browser_memberships where tenant_id=p_tenant and subject_id=p_subject order by principal_id loop
  if cur.tenant_id is not null and cur.members ? m.principal_id then
   if m.status='active' then
    perform control.nexloop_workbench_grants_write_v2(p_tenant,m.principal_id,p_subject,cur.members->>m.principal_id,'revoke',p_source,'system:replay',p_source);
    insert into control.nexloop_workbench_role_history(tenant_id,subject_id,principal_id,role,event,source) values(p_tenant,p_subject,m.principal_id,cur.members->>m.principal_id,'stop',p_source);
   end if;
   perform control.nexloop_workbench_configuration_append(p_tenant,cur.members-m.principal_id,p_source);
   select * into cur from control.nexloop_workbench_configurations where tenant_id=p_tenant order by manifest_version desc limit 1;
  end if;
  if m.status<>'revoked' then perform control.nexloop_workbench_membership_status(p_tenant,m.principal_id,'revoked');n:=n+1;end if;
 end loop;
 return n;
end $$;

-- The one write (handler and replay). Idempotent: every step is a no-op the second time.
create function control.nexloop_workbench_member_erase(p_tenant text,p_subject text,p_approval uuid,p_actor text,p_at timestamptz) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare principals text[];a control.nexloop_browser_accounts;original text;pseudonym text;rewrite uuid[];
 n_sessions integer:=0;n_invitations integer:=0;n_codes integer:=0;n_bodies integer:=0;n integer;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 perform control.nexloop_workbench_member_lock(p_tenant);
 select array_agg(principal_id order by principal_id) into principals from control.nexloop_browser_memberships where tenant_id=p_tenant and subject_id=p_subject;
 if principals is null then raise exception 'workbench member: target_not_revoked' using errcode='22023';end if;
 select * into a from control.nexloop_browser_accounts where tenant_id=p_tenant and subject_id=p_subject for update;
 if found then
  original:=a.username;
  pseudonym:='erased-'||left(encode(sha256(convert_to(p_tenant||'|'||a.local_account_id,'UTF8')),'hex'),16);
  update control.nexloop_browser_accounts set username=pseudonym,password_hash=null,password_history='{}',
   payload=payload||jsonb_build_object('username',pseudonym,'verified_email',pseudonym||'@erased.invalid','status','disabled',
    'session_epoch',(payload->>'session_epoch')::bigint+case when payload->>'status'='disabled' and password_hash is null then 0 else 1 end,
    'revision',(payload->>'revision')::bigint+case when username=pseudonym then 0 else 1 end)
   where tenant_id=p_tenant and local_account_id=a.local_account_id and (username<>pseudonym or password_hash is not null or payload->>'status'<>'disabled');
 end if;
 update control.nexloop_browser_subjects set payload=payload||jsonb_build_object('status','disabled','revision',(payload->>'revision')::bigint+1)
  where subject_id=p_subject and payload->>'status'<>'disabled';
 delete from authz.nexloop_browser_token_realms t using control.nexloop_browser_sessions s
  where s.tenant_id=p_tenant and s.payload->>'subject_id'=p_subject and t.session_id=s.session_id;
 delete from control.nexloop_browser_sessions where tenant_id=p_tenant and payload->>'subject_id'=p_subject;
 get diagnostics n_sessions=row_count;
 -- The initial-account receipt (0050) keeps its ids; its password fingerprint (a value derived from the password) is overwritten. A replay
 -- of the initial manifest used to return the old ids silently; for an erased subject it is now refused outright
 -- (control.nexloop_initial_identity_create below), never answered with the account.
 update control.nexloop_initial_identity_receipts set password_fingerprint=encode(sha256(convert_to('nexloop-erased|'||p_subject,'UTF8')),'hex')
  where tenant_id=p_tenant and subject_id=p_subject;
 -- The finished invite_owner requests of the invitation this subject accepted (found through the approval table and the original username;
 -- a waiting or approved-unconsumed request may be a new namesake's and stays, S-E7).
 select array_agg(distinct r.approval_id) into rewrite from control.nexloop_approval_requests r
  where r.tenant_id=p_tenant and r.capability='workbench_member.invite_owner'
   and (r.approval_id in (select i.approval_id from control.nexloop_workbench_invitations i where i.tenant_id=p_tenant
         and (i.accepting_subject=p_subject or i.accepted_principal=any(principals)) and i.approval_id is not null)
        or (original is not null and r.body->>'username'=original))
   and (r.status not in ('waiting_approval','approved')
        or exists(select 1 from control.nexloop_approval_consumptions c where c.tenant_id=r.tenant_id and c.world=r.world and c.approval_id=r.approval_id));
 if rewrite is not null and pseudonym is not null then
  perform runtime.nexloop_erasure_open('control.nexloop_approval_requests',array['body'],false,'workbench-erasure:'||p_subject);
  update control.nexloop_approval_requests set body=jsonb_set(body,'{username}',to_jsonb(pseudonym))
   where tenant_id=p_tenant and approval_id=any(rewrite) and body->>'username' is distinct from pseudonym;
  get diagnostics n_bodies=row_count;
  perform runtime.nexloop_erasure_close();
 end if;
 delete from control.nexloop_workbench_invitations where tenant_id=p_tenant and (accepting_subject=p_subject or accepted_principal=any(principals));
 get diagnostics n_invitations=row_count;
 -- by principal: issued_by_subject holds what the governed entry passed as p_subject (the subject kind), not the subject id
 update control.nexloop_workbench_invitations set issuer_source=null where tenant_id=p_tenant and issued_by_principal=any(principals) and issuer_source is not null;
 delete from control.nexloop_workbench_reset_codes where tenant_id=p_tenant and target_subject=p_subject;
 get diagnostics n_codes=row_count;
 update control.nexloop_workbench_reset_codes set issuer_source=null where tenant_id=p_tenant and issued_by_principal=any(principals) and issuer_source is not null;
 perform set_config('eios.tenant_id',p_tenant,true);
 insert into control.nexloop_workbench_member_erasures(tenant_id,subject_id,principals,approval_id,erased_by_principal,erased_at)
  values(p_tenant,p_subject,principals,p_approval,
   coalesce(p_actor,(select c.consumed_by_principal from control.nexloop_approval_consumptions c where c.tenant_id=p_tenant and c.approval_id=p_approval)),p_at)
  on conflict(tenant_id,subject_id) do nothing;
 return jsonb_build_object('principals',to_jsonb(principals),'sessions',n_sessions,'invitations',n_invitations,'reset_codes',n_codes,'frozen_bodies',n_bodies);
end $$;

create function control.nexloop_governed_erase_workbench_member(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_target text;v_approval uuid;v_counts jsonb;
begin
 if jsonb_typeof(body) is distinct from 'object'
  or (select array_agg(k order by k) from jsonb_object_keys(body) k) is distinct from array['operation','reason','request_id','target_principal']
  or body->>'operation' is distinct from 'erase_workbench_member'
  or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200
  or not control.nexloop_workbench_member_id_valid(body->>'target_principal')
  or coalesce(body->>'reason','') not in ('offboarding','member_request','other') then
  raise exception 'workbench member: invalid' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'workbench member: owner_world_only' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform control.nexloop_workbench_member_lock(p_tenant);
 -- A replay of this handler's own execution in the same request (the entry replays a finished claim before it gets here).
 if exists(select 1 from runtime.audit_events where event_id='workbench-erase:'||p_intent) then
  return jsonb_build_object('target_principal',body->>'target_principal','replayed',true);
 end if;
 v_approval:=control.nexloop_workbench_owner_approval_v2(p_tenant,p_world,'workbench_member.erase_member',p_principal,body,'target_principal');
 if not control.nexloop_workbench_member_active(p_tenant,p_principal,true) then raise exception 'workbench member: issuer_not_owner' using errcode='22023';end if;
 -- The target (S-E2): its subject, every membership of it locked and revoked (C1, I1); never the requester; never erased twice.
 select subject_id into v_target from control.nexloop_browser_memberships where tenant_id=p_tenant and principal_id=body->>'target_principal';
 if v_target is null then raise exception 'workbench member: target_not_revoked' using errcode='22023';end if;
 if v_target=control.nexloop_approval_subject_of(p_tenant,p_principal) then raise exception 'workbench member: target_is_requester' using errcode='22023';end if;
 perform 1 from control.nexloop_browser_memberships where tenant_id=p_tenant and subject_id=v_target for update;
 if exists(select 1 from control.nexloop_browser_memberships where tenant_id=p_tenant and subject_id=v_target and payload->>'status'<>'revoked') then
  raise exception 'workbench member: target_not_revoked' using errcode='22023';end if;
 if exists(select 1 from control.nexloop_workbench_member_erasures where tenant_id=p_tenant and subject_id=v_target) then
  raise exception 'workbench member: already_erased' using errcode='22023';end if;
 perform control.nexloop_authority_handler_mark(p_tenant);  -- before the first authority write (0341)
 v_counts:=control.nexloop_workbench_member_erase(p_tenant,v_target,v_approval,p_principal,clock_timestamp());
 perform set_config('eios.tenant_id',p_tenant,true);
 -- The audit names principals and counts only: no reason, username or digest (design §6, S1).
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('workbench-erase:'||p_intent,'nexloop.workbench_member.erase_member',p_tenant,p_principal,'workbench_member',p_intent,
   v_counts||jsonb_build_object('target_principal',body->>'target_principal','approval_id',v_approval),clock_timestamp());
 return jsonb_build_object('target_principal',body->>'target_principal','erased',true,'replayed',false);
end $$;

-- erased- is the system's (S-E1, I5): no invitation of such a username, whatever path writes it.
create function control.nexloop_workbench_invitation_username_guard() returns trigger
 language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if new.username like 'erased-%' then raise exception 'workbench member: username_reserved' using errcode='22023';end if;
 return new;
end $$;
create trigger nx058_username_reserved before insert on control.nexloop_workbench_invitations
 for each row execute function control.nexloop_workbench_invitation_username_guard();
-- ... nor any new account (trusted configuration's initial accounts, 0050/0162; acceptance): only the erasure's update writes one.
create trigger nx058_username_reserved before insert on control.nexloop_browser_accounts
 for each row execute function control.nexloop_workbench_invitation_username_guard();

-- The configurator never configures an erased member again (design §6).
create function control.nexloop_workbench_configuration_erased_guard() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);
begin
 perform set_config('eios.tenant_id',new.tenant_id,true);
 if exists(select 1 from jsonb_object_keys(new.members) k join control.nexloop_browser_memberships m on m.tenant_id=new.tenant_id and m.principal_id=k
   join control.nexloop_workbench_member_erasures e on e.tenant_id=m.tenant_id and e.subject_id=m.subject_id) then
  raise exception 'workbench members: member_erased' using errcode='22023';end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return new;
end $$;
create trigger nx058_member_erased before insert on control.nexloop_workbench_configurations
 for each row execute function control.nexloop_workbench_configuration_erased_guard();

create or replace function runtime.nexloop_replay_deletions(p_restore text,p_entries jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare x jsonb;n_consumer integer:=0;n_message integer:=0;n_class integer:=0;n_skipped integer:=0;n_world integer:=0;r jsonb;n_member integer:=0;
begin
 if not exists(select 1 from control.nexloop_deletion_replays where restore_ref=p_restore and state='replaying') then
  raise exception 'deletion replay not begun' using errcode='42501';end if;
 if jsonb_typeof(p_entries) is distinct from 'array' or jsonb_array_length(p_entries)>1000 then raise exception 'deletion replay invalid' using errcode='22023';end if;
 for x in select value from jsonb_array_elements(p_entries) loop
  if jsonb_typeof(x) is distinct from 'object' or x->>'kind' not in ('consumer','message','item_class','sim_world','workbench_member') or coalesce(x->>'tenant_id','')=''
   or coalesce(x->>'world','')='' or coalesce(x->>'subject_ref','')='' or coalesce(x->>'request_ref','')!~'^[A-Za-z0-9._:-]{1,200}$' then
   raise exception 'deletion replay entry invalid' using errcode='22023';end if;
  perform set_config('eios.tenant_id',x->>'tenant_id',true);
  if x->>'kind'='consumer' then
   if coalesce(x->>'subject_ref','')!~'^[a-f0-9]{64}$' then raise exception 'deletion replay entry invalid' using errcode='22023';end if;
   if exists(select 1 from ontology.objects where tenant_id=x->>'tenant_id' and world=x->>'world' and type_name='Consumer' and object_id=x->>'subject_ref')
    and not runtime.nexloop_consumer_erasing(x->>'tenant_id',x->>'world',x->>'subject_ref') then
    -- The same governed handler as the owner's Action: blocking at once (contact, plans), then the keeper purges.
    r:=control.nexloop_governed_erase_consumer(x->>'tenant_id',x->>'world','restore:'||p_restore,'service',x->>'request_ref',
     jsonb_build_object('operation','erase_consumer','consumer_id',x->>'subject_ref','reason','deletion replayed after restore '||p_restore));
    n_consumer:=n_consumer+1;
   else n_skipped:=n_skipped+1;end if;
  elsif x->>'kind'='message' then
   if exists(select 1 from runtime.nexloop_conversation_messages where tenant_id=x->>'tenant_id' and world=x->>'world' and message_id=x->>'subject_ref' and record->>'body'<>'')
    or exists(select 1 from ontology.objects where tenant_id=x->>'tenant_id' and world=x->>'world' and type_name='Message' and object_id=x->>'subject_ref') then
    perform runtime.nexloop_erase_message(x->>'tenant_id',x->>'world',x->>'subject_ref',x->>'request_ref','message_erasure','restore:'||p_restore);
    n_message:=n_message+1;
   else n_skipped:=n_skipped+1;end if;
  elsif x->>'kind'='workbench_member' then
   -- NX-058 slice E (design §2.3): a staff erasure after the backup is erased again, idempotently, with its original time and executor;
   -- the erasure ledger row comes back with it. A subject unknown to the restored directory is already applied.
   if exists(select 1 from control.nexloop_browser_memberships m where m.tenant_id=x->>'tenant_id' and m.subject_id=x->>'subject_ref') then
    perform control.nexloop_workbench_member_revoke_for_replay(x->>'tenant_id',x->>'subject_ref','action:erasure-replay:'||p_restore);
    perform control.nexloop_workbench_member_erase(x->>'tenant_id',x->>'subject_ref',
     case when x->>'request_ref'~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' then (x->>'request_ref')::uuid end,
     nullif(x->>'actor',''),(x->>'cutoff')::timestamptz);
    n_member:=n_member+1;
   else n_skipped:=n_skipped+1;end if;
  elsif x->>'kind'='sim_world' then
   -- NX-041 (review 1.8): a world deleted after the backup is deleting again; its credentials are revoked at once and the
   -- retention keeper finishes the deletion (summary first, as for the owner's Action). A world already deleting or deleted, or
   -- not in the restored registry, is already applied.
   if runtime.nexloop_world_kind(x->>'subject_ref') not in ('simulation','shadow') then raise exception 'deletion replay entry invalid' using errcode='22023';end if;
   update authz.nexloop_service_credentials set status='revoked' where worlds=array[x->>'subject_ref'] and status='active';
   update authz.nexloop_run_credentials set status='revoked' where world=x->>'subject_ref' and status='active';
   update control.nexloop_sim_worlds set status='deleting' where world=x->>'subject_ref' and tenant_id=x->>'tenant_id' and status='active';
   if found then n_world:=n_world+1;else n_skipped:=n_skipped+1;end if;
  else
   -- The class is due at once; its sweep recomputes the cutoff from now, which covers the journal's cutoff.
   update runtime.nexloop_retention_sweeps set last_completed_at=null where tenant_id=x->>'tenant_id' and world=x->>'world' and item_class=x->>'subject_ref';
   n_class:=n_class+1;
  end if;
 end loop;
 update control.nexloop_deletion_replays set entries=entries+jsonb_array_length(p_entries) where restore_ref=p_restore;
 return jsonb_build_object('restore_ref',p_restore,'consumers',n_consumer,'messages',n_message,'classes',n_class,'sim_worlds',n_world,'workbench_members',n_member,'already_applied',n_skipped);
end $$;

create or replace function runtime.nexloop_approval_target_member(p_tenant text,r control.nexloop_approval_requests,pol control.nexloop_approval_policies,p_caller text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);cfg control.nexloop_workbench_configurations;v_value text;
 m control.nexloop_browser_memberships;a control.nexloop_browser_accounts;v jsonb;
begin
 -- 1. No target.
 -- The capability decides, never the body's operation (the requester writes the body; L6 review of fdc06ef2).
 if pol.target_subject_field is null and r.capability is distinct from 'workbench_member.invite_owner' then return null;end if;
 -- 2. Not an owner now: hidden, nothing resolved (a non-owner cannot tell a target that exists from one that does not).
 cfg:=control.nexloop_workbench_current(p_tenant);
 if cfg.tenant_id is null or coalesce(cfg.members->>p_caller,'')<>'owner' then return jsonb_build_object('kind','hidden');end if;
 -- 3. invite_owner (D7, owner 2026-10-25: A): the invitee's username as the body gives it, and whether an account of this tenant already
 -- has exactly that username (the HTTP layer refuses such an invitation, so true means a request that bypassed it; §6.4).
 if r.capability='workbench_member.invite_owner' then
  begin  -- fail closed: any error is unresolved, never a failed view
   -- NX-058 slice E (ADR-039 R3 §8.6): an invitee username the staff erasure rewrote is erased.
   if jsonb_typeof(r.body->'username')='string' and r.body->>'username' like 'erased-%' then
    v:=jsonb_build_object('kind','erased');
   elsif jsonb_typeof(r.body->'username')='string' then
    perform set_config('eios.tenant_id',p_tenant,true);
    v:=jsonb_build_object('kind','invitee','username',r.body->>'username',
     'same_username_exists',exists(select 1 from control.nexloop_browser_accounts x where x.tenant_id=p_tenant and x.username=r.body->>'username'));
   end if;
  exception when others then
   v:=null;
  end;
  perform set_config('eios.tenant_id',coalesce(prior,''),true);
  return coalesce(v,jsonb_build_object('kind','unresolved'));
 end if;
 begin
  v_value:=r.body->>pol.target_subject_field;
  perform set_config('eios.tenant_id',p_tenant,true);
  if v_value is not null then
   -- The body's own principal; a subject id: its first membership by status, then principal.
   select * into m from control.nexloop_browser_memberships where tenant_id=p_tenant and principal_id=v_value;
   if m.principal_id is null then
    select * into m from control.nexloop_browser_memberships where tenant_id=p_tenant and subject_id=v_value
     order by array_position(array['active','invited','suspended','revoked'],payload->>'status'),principal_id limit 1;
   end if;
  end if;
  if m.principal_id is not null then
   select * into a from control.nexloop_browser_accounts where tenant_id=p_tenant and subject_id=m.subject_id
    order by array_position(array['active','invited','disabled'],payload->>'status'),local_account_id limit 1;
   -- 4. Erased (ADR-039 R3 §8.6, NX-058 slice E): the member's subject is in the staff erasure ledger.
   if exists(select 1 from control.nexloop_workbench_member_erasures e where e.tenant_id=p_tenant and e.subject_id=m.subject_id) then
    v:=jsonb_build_object('kind','erased');
   else
    -- 6. A member.
    v:=jsonb_build_object('kind','member','label','p-'||left(encode(sha256(convert_to(m.principal_id,'UTF8')),'hex'),8),
     'username',a.username,'account_status',a.payload->>'status','member_status',m.payload->>'status','role',cfg.members->>m.principal_id);
   end if;
  end if;
 exception when others then
  v:=null;
 end;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 -- 5. Not resolved (and any error): never the body's value.
 return coalesce(v,jsonb_build_object('kind','unresolved'));
end $$;

create or replace function control.nexloop_approval_request_guard() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if tg_op='DELETE' then raise exception 'approval requests are deleted only by retention' using errcode='42501';end if;
 -- Erasure and retention (A5, 0331): only the body changes (pseudonymised or redacted), inside an erasure scope this transaction
 -- opened for this table and column; nothing else, not even the revision.
 if new.body is distinct from old.body and exists(select 1 from control.nexloop_erasure_scope s where s.txid=txid_current()
   and s.table_name='control.nexloop_approval_requests' and 'body'=any(s.columns)) then
  -- Only the owner's functions, and only two forms (L6 review 8): the redacted body, or the same body with whole string values replaced
  -- by pseudonyms.
  if current_user<>'nexloop_owner' or not (new.body=jsonb_build_object('operation',old.body->'operation','redacted',true)
    or control.nexloop_approval_pseudonymised(old.body,new.body)
    -- NX-058 slice E (ADR-039 R3 §8.7): the staff erasure rewrites invite_owner's invitee username, that one string, to an erased- pseudonym.
    or (old.capability='workbench_member.invite_owner' and jsonb_typeof(old.body->'username')='string'
        and new.body=jsonb_set(old.body,'{username}',new.body->'username') and new.body->>'username'~'^erased-[0-9a-f]{16}$'
        -- only a finished request, inside the staff erasure's own scope (L6 review of E1)
        and (old.status not in ('waiting_approval','approved') or exists(select 1 from control.nexloop_approval_consumptions c
          where c.tenant_id=old.tenant_id and c.world=old.world and c.approval_id=old.approval_id))
        and exists(select 1 from control.nexloop_erasure_scope s where s.txid=txid_current() and s.table_name='control.nexloop_approval_requests'
          and s.request_ref like 'workbench-erasure:%'))) then
   raise exception 'approval request is immutable' using errcode='42501';end if;
  if (new.tenant_id,new.world,new.approval_id,new.capability,new.action_resource,new.policy_version,new.request_digest,new.request_id,
      new.requester_subject,new.requester_principal,new.requested_at,new.due_by,new.status,new.revision)
    is distinct from (old.tenant_id,old.world,old.approval_id,old.capability,old.action_resource,old.policy_version,old.request_digest,old.request_id,
      old.requester_subject,old.requester_principal,old.requested_at,old.due_by,old.status,old.revision) then
   raise exception 'approval request is immutable' using errcode='42501';end if;
  return new;
 end if;
 -- Only the status moves, once, out of waiting_approval (or back to it when a vote no longer counts, A2; or approved -> expired when it
 -- passed due_by or its policy changed before it was consumed), with the revision.
 if (new.tenant_id,new.world,new.approval_id,new.capability,new.action_resource,new.policy_version,new.request_digest,new.request_id,new.body,
     new.requester_subject,new.requester_principal,new.requested_at,new.due_by)
   is distinct from (old.tenant_id,old.world,old.approval_id,old.capability,old.action_resource,old.policy_version,old.request_digest,old.request_id,old.body,
     old.requester_subject,old.requester_principal,old.requested_at,old.due_by)
  or new.revision<>old.revision+1
  or not (old.status='waiting_approval' or (old.status='approved' and new.status in ('waiting_approval','expired'))) then
  raise exception 'approval request is immutable' using errcode='42501';end if;
 -- A consumed Approval never changes status again (L6 review 6).
 if exists(select 1 from control.nexloop_approval_consumptions c where c.tenant_id=old.tenant_id and c.world=old.world and c.approval_id=old.approval_id) then
  raise exception 'approval request is consumed' using errcode='42501';end if;
 return new;
end $$;

-- The actual restore path (backup.py -> runtime.nexloop_restore_replay, 0154 -> this inner function, 0147's body): the deletion entry keeps
-- its actor (L6 review of E1, follow-up).
create or replace function runtime.nexloop_restore_replay_before_contact_keys_v0240(p_ref text,p_entries jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare s control.nexloop_restore_state;x jsonb;e jsonb;deletions jsonb:='[]'::jsonb;n_contact integer:=0;n_keys integer:=0;n_cred integer:=0;n_member integer:=0;
 r jsonb:='{}'::jsonb;v_max bigint;
begin
 s:=runtime.nexloop_restore_require(p_ref,'restored');
 if jsonb_typeof(p_entries) is distinct from 'array' then raise exception 'restore replay invalid' using errcode='22023';end if;
 for x in select value from jsonb_array_elements(p_entries) order by (value->>'seq')::bigint loop
  e:=x->'entry';
  if jsonb_typeof(e) is distinct from 'object' or coalesce(x->>'seq','')!~'^[1-9][0-9]{0,18}$' or (x->>'seq')::bigint<=s.journal_from then
   raise exception 'restore replay entry invalid' using errcode='22023';end if;
  v_max:=greatest(coalesce(v_max,0),(x->>'seq')::bigint);
  perform set_config('eios.tenant_id',e->>'tenant_id',true);
  if x->>'kind'='deletion' and e->>'subject'='deletion_ledger' then
   deletions:=deletions||jsonb_build_array(jsonb_build_object('seq',e->'seq','tenant_id',e->'tenant_id','world',e->'world','kind',e->'kind',
    'subject_ref',e->'subject_ref','request_ref',e->'request_ref','cutoff',e->'cutoff','actor',e->'actor'));  -- NX-058 slice E: the executor
  elsif x->>'kind'='contact_restriction' and e->>'subject'='contact_restriction' then
   -- Without the customer's words (the journal never carries them); the newer control revision wins.
   insert into control.nexloop_contact_restrictions(tenant_id,world,consumer_id,active,control_revision,message_id,conversation_id,rule_version,rule_id,
     certainty,matched_text,restricted_at,released_at,released_by,release_intent,release_reason)
    values(e->>'tenant_id',e->>'world',e->>'consumer_id',(e->>'active')::boolean,(e->>'control_revision')::bigint,e->>'message_id',e->>'conversation_id',
     (e->>'rule_version')::integer,e->>'rule_id',e->>'certainty','',(e->>'restricted_at')::timestamptz,(e->>'released_at')::timestamptz,e->>'released_by',
     e->>'release_intent',case when (e->>'active')::boolean then null else 'restored from journal '||p_ref end)
    on conflict(tenant_id,world,consumer_id) do update set active=excluded.active,control_revision=excluded.control_revision,released_at=excluded.released_at,
     released_by=excluded.released_by,release_intent=excluded.release_intent,release_reason=excluded.release_reason
    where control.nexloop_contact_restrictions.control_revision<excluded.control_revision;
   n_contact:=n_contact+1;
  elsif x->>'kind'='contact_restriction' and e->>'subject'='erased_contact_key' then
   insert into control.nexloop_erased_contact_keys(tenant_id,world,key_digest,request_id,created_at,revoked_at,revoked_by)
    values(e->>'tenant_id',e->>'world',e->>'key_digest',e->>'request_id',(e->>'created_at')::timestamptz,(e->>'revoked_at')::timestamptz,e->>'revoked_by')
    on conflict(tenant_id,world,key_digest) do update set revoked_at=excluded.revoked_at,revoked_by=excluded.revoked_by
    where control.nexloop_erased_contact_keys.revoked_at is null and excluded.revoked_at is not null;
   n_keys:=n_keys+1;
  elsif x->>'kind'='revocation' and e->>'subject'='service_credential' then
   update authz.nexloop_service_credentials set status='revoked' where credential_id=e->>'credential_id' and tenant_id=e->>'tenant_id' and status<>'revoked';
   n_cred:=n_cred+1;
  elsif x->>'kind'='revocation' and e->>'subject'='browser_membership' then
   update control.nexloop_browser_memberships set payload=payload||jsonb_build_object('status',e->>'status','revision',greatest((payload->>'revision')::integer+1,(e->>'revision')::integer))
    where tenant_id=e->>'tenant_id' and principal_id=e->>'principal_id' and (payload->>'revision')::integer<(e->>'revision')::integer;
   n_member:=n_member+1;
  else raise exception 'restore replay entry invalid' using errcode='22023';end if;
 end loop;
 if jsonb_array_length(deletions)>0 then
  perform runtime.nexloop_replay_begin(p_ref,s.journal_from);
  for i in 0..(jsonb_array_length(deletions)-1)/1000 loop
   r:=runtime.nexloop_replay_deletions(p_ref,(select jsonb_agg(value order by ord) from jsonb_array_elements(deletions) with ordinality d(value,ord)
    where ord>i*1000 and ord<=(i+1)*1000));
  end loop;
 end if;
 update control.nexloop_restore_state set journal_to=greatest(coalesce(journal_to,journal_from),coalesce(v_max,0));
 return jsonb_build_object('deletions',jsonb_array_length(deletions),'deletion_result',r,'contact_restrictions',n_contact,'erased_contact_keys',n_keys,
  'service_credentials',n_cred,'memberships',n_member);
end $$;

-- A receipt of an erased member's initial account (0162): a definite refusal.
create or replace function control.nexloop_initial_identity_create(p_tenant text,p_application text,p_operator text,p_request text,p_operator_label text,
 p_subject jsonb,p_membership jsonb,p_account jsonb,p_hash text,p_password_fingerprint text,p_seal_key bytea)
returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare allowance control.nexloop_initial_identity_allowances;prior control.nexloop_initial_identity_receipts;
 v_request uuid;v_public_digest text;v_now timestamptz:=clock_timestamp();v_subject text;v_principal text;v_account text;
begin
 if session_user<>'nexloop_identity' or p_operator is distinct from session_user or p_request is null
  or p_tenant is null or p_application is null or p_operator_label is null
  or jsonb_typeof(p_subject) is distinct from 'object' or jsonb_typeof(p_membership) is distinct from 'object' or jsonb_typeof(p_account) is distinct from 'object'
  or octet_length(p_subject::text)+octet_length(p_membership::text)+octet_length(p_account::text)>1048576
  or p_password_fingerprint is null or p_password_fingerprint !~ '^[a-f0-9]{64}$'
  or p_seal_key is null or octet_length(p_seal_key)<>32
  or p_hash is null or p_hash not like '$argon2id$%' or octet_length(p_hash)>1024
 then raise exception 'initial identity rejected' using errcode='42501';end if;
 v_request:=p_request::uuid;
 v_public_digest:=encode(sha256(convert_to(jsonb_build_object('tenant',p_tenant,'application',p_application,'operator',p_operator_label,
  'subject',p_subject,'membership',p_membership,'account',p_account)::text,'UTF8')),'hex');
 perform set_config('eios.tenant_id',p_tenant,true);
 perform 1 from control.nexloop_tenants where tenant_id=p_tenant and status='active' for no key update;
 if not found then raise exception 'initial identity rejected' using errcode='42501';end if;
 perform 1 from control.nexloop_browser_applications where tenant_id=p_tenant and application_id=p_application and active for share;
 if not found then raise exception 'initial identity rejected' using errcode='42501';end if;
 select * into allowance from control.nexloop_initial_identity_allowances where tenant_id=p_tenant and application_id=p_application for update;
 if not found or allowance.operator_label is distinct from p_operator_label or allowance.idempotency_key_digest is distinct from encode(sha256(p_seal_key),'hex') then raise exception 'initial identity rejected' using errcode='42501';end if;
 select * into prior from control.nexloop_initial_identity_receipts where tenant_id=p_tenant and application_id=p_application and request_id=v_request;
 if found then
  -- NX-058 slice E: an erased member's initial account is never returned again - a definite refusal, not the conflict a retry could clear.
  if exists(select 1 from control.nexloop_workbench_member_erasures e where e.tenant_id=p_tenant and e.subject_id=prior.subject_id) then
   raise exception 'initial identity erased' using errcode='42501';end if;
  if prior.public_digest is distinct from v_public_digest or prior.password_fingerprint is distinct from p_password_fingerprint then
   raise exception 'initial identity conflict' using errcode='40001';end if;
  -- Replay after closed allowance does not create/account/grant/session again.
  return jsonb_build_object('created',false,'subject_id',prior.subject_id,'principal_id',prior.principal_id,'account_id',prior.account_id);
 end if;
 if not allowance.enabled or allowance.created_accounts>=allowance.maximum_accounts then raise exception 'initial identity closed' using errcode='42501';end if;
 v_subject:=p_subject->>'subject_id';v_principal:=p_membership->>'principal_id';v_account:=p_account->>'local_account_id';
 if v_subject is null or v_principal is null or v_account is null
  or p_subject->>'kind' is distinct from 'human' or p_subject->>'status' is distinct from 'active' or p_subject->'revision' is distinct from '1'::jsonb
  or p_membership->>'tenant_id' is distinct from p_tenant or p_membership->>'subject_id' is distinct from v_subject
  or p_membership->'trusted_attributes' is distinct from '{}'::jsonb or p_membership->'valid_until' is null
  or p_membership->>'status' is distinct from 'active' or p_membership->'revision' is distinct from '1'::jsonb
  or p_membership->>'valid_from' is null or (p_membership->>'valid_from')::timestamptz>clock_timestamp()
  or p_membership->'valid_until' is distinct from 'null'::jsonb and (p_membership->>'valid_until')::timestamptz<=clock_timestamp()
  or p_account->>'tenant_id' is distinct from p_tenant or p_account->>'subject_id' is distinct from v_subject
  or p_account->>'username' is null or p_account->>'status' is distinct from 'active'
  or p_account->'revision' is distinct from '1'::jsonb or p_account->'session_epoch' is distinct from '1'::jsonb
  or p_account->'failed_attempts' is distinct from '0'::jsonb or p_account->'lockout_level' is distinct from '0'::jsonb
  or p_account->'locked_until' is distinct from 'null'::jsonb or p_account ? 'password_hash' or p_account ? 'password_history'
  or p_account->>'created_at' is null or p_account->>'updated_at' is null
  or (p_account->>'created_at')::timestamptz>clock_timestamp() or (p_account->>'updated_at')::timestamptz>clock_timestamp()
 then raise exception 'initial identity rejected' using errcode='42501';end if;
 insert into control.nexloop_initial_identity_receipts values(p_tenant,p_application,v_request,v_public_digest,p_password_fingerprint,v_subject,v_principal,v_account,p_operator_label,clock_timestamp());
 insert into control.nexloop_browser_subjects values(v_subject,p_subject);
 insert into control.nexloop_browser_memberships values(p_tenant,v_principal,v_subject,p_membership);
 insert into control.nexloop_browser_accounts values(p_tenant,v_account,v_subject,p_account->>'username',p_hash,'{}',p_account);
 update control.nexloop_initial_identity_allowances set created_accounts=created_accounts+1,
  enabled=case when created_accounts+1>=maximum_accounts then false else enabled end where tenant_id=p_tenant and application_id=p_application;
 insert into control.nexloop_browser_login_events(tenant_id,subject_id,event_type,outcome,operator_principal_id,request_id,trace_id,details,created_at)
  values(p_tenant,v_subject,'initial_identity','created',session_user,p_request,p_request,
   jsonb_build_object('account_id',v_account,'principal_id',v_principal,'operator_label',p_operator_label,'business_grant_created',false),clock_timestamp());
 update control.nexloop_tenants set authority_revision=authority_revision+1 where tenant_id=p_tenant;
 return jsonb_build_object('created',true,'subject_id',v_subject,'principal_id',v_principal,'account_id',v_account);
end $$;

do $$ declare f text; begin
 foreach f in array array['control.nexloop_workbench_member_erasure_ledger()','control.nexloop_workbench_member_erase(text,text,uuid,text,timestamptz)',
   'control.nexloop_governed_erase_workbench_member(text,text,text,text,text,jsonb)','control.nexloop_workbench_invitation_username_guard()',
   'control.nexloop_workbench_configuration_erased_guard()','control.nexloop_workbench_member_revoke_for_replay(text,text,text)'] loop
  execute format('alter function %s owner to nexloop_owner',f);
  execute format('revoke all on function %s from public',f);
 end loop;
end $$;

insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('workbench_member.erase_member','erase_workbench_member','human','control.nexloop_governed_erase_workbench_member(text,text,text,text,text,jsonb)','NX-058');
insert into control.nexloop_approval_capability_settings(capability,target_statuses) values ('workbench_member.erase_member','{revoked}');
