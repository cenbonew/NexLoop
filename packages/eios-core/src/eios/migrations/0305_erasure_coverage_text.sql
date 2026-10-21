-- NX-029 / AT-026 erasure coverage, batch 1 (dispatcher 2026-10-21; tests/test_consumer_erasure_coverage_pg.py KNOWN_GAPS): the real
-- tables that hold the Consumer's own words, or an operator's note about them, join the Consumer erasure. The migration number is
-- temporary; it lives only in the file name and the catalog. docs/implementation/erasure-coverage.md has the per-table verdicts.
--
-- 1. When a Consumer's erasure completes (a trigger on control.nexloop_consumer_erasures, as 0296's allocation step; on completion
--    because the purge phases join the conversations and plans by the original id), in that world:
--    control.nexloop_contact_refusal_hits    matched_text ''            consumer_id pseudonym   rule, certainty, revision, time stay
--    control.nexloop_reply_escalations       detail {"erased":true}     consumer_id pseudonym   reason code and time stay
--    control.nexloop_takeovers               reason ''                  consumer_id (and a consumer scope_ref) pseudonym; times stay
--    runtime.nexloop_outreach_sends          body '', body_digest null, control_snapshot {"erased":true} (it names the Consumer)
--                                                                       consumer_id pseudonym   state, purpose, receipts stay
--    runtime.nexloop_marketing_optouts       matched_text null, reason '' consumer_id pseudonym  kind, source, topic, time stay
--    runtime.nexloop_marketing_consents      reason '', evidence_ref the pseudonym (it may name a person: a call or paper reference)
--                                                                       consumer_id pseudonym   kind, source, time stay
--    runtime.nexloop_conversations           consumer_id and principal_id pseudonym (the customer's principal identifies them too),
--                                            idempotency_key a pseudonym per row (L6 review 1.1: every kept message_id /
--                                            conversation_id would otherwise join back to the original id)
--    runtime.nexloop_plans                   consumer_id pseudonym, recipe and control_snapshot {"erased":true} (the plan rows kept the
--                                            original id - the same join - and their content may name the person)
--    One pseudonym per erasure for all of them (rows of one Consumer stay grouped, never linkable to the id). The facts stay - a
--    refusal, an opt-out, a consent, a takeover happened - without the words or the identity (AT-026: deletion reaches every copy).
--    The Consumer's suppression itself is unchanged: the erasure's own contact restriction keeps the original id (0132), so an
--    opt-out turned pseudonymous never re-opens contact. A Consumer retention hold keeps all of it (as the erasure step, 0144).
-- 2. An outreach send still dispatching or unknown is left as it is (as 0138 leaves an unknown effect intent; the erasure's blocking
--    step already refuses any new contact): when it settles, a trigger on the send erases it then (its own fresh pseudonym).
-- 3. The tables' guards admit only this path: their update/delete triggers now ask runtime.nexloop_erasure_permits first (owner,
--    the scope this step opens for that table, erasure values only: '', null, {"erased":true}, pseudonym:<32hex>); everything else
--    is refused exactly as before. New trigger functions (the shared originals are not redefined); the constraints that required a
--    64-hex consumer id or a non-empty note accept the erasure values.

alter table control.nexloop_contact_refusal_hits add constraint nexloop_contact_refusal_hits_consumer_id_check
 check(consumer_id~'^[a-f0-9]{64}$' or consumer_id~'^pseudonym:[0-9a-f]{32}$') not valid;
alter table control.nexloop_reply_escalations add constraint nexloop_reply_escalations_consumer_id_check
 check(consumer_id~'^[a-f0-9]{64}$' or consumer_id~'^pseudonym:[0-9a-f]{32}$') not valid;
alter table control.nexloop_takeovers drop constraint nexloop_takeovers_consumer_id_check,
 add constraint nexloop_takeovers_consumer_id_check check(consumer_id~'^[a-f0-9]{64}$' or consumer_id~'^pseudonym:[0-9a-f]{32}$'),
 drop constraint nexloop_takeovers_scope_ref_check,
 add constraint nexloop_takeovers_scope_ref_check check(scope_ref~'^[a-f0-9]{64}$' or (scope_kind='consumer' and scope_ref~'^pseudonym:[0-9a-f]{32}$')),
 drop constraint nexloop_takeovers_reason_check,
 add constraint nexloop_takeovers_reason_check check(length(reason)<=500 and (length(reason)>=1 or consumer_id~'^pseudonym:'));
