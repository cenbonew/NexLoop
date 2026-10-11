-- NX-053 (temporary number 0240): where an erased contact key refuses contact, and the owner's revocation (owner decision
-- 2026-10-11; NX-029 D5; AT-059).
--
-- NX-029 keeps, per erased Consumer, a salted HMAC of each external identity reference (control.nexloop_erased_contact_keys;
-- the salt is per tenant, owner side). Until now nothing used it. Here:
-- 1. control.nexloop_contact_key_erased(tenant, world, ref): the reference's key is erased and not revoked (comparison by HMAC
--    only; the original is never stored or recovered).
-- 2. Enforcement points (fail closed; one refusal code NXK01 'contact unavailable', never which key matched):
--    a. backstop on ontology.objects: a Consumer that gains an external identity reference whose key is erased is refused
--       (identity association and any import that writes external_id_refs);
--    b. conversation ownership (authz.nexloop_conversation_command, 0138 body kept as …_before_contact_keys_v0240): a principal
--       whose browser identity references (its subject's local accounts, the same reference the identity link derives) are
--       erased is not assigned to a new Consumer (a new inbound sender recognised as a new Consumer).
--    The refusal rolls the attempt back; the caller then records it through authz.nexloop_record_contact_key_refusal, which
--    re-derives the references from the authenticated context and writes an audit event with the path only.
-- 3. Revocation: owner-only governed human Action (0124 registry: contact_key.revoke / revoke_contact_key) by key digest or by
--    the erasure request; a reason is required (kept on the row, never journaled: free text). A revoked key no longer refuses.
--    Erasing the same identity again re-activates the key (before-insert trigger), so a revocation never outlives a new erasure.
-- 4. Restore (NX-035): every state change bumps the key's revision and is journaled (without the reason); replay applies the
--    journaled state only when its revision is newer (runtime.nexloop_restore_replay kept as …_before_contact_keys_v0240 for the
--    other entries), so neither a revocation nor a re-activation is lost or undone by a restore.

-- 0. State columns ---------------------------------------------------------------------------------------------------------------
alter table control.nexloop_erased_contact_keys add column revision bigint not null default 1 check(revision>=1);
alter table control.nexloop_erased_contact_keys add column revoke_reason text check(revoke_reason is null or length(revoke_reason) between 1 and 500);
alter table control.nexloop_erased_contact_keys add constraint nexloop_erased_contact_keys_revoked check((revoked_at is null)=(revoked_by is null));

-- 1. Comparison -------------------------------------------------------------------------------------------------------------------
create function control.nexloop_contact_key_erased(p_tenant text,p_world text,p_ref text) returns boolean
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);k bytea;v boolean:=false;
begin
 if p_ref is null then return false;end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select salt into k from control.nexloop_erasure_salts where tenant_id=p_tenant;
 if k is not null then
  v:=exists(select 1 from control.nexloop_erased_contact_keys x where x.tenant_id=p_tenant and x.world=p_world and x.revoked_at is null
   and x.key_digest=encode(extensions.hmac(convert_to(p_ref,'UTF8'),k,'sha256'),'hex'));
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
alter function control.nexloop_contact_key_erased(text,text,text) owner to nexloop_owner;
revoke all on function control.nexloop_contact_key_erased(text,text,text) from public;

-- The browser identity references of a principal: one per local account of its subject (the 0061 identity link derivation).
create function control.nexloop_principal_identity_refs(p_tenant text,p_world text,p_principal text) returns text[]
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v text[];
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select coalesce(array_agg('nexloop:browser-local:'||encode(sha256(convert_to(p_tenant||':'||p_world||':'||a.subject_id||':'||a.local_account_id,'UTF8')),'hex')
   order by a.local_account_id),'{}') into v
 from control.nexloop_browser_memberships m join control.nexloop_browser_accounts a on a.tenant_id=m.tenant_id and a.subject_id=m.subject_id
 where m.tenant_id=p_tenant and m.principal_id=p_principal;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
alter function control.nexloop_principal_identity_refs(text,text,text) owner to nexloop_owner;
revoke all on function control.nexloop_principal_identity_refs(text,text,text) from public;

