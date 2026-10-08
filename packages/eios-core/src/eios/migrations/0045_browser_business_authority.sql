-- Genuine HUMAN browser authority, independent of API-key authentication.
-- Private bearer-digest locator, no account data or plaintext credential.
create table authz.nexloop_browser_token_realms(token_digest text primary key check(token_digest ~ '^[a-f0-9]{64}$'),tenant_id text not null,session_id text not null unique);
alter table authz.nexloop_browser_token_realms owner to nexloop_owner;
alter table authz.nexloop_browser_token_realms enable row level security;
alter table authz.nexloop_browser_token_realms force row level security;
create policy browser_token_locator_owner on authz.nexloop_browser_token_realms to nexloop_owner using(true) with check(true);
revoke all on authz.nexloop_browser_token_realms from public,nexloop_api,nexloop_identity,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler;
create function authz.nexloop_browser_token_realm_register() returns trigger
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
begin
 insert into authz.nexloop_browser_token_realms values(encode(new.session_token_digest,'hex'),new.tenant_id,new.session_id);
 return new;
end $$;
alter function authz.nexloop_browser_token_realm_register() owner to nexloop_owner;
revoke all on function authz.nexloop_browser_token_realm_register() from public;
create trigger browser_token_realm_register after insert on control.nexloop_browser_sessions
 for each row execute function authz.nexloop_browser_token_realm_register();
do $$declare realm text;begin
 for realm in select t.tenant_id from control.nexloop_tenants t loop
  perform set_config('eios.tenant_id',realm,true);
  insert into authz.nexloop_browser_token_realms select encode(bs.session_token_digest,'hex'),bs.tenant_id,bs.session_id from control.nexloop_browser_sessions bs where bs.tenant_id=realm;
 end loop;
end $$;

create table control.nexloop_browser_business_applications (
 tenant_id text not null,application_id text not null,caller_application_id text not null,application_version text not null,
 requested_scopes jsonb not null check(jsonb_typeof(requested_scopes)='array' and jsonb_array_length(requested_scopes) between 1 and 64),
 primary key(tenant_id,application_id)
);
alter table control.nexloop_browser_business_applications owner to nexloop_owner;
alter table control.nexloop_browser_business_applications enable row level security;
alter table control.nexloop_browser_business_applications force row level security;
create policy browser_business_application_tenant on control.nexloop_browser_business_applications to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_browser_business_applications from public,nexloop_api,nexloop_identity,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler;
create function authz.nexloop_browser_identity_snapshot(p_digest text,p_world text) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on set timezone='UTC' as $$
declare s control.nexloop_browser_sessions;b jsonb;sub jsonb;acc jsonb;mem jsonb;apprev integer;
 locator authz.nexloop_browser_token_realms;config control.nexloop_browser_business_applications;app jsonb;realm control.nexloop_tenants;binding jsonb;deadline timestamptz;now_at timestamptz:=clock_timestamp();
