-- NX-056 A5 (docs/design/unified-approvals.md v5 §3, §10; owner 2026-10-22 Q1 option A): the first three policies' preconditions.
-- Temporary number; the dispatcher renumbers.
--
-- 1. consumer.erase_on_request (design §10: "if the two share one capability, split out consumer.erase_on_request first"). The owner's
--    confirmation of a Consumer's own erasure request (source consumer_request, state pending_confirmation, 0132 D1) went through
--    consumer.erase, the same capability as an erasure the owner starts. A policy on consumer.erase would then hold a Consumer's own
--    request for a second person, and no body field can tell the two apart (the handler ignores confirm_request). The new capability
--    only confirms a pending request of the Consumer and then runs the same erasure handler; without one it is refused. consumer.erase
--    is unchanged (it still confirms a pending request too - under its policy).
-- 2. control.nexloop_configure_approval_policies: trusted configuration (session nexloop_configurator only) of
--    deploy/configuration/approval-policies.v1.json. Per policy: a new version is appended (the 0321 guard checks it); the current
--    version given again must be identical (replay); an older version is refused. A capability absent from the manifest keeps its
--    current policy (policies are append-only; there is no removal in this version).

-- 1. consumer.erase_on_request ------------------------------------------------------------------------------------------------------
create function control.nexloop_governed_erase_consumer_on_request(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if jsonb_typeof(body) is distinct from 'object' or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','consumer_id','reason'))
  or coalesce(body->>'consumer_id','')!~'^[a-f0-9]{64}$' or length(coalesce(body->>'reason','')) not between 1 and 500 then
  raise exception 'consumer erasure invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 -- Only a Consumer's own request waiting for the owner's confirmation (0132 D1); locked so it is confirmed once.
 perform 1 from control.nexloop_consumer_erasures where tenant_id=p_tenant and world=p_world and consumer_id=body->>'consumer_id'
  and source='consumer_request' and state='pending_confirmation' for update;
 if not found then raise exception 'consumer erasure: no_pending_consumer_request' using errcode='22023';end if;
 return control.nexloop_governed_erase_consumer(p_tenant,p_world,p_principal,p_subject,p_intent,
  jsonb_build_object('operation','erase_consumer','request_id',body->>'request_id','consumer_id',body->>'consumer_id','reason',body->>'reason'));
end $$;
alter function control.nexloop_governed_erase_consumer_on_request(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_governed_erase_consumer_on_request(text,text,text,text,text,jsonb) from public;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('consumer.erase_on_request','erase_consumer_on_request','human','control.nexloop_governed_erase_consumer_on_request(text,text,text,text,text,jsonb)','NX-056');

-- 2. Trusted configuration of the policies -----------------------------------------------------------------------------------------
create function control.nexloop_configure_approval_policies(p_manifest jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
declare p jsonb;cur control.nexloop_approval_policies;n_new integer:=0;n_same integer:=0;v_by text;
begin
 if session_user<>'nexloop_configurator' then raise exception 'approval policies are trusted configuration only' using errcode='42501';end if;
 if p_manifest->>'schema_version' is distinct from 'nexloop-approval-policies/1' or jsonb_typeof(p_manifest->'version') is distinct from 'number'
  or (p_manifest->>'version')!~'^[1-9][0-9]{0,8}$' or jsonb_typeof(p_manifest->'policies') is distinct from 'array'
  or jsonb_array_length(p_manifest->'policies') not between 1 and 100
  or exists(select 1 from jsonb_object_keys(p_manifest) k where k not in ('schema_version','version','decision','policies')) then
  raise exception 'approval policies invalid' using errcode='22023';end if;
 if (select count(distinct e->>'capability') from jsonb_array_elements(p_manifest->'policies') e)<>jsonb_array_length(p_manifest->'policies') then
  raise exception 'approval policies: one entry per capability' using errcode='22023';end if;
 v_by:='configuration:approval-policies.v'||(p_manifest->>'version');
 for p in select value from jsonb_array_elements(p_manifest->'policies') loop
  if jsonb_typeof(p) is distinct from 'object'
   or not (p ?& array['capability','version','action_version','condition','quorum','approver_roles','expires_after_seconds','summary_fields'])
   or exists(select 1 from jsonb_object_keys(p) k where k not in ('capability','version','action_version','condition','quorum','approver_roles',
     'expires_after_seconds','summary_fields','target_subject_field','purpose'))
   or jsonb_typeof(p->'version') is distinct from 'number' or jsonb_typeof(p->'action_version') is distinct from 'number'
   or jsonb_typeof(p->'quorum') is distinct from 'number' or jsonb_typeof(p->'expires_after_seconds') is distinct from 'number'
   or jsonb_typeof(p->'approver_roles') is distinct from 'array' or jsonb_typeof(p->'summary_fields') is distinct from 'array'
   or jsonb_typeof(p->'condition') not in ('object','null')
   or (p ? 'target_subject_field' and jsonb_typeof(p->'target_subject_field') not in ('string','null')) then
   raise exception 'approval policy invalid: %',left(coalesce(p->>'capability',''),80) using errcode='22023';end if;
  cur:=control.nexloop_approval_policy(p->>'capability');
  if cur.capability is not null and (p->>'version')::integer=cur.version then
   -- The current version again: a replay only if every field is the same.
   if (cur.action_version,cur.condition,cur.quorum,cur.approver_roles,cur.expires_after_seconds,cur.summary_fields,cur.target_subject_field)
     is distinct from ((p->>'action_version')::integer,case when jsonb_typeof(p->'condition')='null' then null else p->'condition' end,(p->>'quorum')::integer,
      array(select jsonb_array_elements_text(p->'approver_roles')),(p->>'expires_after_seconds')::integer,array(select jsonb_array_elements_text(p->'summary_fields')),
      p->>'target_subject_field') then
    raise exception 'approval policy: version % of % changed',cur.version,p->>'capability' using errcode='22023';end if;
   n_same:=n_same+1;
   continue;
  end if;
  -- A new version (the 0321 guard: human governed capability, not approvals.*, versions only grow, bounds).
  insert into control.nexloop_approval_policies(capability,version,action_version,condition,quorum,approver_roles,expires_after_seconds,summary_fields,
    target_subject_field,published_by)
   values(p->>'capability',(p->>'version')::integer,(p->>'action_version')::integer,case when jsonb_typeof(p->'condition')='null' then null else p->'condition' end,
    (p->>'quorum')::integer,array(select jsonb_array_elements_text(p->'approver_roles')),(p->>'expires_after_seconds')::integer,
    array(select jsonb_array_elements_text(p->'summary_fields')),p->>'target_subject_field',v_by);
  n_new:=n_new+1;
 end loop;
 return jsonb_build_object('configured',true,'version',(p_manifest->>'version')::integer,'appended',n_new,'unchanged',n_same);
end $$;
alter function control.nexloop_configure_approval_policies(jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_configure_approval_policies(jsonb) from public;
grant execute on function control.nexloop_configure_approval_policies(jsonb) to nexloop_configurator;
