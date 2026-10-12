-- NX-031 duplicate replies (temporary number 0440; docs/implementation/NX-031-duplicate-replies.md). One inbound message gets at
-- most one reply the customer can see (ADR-023 §2.6), for every consumer, not only under a contact restriction.
--
-- Found by the load lane's duplicate_effects check (200 customers, C4-low / C8): one trigger message with two governed outbound
-- rows provider_accepted or delivered. The dispatch-time one-reply rule (0110 control.nexloop_contact_assert_intent: message
-- lock, then refuse when another reply to the message is dispatching / provider_accepted / delivered / unknown) applied only to
-- a fallback reply or a restricted consumer. Under backlog the reply guarantee's fallback Run answered first (the original
-- reply still persisted, not yet dispatched), and the original Run's reply was dispatched afterwards without any check.
--
-- 1. Function: the outermost control.nexloop_contact_assert_intent (called in the transaction that admits the dispatch,
--    authz.nexloop_assert_intent_dispatch_controls) now applies the rule to every Agent reply bound to an inbound message
--    (a row in runtime.nexloop_outbound_messages, which every bound reply has, original or fallback): the same per-message
--    advisory lock as 0110, then 'inbound message already answered' (NXC05) when another reply to the message is
--    dispatching, provider_accepted, delivered or unknown, or a staff member replied. The refused intent is never dispatched;
--    the executor makes it failed with its outbox (0152 authz.nexloop_fail_refused_effect, reason already_answered), so it
--    stays a governed internal record and is never sent.
-- 2. Constraint: a partial unique index allows at most one reply per inbound message in a state that can reach (or reached)
--    the customer. The function refuses first with a clear code; the index stops any path that would bypass it. An existing
--    database that already holds such a duplicate fails here and the migration rolls back (the duplicates must be reviewed
--    by the owner first; load templates are rebuilt from the catalog).
alter function control.nexloop_contact_assert_intent(text,text,uuid) rename to nexloop_contact_assert_intent_before_one_reply_v0440;
revoke all on function control.nexloop_contact_assert_intent_before_one_reply_v0440(text,text,uuid) from public;
create function control.nexloop_contact_assert_intent(p_tenant text,p_world text,p_intent uuid) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);o runtime.nexloop_outbound_messages;
begin
 perform control.nexloop_contact_assert_intent_before_one_reply_v0440(p_tenant,p_world,p_intent);
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into o from runtime.nexloop_outbound_messages where tenant_id=p_tenant and world=p_world and intent_id=p_intent;
 if found and o.trigger_message_id is not null then
  -- The same key as 0110: an original and a fallback reply to one message admit one after the other.
  perform pg_advisory_xact_lock(hashtextextended(p_tenant||':'||p_world||':reply:'||o.trigger_message_id,0));
  if exists(select 1 from runtime.nexloop_outbound_messages x where x.tenant_id=p_tenant and x.world=p_world
     and x.trigger_message_id=o.trigger_message_id and x.intent_id<>p_intent
     and x.delivery_state in ('dispatching','provider_accepted','delivered','unknown'))
   or runtime.nexloop_staff_replied(p_tenant,p_world,o.trigger_message_id) then
   raise exception 'inbound message already answered' using errcode='NXC05';end if;
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
end $$;
alter function control.nexloop_contact_assert_intent(text,text,uuid) owner to nexloop_owner;
revoke all on function control.nexloop_contact_assert_intent(text,text,uuid) from public;

create unique index nexloop_outbound_messages_one_reply on runtime.nexloop_outbound_messages(tenant_id,world,trigger_message_id)
 where delivery_state in ('dispatching','provider_accepted','delivered','unknown');
