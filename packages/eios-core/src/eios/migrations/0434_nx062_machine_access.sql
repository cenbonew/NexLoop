-- NX-062 M2a (docs/implementation/NX-062-design.md §1, §2.2, §3.2, §10 C1-C3): machine clients and machine keys, the machine identity
-- snapshot, realm exclusion against the service and browser realms, and the machine capability allowlist. The migration number is
-- temporary; it lives only in the file name and the catalog.
--
-- * authz.nexloop_machine_clients: an enterprise program registered under a tenant as an EIOS service principal (design §1.1 A); its
--   principal never belongs to a service credential or a browser membership (C1). Status active|inactive; inactive revokes every key of
--   the client in the same statement, and activating again never revives one (T-2).
-- * authz.nexloop_machine_keys: one row per key; only the HMAC digest (lowercase hex) and its pepper version are stored, never the key
--   (design §2.2; the pepper stays in the API's private file, owner Q-B). Lookup is by prefix_id (C2); one tenant, one world, a scope of
--   allowlisted capabilities, expiry at most 90 days after issue, at most two active keys per client and a second active key only while
--   the other expires within 7 days (Q-2). A revoked key stays revoked; prefix, digest, client, world and scope never change.
-- * authz.nexloop_machine_key_pepper_version(prefix) and authz.nexloop_machine_identity_snapshot(prefix, digest) for nexloop_api (C2:
--   the API computes the HMAC once, with the row's version). Every validity test reads clock_timestamp().
-- * authz.nexloop_service_identity_snapshot is renamed once (..._before_machine_v0434, no grants) and wrapped: a machine key digest or a
--   machine client principal is refused there (C1), so no service, Run or browser path can authenticate a machine and the machine
--   snapshot refuses any digest or principal of those realms.
-- * control.nexloop_machine_read_verbs and control.nexloop_machine_capabilities (C3): append-only; a capability changes by appending its
--   next revision (state active|retired), the latest revision is current. A capability is accepted only when it is a registered read verb
--   (kind read) or a governed capability whose subject rule is not 'human' (kind write) and matches no never-grant marker
--   (control.nexloop_machine_never_grant_markers(), the list tests lock against nexloop_eios.machine_access). No API, worker or Runtime
--   role holds any privilege on these tables; rows come from migrations (and, in M2b, from the owner's governed Action).
-- * Lifecycle definer functions (register, set status, issue, rotate, revoke) are owner functions granted to no role; M2b registers
--   them behind the governed entry, executed by the owner only (Q-4).
set local timezone = 'UTC';

-- Allowlist ------------------------------------------------------------------------------------------------------
create function control.nexloop_machine_never_grant_markers() returns text[]
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select array['approval','machine_client','machine_key','service_grant','grant','credential','member','role','owner','publish','schema',
  'policy','rule','relation_type','erase','erasure','retention','legal_hold','real_dispatch','channel','remediation','external_effect',
  'nexloop.service.request','nexloop.enterprise.deliver','nexloop.service.query','nexloop.service.receipt_reconcile',
  'nexloop.receipt.reconcile','schema.review']::text[]
$$;
alter function control.nexloop_machine_never_grant_markers() owner to nexloop_owner;

create function control.nexloop_machine_capability_forbidden(p_capability text) returns boolean
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select p_capability is null or exists(select 1 from unnest(control.nexloop_machine_never_grant_markers()) m where strpos(lower(p_capability),m)>0)
$$;
alter function control.nexloop_machine_capability_forbidden(text) owner to nexloop_owner;

create table control.nexloop_machine_read_verbs (
 verb text primary key check(verb~'^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$'),
 projection jsonb not null check(jsonb_typeof(projection)='array' and jsonb_array_length(projection) between 1 and 64),
 description text not null check(length(description) between 1 and 400),
 source_task text not null check(source_task~'^NX-[0-9]{3}$'),
 registered_at timestamptz not null default clock_timestamp()
);
create table control.nexloop_machine_capabilities (
 capability text not null check(capability~'^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$'),
 revision integer not null check(revision>=1),
 state text not null check(state in ('active','retired')),
 kind text not null check(kind in ('read','write')),
 effect boolean not null default false,
 description text not null check(length(description) between 1 and 400),
 source text not null check(source~'^(migration:[0-9]{4}_[a-z0-9_]+\.sql|action:[A-Za-z0-9._:-]{1,200})$'),
 recorded_by text not null check(length(recorded_by) between 1 and 200),
 recorded_at timestamptz not null default clock_timestamp(),
 primary key(capability,revision)
);
create function control.nexloop_machine_capability_guard() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
declare latest integer;rule text;
begin
 if control.nexloop_machine_capability_forbidden(new.capability) then
  raise exception 'machine capability never granted' using errcode='42501';end if;
 select max(revision) into latest from control.nexloop_machine_capabilities where capability=new.capability;
 if new.revision is distinct from coalesce(latest,0)+1 then raise exception 'machine capability revision must follow the latest' using errcode='22023';end if;
 if new.kind='read' then
  if new.effect or not exists(select 1 from control.nexloop_machine_read_verbs v where v.verb=new.capability) then
   raise exception 'machine capability not a registered read verb' using errcode='42501';end if;
 else
  select g.subject_rule into rule from control.nexloop_governed_capabilities g where g.capability=new.capability;
  if rule is null or rule='human' then raise exception 'machine capability not a non-human governed capability' using errcode='42501';end if;
 end if;
 return new;
end $$;
alter function control.nexloop_machine_capability_guard() owner to nexloop_owner;
revoke all on function control.nexloop_machine_capability_guard() from public;
create function control.nexloop_machine_read_verb_guard() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
begin
 if control.nexloop_machine_capability_forbidden(new.verb) then raise exception 'machine capability never granted' using errcode='42501';end if;
 return new;
end $$;
alter function control.nexloop_machine_read_verb_guard() owner to nexloop_owner;
revoke all on function control.nexloop_machine_read_verb_guard() from public;
create trigger nx062_guard before insert on control.nexloop_machine_capabilities for each row execute function control.nexloop_machine_capability_guard();
create trigger nx062_guard before insert on control.nexloop_machine_read_verbs for each row execute function control.nexloop_machine_read_verb_guard();
create trigger nx062_append_only before update or delete on control.nexloop_machine_capabilities for each row execute function control.nexloop_nx022_append_only();
create trigger nx062_append_only before update or delete on control.nexloop_machine_read_verbs for each row execute function control.nexloop_nx022_append_only();
create trigger nx062_no_truncate before truncate on control.nexloop_machine_capabilities for each statement execute function control.nexloop_nx022_append_only();
create trigger nx062_no_truncate before truncate on control.nexloop_machine_read_verbs for each statement execute function control.nexloop_nx022_append_only();

create function control.nexloop_machine_capability_current(p_capability text) returns boolean
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select coalesce((select c.state='active' from control.nexloop_machine_capabilities c where c.capability=p_capability
  order by c.revision desc limit 1),false) and not control.nexloop_machine_capability_forbidden(p_capability)
$$;
alter function control.nexloop_machine_capability_current(text) owner to nexloop_owner;

-- Clients and keys ------------------------------------------------------------------------------------------------
create table authz.nexloop_machine_clients (
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 client_id text not null check(client_id~'^[a-z][a-z0-9-]{2,62}$'),
 principal_id text not null unique check(principal_id~'^nexloop-machine:[a-z][a-z0-9-]{2,62}$'),
 subject_id text not null unique check(subject_id~'^nexloop-machine-subject:[a-z][a-z0-9-]{2,62}$'),
 display_name text not null check(length(display_name) between 1 and 120),
 purpose text not null check(length(purpose) between 1 and 400),
 status text not null check(status in ('active','inactive')),
 issued_by_principal text not null check(length(issued_by_principal) between 1 and 200),
 registered_at timestamptz not null default clock_timestamp(),
 status_changed_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,client_id),
 check(principal_id='nexloop-machine:'||client_id and subject_id='nexloop-machine-subject:'||client_id)
);
create table authz.nexloop_machine_keys (
 key_id uuid primary key,
 tenant_id text not null,
 client_id text not null,
 prefix_id text not null unique check(prefix_id~'^[a-z2-7]{12}$'),
 digest text not null unique check(digest~'^[0-9a-f]{64}$'),
 pepper_version integer not null check(pepper_version between 1 and 999999999),
 world text not null check(runtime.nexloop_world_kind(world) is not null),
 scope text[] not null check(cardinality(scope) between 1 and 64),
 issued_at timestamptz not null default clock_timestamp(),
 expires_at timestamptz not null,
 status text not null check(status in ('active','revoked')),
 revoked_at timestamptz,
 revoked_reason text check(revoked_reason in ('revoked','rotated','client_deactivated')),
 issued_by_principal text not null check(length(issued_by_principal) between 1 and 200),
 issue_request text not null check(issue_request~'^[A-Za-z0-9._:-]{1,200}$'),
 foreign key(tenant_id,client_id) references authz.nexloop_machine_clients(tenant_id,client_id),
 check(expires_at>issued_at and expires_at<=issued_at+interval '90 days'),
 check((status='revoked')=(revoked_at is not null and revoked_reason is not null))
);
create index nexloop_machine_keys_client on authz.nexloop_machine_keys(tenant_id,client_id) where status='active';

-- C1 at write time: a machine principal is never a service or browser principal, and a machine digest never a service or browser digest.
create function authz.nexloop_machine_client_guard() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if tg_op='UPDATE' and (new.tenant_id,new.client_id,new.principal_id,new.subject_id,new.registered_at,new.issued_by_principal)
   is distinct from (old.tenant_id,old.client_id,old.principal_id,old.subject_id,old.registered_at,old.issued_by_principal) then
  raise exception 'machine client identity is immutable' using errcode='42501';end if;
 if exists(select 1 from authz.nexloop_service_credentials sc where new.principal_id in (sc.binding->>'principal_id',sc.binding->>'subject_principal_id')
   or new.subject_id=sc.binding->>'subject_id')
  or exists(select 1 from control.nexloop_browser_memberships bm where bm.principal_id=new.principal_id) then
  raise exception 'authentication kind conflict' using errcode='42501';end if;
 return new;
end $$;
alter function authz.nexloop_machine_client_guard() owner to nexloop_owner;
revoke all on function authz.nexloop_machine_client_guard() from public;
create trigger nx062_guard before insert or update on authz.nexloop_machine_clients for each row execute function authz.nexloop_machine_client_guard();
create trigger nx062_no_delete before delete or truncate on authz.nexloop_machine_clients for each statement execute function control.nexloop_nx022_append_only();

create function authz.nexloop_machine_key_guard() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare others integer;soonest timestamptz;s text;
begin
 if tg_op='UPDATE' then
  if (new.key_id,new.tenant_id,new.client_id,new.prefix_id,new.digest,new.pepper_version,new.world,new.scope,new.issued_at,new.issued_by_principal,new.issue_request)
    is distinct from (old.key_id,old.tenant_id,old.client_id,old.prefix_id,old.digest,old.pepper_version,old.world,old.scope,old.issued_at,old.issued_by_principal,old.issue_request)
   or (old.status='revoked' and (new.status,new.revoked_at,new.revoked_reason,new.expires_at) is distinct from (old.status,old.revoked_at,old.revoked_reason,old.expires_at))
   or new.expires_at>old.expires_at then
   raise exception 'machine key is immutable' using errcode='42501';end if;
  return new;
 end if;
 if new.status<>'active' then raise exception 'machine key must be issued active' using errcode='22023';end if;
 if not exists(select 1 from authz.nexloop_machine_clients c where c.tenant_id=new.tenant_id and c.client_id=new.client_id and c.status='active') then
  raise exception 'machine client inactive' using errcode='42501';end if;
 foreach s in array new.scope loop
  if not control.nexloop_machine_capability_current(s) then raise exception 'machine key scope outside the allowlist' using errcode='42501';end if;
 end loop;
 if cardinality(new.scope)<>(select count(distinct x) from unnest(new.scope) x) then raise exception 'machine key scope duplicated' using errcode='22023';end if;
 if exists(select 1 from authz.nexloop_service_credentials sc where sc.token_digest=new.digest)
  or exists(select 1 from authz.nexloop_browser_token_realms br where br.token_digest=new.digest) then
  raise exception 'authentication kind conflict' using errcode='42501';end if;
 select count(*),min(k.expires_at) into others,soonest from authz.nexloop_machine_keys k
  where k.tenant_id=new.tenant_id and k.client_id=new.client_id and k.status='active' and k.expires_at>clock_timestamp();
 if others>=2 then raise exception 'machine_key_limit' using errcode='42501';end if;
 if others=1 and soonest>clock_timestamp()+interval '7 days' then raise exception 'machine_key_overlap' using errcode='42501';end if;
 return new;
end $$;
alter function authz.nexloop_machine_key_guard() owner to nexloop_owner;
revoke all on function authz.nexloop_machine_key_guard() from public;
create trigger nx062_guard before insert or update on authz.nexloop_machine_keys for each row execute function authz.nexloop_machine_key_guard();
create trigger nx062_no_delete before delete or truncate on authz.nexloop_machine_keys for each statement execute function control.nexloop_nx022_append_only();

-- C1 for later rows of the other realms: a service credential or browser locator may not take a machine digest or principal.
create function authz.nexloop_machine_realm_exclusion() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if exists(select 1 from authz.nexloop_machine_keys k where k.digest=new.token_digest) then
  raise exception 'authentication kind conflict' using errcode='42501';end if;
 -- new.binding exists only on the service credentials (a nested IF: plpgsql plans the whole expression of one IF).
 if tg_table_name='nexloop_service_credentials' then
  if exists(select 1 from authz.nexloop_machine_clients c
    where c.principal_id in (to_jsonb(new)->'binding'->>'principal_id',to_jsonb(new)->'binding'->>'subject_principal_id')
     or c.subject_id=to_jsonb(new)->'binding'->>'subject_id') then
   raise exception 'authentication kind conflict' using errcode='42501';end if;
 end if;
 return new;
end $$;
alter function authz.nexloop_machine_realm_exclusion() owner to nexloop_owner;
revoke all on function authz.nexloop_machine_realm_exclusion() from public;
create trigger nx062_machine_exclusion before insert or update on authz.nexloop_service_credentials for each row execute function authz.nexloop_machine_realm_exclusion();
create trigger nx062_machine_exclusion before insert or update on authz.nexloop_browser_token_realms for each row execute function authz.nexloop_machine_realm_exclusion();
create function authz.nexloop_machine_membership_exclusion() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if exists(select 1 from authz.nexloop_machine_clients c where c.principal_id=new.principal_id) then
  raise exception 'authentication kind conflict' using errcode='42501';end if;
 return new;
end $$;
alter function authz.nexloop_machine_membership_exclusion() owner to nexloop_owner;
revoke all on function authz.nexloop_machine_membership_exclusion() from public;
create trigger nx062_machine_exclusion before insert or update on control.nexloop_browser_memberships for each row execute function authz.nexloop_machine_membership_exclusion();

do $$declare t text;begin
 foreach t in array array['authz.nexloop_machine_clients','authz.nexloop_machine_keys','control.nexloop_machine_capabilities','control.nexloop_machine_read_verbs'] loop
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy nx062_owner on %s to nexloop_owner using(true) with check(true)',t);
  execute format('revoke all on %s from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator',t);
 end loop;
end $$;

-- C1 read side: the service snapshot refuses machine digests and machine principals -------------------------------------------
alter function authz.nexloop_service_identity_snapshot(text,text) rename to nexloop_service_identity_snapshot_before_machine_v0434;
revoke all on function authz.nexloop_service_identity_snapshot_before_machine_v0434(text,text) from public,nexloop_api,nexloop_identity,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler;
create function authz.nexloop_service_identity_snapshot(p_digest text,p_world text) returns jsonb
language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v jsonb;
begin
 if exists(select 1 from authz.nexloop_machine_keys k where k.digest=p_digest) then raise exception 'authentication kind conflict' using errcode='42501';end if;
 v:=authz.nexloop_service_identity_snapshot_before_machine_v0434(p_digest,p_world);
 if v is not null and exists(select 1 from authz.nexloop_machine_clients c
   where c.principal_id=v->'binding'->>'subject_principal_id' or c.subject_id=v->'binding'->>'subject_id') then
  raise exception 'authentication kind conflict' using errcode='42501';end if;
 return v;
end $$;
alter function authz.nexloop_service_identity_snapshot(text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_service_identity_snapshot(text,text) from public;
grant execute on function authz.nexloop_service_identity_snapshot(text,text) to nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler;

-- C2: lookup by prefix, one HMAC by the API, validity at clock_timestamp() --------------------------------------------------
create function authz.nexloop_machine_key_pepper_version(p_prefix text) returns integer
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select k.pepper_version from authz.nexloop_machine_keys k where p_prefix~'^[a-z2-7]{12}$' and k.prefix_id=p_prefix
$$;
alter function authz.nexloop_machine_key_pepper_version(text) owner to nexloop_owner;
revoke all on function authz.nexloop_machine_key_pepper_version(text) from public;
grant execute on function authz.nexloop_machine_key_pepper_version(text) to nexloop_api;

create function authz.nexloop_machine_identity_snapshot(p_prefix text,p_digest text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare k authz.nexloop_machine_keys;c authz.nexloop_machine_clients;now_ timestamptz:=clock_timestamp();
begin
 select * into k from authz.nexloop_machine_keys where p_prefix~'^[a-z2-7]{12}$' and prefix_id=p_prefix;
 if not found or p_digest is null or k.digest is distinct from p_digest or k.status<>'active' or k.expires_at<=now_ then
  raise exception 'machine authentication denied' using errcode='42501';end if;
 select * into c from authz.nexloop_machine_clients where tenant_id=k.tenant_id and client_id=k.client_id;
 if not found or c.status<>'active' then raise exception 'machine authentication denied' using errcode='42501';end if;
 -- C1: never a digest or a principal of the service or browser realm (also refused at write time; checked again here).
 if exists(select 1 from authz.nexloop_service_credentials sc where sc.token_digest=p_digest
    or c.principal_id in (sc.binding->>'principal_id',sc.binding->>'subject_principal_id') or c.subject_id=sc.binding->>'subject_id')
  or exists(select 1 from authz.nexloop_browser_token_realms br where br.token_digest=p_digest)
  or exists(select 1 from control.nexloop_browser_memberships bm where bm.principal_id=c.principal_id) then
  raise exception 'machine authentication denied' using errcode='42501';end if;
 return jsonb_build_object(
  'binding',jsonb_build_object('tenant_id',k.tenant_id,'credential_tenant_id',k.tenant_id,'credential_id',k.key_id::text,'subject_id',c.subject_id,
   'subject_kind','service','subject_principal_id',c.principal_id,'credential_kind','machine_key'),
  'client_id',c.client_id,'key_prefix',k.prefix_id,'world',k.world,'scope',to_jsonb(k.scope),
  'expires_at',to_jsonb(k.expires_at),'checked_at',to_jsonb(now_));
end $$;
alter function authz.nexloop_machine_identity_snapshot(text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_machine_identity_snapshot(text,text) from public;
grant execute on function authz.nexloop_machine_identity_snapshot(text,text) to nexloop_api;

-- Lifecycle (owner functions, granted to no role; M2b puts them behind the governed entry) --------------------------------
create function authz.nexloop_machine_client_register(p_tenant text,p_client text,p_display text,p_purpose text,p_by text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 insert into authz.nexloop_machine_clients(tenant_id,client_id,principal_id,subject_id,display_name,purpose,status,issued_by_principal)
  values(p_tenant,p_client,'nexloop-machine:'||p_client,'nexloop-machine-subject:'||p_client,p_display,p_purpose,'active',p_by);
 return jsonb_build_object('client_id',p_client,'principal_id','nexloop-machine:'||p_client,'status','active');
end $$;

create function authz.nexloop_machine_client_set_status(p_tenant text,p_client text,p_status text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare revoked integer:=0;
begin
 if p_status not in ('active','inactive') then raise exception 'machine client status invalid' using errcode='22023';end if;
 update authz.nexloop_machine_clients set status=p_status,status_changed_at=clock_timestamp() where tenant_id=p_tenant and client_id=p_client;
 if not found then raise exception 'machine client unavailable' using errcode='42501';end if;
 if p_status='inactive' then
  update authz.nexloop_machine_keys set status='revoked',revoked_at=clock_timestamp(),revoked_reason='client_deactivated'
   where tenant_id=p_tenant and client_id=p_client and status='active';
  get diagnostics revoked=row_count;
 end if;
 return jsonb_build_object('client_id',p_client,'status',p_status,'keys_revoked',revoked);
end $$;

create function authz.nexloop_machine_key_issue(p_tenant text,p_client text,p_key uuid,p_prefix text,p_digest text,p_pepper integer,
  p_world text,p_scope text[],p_expires timestamptz,p_by text,p_request text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 perform 1 from authz.nexloop_machine_clients where tenant_id=p_tenant and client_id=p_client for update;
 insert into authz.nexloop_machine_keys(key_id,tenant_id,client_id,prefix_id,digest,pepper_version,world,scope,expires_at,status,issued_by_principal,issue_request)
  values(p_key,p_tenant,p_client,p_prefix,p_digest,p_pepper,p_world,p_scope,p_expires,'active',p_by,p_request);
 return jsonb_build_object('key_id',p_key,'key_prefix',p_prefix,'world',p_world,'expires_at',p_expires);
end $$;

-- Rotation: the previous key ends within the overlap (at most 7 days, never later than its own expiry), then the new key is issued.
create function authz.nexloop_machine_key_rotate(p_tenant text,p_previous uuid,p_overlap interval,p_key uuid,p_prefix text,p_digest text,
  p_pepper integer,p_scope text[],p_expires timestamptz,p_by text,p_request text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare old authz.nexloop_machine_keys;
begin
 if p_overlap is null or p_overlap<interval '0' or p_overlap>interval '7 days' then raise exception 'machine_key_overlap' using errcode='22023';end if;
 select * into old from authz.nexloop_machine_keys where tenant_id=p_tenant and key_id=p_previous;
 if not found or old.status<>'active' or old.expires_at<=clock_timestamp() then raise exception 'machine key unavailable' using errcode='42501';end if;
 perform 1 from authz.nexloop_machine_clients where tenant_id=p_tenant and client_id=old.client_id for update;
 update authz.nexloop_machine_keys set expires_at=least(expires_at,clock_timestamp()+p_overlap) where key_id=p_previous;
 return authz.nexloop_machine_key_issue(p_tenant,old.client_id,p_key,p_prefix,p_digest,p_pepper,old.world,p_scope,p_expires,p_by,p_request)
  ||jsonb_build_object('previous_key_id',p_previous);
end $$;

create function authz.nexloop_machine_key_revoke(p_tenant text,p_key uuid) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 update authz.nexloop_machine_keys set status='revoked',revoked_at=clock_timestamp(),revoked_reason='revoked'
  where tenant_id=p_tenant and key_id=p_key and status='active';
 if not found then raise exception 'machine key unavailable' using errcode='42501';end if;
 return jsonb_build_object('key_id',p_key,'status','revoked');
end $$;

do $$declare f text;begin
 foreach f in array array['authz.nexloop_machine_client_register(text,text,text,text,text)','authz.nexloop_machine_client_set_status(text,text,text)',
  'authz.nexloop_machine_key_issue(text,text,uuid,text,text,integer,text,text[],timestamptz,text,text)',
  'authz.nexloop_machine_key_rotate(text,uuid,interval,uuid,text,text,integer,text[],timestamptz,text,text)',
  'authz.nexloop_machine_key_revoke(text,uuid)','control.nexloop_machine_capability_current(text)',
  'control.nexloop_machine_capability_forbidden(text)','control.nexloop_machine_never_grant_markers()'] loop
  execute format('alter function %s owner to nexloop_owner',f);
  execute format('revoke all on function %s from public',f);
 end loop;
end $$;