alter table runtime.nexloop_marketing_optouts drop constraint nexloop_marketing_optouts_consumer_id_check,
 add constraint nexloop_marketing_optouts_consumer_id_check check(consumer_id~'^[a-f0-9]{64}$' or consumer_id~'^pseudonym:[0-9a-f]{32}$');
alter table runtime.nexloop_outreach_sends alter column body_digest drop not null,
 add constraint nexloop_outreach_sends_erased_digest check(body_digest is not null or consumer_id~'^pseudonym:');
alter table runtime.nexloop_plans drop constraint nexloop_plans_consumer_id_check,
 add constraint nexloop_plans_consumer_id_check check(consumer_id~'^[a-f0-9]{64}$' or consumer_id~'^pseudonym:[0-9a-f]{32}$');
alter table runtime.nexloop_marketing_consents drop constraint nexloop_marketing_consents_consumer_id_check,
 add constraint nexloop_marketing_consents_consumer_id_check check(consumer_id~'^[a-f0-9]{64}$' or consumer_id~'^pseudonym:[0-9a-f]{32}$'),
 drop constraint nexloop_marketing_consents_reason_check,
 add constraint nexloop_marketing_consents_reason_check check(char_length(reason)<=500 and (char_length(reason)>=1 or consumer_id~'^pseudonym:'));

-- 3. Guards: the erasure path first, then the original refusal.
create function control.nexloop_append_only_except_erasure() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if runtime.nexloop_erasure_permits(current_user::text,tg_table_schema||'.'||tg_table_name,tg_op,to_jsonb(old),case when tg_op='UPDATE' then to_jsonb(new) end) then
  if tg_op='DELETE' then return old;end if;
  return new;end if;
 raise exception '% is append-only',tg_table_name using errcode='42501';
end $$;
alter function control.nexloop_append_only_except_erasure() owner to nexloop_owner;
revoke all on function control.nexloop_append_only_except_erasure() from public;
drop trigger nx025_append_only on control.nexloop_contact_refusal_hits;
drop trigger nx025_append_only on control.nexloop_reply_escalations;
create trigger nx025_append_only before update or delete on control.nexloop_contact_refusal_hits for each row execute function control.nexloop_append_only_except_erasure();
create trigger nx025_append_only before update or delete on control.nexloop_reply_escalations for each row execute function control.nexloop_append_only_except_erasure();

create function runtime.nexloop_marketing_events_append_only_except_erasure() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if tg_op='UPDATE' and runtime.nexloop_erasure_permits(current_user::text,tg_table_schema||'.'||tg_table_name,'UPDATE',to_jsonb(old),to_jsonb(new)) then return new;end if;
 raise exception 'marketing consent and opt-out events are append-only' using errcode='42501';
end $$;
alter function runtime.nexloop_marketing_events_append_only_except_erasure() owner to nexloop_owner;
revoke all on function runtime.nexloop_marketing_events_append_only_except_erasure() from public;
drop trigger marketing_events_append_only on runtime.nexloop_marketing_optouts;
drop trigger marketing_events_append_only on runtime.nexloop_marketing_consents;
create trigger marketing_events_append_only before update or delete on runtime.nexloop_marketing_optouts for each row execute function runtime.nexloop_marketing_events_append_only_except_erasure();
create trigger marketing_events_append_only before update or delete on runtime.nexloop_marketing_consents for each row execute function runtime.nexloop_marketing_events_append_only_except_erasure();

-- The takeover and outreach guards: same rules as 0126 and 0238 (copied, not redefined), the erasure update admitted first.
create function control.nexloop_takeover_guard_v2() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if tg_op='UPDATE' and runtime.nexloop_erasure_permits(current_user::text,'control.nexloop_takeovers','UPDATE',to_jsonb(old),to_jsonb(new)) then return new;end if;
 if tg_op='DELETE' or old.ended_at is not null or new.ended_at is null
  or (to_jsonb(new)-'ended_at'-'end_reason'-'ended_by'-'end_intent'-'end_revision') is distinct from (to_jsonb(old)-'ended_at'-'end_reason'-'ended_by'-'end_intent'-'end_revision') then
  raise exception 'takeover record is immutable' using errcode='42501';end if;
 return new;
end $$;
alter function control.nexloop_takeover_guard_v2() owner to nexloop_owner;
revoke all on function control.nexloop_takeover_guard_v2() from public;
drop trigger nx028_takeover_guard on control.nexloop_takeovers;
create trigger nx028_takeover_guard before update or delete on control.nexloop_takeovers for each row execute function control.nexloop_takeover_guard_v2();

