#!/usr/bin/env bash
# Development machine only: one-command workbench smoke environment (NX-028 page integration).
#
#   scripts/dev/workbench_smoke.sh     # Web http://127.0.0.1:8744 (NEXLOOP_DEV_SMOKE_WEB_PORT), API 8743 (NEXLOOP_DEV_SMOKE_PORT)
#   NEXLOOP_DEV_SMOKE_SEED=large scripts/dev/workbench_smoke.sh   # plus 40 synthetic customers, multi-row tables, a 60-turn conversation
#                                                                 # (tests/smoke_seed.py; layout batch 2 acceptance)
#
# Starts a disposable PostgreSQL through the test fixtures (bootstrap + synthetic tenant, one owner and one operator
# workbench member via the configurator, one customer conversation with one inbound message), serves the production API on
# https://127.0.0.1:<api port> (loopback only, self-signed one-day certificate, REAL_DISPATCH_ENABLED=false, deterministic
# test profile) and the Web dev server on http://127.0.0.1:<web port>, which proxies /api to the API (loopback HTTP is a
# secure context for the browser; the self-signed certificate is accepted only by the proxy). Accounts and random passwords
# are in .dev-smoke/credentials.json (mode 0600, git-ignored); nothing secret is printed. Ctrl-C / SIGTERM stops both servers
# and removes .dev-smoke/.
#
# Shutdown: SIGTERM goes to the holder (pytest) itself, so uvicorn stops and the fixtures' teardown stops PostgreSQL and
# removes its temp directory. If the holder does not exit within 30 s (or was killed), the exact cluster it recorded in
# .dev-smoke/pg-root is stopped with `pg_ctl stop -m fast` and that directory removed. Manual check after a run:
#   kill -TERM <script pid>; pgrep -fl 'postgres -D .*/nexloop-pg-'   # prints nothing
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$ROOT"
PORT="${NEXLOOP_DEV_SMOKE_PORT:-8743}"
WEB_PORT="${NEXLOOP_DEV_SMOKE_WEB_PORT:-8744}"
DIR="$ROOT/.dev-smoke"
export LC_ALL=C.UTF-8 LANG=C.UTF-8 REAL_DISPATCH_ENABLED=false
export PYTHONPATH="$ROOT/packages/eios-core/src:$ROOT/tests"
export NEXLOOP_DEV_SMOKE_DIR="$DIR" NEXLOOP_DEV_SMOKE_PORT="$PORT" NEXLOOP_DEV_SMOKE_WEB="$DIR/web"
case "${NEXLOOP_DEV_SMOKE_SEED:-}" in ""|large) ;; *) echo "workbench smoke: NEXLOOP_DEV_SMOKE_SEED must be empty or large" >&2; exit 1;; esac
export NEXLOOP_DEV_SMOKE_SEED="${NEXLOOP_DEV_SMOKE_SEED:-}"

if [[ -e "$DIR" ]]; then
  echo "workbench smoke: $DIR exists (another run, or an unclean exit); stop that run or remove the directory" >&2
  exit 1
fi
mkdir -m 700 "$DIR" "$DIR/tmp"
PG_BIN="${NEXLOOP_TEST_PG_BIN:-/opt/homebrew/opt/postgresql@18/bin}"
child='';web=''
stop_pid() {  # TERM, wait up to $2 seconds, then KILL
  local pid=$1 limit=$2
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null || return 0
  kill -TERM "$pid" 2>/dev/null || true
  for _ in $(seq 1 $((limit * 2))); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
  kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}
cleanup() {
  trap - EXIT INT TERM
  stop_pid "$web" 10
  # The holder's own teardown stops PostgreSQL; give it time.
  stop_pid "$child" 30
  # Fallback: the exact cluster the holder recorded, only if its teardown did not run.
  if [[ -s "$DIR/pg-root" ]]; then
    pg_root=$(cat "$DIR/pg-root")
    tmp=$(cd "${TMPDIR:-/tmp}" && pwd -P)
    if [[ "$(basename "$pg_root")" == nexloop-pg-* && "$(cd "$(dirname "$pg_root")" 2>/dev/null && pwd -P)" == "$tmp" && -d "$pg_root/data" ]]; then
      echo "workbench smoke: holder did not stop PostgreSQL; stopping it" >&2
      "$PG_BIN/pg_ctl" -D "$pg_root/data" -m fast -w stop >/dev/null 2>&1 || true
      rm -rf "$pg_root"
    fi
  fi
  # Only the directory this script created.
  [[ "$DIR" == "$ROOT/.dev-smoke" ]] && rm -rf "$DIR"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ -s "$HOME/.nvm/nvm.sh" ]]; then
  # shellcheck disable=SC1091
  source "$HOME/.nvm/nvm.sh" >/dev/null && nvm use 24 >/dev/null
fi
echo "workbench smoke: building the Web (also served by the API itself)"
(cd apps/web && npx vite build --outDir "$DIR/web" --emptyOutDir >/dev/null)

echo "workbench smoke: starting PostgreSQL and the API (synthetic data only)"
# The project interpreter itself (not the `uv run` wrapper), so the holder is the process that receives SIGTERM.
PYTHON=$(uv run --frozen python -c 'import sys; print(sys.executable)')
"$PYTHON" -m pytest -q -s -p no:xdist -p no:randomly -p no:cacheprovider --basetemp "$DIR/tmp" \
  scripts/dev/workbench_smoke_hold.py >"$DIR/server.log" 2>&1 &
child=$!
for _ in $(seq 1 240); do
  [[ -s "$DIR/ready" ]] && break
  if ! kill -0 "$child" 2>/dev/null; then
    echo "workbench smoke: setup failed; see the last lines of the log:" >&2
    tail -n 30 "$DIR/server.log" >&2
    exit 1
  fi
  sleep 0.5
done
[[ -s "$DIR/ready" ]] || { echo "workbench smoke: not ready after 120 s" >&2; exit 1; }
NEXLOOP_DEV_SMOKE_API_ORIGIN=$(cat "$DIR/ready") NEXLOOP_DEV_SMOKE_WEB_PORT="$WEB_PORT" \
  apps/web/node_modules/.bin/vite --config scripts/dev/vite.smoke.config.mjs >"$DIR/web.log" 2>&1 &
web=$!
for _ in $(seq 1 60); do
  curl -fsS -o /dev/null "http://127.0.0.1:$WEB_PORT/workbench" 2>/dev/null && break
  kill -0 "$web" 2>/dev/null || { echo "workbench smoke: Web dev server failed" >&2; tail -n 20 "$DIR/web.log" >&2; exit 1; }
  sleep 0.5
done
echo "workbench smoke: ready"
echo "  workbench:   http://127.0.0.1:$WEB_PORT/workbench   (owner / operator)"
echo "  WebChat:     http://127.0.0.1:$WEB_PORT/            (customer)"
echo "  accounts:    $DIR/credentials.json (passwords only in that file)"
echo "  stop:        Ctrl-C (removes .dev-smoke/)"
wait "$child"
