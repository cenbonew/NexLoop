#!/usr/bin/env bash
# Simulation process group stage enablement on DATA_HOST (docs/implementation/sim-stage-enable.md): the pg_hba line of the simulation
# login roles (hostssl from APP_HOST, stage catalog, SCRAM), rendered from deploy/stage/config/pg_hba.conf.template like every other
# rule. It may install the evolution local line in the same run (scripts/deploy/evolution_hba_data_host.sh) - one owner run, one reload.
# Owner command (config/pg_hba.conf is root's), one run, one password:
#     sudo bash sim_hba_data_host.sh <private config>
#   <private config>: a 0600 file of KEY=VALUE lines, exactly NEXLOOP_ROOT, STAGE_ENV and RELEASE_CHECKOUT (read, never sourced).
# Steps (each stops the run on failure; nothing is printed but codes and counts):
#   1 preflight: the newly rendered pg_hba differs from the installed one by the simulation hostssl line (and possibly the evolution
#     local line) added, and removes none
#   2 write the new pg_hba in place (same inode; old content kept as .before-sim), check the container sees it, HUP PostgreSQL, check
#     the reload; otherwise the old content is written back in place and reloaded
# Passwords are not set here (the bootstrap window does that); without a password the line admits nobody.
set -euo pipefail
PROJECT=nexloop-data
# Rewrite a single-file bind mount in place: same inode, owner and mode (the container sees the new content); refuses if the inode
# changed (for example a link replaced by an editor). Same functions as metrics_data_host.sh.
inode_of() { ls -di "$1" | awk '{print $1}'; }
write_in_place() { local src=$1 dst=$2 inode; inode=$(inode_of "$dst"); cat "$src" > "$dst"; [ "$(inode_of "$dst")" = "$inode" ]; }
step() { printf '%s\n' "step $1: $2"; }
fail() { printf '%s\n' "FAILED step $1: $2" >&2; exit 1; }
# KEY=VALUE, exactly the three keys, absolute paths without spaces or shell characters; the file 0600 and the caller's.
read_config() {
  local file=$1 line key value
  [ -f "$file" ] && [ ! -L "$file" ] || fail 0 "config: not a regular file"
  [ "$(stat -c '%a %u' "$file" 2>/dev/null || stat -f '%Lp %u' "$file")" = "600 $(id -u)" ] || fail 0 "config: must be 0600 and the caller's"
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    key=${line%%=*}; value=${line#*=}
    case "$key" in NEXLOOP_ROOT|STAGE_ENV|RELEASE_CHECKOUT) ;; *) fail 0 "config: unknown key" ;; esac
    [[ "$value" =~ ^/[A-Za-z0-9._/-]+$ ]] && [[ "$value" != *..* ]] || fail 0 "config: $key must be an absolute path"
    printf -v "$key" '%s' "$value"
  done < "$file"
  [ -n "${NEXLOOP_ROOT:-}" ] && [ -n "${STAGE_ENV:-}" ] && [ -n "${RELEASE_CHECKOUT:-}" ] || fail 0 "config: NEXLOOP_ROOT, STAGE_ENV and RELEASE_CHECKOUT required"
}
# The rules of a pg_hba file: comments and blank lines dropped, whitespace collapsed.
rules() { grep -v '^[[:space:]]*#' "$1" | sed '/^[[:space:]]*$/d' | tr -s ' \t' ' '; }
EVOLUTION='^local [a-z_][a-z0-9_]* nexloop_evo_reader,nexloop_evo_writer,nexloop_evo_evaluator scram-sha-256$'
SIM='^hostssl [a-z_][a-z0-9_]* nexloop_sim_worker,nexloop_sim_guard,nexloop_sim_api,nexloop_stand_in_worker,nexloop_sim_files,nexloop_twin_port [0-9.]+/32 scram-sha-256$'

