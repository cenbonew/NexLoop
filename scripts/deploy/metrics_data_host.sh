#!/usr/bin/env bash
# G-METRICS-DSN on DATA_HOST (run sheet run-metrics-gate-20261016 §1.3): everything that needs sudo-D in one run, so the owner types
# the password once:  sudo NEXLOOP_ROOT=... STAGE_ENV=... RELEASE_CHECKOUT=... METRICS_SRC=... bash metrics_data_host.sh
#   METRICS_SRC: a private directory (0700) holding metrics.dsn and metrics-role-password.sql from the management tree
#                (`stage_secrets.py metrics`: data/secrets/backup/metrics.dsn and management/metrics-role-password.sql).
# Steps (each stops the run on failure; nothing is printed but codes and counts):
#   1 preflight: inputs present; the newly rendered pg_hba differs from the installed one only by nexloop_metrics lines
#   2 ALTER ROLE nexloop_metrics (SCRAM verifier only) as nexloop_bootstrap over the container socket (bootstrap window)
#   3 install the new pg_hba (same owner and mode, old one kept as .before-metrics), HUP PostgreSQL, check the reload; on a reload
#     error the old file is restored and HUP'd again
#   4 install metrics.dsn as $NEXLOOP_ROOT/secrets/backup/metrics.dsn (nexloop-backup, 0600; never overwritten)
#   5 check: as nexloop-backup, select current_user over that DSN = nexloop_metrics
set -euo pipefail
: "${NEXLOOP_ROOT:?}" "${STAGE_ENV:?}" "${RELEASE_CHECKOUT:?}" "${METRICS_SRC:?}"
PROJECT=nexloop-data
HBA="$NEXLOOP_ROOT/config/pg_hba.conf"
DSN_TARGET="$NEXLOOP_ROOT/secrets/backup/metrics.dsn"
BACKUP_PY=${BACKUP_PY:-/opt/nexloop/backup-venv/bin/python}
step() { printf '%s\n' "step $1: $2"; }
fail() { printf '%s\n' "FAILED step $1: $2" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || fail 0 "run with sudo"

step 1 preflight
for f in "$METRICS_SRC/metrics.dsn" "$METRICS_SRC/metrics-role-password.sql" "$HBA" "$STAGE_ENV"; do [ -f "$f" ] || fail 1 "missing $(basename "$f")"; done
[ ! -e "$DSN_TARGET" ] || fail 1 "secrets/backup/metrics.dsn exists (never overwritten)"
grep -q 'user=nexloop_metrics' "$METRICS_SRC/metrics.dsn" || fail 1 "metrics.dsn is not the nexloop_metrics socket DSN"
[ "$(grep -c '^ALTER ROLE' "$METRICS_SRC/metrics-role-password.sql")" -eq 1 ] || fail 1 "metrics-role-password.sql must hold one ALTER ROLE"
grep -q "^ALTER ROLE nexloop_metrics PASSWORD 'SCRAM-SHA-256\\$" "$METRICS_SRC/metrics-role-password.sql" || fail 1 "not a SCRAM verifier for nexloop_metrics"
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT; chmod 0700 "$WORK"
cp "$STAGE_ENV" "$WORK/stage.env"; chmod 0600 "$WORK/stage.env"
python3 "$RELEASE_CHECKOUT/scripts/deploy/render.py" --env "$WORK/stage.env" --template "$RELEASE_CHECKOUT/deploy/stage/config/pg_hba.conf.template" \
  --out "$WORK/pg_hba.conf" >/dev/null || fail 1 "pg_hba render refused"
strip() { grep -v '^[[:space:]]*#' "$1" | sed '/^[[:space:]]*$/d'; }
removed=$(diff <(strip "$HBA") <(strip "$WORK/pg_hba.conf") | grep -c '^<' || true)
added=$(diff <(strip "$HBA") <(strip "$WORK/pg_hba.conf") | grep '^>' || true)
[ "$removed" -eq 0 ] || fail 1 "the rendered pg_hba removes $removed rule line(s); render with the deployed stage.env and a release checkout with G-METRICS-DSN"
[ "$(printf '%s\n' "$added" | sed '/^$/d' | grep -vc nexloop_metrics || true)" -eq 0 ] || fail 1 "the rendered pg_hba adds rules other than nexloop_metrics"
[ "$(printf '%s\n' "$added" | sed '/^$/d' | wc -l)" -eq 2 ] || fail 1 "expected exactly the two nexloop_metrics rules"

step 2 "ALTER ROLE nexloop_metrics (bootstrap window)"
docker compose -p "$PROJECT" exec -T postgres sh -c \
  'PGPASSWORD="$(cat /run/secrets/pg_bootstrap_password)" exec psql -X -q -v ON_ERROR_STOP=1 -h /var/run/postgresql -U nexloop_bootstrap -d "$POSTGRES_DB"' \
  < "$METRICS_SRC/metrics-role-password.sql" >/dev/null || fail 2 "ALTER ROLE refused (is nexloop_bootstrap still in its login window?)"

step 3 "install pg_hba and reload"
cp -p "$HBA" "$HBA.before-metrics"
install -o "$(stat -c %u "$HBA")" -g "$(stat -c %g "$HBA")" -m "$(stat -c %a "$HBA")" "$WORK/pg_hba.conf" "$HBA"
reload() { since=$(date -u +%Y-%m-%dT%H:%M:%SZ); docker compose -p "$PROJECT" kill -s HUP postgres >/dev/null; sleep 3
  logs=$(docker compose -p "$PROJECT" logs --since "$since" postgres 2>&1)
  printf '%s' "$logs" | grep -q 'received SIGHUP' && ! printf '%s' "$logs" | grep -qiE 'pg_hba.conf.*(not reloaded|invalid)|could not load pg_hba'; }
if ! reload; then
  cp -p "$HBA.before-metrics" "$HBA"; reload || true
  fail 3 "pg_hba reload not clean; the previous file is restored and reloaded"
fi

step 4 "install secrets/backup/metrics.dsn"
install -o nexloop-backup -g nexloop-backup -m 0600 "$METRICS_SRC/metrics.dsn" "$DSN_TARGET"

step 5 "check select current_user"
user=$(sudo -u nexloop-backup "$BACKUP_PY" -c 'import sys,psycopg
from nexloop_eios.private_configuration import read_private_text
with psycopg.connect(read_private_text(sys.argv[1],maximum=16384),connect_timeout=5) as c:print(c.execute("select current_user").fetchone()[0])' "$DSN_TARGET") \
  || fail 5 "nexloop-backup cannot connect with metrics.dsn"
[ "$user" = nexloop_metrics ] || fail 5 "current_user is not nexloop_metrics"
printf '%s\n' "done: current_user=nexloop_metrics; pg_hba backup kept as config/pg_hba.conf.before-metrics"
