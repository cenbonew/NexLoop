-- Out-of-workbench alert notification (owner 2026-11-08; docs/implementation/alert-notifier-design.md): the firing alerts, for the
-- read-only nexloop_metrics login only. The migration number lives only in the file name and the catalog.
--
-- authz.nexloop_alerts_firing_export(): nexloop_metrics can execute authz.nexloop_metrics_export() and nothing else (0131 D4); the
-- snapshot has the evaluator's freshness but not which alerts fire. This function gives that list and nothing more: per tenant and
-- world, each firing alert's dedupe_key, rule_id, severity, first_fired_at and fire_count, and the evaluator's last evaluation.
-- No value, threshold, selector text beyond the dedupe key, business data or Consumer data. Same login, no new role, no table grant.
create function authz.nexloop_alerts_firing_export() returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);t text;v jsonb:='[]'::jsonb;
begin
 if session_user is distinct from 'nexloop_metrics' then raise exception 'alerts export unavailable' using errcode='42501';end if;
 for t in select tenant_id from control.nexloop_tenants order by tenant_id loop
  perform set_config('eios.tenant_id',t,true);
  v:=v||coalesce((select jsonb_agg(jsonb_build_object('tenant_id',w.tenant_id,'world',w.world,
      'last_evaluated_at',to_char(w.last_evaluated_at at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"'),
      'firing',coalesce((select jsonb_agg(jsonb_build_object('dedupe_key',s.dedupe_key,'rule_id',s.rule_id,'severity',s.severity,
         'first_fired_at',to_char(s.first_fired_at at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"'),'fire_count',s.fire_count) order by s.dedupe_key)
        from control.nexloop_alert_state s where s.tenant_id=w.tenant_id and s.world=w.world and s.firing),'[]'::jsonb)) order by w.world)
    from (select tenant_id,world,max(last_evaluated_at) last_evaluated_at from control.nexloop_alert_state where tenant_id=t group by 1,2) w),'[]'::jsonb);
 end loop;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
alter function authz.nexloop_alerts_firing_export() owner to nexloop_owner;
revoke all on function authz.nexloop_alerts_firing_export() from public;
grant execute on function authz.nexloop_alerts_firing_export() to nexloop_metrics;
