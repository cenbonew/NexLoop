-- NX-042 T4 (design docs/implementation/NX-042-design.md §3.2, §8 migration B; owner D2, D3 2026-10-20 as recommended): the
-- aggregate calibration read of the consumer twin. The migration number lives only in the file name and the catalog; nothing below
-- repeats it.
-- 1. authz.nexloop_twin_calibration_aggregates(tenant, split_at, span_days, window), executable by the read-only metrics role only
--    (granted to nexloop_metrics, and the session must be nexloop_metrics): counts and rates of the real world only, never a
--    Consumer id, message, text or per-Consumer value. No new role, no other grant; no existing function changes.
-- 2. Unit and groups: the Consumer (design §3.2: grouped by Consumer, not by message). A Consumer counts in a window when an agent
--    sent it an outbound message in that window (its first one there is its contact); per Consumer: replied within 24 hours of that
--    contact (an inbound message of the Consumer), and refused contact (a real contact restriction recorded after the contact,
--    inside the window). Groups: all, and the UTC daypart of the contact (00-06, 06-12, 12-18, 18-24).
-- 3. D3 minimum sample: a group of fewer than 30 Consumers is not output. Secondary suppression: when any daypart is below 30, no
--    daypart is output (otherwise all minus the others would give it); when all is below 30, nothing is output.
-- 4. Excluded and counted, never hidden: Consumers whose persona is not real (simulated_agent, synthetic; ADR-034, as 0233) and
--    Consumers being or already erased (NX-029). Only world 'real' is read; sim:, shadow: and test rows never are.
-- 5. Independence (design §3.2, AT-048): a window is cut by time - reference [split_at - span, split_at) for setting parameters,
--    calibration [split_at, split_at + span) for checking them - and grouped by Consumer: a Consumer contacted in the reference
--    window is excluded from the calibration window (counted), so the two never share a Consumer. split_at is a UTC midnight, the
--    span 28, 56 or 84 days, and a calibration window must have ended (no partial window).

create function runtime.nexloop_twin_calibration_aggregates(p_tenant text,p_since timestamptz,p_until timestamptz,p_exclude_before timestamptz)
 returns jsonb language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v jsonb;c_min constant integer:=30;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 with outbound as (
   select cv.consumer_id,(m.record->>'accepted_at')::timestamptz at
   from runtime.nexloop_conversation_messages m
   join runtime.nexloop_conversations cv on cv.tenant_id=m.tenant_id and cv.world=m.world and cv.conversation_id=m.conversation_id
   where m.tenant_id=p_tenant and m.world='real' and m.record->>'direction'='outbound' and m.record->>'sender_kind'='agent'),
  contacted as (
   select consumer_id,min(at) first_at from outbound where at>=p_since and at<p_until group by consumer_id),
  labelled as (
   select c.*,runtime.nexloop_consumer_persona(p_tenant,'real',c.consumer_id) persona,
    runtime.nexloop_consumer_erasing(p_tenant,'real',c.consumer_id) erasing,
    p_exclude_before is not null and exists(select 1 from outbound o where o.consumer_id=c.consumer_id and o.at>=p_exclude_before and o.at<p_since) earlier
   from contacted c),
  kept as (
   select l.consumer_id,l.first_at,
    (extract(hour from l.first_at at time zone 'UTC')::integer/6) daypart,
    exists(select 1 from runtime.nexloop_conversation_messages m join runtime.nexloop_conversations cv
      on cv.tenant_id=m.tenant_id and cv.world=m.world and cv.conversation_id=m.conversation_id
      where m.tenant_id=p_tenant and m.world='real' and cv.consumer_id=l.consumer_id and not (m.record ? 'direction')
        and (m.record->>'accepted_at')::timestamptz>l.first_at and (m.record->>'accepted_at')::timestamptz<=l.first_at+interval '24 hours') replied,
    exists(select 1 from control.nexloop_contact_restrictions r where r.tenant_id=p_tenant and r.world='real' and r.consumer_id=l.consumer_id
        and r.restricted_at>=l.first_at and r.restricted_at<p_until) refused
   from labelled l where l.persona='real' and not l.erasing and not l.earlier),
  groups as (
   select 'all' grp,count(*) n,count(*) filter (where replied) replied,count(*) filter (where refused) refused from kept
   union all
   select 'daypart_utc_'||lpad((d*6)::text,2,'0')||'_'||lpad((d*6+6)::text,2,'0'),count(k.consumer_id),
    count(*) filter (where k.replied),count(*) filter (where k.refused)
   from generate_series(0,3) d left join kept k on k.daypart=d group by d),
  shown as (
   select g.* from groups g where (select n from groups where grp='all')>=c_min
    and (g.grp='all' or not exists(select 1 from groups x where x.grp<>'all' and x.n<c_min)))
 select jsonb_build_object(
  'groups',coalesce((select jsonb_agg(jsonb_build_object('group',s.grp,'consumers',s.n,'replied_24h',s.replied,'refused',s.refused,
     'reply_rate_24h',round(s.replied::numeric/s.n,4),'refusal_rate',round(s.refused::numeric/s.n,4)) order by s.grp) from shown s),'[]'::jsonb),
  'suppressed',case when (select n from groups where grp='all')<c_min then jsonb_build_array('all','daypart')
                    when exists(select 1 from groups where grp<>'all' and n<c_min) then jsonb_build_array('daypart') else '[]'::jsonb end,
  'excluded',jsonb_build_object('non_real_persona',(select count(*) from labelled where persona<>'real'),
    'erasing',(select count(*) from labelled where persona='real' and erasing),
    'in_reference_window',(select count(*) from labelled where persona='real' and not erasing and earlier))) into v;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
