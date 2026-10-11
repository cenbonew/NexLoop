-- Relay replan fix (temporary number 0190): a message the relay cannot complete is ended and handed to the reply guarantee
-- (ADR-023 fallback) instead of being retried for ever. Dispatcher ruling 2026-10-11: never reuse the first issued command (that
-- would dispatch under an outdated control); end the message, release the consumer's serial position (D2), audit and count it.
--
-- Root cause (L4 C4-low): a message whose first attempt issued a Run credential (authz.nexloop_message_run_issuances pins its
-- assignment) and then failed was never released ('release' only accepts a routed message); after the 30 s lease it was claimed
-- again with the consumer's current recipe, whose assignment no longer matched the pinned one, so every attempt failed, and after
-- the 300 s credential every attempt returned requires_governed_replan, which the relay neither released nor escalated. claim_next
-- serves a consumer's oldest pending message only, so all later messages of that consumer waited behind it.
--
-- 1. New verbs of the message run issuance command (0141 wrapper renamed once and kept, wrapped again; earlier bodies unchanged):
--    yield   — after a transient failure the relay gives the message back: lease ends after a short backoff, one attempt counted;
--              the answer says when the bounded attempts (5) are used up;
--    handoff — the message ends on the relay path: reason requires_governed_replan | credential_expired | binding_conflict |
--              attempts_exhausted. The outbox item leaves 'pending' (as a takeover settlement does), the lease ends, the Run
--              credential issued for it (if any) is revoked, its pending reply becomes due now for the reply guarantee, one
--              append-only handoff row and one audit event are written. Idempotent: a second handoff returns the first.
--    lookup  — also returns the pinned assignment digest, so the relay recognises a binding conflict before issuing.
-- 2. runtime.nexloop_relay_metrics (0143 body + two counts): handoffs in the last hour and in total.
-- Published migrations are not edited.

create table runtime.nexloop_relay_handoffs (
 tenant_id text not null,world text not null,message_id text not null check(message_id~'^[a-f0-9]{64}$'),consumer_id text not null,
 reason text not null check(reason in ('requires_governed_replan','credential_expired','binding_conflict','attempts_exhausted')),
 relay_principal text not null,run_id uuid,attempts integer not null default 0,handed_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,message_id)
);
create table runtime.nexloop_relay_attempts (
 tenant_id text not null,world text not null,message_id text not null,attempts integer not null default 0 check(attempts>=0),
 last_failure_at timestamptz not null default clock_timestamp(),primary key(tenant_id,world,message_id)
);
do $t$
declare t text;
begin
 foreach t in array array['runtime.nexloop_relay_handoffs','runtime.nexloop_relay_attempts'] loop
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy tenant_boundary on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on %s from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator',t);
 end loop;
end $t$;
create trigger relay_handoff_append_only before update or delete on runtime.nexloop_relay_handoffs for each row execute function control.nexloop_nx022_append_only();

create function runtime.nexloop_relay_attempt_limit() returns integer language sql immutable set search_path=pg_catalog,pg_temp as $$ select 5 $$;
alter function runtime.nexloop_relay_attempt_limit() owner to nexloop_owner;
revoke all on function runtime.nexloop_relay_attempt_limit() from public;

-- Same authentication as 0049 / 0141; returns the tenant. The lease must be the caller's and current unless p_any_lease.
create function authz.nexloop_relay_handoff_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;v_verb text:=c->>'verb';v_tenant text:=a->>'tenant_id';v_principal text:=a->>'principal_id';
 v_key bytea;v_def control.nexloop_action_definitions;v_lease runtime.nexloop_message_relay_leases;v_out runtime.nexloop_message_outbox;
 v_handoff runtime.nexloop_relay_handoffs;v_issued authz.nexloop_message_run_issuances;v_attempts integer;v_conv runtime.nexloop_conversations;
