#!/usr/bin/env bash
# NX-031 full matrix run on the CI lane (design §5, D8, D9). The dispatcher runs it; it is never part of scripts/ci/check.
#
#   scripts/load/run_on_lane.sh [--outside-window] [--prune-old] [--resume RUN_ID]
#
# --resume RUN_ID continues an interrupted run in .ci-results/load/RUN_ID at the same commit: a complete template is reused,
# completed cells are kept, an incomplete cell's directory is moved to attempts/<cell>.<n> (never overwritten) and the cell
# runs again (matrix.py --resume); run.json records each attempt. See docs/implementation/NX-031-lane-runbook.md.
#
# What it does, in order, holding the CI resource lock for the whole run:
#   1. preflight: night window (LOAD_WINDOW, default 22:00-06:00 local), lock, clean checkout, tools, disk, and the §5.1
#      safety rules (no deployment env, REAL_DISPATCH_ENABLED=false, no model/channel credentials in the environment);
#   2. template: L4's build_template.sh (fixed seed, governed loading, pg_basebackup template);
#   3. matrix: L4's matrix runner (one disposable cluster per cell) writing the results contract
#      (docs/implementation/NX-031-results-contract.md);
#   4. report: scripts/load/report.py → report.json / report.md (numbers, verdicts, seed, digests, commit only).
# On failure the run directory is kept with lane-summary.json and failure-summary.md (step, exit code, timings, cells
# present, load, disk, leftovers), and every process and PostgreSQL cluster under the run directory is stopped.
#
# Parameters (environment; the values live on the lane, never in the repository):
#   NEXLOOP_CI_LOCK      required  lock file shared with the regular CI (one build at a time, 12 §9)
#   NEXLOOP_TEST_PG_BIN  required  PostgreSQL 18 bin directory
#   LOAD_WINDOW          optional  HH:MM-HH:MM local night window (default 22:00-06:00)
#   LOAD_LOCK_WAIT       optional  seconds to wait for the lock (default 1800)
#   LOAD_SEED / LOAD_CONSUMERS     default 31 / 1000
#   LOAD_MATRIX          optional  matrix file (default deploy/load/matrix.v1.json)
#   LOAD_TEMPLATE_CMD    optional  template builder (default scripts/load/build_template.sh)
#   LOAD_RUNNER_CMD      optional  matrix runner (default scripts/load/matrix.py)
#   LOAD_CI_BASELINE     optional  JSON of the day's last regular full CI (run_id, wall_seconds, deadline_gate_failures, failed)
#   LOAD_MIN_FREE_GIB    optional  free disk required under .ci-results (default 20)
#   LOAD_RELAY_PROCESSES optional  relay processes of the message chain, 1..3 (default 1; dispatcher 2026-10-13)
# Exit codes: 0 report written (read AT-060 from it), 1 a step failed, 2 preflight refused, 75 lock busy.
set -uo pipefail

