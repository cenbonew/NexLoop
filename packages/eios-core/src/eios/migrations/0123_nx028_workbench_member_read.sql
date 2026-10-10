-- NX-028 slice 1 follow-up (ADR-025): workbench members read message bodies and Consumer properties by role, every read audited.
--
-- * A new read derivation `workbench-member-v1`, only for an actual browser Human session of the tenant's workbench application
--   whose principal is a current workbench member (0140) and never a customer principal. Derived in SQL at every read from the
--   live session, membership, role and the target; revoking the membership, changing the role or revoking the session ends it on
--   the next request. No per-object grant, no fact set: the role is the basis.
--     owner, operator: every Message (object and fields) of the tenant and world, and every Consumer and its properties except the
--                      groups the owner marked restricted (owner-property-restrictions, `property_group_restriction` facts);
--     reviewer:        only the evidence Messages of Claims that a pending_review candidate definition depends on; ends with the review.
-- * authz.nexloop_assert_read_authority is renamed and kept (the 0107 memo wrapper, logic unchanged) and the new wrapper sends only
--   claims tagged `workbench-member-v1` to the new check; every other claim (customer, service, Agent, Run, configured or derived)
--   goes to the renamed function exactly as before. authz.nexloop_fact_coverage is not involved and unchanged.
-- * Every derived read appends one audit row per object and transaction (runtime.nexloop_workbench_read_audit); if the audit write
--   fails the read fails. The audit is readable only by the workbench owner (authz.nexloop_workbench_audit_read).

create table runtime.nexloop_workbench_read_audit (
 audit_id bigint generated always as identity primary key,tenant_id text not null,world text not null,
 principal_id text not null,role text not null check(role in ('owner','operator','reviewer')),session_id text not null,
 object_kind text not null check(object_kind in ('message','consumer')),target_resource text not null check(target_resource~'^eios:object:(Message|Consumer)/[a-f0-9]{64}$'),
 read_purpose text not null check(read_purpose~'^[a-z][a-z_]{0,63}$'),read_txid bigint not null,read_at timestamptz not null default clock_timestamp(),
 unique(tenant_id,read_txid,principal_id,target_resource)
);
create index nexloop_workbench_read_audit_time on runtime.nexloop_workbench_read_audit(tenant_id,world,read_at desc);
alter table runtime.nexloop_workbench_read_audit owner to nexloop_owner;
alter table runtime.nexloop_workbench_read_audit enable row level security;
alter table runtime.nexloop_workbench_read_audit force row level security;
create policy tenant_boundary on runtime.nexloop_workbench_read_audit to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
create trigger nx028_append_only before update or delete on runtime.nexloop_workbench_read_audit for each row execute function control.nexloop_nx022_append_only();
revoke all on runtime.nexloop_workbench_read_audit from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator;