begin
 if v_tenant is null or v_principal is null or session_user<>'nexloop_api' or p_world is distinct from 'real' or octet_length(p_text)>1048576 or octet_length(p_payload)>262144
  or a->>'protocol' is distinct from 'nexloop-message-run-issuance-v1' or a->>'action_resource' is distinct from 'eios:action:nexloop.conversation.route:1'
  or a->>'operation' is distinct from 'execute' or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or v_verb not in ('yield','handoff') or coalesce(c->>'message_id','')!~'^[a-f0-9]{64}$'
  or jsonb_typeof(c->'fence') is distinct from 'number' or c->>'fence'!~'^[0-9]{1,18}$' then raise exception 'message relay unavailable' using errcode='42501';end if;
 select key_material into v_key from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-message-run-issuance-v1:'||p_text,'UTF8'),v_key,'sha256'),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then raise exception 'message relay unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 perform set_config('eios.tenant_id',v_tenant,true);
 if not exists(select 1 from authz.nexloop_service_credentials s where s.token_digest=p_digest and s.tenant_id=v_tenant and s.status='active' and s.binding->>'subject_kind'='service') then
  raise exception 'message relay unavailable' using errcode='42501';end if;
 select * into v_def from control.nexloop_action_definitions where tenant_id=v_tenant and world=p_world and resource_id=a->>'action_resource' and active for share;
 if not found or v_def.definition is distinct from a->'definition' or v_def.capability is distinct from a->'capability' then raise exception 'message relay unavailable' using errcode='42501';end if;
 perform pg_advisory_xact_lock(hashtextextended(v_tenant||':'||p_world||':message-route:'||(c->>'message_id'),0));
 select * into v_handoff from runtime.nexloop_relay_handoffs where tenant_id=v_tenant and world=p_world and message_id=c->>'message_id';
 if found then
  return jsonb_build_object('status','handed_to_fallback','reason',v_handoff.reason,'replay',true);
 end if;
 select * into v_lease from runtime.nexloop_message_relay_leases where tenant_id=v_tenant and world=p_world and message_id=c->>'message_id' for update;
 if not found or v_lease.principal_id is distinct from v_principal or v_lease.credential_digest is distinct from p_digest or v_lease.fence<>(c->>'fence')::bigint then
  raise exception 'message relay unavailable' using errcode='42501';end if;
 select * into v_out from runtime.nexloop_message_outbox where tenant_id=v_tenant and world=p_world and message_id=v_lease.message_id for update;
 if not found or v_out.status is distinct from 'pending' then raise exception 'message relay unavailable' using errcode='42501';end if;
 if v_verb='yield' then
  insert into runtime.nexloop_relay_attempts as x(tenant_id,world,message_id,attempts) values(v_tenant,p_world,v_out.message_id,1)
   on conflict(tenant_id,world,message_id) do update set attempts=x.attempts+1,last_failure_at=clock_timestamp() returning attempts into v_attempts;
  -- Short backoff (D2 keeps the consumer waiting meanwhile), never longer than a minute.
  update runtime.nexloop_message_relay_leases set lease_until=clock_timestamp()+make_interval(secs=>least(v_attempts*10,60))
   where tenant_id=v_tenant and world=p_world and message_id=v_out.message_id;
  return jsonb_build_object('status','yielded','attempts',v_attempts,'handoff_due',v_attempts>=runtime.nexloop_relay_attempt_limit());
 end if;
 if c->>'reason' not in ('requires_governed_replan','credential_expired','binding_conflict','attempts_exhausted') then
  raise exception 'message relay unavailable' using errcode='42501';end if;
 if c->>'reason'='attempts_exhausted' and coalesce((select attempts from runtime.nexloop_relay_attempts where tenant_id=v_tenant and world=p_world and message_id=v_out.message_id),0)
   <runtime.nexloop_relay_attempt_limit() then raise exception 'relay attempts not exhausted' using errcode='42501';end if;
 select * into v_conv from runtime.nexloop_conversations where tenant_id=v_tenant and world=p_world and conversation_id=v_out.conversation_id;
 select * into v_issued from authz.nexloop_message_run_issuances where tenant_id=v_tenant and world=p_world and message_id=v_out.message_id;
 -- The first command is never reused: its Run credential ends here.
 if v_issued.run_id is not null then
  update authz.nexloop_run_credentials set status='revoked' where run_id=v_issued.run_id and status<>'revoked';
 end if;
 update runtime.nexloop_message_outbox set status='delivered' where tenant_id=v_tenant and world=p_world and message_id=v_out.message_id and status='pending';
 update runtime.nexloop_message_relay_leases set lease_until=clock_timestamp() where tenant_id=v_tenant and world=p_world and message_id=v_out.message_id;
 -- ADR-023: the pending reply becomes due now; the reply guarantee answers it under the current controls.
 update runtime.nexloop_work_feed set available_at=least(available_at,clock_timestamp()),changed_at=clock_timestamp(),change_seq=change_seq+1
  where tenant_id=v_tenant and world=p_world and feed='reply-due' and item_key='reply:'||v_out.message_id;
 insert into runtime.nexloop_relay_handoffs(tenant_id,world,message_id,consumer_id,reason,relay_principal,run_id,attempts)
  values(v_tenant,p_world,v_out.message_id,v_conv.consumer_id,c->>'reason',v_principal,v_issued.run_id,
   coalesce((select attempts from runtime.nexloop_relay_attempts where tenant_id=v_tenant and world=p_world and message_id=v_out.message_id),0));
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('relay-handoff:'||v_out.message_id,'nexloop.relay.handoff',v_tenant,'service:'||v_principal,'relay',v_out.message_id,
   jsonb_build_object('reason',c->>'reason','consumer_id',v_conv.consumer_id,'run_revoked',v_issued.run_id is not null),clock_timestamp()) on conflict do nothing;
 return jsonb_build_object('status','handed_to_fallback','reason',c->>'reason','replay',false,'run_revoked',v_issued.run_id is not null);
