-- Relay backlog settlement (S42 option A, owner decision 2026-10-30; docs/design/relay-backlog-disposition.md; temporary number,
-- renumbered at merge). An inbound message no relay will ever serve stays pending forever and keeps relay_backlog_age and
-- relay_unenrolled_pending firing. This migration gives it a terminal state with an audit row:
-- * runtime.nexloop_message_outbox.status gains 'settled' (never served, never answered; the reply guarantee is not re-armed).
-- * runtime.nexloop_relay_backlog_settlements: one append-only row per settled message - reason, source (owner or erasure), the
--   acting principal and intent for an owner settlement, the owner's rationale; ids only, no message text and no consumer id.
-- * nexloop.relay.backlog_settle:1 (capability relay.backlog_settle, owner only, governed entry): settles 1-50 pending messages, each
--   of whose Consumer is erasing/erased (reason consumer_erased) or has no active and serviceable relay enrollment now (reason
--   no_serviceable_enrollment). All or nothing; refusal codes (22023, 'relay backlog: <code>'): invalid, real_world_only (42501),
--   not_owner (42501), unknown_message, not_pending, consumer_serviceable. A retry of the same intent replays.
-- * The NX-029 erasure settles the Consumer's pending messages in the transaction that takes it out of pending_confirmation.
alter table runtime.nexloop_message_outbox drop constraint nexloop_message_outbox_status_check;
alter table runtime.nexloop_message_outbox add constraint nexloop_message_outbox_status_check check(status in ('pending','delivered','settled'));

create table runtime.nexloop_relay_backlog_settlements (
 tenant_id text not null,world text not null,message_id text not null,
 reason text not null check(reason in ('consumer_erased','no_serviceable_enrollment')),
 source text not null check(source in ('owner','erasure')),
 actor_principal text,intent_id text,rationale text check(rationale is null or char_length(rationale) between 1 and 500),
 settled_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,message_id),foreign key(tenant_id) references control.nexloop_tenants(tenant_id),
 check((source='owner')=(actor_principal is not null and intent_id is not null and rationale is not null))
);
alter table runtime.nexloop_relay_backlog_settlements owner to nexloop_owner;
alter table runtime.nexloop_relay_backlog_settlements enable row level security;
alter table runtime.nexloop_relay_backlog_settlements force row level security;
create policy tenant_boundary on runtime.nexloop_relay_backlog_settlements to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_relay_backlog_settlements from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,
 nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_stand_in_worker;
create trigger relay_backlog_settlements_append_only before update or delete on runtime.nexloop_relay_backlog_settlements
 for each row execute function control.nexloop_nx022_append_only();
-- Declared for the NX-041 outbound guard: an audit of governed settlement; nothing leaves the system through it, so not guarded.
insert into control.nexloop_real_effect_tables(table_name,guarded,reason) values
 ('runtime.nexloop_relay_backlog_settlements',false,'append-only audit of governed relay backlog settlement (ids only, no message text); no external effect');

-- The Consumer of one outbox message (null when the message or its conversation is unknown).
create function runtime.nexloop_outbox_consumer(p_tenant text,p_world text,p_message text) returns text
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select cv.consumer_id from runtime.nexloop_message_outbox b join runtime.nexloop_conversations cv
  on cv.tenant_id=b.tenant_id and cv.world=b.world and cv.conversation_id=b.conversation_id
 where b.tenant_id=p_tenant and b.world=p_world and b.message_id=p_message $$;
