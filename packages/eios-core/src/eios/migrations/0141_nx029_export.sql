-- NX-029 slice 3 (temporary number 0141): governed data export of one Consumer (design §6; AT-003 export part).
--
-- 1. Human Action Consumer.export (registry: consumer.export / export_consumer, human only) records an export request
--    (control.nexloop_data_exports: requester, Consumer, 7-day validity). The export itself is assembled for the
--    requester only, through the requester's own governed reads (object, property and Message READ, ADR-025 member
--    reads, the commercial observe Action): a field or Message the requester cannot read is withheld, never copied in by
--    the server (AT-003). Nothing is stored server side; each download is an audit event without content.
-- 2. authz.nexloop_data_export (nexloop-data-export-v1; EXECUTE on nexloop.consumer.export:1): verbs 'open' (the
--    requester's own unexpired export of a Consumer that is not erasing: Consumer id, property names, its Message ids)
--    and 'record' (download audit: counts and payload digest). Identity first, as 0095/0122: services, Agents and Run
--    credentials are refused before any grant.
-- Published migrations are not edited.

create table control.nexloop_data_exports (
 tenant_id text not null,world text not null,export_id text not null check(export_id~'^[A-Za-z0-9._:-]{1,190}$'),
 consumer_id text not null check(consumer_id~'^[a-f0-9]{64}$'),requested_by text not null,reason text not null check(char_length(reason) between 1 and 500),
 created_at timestamptz not null default clock_timestamp(),expires_at timestamptz not null,downloads integer not null default 0 check(downloads>=0),
 primary key(tenant_id,world,export_id)
);
alter table control.nexloop_data_exports owner to nexloop_owner;
alter table control.nexloop_data_exports enable row level security;
alter table control.nexloop_data_exports force row level security;
create policy tenant_boundary on control.nexloop_data_exports to nexloop_owner using(tenant_id=current_setting('eios.tenant_id',true))
 with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_data_exports from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;
create trigger nx029_no_delete before delete on control.nexloop_data_exports for each row execute function control.nexloop_nx022_append_only();

create function control.nexloop_governed_export_consumer(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','consumer_id','reason'))
  or coalesce(body->>'consumer_id','')!~'^[a-f0-9]{64}$' or length(coalesce(body->>'reason','')) not between 1 and 500 then
  raise exception 'consumer export invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 if not exists(select 1 from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='Consumer' and object_id=body->>'consumer_id')
  or runtime.nexloop_consumer_erasing(p_tenant,p_world,body->>'consumer_id') then raise exception 'consumer unavailable' using errcode='42501';end if;
 insert into control.nexloop_data_exports(tenant_id,world,export_id,consumer_id,requested_by,reason,expires_at)
  values(p_tenant,p_world,p_intent,body->>'consumer_id',p_principal,body->>'reason',clock_timestamp()+interval '7 days') on conflict do nothing;
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('data-export:'||p_intent,'nexloop.consumer.export.requested',p_tenant,p_principal,'data-export',p_intent,
   jsonb_build_object('consumer_id',body->>'consumer_id'),clock_timestamp()) on conflict do nothing;
 return jsonb_build_object('export_id',p_intent,'consumer_id',body->>'consumer_id');
end $$;
alter function control.nexloop_governed_export_consumer(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_governed_export_consumer(text,text,text,text,text,jsonb) from public;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('consumer.export','export_consumer','human','control.nexloop_governed_export_consumer(text,text,text,text,text,jsonb)','NX-029');

create function authz.nexloop_data_export(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
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
   'message_ids',v);
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
alter function authz.nexloop_data_export(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_data_export(text,text,text,text,text) from public;
grant execute on function authz.nexloop_data_export(text,text,text,text,text) to nexloop_api;