begin
 if session_user<>'nexloop_api' or p_digest is null or p_digest !~ '^[a-f0-9]{64}$' or p_world is distinct from 'real' then
  raise exception 'browser business authority unavailable' using errcode='42501';end if;
 select lr.* into locator from authz.nexloop_browser_token_realms lr where lr.token_digest=p_digest;
 if not found then raise exception 'browser business authority unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',locator.tenant_id,true);
 select * into s from control.nexloop_browser_sessions bs where bs.tenant_id=locator.tenant_id and bs.session_id=locator.session_id and bs.session_token_digest=decode(p_digest,'hex');
 if not found then raise exception 'browser business authority unavailable' using errcode='42501';end if;
 b:=s.payload;
 select t.* into realm from control.nexloop_tenants t where t.tenant_id=s.tenant_id;
 if not found or realm.status<>'active' then raise exception 'browser business realm unavailable' using errcode='42501';end if;
 select ba.revision into apprev from control.nexloop_browser_applications ba where ba.tenant_id=s.tenant_id and ba.application_id=s.application_id and ba.active;
 if not found or apprev::text is distinct from b->>'application_revision' then raise exception 'browser business application stale' using errcode='42501';end if;
 select su.payload into sub from control.nexloop_browser_subjects su where su.subject_id=b->>'subject_id';
 if not found or sub->>'kind' is distinct from 'human' or sub->>'status' is distinct from 'active' or sub->'revision' is distinct from b->'subject_revision' or b->>'subject_kind' is distinct from 'human' then
  raise exception 'browser business subject stale' using errcode='42501';end if;
 select ac.payload into acc from control.nexloop_browser_accounts ac where ac.tenant_id=s.tenant_id and ac.local_account_id=b->>'credential_id' and ac.password_hash is not null;
 if not found or b->>'credential_kind' is distinct from 'local_account' or b->>'credential_tenant_id' is distinct from s.tenant_id
  or acc->>'status' is distinct from 'active' or acc->'subject_id' is distinct from b->'subject_id'
  or acc->'revision' is distinct from b->'credential_revision' or acc->'session_epoch' is distinct from b->'credential_session_epoch'
  or acc->'must_change_password' is distinct from 'false'::jsonb or b->'restricted' is distinct from 'false'::jsonb
  or (acc->>'locked_until')::timestamptz>now_at then raise exception 'browser business account stale' using errcode='42501';end if;
 select bm.payload into mem from control.nexloop_browser_memberships bm where bm.tenant_id=s.tenant_id and bm.principal_id=b->>'principal_id';
 if not found or mem->>'status' is distinct from 'active' or mem->'subject_id' is distinct from b->'subject_id'
  or mem->'revision' is distinct from b->'membership_revision' or mem->>'valid_from' is null
  or (mem->>'valid_from')::timestamptz>now_at or (mem->>'valid_until')::timestamptz<=now_at
  or b->'revoked_at' is distinct from 'null'::jsonb or b->>'idle_expires_at' is null or b->>'absolute_expires_at' is null then
  raise exception 'browser business membership stale' using errcode='42501';end if;
 deadline:=least((b->>'idle_expires_at')::timestamptz,(b->>'absolute_expires_at')::timestamptz);
 if deadline<=now_at then raise exception 'browser business session expired' using errcode='42501';end if;
 select bc.* into config from control.nexloop_browser_business_applications bc where bc.tenant_id=s.tenant_id and bc.application_id=s.application_id;
 if not found then raise exception 'browser business application unpublished' using errcode='42501';end if;
 select af.payload into app from authz.nexloop_authority_facts af where af.tenant_id=s.tenant_id and af.fact_kind='application' and af.entity_key=array[config.caller_application_id,config.application_version];
 if not found or app->>'application_status' is distinct from 'active' or app->>'application_id' is distinct from config.caller_application_id
  or app->>'version' is distinct from config.application_version then raise exception 'browser business application unpublished' using errcode='42501';end if;
 binding:=jsonb_build_object('tenant_id',s.tenant_id,'credential_tenant_id',b->>'credential_tenant_id','credential_id',b->>'credential_id',
  'subject_id',b->>'subject_id','subject_kind','human','subject_principal_id',b->>'principal_id','subject_revision',b->'subject_revision',
  'membership_revision',b->'membership_revision','credential_revision',b->'credential_revision','credential_epoch',b->'credential_session_epoch',
  'caller_application_id',config.caller_application_id,'caller_application_version',config.application_version,'caller_application_digest',app->>'version_digest',
  'requested_scopes',config.requested_scopes,'credential_kind','local_account','session_id',s.session_id,'session_revision',b->'revision');
 return jsonb_build_object('binding',binding,'identity_kind','browser','expires_at',deadline,'absolute_expires_at',b->'absolute_expires_at',
  'directory_hash',encode(sha256(convert_to(jsonb_build_object('session',b,'subject',sub,'account',acc,'membership',mem,'application_revision',apprev,
    'configuration',to_jsonb(config),'tenant_revision',realm.authority_revision,'tenant_status',realm.status)::text,'UTF8')),'hex'),
  'browser_application_id',s.application_id,'agent_invocation',null,'run_context',null);