alter function runtime.nexloop_outbox_consumer(text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_outbox_consumer(text,text,text) from public;

create function control.nexloop_governed_relay_backlog_settle(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare prior text:=current_setting('eios.tenant_id',true);v_ids text[];v_id text;v_consumer text;v_status text;v_reason text;
 v_out jsonb:='[]'::jsonb;v_replayed integer:=0;s runtime.nexloop_relay_backlog_settlements;v_text text:=body->>'reason';
begin
 if jsonb_typeof(body) is distinct from 'object' or body->>'operation' is distinct from 'settle_relay_backlog'
  or exists(select 1 from jsonb_object_keys(body) x where x not in ('operation','request_id','message_ids','reason'))
  or jsonb_typeof(body->'message_ids') is distinct from 'array' or jsonb_array_length(body->'message_ids') not between 1 and 50
  or exists(select 1 from jsonb_array_elements(body->'message_ids') m where jsonb_typeof(m) is distinct from 'string' or m#>>'{}'!~'^[a-f0-9]{64}$')
  or (select count(distinct m) from jsonb_array_elements_text(body->'message_ids') m)<>jsonb_array_length(body->'message_ids')
  or jsonb_typeof(body->'reason') is distinct from 'string' or char_length(btrim(v_text)) not between 1 and 500 then
  raise exception 'relay backlog: invalid' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'relay backlog: real_world_only' using errcode='42501';end if;
 if control.nexloop_workbench_role(p_tenant,p_principal) is distinct from 'owner' then raise exception 'relay backlog: not_owner' using errcode='42501';end if;
 v_ids:=array(select jsonb_array_elements_text(body->'message_ids') order by 1);
 perform set_config('eios.tenant_id',p_tenant,true);
 foreach v_id in array v_ids loop
  perform pg_advisory_xact_lock(hashtextextended(p_tenant||':'||p_world||':message-route:'||v_id,0));
  select status into v_status from runtime.nexloop_message_outbox where tenant_id=p_tenant and world=p_world and message_id=v_id for update;
  if not found then raise exception 'relay backlog: unknown_message' using errcode='22023';end if;
  select * into s from runtime.nexloop_relay_backlog_settlements where tenant_id=p_tenant and world=p_world and message_id=v_id;
  if s.message_id is not null and s.intent_id is not distinct from p_intent then
   v_replayed:=v_replayed+1;v_out:=v_out||jsonb_build_object('message_id',v_id,'reason',s.reason);continue;end if;
  if v_status<>'pending' then raise exception 'relay backlog: not_pending' using errcode='22023';end if;
  v_consumer:=runtime.nexloop_outbox_consumer(p_tenant,p_world,v_id);
  v_reason:=case when v_consumer is null or runtime.nexloop_consumer_erasing(p_tenant,p_world,v_consumer) then 'consumer_erased'
   when runtime.nexloop_relay_profile(p_tenant,p_world,v_consumer,null) is null then 'no_serviceable_enrollment' end;
  perform set_config('eios.tenant_id',p_tenant,true);
  if v_reason is null then raise exception 'relay backlog: consumer_serviceable' using errcode='22023';end if;
  update runtime.nexloop_message_outbox set status='settled' where tenant_id=p_tenant and world=p_world and message_id=v_id and status='pending';
  insert into runtime.nexloop_relay_backlog_settlements(tenant_id,world,message_id,reason,source,actor_principal,intent_id,rationale)
   values(p_tenant,p_world,v_id,v_reason,'owner',p_principal,p_intent,btrim(v_text));
  v_out:=v_out||jsonb_build_object('message_id',v_id,'reason',v_reason);
 end loop;
 if v_replayed not in (0,array_length(v_ids,1)) then raise exception 'relay backlog: not_pending' using errcode='22023';end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return jsonb_build_object('settled',v_out,'count',jsonb_array_length(v_out),'replayed',v_replayed>0);
end $$;
alter function control.nexloop_governed_relay_backlog_settle(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_governed_relay_backlog_settle(text,text,text,text,text,jsonb) from public;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('relay.backlog_settle','settle_relay_backlog','human','control.nexloop_governed_relay_backlog_settle(text,text,text,text,text,jsonb)','NX-030');

-- The erasure settles the Consumer's pending messages when it leaves pending_confirmation (same transaction).
create function runtime.nexloop_relay_backlog_settle_on_erasure() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);
begin
 if new.state='pending_confirmation' or (tg_op='UPDATE' and old.state is distinct from 'pending_confirmation') then return new;end if;
 perform set_config('eios.tenant_id',new.tenant_id,true);
 with settled as (update runtime.nexloop_message_outbox b set status='settled' from runtime.nexloop_conversations cv
   where b.tenant_id=new.tenant_id and b.world=new.world and b.status='pending' and cv.tenant_id=b.tenant_id and cv.world=b.world
    and cv.conversation_id=b.conversation_id and cv.consumer_id=new.consumer_id returning b.message_id)
 insert into runtime.nexloop_relay_backlog_settlements(tenant_id,world,message_id,reason,source)
  select new.tenant_id,new.world,s.message_id,'consumer_erased','erasure' from settled s on conflict do nothing;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return new;
end $$;
alter function runtime.nexloop_relay_backlog_settle_on_erasure() owner to nexloop_owner;
revoke all on function runtime.nexloop_relay_backlog_settle_on_erasure() from public;
create trigger relay_backlog_settle_on_erasure after insert or update of state on control.nexloop_consumer_erasures
 for each row execute function runtime.nexloop_relay_backlog_settle_on_erasure();
