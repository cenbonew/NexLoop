-- Unknown re-query bound (temporary number; 10/16 stage): the 0174 back-off of an unknown intent (min(60, 2^n) s) now also
-- has a maximum automatic query window. Once the intent's first unknown result is older than 6 hours, automatic re-query
-- stops (available_at = infinity, lease untouched), as for an expired origin in 0174. Nothing is sent and nothing is retried
-- blindly: the reply guarantee's reconciliation (0152) may still ask once (it brings available_at back to now) and escalates
-- to the owner when the result stays unknown. Within the window events stay bounded by the back-off: at most one unknown
-- event a minute after the first seven (about 360 in 6 hours).
create or replace function authz.nexloop_effect_query_backoff(p_digest text,p_world text,p_intent uuid) returns integer
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;v_tenant text;v_principal text;n integer;delay integer;first_unknown timestamptz;
begin
 if session_user is distinct from 'nexloop_action_worker' then raise exception 'effect backoff unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 v_tenant:=ident->'binding'->>'tenant_id';v_principal:=ident->'binding'->>'subject_principal_id';
 perform set_config('eios.tenant_id',v_tenant,true);
 if not exists(select 1 from runtime.nexloop_effect_intents x where x.intent_id=p_intent and x.tenant_id=v_tenant and x.world=p_world
   and x.executor_principal=v_principal and x.state='unknown') then return null;end if;
 select count(*),min(e.created_at) into n,first_unknown from runtime.nexloop_effect_events e where e.intent_id=p_intent and e.event_kind='unknown';
 if first_unknown is not null and first_unknown<clock_timestamp()-interval '6 hours' then
  update runtime.nexloop_effect_outbox set available_at='infinity' where intent_id=p_intent and state='unknown';
  return -1;
 end if;
 delay:=least(60,(2^least(greatest(n-1,0),6))::integer);
 update runtime.nexloop_effect_outbox set available_at=greatest(available_at,clock_timestamp()+make_interval(secs=>delay))
  where intent_id=p_intent and state='unknown' and available_at<>'infinity';
 return delay;
end $$;