end $$;
alter function authz.nexloop_browser_identity_snapshot(text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_browser_identity_snapshot(text,text) from public;
grant execute on function authz.nexloop_browser_identity_snapshot(text,text) to nexloop_api;

create function authz.nexloop_browser_authority_fact_snapshot(p_digest text,p_world text,p_kind text,p_key text[]) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare identity jsonb;binding jsonb;value jsonb;body jsonb;principal text;valid boolean:=false;
begin
 identity:=authz.nexloop_browser_identity_snapshot(p_digest,p_world);binding:=identity->'binding';principal:=binding->>'subject_principal_id';
 case p_kind
 when 'subject' then valid:=p_key=array[binding->>'subject_id'];
 when 'membership' then valid:=p_key=array[binding->>'subject_id',principal];
 when 'actor','subject_authority' then valid:=p_key=array[principal];
 when 'browser_authentication' then valid:=p_key=array[binding->>'session_id'];
 when 'application' then valid:=p_key=array[binding->>'caller_application_id',binding->>'caller_application_version'];
 when 'grants' then valid:=cardinality(p_key)=2 and p_key[1]=principal;
 when 'scope','controls','policies' then valid:=cardinality(p_key)=3 and p_key[1]=principal;
 when 'resource_graph' then valid:=cardinality(p_key)=1;
 when 'revision' then valid:=p_key=array['catalog'];
 else valid:=false;end case;
 if valid is distinct from true then raise exception 'browser authority fact binding denied' using errcode='42501';end if;
 if p_kind='subject' then
  select su.payload into body from control.nexloop_browser_subjects su where su.subject_id=binding->>'subject_id';
  value:=jsonb_build_object('tenant_id',binding->>'tenant_id','subject_id',body->>'subject_id','kind',body->>'kind','status',body->>'status','revision',body->'revision','repository_witness','nexloop-postgres-authority-v1');
 elsif p_kind='membership' then
  select bm.payload into body from control.nexloop_browser_memberships bm where bm.tenant_id=binding->>'tenant_id' and bm.principal_id=principal;
  value:=jsonb_build_object('tenant_id',binding->>'tenant_id','subject_id',body->>'subject_id','principal_id',principal,'kind',body->>'kind','status',body->>'status','valid_from',body->'valid_from','valid_until',body->'valid_until','revision',body->'revision','repository_witness','nexloop-postgres-authority-v1');
 elsif p_kind='browser_authentication' then
  value:=binding||jsonb_build_object('status','active','expires_at',identity->'expires_at','absolute_expires_at',identity->'absolute_expires_at','repository_witness','nexloop-postgres-authority-v1');
 else
  select af.payload into value from authz.nexloop_authority_facts af where af.tenant_id=binding->>'tenant_id' and af.fact_kind=p_kind and af.entity_key=p_key;
 end if;
 if value is null then return null;end if;
 if p_kind='actor' and (value->>'kind' is distinct from 'human' or value->>'actor_principal_id' is distinct from principal) then raise exception 'browser actor unavailable' using errcode='42501';end if;
 return jsonb_build_object('payload',value,'record_hash',encode(sha256(convert_to(value::text,'UTF8')),'hex'));
end $$;
alter function authz.nexloop_browser_authority_fact_snapshot(text,text,text,text[]) owner to nexloop_owner;
revoke all on function authz.nexloop_browser_authority_fact_snapshot(text,text,text,text[]) from public;
grant execute on function authz.nexloop_browser_authority_fact_snapshot(text,text,text,text[]) to nexloop_api;

alter function authz.nexloop_service_identity_snapshot(text,text) rename to nexloop_nonbrowser_identity_snapshot_v0044;
revoke all on function authz.nexloop_nonbrowser_identity_snapshot_v0044(text,text) from public,nexloop_api,nexloop_identity,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler;
create function authz.nexloop_service_identity_snapshot(p_digest text,p_world text) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
begin
 if exists(select 1 from authz.nexloop_browser_token_realms br where br.token_digest=p_digest) then
  if exists(select 1 from authz.nexloop_service_credentials sc where sc.token_digest=p_digest) then raise exception 'authentication kind conflict' using errcode='42501';end if;
  return authz.nexloop_browser_identity_snapshot(p_digest,p_world);
 end if;
 return authz.nexloop_nonbrowser_identity_snapshot_v0044(p_digest,p_world);
end $$;
alter function authz.nexloop_service_identity_snapshot(text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_service_identity_snapshot(text,text) from public;
grant execute on function authz.nexloop_service_identity_snapshot(text,text) to nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler;

alter function authz.nexloop_assert_action_authority(text,text,jsonb) rename to nexloop_nonbrowser_assert_action_authority_v0044;
revoke all on function authz.nexloop_nonbrowser_assert_action_authority_v0044(text,text,jsonb) from public,nexloop_api,nexloop_identity,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler;
create function authz.nexloop_assert_action_authority(p_digest text,p_world text,p_claims jsonb) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare identity jsonb;binding jsonb;entry jsonb;keys text[];snap jsonb;kind text;counted integer:=0;
 locator authz.nexloop_browser_token_realms;
begin
 select br.* into locator from authz.nexloop_browser_token_realms br where br.token_digest=p_digest;
 if not found then return authz.nexloop_nonbrowser_assert_action_authority_v0044(p_digest,p_world,p_claims);end if;
 identity:=authz.nexloop_browser_identity_snapshot(p_digest,p_world);binding:=identity->'binding';
 -- Lock live canonical rows through the eventual business commit. No cached
 -- HTTP inspection or model supplied principal/realm is authority.
 perform 1 from control.nexloop_browser_sessions bs where bs.tenant_id=locator.tenant_id and bs.session_id=locator.session_id for share;
 perform 1 from control.nexloop_browser_applications ba where ba.tenant_id=locator.tenant_id and ba.application_id=identity->>'browser_application_id' for share;
 perform 1 from control.nexloop_browser_subjects su where su.subject_id=binding->>'subject_id' for share;
 perform 1 from control.nexloop_browser_accounts ac where ac.tenant_id=locator.tenant_id and ac.local_account_id=binding->>'credential_id' for share;
 perform 1 from control.nexloop_browser_memberships bm where bm.tenant_id=locator.tenant_id and bm.principal_id=binding->>'subject_principal_id' for share;
 perform 1 from control.nexloop_browser_business_applications bc where bc.tenant_id=locator.tenant_id and bc.application_id=identity->>'browser_application_id' for share;
 identity:=authz.nexloop_browser_identity_snapshot(p_digest,p_world);binding:=identity->'binding';
 if p_claims->>'tenant_id' is distinct from binding->>'tenant_id' or p_claims->>'credential_id' is distinct from binding->>'credential_id'
  or p_claims->>'principal_id' is distinct from binding->>'subject_principal_id' or p_claims->>'world' is distinct from p_world
  or p_claims->>'directory_hash' is distinct from identity->>'directory_hash'
  or p_claims->>'resource_id' is distinct from p_claims->>'action_resource' or p_claims->>'operation' is distinct from 'execute'
  or p_claims->>'expires_at' is null or (p_claims->>'expires_at')::timestamptz<=clock_timestamp()
  or jsonb_typeof(p_claims->'facts') is distinct from 'array' or jsonb_array_length(p_claims->'facts')<>12 then
  raise exception 'browser action authority stale' using errcode='42501';end if;
 for entry in select value from jsonb_array_elements(p_claims->'facts') order by value->>'kind',(value->'key')::text loop
  kind:=entry->>'kind';select array_agg(value) into keys from jsonb_array_elements_text(entry->'key');
  if kind in ('actor','application','subject_authority','resource_graph','grants','scope','controls','policies','revision') then
   perform 1 from authz.nexloop_authority_facts af where af.tenant_id=locator.tenant_id and af.fact_kind=kind and af.entity_key=keys for share;
   if not found then raise exception 'browser action authority missing' using errcode='42501';end if;
  elsif kind not in ('subject','membership','browser_authentication') then raise exception 'browser action authority invalid' using errcode='42501';end if;
  snap:=authz.nexloop_browser_authority_fact_snapshot(p_digest,p_world,kind,keys);
  if snap is null or snap->>'record_hash' is distinct from entry->>'record_hash' then raise exception 'browser action authority changed' using errcode='42501';end if;
  counted:=counted+1;
 end loop;
 if (select count(distinct e->>'kind') from jsonb_array_elements(p_claims->'facts') e)<>12 then raise exception 'browser action fact coverage invalid' using errcode='42501';end if;
 perform 1 from control.nexloop_tenants t where t.tenant_id=locator.tenant_id for share;
 identity:=authz.nexloop_browser_identity_snapshot(p_digest,p_world);
 if p_claims->>'directory_hash' is distinct from identity->>'directory_hash' or (p_claims->>'expires_at')::timestamptz<=clock_timestamp() then
  raise exception 'browser action authority expired' using errcode='42501';end if;
 return identity->'binding';
end $$;
alter function authz.nexloop_assert_action_authority(text,text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_assert_action_authority(text,text,jsonb) from public,nexloop_api,nexloop_identity,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler;

-- The old0010 adapter checks before INSERT. A real trigger/lock wait after
-- INSERT must not extend the browser decision, execution permit or claim lease.
alter function authz.nexloop_create_object_action(text,text,text,text,text) rename to nexloop_create_object_action_v0044;
revoke all on function authz.nexloop_create_object_action_v0044(text,text,text,text,text)
 from public,nexloop_api,nexloop_identity,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler;
create function authz.nexloop_create_object_action(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;result jsonb;
begin
 result:=authz.nexloop_create_object_action_v0044(p_digest,p_world,p_text,p_signature,p_payload);
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 if a->'permit'->>'expires_at' is null or (a->'permit'->>'expires_at')::timestamptz<=clock_timestamp()
  or a->'permit'->'claim'->>'lease_expires_at' is null or (a->'permit'->'claim'->>'lease_expires_at')::timestamptz<=clock_timestamp()
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() then
  raise exception 'instance final permit expired' using errcode='42501';end if;
 return result;
end $$;
alter function authz.nexloop_create_object_action(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_create_object_action(text,text,text,text,text) from public;
grant execute on function authz.nexloop_create_object_action(text,text,text,text,text) to nexloop_api,nexloop_domain_worker,nexloop_action_worker;
