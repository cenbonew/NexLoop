-- UI backend X4 (L9 workbench UX audit, B12): the customer's own conversation list carries times, so WebChat can name a conversation
-- by when it started and when it last moved instead of "服务对话 N". There is no title: none is stored, and a title written by the
-- customer would be a new write path. Temporary number; the dispatcher renumbers.
--
-- authz.nexloop_conversation_command is renamed and wrapped (the 0154 pattern; every verb and its checks unchanged in the wrapped
-- function). For the verb 'conversations' only, after the wrapped function verified the signed command and that the caller is the
-- browser Human owning these conversations, each item gains
--   started_at       the acceptance time of the conversation's first Message, or null when it has none yet;
--   last_message_at  the acceptance time of its newest Message, or null;
--   message_count    the number of Messages (the conversation's last sequence).
-- The times are the ones stored with each Message (the conversation read's accepted_at). Items are neither added nor dropped and keep
-- their order; no body is read.
set local timezone = 'UTC';

alter function authz.nexloop_conversation_command(text,text,text,text,text) rename to nexloop_conversation_command_before_conversation_times;
revoke all on function authz.nexloop_conversation_command_before_conversation_times(text,text,text,text,text) from public,nexloop_api;
create function authz.nexloop_conversation_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare c jsonb:=p_payload::jsonb;v jsonb;v_tenant text;items jsonb;
begin
 v:=authz.nexloop_conversation_command_before_conversation_times(p_digest,p_world,p_text,p_signature,p_payload);
 if c->>'verb' is distinct from 'conversations' or v is null or jsonb_typeof(v->'items') is distinct from 'array' then return v;end if;
 -- The wrapped function checked that the signed command's tenant is the caller's (as 0154 reads it).
 v_tenant:=(p_text::jsonb)->>'tenant_id';
 select coalesce(jsonb_agg(r.x||coalesce((select jsonb_build_object(
    'started_at',(select m.record->>'accepted_at' from runtime.nexloop_conversation_messages m where m.tenant_id=t.tenant_id and m.world=t.world
      and m.conversation_id=t.conversation_id order by m.sequence limit 1),
    'last_message_at',(select m.record->>'accepted_at' from runtime.nexloop_conversation_messages m where m.tenant_id=t.tenant_id and m.world=t.world
      and m.conversation_id=t.conversation_id order by m.sequence desc limit 1),
    'message_count',t.last_sequence)
   from runtime.nexloop_conversations t where t.tenant_id=v_tenant and t.world=p_world and t.conversation_id=r.x->>'id'),'{}'::jsonb) order by r.n),'[]'::jsonb)
  into items from jsonb_array_elements(v->'items') with ordinality r(x,n);
 return v||jsonb_build_object('items',items);
end $$;
alter function authz.nexloop_conversation_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_conversation_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_conversation_command(text,text,text,text,text) to nexloop_api;