end $$;
alter function authz.nexloop_relay_handoff_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_relay_handoff_command(text,text,text,text,text) from public;

alter function authz.nexloop_message_run_issuance_command(text,text,text,text,text) rename to nexloop_message_run_issuance_command_before_relay_handoff_v0190;
revoke all on function authz.nexloop_message_run_issuance_command_before_relay_handoff_v0190(text,text,text,text,text) from public,nexloop_api;
create function authz.nexloop_message_run_issuance_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare c jsonb:=p_payload::jsonb;v_result jsonb;v_tenant text:=p_text::jsonb->>'tenant_id';
begin
 if c->>'verb' in ('yield','handoff') then return authz.nexloop_relay_handoff_command(p_digest,p_world,p_text,p_signature,p_payload);end if;
 v_result:=authz.nexloop_message_run_issuance_command_before_relay_handoff_v0190(p_digest,p_world,p_text,p_signature,p_payload);
 if c->>'verb'='lookup' and v_result is not null then
  perform set_config('eios.tenant_id',v_tenant,true);
  v_result:=v_result||jsonb_build_object('assignment_digest',(select assignment_digest from authz.nexloop_message_run_issuances
   where tenant_id=v_tenant and world=p_world and message_id=c->>'message_id'));
 end if;
 return v_result;
end $$;
alter function authz.nexloop_message_run_issuance_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_message_run_issuance_command(text,text,text,text,text) from public,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;
grant execute on function authz.nexloop_message_run_issuance_command(text,text,text,text,text) to nexloop_api;

-- 2. Metrics: the 0143 body plus handoff counts.
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
     from authz.nexloop_authority_facts f where f.tenant_id=p_tenant and f.fact_kind='relay_source_read_rule'),
   'handoffs_last_hour',(select count(*) from runtime.nexloop_relay_handoffs h where h.tenant_id=p_tenant and h.world=p_world and h.handed_at>p_now-interval '1 hour'),
   'handoffs_total',(select count(*) from runtime.nexloop_relay_handoffs h where h.tenant_id=p_tenant and h.world=p_world))
 from runtime.nexloop_message_outbox b
 join runtime.nexloop_conversations cv on cv.tenant_id=b.tenant_id and cv.world=b.world and cv.conversation_id=b.conversation_id
 left join lateral (select x.state from control.nexloop_relay_enrollments x where x.tenant_id=b.tenant_id and x.world=b.world and x.consumer_id=cv.consumer_id
   order by x.enrollment_revision desc limit 1) e on true
 where b.tenant_id=p_tenant and b.world=p_world and b.status='pending' $$;
