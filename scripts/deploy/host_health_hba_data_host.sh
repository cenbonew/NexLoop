#!/usr/bin/env bash
# Host health (E2/E3, docs/implementation/host-health.md §4) on DATA_HOST: the two pg_hba lines of the recorder roles - the local
# socket line of nexloop_host_recorder_data_host and the hostssl SCRAM line of nexloop_host_recorder_app_host from APP_HOST - rendered
# from deploy/stage/config/pg_hba.conf.template like every other rule (the sim_hba_data_host.sh procedure, same safeguards).
# Called by host_health_install.py on DATA_HOST (owner, sudo), or alone:
#     sudo bash host_health_hba_data_host.sh <private config>
#   <private config>: a 0600 file of KEY=VALUE lines, exactly NEXLOOP_ROOT, STAGE_ENV and RELEASE_CHECKOUT (read, never sourced).
# Steps (each stops the run on failure; nothing is printed but codes and counts):
#   1 preflight: the newly rendered pg_hba differs from the installed one by the host health lines added (both, or the one still
#     missing), and removes none
#   2 write the new pg_hba in place (same inode; old content kept as .before-host-health), check the container sees it, HUP
#     PostgreSQL, check the reload; otherwise the old content is written back in place and reloaded
# Passwords are not set here (host_recorder_password.py does that); without a password the lines admit nobody.
set -euo pipefail
PROJECT=nexloop-data
inode_of() { ls -di "$1" | awk '{print $1}'; }
write_in_place() { local src=$1 dst=$2 inode; inode=$(inode_of "$dst"); cat "$src" > "$dst"; [ "$(inode_of "$dst")" = "$inode" ]; }
step() { printf '%s\n' "step $1: $2"; }
fail() { printf '%s\n' "FAILED step $1: $2" >&2; exit 1; }
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
rules() { grep -v '^[[:space:]]*#' "$1" | sed '/^[[:space:]]*$/d' | tr -s ' \t' ' '; }
HH_DATA='^local [a-z_][a-z0-9_]* nexloop_host_recorder_data_host scram-sha-256$'
HH_APP='^hostssl [a-z_][a-z0-9_]* nexloop_host_recorder_app_host [0-9.]+/32 scram-sha-256$'

# The added rules: the host health lines only, each at most once, at least one.
check_added() {
  local count
  count=$(printf '%s\n' "$1" | sed '/^$/d' | wc -l | tr -d ' ')
  if [ "$count" -lt 1 ] || [ "$count" -gt 2 ]; then CHECK_ADDED="expected the host health lines added, found $count"; return 1; fi
  if [ "$(printf '%s\n' "$1" | sed '/^$/d' | grep -Evc "$HH_DATA|$HH_APP" || true)" -ne 0 ]; then CHECK_ADDED="an added rule is not a host health line"; return 1; fi
  if [ "$(printf '%s\n' "$1" | grep -Ec "$HH_DATA" || true)" -gt 1 ] || [ "$(printf '%s\n' "$1" | grep -Ec "$HH_APP" || true)" -gt 1 ]; then
    CHECK_ADDED="a host health line is added twice"; return 1; fi
  return 0
}
check_removed() {
  local count
  count=$(printf '%s\n' "$1" | sed '/^$/d' | wc -l | tr -d ' ')
  [ "$count" -eq 0 ] && return 0
  CHECK_REMOVED="the rendered pg_hba removes $count rule line(s)"; return 1
}

if [ "${HOST_HEALTH_HBA_LIB:-}" = 1 ]; then return 0 2>/dev/null || exit 0; fi
CONFIG=${1:?usage: sudo bash host_health_hba_data_host.sh <private config>}
[ "$(id -u)" -eq 0 ] || fail 0 "run with sudo"
read_config "$CONFIG"
HBA="$NEXLOOP_ROOT/config/pg_hba.conf"

step 1 preflight
for f in "$HBA" "$STAGE_ENV" "$RELEASE_CHECKOUT/scripts/deploy/render.py" "$RELEASE_CHECKOUT/deploy/stage/config/pg_hba.conf.template"; do
  [ -f "$f" ] || fail 1 "missing $(basename "$f")"; done
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT; chmod 0700 "$WORK"
cp "$STAGE_ENV" "$WORK/stage.env"; chmod 0600 "$WORK/stage.env"
"${HOST_HEALTH_PYTHON:-python3}" -I "$RELEASE_CHECKOUT/scripts/deploy/render.py" --env "$WORK/stage.env" --template "$RELEASE_CHECKOUT/deploy/stage/config/pg_hba.conf.template" \
  --out "$WORK/pg_hba.conf" >/dev/null || fail 1 "pg_hba render refused"
removed=$(diff <(rules "$HBA") <(rules "$WORK/pg_hba.conf") | sed -n 's/^< //p')
added=$(diff <(rules "$HBA") <(rules "$WORK/pg_hba.conf") | sed -n 's/^> //p')
check_removed "$removed" || fail 1 "$CHECK_REMOVED; render with the deployed stage.env and the release checkout"
if [ -z "$added" ] && grep -Eq "$HH_DATA" <(rules "$HBA") && grep -Eq "$HH_APP" <(rules "$HBA"); then
  printf '%s\n' "done: the host health lines are already installed (nothing to do)"; exit 0; fi
check_added "$added" || fail 1 "$CHECK_ADDED"
count=$(printf '%s\n' "$added" | sed '/^$/d' | wc -l | tr -d ' ')

step 2 "write pg_hba in place and reload"
cp -p "$HBA" "$HBA.before-host-health"
write_in_place "$WORK/pg_hba.conf" "$HBA" || fail 2 "pg_hba could not be rewritten in place"
container_matches() { docker compose -p "$PROJECT" exec -T postgres cat /etc/postgresql/pg_hba.conf | cmp -s - "$1"; }
reload() { since=$(date -u +%Y-%m-%dT%H:%M:%SZ); docker compose -p "$PROJECT" kill -s HUP postgres >/dev/null; sleep 3
  logs=$(docker compose -p "$PROJECT" logs --since "$since" postgres 2>&1)
  printf '%s' "$logs" | grep -q 'received SIGHUP' && ! printf '%s' "$logs" | grep -qiE 'pg_hba.conf.*(not reloaded|invalid)|could not load pg_hba'; }
if ! container_matches "$HBA" || ! docker compose -p "$PROJECT" exec -T postgres grep -q nexloop_host_recorder_data_host /etc/postgresql/pg_hba.conf; then
  write_in_place "$HBA.before-host-health" "$HBA" || true
  fail 2 "the container does not see the new pg_hba (its bind mount holds another inode): the previous content is written back; restart the postgres container (docker compose -p $PROJECT restart postgres) and rerun"
fi
if ! reload; then
  write_in_place "$HBA.before-host-health" "$HBA" || true; reload || true
  fail 2 "pg_hba reload not clean; the previous content is written back in place and reloaded"
fi
printf '%s\n' "done: host health lines installed and reloaded ($count rule(s) added); pg_hba backup kept as config/pg_hba.conf.before-host-health"
