-- NX-061 B5 threshold (owner 2026-11-07, as recommended; relayed by the dispatcher): a failure rate is judged only on enough decided
-- outcomes. The alert evaluator (0131) compares one JSON number against one threshold, so the composite "decided >= 10 and
-- failure_rate > 0.2" becomes a derived field the rule reads:
-- * outcomes.min_decided: 10, the owner's sample gate (to be revisited on real data after go-live);
-- * outcomes.failure_rate_gated: failure_rate when decided >= min_decided, JSON null otherwise (the evaluator never compares a null, so a
--   thin hour fires nothing; alert rules v31 outcomes_failure_rate_high reads it, warning above 0.2);
-- * outcomes.gate: 'measured' | 'insufficient_data', and outcomes.insufficient_data: 1 | 0 (a number, shown like data_health's no_data;
--   the owner chose no alert for it). Insufficient data is not a pass: it says "not judged", never "within the threshold".
-- A snapshot without an outcomes block (no snapshot at all) has no gated value either: the rule cannot fire and nothing is judged;
-- nexloop_eios.run_outcome_taxonomy.failure_rate_gate reads that as no_data.
-- The snapshot is renamed once and wrapped (the 0277/0318/0405 pattern); every other block is unchanged. Temporary number.
alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_outcome_gate;
revoke all on function runtime.nexloop_metrics_snapshot_before_outcome_gate(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v jsonb:=runtime.nexloop_metrics_snapshot_before_outcome_gate(p_tenant,p_world);o jsonb:=v->'outcomes';enough boolean;
begin
 if jsonb_typeof(o) is distinct from 'object' then return v;end if;
 enough:=jsonb_typeof(o->'decided')='number' and (o->>'decided')::numeric>=10;
 return v||jsonb_build_object('outcomes',o||jsonb_build_object('min_decided',10,
  'failure_rate_gated',case when enough then o->'failure_rate' else 'null'::jsonb end,
  'gate',case when enough then 'measured' else 'insufficient_data' end,
  'insufficient_data',case when enough then 0 else 1 end));
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;