-- 2a. Backstop: a Consumer never gains an erased external identity reference.
create function ontology.nexloop_consumer_contact_key_guard() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
declare ref jsonb;
begin
 if new.type_name<>'Consumer' or jsonb_typeof(new.properties->'external_id_refs') is distinct from 'array' then return new;end if;
 for ref in select value from jsonb_array_elements(new.properties->'external_id_refs') loop
  if tg_op='UPDATE' and coalesce(old.properties->'external_id_refs','[]'::jsonb)@>jsonb_build_array(ref) then continue;end if;
  if jsonb_typeof(ref)='string' and control.nexloop_contact_key_erased(new.tenant_id,new.world,ref#>>'{}') then
   raise exception 'contact unavailable' using errcode='NXK01';end if;
 end loop;
 return new;
end $$;
alter function ontology.nexloop_consumer_contact_key_guard() owner to nexloop_owner;
revoke all on function ontology.nexloop_consumer_contact_key_guard() from public;
-- Named to fire before the 0061 identity guard (triggers fire in name order): an erased reference is refused as such.
create trigger nexloop_consumer_contact_key_guard before insert or update of properties on ontology.objects
 for each row execute function ontology.nexloop_consumer_contact_key_guard();

-- 2b. Conversation ownership: a principal with an erased identity reference is not assigned to a Consumer.
alter function authz.nexloop_conversation_command(text,text,text,text,text) rename to nexloop_conversation_command_before_contact_keys_v0240;
revoke all on function authz.nexloop_conversation_command_before_contact_keys_v0240(text,text,text,text,text) from public,nexloop_api;
create function authz.nexloop_conversation_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare c jsonb:=p_payload::jsonb;v jsonb;ref text;
begin
 v:=authz.nexloop_conversation_command_before_contact_keys_v0240(p_digest,p_world,p_text,p_signature,p_payload);
 -- After the kept body verified the signed command and the caller (a replay of an existing ownership is not a new contact).
 if c->>'verb' in ('prepare_owner','commit_owner') and coalesce((v->>'replay')::boolean,false) is false then
  foreach ref in array control.nexloop_principal_identity_refs((p_text::jsonb)->>'tenant_id',p_world,c->>'principal_id') loop
   if control.nexloop_contact_key_erased((p_text::jsonb)->>'tenant_id',p_world,ref) then raise exception 'contact unavailable' using errcode='NXK01';end if;
  end loop;
 end if;
 return v;
end $$;
alter function authz.nexloop_conversation_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_conversation_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_conversation_command(text,text,text,text,text) to nexloop_api;

-- Recording a refusal (its own transaction, after the refused one rolled back): the references are re-derived from the
-- authenticated context, never taken from the caller; nothing is written unless one of them is erased. Path only, no key.
create function authz.nexloop_record_contact_key_refusal(p_digest text,p_world text,p_path text,p_principal text) returns boolean
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;v_tenant text;v_actor text;refs text[];ref text;hit boolean:=false;
begin
 if session_user<>'nexloop_api' or p_path not in ('identity_link','conversation_owner') then raise exception 'contact key refusal unavailable' using errcode='42501';end if;
 if p_path='identity_link' then
  ident:=authz.nexloop_browser_identity_snapshot(p_digest,p_world);v_tenant:=ident->'binding'->>'tenant_id';v_actor:=ident->'binding'->>'subject_principal_id';
  refs:=array['nexloop:browser-local:'||encode(sha256(convert_to(v_tenant||':'||p_world||':'||(ident->'binding'->>'subject_id')||':'||(ident->'binding'->>'credential_id'),'UTF8')),'hex')];
 else
  ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);v_tenant:=ident->'binding'->>'tenant_id';v_actor:=ident->'binding'->>'subject_principal_id';
  refs:=control.nexloop_principal_identity_refs(v_tenant,p_world,p_principal);
 end if;
 foreach ref in array coalesce(refs,'{}') loop
  hit:=hit or control.nexloop_contact_key_erased(v_tenant,p_world,ref);
 end loop;
 if not hit then return false;end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('contact-key-refused:'||gen_random_uuid(),'nexloop.contact_key.refused',v_tenant,coalesce(v_actor,'unknown'),'contact-key',p_path,
   jsonb_build_object('path',p_path,'world',p_world,'code','NXK01'),clock_timestamp());
 return true;
