-- NX-029 (temporary number 0142): conversation summaries follow their Messages (AT-026 'summaries').
--
-- NX-019 keeps, per extraction topic, a topic label and a conversation summary written from the topic's Messages
-- (ontology.nexloop_conversation_topics). Whenever a Message's text is erased - expiry (0129), Message.erase or a
-- Consumer erasure (0132), or a replayed deletion (0140) - the stream record is marked erased; this trigger clears the
-- topic label and summary of every topic built from that Message in the same transaction. Topic keys, sequences and
-- Message ids stay (no content). Published migrations are not edited.
create function runtime.nexloop_topics_on_message_erased() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);
begin
 if (new.record ? 'erased') and not (old.record ? 'erased') then
  perform set_config('eios.tenant_id',new.tenant_id,true);
  update ontology.nexloop_conversation_topics set topic='',conversation_summary=''
   where tenant_id=new.tenant_id and world=new.world and conversation_id=new.conversation_id and message_ids ? new.message_id
    and (topic<>'' or conversation_summary<>'');
  perform set_config('eios.tenant_id',coalesce(prior,''),true);
 end if;
 return null;
end $$;
alter function runtime.nexloop_topics_on_message_erased() owner to nexloop_owner;
revoke all on function runtime.nexloop_topics_on_message_erased() from public;
create trigger nx029_topics_on_message_erased after update of record on runtime.nexloop_conversation_messages
 for each row execute function runtime.nexloop_topics_on_message_erased();
