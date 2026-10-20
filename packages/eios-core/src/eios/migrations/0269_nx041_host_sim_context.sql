-- NX-041 Host simulation context protocol (L6 nx041-s7b-review 3.4): the database side. The migration number lives only in the
-- file name and the catalog; nothing below repeats it.
-- The simulation group's event intake (nexloop_sim_api, S6c) builds a simulation Run's input from S7b's entry
-- authz.nexloop_sim_context_command with that world's own credential. S6c introduces the simulation roles and
-- authz.nexloop_app_role(p_world); this branch's base does not have them yet, so the change below runs only when S6c is present
-- (at integration S6c comes first): the entry's role check then reads authz.nexloop_app_role(p_world) - a simulation role counts as
-- its real counterpart for a sim:/shadow: world only, real roles exactly as before - and nexloop_sim_api may execute it. The body
-- is 0267_nx041_sim_context.sql's (as of S7b's review fixes; re-copied at integration), otherwise unchanged. Without S6c nothing changes.
do $outer$
begin
 if to_regprocedure('authz.nexloop_app_role(text)') is null then return;end if;
 execute $q$
create or replace function authz.nexloop_sim_context_command(p_digest text,p_world text,p_consumer text,p_limit integer) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text;v_limit integer;v_now text;v_total integer;v_profile jsonb;v_persona record;v_state jsonb:='[]';v_evidence jsonb;
 v_latest runtime.nexloop_sim_consumer_events;v_insufficient jsonb:='[]';
begin
 if authz.nexloop_app_role(p_world) not in ('nexloop_api','nexloop_domain_worker') then raise exception 'sim context: unavailable' using errcode='42501';end if;
 -- Parameters first (review 2.2): a malformed request gets the same answer whatever the credential.
 if coalesce(p_consumer,'')!~'^[0-9a-f]{64}$' or p_limit is null or p_limit not between 1 and 256 then
  raise exception 'sim context: invalid' using errcode='22023';end if;
 v_tenant:=authz.nexloop_sim_effect_tenant(p_digest,p_world);
 perform set_config('eios.tenant_id',v_tenant,true);
 if not runtime.nexloop_sim_consumer_known(v_tenant,p_world,p_consumer) then raise exception 'sim context: consumer_unknown' using errcode='22023';end if;
 v_limit:=p_limit;
 v_now:=runtime.nexloop_commercial_ts(runtime.nexloop_world_now(v_tenant,p_world));
 -- Consumer state: the generated profile and the persona marker, both simulated.
 select o.properties into v_profile from runtime.nexloop_sim_objects o
  where o.tenant_id=v_tenant and o.world=p_world and o.type_name='Consumer' and o.object_id=p_consumer;
 v_state:=v_state||jsonb_build_array(jsonb_build_object('subsection','profile','ref','sim:object:'||p_consumer,
  'revision',substr(encode(sha256(convert_to(v_profile::text,'UTF8')),'hex'),1,16),'content',v_profile,
  'content_hash',encode(sha256(convert_to(v_profile::text,'UTF8')),'hex'),'evidence_kind','synthetic_profile','source','simulated','at',v_now,'tags','[]'::jsonb));
 select p.persona,p.revision into v_persona from control.nexloop_consumer_personas p where p.tenant_id=v_tenant and p.world=p_world and p.consumer_id=p_consumer;
 v_state:=v_state||jsonb_build_array(jsonb_build_object('subsection','persona','ref','sim:persona:'||p_consumer,'revision',v_persona.revision::text,
  'content',jsonb_build_object('persona',v_persona.persona),'content_hash',encode(sha256(convert_to(jsonb_build_object('persona',v_persona.persona)::text,'UTF8')),'hex'),
  'evidence_kind','synthetic_persona','source','simulated','at',v_now,'tags','[]'::jsonb));
 -- Simulated conversation: the newest events up to the limit, oldest first.
 select count(*) into v_total from runtime.nexloop_sim_consumer_events e where e.tenant_id=v_tenant and e.world=p_world and e.consumer_id=p_consumer;
 select * into v_latest from runtime.nexloop_sim_consumer_events e where e.tenant_id=v_tenant and e.world=p_world and e.consumer_id=p_consumer
  order by e.sequence desc limit 1;
 select coalesce(jsonb_agg(jsonb_build_object('subsection','simulated_conversation','ref','sim:event:'||x.event_id,'revision',x.sequence::text,
   -- content_hash is the sha256 of the content given here (its jsonb text), as for consumer_state (review 3.3).
   'content',jsonb_build_object('event_kind',x.event_kind)||x.content,
   'content_hash',encode(sha256(convert_to((jsonb_build_object('event_kind',x.event_kind)||x.content)::text,'UTF8')),'hex'),'evidence_kind','simulated_input',
   'source','simulated','at',runtime.nexloop_commercial_ts(x.sim_time),
   'tags',(case when x.sequence=v_latest.sequence then '["latest_consumer_turn"]'::jsonb else '[]'::jsonb end)
     ||(case when x.event_kind='refuse_contact' then '["contact_limit"]'::jsonb else '[]'::jsonb end)) order by x.sequence),'[]'::jsonb)
  into v_evidence from (select * from runtime.nexloop_sim_consumer_events e where e.tenant_id=v_tenant and e.world=p_world and e.consumer_id=p_consumer
   order by e.sequence desc limit v_limit) x;
 if v_total=0 then v_insufficient:=v_insufficient||jsonb_build_array(jsonb_build_object('code','no_simulated_events','section','current_event'));end if;
 if v_total>v_limit then v_insufficient:=v_insufficient||jsonb_build_array(jsonb_build_object('code','older_events_omitted','section','evidence'));end if;
 return jsonb_build_object('schema_version','nexloop.context-pack.sim.v1','world',p_world,'tenant_id',v_tenant,'consumer_id',p_consumer,
  'data_mode','simulation','world_time',v_now,
  'current_event',case when v_latest.event_id is null then null else jsonb_build_object('kind','simulated_consumer_event','event_id',v_latest.event_id,
    'sequence',v_latest.sequence,'event_kind',v_latest.event_kind,'provenance','sim:event:'||v_latest.event_id,'source','simulated') end,
  'consumer_state',v_state,'evidence',v_evidence,
  'budget_report',jsonb_build_object('event_limit',v_limit,'events_total',v_total,'events_included',jsonb_array_length(v_evidence),
    'omitted',greatest(v_total-jsonb_array_length(v_evidence),0)),
  'insufficient',v_insufficient);
end $$
$q$;
 execute 'grant execute on function authz.nexloop_sim_context_command(text,text,text,integer) to nexloop_sim_api';
end $outer$;