end $$;
alter function authz.nexloop_record_contact_key_refusal(text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_record_contact_key_refusal(text,text,text,text) from public;
grant execute on function authz.nexloop_record_contact_key_refusal(text,text,text,text) to nexloop_api;

-- 3. Revocation (owner only, governed entry) and re-activation by a new erasure --------------------------------------------------
create function control.nexloop_governed_revoke_contact_key(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare n integer;
begin
 if body->>'operation' is distinct from 'revoke_contact_key' or coalesce(length(body->>'reason'),0) not between 1 and 500
  or ((body->>'key_digest') is null)=((body->>'erasure_request_id') is null)
  or coalesce(body->>'key_digest','0'||repeat('0',63))!~'^[0-9a-f]{64}$' or coalesce(body->>'erasure_request_id','x')!~'^[A-Za-z0-9._:-]{1,190}$' then
  raise exception 'contact key revocation invalid' using errcode='22023';end if;
 if control.nexloop_workbench_role(p_tenant,p_principal) is distinct from 'owner' then raise exception 'contact key revocation is owner only' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 update control.nexloop_erased_contact_keys set revoked_at=clock_timestamp(),revoked_by=p_principal,revoke_reason=body->>'reason',revision=revision+1
  where tenant_id=p_tenant and world=p_world and revoked_at is null
   and (key_digest=body->>'key_digest' or request_id=body->>'erasure_request_id');
 get diagnostics n=row_count;
 if n=0 and not exists(select 1 from control.nexloop_erased_contact_keys where tenant_id=p_tenant and world=p_world
   and (key_digest=body->>'key_digest' or request_id=body->>'erasure_request_id')) then
  raise exception 'contact key unknown' using errcode='22023';end if;
 return jsonb_build_object('revoked',n,'world',p_world);
end $$;
alter function control.nexloop_governed_revoke_contact_key(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_governed_revoke_contact_key(text,text,text,text,text,jsonb) from public;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('contact_key.revoke','revoke_contact_key','human','control.nexloop_governed_revoke_contact_key(text,text,text,text,text,jsonb)','NX-053');

create function control.nexloop_erased_contact_key_reactivate() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
begin
 if current_setting('nexloop.contact_key_replay',true)='on' or new.revoked_at is not null then return new;end if;
 update control.nexloop_erased_contact_keys set revoked_at=null,revoked_by=null,revoke_reason=null,request_id=new.request_id,revision=revision+1
  where tenant_id=new.tenant_id and world=new.world and key_digest=new.key_digest and revoked_at is not null;
 return new;  -- an existing row then conflicts and the erasure's insert does nothing, as before
end $$;
alter function control.nexloop_erased_contact_key_reactivate() owner to nexloop_owner;
revoke all on function control.nexloop_erased_contact_key_reactivate() from public;
create trigger nx053_reactivate before insert on control.nexloop_erased_contact_keys for each row execute function control.nexloop_erased_contact_key_reactivate();

-- 4. Journal (with the revision; never the reason) and replay ------------------------------------------------------------------
create or replace function control.nexloop_journal_on_erased_contact_key() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set timezone='UTC' as $$
begin
 if tg_op='UPDATE' and new.revoked_at is not distinct from old.revoked_at and new.revision=old.revision then return null;end if;
 perform control.nexloop_restore_journal_append('contact_restriction',jsonb_build_object('subject','erased_contact_key','tenant_id',new.tenant_id,
  'world',new.world,'key_digest',new.key_digest,'request_id',new.request_id,'created_at',new.created_at,'revoked_at',new.revoked_at,'revoked_by',new.revoked_by,
  'revision',new.revision));
 return null;
end $$;

alter function runtime.nexloop_restore_replay(text,jsonb) rename to nexloop_restore_replay_before_contact_keys_v0240;
revoke all on function runtime.nexloop_restore_replay_before_contact_keys_v0240(text,jsonb) from public;
create function runtime.nexloop_restore_replay(p_ref text,p_entries jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare s control.nexloop_restore_state;x jsonb;e jsonb;rest jsonb:='[]'::jsonb;n integer:=0;v jsonb;v_rev bigint;
begin
 s:=runtime.nexloop_restore_require(p_ref,'restored');
 if jsonb_typeof(p_entries) is distinct from 'array' then raise exception 'restore replay invalid' using errcode='22023';end if;
 perform set_config('nexloop.contact_key_replay','on',true);
 for x in select value from jsonb_array_elements(p_entries) order by (value->>'seq')::bigint loop
  e:=x->'entry';
  if x->>'kind'='contact_restriction' and jsonb_typeof(e)='object' and e->>'subject'='erased_contact_key' then
   if coalesce(x->>'seq','')!~'^[1-9][0-9]{0,18}$' or (x->>'seq')::bigint<=s.journal_from then raise exception 'restore replay entry invalid' using errcode='22023';end if;
   perform set_config('eios.tenant_id',e->>'tenant_id',true);
   if e ? 'revision' then
    -- The journaled state wins when it is newer (revocation and re-activation alike).
    v_rev:=(e->>'revision')::bigint;
    insert into control.nexloop_erased_contact_keys(tenant_id,world,key_digest,request_id,created_at,revoked_at,revoked_by,revoke_reason,revision)
     values(e->>'tenant_id',e->>'world',e->>'key_digest',e->>'request_id',(e->>'created_at')::timestamptz,(e->>'revoked_at')::timestamptz,e->>'revoked_by',
      case when e->>'revoked_at' is not null then 'restored from journal '||p_ref end,v_rev)
     on conflict(tenant_id,world,key_digest) do update set revoked_at=excluded.revoked_at,revoked_by=excluded.revoked_by,revoke_reason=excluded.revoke_reason,
      request_id=excluded.request_id,revision=excluded.revision
     where control.nexloop_erased_contact_keys.revision<excluded.revision;
   else
    -- An entry journaled before 0240: the earlier rule (a revocation is never undone by replay).
    insert into control.nexloop_erased_contact_keys(tenant_id,world,key_digest,request_id,created_at,revoked_at,revoked_by,revoke_reason)
     values(e->>'tenant_id',e->>'world',e->>'key_digest',e->>'request_id',(e->>'created_at')::timestamptz,(e->>'revoked_at')::timestamptz,e->>'revoked_by',
      case when e->>'revoked_at' is not null then 'restored from journal '||p_ref end)
     on conflict(tenant_id,world,key_digest) do update set revoked_at=excluded.revoked_at,revoked_by=excluded.revoked_by,revoke_reason=excluded.revoke_reason
     where control.nexloop_erased_contact_keys.revoked_at is null and excluded.revoked_at is not null;
   end if;
   n:=n+1;
  else
   rest:=rest||jsonb_build_array(x);
  end if;
 end loop;
 perform set_config('nexloop.contact_key_replay','off',true);
 if jsonb_array_length(rest)>0 or n=0 then
  v:=runtime.nexloop_restore_replay_before_contact_keys_v0240(p_ref,rest);
 else
  v:=jsonb_build_object('deletions',0,'deletion_result','{}'::jsonb,'contact_restrictions',0,'erased_contact_keys',0,'service_credentials',0,'memberships',0);
 end if;
 -- The journal position covers the entries handled here too.
 update control.nexloop_restore_state set journal_to=greatest(coalesce(journal_to,journal_from),coalesce((select max((value->>'seq')::bigint) from jsonb_array_elements(p_entries)),0));
 return v||jsonb_build_object('erased_contact_keys',(v->>'erased_contact_keys')::integer+n);
end $$;
alter function runtime.nexloop_restore_replay(text,jsonb) owner to nexloop_owner;
revoke all on function runtime.nexloop_restore_replay(text,jsonb) from public;
