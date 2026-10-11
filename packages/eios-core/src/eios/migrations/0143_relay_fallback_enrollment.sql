-- Relay multi-consumer (temporary number 0162; dispatcher 2026-10-11, required before merge): the fallback reply Run issuance
-- (0110, wrapped by 0126) re-checks, in SQL and in the issuing transaction, what the message issuance already re-checks (0160):
--   * the message's consumer is not erasing (NX-029), for every fallback;
--   * a consumer that has ever been enrolled for a relay (0160) must have an active current enrollment for the calling relay
--     principal. A consumer never enrolled is the single-consumer recipe mode (D4) and keeps its behaviour.
-- A refusal raises after the existing checks, so the whole issuance rolls back: withdrawal between the launcher's profile read and
-- the issuance leaves no window. 0110 and 0126 are unchanged; the public entry is renamed and wrapped.
alter function authz.nexloop_reply_fallback_command(text,text,text,text,text) rename to nexloop_reply_fallback_command_before_relay_enrollment_v0162;
revoke all on function authz.nexloop_reply_fallback_command_before_relay_enrollment_v0162(text,text,text,text,text) from public,nexloop_api;
create function authz.nexloop_reply_fallback_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_result jsonb:=authz.nexloop_reply_fallback_command_before_relay_enrollment_v0162(p_digest,p_world,p_text,p_signature,p_payload);
 c jsonb:=p_payload::jsonb;v_tenant text:=p_text::jsonb->>'tenant_id';v_principal text:=p_text::jsonb->>'principal_id';v_consumer text;
 e control.nexloop_relay_enrollments;
begin
 if c->>'verb'='issue' then
  perform set_config('eios.tenant_id',v_tenant,true);
  select cv.consumer_id into v_consumer from runtime.nexloop_message_outbox b join runtime.nexloop_conversations cv
   on cv.tenant_id=b.tenant_id and cv.world=b.world and cv.conversation_id=b.conversation_id where b.tenant_id=v_tenant and b.world=p_world and b.message_id=c->>'message_id';
  if runtime.nexloop_consumer_erasing(v_tenant,p_world,v_consumer) then raise exception 'consumer erasing: no fallback reply Run' using errcode='42501';end if;
  e:=control.nexloop_relay_enrollment_current(v_tenant,p_world,v_consumer);
  perform set_config('eios.tenant_id',v_tenant,true);
  if e.consumer_id is not null and (e.state is distinct from 'active' or e.relay_principal is distinct from v_principal) then
   raise exception 'relay enrollment withdrawn or for another relay: no fallback reply Run' using errcode='42501';end if;
 end if;
 return v_result;
end $$;
alter function authz.nexloop_reply_fallback_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_reply_fallback_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_reply_fallback_command(text,text,text,text,text) to nexloop_api;

-- doctor --relay-source (read-only metrics role): the relay section also reports how many consumers are actively enrolled and,
-- for each relay_source_read_rule, its Source and relay principal, whether it is active, and whether that Source principal holds an
-- active service credential. IDs and booleans only.
create or replace function runtime.nexloop_relay_metrics(p_tenant text,p_world text,p_now timestamptz) returns jsonb
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select jsonb_build_object('pending_messages',count(*),'consumers_pending',count(distinct cv.consumer_id),
   'oldest_pending_seconds',floor(extract(epoch from p_now-min((b.record->>'accepted_at')::timestamptz)))::bigint,
   'unenrolled_consumers_pending',count(distinct cv.consumer_id) filter (where e.state is distinct from 'active'),
   'enrolled_consumers',(select count(*) from (select distinct on (x.consumer_id) x.state from control.nexloop_relay_enrollments x
      where x.tenant_id=p_tenant and x.world=p_world order by x.consumer_id,x.enrollment_revision desc) z where z.state='active'),
   'source_rules',(select coalesce(jsonb_agg(jsonb_build_object('source_principal',f.entity_key[1],'relay_principal',f.payload->>'relay_principal',
      'active',f.payload->'active'='true'::jsonb and (f.payload->>'valid_until')::timestamptz>p_now,
      'credential_active',exists(select 1 from authz.nexloop_service_credentials s where s.tenant_id=p_tenant and s.status='active' and s.expires_at>p_now
        and s.binding->>'subject_kind'='service' and s.binding->>'subject_principal_id'=f.entity_key[1])) order by f.entity_key[1]),'[]'::jsonb)
     from authz.nexloop_authority_facts f where f.tenant_id=p_tenant and f.fact_kind='relay_source_read_rule'))
 from runtime.nexloop_message_outbox b
 join runtime.nexloop_conversations cv on cv.tenant_id=b.tenant_id and cv.world=b.world and cv.conversation_id=b.conversation_id
 left join lateral (select x.state from control.nexloop_relay_enrollments x where x.tenant_id=b.tenant_id and x.world=b.world and x.consumer_id=cv.consumer_id
   order by x.enrollment_revision desc limit 1) e on true
 where b.tenant_id=p_tenant and b.world=p_world and b.status='pending' $$;
