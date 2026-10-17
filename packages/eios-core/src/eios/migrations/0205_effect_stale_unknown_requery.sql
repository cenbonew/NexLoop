-- Owner-ordered re-query of a stale unknown effect (temporary number 0205; owner 2026-10-18, trial e80e360c follow-up:
-- the 10/16 smoke left two service.request intents unknown whose origin Run credential expired, so 0174 stopped their automatic
-- re-query (available_at = infinity) and 0199 keeps them stopped). AGENTS: an unknown external result is queried and
-- reconciled first, never blindly retried; nothing here sends anything.
-- authz.nexloop_effect_requery_order: the intent's own executor (the effect worker's service credential, as 0174) arms one
-- more query of its stopped unknown intent: state unknown, outbox stopped (available_at infinity), origin Run credential
-- expired, dispatch controls passing, at most 3 orders per intent and one per owner confirmation reference. The order is
-- recorded (runtime.nexloop_effect_requery_orders: who, when, the owner's reference) and the outbox is offered now. The
-- effect worker then takes its existing query-first path: fulfilled is recorded through the receipt reconciliation
-- (0065) and the outbound reply follows (0079); not_found or an unavailable query records unknown again and 0199 stops it
-- (its first unknown is older than 6 hours). No dispatch, no resend, no new intent.
create table runtime.nexloop_effect_requery_orders (
 order_id uuid primary key default gen_random_uuid(),tenant_id text not null,world text not null,intent_id uuid not null,
 confirmation_ref text not null check(confirmation_ref ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),ordered_by text not null,
 ordered_at timestamptz not null default clock_timestamp(),unique(intent_id,confirmation_ref),
 foreign key(intent_id,tenant_id,world) references runtime.nexloop_effect_intents(intent_id,tenant_id,world)
);
alter table runtime.nexloop_effect_requery_orders owner to nexloop_owner;
alter table runtime.nexloop_effect_requery_orders enable row level security;
alter table runtime.nexloop_effect_requery_orders force row level security;
create policy effect_requery_order_tenant on runtime.nexloop_effect_requery_orders to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_effect_requery_orders from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator;

create function authz.nexloop_effect_requery_order(p_digest text,p_world text,p_intent uuid,p_reference text) returns jsonb
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
 -- Owner controls first: while a restore fence, a paused channel or the closed real-dispatch switch refuses it, nothing is armed.
 begin perform authz.nexloop_assert_intent_dispatch_controls(p_digest,p_world,p_intent);
 exception when others then return jsonb_build_object('intent_id',p_intent,'armed',false,'reason','controls_refuse');end;
 if exists(select 1 from runtime.nexloop_effect_requery_orders r where r.intent_id=p_intent and r.confirmation_ref=p_reference) then
  return jsonb_build_object('intent_id',p_intent,'armed',false,'reason','reference_used');end if;
 select count(*) into n from runtime.nexloop_effect_requery_orders r where r.intent_id=p_intent;
 if n>=3 then return jsonb_build_object('intent_id',p_intent,'armed',false,'reason','orders_exhausted');end if;
 insert into runtime.nexloop_effect_requery_orders(tenant_id,world,intent_id,confirmation_ref,ordered_by) values(v_tenant,p_world,p_intent,p_reference,v_principal);
 update runtime.nexloop_effect_outbox set available_at=clock_timestamp(),lease_until=null,lease_credential=null,lease_principal=null where intent_id=p_intent;
 return jsonb_build_object('intent_id',p_intent,'armed',true,'orders',n+1);
end $$;
alter function authz.nexloop_effect_requery_order(text,text,uuid,text) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_requery_order(text,text,uuid,text) from public;
grant execute on function authz.nexloop_effect_requery_order(text,text,uuid,text) to nexloop_action_worker;
