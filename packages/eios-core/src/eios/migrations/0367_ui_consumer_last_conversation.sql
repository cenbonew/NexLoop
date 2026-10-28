-- UI backend X3 (L9 workbench UX audit, C8): each row of the workbench consumers list carries the time of the customer's latest
-- conversation message, so the list can be sorted by it. Temporary number; the dispatcher renumbers.
--
-- authz.nexloop_workbench_read is renamed and wrapped (the 0246 / 0345 / 0359 pattern; every verb and its checks unchanged in the
-- wrapped function). For the verb 'consumers' only, after the wrapped function verified the signed read, the member, the role and
-- the read Action and answered, each consumer row gains
--   last_conversation_at  the acceptance time of the newest Message in any of this Consumer's conversations (the time stored with
--                         the Message, as the conversation read shows it), or null: no message yet, or the Consumer is being
--                         erased or was erased (NX-029) - such a Consumer never takes part in the time order.
-- Rows are neither added nor dropped and keep their order (the id order of the page); the workbench API sorts.
set local timezone = 'UTC';

create function runtime.nexloop_consumer_last_conversation_at(p_tenant text,p_world text,p_consumer text) returns text
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select case when runtime.nexloop_consumer_erasing(p_tenant,p_world,p_consumer) then null else
  (select runtime.nexloop_commercial_ts(max((m.record->>'accepted_at')::timestamptz)) from runtime.nexloop_conversations t
    join runtime.nexloop_conversation_messages m on m.tenant_id=t.tenant_id and m.world=t.world and m.conversation_id=t.conversation_id
   where t.tenant_id=p_tenant and t.world=p_world and t.consumer_id=p_consumer) end
$$;
alter function runtime.nexloop_consumer_last_conversation_at(text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_consumer_last_conversation_at(text,text,text) from public;

alter function authz.nexloop_workbench_read(text,text,text,text,text) rename to nexloop_workbench_read_before_last_conversation;
revoke all on function authz.nexloop_workbench_read_before_last_conversation(text,text,text,text,text) from public,nexloop_api;
create function authz.nexloop_workbench_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare c jsonb:=p_payload::jsonb;v jsonb;v_tenant text;rows jsonb;
begin
 v:=authz.nexloop_workbench_read_before_last_conversation(p_digest,p_world,p_text,p_signature,p_payload);
 if c->>'verb' is distinct from 'consumers' or v is null or jsonb_typeof(v->'consumers') is distinct from 'array' then return v;end if;
 -- Verified by the wrapped function (signature, member, role, Action authority) before it answered; the tenant is the member's.
 v_tenant:=authz.nexloop_workbench_member(p_digest,p_world)->>'tenant_id';
 perform set_config('eios.tenant_id',v_tenant,true);
 select coalesce(jsonb_agg(r.x||jsonb_build_object('last_conversation_at',runtime.nexloop_consumer_last_conversation_at(v_tenant,p_world,r.x->>'consumer_id'))
   order by r.n),'[]'::jsonb) into rows from jsonb_array_elements(v->'consumers') with ordinality r(x,n);
 return v||jsonb_build_object('consumers',rows);
end $$;
alter function authz.nexloop_workbench_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_read(text,text,text,text,text) from public;
grant execute on function authz.nexloop_workbench_read(text,text,text,text,text) to nexloop_api;
