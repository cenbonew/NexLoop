-- NX-043 report Action (X3 review 4.3, dispatcher 2026-10-21): one owner-only governed read of both reports. The migration number lives
-- only in the file name and the catalog; nothing below repeats it.
-- nexloop.experiment.report / read_experiment_report (human, owner only, from real): the tenant and the actor come from the governed
-- entry (the server's context), never from the body. body.kind 'shadow' answers the X3 difference report of one strategy version in
-- one shadow world of the tenant over whole UTC days; 'experiment' answers the X6 result of one experiment of the tenant at a whole
-- UTC day; 'pooled' (0296: the pooled function is created there, the handler resolves it at call time) answers the pooled result of
-- several experiments of one comparison. All are frozen on first read (the same read answers the frozen report afterwards) and hold aggregates only, with the
-- suppression of X6 / T5 (minimum sample; side counts none / some); no Consumer id. Audited.

create function control.nexloop_governed_read_experiment_report(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v jsonb;
begin
 if jsonb_typeof(body) is distinct from 'object' or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200
  or body->>'kind' is null or body->>'kind' not in ('shadow','experiment','pooled')
  or (body->>'kind'='pooled' and (exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','kind','experiment_ids','as_of'))
      or jsonb_typeof(body->'experiment_ids') is distinct from 'array' or jsonb_array_length(body->'experiment_ids') not between 2 and 50
      or exists(select 1 from jsonb_array_elements(body->'experiment_ids') e where jsonb_typeof(e)<>'string' or e#>>'{}'!~'^[a-z][a-z0-9_.-]{0,63}$')
      or jsonb_typeof(body->'as_of') is distinct from 'string'))
  or (body->>'kind'='shadow' and (exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','kind','world','strategy_id','strategy_version','since','until'))
      or coalesce(runtime.nexloop_world_kind(body->>'world'),'')<>'shadow' or coalesce(body->>'strategy_id','')!~'^[a-z][a-z0-9_.-]{0,63}$'
      or jsonb_typeof(body->'strategy_version') is distinct from 'number' or (body->>'strategy_version')!~'^[0-9]{1,9}$'
      or jsonb_typeof(body->'since') is distinct from 'string' or jsonb_typeof(body->'until') is distinct from 'string'))
  or (body->>'kind'='experiment' and (exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','kind','experiment_id','as_of'))
      or coalesce(body->>'experiment_id','')!~'^[a-z][a-z0-9_.-]{0,63}$' or jsonb_typeof(body->'as_of') is distinct from 'string')) then
  raise exception 'experiment report: invalid' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'experiment report: owner_world_only' using errcode='22023';end if;
 begin
  if body->>'kind'='shadow' then
   v:=runtime.nexloop_shadow_report_freeze(p_tenant,body->>'world',body->>'strategy_id',(body->>'strategy_version')::integer,
     (body->>'since')::timestamptz,(body->>'until')::timestamptz,p_principal);
  elsif body->>'kind'='pooled' then
   v:=runtime.nexloop_experiment_pooled_freeze(p_tenant,array(select jsonb_array_elements_text(body->'experiment_ids')),(body->>'as_of')::timestamptz,p_principal);
  else
   v:=runtime.nexloop_experiment_report_freeze(p_tenant,body->>'experiment_id',(body->>'as_of')::timestamptz,p_principal);
  end if;
 exception when invalid_datetime_format or datetime_field_overflow then
  raise exception 'experiment report: invalid' using errcode='22023';
 end;
 perform set_config('eios.tenant_id',p_tenant,true);
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('experiment-report:'||md5(p_tenant||(body->>'request_id')||clock_timestamp()::text),'nexloop.experiment.report_read',p_tenant,p_principal,'experiment_report',p_intent,
   jsonb_build_object('kind',body->>'kind','report_digest',v->>'report_digest'),clock_timestamp());
 return v;
end $$;
alter function control.nexloop_governed_read_experiment_report(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_governed_read_experiment_report(text,text,text,text,text,jsonb) from public;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('experiment.report','read_experiment_report','human','control.nexloop_governed_read_experiment_report(text,text,text,text,text,jsonb)','NX-043');