create function runtime.nexloop_outreach_send_guard_v2() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
declare v_moving text[]:=array['state','refusal_code','refusal_gates','fence','lease_until','provider_reference','admitted_at','finished_at','updated_at'];
begin
 if tg_op='UPDATE' and runtime.nexloop_erasure_permits(current_user::text,'runtime.nexloop_outreach_sends','UPDATE',to_jsonb(old),to_jsonb(new)) then return new;end if;
 if tg_op='DELETE' then raise exception 'outreach intents are never deleted' using errcode='42501';end if;
 if (to_jsonb(new)-v_moving) is distinct from (to_jsonb(old)-v_moving)
  or (old.state in ('fulfilled','failed','refused') and (new.state,new.fence) is distinct from (old.state,old.fence)) then
  raise exception 'outreach intent is immutable' using errcode='42501';end if;
 return new;
end $$;
alter function runtime.nexloop_outreach_send_guard_v2() owner to nexloop_owner;
revoke all on function runtime.nexloop_outreach_send_guard_v2() from public;
drop trigger outreach_send_guard on runtime.nexloop_outreach_sends;
create trigger outreach_send_guard before update or delete on runtime.nexloop_outreach_sends for each row execute function runtime.nexloop_outreach_send_guard_v2();

-- 1. The propagation step.
create function control.nexloop_consumer_text_erasure() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);v_p text:='pseudonym:'||encode(extensions.gen_random_bytes(16),'hex');
 v_ref text:='consumer-erasure:'||new.request_id;n jsonb:='{}'::jsonb;c integer;
begin
 -- On completion (L6 review 1.1): the purge phases before it find the Consumer's messages and plans through the conversations and
 -- plans rows by the original id, so those rows change only now; the six tables change with them, all under one pseudonym.
 if new.state<>'completed' or old.state='completed' then return null;end if;
 perform set_config('eios.tenant_id',new.tenant_id,true);
 -- A Consumer retention hold keeps everything (the erasure step completes with the hold recorded, 0144): nothing here either.
 if runtime.nexloop_retention_held(new.tenant_id,new.world,'consumer',new.consumer_id) then
  perform set_config('eios.tenant_id',coalesce(v_prior,''),true);return null;end if;
 perform runtime.nexloop_erasure_open('control.nexloop_contact_refusal_hits',array['consumer_id','matched_text'],false,v_ref);
 update control.nexloop_contact_refusal_hits set consumer_id=v_p,matched_text='' where tenant_id=new.tenant_id and world=new.world and consumer_id=new.consumer_id;
 get diagnostics c=row_count;n:=n||jsonb_build_object('refusal_hits',c);
 perform runtime.nexloop_erasure_open('control.nexloop_reply_escalations',array['consumer_id','detail'],false,v_ref);
 update control.nexloop_reply_escalations set consumer_id=v_p,detail='{"erased":true}'::jsonb where tenant_id=new.tenant_id and world=new.world and consumer_id=new.consumer_id;
 get diagnostics c=row_count;n:=n||jsonb_build_object('reply_escalations',c);
 perform runtime.nexloop_erasure_open('control.nexloop_takeovers',array['consumer_id','scope_ref','reason'],false,v_ref);
 update control.nexloop_takeovers set consumer_id=v_p,reason='',scope_ref=case when scope_kind='consumer' then v_p else scope_ref end
  where tenant_id=new.tenant_id and world=new.world and consumer_id=new.consumer_id;
 get diagnostics c=row_count;n:=n||jsonb_build_object('takeovers',c);
 perform runtime.nexloop_erasure_open('runtime.nexloop_outreach_sends',array['consumer_id','body','body_digest','control_snapshot'],false,v_ref);
 update runtime.nexloop_outreach_sends set consumer_id=v_p,body='',body_digest=null,control_snapshot='{"erased":true}'::jsonb where tenant_id=new.tenant_id and world=new.world and consumer_id=new.consumer_id
  and state not in ('dispatching','unknown');
 get diagnostics c=row_count;n:=n||jsonb_build_object('outreach_sends',c);
 perform runtime.nexloop_erasure_open('runtime.nexloop_marketing_optouts',array['consumer_id','matched_text','reason'],false,v_ref);
 update runtime.nexloop_marketing_optouts set consumer_id=v_p,matched_text=null,reason=case when reason is null then null else '' end
  where tenant_id=new.tenant_id and world=new.world and consumer_id=new.consumer_id;
 get diagnostics c=row_count;n:=n||jsonb_build_object('marketing_optouts',c);
 perform runtime.nexloop_erasure_open('runtime.nexloop_marketing_consents',array['consumer_id','reason','evidence_ref'],false,v_ref);
 update runtime.nexloop_marketing_consents set consumer_id=v_p,reason='',evidence_ref=case when evidence_ref is null then null else v_p end
  where tenant_id=new.tenant_id and world=new.world and consumer_id=new.consumer_id;
 get diagnostics c=row_count;n:=n||jsonb_build_object('marketing_consents',c);
 -- The conversations and plans (L6 review 1.1): the rows every kept message, conversation and plan id would join back through.
 perform runtime.nexloop_erasure_open('runtime.nexloop_plans',array['consumer_id','recipe','control_snapshot'],false,v_ref);
 update runtime.nexloop_plans set consumer_id=v_p,recipe='{"erased":true}'::jsonb,control_snapshot='{"erased":true}'::jsonb where tenant_id=new.tenant_id and world=new.world and consumer_id=new.consumer_id;
 get diagnostics c=row_count;n:=n||jsonb_build_object('plans',c);
 update runtime.nexloop_conversations set consumer_id=v_p,principal_id=v_p,idempotency_key='pseudonym:'||encode(extensions.gen_random_bytes(16),'hex')
  where tenant_id=new.tenant_id and world=new.world and consumer_id=new.consumer_id;
 get diagnostics c=row_count;n:=n||jsonb_build_object('conversations',c);
 -- Only the scopes opened here are closed: the erasure step may hold others open in the same transaction.
 delete from control.nexloop_erasure_scope where txid=txid_current() and table_name in ('control.nexloop_contact_refusal_hits','control.nexloop_reply_escalations',
  'control.nexloop_takeovers','runtime.nexloop_outreach_sends','runtime.nexloop_marketing_optouts','runtime.nexloop_marketing_consents','runtime.nexloop_plans');
 -- One content-free tombstone, as the relay step (0144): the subject is the erased Consumer, counts per table.
 perform runtime.nexloop_erasure_tombstone(new.tenant_id,new.world,'consumer_erasure',v_ref,'consumer_text','consumer_text',new.consumer_id,'pseudonymized',n,'nexloop_owner');
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return null;
end $$;
alter function control.nexloop_consumer_text_erasure() owner to nexloop_owner;
revoke all on function control.nexloop_consumer_text_erasure() from public;
create trigger zz_nx029_3_consumer_text_erasure after update of state on control.nexloop_consumer_erasures for each row execute function control.nexloop_consumer_text_erasure();

