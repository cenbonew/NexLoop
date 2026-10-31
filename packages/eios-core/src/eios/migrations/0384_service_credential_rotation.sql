-- Service credential rotation (I31, dispatcher 2026-10-31; temporary number, renumbered at merge). Three stage service credentials
-- expire on 2026-11-04 and there was no governed way to renew one: 0050 writes a credential once (on conflict do nothing, then the
-- stored row must equal the manifest), credential_id was unique, and nothing but erasure revoked a service credential.
--   1. credential_id is no longer unique: one credential_id may hold at most two active tokens at a time (the overlap of a rotation).
--      A second row for an existing credential_id is accepted only from control.nexloop_rotate_service_credential (any other insert is
--      refused with 23505 as before, so 0050 behaves as with the unique key); a third active row is refused (23505). Authentication
--      looks a token up by its digest (authz.nexloop_service_identity, 0033) and both rows carry the same binding, so the binding and
--      the authentication fact (keyed by credential_id) are the same whichever token authenticates.
--   2. control.nexloop_rotate_service_credential: the configurator (session user) with the active signing key adds a new token for a
--      service credential whose given current token is its only active one; the new expiry is later than the current one, at least an
--      hour and at most 400 days away. The authentication fact (keyed by credential_id) takes the new expiry: the caller passes it
--      sealed (its snapshot_digest is computed by the EIOS fact model) and it must equal the stored fact but for expires_at and
--      snapshot_digest. Idempotent by request_id.
--   3. control.nexloop_revoke_service_credential: revokes one token by digest when the credential keeps another active token that
--      expires later (the fact already carries that later expiry); a retired principal (no grant left) may lose its last tokens.
--      The authentication fact is not changed: a token without an active row is refused before any fact is read. Idempotent by
--      request_id.
--   4. The restore journal names the revoked token (token_digest) and the replay revokes that token only; entries without it (before
--      this migration) still revoke by credential_id. A rotation itself is not journalled: after a PITR to a point before it, the
--      service holds a token the database does not know (fail closed) and the rotation is run again.
--   5. The metrics snapshot (world real) gains service_credentials for alert rules v25: active, expiring_7d, expired_active and
--      earliest_expires_at over the active service tokens of the world (agents and run credentials not counted).
-- Every write goes through these definer functions; no role gains a table privilege.

-- 1. -------------------------------------------------------------------------------------------------------------------------------
alter table authz.nexloop_service_credentials drop constraint nexloop_service_credentials_credential_id_key;
create index nexloop_service_credentials_credential on authz.nexloop_service_credentials(credential_id);

create function authz.nexloop_service_credential_rows_guard() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
begin
 perform pg_advisory_xact_lock(hashtextextended('nexloop-service-credential:'||new.credential_id,0));
 if tg_op='UPDATE' and new.credential_id is distinct from old.credential_id then
  raise exception 'service credential id is immutable' using errcode='42501';end if;
 if tg_op='INSERT' and exists(select 1 from authz.nexloop_service_credentials c where c.credential_id=new.credential_id and c.token_digest<>new.token_digest)
  and current_setting('nexloop.service_credential_rotation',true) is distinct from new.credential_id||':'||new.token_digest then
  raise exception 'duplicate key value violates unique constraint "nexloop_service_credentials_credential_id_key"' using errcode='23505';end if;
 if new.status='active' and (select count(*) from authz.nexloop_service_credentials c
   where c.credential_id=new.credential_id and c.status='active' and c.token_digest<>new.token_digest)>=2 then
  raise exception 'a service credential holds at most two active tokens' using errcode='23505';end if;
 return new;
end $$;
alter function authz.nexloop_service_credential_rows_guard() owner to nexloop_owner;
revoke all on function authz.nexloop_service_credential_rows_guard() from public;
create trigger nexloop_service_credential_rows before insert or update on authz.nexloop_service_credentials
 for each row execute function authz.nexloop_service_credential_rows_guard();

-- The signing key check of 0050 without its insert: the key must already be the active key under that id.
create function control.nexloop_credential_configurator_check(p_tenant text,p_key_id text,p_key bytea) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
begin
 if session_user<>'nexloop_configurator' or p_tenant is null or p_tenant !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$'
  or p_key_id is null or p_key_id !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$' or p_key is null or octet_length(p_key)<>32
  or not exists(select 1 from authz.nexloop_authority_signing_keys where key_id=p_key_id and key_material=p_key and active)
  or not exists(select 1 from control.nexloop_tenants where tenant_id=p_tenant) then
  raise exception 'credential configuration rejected' using errcode='42501';end if;
