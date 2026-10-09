#!/usr/bin/env bash
# NX-049 deploy-host timing profile (measurement only). Usage:
#   scripts/perf/nx049_profile.sh <label> [repeats=3]
# Runs v4[complete] and the two-Pi Role test serially, <repeats> times each, on disposable
# PostgreSQL with scripts/perf/hooks (timeline mode), then writes
# scripts/perf/out/<label>/summary.json and summary.md (see docs/implementation/NX-049-profile.md).
# Requirements on PATH / env: node v24, PostgreSQL 18 bin dir in NEXLOOP_TEST_PG_BIN, a built
# Agent Host (apps/agent-host/dist/main.js) and the repository .venv (or uv). No Mac paths.
set -euo pipefail
cd "$(dirname "$0")/../.."
label="${1:?usage: nx049_profile.sh <label> [repeats]}"; repeats="${2:-3}"
: "${NEXLOOP_TEST_PG_BIN:?set NEXLOOP_TEST_PG_BIN to the PostgreSQL 18 bin directory}"
[ -x "$NEXLOOP_TEST_PG_BIN/initdb" ] || { echo "no initdb under NEXLOOP_TEST_PG_BIN" >&2; exit 2; }
node --version | grep -q '^v24\.' || { echo "Node 24 required on PATH (found $(node --version 2>/dev/null || echo none))" >&2; exit 2; }
[ -f apps/agent-host/dist/main.js ] || { echo "built Agent Host required: apps/agent-host/dist/main.js" >&2; exit 2; }
if [ -x .venv/bin/python ]; then PY=(.venv/bin/python); else PY=(uv run --frozen python); fi
if locale -a 2>/dev/null | grep -qi '^en_US\.utf-\?8$'; then loc=en_US.UTF-8; else loc=C.UTF-8; fi
export LC_ALL="$loc" LANG="$loc"
out="$PWD/scripts/perf/out/$label"; rm -rf "$out"; mkdir -p "$out"
printf '%s\n' "$out" > scripts/perf/out/.active
export PYTHONPATH="$PWD/scripts/perf/hooks:$PWD/packages/eios-core/src:$PWD/tests"
export NEXLOOP_PERF_OUT="$out" NEXLOOP_PERF_TIMELINE=1
NODES=(
 "tests/test_relationship_context_v4.py::test_real_human_message_v4_bound_artifact[complete]"
 "tests/test_role_pi_effect_checkpoint.py::test_actual_two_pi_role_runs_one_shared_intent_one_real_loopback_effect"
)
{ uname -a; nproc 2>/dev/null || sysctl -n hw.ncpu; node --version; "$NEXLOOP_TEST_PG_BIN/postgres" --version; "${PY[@]}" --version; uptime; } > "$out/host.txt" 2>&1 || true
for iteration in $(seq 1 "$repeats"); do
  for node in "${NODES[@]}"; do
    export NEXLOOP_PERF_ITER="$iteration"
    log="$out/pytest-$iteration-$(echo "$node" | tr -c 'A-Za-z0-9_.-' '_' | tail -c 100).log"
    start=$("${PY[@]}" -c 'import time;print(time.time())')
    rc=0; "${PY[@]}" -m pytest -q -p perf_plugin -p no:xdist -p no:cacheprovider "$node" --tb=short > "$log" 2>&1 || rc=$?
    end=$("${PY[@]}" -c 'import time;print(time.time())')
    "${PY[@]}" -c "import json,sys;print(json.dumps({'node':sys.argv[1],'iteration':int(sys.argv[2]),'start_wall':float(sys.argv[3]),'end_wall':float(sys.argv[4]),'log':sys.argv[5],'rc':int(sys.argv[6]),'load':open('/proc/loadavg').read().split()[:3] if __import__('os').path.exists('/proc/loadavg') else None}))" \
      "$node" "$iteration" "$start" "$end" "$(basename "$log")" "$rc" >> "$out/walls.jsonl"
    echo "iteration $iteration rc=$rc $node"
  done
done
rm -f scripts/perf/out/.active
"${PY[@]}" scripts/perf/nx049_report.py "$out"
echo "summary: $out/summary.md  $out/summary.json"
