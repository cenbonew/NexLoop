#!/bin/bash
# S6 / NX-037 speaking with a readiness handshake (S69, after S6 rerun dc7645a9 SEQ 1 timed out). Example only: copy it into the
# private run directory and fill the environment there; it holds no account, host or path of any deployment.
#
#   bash speak-when-ready.sh <SEQ> -- <speak command and its arguments>
#
# The run entry (scripts/nx037/run_interactions.py --driver real) gives each interaction SEQ --interaction-timeout seconds
# (default 900) counted from its prompt; a SEQ not done by then is interaction_timeout and the trial is paused. So the observer is
# started or resumed only when the speaker is ready:
#   1. this script writes the readiness marker  $READY_DIR/speaker-ready  (content: SEQ and time) on APP_HOST;
#   2. the dispatcher's watcher sees the marker and starts the run entry (first SEQ) or resumes it (--resume REF, with
#      --void-seq N --void-confirmation REF after a timeout); a resume appends a line to $TRIAL_DIR/resumes.jsonl;
#   3. this script waits for that: the prompt line of SEQ in $TRIAL_DIR/prompts.jsonl written after the marker (and, when it
#      expects a resume, a new resumes.jsonl line), then runs the speak command;
#   4. only when the speak command exits 0 it creates $TRIAL_DIR/operator-done/<SEQ> and removes the marker.
# It never starts, resumes or voids anything itself: those stay the dispatcher's, with the owner's confirmation reference.
#
# Environment (all required unless marked):
#   APP_SSH      ssh target of APP_HOST (an alias of your own ssh config)
#   TRIAL_DIR    the run entry's --out on APP_HOST
#   READY_DIR    directory of the readiness marker on APP_HOST
#   EXPECT_RESUME  optional, 1 when this SEQ follows a pause (waits for a new resumes.jsonl line as well)
#   READY_WAIT_SECONDS  optional, how long to wait for the prompt (default 600); on expiry nothing is spoken (exit 4)
set -euo pipefail
SEQ=${1:?SEQ}; shift
[ "${1:-}" = "--" ] && shift
[ "$#" -gt 0 ] || { echo "usage: speak-when-ready.sh <SEQ> -- <speak command>" >&2; exit 64; }
: "${APP_SSH:?ssh target of APP_HOST}" "${TRIAL_DIR:?run entry --out on APP_HOST}" "${READY_DIR:?readiness marker directory on APP_HOST}"
case "$SEQ" in ''|*[!0-9]*) echo "SEQ must be a number" >&2; exit 64;; esac
WAIT=${READY_WAIT_SECONDS:-600}

remote() { ssh "$APP_SSH" "$@"; }
count_resumes() { remote "sudo -n sh -c 'test -f \"$TRIAL_DIR/resumes.jsonl\" && wc -l < \"$TRIAL_DIR/resumes.jsonl\" || echo 0'" | tr -d ' '; }
prompt_after() {  # 0 when prompts.jsonl has a line for SEQ written after the marker (the file grew past its size at marking)
  remote "sudo -n sh -c 'test -f \"$TRIAL_DIR/prompts.jsonl\" && tail -c +$(( PROMPT_BYTES + 1 )) \"$TRIAL_DIR/prompts.jsonl\"'" 2>/dev/null \
    | python3 -c 'import json,sys; s=int(sys.argv[1]); sys.exit(0 if any(json.loads(l).get("seq")==s for l in sys.stdin if l.strip()) else 1)' "$SEQ"
}

RESUMES=$(count_resumes)
PROMPT_BYTES=$(remote "sudo -n sh -c 'test -f \"$TRIAL_DIR/prompts.jsonl\" && wc -c < \"$TRIAL_DIR/prompts.jsonl\" || echo 0'" | tr -d ' ')
remote "sudo -n sh -c 'mkdir -p \"$READY_DIR\" && printf \"seq=%s at=%s\n\" $SEQ \"\$(date -u +%Y-%m-%dT%H:%M:%SZ)\" > \"$READY_DIR/speaker-ready\"'"
echo "speaker-ready seq=$SEQ written; waiting for the observer" >&2

deadline=$(( $(date +%s) + WAIT ))
until { [ "${EXPECT_RESUME:-0}" != 1 ] || [ "$(count_resumes)" -gt "$RESUMES" ]; } && prompt_after; do
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "no prompt for seq=$SEQ within ${WAIT}s: nothing spoken (marker left for the dispatcher)" >&2; exit 4
  fi
  sleep 5
done
echo "prompt seq=$SEQ seen: speaking" >&2

"$@"   # the speak command (for S6: speak-turn.sh without its own done marker); a nonzero exit leaves SEQ open to rerun

remote "sudo -n sh -c 'touch \"$TRIAL_DIR/operator-done/$SEQ\" && rm -f \"$READY_DIR/speaker-ready\"'"
echo "operator-done/$SEQ" >&2
