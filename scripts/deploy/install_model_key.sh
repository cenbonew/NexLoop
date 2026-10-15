#!/usr/bin/env bash
# NX-032 phase C (docs/release/run-model-smoke-20261018.md §1): installs the model provider key on APP_HOST from stdin.
#
#   security find-generic-password -a nexloop -s nexloop/model/deepseek -w \
#     | ssh <APP_HOST> 'sudo NEXLOOP_ROOT=<root> bash <release tree>/scripts/deploy/install_model_key.sh'
#
# The key arrives on stdin only: never as an argument, never echoed, never logged. Checks: one line of printable ASCII without
# spaces, at most 8192 characters (the Agent Host's trusted-model-profile.ts and provider_credentials.py limits). The Agent Host
# key file's owner must be the Agent Host process UID (private-material.ts compares the owner with the process UID), read from
# the running container and checked against the owner of its model configuration file; a mismatch stops before writing.
# Writes three files with install (owner and mode in one step) and prints only names, owners, modes and sizes.
# Recreating the services is a separate step (run sheet §1.5).
set -euo pipefail
set +x
umask 077
export LC_ALL=C
: "${NEXLOOP_ROOT:?NEXLOOP_ROOT}"
PROJECT=${NEXLOOP_APP_PROJECT:-nexloop-app}
SECRETS=$NEXLOOP_ROOT/secrets

fail() { echo "install_model_key: $1" >&2; exit 2; }

[ "$(id -u)" = 0 ] || fail 'run as root (sudo)'
[ ! -t 0 ] || fail 'the key is read from a pipe, not a terminal'

IFS= read -r KEY || [ -n "${KEY:-}" ] || fail 'no key on stdin'
if IFS= read -r _extra; then fail 'more than one line on stdin'; fi
case "$KEY" in *[![:graph:]]*|'') fail 'the key must be printable ASCII without spaces' ;; esac
[ "${#KEY}" -le 8192 ] || fail 'the key is longer than 8192 characters'

HOST_UID=$(docker compose -p "$PROJECT" exec -T agent-host id -u 2>/dev/null) || fail 'agent-host is not running (its UID is read from the container)'
CONFIG_UID=$(stat -c '%u' "$SECRETS/agent-host/model-configuration.json") || fail 'model-configuration.json missing'
[ "$HOST_UID" = "$CONFIG_UID" ] || fail "agent-host runs as $HOST_UID but its model configuration is owned by $CONFIG_UID (G-UID): stop"

T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
printf '%s\n' "$KEY" > "$T/model.key"
printf 'MODEL_PROVIDER=deepseek\nMODEL_ID=deepseek-flash\nMODEL_BASE_URL=https://api.deepseek.com\nMODEL_API_KEY=%s\n' "$KEY" > "$T/model.env"
unset KEY

install -d -o "$HOST_UID" -g "$HOST_UID" -m 0700 "$SECRETS/agent-host-model"
install -o "$HOST_UID" -g "$HOST_UID" -m 0600 "$T/model.key" "$SECRETS/agent-host-model/model.key"
for worker in claim-extraction-worker claim-matcher; do
  install -o 10001 -g 10001 -m 0600 "$T/model.env" "$SECRETS/$worker/model.env"
done
shred -u "$T/model.key" "$T/model.env"

stat -c '%n %u %a %s' "$SECRETS/agent-host-model/model.key" "$SECRETS/claim-extraction-worker/model.env" "$SECRETS/claim-matcher/model.env"
