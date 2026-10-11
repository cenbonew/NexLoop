-- Reply reconciliation (temporary number 0210; dispatcher 2026-10-11). AGENTS invariant: an unknown external result is not a
-- failure to blindly retry; query and reconcile first.
-- 1. Reply policy v2 (deploy/configuration/reply-guarantee.v2.json, byte-equal canonical JSON): fallback.reconcile_window_seconds.
-- 2. control.nexloop_reply_reconciles: when the reply worker first found a message's original reply unknown and triggered its
--    reconciliation query (one row per message).
-- 3. authz.nexloop_reply_command is renamed once and wrapped: `state` also reports the original reply's unknown intent, an
--    explicitly failed reply and the reconciliation start; the new verb `reconcile` moves that intent's due time to now on the
--    existing effect query path (as the human request of 0124 does; nothing is resent, state and idempotency key unchanged)
--    and records the start. runtime.nexloop_inbound_answered keeps counting an unknown reply as answered (no second reply is
--    sent while the first one may have arrived); the reply-due item settles only on a reply the channel accepted.
-- 4. authz.nexloop_fail_refused_effect: a never-dispatched bound reply refused under the one-reply rule becomes failed with its
--    outbox; the refusal row (0131) keeps NXC05. Only these refusal reasons are terminal: the inbound message was already
--    answered, its reply window closed, or a fallback reply is bound to another message. Every other NXC05 (contact
--    restriction: outreach, unbound reply, attached notification) stays pending: an owner release (Contact.release) can admit
--    it at the next dispatch. Control refusals (NXC01..NXC04, NXC06) stay pending too: a new Run re-submits the same business
--    intent with a current snapshot (NX-022 reevaluation, never replay).
insert into control.nexloop_reply_policies(version,definition,definition_digest,published_by)
 values(2,'{"decision":"ADR-023 §2.6/§2.7 (dispatcher defaults 2026-10-10): every accepted inbound consumer message gets a reply. A reply is an Agent outbound bound (server-derived trigger_message_id) to that message, same conversation and consumer. Under a contact restriction only such a bound reply may dispatch, at most one per inbound message, within reply_window_seconds of its acceptance. A message still unsettled start_after_seconds after its acceptance starts at most one fallback reply Run (LLM-generated, single bound-reply tool, fixed small budget); if that fails too, the owner is escalated with evidence. Loading refuses a policy where start_after_seconds + the fallback Run''s longest duration is not below reply_window_seconds. Initial values; owner-adjustable by a new version. v2 (dispatcher 2026-10-11, AGENTS: an unknown external result is queried and reconciled first, never blindly retried): when the message''s original reply effect is unknown at its due time, one reconciliation query is triggered on the existing effect query path and the reply stays due for at most reconcile_window_seconds (default 600, at most 3600): delivered settles it, an explicit failure starts the fallback as before, still unknown at the end escalates to the owner (reason reply_unknown) without a fallback. start_after_seconds + reconcile_window_seconds + the fallback Run''s longest duration must stay below the reply window.","fallback":{"batch":10,"context_strategy":"recent_plus_required","lease_seconds":120,"max_attempts":1,"reconcile_window_seconds":600,"run_budget":{"active_timeout_seconds":60,"currency":"USD","maximum_cost":"0.20","maximum_model_turns":2,"maximum_tool_calls":2},"run_ttl_seconds":120,"runtime_profile":"deepseek-flash","start_after_seconds":300},"reply_window_seconds":1800,"schema":"nexloop-reply-guarantee/1","version":2}'::jsonb,'51b583482eaba29187e8003e4ca7602d41a1ae5fbabcf1f6b3d158148a3ddeab','deploy/configuration/reply-guarantee.v2.json');

create table control.nexloop_reply_reconciles (
 tenant_id text not null,world text not null,message_id text not null,intent_id uuid not null,started_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,message_id)
);
alter table control.nexloop_reply_reconciles owner to nexloop_owner;
alter table control.nexloop_reply_reconciles enable row level security;
alter table control.nexloop_reply_reconciles force row level security;
create policy reply_reconcile_tenant on control.nexloop_reply_reconciles to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_reply_reconciles from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator;

alter function authz.nexloop_reply_command(text,text,text,text,text) rename to nexloop_reply_command_before_reconcile_v0210;
revoke all on function authz.nexloop_reply_command_before_reconcile_v0210(text,text,text,text,text) from public;
create function authz.nexloop_reply_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-reply-guarantee-v1','eios:action:nexloop.reply.guarantee:1',
  array['nexloop_domain_worker']);c jsonb:=p_payload::jsonb;v jsonb;v_unknown uuid;r control.nexloop_reply_reconciles;m runtime.nexloop_conversation_messages;