alter function runtime.nexloop_twin_calibration_aggregates(text,timestamptz,timestamptz,timestamptz) owner to nexloop_owner;
revoke all on function runtime.nexloop_twin_calibration_aggregates(text,timestamptz,timestamptz,timestamptz) from public;

create function authz.nexloop_twin_calibration_aggregates(p_tenant text,p_split_at timestamptz,p_span_days integer,p_window text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set timezone='UTC' as $$
declare v_since timestamptz;v_until timestamptz;v jsonb;
begin
 if session_user is distinct from 'nexloop_metrics' then raise exception 'twin calibration read unavailable' using errcode='42501';end if;
 if p_tenant is null or p_tenant!~'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$' or p_split_at is null or p_split_at<>date_trunc('day',p_split_at)
  or p_span_days is null or p_span_days not in (28,56,84) or p_window is null or p_window not in ('reference','calibration') then
  raise exception 'twin calibration read invalid' using errcode='22023';end if;
 if p_window='reference' then
  v_since:=p_split_at-make_interval(days=>p_span_days);v_until:=p_split_at;
 else
  v_since:=p_split_at;v_until:=p_split_at+make_interval(days=>p_span_days);
 end if;
 if v_until>clock_timestamp() then raise exception 'twin calibration read: window_open' using errcode='22023';end if;
 v:=runtime.nexloop_twin_calibration_aggregates(p_tenant,v_since,v_until,
   case when p_window='calibration' then p_split_at-make_interval(days=>p_span_days) end);
 return jsonb_build_object('protocol','nexloop-twin-calibration/1','tenant_id',p_tenant,'world','real','window',p_window,
   'split_at',p_split_at,'span_days',p_span_days,'since',v_since,'until',v_until,'min_group_consumers',30)||v;
end $$;
alter function authz.nexloop_twin_calibration_aggregates(text,timestamptz,integer,text) owner to nexloop_owner;
revoke all on function authz.nexloop_twin_calibration_aggregates(text,timestamptz,integer,text) from public;
grant execute on function authz.nexloop_twin_calibration_aggregates(text,timestamptz,integer,text) to nexloop_metrics;
