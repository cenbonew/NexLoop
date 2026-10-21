#!/usr/bin/env bash
# Simulation stage enablement, step 8 (docs/implementation/sim-stage-enable.md; L6 at046-split reviews 2.1 and 2.2): move a simulation
# world's tokens from DATA_HOST (where step 7 issued them, socket only) to APP_HOST's simulation secret and token directories. Run on
# the DISPATCHER's machine, which already reaches each host on its own: one pipeline of two ssh sessions,
#     ssh DATA_HOST '<sim-issue ... --export>' | ssh APP_HOST '<sim-setup ... --install>'
# so the tokens pass only through this pipe's memory here - never a terminal, a log, an argument or a file on any host between the
# two - and the hosts never trust each other (no ssh from APP_HOST to DATA_HOST: docker on DATA_HOST is root there, review 2.2). Once
# the install succeeded, DATA_HOST forgets its copy (ssh DATA_HOST ... --forget); a failed install keeps it.
#     bash sim_credentials_transfer.sh <private config>
#   <private config>: a 0600 file of KEY=VALUE lines (read, never sourced), exactly:
#     DATA_HOST_SSH, APP_HOST_SSH  the ssh destinations of the two hosts (host aliases of the dispatcher's ssh configuration)
#     DATA_RELEASE        the release checkout on DATA_HOST            DATA_ENV       the private sim-issue env file on DATA_HOST
#     ISSUE_CONFIG        the issuing configuration, as the sim-issue container sees it (/setup/...)
#     APP_RELEASE         the release checkout on APP_HOST             APP_ENV        the private simulation env file on APP_HOST
#     INSTALL_CONFIG      the install configuration, as the sim-setup container sees it (/setup/...)
# Output: the installer's JSON line, then the forget's (uses and counts only). Exit 1 on any failure (pipefail: either side).
# --export runs only with NEXLOOP_SIM_TRANSFER_PIPE=1, which this script alone sets: a guard against running it by hand, not a security
# boundary (inside `docker compose run -T` the stdout is always a pipe, so the tool cannot tell where it goes) - the protection is
# that the only documented way to run it is this pipeline.
set -euo pipefail
fail() { printf '%s\n' "FAILED: $1" >&2; exit 1; }
read_config() {
  local file=$1 line key value
  [ -f "$file" ] && [ ! -L "$file" ] || fail "config: not a regular file"
  [ "$(stat -c '%a %u' "$file" 2>/dev/null || stat -f '%Lp %u' "$file")" = "600 $(id -u)" ] || fail "config: must be 0600 and the caller's"
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    key=${line%%=*}; value=${line#*=}
    case "$key" in
      DATA_HOST_SSH|APP_HOST_SSH) [[ "$value" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || fail "config: $key must be a host alias" ;;
      DATA_RELEASE|DATA_ENV|ISSUE_CONFIG|APP_RELEASE|APP_ENV|INSTALL_CONFIG)
        [[ "$value" =~ ^/[A-Za-z0-9._/-]+$ ]] && [[ "$value" != *..* ]] || fail "config: $key must be an absolute path" ;;
      *) fail "config: unknown key" ;;
    esac
    printf -v "$key" '%s' "$value"
  done < "$file"
  for key in DATA_HOST_SSH APP_HOST_SSH DATA_RELEASE DATA_ENV ISSUE_CONFIG APP_RELEASE APP_ENV INSTALL_CONFIG; do
    [ -n "${!key:-}" ] || fail "config: $key required"; done
  [ "$DATA_HOST_SSH" != "$APP_HOST_SSH" ] || fail "config: DATA_HOST_SSH and APP_HOST_SSH must differ"
}
issue_cmd() {  # the DATA_HOST side (one argument for ssh; every value checked above, no shell characters)
  printf '%s' "cd $DATA_RELEASE && docker compose -p nexloop-sim-issue -f deploy/stage/compose.sim-issue.example.yaml --env-file $DATA_ENV" \
    " --profile sim-issue run --rm -T -e NEXLOOP_SIM_TRANSFER_PIPE=1 sim-issue scripts/deploy/sim_world_credentials.py --config $ISSUE_CONFIG $1"
}
install_cmd() {  # the APP_HOST side, reading the tokens on its stdin (the ssh session's)
  printf '%s' "cd $APP_RELEASE && docker compose -p nexloop-simulation -f deploy/stage/compose.simulation.example.yaml --env-file $APP_ENV" \
    " --profile sim-setup run --rm -T sim-setup scripts/deploy/sim_world_credentials.py --install $INSTALL_CONFIG"
}

if [ "${SIM_TRANSFER_LIB:-}" = 1 ]; then return 0 2>/dev/null || exit 0; fi
read_config "${1:?usage: bash sim_credentials_transfer.sh <private config>}"
# One pipeline here: DATA_HOST's export stdout is APP_HOST's installer stdin and nothing else; the installer prints only its JSON result.
ssh -T -o BatchMode=yes "$DATA_HOST_SSH" "$(issue_cmd --export)" | ssh -T -o BatchMode=yes "$APP_HOST_SSH" "$(install_cmd)" \
  || fail "transfer (export or install) failed; DATA_HOST keeps its copy"
ssh -T -o BatchMode=yes "$DATA_HOST_SSH" "$(issue_cmd --forget)" </dev/null || fail "installed, but DATA_HOST could not forget its copy: run --forget there"
