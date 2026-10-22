-- NX-056 A5 (docs/design/unified-approvals.md v5 §8; NX-029): the frozen request bodies under retention and Consumer erasure.
-- Temporary number; the dispatcher renumbers.
--
-- 1. Retention: a request's frozen body lives as long as governance audit records (retention class governance_audit, 365 days by
--    default) after the request ended (its due_by; every request is terminal or consumed by then). control.nexloop_approval_redact_due
--    replaces the body by {operation, redacted: true} - ids, digest, status, decisions and the consumption stay - bounded, oldest first,
--    with a tombstone per pass. The retention keeper's 'approvals_expire' verb (0323) runs it after the expiry sweep.
-- 2. Erasure: when a Consumer's erasure completes (not with a retention hold; the same rule as 0307), every request body that names
--    the Consumer's id as a whole JSON string has it replaced by one pseudonym of this step (consumer.erase bodies and any other
--    capability's), whatever the request's status; a tombstone records the count (its subject key is the keyed HMAC, 0308).
-- Both write the body only, inside an erasure scope for this table (the 0321 guard allows nothing else).

create function control.nexloop_approval_redact_due(p_tenant text,p_world text,p_limit integer) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_days integer:=runtime.nexloop_retention_days('governance_audit');n integer;v_ref text:='retention:approval-bodies:'||gen_random_uuid();
begin
 if p_limit is null or p_limit not between 1 and 1000 then raise exception 'approval redaction limit' using errcode='22023';end if;
 if v_days is null then return jsonb_build_object('redacted',0);end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform runtime.nexloop_erasure_open('control.nexloop_approval_requests',array['body'],false,v_ref);
 update control.nexloop_approval_requests r set body=jsonb_build_object('operation',r.body->'operation','redacted',true)
  where (r.tenant_id,r.world,r.approval_id) in (select q.tenant_id,q.world,q.approval_id from control.nexloop_approval_requests q
   where q.tenant_id=p_tenant and q.world=p_world and q.due_by<clock_timestamp()-make_interval(days=>v_days) and not (q.body ? 'redacted')
   order by q.due_by,q.approval_id limit p_limit for update skip locked);
 get diagnostics n=row_count;
 delete from control.nexloop_erasure_scope where txid=txid_current() and table_name='control.nexloop_approval_requests';
 if n>0 then
  perform runtime.nexloop_erasure_tombstone(p_tenant,p_world,'retention_expiry',v_ref,'governance_audit','approval_request','approval_requests','redacted',
   jsonb_build_object('approval_requests',n),'service:retention_keeper');
 end if;
 return jsonb_build_object('redacted',n);
end $$;
alter function control.nexloop_approval_redact_due(text,text,integer) owner to nexloop_owner;
revoke all on function control.nexloop_approval_redact_due(text,text,integer) from public;

create function control.nexloop_approval_consumer_erasure() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);v_p text:='pseudonym:'||encode(extensions.gen_random_bytes(16),'hex');
 v_ref text:='consumer-erasure:'||new.request_id;n integer;tn text:=new.tenant_id;w text:=new.world;k text:=new.consumer_id;
begin
 if new.state<>'completed' or old.state='completed' then return null;end if;
 perform set_config('eios.tenant_id',tn,true);
 if runtime.nexloop_retention_held(tn,w,'consumer',k) then perform set_config('eios.tenant_id',coalesce(v_prior,''),true);return null;end if;
 perform runtime.nexloop_erasure_open('control.nexloop_approval_requests',array['body'],false,v_ref);
 -- Only where the id stands as a whole JSON string (the 0307 rule: never inside another value).
 update control.nexloop_approval_requests set body=replace(body::text,'"'||k||'"','"'||v_p||'"')::jsonb
  where tenant_id=tn and world=w and strpos(body::text,'"'||k||'"')>0;
 get diagnostics n=row_count;
 delete from control.nexloop_erasure_scope where txid=txid_current() and table_name='control.nexloop_approval_requests';
 perform runtime.nexloop_erasure_tombstone(tn,w,'consumer_erasure',v_ref,'consumer_identity','approval_requests',k,'pseudonymized',
  jsonb_build_object('approval_requests',n),'nexloop_owner');
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return null;
end $$;
alter function control.nexloop_approval_consumer_erasure() owner to nexloop_owner;
revoke all on function control.nexloop_approval_consumer_erasure() from public;
-- After the identity batches (zz_nx029_*): they read original ids first; this one only rewrites approval bodies.
create trigger zz_nx056_approval_consumer_erasure after update of state on control.nexloop_consumer_erasures
 for each row execute function control.nexloop_approval_consumer_erasure();
