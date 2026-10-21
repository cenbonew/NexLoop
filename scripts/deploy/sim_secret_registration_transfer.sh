#!/usr/bin/env bash
# Simulation stage enablement (docs/implementation/sim-stage-enable.md, "compose 需要的 secret 文件"): register the simulation group's
# signing key and Host purge key, generated on APP_HOST by sim_secret_files.py, in the database on DATA_HOST. Both keys are
# symmetric (the database holds the material itself), so the registration needs the material: it moves through one pipeline run on
# the DISPATCHER's machine, which already reaches each host on its own,
#     ssh APP_HOST '<sim-setup ... sim_secret_files.py --export-registration>' | ssh DATA_HOST '<sim-issue ... sim_secret_register.py>'
# - only through this pipe's memory here: never a terminal, a log, an argument or a file on either host between the two, and the hosts
# never trust each other (as sim_credentials_transfer.sh, L6 at046-split review 2.2). DATA_HOST checks the keys against the digests
# APP_HOST printed (the register configuration's expect), registers what is missing and self-checks.
#     bash sim_secret_registration_transfer.sh <private config>
#   <private config>: a 0600 file of KEY=VALUE lines (read, never sourced), exactly:
#     APP_HOST_SSH, DATA_HOST_SSH  the ssh destinations of the two hosts (host aliases of the dispatcher's ssh configuration)
#     APP_RELEASE, APP_ENV         the release checkout and the private simulation env file on APP_HOST
#     SECRET_CONFIG                sim_secret_files.py's configuration, as the sim-setup container sees it (/setup/...)
#     DATA_RELEASE, DATA_ENV       the release checkout and the private sim-issue env file on DATA_HOST
#     REGISTER_CONFIG              sim_secret_register.py's configuration, as the sim-issue container sees it (/setup/...)
# Output: the registration's JSON line (ids, flags, the self-check; no value). Exit 1 on any failure (pipefail: either side).
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
      APP_HOST_SSH|DATA_HOST_SSH) [[ "$value" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || fail "config: $key must be a host alias" ;;
      APP_RELEASE|APP_ENV|SECRET_CONFIG|DATA_RELEASE|DATA_ENV|REGISTER_CONFIG)
        [[ "$value" =~ ^/[A-Za-z0-9._/-]+$ ]] && [[ "$value" != *..* ]] || fail "config: $key must be an absolute path" ;;
      *) fail "config: unknown key" ;;
    esac
    printf -v "$key" '%s' "$value"
  done < "$file"
  for key in APP_HOST_SSH DATA_HOST_SSH APP_RELEASE APP_ENV SECRET_CONFIG DATA_RELEASE DATA_ENV REGISTER_CONFIG; do
    [ -n "${!key:-}" ] || fail "config: $key required"; done
  [ "$DATA_HOST_SSH" != "$APP_HOST_SSH" ] || fail "config: DATA_HOST_SSH and APP_HOST_SSH must differ"
}
export_cmd() {  # the APP_HOST side (every value checked above, no shell characters)
  printf '%s' "cd $APP_RELEASE && docker compose -p nexloop-simulation -f deploy/stage/compose.simulation.example.yaml --env-file $APP_ENV" \
    " --profile sim-setup run --rm -T -e NEXLOOP_SIM_TRANSFER_PIPE=1 sim-setup scripts/deploy/sim_secret_files.py --config $SECRET_CONFIG --export-registration"
}
register_cmd() {  # the DATA_HOST side, reading the keys on its stdin (the ssh session's)
  printf '%s' "cd $DATA_RELEASE && docker compose -p nexloop-sim-issue -f deploy/stage/compose.sim-issue.example.yaml --env-file $DATA_ENV" \
    " --profile sim-issue run --rm -T sim-issue scripts/deploy/sim_secret_register.py --config $REGISTER_CONFIG"
}

if [ "${SIM_TRANSFER_LIB:-}" = 1 ]; then return 0 2>/dev/null || exit 0; fi
read_config "${1:?usage: bash sim_secret_registration_transfer.sh <private config>}"
ssh -T -o BatchMode=yes "$APP_HOST_SSH" "$(export_cmd)" | ssh -T -o BatchMode=yes "$DATA_HOST_SSH" "$(register_cmd)" \
  || fail "registration (export or register) failed"
