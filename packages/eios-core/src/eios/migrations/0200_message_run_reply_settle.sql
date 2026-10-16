-- Reply fail-fast (temporary number 0200; 10/16 stage smoke): a message Run (or a fallback reply Run) that ended without
-- submitting a reply is a definite failure, and its message's reply guarantee is due now instead of after
-- start_after_seconds. Read-only except for that one due time, which only moves earlier. The runtime worker calls it with
-- its own service session after the Host reported the Run terminal; the reply itself is never created here.
create function authz.nexloop_message_run_reply_settle(p_digest text,p_world text,p_run uuid) returns jsonb
language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text;v_message text;v_kind text;v_replied boolean;v_expedited integer:=0;
begin
 v_tenant:=authz.nexloop_service_identity_snapshot(p_digest,p_world)->'binding'->>'tenant_id';
 if v_tenant is null or p_run is null then raise exception 'reply settle rejected' using errcode='42501';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 select message_id,'message' into v_message,v_kind from runtime.nexloop_message_routes
  where tenant_id=v_tenant and world=p_world and run_id=p_run;
 if v_message is null then
  select message_id,'fallback' into v_message,v_kind from runtime.nexloop_fallback_context_bindings
   where tenant_id=v_tenant and world=p_world and run_id=p_run;
 end if;
 if v_message is null then return jsonb_build_object('reply_run',false);end if;
 -- A reply is the Run's own submitted nexloop.service.request intent (0079 turns it into the outbound message).
 v_replied:=exists(select 1 from runtime.nexloop_effect_submissions s join runtime.nexloop_effect_intents i on i.intent_id=s.intent_id
  where s.run_id=p_run and i.tenant_id=v_tenant and i.world=p_world and i.action_name='nexloop.service.request');
 if not v_replied then
  update runtime.nexloop_work_feed set available_at=clock_timestamp(),changed_at=clock_timestamp()
   where tenant_id=v_tenant and world=p_world and feed='reply-due' and item_key='reply:'||v_message
    and status='pending' and available_at>clock_timestamp();
  get diagnostics v_expedited=row_count;
 end if;
 return jsonb_build_object('reply_run',true,'kind',v_kind,'replied',v_replied,'expedited',v_expedited=1);
end $$;
alter function authz.nexloop_message_run_reply_settle(text,text,uuid) owner to nexloop_owner;
revoke all on function authz.nexloop_message_run_reply_settle(text,text,uuid) from public;
grant execute on function authz.nexloop_message_run_reply_settle(text,text,uuid) to nexloop_scheduler,nexloop_domain_worker;
