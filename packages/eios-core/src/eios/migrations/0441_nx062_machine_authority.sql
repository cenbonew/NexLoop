-- NX-062 M5a (docs/implementation/NX-062-M5-design.md §2, §5, §9 R1 and R6; ADR-041 amendment A1): the machine authority facts, derived
-- at snapshot time from the grants and never stored; the machine Action authority assertion the M5b machine core will call; no stored
-- fact row for a machine principal; and the three approval exclusions (decision rows, single-person mode, eligible approvers), all decided
-- by the machine client table, never by a payload. The migration number is temporary; it lives only in the file name and the catalog.
--
-- Machine fact kinds and keys (each payload carries tenant_id; record_hash = sha256 of the payload text):
--   subject                [subject_id]              the client row (status, registration)
--   actor                  [principal_id]            the client row, actor kind 'machine'
--   machine_authentication [key_id]                  the key row (prefix, world, scope, expiry, pepper version; never the digest)
--   grants                 [principal_id,capability] the latest grant row: revision, state, object types (R1: a revoke and a re-grant of
--                                                    the same types changes the hash)
--   scope                  [key_id,capability]       whether the key's scope holds the capability
--   policies               [capability]              the current allowlist row (revision, state, kind, effect)

-- R1: no stored authority fact for a machine principal (or subject), judged by the client table.
create function authz.nexloop_machine_fact_row_guard() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if exists(select 1 from authz.nexloop_machine_clients c where c.principal_id=any(new.entity_key) or c.subject_id=any(new.entity_key)) then
  raise exception 'machine authority facts are derived, never stored' using errcode='42501';end if;
 return new;
end $$;
alter function authz.nexloop_machine_fact_row_guard() owner to nexloop_owner;
revoke all on function authz.nexloop_machine_fact_row_guard() from public;
create trigger nx062_machine_fact_row_guard before insert or update on authz.nexloop_authority_facts
 for each row execute function authz.nexloop_machine_fact_row_guard();

-- The fact payload of one (kind, key), from the identity of a live machine key. Null when the fact does not exist (no grant row yet).
create function authz.nexloop_machine_authority_fact_snapshot(p_prefix text,p_digest text,p_kind text,p_key text[]) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;b jsonb;v_tenant text;v_client text;v_principal text;v_key uuid;c authz.nexloop_machine_clients;k authz.nexloop_machine_keys;
 g authz.nexloop_machine_client_grants;cap control.nexloop_machine_capabilities;value jsonb;valid boolean;
begin
 ident:=authz.nexloop_machine_identity_snapshot(p_prefix,p_digest);b:=ident->'binding';
 v_tenant:=b->>'tenant_id';v_client:=ident->>'client_id';v_principal:=b->>'subject_principal_id';v_key:=(b->>'credential_id')::uuid;
 valid:=case p_kind
  when 'subject' then p_key=array[b->>'subject_id']
  when 'actor' then p_key=array[v_principal]
  when 'machine_authentication' then p_key=array[v_key::text]
  when 'grants' then cardinality(p_key)=2 and p_key[1]=v_principal
  when 'scope' then cardinality(p_key)=2 and p_key[1]=v_key::text
  when 'policies' then cardinality(p_key)=1
  else false end;
 if valid is distinct from true or exists(select 1 from unnest(p_key) x where x is null) then
  raise exception 'machine authority fact binding denied' using errcode='42501';end if;
 select * into c from authz.nexloop_machine_clients where tenant_id=v_tenant and client_id=v_client;
 if p_kind='subject' then
  value:=jsonb_build_object('tenant_id',v_tenant,'subject_id',c.subject_id,'kind','machine','status',c.status,'registered_at',to_jsonb(c.registered_at),
   'status_changed_at',to_jsonb(c.status_changed_at));
 elsif p_kind='actor' then
  value:=jsonb_build_object('tenant_id',v_tenant,'actor_principal_id',c.principal_id,'kind','machine','client_id',c.client_id,'status',c.status,
   'status_changed_at',to_jsonb(c.status_changed_at));
 elsif p_kind='machine_authentication' then
  select * into k from authz.nexloop_machine_keys where key_id=v_key;
  value:=jsonb_build_object('tenant_id',v_tenant,'key_id',k.key_id::text,'key_prefix',k.prefix_id,'client_id',k.client_id,'world',k.world,
   'scope',to_jsonb(k.scope),'status',k.status,'expires_at',to_jsonb(k.expires_at),'pepper_version',k.pepper_version);
 elsif p_kind='grants' then
  select * into g from authz.nexloop_machine_client_grants where tenant_id=v_tenant and client_id=v_client and capability=p_key[2]
   order by revision desc limit 1;
  if not found then return null;end if;
  value:=jsonb_build_object('tenant_id',v_tenant,'principal_id',v_principal,'capability',g.capability,'revision',g.revision,'state',g.state,
   'object_types',to_jsonb(g.object_types),'recorded_at',to_jsonb(g.recorded_at));
 elsif p_kind='scope' then
  select * into k from authz.nexloop_machine_keys where key_id=v_key;
  value:=jsonb_build_object('tenant_id',v_tenant,'key_id',k.key_id::text,'capability',p_key[2],'in_scope',p_key[2]=any(k.scope));
 else
  select * into cap from control.nexloop_machine_capabilities where capability=p_key[1] order by revision desc limit 1;
  if not found then return null;end if;
  value:=jsonb_build_object('tenant_id',v_tenant,'capability',cap.capability,'revision',cap.revision,'state',cap.state,'kind',cap.kind,'effect',cap.effect,
   'forbidden',control.nexloop_machine_capability_forbidden(cap.capability));
 end if;
 return jsonb_build_object('payload',value,'record_hash',encode(sha256(convert_to(value::text,'UTF8')),'hex'));
