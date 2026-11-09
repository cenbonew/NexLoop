-- NX-062 M2b (docs/implementation/NX-062-design.md §1.4, §2.2, §3.6; ADR-041 §2.1-2.2, Q-4): the machine access lifecycle as owner-only
-- governed Actions behind the one governed entry (0124 registry rows only; the entry is not changed). The migration number is temporary;
-- it lives only in the file name and the catalog.
--
-- * Six capabilities, subject rule 'human': machine_client.register / .deactivate / .activate and machine_key.issue / .rotate / .revoke.
--   Each handler also requires the acting principal to be an active workbench owner (control.nexloop_workbench_member_active, owner
--   true), besides the owner-only role grant: an operator, a reviewer, a service, a Run or a machine principal is refused.
-- * The owner session's world is 'real' (owner world only). A key is bound to the world the owner names: 'real' or 'test' (Q-7); a
--   simulation or shadow world never gets a machine key in this slice.
-- * issue and rotate receive the key's prefix_id, HMAC digest and pepper version, drawn by the workbench HTTP layer at submission (the key
--   itself never reaches SQL, a log or the audit); the governed claim makes a replay of the same request return the first outcome, and the
--   HTTP layer shows the key only in a response that executed (replayed false).
-- * Refusals are fixed codes 'machine access: <code>' (22023) checked before the 0434 functions, whose triggers stay the backstop.
-- * Every change writes runtime.audit_events (hence the NX-059 hash chain): ids, prefix, world, scope, expiry and reason codes only - never
--   a digest or a key.
-- * The EIOS authority facts of a machine principal are not written here: the machine fact snapshot and the 0124 machine branch come
--   together in M5 (dispatcher 2026-11-09, design §10 C7).
set local timezone = 'UTC';

create function control.nexloop_machine_access_refuse(p_code text) returns void
 language plpgsql set search_path=pg_catalog,pg_temp as $$
begin raise exception 'machine access: %',p_code using errcode='22023';end $$;

-- Common preconditions: an object body with exactly the allowed keys, a request id, the owner world, an active owner.
create function control.nexloop_machine_access_check(p_tenant text,p_world text,p_principal text,p_subject text,body jsonb,p_keys text[]) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if jsonb_typeof(body) is distinct from 'object' or exists(select 1 from jsonb_object_keys(body) k where k<>all(p_keys||array['operation','request_id']))
  or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200 then
  perform control.nexloop_machine_access_refuse('invalid');end if;
 if p_world is distinct from 'real' then perform control.nexloop_machine_access_refuse('owner_world_only');end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 if p_principal like 'nexloop-machine:%' or p_subject like 'nexloop-machine-subject:%'
  or not control.nexloop_workbench_member_active(p_tenant,p_principal,true) then
  raise exception 'machine access: owner_required' using errcode='42501';end if;
end $$;

create function control.nexloop_machine_access_audit(p_tenant text,p_principal text,p_intent text,p_action text,p_event text,p_payload jsonb) returns void
 language sql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,capability_name,payload,created_at)
  values(p_event,p_action,p_tenant,p_principal,'machine_access',p_intent,p_action,p_payload,clock_timestamp())
$$;

create function control.nexloop_machine_access_scope(body jsonb) returns text[]
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare s text[];x text;
begin
 if jsonb_typeof(body->'scope') is distinct from 'array' or jsonb_array_length(body->'scope') not between 1 and 64
  or exists(select 1 from jsonb_array_elements(body->'scope') e where jsonb_typeof(e)<>'string') then
  perform control.nexloop_machine_access_refuse('invalid');end if;
 select array_agg(value order by value) into s from jsonb_array_elements_text(body->'scope');
 if cardinality(s)<>(select count(distinct v) from unnest(s) v) then perform control.nexloop_machine_access_refuse('invalid');end if;
 foreach x in array s loop
  if not control.nexloop_machine_capability_current(x) then perform control.nexloop_machine_access_refuse('scope_not_allowed');end if;
 end loop;
 return s;
end $$;

create function control.nexloop_machine_access_key_fields(body jsonb) returns void
 language plpgsql stable set search_path=pg_catalog,pg_temp as $$
