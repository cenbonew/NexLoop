-- Requery orders without dispatch controls (temporary number 0206; 10/18 release 13: both owner orders for the 10/16 unknown
-- replies were refused controls_refuse). authz.nexloop_effect_requery_order (latest body: 0205, copied verbatim) no longer runs
-- authz.nexloop_assert_intent_dispatch_controls: an order arms one provider query, never a dispatch, and the query path itself
-- runs no dispatch controls (0042). Every other condition stays: own executor, unknown, stopped, origin expired, one order per
-- owner reference, at most three orders. Nothing here sends anything.
create or replace function authz.nexloop_effect_requery_order(p_digest text,p_world text,p_intent uuid,p_reference text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;v_tenant text;v_principal text;i runtime.nexloop_effect_intents;o runtime.nexloop_effect_outbox;v_origin uuid;n integer;
begin
 if session_user is distinct from 'nexloop_action_worker' then raise exception 'effect requery unavailable' using errcode='42501';end if;
 if p_reference is null or p_reference !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$' then raise exception 'effect requery reference invalid' using errcode='22023';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 v_tenant:=ident->'binding'->>'tenant_id';v_principal:=ident->'binding'->>'subject_principal_id';
 perform set_config('eios.tenant_id',v_tenant,true);
 select * into i from runtime.nexloop_effect_intents x where x.intent_id=p_intent and x.tenant_id=v_tenant and x.world=p_world and x.executor_principal=v_principal for update;
 if not found then raise exception 'effect requery unavailable' using errcode='42501';end if;
 if i.state<>'unknown' then return jsonb_build_object('intent_id',p_intent,'armed',false,'reason','not_unknown','state',i.state);end if;
 select * into o from runtime.nexloop_effect_outbox x where x.intent_id=p_intent for update;
 if not found or o.available_at<>'infinity' then return jsonb_build_object('intent_id',p_intent,'armed',false,'reason','not_stopped');end if;
 -- The same origin and expiry rule as 0174: only a stopped intent whose origin can never authorize anything again.
 v_origin:=coalesce(i.origin_run_id,(select s.run_id from runtime.nexloop_effect_submissions s where s.intent_id=p_intent order by s.created_at,s.run_id limit 1));
 if v_origin is null or not exists(select 1 from authz.nexloop_run_credentials r where r.run_id=v_origin and r.expires_at<=clock_timestamp()) then
  return jsonb_build_object('intent_id',p_intent,'armed',false,'reason','origin_current');end if;
 -- 0206: no dispatch-control check. An order arms a provider QUERY only, which sends nothing; the worker's query path (0042)
 -- never runs dispatch controls either. The 0153 check also read the deployment switch as closed here (its layer is set only
 -- inside the worker's admitting transaction) and its supersession refused every intent older than a control change.
 if exists(select 1 from runtime.nexloop_effect_requery_orders r where r.intent_id=p_intent and r.confirmation_ref=p_reference) then
  return jsonb_build_object('intent_id',p_intent,'armed',false,'reason','reference_used');end if;
 select count(*) into n from runtime.nexloop_effect_requery_orders r where r.intent_id=p_intent;
 if n>=3 then return jsonb_build_object('intent_id',p_intent,'armed',false,'reason','orders_exhausted');end if;
 insert into runtime.nexloop_effect_requery_orders(tenant_id,world,intent_id,confirmation_ref,ordered_by) values(v_tenant,p_world,p_intent,p_reference,v_principal);
 update runtime.nexloop_effect_outbox set available_at=clock_timestamp(),lease_until=null,lease_credential=null,lease_principal=null where intent_id=p_intent;
 return jsonb_build_object('intent_id',p_intent,'armed',true,'orders',n+1);
end $$;