end $$;
alter function authz.nexloop_machine_authority_fact_snapshot(text,text,text,text[]) owner to nexloop_owner;
revoke all on function authz.nexloop_machine_authority_fact_snapshot(text,text,text,text[]) from public;
grant execute on function authz.nexloop_machine_authority_fact_snapshot(text,text,text,text[]) to nexloop_api;

-- The machine Action authority assertion (granted to no role; the M5b machine core calls it before the handler and again at the commit
-- tail). The claims name the capability and carry the six facts with the hashes the API read; every hash is recomputed here. R1: the
-- client row, the key row and the capability's latest grant row are locked FOR SHARE, so a revoke (the grant and revoke handlers lock
-- the client row FOR UPDATE) and a commit serialise: once the revoke is committed, a later assertion refuses.
create function authz.nexloop_machine_assert_action_authority(p_prefix text,p_digest text,p_world text,p_claims jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;b jsonb;v_tenant text;v_client text;v_principal text;v_key text;v_cap text:=p_claims->>'capability';entry jsonb;keys text[];
 snap jsonb;v_grant integer;expected text[];
begin
 ident:=authz.nexloop_machine_identity_snapshot(p_prefix,p_digest);b:=ident->'binding';
 v_tenant:=b->>'tenant_id';v_client:=ident->>'client_id';v_principal:=b->>'subject_principal_id';v_key:=b->>'credential_id';
 perform 1 from authz.nexloop_machine_clients c where c.tenant_id=v_tenant and c.client_id=v_client for share;
 perform 1 from authz.nexloop_machine_keys k where k.key_id=v_key::uuid for share;
 select max(g.revision) into v_grant from authz.nexloop_machine_client_grants g where g.tenant_id=v_tenant and g.client_id=v_client and g.capability=v_cap;
 perform 1 from authz.nexloop_machine_client_grants g where g.tenant_id=v_tenant and g.client_id=v_client and g.capability=v_cap and g.revision=v_grant for share;
 -- Identity again, under the locks.
 ident:=authz.nexloop_machine_identity_snapshot(p_prefix,p_digest);
 if p_claims->>'tenant_id' is distinct from v_tenant or p_claims->>'credential_id' is distinct from v_key
  or p_claims->>'principal_id' is distinct from v_principal or p_claims->>'client_id' is distinct from v_client
  or p_claims->>'world' is distinct from p_world or ident->>'world' is distinct from p_world
  or p_claims->>'operation' is distinct from 'execute' or coalesce(v_cap,'')!~'^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$'
  or p_claims->>'expires_at' is null or (p_claims->>'expires_at')::timestamptz<=clock_timestamp()
  or (p_claims->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds'
  or jsonb_typeof(p_claims->'facts') is distinct from 'array' or jsonb_array_length(p_claims->'facts')<>6 then
  raise exception 'machine action authority stale' using errcode='42501';end if;
 expected:=array['actor','grants','machine_authentication','policies','scope','subject'];
 if (select array_agg(e->>'kind' order by e->>'kind') from jsonb_array_elements(p_claims->'facts') e) is distinct from expected then
  raise exception 'machine action fact coverage invalid' using errcode='42501';end if;
 for entry in select value from jsonb_array_elements(p_claims->'facts') loop
  if jsonb_typeof(entry->'key') is distinct from 'array' then raise exception 'machine action authority invalid' using errcode='42501';end if;
  select array_agg(x) into keys from jsonb_array_elements_text(entry->'key') x;
  if entry->>'kind' in ('grants','scope','policies') and keys[cardinality(keys)] is distinct from v_cap then
   raise exception 'machine action authority invalid' using errcode='42501';end if;
  snap:=authz.nexloop_machine_authority_fact_snapshot(p_prefix,p_digest,entry->>'kind',keys);
  if snap is null or snap->>'record_hash' is distinct from entry->>'record_hash' then
   raise exception 'machine action authority changed' using errcode='42501';end if;
  -- The facts must also grant: an exact copy of a refusing fact is no authority.
  if (entry->>'kind'='subject' and snap->'payload'->>'status'<>'active')
   or (entry->>'kind'='actor' and (snap->'payload'->>'status'<>'active' or snap->'payload'->>'kind'<>'machine'))
   or (entry->>'kind'='grants' and snap->'payload'->>'state'<>'active')
   or (entry->>'kind'='scope' and (snap->'payload'->>'in_scope')::boolean is not true)
   or (entry->>'kind'='policies' and (snap->'payload'->>'state'<>'active' or snap->'payload'->>'kind'<>'write' or (snap->'payload'->>'forbidden')::boolean)) then
   raise exception 'machine action authority denied' using errcode='42501';end if;
 end loop;
 perform set_config('eios.tenant_id',v_tenant,true);
 return b||jsonb_build_object('client_id',v_client,'key_prefix',ident->>'key_prefix','capability',v_cap,'grant_revision',v_grant);
end $$;
alter function authz.nexloop_machine_assert_action_authority(text,text,text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_machine_assert_action_authority(text,text,text,jsonb) from public;

-- Approvals (C8, R6) --------------------------------------------------------------------------------------------------------------
create function authz.nexloop_machine_principal(p_tenant text,p_who text) returns boolean
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select exists(select 1 from authz.nexloop_machine_clients c where (p_tenant is null or c.tenant_id=p_tenant) and (c.principal_id=p_who or c.subject_id=p_who))
$$;
alter function authz.nexloop_machine_principal(text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_machine_principal(text,text) from public;

-- 1. Eligible approvers never include a machine subject.
alter function control.nexloop_approval_eligible_subjects(text,text[]) rename to nexloop_approval_eligible_subjects_before_machine;
create function control.nexloop_approval_eligible_subjects(p_tenant text,p_roles text[]) returns text[]
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 return coalesce((select array_agg(s order by s) from unnest(control.nexloop_approval_eligible_subjects_before_machine(p_tenant,p_roles)) s
  where not authz.nexloop_machine_principal(null,s)),'{}'::text[]);
end $$;

-- 2. The single-person mode never takes effect for a machine requester.
alter function control.nexloop_approval_solo_effective(text,text,text,integer,text) rename to nexloop_approval_solo_effective_before_machine;
create function control.nexloop_approval_solo_effective(p_tenant text,p_world text,p_capability text,p_policy_version integer,p_requester text) returns boolean
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if authz.nexloop_machine_principal(null,p_requester) then return false;end if;
 return control.nexloop_approval_solo_effective_before_machine(p_tenant,p_world,p_capability,p_policy_version,p_requester);
end $$;

-- 3. A machine never decides an approval: every approve or reject decision row, whichever path writes it, is refused when its actor is a
-- machine principal or subject (the decision handler is registered by its oid and keeps its 0326 body; the check sits on its table).
create function control.nexloop_approval_decision_machine_guard() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if new.outcome in ('approved','rejected') and (authz.nexloop_machine_principal(null,new.actor_principal) or authz.nexloop_machine_principal(null,new.actor_subject)) then
  raise exception 'approval: approver_unauthorized' using errcode='42501';end if;
 return new;
end $$;
alter function control.nexloop_approval_decision_machine_guard() owner to nexloop_owner;
revoke all on function control.nexloop_approval_decision_machine_guard() from public;
create trigger nx062_machine_never_decides before insert or update on control.nexloop_approval_decisions
 for each row execute function control.nexloop_approval_decision_machine_guard();

do $grants$
declare pair text[];h text;
begin
 -- Each wrapper takes over the grantees of the function it wraps; the renamed or copied body is granted to no role.
 foreach pair slice 1 in array array[
  array['control.nexloop_approval_eligible_subjects(text,text[])','control.nexloop_approval_eligible_subjects_before_machine(text,text[])'],
  array['control.nexloop_approval_solo_effective(text,text,text,integer,text)','control.nexloop_approval_solo_effective_before_machine(text,text,text,integer,text)']] loop
  execute 'alter function '||pair[1]||' owner to nexloop_owner';
  execute 'alter function '||pair[2]||' owner to nexloop_owner';
  execute 'revoke all on function '||pair[1]||' from public';
  for h in select r.rolname from pg_proc p cross join aclexplode(p.proacl) a join pg_roles r on r.oid=a.grantee
    where p.oid=pair[2]::regprocedure and a.privilege_type='EXECUTE' and a.grantee<>p.proowner loop
   execute format('grant execute on function %s to %I',pair[1],h);
  end loop;
  execute 'revoke all on function '||pair[2]||' from public';
  for h in select r.rolname from pg_proc p cross join aclexplode(p.proacl) a join pg_roles r on r.oid=a.grantee
    where p.oid=pair[2]::regprocedure and a.privilege_type='EXECUTE' and a.grantee<>p.proowner loop
   execute format('revoke all on function %s from %I',pair[2],h);
  end loop;
 end loop;
end $grants$;
