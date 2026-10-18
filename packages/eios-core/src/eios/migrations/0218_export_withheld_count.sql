-- Walkthrough batch 7 / D2 export exception (owner 2026-10-18: option A). 0217 stopped the workbench field list from saying whether an
-- owner-restricted Consumer property is recorded; the Consumer export (NX-029, AT-003: restricted groups withheld and counted) took its
-- withheld count from that list. The export now counts on its own path: authz.nexloop_data_export `open` (human requester only, the
-- requester's own unexpired export, nexloop.consumer.export:1 authority, download audited) also returns `withheld_present`, the number
-- of recorded Consumer properties the requester's workbench role does not derive (authz.nexloop_workbench_member_may_read), never their
-- names. The 0136 body is copied with that one addition; create or replace keeps owner and grants.
create or replace function authz.nexloop_data_export(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;ident jsonb;v_tenant text;d jsonb;x control.nexloop_data_exports;v jsonb;
begin
 if session_user<>'nexloop_api' or p_world is null or octet_length(p_text)>1048576 or octet_length(p_payload)>4096
  or a->>'protocol' is distinct from 'nexloop-data-export-v1' or a->>'resource_id' is distinct from 'eios:action:nexloop.consumer.export:1'
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds'
  or c->>'verb' not in ('open','record') or coalesce(c->>'export_id','')!~'^[A-Za-z0-9._:-]{1,190}$' then
  raise exception 'data export unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-data-export-v1:'||p_text,'UTF8'),k,'sha256'),'hex') then
  raise exception 'data export unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ident->'run_context' is distinct from 'null'::jsonb or ident->'binding'->>'subject_kind' is distinct from 'human' then
  raise exception 'human data export authority required' using errcode='42501';end if;
 v_tenant:=ident->'binding'->>'tenant_id';
 if a->>'tenant_id' is distinct from v_tenant or a->>'principal_id' is distinct from ident->'binding'->>'subject_principal_id' then
  raise exception 'data export identity binding mismatch' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 perform set_config('eios.tenant_id',v_tenant,true);
 select * into x from control.nexloop_data_exports where tenant_id=v_tenant and world=p_world and export_id=c->>'export_id' for update;
 -- Only the requester's own unexpired export, and never of a Consumer being erased.
 if not found or x.requested_by is distinct from ident->'binding'->>'subject_principal_id' or x.expires_at<=clock_timestamp()
  or runtime.nexloop_consumer_erasing(v_tenant,p_world,x.consumer_id) then raise exception 'data export unavailable' using errcode='42501';end if;
 if c->>'verb'='open' then
  select definition into d from ontology.object_type_versions t join ontology.objects o on o.tenant_id=t.tenant_id and o.type_name=t.type_name and o.schema_version=t.version
   where o.tenant_id=v_tenant and o.world=p_world and o.type_name='Consumer' and o.object_id=x.consumer_id;
  select coalesce(jsonb_agg(m.message_id order by m.conversation_id,m.sequence),'[]'::jsonb) into v from runtime.nexloop_conversation_messages m
   join runtime.nexloop_conversations cv on cv.tenant_id=m.tenant_id and cv.world=m.world and cv.conversation_id=m.conversation_id
   where m.tenant_id=v_tenant and m.world=p_world and cv.consumer_id=x.consumer_id and not (m.record ? 'erased');
  return jsonb_build_object('export_id',x.export_id,'world',p_world,'consumer_id',x.consumer_id,
   'property_names',coalesce((select jsonb_agg(q->>'property_name' order by q->>'property_name') from jsonb_array_elements(coalesce(d->'properties','[]'::jsonb)) q),'[]'::jsonb),
   'message_ids',v,
   -- Owner decision 2026-10-18 (option A): the export's own count of recorded properties the requester's workbench role may not
   -- read (owner-restricted groups), a number only, never which ones; the workbench reads keep hiding presence (0217 / D2).
   'withheld_present',(select count(*) from ontology.objects o,jsonb_array_elements(coalesce(d->'properties','[]'::jsonb)) q
    where o.tenant_id=v_tenant and o.world=p_world and o.type_name='Consumer' and o.object_id=x.consumer_id and o.properties ? (q->>'property_name')
     and not coalesce(authz.nexloop_workbench_member_may_read(v_tenant,p_world,(control.nexloop_workbench_current(v_tenant)).members->>(ident->'binding'->>'subject_principal_id'),
      'eios:property:Consumer/'||x.consumer_id||'/'||(q->>'property_name')),false)));
 end if;
 if jsonb_typeof(c->'counts') is distinct from 'object' or coalesce(c->>'payload_digest','')!~'^[0-9a-f]{64}$' then
  raise exception 'data export record invalid' using errcode='22023';end if;
 update control.nexloop_data_exports set downloads=downloads+1 where tenant_id=v_tenant and world=p_world and export_id=x.export_id;
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('data-export:'||x.export_id||':'||(x.downloads+1),'nexloop.consumer.export.downloaded',v_tenant,ident->'binding'->>'subject_principal_id','data-export',
   x.export_id,jsonb_build_object('consumer_id',x.consumer_id,'counts',c->'counts','payload_digest',c->>'payload_digest'),clock_timestamp());
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 return jsonb_build_object('export_id',x.export_id,'downloads',x.downloads+1);
end $$;
