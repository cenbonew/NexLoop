-- Preserve frozen EIOS operator == database session_user identity semantics.
create or replace function control.nexloop_consume_browser_rate_limit(p_tenant text,p_action text,p_digest bytea,p_operator text,p_request text,p_trace text)
 returns table(allowed boolean,retry_after_seconds integer,remaining integer,window_ends_at timestamptz,revision bigint)
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare policy_row control.nexloop_browser_rate_policies;counter_row control.nexloop_browser_rate_counters;now_at timestamptz;
begin
 if session_user<>'nexloop_api' or p_operator is distinct from session_user or p_action not in ('local_login','oidc_start','password_reset','password_reset_complete','recovery','invitation_accept')
  or p_tenant is null or length(p_tenant) not between 1 and 320 or p_operator is null or length(p_operator) not between 1 and 320
  or p_request is null or length(p_request) not between 1 and 320 or p_trace is null or length(p_trace) not between 1 and 320
  or p_digest is null or octet_length(p_digest)<>32 then raise exception 'identity rate limit unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into policy_row from control.nexloop_browser_rate_policies p
  where p.tenant_id=p_tenant and p.action=p_action and p.operator_principal_id=p_operator and p.active for share;
 if not found then raise exception 'identity rate limit unavailable' using errcode='42501';end if;
 now_at:=clock_timestamp();
 insert into control.nexloop_browser_rate_counters values(p_tenant,p_action,p_operator,p_digest,now_at+make_interval(secs=>policy_row.window_seconds),0,1)
 on conflict do nothing;
 select * into counter_row from control.nexloop_browser_rate_counters c where c.tenant_id=p_tenant and c.action=p_action
  and c.operator_principal_id=p_operator and c.client_digest=p_digest for update;
 now_at:=clock_timestamp();
 if counter_row.window_ends_at<=now_at then
  counter_row.window_ends_at:=now_at+make_interval(secs=>policy_row.window_seconds);counter_row.attempts:=0;
 end if;
 allowed:=counter_row.attempts<policy_row.maximum_attempts;
 if allowed then counter_row.attempts:=counter_row.attempts+1;end if;
 counter_row.revision:=counter_row.revision+1;
 update control.nexloop_browser_rate_counters c set attempts=counter_row.attempts,window_ends_at=counter_row.window_ends_at,revision=counter_row.revision
 where c.tenant_id=p_tenant and c.action=p_action and c.operator_principal_id=p_operator and c.client_digest=p_digest;
 remaining:=case when allowed then greatest(0,policy_row.maximum_attempts-counter_row.attempts) else 0 end;
 retry_after_seconds:=case when allowed then 0 else least(3600,greatest(1,ceil(extract(epoch from counter_row.window_ends_at-now_at))::integer)) end;
 window_ends_at:=counter_row.window_ends_at;revision:=counter_row.revision;return next;
end $$;
alter function control.nexloop_consume_browser_rate_limit(text,text,bytea,text,text,text) owner to nexloop_owner;
revoke all on function control.nexloop_consume_browser_rate_limit(text,text,bytea,text,text,text) from public;
grant execute on function control.nexloop_consume_browser_rate_limit(text,text,bytea,text,text,text) to nexloop_api;