if [ "${SIM_HBA_LIB:-}" = 1 ]; then return 0 2>/dev/null || exit 0; fi
CONFIG=${1:?usage: sudo bash sim_hba_data_host.sh <private config>}
[ "$(id -u)" -eq 0 ] || fail 0 "run with sudo"
read_config "$CONFIG"
HBA="$NEXLOOP_ROOT/config/pg_hba.conf"

step 1 preflight
for f in "$HBA" "$STAGE_ENV" "$RELEASE_CHECKOUT/scripts/deploy/render.py" "$RELEASE_CHECKOUT/deploy/stage/config/pg_hba.conf.template"; do
  [ -f "$f" ] || fail 1 "missing $(basename "$f")"; done
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT; chmod 0700 "$WORK"
cp "$STAGE_ENV" "$WORK/stage.env"; chmod 0600 "$WORK/stage.env"
python3 "$RELEASE_CHECKOUT/scripts/deploy/render.py" --env "$WORK/stage.env" --template "$RELEASE_CHECKOUT/deploy/stage/config/pg_hba.conf.template" \
  --out "$WORK/pg_hba.conf" >/dev/null || fail 1 "pg_hba render refused"
removed=$(diff <(rules "$HBA") <(rules "$WORK/pg_hba.conf") | grep -c '^<' || true)
added=$(diff <(rules "$HBA") <(rules "$WORK/pg_hba.conf") | sed -n 's/^> //p')
[ "$removed" -eq 0 ] || fail 1 "the rendered pg_hba removes $removed rule line(s); render with the deployed stage.env and a release checkout with the simulation line"
if [ -z "$added" ] && grep -Eq "$SIM" <(rules "$HBA"); then
  printf '%s\n' "done: the simulation line is already installed (nothing to do)"; exit 0; fi
count=$(printf '%s\n' "$added" | sed '/^$/d' | wc -l | tr -d ' ')
[ "$count" -ge 1 ] && [ "$count" -le 2 ] || fail 1 "expected the simulation line (and at most the evolution line) added, found $count"
printf '%s\n' "$added" | grep -Eq "$SIM" || fail 1 "the simulation hostssl line is not among the added rules"
printf '%s\n' "$added" | sed '/^$/d' | while IFS= read -r rule; do
  printf '%s\n' "$rule" | grep -Eq "$SIM|$EVOLUTION" || { printf '%s\n' "FAILED step 1: an added rule is neither the simulation nor the evolution line" >&2; exit 1; }
done || exit 1

step 2 "write pg_hba in place and reload"
cp -p "$HBA" "$HBA.before-sim"
write_in_place "$WORK/pg_hba.conf" "$HBA" || fail 2 "pg_hba could not be rewritten in place"
container_matches() { docker compose -p "$PROJECT" exec -T postgres cat /etc/postgresql/pg_hba.conf | cmp -s - "$1"; }
reload() { since=$(date -u +%Y-%m-%dT%H:%M:%SZ); docker compose -p "$PROJECT" kill -s HUP postgres >/dev/null; sleep 3
  logs=$(docker compose -p "$PROJECT" logs --since "$since" postgres 2>&1)
  printf '%s' "$logs" | grep -q 'received SIGHUP' && ! printf '%s' "$logs" | grep -qiE 'pg_hba.conf.*(not reloaded|invalid)|could not load pg_hba'; }
if ! container_matches "$HBA" || ! docker compose -p "$PROJECT" exec -T postgres grep -q nexloop_sim_worker /etc/postgresql/pg_hba.conf; then
  write_in_place "$HBA.before-sim" "$HBA" || true
  fail 2 "the container does not see the new pg_hba (its bind mount holds another inode): the previous content is written back; restart the postgres container (docker compose -p $PROJECT restart postgres) and rerun"
fi
if ! reload; then
  write_in_place "$HBA.before-sim" "$HBA" || true; reload || true
  fail 2 "pg_hba reload not clean; the previous content is written back in place and reloaded"
fi
printf '%s\n' "done: simulation line installed and reloaded ($count rule(s) added); pg_hba backup kept as config/pg_hba.conf.before-sim"