outside_window=0; prune=0; resume=''
while [[ $# -gt 0 ]]; do
  case "$1" in
    --outside-window) outside_window=1; shift ;;
    --prune-old) prune=1; shift ;;
    --resume) [[ $# -ge 2 && "$2" =~ ^20[0-9]{6}T[0-9]{6}Z-[0-9a-f]{7}$ ]] || { echo "--resume needs a run id" >&2; exit 2; }; resume=$2; shift 2 ;;
    *) echo "usage: $0 [--outside-window] [--prune-old] [--resume RUN_ID]" >&2; exit 2 ;;
  esac
done

root=$(git -C "$(dirname "$0")/../.." rev-parse --show-toplevel) || exit 2
cd "$root" || exit 2
refuse() { echo "lane preflight refused: $1" >&2; exit 2; }
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
secs() { date -u +%s; }

# ---- 1. preflight -----------------------------------------------------------------------------------------------------
[[ -n "${NEXLOOP_CI_LOCK:-}" ]] || refuse "NEXLOOP_CI_LOCK is not set"
[[ -n "${NEXLOOP_TEST_PG_BIN:-}" && -x "$NEXLOOP_TEST_PG_BIN/initdb" ]] || refuse "NEXLOOP_TEST_PG_BIN has no initdb"
window="${LOAD_WINDOW:-22:00-06:00}"
[[ "$window" =~ ^([0-2][0-9]):([0-5][0-9])-([0-2][0-9]):([0-5][0-9])$ ]] || refuse "LOAD_WINDOW must be HH:MM-HH:MM"
if [[ $outside_window -eq 0 ]]; then
  from=$((10#${BASH_REMATCH[1]}*60+10#${BASH_REMATCH[2]})); to=$((10#${BASH_REMATCH[3]}*60+10#${BASH_REMATCH[4]}))
  minute=$((10#$(date +%H)*60+10#$(date +%M)))
  if (( from <= to )); then inside=$(( minute >= from && minute < to )); else inside=$(( minute >= from || minute < to )); fi
  (( inside )) || refuse "outside the night window $window (use --outside-window only when the dispatcher decides so)"
fi
# §5.1: no deployment configuration, no real dispatch, no real model or channel credentials, loopback only.
for name in DATABASE_URL NEX_EIOS_DATABASE_URL NEXLOOP_DATABASE_URL PGHOST PGPORT PGDATABASE PGUSER PGPASSWORD PGSERVICE COMPOSE_ENV_FILES NEXLOOP_ENV_FILE; do
  [[ -z "${!name:-}" ]] || refuse "$name is set (the lane uses only its own disposable clusters)"
done
while IFS='=' read -r name _; do
  case "$name" in MODEL_*|EMBEDDING_*|*_API_KEY|*_TOKEN|*_SECRET|DEEPSEEK_*|OPENAI_*|ANTHROPIC_*) refuse "$name is set (no real model or channel credentials on the lane)" ;; esac
done < <(env)
[[ "${REAL_DISPATCH_ENABLED:-false}" == false ]] || refuse "REAL_DISPATCH_ENABLED must be false"
export REAL_DISPATCH_ENABLED=false NEXLOOP_TEST_GUARD_WORKERS=4
[[ -z "$(git status --porcelain --untracked-files=no)" ]] || refuse "the checkout has local changes (results must belong to one commit)"
commit=$(git rev-parse HEAD)
command -v uv >/dev/null || refuse "uv not on PATH"
command -v flock >/dev/null || refuse "flock not on PATH"
template_cmd="${LOAD_TEMPLATE_CMD:-scripts/load/build_template.sh}"; runner_cmd="${LOAD_RUNNER_CMD:-scripts/load/matrix.py}"
matrix="${LOAD_MATRIX:-deploy/load/matrix.v1.json}"
[[ -e "$template_cmd" ]] || refuse "template builder $template_cmd not found"
[[ -e "$runner_cmd" ]] || refuse "matrix runner $runner_cmd not found (L4, NX-031 slice 3)"
[[ -f "$matrix" ]] || refuse "matrix file $matrix not found"
[[ -z "${LOAD_CI_BASELINE:-}" || -f "$LOAD_CI_BASELINE" ]] || refuse "LOAD_CI_BASELINE is not a file"
mkdir -p .ci-results/load
free_gib=$(df -Pk .ci-results/load | awk 'NR==2{print int($4/1048576)}')
[[ "${LOAD_RELAY_PROCESSES:-1}" =~ ^[123]$ ]] || refuse "LOAD_RELAY_PROCESSES must be 1, 2 or 3"
(( free_gib >= ${LOAD_MIN_FREE_GIB:-20} )) || refuse "only ${free_gib} GiB free under .ci-results/load"

# ---- lock (held until this process exits) ------------------------------------------------------------------------------
exec 9>>"$NEXLOOP_CI_LOCK" || refuse "cannot open the CI lock"
if ! flock -w "${LOAD_LOCK_WAIT:-1800}" 9; then echo "CI lock busy after ${LOAD_LOCK_WAIT:-1800} s" >&2; exit 75; fi

if [[ -n "$resume" ]]; then
  run_id=$resume; run_dir="$root/.ci-results/load/$run_id"
  [[ -d "$run_dir" ]] || refuse "no run directory for $run_id"
  [[ "${run_id##*-}" == "${commit:0:7}" ]] || refuse "resume needs the interrupted run's commit (${run_id##*-}), checkout is ${commit:0:7}"
  # The earlier failure record stays beside the new one.
  stamp=$(date -u +%H%M%S)
  for f in lane-summary.json failure-summary.md logs; do [[ -e "$run_dir/$f" ]] && mv "$run_dir/$f" "$run_dir/$f.before-resume-$stamp"; done
  if [[ -f "$run_dir/template/template.json" && -d "$run_dir/template/pgdata" ]]; then template_done=1
  elif [[ -e "$run_dir/template" ]]; then mv "$run_dir/template" "$run_dir/template.incomplete-$stamp"; fi
else
  run_id="$(date -u +%Y%m%dT%H%M%SZ)-${commit:0:7}"
  run_dir="$root/.ci-results/load/$run_id"
fi
mkdir -p "$run_dir/logs" "$run_dir/lane" || exit 1
chmod 0700 "$run_dir"
started=$(secs); step=preflight; status=running; code=0; durations=''

summary() {
  # Numbers, codes and names only (D8); stays in the ignored run directory.
  local cells; cells=$(cd "$run_dir" && ls -1 cells 2>/dev/null | paste -sd, -)
  local leftovers; leftovers=$(pgrep -f -- "$run_dir" 2>/dev/null | grep -vc "^$$\$" || true)
  printf '{"run_id":"%s","commit":"%s","status":"%s","step":"%s","exit_code":%d,"started_at":%d,"finished_at":%d,"seconds":{%s},"cells":"%s","load":"%s","free_gib":%d,"leftover_processes":%d}\n' \
    "$run_id" "$commit" "$status" "$step" "$code" "$started" "$(secs)" "${durations%,}" "${cells}" \
    "$(uptime | sed -E 's/.*load averages?: //; s/,//g')" "$(df -Pk "$run_dir" | awk 'NR==2{print int($4/1048576)}')" "${leftovers:-0}" > "$run_dir/lane-summary.json"
}

stop_leftovers() {
  # Only what this run started: PostgreSQL clusters and processes whose command line names this run directory.
  local pgdata
  while IFS= read -r pid_file; do
    pgdata=$(dirname "$pid_file")
    "$NEXLOOP_TEST_PG_BIN/pg_ctl" -D "$pgdata" -m fast -w -t 30 stop >/dev/null 2>&1 || true
  done < <(find "$run_dir" -name postmaster.pid -type f 2>/dev/null)
  local pids; pids=$(pgrep -f -- "$run_dir" | grep -v "^$$\$" || true)
  if [[ -n "$pids" ]]; then kill $pids 2>/dev/null || true; sleep 5; pids=$(pgrep -f -- "$run_dir" | grep -v "^$$\$" || true); [[ -z "$pids" ]] || kill -9 $pids 2>/dev/null || true; fi
}

finish() {
  local exit_code=$?
  if [[ $status == running ]]; then status=failed; [[ $code -ne 0 ]] || code=$exit_code; fi
  stop_leftovers
  summary
  if [[ $status != completed ]]; then
    {
      echo "# NX-031 lane run $run_id: $status"
      echo
      echo "- step: $step, exit code: $code, commit: $commit"
      echo "- step logs: $run_dir/logs/ (local only; not for the repository)"
      echo "- cells present: $(cd "$run_dir" && ls -1 cells 2>/dev/null | paste -sd, - || true)"
      echo
      echo '## last 40 lines of the failing step log'
      echo
      echo '```'
      tail -n 40 "$run_dir/logs/$step.log" 2>/dev/null || echo '(no log)'
      echo '```'
    } > "$run_dir/failure-summary.md"
  fi
  echo "lane run $run_id: $status (step $step, exit $code) → $run_dir"
}
trap finish EXIT
trap 'code=130; exit 130' INT TERM

step_run() {
  # step_run <name> <command...>: logs to logs/<name>.log, records seconds, stops the run on failure.
  step=$1; shift
  local t0; t0=$(secs)
  "$@" > "$run_dir/logs/$step.log" 2>&1
  code=$?
  durations="${durations}\"$step\":$(( $(secs) - t0 )),"
  if [[ $code -ne 0 ]]; then status=failed; exit 1; fi
}

# ---- 2–4. template, matrix, report -------------------------------------------------------------------------------------
[[ "${template_done:-0}" -eq 1 ]] || step_run template "$template_cmd" --seed "${LOAD_SEED:-31}" --consumers "${LOAD_CONSUMERS:-1000}" --out "$run_dir/template"
export LOAD_CI_COMMAND="scripts/ci/check"   # the C4-ci cell runs the full CI inside this lock (design §5.2)
step_run matrix uv run --frozen python "$runner_cmd" --template "$run_dir/template" --matrix "$matrix" --out "$run_dir" --lane-root "$run_dir/lane" --relay-processes "${LOAD_RELAY_PROCESSES:-1}" ${resume:+--resume}
report_args=("$run_dir" --out "$run_dir")
[[ -z "${LOAD_CI_BASELINE:-}" ]] || report_args+=(--ci-baseline "$LOAD_CI_BASELINE")
step_run report uv run --frozen python scripts/load/report.py "${report_args[@]}"
step=done; status=completed; code=0

if [[ $prune -eq 1 ]]; then
  # D8: raw samples are kept 30 days. Only run directories of this lane (name pattern) are removed, and each one is listed.
  find "$root/.ci-results/load" -mindepth 1 -maxdepth 1 -type d -name '20[0-9][0-9][0-1][0-9][0-3][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z-*' -mtime +30 -print -exec rm -rf {} +
fi
exit 0
