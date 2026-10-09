#!/usr/bin/env bash
# Measurement-only authorization profile. Usage:
#   scripts/perf/run_profile.sh <label> <pytest node ids...>
# Runs the given tests serially on disposable PostgreSQL (tests/conftest.py) with
# scripts/perf/hooks on PYTHONPATH, then summarizes into scripts/perf/out/<label>/.
# Product code is not modified; hooks are monkeypatches inside the test processes.
set -euo pipefail
cd "$(dirname "$0")/../.."
label="$1"; shift
out="$PWD/scripts/perf/out/$label"; rm -rf "$out"; mkdir -p "$out"
printf '%s\n' "$out" > scripts/perf/out/.active
rm -f scripts/perf/out/.nocache; [ "${NEXLOOP_PERF_NO_FACT_CACHE:-0}" = 1 ] && touch scripts/perf/out/.nocache
source ~/.nvm/nvm.sh >/dev/null && nvm use 24 >/dev/null
export LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8
export PYTHONPATH="$PWD/scripts/perf/hooks:$PWD/packages/eios-core/src:$PWD/tests"
export NEXLOOP_PERF_OUT="$out"
status=0
for node in "$@"; do
  start=$(python3 -c 'import time;print(time.time())')
  log="$out/pytest-$(echo "$node" | tr -c 'A-Za-z0-9_.-' '_' | tail -c 120).log"
  rc=0; uv run --frozen pytest -q -p perf_plugin "$node" --tb=short -p no:cacheprovider > "$log" 2>&1 || rc=$?; [ $rc -eq 0 ] || status=$rc
  end=$(python3 -c 'import time;print(time.time())')
  python3 -c "import json,sys;print(json.dumps({'node':sys.argv[1],'wall_seconds':float(sys.argv[3])-float(sys.argv[2]),'log':sys.argv[4],'rc':int(sys.argv[5])}))" "$node" "$start" "$end" "$(basename "$log")" "$rc" >> "$out/walls.jsonl"
done
rm -f scripts/perf/out/.active scripts/perf/out/.nocache
uv run --frozen python scripts/perf/summarize.py "$out" > "$out/summary.md"
echo "summary: $out/summary.md (last pytest status $status)"
