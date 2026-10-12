-- Effect re-query after the origin Run expired, and backoff of unknown re-queries (temporary number 0400; dispatcher
-- 2026-10-12, 500-customer ladder). The effect worker re-queried intents whose origin Run credential had expired: every
-- claim re-derived the Run identity and was refused ('Run authentication denied', about every 0.4 s), the intent stayed
-- unknown, and unknown intents were offered again at once (0042 'unknown': available_at = now). AGENTS: an unknown external
-- result is queried and reconciled first, never blindly retried; nothing here sends anything.
-- 1. authz.nexloop_effect_origin_expired: for the executor's own intent whose origin Run credential has expired (checked here,
--    never taken from the caller; a revocation alone is not enough, it may be undone) and that no dispatch control refuses: an intent never dispatched (state accepted, no attempt) fails
--    with its outbox item (no effect happened; the reply guarantee answers the message as for any failed Run); a dispatched
--    one (dispatching/unknown) stops automatic re-query (available_at = infinity, lease cleared): the reply guarantee's
--    reconciliation (0152) may still ask once and escalates when it stays unknown.
-- 2. authz.nexloop_effect_query_backoff: after an unknown result the executor's own unknown intent is offered again only after
--    min(60, 2^n) seconds, n = its unknown events so far (1 s, 2 s, 4 s ... 60 s).
create function authz.nexloop_effect_origin_expired(p_digest text,p_world text,p_intent uuid) returns text
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;v_tenant text;v_principal text;i runtime.nexloop_effect_intents;v_origin uuid;
begin
 if session_user is distinct from 'nexloop_action_worker' then raise exception 'effect origin unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 v_tenant:=ident->'binding'->>'tenant_id';v_principal:=ident->'binding'->>'subject_principal_id';
 perform set_config('eios.tenant_id',v_tenant,true);
 select * into i from runtime.nexloop_effect_intents x where x.intent_id=p_intent and x.tenant_id=v_tenant and x.world=p_world and x.executor_principal=v_principal for update;
 if not found then raise exception 'effect origin unavailable' using errcode='42501';end if;
 -- The origin is the one the claim uses (0042): the recorded origin_run_id, else the earliest submission's Run.
 v_origin:=coalesce(i.origin_run_id,(select s.run_id from runtime.nexloop_effect_submissions s where s.intent_id=p_intent order by s.created_at,s.run_id limit 1));
 -- Only an expired credential is permanent: a revocation may be undone (a restore revokes every Run credential and the
 -- owner's opening re-issues them), so a revoked origin is left to wait like any other refused admission.
 if v_origin is null or not exists(select 1 from authz.nexloop_run_credentials r where r.run_id=v_origin and r.expires_at<=clock_timestamp()) then
  return 'none';end if;
 -- Dispatch controls come first: while a restore fence, a paused channel, a closed real-dispatch switch or any other
 -- control refuses this intent, it stays blocked exactly as before (the owner's decision), never failed or stopped here.
 begin perform authz.nexloop_assert_intent_dispatch_controls(p_digest,p_world,p_intent);
 exception when others then return 'none';end;
 if i.state='accepted' and not exists(select 1 from runtime.nexloop_effect_attempts a where a.intent_id=p_intent) then
  update runtime.nexloop_effect_outbox set state='failed',lease_until=null,lease_credential=null,lease_principal=null
   where intent_id=p_intent and state in ('pending','leased');
  update runtime.nexloop_effect_intents set state='failed' where intent_id=p_intent;
  return 'failed';
 end if;
 if i.state in ('dispatching','unknown') then
  update runtime.nexloop_effect_outbox set available_at='infinity',lease_until=null,lease_credential=null,lease_principal=null
   where intent_id=p_intent and state not in ('fulfilled','failed');
  return 'stopped';
 end if;
 return 'none';
end $$;
alter function authz.nexloop_effect_origin_expired(text,text,uuid) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_origin_expired(text,text,uuid) from public;
grant execute on function authz.nexloop_effect_origin_expired(text,text,uuid) to nexloop_action_worker;

create function authz.nexloop_effect_query_backoff(p_digest text,p_world text,p_intent uuid) returns integer
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;v_tenant text;v_principal text;n integer;delay integer;
begin
 if session_user is distinct from 'nexloop_action_worker' then raise exception 'effect backoff unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 v_tenant:=ident->'binding'->>'tenant_id';v_principal:=ident->'binding'->>'subject_principal_id';
 perform set_config('eios.tenant_id',v_tenant,true);
 if not exists(select 1 from runtime.nexloop_effect_intents x where x.intent_id=p_intent and x.tenant_id=v_tenant and x.world=p_world
   and x.executor_principal=v_principal and x.state='unknown') then return null;end if;
 select count(*) into n from runtime.nexloop_effect_events e where e.intent_id=p_intent and e.event_kind='unknown';
 delay:=least(60,(2^least(greatest(n-1,0),6))::integer);
 update runtime.nexloop_effect_outbox set available_at=greatest(available_at,clock_timestamp()+make_interval(secs=>delay))
  where intent_id=p_intent and state='unknown' and available_at<>'infinity';
 return delay;
end $$;
alter function authz.nexloop_effect_query_backoff(text,text,uuid) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_query_backoff(text,text,uuid) from public;
grant execute on function authz.nexloop_effect_query_backoff(text,text,uuid) to nexloop_action_worker;

-- 3. authz.nexloop_relay_backlog: the caller's tenant's open message Run tasks (operations queue: pending, retry_wait,
--    running), a number only. The message relay stops claiming while it is at its configured bound, so a Run credential is
--    issued close to its dispatch instead of expiring in the queue (500-customer ladder: Runs failed after 300 s queued).
create function authz.nexloop_relay_backlog(p_digest text,p_world text) returns bigint
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;v_tenant text;n bigint;
begin
 if session_user is distinct from 'nexloop_api' then raise exception 'relay backlog unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ident->'binding'->>'subject_kind' is distinct from 'service' or (ident->'run_context' is not null and ident->'run_context'<>'null'::jsonb) then
  raise exception 'relay backlog unavailable' using errcode='42501';end if;
 v_tenant:=ident->'binding'->>'tenant_id';
 perform set_config('eios.tenant_id',v_tenant,true);
 select count(*) into n from runtime.jobs j where j.tenant_id=v_tenant and j.world=p_world and j.queue='operations' and j.status in ('pending','retry_wait','running');
 return n;
end $$;
alter function authz.nexloop_relay_backlog(text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_relay_backlog(text,text) from public;
grant execute on function authz.nexloop_relay_backlog(text,text) to nexloop_api;
