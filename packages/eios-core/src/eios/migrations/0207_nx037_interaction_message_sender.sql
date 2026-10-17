-- Sender of each interaction message (temporary number 0207; 10/18 trial 4f5d795c: interaction 1 planned customer-1, its
-- first message came from customer-2 and was judged as part of it). runtime.nexloop_nx037_interaction_messages (latest body:
-- 0196, copied verbatim) also returns each message's consumer_id (its conversation's Consumer), so the driver checks the sender
-- against the planned consumer. Ids and times only; read-only, as before.
create or replace function runtime.nexloop_nx037_interaction_messages(p_tenant text,p_world text,p_since timestamptz) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select coalesce(jsonb_agg(jsonb_build_object('message_id',x.message_id,'accepted_at',x.accepted_at,'consumer_id',x.consumer_id) order by x.accepted_at,x.message_id),'[]'::jsonb) into v
  from (select cm.message_id,(cm.record->>'accepted_at')::timestamptz accepted_at,cv.consumer_id from runtime.nexloop_conversation_messages cm
         join runtime.nexloop_conversations cv on cv.tenant_id=cm.tenant_id and cv.world=cm.world and cv.conversation_id=cm.conversation_id
         where cm.tenant_id=p_tenant and cm.world=p_world and not (cm.record ? 'direction')
           and (cm.record->>'accepted_at')::timestamptz>=p_since order by 2,1 limit 64) x;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