-- The live workbench member behind a browser session digest, or an error (never a default).
create function authz.nexloop_workbench_member(p_digest text,p_world text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;cfg control.nexloop_workbench_configurations;v_tenant text;v_principal text;v_role text;
begin
 if session_user is distinct from 'nexloop_api' or p_world is distinct from 'real'
  or not exists(select 1 from authz.nexloop_browser_token_realms where token_digest=p_digest) then
  raise exception 'workbench member read unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_browser_identity_snapshot(p_digest,p_world);
 v_tenant:=ident->'binding'->>'tenant_id';v_principal:=ident->'binding'->>'subject_principal_id';
 cfg:=control.nexloop_workbench_current(v_tenant);v_role:=cfg.members->>v_principal;
 if ident->'binding'->>'subject_kind' is distinct from 'human' or cfg.tenant_id is null or ident->>'browser_application_id' is distinct from cfg.application_id
  or v_role is null or control.nexloop_is_customer_principal(v_tenant,v_principal) then
  raise exception 'workbench member read forbidden' using errcode='42501';end if;
 return jsonb_build_object('tenant_id',v_tenant,'principal_id',v_principal,'role',v_role,'identity',ident);
end $$;

-- ADR-025 §2.2: is this target readable for this member's role right now?
create function authz.nexloop_workbench_member_may_read(p_tenant text,p_world text,p_role text,p_target text) returns boolean
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare m text[];v_kind text;v_type text;v_id text;v_field text;o ontology.objects;def jsonb;grp text;restriction jsonb;
begin
 m:=regexp_match(p_target,'^eios:(object|property):(Message|Consumer)/([a-f0-9]{64})(?:/([A-Za-z][A-Za-z0-9_]{0,63}))?$');
 if m is null or (m[1]='object')<>(m[4] is null) then return false;end if;
 v_kind:=m[1];v_type:=m[2];v_id:=m[3];v_field:=m[4];
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into o from ontology.objects where tenant_id=p_tenant and world=p_world and type_name=v_type and object_id=v_id;
 if not found then return false;end if;
 if v_type='Message' then
  if p_role in ('owner','operator') then return true;end if;
  -- reviewer: evidence of a Claim that a definition still pending review depends on.
  return p_role='reviewer' and exists(select 1 from ontology.nexloop_claims c join ontology.nexloop_candidate_definitions x
    on x.tenant_id=c.tenant_id and x.world=c.world and x.status='pending_review' and x.dependent_claims @> array['claim:'||c.claim_id]
   where c.tenant_id=p_tenant and c.world=p_world and c.source_message_id=v_id);
 end if;
 -- Consumer: owner and operator only; a property outside every group, or in an owner-restricted group, is never derived.
 if p_role not in ('owner','operator') then return false;end if;
 if v_field is null then return true;end if;
 select definition into def from ontology.object_type_versions where tenant_id=p_tenant and type_name=v_type and version=o.schema_version;
 grp:=ontology.nexloop_property_group_of(def,v_field);
 select payload into restriction from authz.nexloop_authority_facts where tenant_id=p_tenant and fact_kind='property_group_restriction' and entity_key=array[v_type];
 return grp is not null and not coalesce(restriction->'restricted_groups' ? grp,false);
end $$;

create function authz.nexloop_assert_workbench_member_read(p_digest text,p_world text,p_claims jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare w jsonb;ident jsonb;v_target text:=p_claims->>'target_resource';v_object text;
begin
 w:=authz.nexloop_workbench_member(p_digest,p_world);ident:=w->'identity';
 if p_claims->>'derivation' is distinct from 'workbench-member-v1' or p_claims->>'operation' is distinct from 'read'
  or p_claims->>'resource_id' is distinct from v_target or p_claims->>'world' is distinct from p_world
  or p_claims->>'tenant_id' is distinct from w->>'tenant_id' or p_claims->>'principal_id' is distinct from w->>'principal_id'
  or p_claims->>'credential_id' is distinct from ident->'binding'->>'credential_id'
  or p_claims->>'directory_hash' is distinct from ident->>'directory_hash'
  or coalesce(p_claims->>'read_purpose','')!~'^[a-z][a-z_]{0,63}$'
  or p_claims->>'expires_at' is null or (p_claims->>'expires_at')::timestamptz<=clock_timestamp()
  or (p_claims->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'workbench member read claim invalid' using errcode='42501';end if;
 if not authz.nexloop_workbench_member_may_read(w->>'tenant_id',p_world,w->>'role',v_target) then
  raise exception 'workbench member read denied' using errcode='42501';end if;
 v_object:=substring(v_target from '^eios:(?:object|property):((?:Message|Consumer)/[a-f0-9]{64})');
 perform set_config('eios.tenant_id',w->>'tenant_id',true);
 -- One audit row per object and transaction; a failed write fails the read (no exception handler here).
 insert into runtime.nexloop_workbench_read_audit(tenant_id,world,principal_id,role,session_id,object_kind,target_resource,read_purpose,read_txid)
  values(w->>'tenant_id',p_world,w->>'principal_id',w->>'role',ident->'binding'->>'session_id',
   case when v_object like 'Message/%' then 'message' else 'consumer' end,'eios:object:'||v_object,p_claims->>'read_purpose',txid_current())
  on conflict(tenant_id,read_txid,principal_id,target_resource) do nothing;
 return ident->'binding';
end $$;

-- Rename and keep (the 0107 memo wrapper, unchanged); only `workbench-member-v1` claims take the new path.
alter function authz.nexloop_assert_read_authority(text,text,jsonb) rename to nexloop_assert_read_authority_before_workbench_v0144;
revoke all on function authz.nexloop_assert_read_authority_before_workbench_v0144(text,text,jsonb) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator;
create function authz.nexloop_assert_read_authority(p_digest text,p_world text,p_claims jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if p_claims->>'derivation'='workbench-member-v1' then return authz.nexloop_assert_workbench_member_read(p_digest,p_world,p_claims);end if;
 return authz.nexloop_assert_read_authority_before_workbench_v0144(p_digest,p_world,p_claims);
end $$;

-- Which Consumer fields a member may read (names and restricted flags only; the values still go through the derivation above).
create function authz.nexloop_workbench_consumer_fields(p_digest text,p_world text,p_consumer text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare w jsonb:=authz.nexloop_workbench_member(p_digest,p_world);o ontology.objects;def jsonb;
begin
 if w->>'role' not in ('owner','operator') or coalesce(p_consumer,'')!~'^[a-f0-9]{64}$' then raise exception 'workbench member read forbidden' using errcode='42501';end if;
 perform set_config('eios.tenant_id',w->>'tenant_id',true);
 select * into o from ontology.objects where tenant_id=w->>'tenant_id' and world=p_world and type_name='Consumer' and object_id=p_consumer;
 if not found then return null;end if;
 select definition into def from ontology.object_type_versions where tenant_id=o.tenant_id and type_name='Consumer' and version=o.schema_version;
 return coalesce((select jsonb_agg(jsonb_build_object('name',p->>'property_name','present',o.properties ? (p->>'property_name'),
   'readable',authz.nexloop_workbench_member_may_read(o.tenant_id,p_world,w->>'role','eios:property:Consumer/'||p_consumer||'/'||(p->>'property_name')))
   order by p->>'property_name') from jsonb_array_elements(coalesce(def->'properties','[]'::jsonb)) p),'[]'::jsonb);
end $$;

-- ADR-025 §2.3: the audit is readable by the workbench owner only (signed read, nexloop.workbench.read, owner role).
create function authz.nexloop_workbench_audit_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;w jsonb;v_limit integer;
begin
 if octet_length(p_text)>1048576 or octet_length(p_payload)>4096 or a->>'protocol' is distinct from 'nexloop-workbench-audit-v1'
  or a->>'operation' is distinct from 'execute' or a->>'action_resource' is distinct from 'eios:action:nexloop.workbench.read:1'
  or a->>'resource_id' is distinct from 'eios:action:nexloop.workbench.read:1'
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'workbench audit unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-workbench-audit-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'workbench audit unavailable' using errcode='42501';end if;
 w:=authz.nexloop_workbench_member(p_digest,p_world);
 if w->>'role' is distinct from 'owner' or a->>'tenant_id' is distinct from w->>'tenant_id' or a->>'principal_id' is distinct from w->>'principal_id' then
  raise exception 'workbench audit forbidden' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 v_limit:=coalesce((c->>'limit')::integer,100);
 if v_limit not between 1 and 500 or (c ? 'before' and (c->>'before')!~'^[1-9][0-9]{0,18}$') then raise exception 'workbench audit invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',w->>'tenant_id',true);
 return jsonb_build_object('items',coalesce((select jsonb_agg(x.v order by x.audit_id desc) from (select r.audit_id,jsonb_build_object('audit_id',r.audit_id,
   'principal_id',r.principal_id,'role',r.role,'object_kind',r.object_kind,'target_resource',r.target_resource,'read_purpose',r.read_purpose,'read_at',r.read_at) v
  from runtime.nexloop_workbench_read_audit r where r.tenant_id=w->>'tenant_id' and r.world=p_world and (not c ? 'before' or r.audit_id<(c->>'before')::bigint)
  order by r.audit_id desc limit v_limit) x),'[]'::jsonb));
end $$;

do $grants$
declare f text;
begin
 foreach f in array array['authz.nexloop_workbench_member(text,text)','authz.nexloop_workbench_member_may_read(text,text,text,text)',
  'authz.nexloop_assert_workbench_member_read(text,text,jsonb)','authz.nexloop_assert_read_authority(text,text,jsonb)',
  'authz.nexloop_workbench_consumer_fields(text,text,text)','authz.nexloop_workbench_audit_read(text,text,text,text,text)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';
  execute 'revoke all on function '||f||' from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator';
 end loop;
end $grants$;
grant execute on function authz.nexloop_workbench_consumer_fields(text,text,text),authz.nexloop_workbench_audit_read(text,text,text,text,text) to nexloop_api;