begin
 perform set_config('eios.tenant_id',t,true);
 if c->>'verb'='state' then v:=authz.nexloop_reply_command_before_reconcile_v0210(p_digest,p_world,p_text,p_signature,p_payload);
 elsif c->>'verb'='reconcile' then
  -- The same message check as the original command: an inbound consumer message of this tenant.
  select cm.* into m from runtime.nexloop_conversation_messages cm where cm.tenant_id=t and cm.world=p_world and cm.message_id=c->>'message_id'
   and not (cm.record ? 'direction') and cm.idempotency_key not like 'agent-%';
  if not found then raise exception 'reply message unavailable' using errcode='42501';end if;
  v:=jsonb_build_object('message_id',m.message_id);
 else return authz.nexloop_reply_command_before_reconcile_v0210(p_digest,p_world,p_text,p_signature,p_payload);end if;
 select o.intent_id into v_unknown from runtime.nexloop_outbound_messages o where o.tenant_id=t and o.world=p_world and o.trigger_message_id=v->>'message_id'
  and o.delivery_state='unknown' order by o.intent_id limit 1;
 select * into r from control.nexloop_reply_reconciles x where x.tenant_id=t and x.world=p_world and x.message_id=v->>'message_id';
 if c->>'verb'='state' then
  return v||jsonb_build_object('reply_unknown_intent',v_unknown,
   'reply_failed',exists(select 1 from runtime.nexloop_outbound_messages o where o.tenant_id=t and o.world=p_world and o.trigger_message_id=v->>'message_id'
     and o.delivery_state='failed'),
   'reconcile_started_at',r.started_at);
 end if;
 if v_unknown is null then raise exception 'no unknown reply to reconcile' using errcode='22023';end if;
 update runtime.nexloop_effect_outbox set available_at=least(available_at,clock_timestamp())
  where intent_id=v_unknown and tenant_id=t and world=p_world and state in ('unknown','query_pending');
 insert into control.nexloop_reply_reconciles(tenant_id,world,message_id,intent_id) values(t,p_world,v->>'message_id',v_unknown) on conflict do nothing;
 select * into r from control.nexloop_reply_reconciles x where x.tenant_id=t and x.world=p_world and x.message_id=v->>'message_id';
 return jsonb_build_object('message_id',v->>'message_id','intent_id',v_unknown,'reconcile_started_at',r.started_at);
end $$;
alter function authz.nexloop_reply_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_reply_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_reply_command(text,text,text,text,text) to nexloop_domain_worker;

create function authz.nexloop_fail_refused_effect(p_digest text,p_world text,p_intent uuid,p_code text,p_reason text) returns boolean
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;v_tenant text;v_principal text;i runtime.nexloop_effect_intents;
begin
 if session_user is distinct from 'nexloop_action_worker' or p_code<>'NXC05'
  or p_reason not in ('already_answered','reply_window_closed','fallback_bound_elsewhere') then
  raise exception 'effect refusal unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 v_tenant:=ident->'binding'->>'tenant_id';v_principal:=ident->'binding'->>'subject_principal_id';
 perform set_config('eios.tenant_id',v_tenant,true);
 select * into i from runtime.nexloop_effect_intents x where x.intent_id=p_intent and x.tenant_id=v_tenant and x.world=p_world and x.executor_principal=v_principal for update;
 if not found then raise exception 'effect refusal unavailable' using errcode='42501';end if;
 -- Only a refusal this executor recorded (0131), only a reply bound to an inbound message, and only an intent never
 -- dispatched (no attempt): an attempted intent is reconciled by query, never failed by a refusal.
 if not exists(select 1 from runtime.nexloop_dispatch_refusals r where r.tenant_id=v_tenant and r.world=p_world and r.intent_id=p_intent and r.code=p_code)
  or not exists(select 1 from runtime.nexloop_outbound_messages o where o.tenant_id=v_tenant and o.world=p_world and o.intent_id=p_intent and o.trigger_message_id is not null)
  or i.state<>'accepted' or exists(select 1 from runtime.nexloop_effect_attempts a where a.intent_id=p_intent) then return false;end if;
 -- The already-answered reason is re-derived here, not trusted: another reply to the same message may have arrived.
 if p_reason='already_answered' and not exists(select 1 from runtime.nexloop_outbound_messages o join runtime.nexloop_outbound_messages x
   on x.tenant_id=o.tenant_id and x.world=o.world and x.trigger_message_id=o.trigger_message_id and x.intent_id<>o.intent_id
   where o.tenant_id=v_tenant and o.world=p_world and o.intent_id=p_intent and x.delivery_state in ('dispatching','provider_accepted','delivered','unknown')) then
  return false;end if;
 update runtime.nexloop_effect_outbox set state='failed',lease_until=null,lease_credential=null,lease_principal=null
  where intent_id=p_intent and state in ('pending','leased');
 update runtime.nexloop_effect_intents set state='failed' where intent_id=p_intent;
 return true;
end $$;
alter function authz.nexloop_fail_refused_effect(text,text,uuid,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_fail_refused_effect(text,text,uuid,text,text) from public;
grant execute on function authz.nexloop_fail_refused_effect(text,text,uuid,text,text) to nexloop_action_worker;
