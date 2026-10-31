-- Relay backlog settlement reads a pseudonymized Consumer as erased (R60, stage 2026-10-31; temporary number, renumbered at merge).
-- The NX-029 erasure replaces the Consumer id of its conversations with a fresh 'pseudonym:<hex>' when it completes (0305), and the
-- erasure table keeps the original id, so runtime.nexloop_consumer_erasing(pseudonym) is false: 0376 classified the messages of
-- Consumers erased before it as no_serviceable_enrollment. Only the erasure writes a pseudonym into a conversation, so such a message
-- is consumer_erased. Nothing else changes. Audit rows already written stay as they are (append-only;
-- docs/design/relay-backlog-disposition.md section 5).
create or replace function control.nexloop_governed_relay_backlog_settle(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
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
  v_reason:=case when v_consumer is null or v_consumer like 'pseudonym:%' or runtime.nexloop_consumer_erasing(p_tenant,p_world,v_consumer) then 'consumer_erased'
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
