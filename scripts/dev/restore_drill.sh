#!/usr/bin/env bash
# Development machine only: the source side of the AT-072 local restore drill (docs/release/at-072-local-restore-drill.md).
#
#   scripts/dev/restore_drill.sh        # API https://127.0.0.1:8753 (NEXLOOP_DRILL_API_PORT); files in .dev-drill/
#   scripts/dev/restore_drill.sh --stop # stop exactly the source PostgreSQL recorded in .dev-drill/pg-root and remove its directory
#
# Starts a disposable WAL-archived PostgreSQL 18 cluster through the test fixtures (bootstrap + synthetic tenant, one customer
# conversation, owner and operator workbench members via the configurator) and serves the production workbench API on
# loopback HTTPS (self-signed one-day certificate, REAL_DISPATCH_ENABLED=false, deterministic test profile). The drill's
# private files (role connection strings, accounts, authority key, certificate, stack.json) are in .dev-drill/ (0600,
# git-ignored); nothing secret is printed. Keep it running until the restore has finished: the WAL archive lives in the
# cluster's own temp directory. Ctrl-C, SIGTERM or SIGHUP (also through a pipe such as `| tee`) stops the API and PostgreSQL
# and removes the cluster's temp directory; .dev-drill/ (with the backup, the journal, the restored instance and the reports)
# is kept for the evidence and removed by hand at the end of the drill, after --stop or the check of the runbook §9.
# PostgreSQL runs in its own process group, so a SIGKILL of this script or of its process group would leave it running: a
# watchdog in its own session (scripts/dev/restore_drill_stop.py watch) stops exactly the recorded cluster once this script
# or the holder is gone and the holder's own teardown did not remove it within the grace period.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$ROOT"
if [[ "${1:-}" == --stop ]]; then
  PYTHON=$(uv run --frozen python -c 'import sys; print(sys.executable)')
  if out=$("$PYTHON" scripts/dev/restore_drill_stop.py stop --drill-dir "${NEXLOOP_DRILL_DIR:-$ROOT/.dev-drill}"); then
    echo "restore drill: stopped ($out)"; exit 0
  fi
  echo "restore drill: not stopped ($out)" >&2; exit 1
fi
PORT="${NEXLOOP_DRILL_API_PORT:-8753}"
DIR="${NEXLOOP_DRILL_DIR:-$ROOT/.dev-drill}"  # tests point it at their own temporary directory
export LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 REAL_DISPATCH_ENABLED=false
export PYTHONPATH="$ROOT/packages/eios-core/src:$ROOT/tests"
export NEXLOOP_DRILL_DIR="$DIR" NEXLOOP_DRILL_API_PORT="$PORT"
if [[ -e "$DIR" ]]; then
  echo "restore drill: $DIR exists (an earlier drill); keep its evidence elsewhere and remove it first" >&2
  exit 1
fi
mkdir -m 700 "$DIR" "$DIR/tmp"
PG_BIN="${NEXLOOP_TEST_PG_BIN:-/opt/homebrew/opt/postgresql@18/bin}"
export NEXLOOP_TEST_PG_BIN="$PG_BIN"
child=''
cleanup() {
  trap - EXIT INT TERM
  if [[ -n "$child" ]] && kill -0 "$child" 2>/dev/null; then
    kill -TERM "$child" 2>/dev/null || true
    for _ in $(seq 1 60); do kill -0 "$child" 2>/dev/null || break; sleep 0.5; done
    kill -0 "$child" 2>/dev/null && kill -KILL "$child" 2>/dev/null || true
  fi
  wait "$child" 2>/dev/null || true
  # Fallback: the exact cluster the holder recorded, only if its teardown did not run.
  if [[ -s "$DIR/pg-root" ]]; then
    pg_root=$(cat "$DIR/pg-root")
    if [[ "$(basename "$pg_root")" == nexloop-pg-* && -d "$pg_root/data" ]]; then
      "$PG_BIN/pg_ctl" -D "$pg_root/data" -m fast -w stop >/dev/null 2>&1 || true
      rm -rf "$pg_root"
    fi
  fi
  rm -rf "$DIR/tmp"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
# Through `| tee`: a hang-up kills tee, and the next echo would end this shell by SIGPIPE before the EXIT trap. Ignored, the
# write just fails, `set -e` exits and the EXIT trap still stops the holder.
trap '' PIPE

echo "restore drill: starting the archived PostgreSQL and the API (synthetic data only)"
PYTHON=$(uv run --frozen python -c 'import sys; print(sys.executable)')
# NEXLOOP_DRILL_HOLDER replaces the holder command in tests/test_restore_drill_script.py only (a stand-in without PostgreSQL).
if [[ -n "${NEXLOOP_DRILL_HOLDER:-}" ]]; then
  "$NEXLOOP_DRILL_HOLDER" >"$DIR/server.log" 2>&1 &
else
  "$PYTHON" -m pytest -q -s -p no:xdist -p no:randomly -p no:cacheprovider --basetemp "$DIR/tmp" \
    scripts/dev/restore_drill_hold.py >"$DIR/server.log" 2>&1 &
fi
child=$!
# The watchdog starts now, before the cluster exists, so it is already in its own session when a signal can matter.
"$PYTHON" scripts/dev/restore_drill_stop.py watch --drill-dir "$DIR" --pg-bin "$PG_BIN" --pid "$$" --pid "$child" --holder "$child" \
  --grace "${NEXLOOP_DRILL_WATCH_GRACE:-45}"
for _ in $(seq 1 240); do
  [[ -s "$DIR/ready" ]] && break
  if ! kill -0 "$child" 2>/dev/null; then
    echo "restore drill: setup failed; see the last lines of the log:" >&2
    tail -n 30 "$DIR/server.log" >&2
    exit 1
  fi
  sleep 0.5
done
[[ -s "$DIR/ready" ]] || { echo "restore drill: not ready after 120 s" >&2; exit 1; }
echo "restore drill: source ready"
echo "  API:      $(cat "$DIR/ready")"
echo "  files:    $DIR (stack.json, *.dsn, credentials.json; passwords only in that file)"
echo "  stop:     Ctrl-C, only after the restore has finished; check or force with: scripts/dev/restore_drill.sh --stop"
wait "$child"
