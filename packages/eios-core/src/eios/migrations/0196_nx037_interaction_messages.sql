-- NX-037 multi-turn interactions (owner 2026-10-14, plan v3: long conversations, repeated refusals): the customer messages of
-- the tenant accepted at or after an interaction's prompt time, in order, for the read-only metrics role. Ids and times only;
-- the driver observes each one through the 0172 observation (since = its accepted_at) so that every turn's Run, refusal
-- contact and cost belongs to the interaction. Nothing is written. Owner function under row security for the tenant.
create function runtime.nexloop_nx037_interaction_messages(p_tenant text,p_world text,p_since timestamptz) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select coalesce(jsonb_agg(jsonb_build_object('message_id',x.message_id,'accepted_at',x.accepted_at) order by x.accepted_at,x.message_id),'[]'::jsonb) into v
  from (select cm.message_id,(cm.record->>'accepted_at')::timestamptz accepted_at from runtime.nexloop_conversation_messages cm
         where cm.tenant_id=p_tenant and cm.world=p_world and not (cm.record ? 'direction')
           and (cm.record->>'accepted_at')::timestamptz>=p_since order by 2,1 limit 64) x;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
alter function runtime.nexloop_nx037_interaction_messages(text,text,timestamptz) owner to nexloop_owner;
revoke all on function runtime.nexloop_nx037_interaction_messages(text,text,timestamptz) from public;
create function authz.nexloop_nx037_interaction_messages(p_tenant text,p_world text,p_since timestamptz) returns jsonb
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$ select runtime.nexloop_nx037_interaction_messages(p_tenant,p_world,p_since) $$;
alter function authz.nexloop_nx037_interaction_messages(text,text,timestamptz) owner to nexloop_owner;
revoke all on function authz.nexloop_nx037_interaction_messages(text,text,timestamptz) from public;
grant execute on function authz.nexloop_nx037_interaction_messages(text,text,timestamptz) to nexloop_metrics;
