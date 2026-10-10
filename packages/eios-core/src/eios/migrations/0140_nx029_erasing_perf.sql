-- NX-029 (temporary number 0140): the erasing read refusal (0132) at near O(1) cost, same semantics.
--
-- 0132's runtime.nexloop_assert_not_erasing ran on every read at the public entry and at v0072 (and 0084, the edit entry)
-- through two nested SECURITY DEFINER helpers, and resolved a Message target by scanning the conversation stream
-- (no index on message_id). Now:
-- a. a partial index (tenant, world, Consumer) on the erasure requests that make a Consumer erasing (state <> 'pending_confirmation'): a tenant and
--    world without any is answered by one index probe, before the target is even resolved; one Consumer by one more;
-- b. an index on runtime.nexloop_conversation_messages(tenant_id, world, message_id): with erasing Consumers present,
--    a Message target costs index probes, not a scan (also an independent scalability fix for every Message lookup);
-- c. one function body (no nested definer calls, the tenant set once and restored), replaced in place: the four wrappers
--    call it by name and are unchanged.
-- Every answer is read live in the calling statement: an erasure committed before a read is seen by that read; there is
-- no request- or session-level memo (ADR-025: revocation applies to the next read). Published migrations are not edited.

create index nexloop_consumer_erasures_erasing on control.nexloop_consumer_erasures(tenant_id,world,consumer_id) where state<>'pending_confirmation';
create index nexloop_conversation_messages_message on runtime.nexloop_conversation_messages(tenant_id,world,message_id);

create or replace function runtime.nexloop_assert_not_erasing(p_world text,p_claims jsonb) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
declare prior text:=current_setting('eios.tenant_id',true);t text:=p_claims->>'tenant_id';
 r text:=coalesce(p_claims->>'resource_id',p_claims->>'target_resource','');v text;c text;hit boolean:=false;
begin
 if coalesce(t,'')='' then return;end if;
 if prior is distinct from t then perform set_config('eios.tenant_id',t,true);end if;
 -- (a) No erasing Consumer in this tenant and world: nothing to refuse.
 if exists(select 1 from control.nexloop_consumer_erasures e where e.tenant_id=t and e.world=p_world and e.state<>'pending_confirmation') then
  c:=substring(r from '^eios:(?:object|property):Consumer/([a-f0-9]{64})');
  if c is null and p_claims->>'type_name'='Consumer' then c:=p_claims->>'object_id';end if;
  if c is null then
   v:=substring(r from '^eios:(?:object|property):Conversation/([a-f0-9]{64})');
   if v is not null then
    select cv.consumer_id into c from runtime.nexloop_conversations cv where cv.tenant_id=t and cv.world=p_world and cv.conversation_id=v;
   else
    v:=substring(r from '^eios:(?:object|property):Message/([a-f0-9]{64})');
    -- (b) index probes: the Message, then its Conversation (primary key).
    if v is not null then
     select cv.consumer_id into c from runtime.nexloop_conversation_messages m join runtime.nexloop_conversations cv
      on cv.tenant_id=m.tenant_id and cv.world=m.world and cv.conversation_id=m.conversation_id
      where m.tenant_id=t and m.world=p_world and m.message_id=v limit 1;
    end if;
   end if;
  end if;
  if c is not null then
   hit:=exists(select 1 from control.nexloop_consumer_erasures e where e.tenant_id=t and e.world=p_world and e.consumer_id=c and e.state<>'pending_confirmation');
  end if;
 end if;
 if prior is distinct from t then perform set_config('eios.tenant_id',coalesce(prior,''),true);end if;
 if hit then raise exception 'consumer is being erased' using errcode='42501';end if;
end $$;