end $$;
alter function control.nexloop_credential_configurator_check(text,text,bytea) owner to nexloop_owner;
revoke all on function control.nexloop_credential_configurator_check(text,text,bytea) from public;

-- An idempotent request: the same request_id with the same request and secret returns the recorded result; anything else conflicts.
create function control.nexloop_credential_request_prior(p_tenant text,p_request uuid,p_digest text,p_fingerprint text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior control.nexloop_configuration_publications;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into prior from control.nexloop_configuration_publications where tenant_id=p_tenant and manifest_id=p_request;
 if not found then return null;end if;
 if prior.manifest_digest is distinct from p_digest or prior.secret_fingerprint is distinct from p_fingerprint then
  raise exception 'credential configuration conflict' using errcode='40001';end if;
 return prior.public_manifest->'result'||jsonb_build_object('replayed',true);
end $$;
alter function control.nexloop_credential_request_prior(text,uuid,text,text) owner to nexloop_owner;
revoke all on function control.nexloop_credential_request_prior(text,uuid,text,text) from public;

create function control.nexloop_credential_request_record(p_tenant text,p_request uuid,p_digest text,p_fingerprint text,p_public jsonb) returns bigint
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_revision bigint;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select authority_revision into v_revision from control.nexloop_tenants where tenant_id=p_tenant;
 insert into control.nexloop_configuration_publications values(p_tenant,p_request,p_digest,'nexloop_configurator',clock_timestamp(),v_revision,
  p_public||jsonb_build_object('result',(p_public->'result')||jsonb_build_object('authority_revision',v_revision)),p_fingerprint);
 return v_revision;
end $$;
alter function control.nexloop_credential_request_record(text,uuid,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_credential_request_record(text,uuid,text,text,jsonb) from public;

-- 2. -------------------------------------------------------------------------------------------------------------------------------
-- p_request: {request_id, credential_id, current_digest, expires_at}; p_new_digest: sha256 of the new token (never the token);
-- p_fact: the sealed authentication fact with the new expiry.
create function control.nexloop_rotate_service_credential(p_tenant text,p_request jsonb,p_new_digest text,p_fact jsonb,p_key_id text,p_key bytea) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare v_text text;v_digest text;v_fingerprint text;v_prior jsonb;v_request uuid;v_until timestamptz;cur authz.nexloop_service_credentials;
 v_revision bigint;v_result jsonb;v_fact jsonb;
begin
 perform control.nexloop_credential_configurator_check(p_tenant,p_key_id,p_key);
 if jsonb_typeof(p_request) is distinct from 'object' or (select count(*) from jsonb_object_keys(p_request))<>4
  or exists(select 1 from jsonb_object_keys(p_request) k where k not in ('request_id','credential_id','current_digest','expires_at'))
  or coalesce(p_request->>'request_id','')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
  or coalesce(p_request->>'credential_id','')!~'^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$'
  or coalesce(p_request->>'current_digest','')!~'^[a-f0-9]{64}$' or coalesce(p_new_digest,'')!~'^[a-f0-9]{64}$'
  or p_new_digest=p_request->>'current_digest'
  or coalesce(p_request->>'expires_at','')!~'^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+(Z|[+-][0-9]{2}:[0-9]{2})$' then
  raise exception 'credential rotation rejected' using errcode='22023';end if;
 v_request:=(p_request->>'request_id')::uuid;v_until:=(p_request->>'expires_at')::timestamptz;
 v_text:=p_request::text;v_digest:=encode(sha256(convert_to(v_text,'UTF8')),'hex');
 v_fingerprint:=encode(sha256(p_key||convert_to(p_new_digest,'UTF8')),'hex');
 perform pg_advisory_xact_lock(hashtextextended('nexloop-configure:'||p_tenant,0));
 v_prior:=control.nexloop_credential_request_prior(p_tenant,v_request,v_digest,v_fingerprint);
 if v_prior is not null then return v_prior;end if;
 perform pg_advisory_xact_lock(hashtextextended('nexloop-service-credential:'||(p_request->>'credential_id'),0));
 select payload into v_fact from authz.nexloop_authority_facts where tenant_id=p_tenant and fact_kind='authentication'
  and entity_key=array[p_request->>'credential_id'];
 select * into cur from authz.nexloop_service_credentials where token_digest=p_request->>'current_digest' and tenant_id=p_tenant
  and credential_id=p_request->>'credential_id' and status='active' and binding->>'subject_kind'='service';
 if not found or exists(select 1 from authz.nexloop_service_credentials where credential_id=cur.credential_id and status='active' and token_digest<>cur.token_digest)
  or exists(select 1 from authz.nexloop_service_credentials where token_digest=p_new_digest)
  or v_until<=cur.expires_at or v_until<clock_timestamp()+interval '1 hour' or v_until>clock_timestamp()+interval '400 days'
  or v_fact is null or jsonb_typeof(p_fact) is distinct from 'object' or (p_fact-'expires_at'-'snapshot_digest') is distinct from (v_fact-'expires_at'-'snapshot_digest')
  or p_fact->>'status' is distinct from 'active' or (p_fact->>'expires_at')::timestamptz is distinct from v_until
  or coalesce(p_fact->>'snapshot_digest','')!~'^[a-f0-9]{64}$' then
  raise exception 'credential rotation refused' using errcode='42501';end if;
 perform set_config('nexloop.service_credential_rotation',cur.credential_id||':'||p_new_digest,true);
 insert into authz.nexloop_service_credentials(token_digest,tenant_id,credential_id,binding,worlds,audience,status,expires_at,agent_invocation,sim_use)
  values(p_new_digest,p_tenant,cur.credential_id,cur.binding,cur.worlds,cur.audience,'active',v_until,cur.agent_invocation,cur.sim_use);
 perform set_config('nexloop.service_credential_rotation','',true);
 update authz.nexloop_authority_facts set payload=p_fact where tenant_id=p_tenant and fact_kind='authentication' and entity_key=array[cur.credential_id];
 v_result:=jsonb_build_object('rotated',true,'credential_id',cur.credential_id,'expires_at',v_until,'previous_expires_at',cur.expires_at,'active_tokens',2,
  'replayed',false);
 v_revision:=control.nexloop_credential_request_record(p_tenant,v_request,v_digest,v_fingerprint,
  jsonb_build_object('kind','service_credential_rotation','credential_id',cur.credential_id,'expires_at',v_until,'result',v_result));
 return v_result||jsonb_build_object('authority_revision',v_revision);
end $$;
alter function control.nexloop_rotate_service_credential(text,jsonb,text,jsonb,text,bytea) owner to nexloop_owner;
revoke all on function control.nexloop_rotate_service_credential(text,jsonb,text,jsonb,text,bytea) from public;
grant execute on function control.nexloop_rotate_service_credential(text,jsonb,text,jsonb,text,bytea) to nexloop_configurator;

-- 3. -------------------------------------------------------------------------------------------------------------------------------
-- p_request: {request_id, credential_id, token_digest} revokes that token (another active token of the credential must expire later);
-- {request_id, credential_id, retired: true} revokes every active token of a credential whose principal holds no grant any more.
create function control.nexloop_revoke_service_credential(p_tenant text,p_request jsonb,p_key_id text,p_key bytea) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare v_text text;v_digest text;v_fingerprint text;v_prior jsonb;v_request uuid;v_credential text;v_retired boolean;tgt authz.nexloop_service_credentials;
 v_principal text;n integer;v_left integer;v_revision bigint;v_result jsonb;
begin
 perform control.nexloop_credential_configurator_check(p_tenant,p_key_id,p_key);
 if jsonb_typeof(p_request) is distinct from 'object' or (select count(*) from jsonb_object_keys(p_request))<>3
  or exists(select 1 from jsonb_object_keys(p_request) k where k not in ('request_id','credential_id','token_digest','retired'))
  or coalesce(p_request->>'request_id','')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
  or coalesce(p_request->>'credential_id','')!~'^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$'
  or not (coalesce(p_request->>'token_digest','')~'^[a-f0-9]{64}$' or p_request->'retired'='true'::jsonb) then
  raise exception 'credential revocation rejected' using errcode='22023';end if;
 v_request:=(p_request->>'request_id')::uuid;v_credential:=p_request->>'credential_id';v_retired:=p_request ? 'retired';
 v_text:=p_request::text;v_digest:=encode(sha256(convert_to(v_text,'UTF8')),'hex');
 v_fingerprint:=encode(sha256(p_key||convert_to(coalesce(p_request->>'token_digest','retired'),'UTF8')),'hex');
 perform pg_advisory_xact_lock(hashtextextended('nexloop-configure:'||p_tenant,0));
 v_prior:=control.nexloop_credential_request_prior(p_tenant,v_request,v_digest,v_fingerprint);
 if v_prior is not null then return v_prior;end if;
 perform pg_advisory_xact_lock(hashtextextended('nexloop-service-credential:'||v_credential,0));
 if v_retired then
  select binding->>'subject_principal_id' into v_principal from authz.nexloop_service_credentials
   where tenant_id=p_tenant and credential_id=v_credential and status='active' and binding->>'subject_kind'='service' limit 1;
  if v_principal is null or exists(select 1 from authz.nexloop_authority_facts f where f.tenant_id=p_tenant and f.fact_kind='grants'
    and f.entity_key[1]=v_principal and jsonb_typeof(f.payload->'grants')='array' and jsonb_array_length(f.payload->'grants')>0) then
   raise exception 'credential revocation refused' using errcode='42501';end if;
  update authz.nexloop_service_credentials set status='revoked' where tenant_id=p_tenant and credential_id=v_credential and status='active';
  get diagnostics n=row_count;
 else
  select * into tgt from authz.nexloop_service_credentials where token_digest=p_request->>'token_digest' and tenant_id=p_tenant
   and credential_id=v_credential and status='active' and binding->>'subject_kind'='service';
  if not found or not exists(select 1 from authz.nexloop_service_credentials where credential_id=v_credential and status='active'
    and token_digest<>tgt.token_digest and expires_at>tgt.expires_at) then
   raise exception 'credential revocation refused' using errcode='42501';end if;
  update authz.nexloop_service_credentials set status='revoked' where token_digest=tgt.token_digest;
  n:=1;
 end if;
 select count(*) into v_left from authz.nexloop_service_credentials where credential_id=v_credential and status='active';
 v_result:=jsonb_build_object('revoked',n,'credential_id',v_credential,'active_tokens',v_left,'retired',v_retired,'replayed',false);
 v_revision:=control.nexloop_credential_request_record(p_tenant,v_request,v_digest,v_fingerprint,
  jsonb_build_object('kind','service_credential_revocation','credential_id',v_credential,'result',v_result));
 return v_result||jsonb_build_object('authority_revision',v_revision);
end $$;
alter function control.nexloop_revoke_service_credential(text,jsonb,text,bytea) owner to nexloop_owner;
revoke all on function control.nexloop_revoke_service_credential(text,jsonb,text,bytea) from public;
grant execute on function control.nexloop_revoke_service_credential(text,jsonb,text,bytea) to nexloop_configurator;

-- 4. -------------------------------------------------------------------------------------------------------------------------------
-- As 0146's, with the revoked token's digest.
create or replace function control.nexloop_journal_on_service_credential() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set timezone='UTC' as $$
begin
 if new.status='revoked' and old.status is distinct from 'revoked' then
  perform control.nexloop_restore_journal_append('revocation',jsonb_build_object('subject','service_credential','tenant_id',new.tenant_id,
   'credential_id',new.credential_id,'token_digest',new.token_digest,'revoked_at',clock_timestamp()));
 end if;
 return null;
end $$;

-- As 0352's, revoking the journalled token when the entry names it.
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
   update authz.nexloop_service_credentials set status='revoked' where credential_id=e->>'credential_id' and tenant_id=e->>'tenant_id' and status<>'revoked'
    and (not e ? 'token_digest' or token_digest=e->>'token_digest');
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

-- 5. -------------------------------------------------------------------------------------------------------------------------------
create function runtime.nexloop_service_credential_health(p_tenant text,p_world text,p_now timestamptz) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare prior text:=current_setting('eios.tenant_id',true);v jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select jsonb_build_object('active',count(*),'expiring_7d',count(*) filter (where c.expires_at>p_now and c.expires_at<=p_now+interval '7 days'),
   'expired_active',count(*) filter (where c.expires_at<=p_now),'earliest_expires_at',min(c.expires_at)) into v
  from authz.nexloop_service_credentials c
  where c.tenant_id=p_tenant and c.status='active' and c.binding->>'subject_kind'='service' and p_world=any(c.worlds);
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
alter function runtime.nexloop_service_credential_health(text,text,timestamptz) owner to nexloop_owner;
revoke all on function runtime.nexloop_service_credential_health(text,text,timestamptz) from public;

alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_service_credentials;
revoke all on function runtime.nexloop_metrics_snapshot_before_service_credentials(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp as $$
declare v jsonb:=runtime.nexloop_metrics_snapshot_before_service_credentials(p_tenant,p_world);
begin
 if p_world is distinct from 'real' then return v;end if;
 return v||jsonb_build_object('service_credentials',runtime.nexloop_service_credential_health(p_tenant,p_world,clock_timestamp()));
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;