begin
 if coalesce(body->>'prefix_id','')!~'^[a-z2-7]{12}$' or coalesce(body->>'digest','')!~'^[0-9a-f]{64}$'
  or jsonb_typeof(body->'pepper_version') is distinct from 'number' or (body->>'pepper_version')!~'^[1-9][0-9]{0,8}$'
  or jsonb_typeof(body->'expires_in_days') is distinct from 'number' or (body->>'expires_in_days')!~'^[1-9][0-9]?$'
  or (body->>'expires_in_days')::integer>90 then
  perform control.nexloop_machine_access_refuse('invalid');end if;
end $$;

-- register ---------------------------------------------------------------------------------------------------------------
create function control.nexloop_governed_register_machine_client(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r jsonb;
begin
 perform control.nexloop_machine_access_check(p_tenant,p_world,p_principal,p_subject,body,array['client_id','display_name','purpose']);
 if coalesce(body->>'client_id','')!~'^[a-z][a-z0-9-]{2,62}$' or jsonb_typeof(body->'display_name') is distinct from 'string'
  or char_length(btrim(body->>'display_name')) not between 1 and 120 or jsonb_typeof(body->'purpose') is distinct from 'string'
  or char_length(btrim(body->>'purpose')) not between 1 and 400 then
  perform control.nexloop_machine_access_refuse('invalid');end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 if exists(select 1 from authz.nexloop_machine_clients c where c.client_id=body->>'client_id') then
  perform control.nexloop_machine_access_refuse('client_exists');end if;
 if exists(select 1 from authz.nexloop_service_credentials sc where 'nexloop-machine:'||(body->>'client_id') in (sc.binding->>'principal_id',sc.binding->>'subject_principal_id'))
  or exists(select 1 from control.nexloop_browser_memberships bm where bm.principal_id='nexloop-machine:'||(body->>'client_id')) then
  perform control.nexloop_machine_access_refuse('principal_conflict');end if;
 r:=authz.nexloop_machine_client_register(p_tenant,body->>'client_id',btrim(body->>'display_name'),btrim(body->>'purpose'),p_principal);
 perform control.nexloop_machine_access_audit(p_tenant,p_principal,p_intent,'nexloop.machine_client.register','machine-client-register:'||p_tenant||':'||(body->>'client_id'),
  jsonb_build_object('client_id',body->>'client_id','principal_id',r->>'principal_id'));
 return r;
end $$;

-- deactivate / activate (one handler, two registry rows) ------------------------------------------------------------------
create function control.nexloop_governed_set_machine_client_status(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare c authz.nexloop_machine_clients;v_status text;r jsonb;v_action text;
begin
 perform control.nexloop_machine_access_check(p_tenant,p_world,p_principal,p_subject,body,array['client_id','reason']);
 v_status:=case body->>'operation' when 'deactivate_machine_client' then 'inactive' when 'activate_machine_client' then 'active' end;
 if v_status is null or coalesce(body->>'client_id','')!~'^[a-z][a-z0-9-]{2,62}$' or jsonb_typeof(body->'reason') is distinct from 'string'
  or char_length(btrim(body->>'reason')) not between 1 and 500 then
  perform control.nexloop_machine_access_refuse('invalid');end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into c from authz.nexloop_machine_clients where tenant_id=p_tenant and client_id=body->>'client_id' for update;
 if not found then perform control.nexloop_machine_access_refuse('client_not_found');end if;
 v_action:=case v_status when 'inactive' then 'nexloop.machine_client.deactivate' else 'nexloop.machine_client.activate' end;
 if c.status=v_status then return jsonb_build_object('client_id',c.client_id,'status',c.status,'changed',false);end if;
 r:=authz.nexloop_machine_client_set_status(p_tenant,c.client_id,v_status);
 perform control.nexloop_machine_access_audit(p_tenant,p_principal,p_intent,v_action,'machine-client-status:'||p_tenant||':'||c.client_id||':'||p_intent,
  jsonb_build_object('client_id',c.client_id,'status',v_status,'keys_revoked',r->'keys_revoked'));
 return r||jsonb_build_object('changed',true);
end $$;

-- issue --------------------------------------------------------------------------------------------------------------------
create function control.nexloop_governed_issue_machine_key(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare c authz.nexloop_machine_clients;v_scope text[];v_key uuid:=gen_random_uuid();v_expires timestamptz;r jsonb;active_count integer;
begin
 perform control.nexloop_machine_access_check(p_tenant,p_world,p_principal,p_subject,body,
  array['client_id','world','scope','expires_in_days','prefix_id','digest','pepper_version']);
 perform control.nexloop_machine_access_key_fields(body);
 if coalesce(body->>'client_id','')!~'^[a-z][a-z0-9-]{2,62}$' then perform control.nexloop_machine_access_refuse('invalid');end if;
 if body->>'world' is null or body->>'world' not in ('real','test') then perform control.nexloop_machine_access_refuse('world_not_allowed');end if;
 v_scope:=control.nexloop_machine_access_scope(body);
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into c from authz.nexloop_machine_clients where tenant_id=p_tenant and client_id=body->>'client_id' for update;
 if not found then perform control.nexloop_machine_access_refuse('client_not_found');end if;
 if c.status<>'active' then perform control.nexloop_machine_access_refuse('client_inactive');end if;
 select count(*) into active_count from authz.nexloop_machine_keys k where k.tenant_id=p_tenant and k.client_id=c.client_id and k.status='active' and k.expires_at>clock_timestamp();
 if active_count>0 then perform control.nexloop_machine_access_refuse('rotate_required');end if;
 if exists(select 1 from authz.nexloop_machine_keys k where k.prefix_id=body->>'prefix_id' or k.digest=body->>'digest') then
  perform control.nexloop_machine_access_refuse('key_conflict');end if;
 v_expires:=clock_timestamp()+make_interval(days=>(body->>'expires_in_days')::integer);
 r:=authz.nexloop_machine_key_issue(p_tenant,c.client_id,v_key,body->>'prefix_id',body->>'digest',(body->>'pepper_version')::integer,
  body->>'world',v_scope,v_expires,p_principal,p_intent);
 perform control.nexloop_machine_access_audit(p_tenant,p_principal,p_intent,'nexloop.machine_key.issue','machine-key-issue:'||v_key,
  jsonb_build_object('client_id',c.client_id,'key_id',v_key,'key_prefix',body->>'prefix_id','world',body->>'world','scope',to_jsonb(v_scope),
   'expires_at',to_jsonb(v_expires),'pepper_version',(body->>'pepper_version')::integer));
 return r||jsonb_build_object('client_id',c.client_id,'scope',to_jsonb(v_scope),'replayed',false);
end $$;

-- rotate -------------------------------------------------------------------------------------------------------------------
create function control.nexloop_governed_rotate_machine_key(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare k authz.nexloop_machine_keys;c authz.nexloop_machine_clients;v_scope text[];v_key uuid:=gen_random_uuid();v_expires timestamptz;r jsonb;
begin
 perform control.nexloop_machine_access_check(p_tenant,p_world,p_principal,p_subject,body,
  array['previous_key_id','overlap_days','scope','expires_in_days','prefix_id','digest','pepper_version']);
 perform control.nexloop_machine_access_key_fields(body);
 if coalesce(body->>'previous_key_id','')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
  or jsonb_typeof(body->'overlap_days') is distinct from 'number' or (body->>'overlap_days')!~'^[0-7]$' then
  perform control.nexloop_machine_access_refuse('invalid');end if;
 v_scope:=control.nexloop_machine_access_scope(body);
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into k from authz.nexloop_machine_keys where tenant_id=p_tenant and key_id=(body->>'previous_key_id')::uuid;
 if not found then perform control.nexloop_machine_access_refuse('key_not_found');end if;
 select * into c from authz.nexloop_machine_clients where tenant_id=p_tenant and client_id=k.client_id for update;
 if c.status<>'active' then perform control.nexloop_machine_access_refuse('client_inactive');end if;
 if k.status<>'active' or k.expires_at<=clock_timestamp() then perform control.nexloop_machine_access_refuse('key_not_active');end if;
 if exists(select 1 from authz.nexloop_machine_keys o where o.tenant_id=p_tenant and o.client_id=k.client_id and o.key_id<>k.key_id
   and o.status='active' and o.expires_at>clock_timestamp()) then perform control.nexloop_machine_access_refuse('key_limit');end if;
 if exists(select 1 from authz.nexloop_machine_keys o where o.prefix_id=body->>'prefix_id' or o.digest=body->>'digest') then
  perform control.nexloop_machine_access_refuse('key_conflict');end if;
 v_expires:=clock_timestamp()+make_interval(days=>(body->>'expires_in_days')::integer);
 r:=authz.nexloop_machine_key_rotate(p_tenant,k.key_id,make_interval(days=>(body->>'overlap_days')::integer),v_key,body->>'prefix_id',body->>'digest',
  (body->>'pepper_version')::integer,v_scope,v_expires,p_principal,p_intent);
 perform control.nexloop_machine_access_audit(p_tenant,p_principal,p_intent,'nexloop.machine_key.rotate','machine-key-rotate:'||v_key,
  jsonb_build_object('client_id',k.client_id,'key_id',v_key,'previous_key_id',k.key_id,'key_prefix',body->>'prefix_id',
   'previous_key_prefix',k.prefix_id,'world',k.world,'scope',to_jsonb(v_scope),'expires_at',to_jsonb(v_expires),
   'overlap_days',(body->>'overlap_days')::integer,'pepper_version',(body->>'pepper_version')::integer));
 return r||jsonb_build_object('client_id',k.client_id,'scope',to_jsonb(v_scope),'replayed',false);
end $$;

-- revoke -------------------------------------------------------------------------------------------------------------------
create function control.nexloop_governed_revoke_machine_key(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare k authz.nexloop_machine_keys;
begin
 perform control.nexloop_machine_access_check(p_tenant,p_world,p_principal,p_subject,body,array['key_id','reason']);
 if coalesce(body->>'key_id','')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' or jsonb_typeof(body->'reason') is distinct from 'string'
  or char_length(btrim(body->>'reason')) not between 1 and 500 then
  perform control.nexloop_machine_access_refuse('invalid');end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into k from authz.nexloop_machine_keys where tenant_id=p_tenant and key_id=(body->>'key_id')::uuid for update;
 if not found then perform control.nexloop_machine_access_refuse('key_not_found');end if;
 if k.status<>'active' then return jsonb_build_object('key_id',k.key_id,'status',k.status,'changed',false);end if;
 perform authz.nexloop_machine_key_revoke(p_tenant,k.key_id);
 perform control.nexloop_machine_access_audit(p_tenant,p_principal,p_intent,'nexloop.machine_key.revoke','machine-key-revoke:'||k.key_id,
  jsonb_build_object('client_id',k.client_id,'key_id',k.key_id,'key_prefix',k.prefix_id));
 return jsonb_build_object('key_id',k.key_id,'status','revoked','changed',true);
end $$;

do $$declare f text;begin
 foreach f in array array['control.nexloop_machine_access_refuse(text)','control.nexloop_machine_access_check(text,text,text,text,jsonb,text[])',
  'control.nexloop_machine_access_audit(text,text,text,text,text,jsonb)','control.nexloop_machine_access_scope(jsonb)',
  'control.nexloop_machine_access_key_fields(jsonb)',
  'control.nexloop_governed_register_machine_client(text,text,text,text,text,jsonb)','control.nexloop_governed_set_machine_client_status(text,text,text,text,text,jsonb)',
  'control.nexloop_governed_issue_machine_key(text,text,text,text,text,jsonb)','control.nexloop_governed_rotate_machine_key(text,text,text,text,text,jsonb)',
  'control.nexloop_governed_revoke_machine_key(text,text,text,text,text,jsonb)'] loop
  execute format('alter function %s owner to nexloop_owner',f);
  execute format('revoke all on function %s from public',f);
 end loop;
end $$;

insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('machine_client.register','register_machine_client','human','control.nexloop_governed_register_machine_client(text,text,text,text,text,jsonb)','NX-062'),
 ('machine_client.deactivate','deactivate_machine_client','human','control.nexloop_governed_set_machine_client_status(text,text,text,text,text,jsonb)','NX-062'),
 ('machine_client.activate','activate_machine_client','human','control.nexloop_governed_set_machine_client_status(text,text,text,text,text,jsonb)','NX-062'),
 ('machine_key.issue','issue_machine_key','human','control.nexloop_governed_issue_machine_key(text,text,text,text,text,jsonb)','NX-062'),
 ('machine_key.rotate','rotate_machine_key','human','control.nexloop_governed_rotate_machine_key(text,text,text,text,text,jsonb)','NX-062'),
 ('machine_key.revoke','revoke_machine_key','human','control.nexloop_governed_revoke_machine_key(text,text,text,text,text,jsonb)','NX-062');
