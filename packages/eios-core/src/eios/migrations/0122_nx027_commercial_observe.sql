-- NX-027 (temporary number 0134): the human owner's read of commercial observations, costs and key results
-- (D09: GET /commercial-observations, /costs, /metrics), docs/implementation/NX-027-design.md §7, §9.
--
-- One human-only Action CommercialRecord.observe:1 (capability commercial.observe, deployment configuration
-- business-actions v7; the human grant comes from trusted configuration, never from service-grants). As in 0095
-- (context audit) identity comes first: services, Agents and Run credentials are refused before any grant is evaluated.
-- The projections are the same ones the service read ports return (runtime.nexloop_commercial_view /
-- runtime.nexloop_cost_view, 0130/0132) and the key result is the 0131 computation; every answer names its world and
-- each item its data_mode. Read-only apart from the 0131 cohort freeze that any key result computation performs.

create function authz.nexloop_commercial_observe(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;ident jsonb;v_tenant text;d jsonb;v_result jsonb;v_verb text:=c->>'verb';
begin
 if session_user<>'nexloop_api' or p_world is null or octet_length(p_text)>1048576 or octet_length(p_payload)>4096
  or a->>'protocol' is distinct from 'nexloop-commercial-observe-v1' or a->>'resource_id' is distinct from 'eios:action:CommercialRecord.observe:1'
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds'
  or jsonb_typeof(c) is distinct from 'object'
  or v_verb is null or v_verb not in ('records','record','exceptions','receipts','summary','entries','settlements','metric') then
  raise exception 'commercial observation unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-commercial-observe-v1:'||p_text,'UTF8'),k,'sha256'),'hex') then
  raise exception 'commercial observation unavailable' using errcode='42501';end if;
 -- Identity first: no service, Agent or Run credential reaches the grant check at all.
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ident->'run_context' is distinct from 'null'::jsonb or ident->'binding'->>'subject_kind' is distinct from 'human' then
  raise exception 'human commercial observation authority required' using errcode='42501';end if;
 v_tenant:=ident->'binding'->>'tenant_id';
 if a->>'tenant_id' is distinct from v_tenant or a->>'principal_id' is distinct from ident->'binding'->>'subject_principal_id' then
  raise exception 'commercial observation identity binding mismatch' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 select definition into d from control.nexloop_action_definitions where tenant_id=v_tenant and world=p_world and resource_id=a->>'resource_id' and active;
 if d is null or d is distinct from a->'definition' or d->'capability_binding'->>'capability_name' is distinct from 'commercial.observe'
  or d->'governance'->>'approval_mode' is distinct from 'none' or jsonb_array_length(d->'governance'->'policy_refs')<>0 then
  raise exception 'commercial observation Action contract unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 if v_verb in ('records','record','exceptions','receipts') then v_result:=runtime.nexloop_commercial_view(v_tenant,p_world,c);
 elsif v_verb in ('summary','entries','settlements') then v_result:=runtime.nexloop_cost_view(v_tenant,p_world,c);
 else
  if (select array_agg(x order by x) from jsonb_object_keys(c) x) is distinct from array['goal_id','goal_version','kr_key','verb']
   or coalesce(c->>'goal_id','')!~'^[a-z0-9][a-z0-9._-]{0,127}$' or jsonb_typeof(c->'goal_version') is distinct from 'number'
   or (c->>'goal_version')!~'^[1-9][0-9]{0,8}$' or coalesce(c->>'kr_key','')!~'^[a-z0-9][a-z0-9._-]{0,63}$' then
   raise exception 'commercial observation invalid' using errcode='22023';end if;
  v_result:=authz.nexloop_compute_key_result(p_digest,p_world,c->>'goal_id',(c->>'goal_version')::integer,c->>'kr_key',null);
 end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 if (a->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'commercial observation expired' using errcode='42501';end if;
 return jsonb_build_object('world',p_world)||v_result;
end $$;
alter function authz.nexloop_commercial_observe(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_commercial_observe(text,text,text,text,text) from public;
grant execute on function authz.nexloop_commercial_observe(text,text,text,text,text) to nexloop_api;