-- 2. A send that was in flight at completion is erased when it settles (the erasure has completed and holds no hold).
create function runtime.nexloop_outreach_late_erasure() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);v_req text;
begin
 if old.state not in ('dispatching','unknown') or new.state in ('dispatching','unknown') or new.consumer_id~'^pseudonym:' then return null;end if;
 perform set_config('eios.tenant_id',new.tenant_id,true);
 select e.request_id into v_req from control.nexloop_consumer_erasures e where e.tenant_id=new.tenant_id and e.world=new.world and e.consumer_id=new.consumer_id
  and e.state='completed';
 if v_req is not null and not runtime.nexloop_retention_held(new.tenant_id,new.world,'consumer',new.consumer_id) then
  perform runtime.nexloop_erasure_open('runtime.nexloop_outreach_sends',array['consumer_id','body','body_digest','control_snapshot'],false,'consumer-erasure:'||v_req);
  update runtime.nexloop_outreach_sends set consumer_id='pseudonym:'||encode(extensions.gen_random_bytes(16),'hex'),body='',body_digest=null,control_snapshot='{"erased":true}'::jsonb
   where tenant_id=new.tenant_id and world=new.world and intent_id=new.intent_id;
  delete from control.nexloop_erasure_scope where txid=txid_current() and table_name='runtime.nexloop_outreach_sends';
  perform runtime.nexloop_erasure_tombstone(new.tenant_id,new.world,'consumer_erasure','consumer-erasure:'||v_req,'consumer_text','consumer_text',new.consumer_id,
   'pseudonymized',jsonb_build_object('outreach_sends',1),'nexloop_owner');
 end if;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return null;
end $$;
alter function runtime.nexloop_outreach_late_erasure() owner to nexloop_owner;
revoke all on function runtime.nexloop_outreach_late_erasure() from public;
create trigger nx029_outreach_late_erasure after update of state on runtime.nexloop_outreach_sends for each row execute function runtime.nexloop_outreach_late_erasure();
