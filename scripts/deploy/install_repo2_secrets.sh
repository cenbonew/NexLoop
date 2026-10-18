#!/usr/bin/env bash
# NX-035 repo2 activation (run sheet repo2-activate): installs the repo2 (Volcengine TOS) configuration on one host from stdin.
#
#   { security find-generic-password -s nexloop/backup/tos-data-access-key -w
#     security find-generic-password -s nexloop/backup/tos-data-secret-key -w
#     security find-generic-password -s nexloop/backup/repo2-cipher-pass -w; } \
#     | ssh <DATA_HOST> 'sudo NEXLOOP_ROOT=<root> TOS_BUCKET=<bucket> TOS_REGION=<region> bash <release tree>/scripts/deploy/install_repo2_secrets.sh pgbackrest'
#
# Roles (three lines on stdin each: access key id, secret access key, then the role's own passphrase):
#   pgbackrest  DATA_HOST  the [global] repo2 lines of $NEXLOOP_ROOT/secrets/pgbackrest/pgbackrest.conf, rendered from
#                          deploy/stage/config/pgbackrest-repo2-tos.conf.example (passphrase = repo2 cipher pass)
#   journal     DATA_HOST  restic repo2 of the restore journal: /etc/nexloop/restic-repo2.env, its repository and password files,
#                          and the repo2 lines of /etc/nexloop/restic.env (nexloop-restic-maint) (passphrase = restic-repo2-journal)
#   files       APP_HOST   restic repo2 of the file backup: the repository, password and object storage environment files named
#                          in /etc/nexloop/file-backup.env (passphrase = restic-repo2-files)
# Optional BUCKET_LOOKUP=dns (journal, files): restic's -o s3.bucket-lookup=dns, for a TOS that refuses path-style requests.
#
# Secrets arrive on stdin only: never as arguments, never echoed or logged. Every file is written with install (owner and mode in
# one step); a file that already holds repo2 settings is left alone unless REPLACE=1, and the previous version is kept as
# <file>.pre-repo2 (0600) for the rollback. Prints only paths, owners, modes and sizes. Exit 2 refused, nothing written.
set -euo pipefail
set +x
umask 077
export LC_ALL=C
ROLE=${1:-}
: "${NEXLOOP_ROOT:?NEXLOOP_ROOT}" "${TOS_BUCKET:?TOS_BUCKET}" "${TOS_REGION:?TOS_REGION}"
HERE=$(cd "$(dirname "$0")/../.." && pwd)
SECRETS=$NEXLOOP_ROOT/secrets
ENDPOINT=tos-s3-$TOS_REGION.volces.com
ETC=${NEXLOOP_ETC:-/etc/nexloop}  # tests point this elsewhere

fail() { echo "install_repo2_secrets: $1" >&2; exit 2; }
[ "$(id -u)" = 0 ] || fail 'run as root (sudo)'
[ ! -t 0 ] || fail 'the secrets are read from a pipe, not a terminal'
case "$ROLE" in pgbackrest|journal|files) ;; *) fail 'role: pgbackrest | journal | files' ;; esac
[[ "$TOS_BUCKET" =~ ^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$ ]] || fail 'TOS_BUCKET malformed'
[[ "$TOS_REGION" =~ ^[a-z]{2}-[a-z]+(-[a-z0-9]+)*$ ]] || fail 'TOS_REGION malformed'
case "${BUCKET_LOOKUP:-}" in ''|auto|dns|path) ;; *) fail 'BUCKET_LOOKUP: auto | dns | path' ;; esac

line() { local v; IFS= read -r v || [ -n "${v:-}" ] || fail 'three lines expected on stdin'
  case "$v" in *[![:graph:]]*|'') fail 'stdin values must be printable ASCII without spaces' ;; esac
  [ "${#v}" -le 1024 ] || fail 'stdin value too long'; printf '%s' "$v"; }
AK=$(line); SK=$(line); PASS=$(line)
if IFS= read -r _extra; then fail 'more than three lines on stdin'; fi
[ "${#PASS}" -ge 32 ] || fail 'the passphrase must have at least 32 characters'

T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
keep() { [ -e "$1" ] && install -m 0600 -o root -g root "$1" "$1.pre-repo2" || true; }
put() { install -o "$2" -g "$3" -m "$4" "$T/$(basename "$1")" "$1"; stat -c '%n %U:%G %a %s' "$1"; }
holds_repo2() { [ -e "$1" ] && grep -Eq "$2" "$1"; }

case "$ROLE" in
pgbackrest)
  CONF=$SECRETS/pgbackrest/pgbackrest.conf
  [ -f "$CONF" ] || fail "$CONF missing (repo1 first)"
  grep -qx '\[global\]' "$CONF" || fail "$CONF has no [global] section"
  if holds_repo2 "$CONF" '^repo2-'; then [ "${REPLACE:-}" = 1 ] || fail "$CONF already has repo2 lines (REPLACE=1 to rewrite them)"; fi
  # The template's settings, comments dropped, placeholders filled literally; values never pass through sed or argv (awk reads ENVIRON).
  grep -Ev '^[[:space:]]*(#|$)' "$HERE/deploy/stage/config/pgbackrest-repo2-tos.conf.example" \
    | R_REGION=$TOS_REGION R_BUCKET=$TOS_BUCKET R_AK=$AK R_SK=$SK R_PASS=$PASS awk '
        # Literal replacement (index/substr): sub()/gsub() would read & and \ in a value as metacharacters.
        function put(line, ph, v,   i, out) { out=""; while ((i=index(line, ph)) > 0) { out=out substr(line, 1, i-1) v; line=substr(line, i+length(ph)) } return out line }
        { $0=put($0,"__TOS_REGION__",ENVIRON["R_REGION"]); $0=put($0,"__TOS_BUCKET__",ENVIRON["R_BUCKET"]);
          $0=put($0,"__TOS_DATA_ACCESS_KEY_ID__",ENVIRON["R_AK"]); $0=put($0,"__TOS_DATA_SECRET_ACCESS_KEY__",ENVIRON["R_SK"]);
          $0=put($0,"__REPO2_CIPHER_PASS__",ENVIRON["R_PASS"]); print }' > "$T/block"
  ! grep -Eq '__[A-Z0-9_]+__' "$T/block" || fail 'a placeholder is left in the repo2 block'
  # Existing repo2 lines out, the new block right after [global].
  grep -v '^repo2-' "$CONF" | awk -v block="$T/block" '{print} $0=="[global]"{while((getline l < block)>0)print l}' > "$T/pgbackrest.conf"
  ! grep -Eq '__[A-Z0-9_]+__' "$T/pgbackrest.conf" || fail 'placeholders left in pgbackrest.conf'
  OWNER=$(stat -c '%u' "$CONF")
  keep "$CONF"; put "$CONF" "$OWNER" "$(stat -c '%g' "$CONF")" 0600
  ;;
journal)
  ENV1=$ETC/restic-repo1.env; ENV2=$ETC/restic-repo2.env; MAINT=$ETC/restic.env
  [ -f "$ENV1" ] || fail "$ENV1 missing (repo1 first)"
  JOURNAL_DIR=$(sed -n 's/^JOURNAL_DIR=//p' "$ENV1" | head -1); [ -n "$JOURNAL_DIR" ] || fail "JOURNAL_DIR missing in $ENV1"
  if holds_repo2 "$ENV2" '^AWS_'; then [ "${REPLACE:-}" = 1 ] || fail "$ENV2 already configured (REPLACE=1 to rewrite)"; fi
  REPOFILE=$SECRETS/backup/restic-repo2.repository; PASSFILE=$SECRETS/backup/restic-repo2.pass
  printf 's3:https://%s/%s/nexloop/restic-journal\n' "$ENDPOINT" "$TOS_BUCKET" > "$T/$(basename "$REPOFILE")"
  printf '%s\n' "$PASS" > "$T/$(basename "$PASSFILE")"
  { printf 'JOURNAL_DIR=%s\nRESTIC_REPOSITORY_FILE=%s\nRESTIC_PASSWORD_FILE=%s\n' "$JOURNAL_DIR" "$REPOFILE" "$PASSFILE"
    printf 'AWS_ACCESS_KEY_ID=%s\nAWS_SECRET_ACCESS_KEY=%s\nAWS_DEFAULT_REGION=%s\n' "$AK" "$SK" "$TOS_REGION"
    [ -z "${BUCKET_LOOKUP:-}" ] || printf 'RESTIC_EXTRA_OPTIONS=-o s3.bucket-lookup=%s\n' "$BUCKET_LOOKUP"; } > "$T/$(basename "$ENV2")"
  # nexloop-restic-maint: its repo2 lines replaced (repo1 lines kept as they are).
  { [ -f "$MAINT" ] && grep -Ev '^(RESTIC_REPO2_|AWS_|RESTIC_REPO2_OPTIONS=)' "$MAINT" || true
    printf 'RESTIC_REPO2_REPOSITORY_FILE=%s\nRESTIC_REPO2_PASSWORD_FILE=%s\n' "$REPOFILE" "$PASSFILE"
    printf 'AWS_ACCESS_KEY_ID=%s\nAWS_SECRET_ACCESS_KEY=%s\nAWS_DEFAULT_REGION=%s\n' "$AK" "$SK" "$TOS_REGION"
    [ -z "${BUCKET_LOOKUP:-}" ] || printf 'RESTIC_REPO2_OPTIONS=-o s3.bucket-lookup=%s\n' "$BUCKET_LOOKUP"; } > "$T/$(basename "$MAINT")"
  id nexloop-backup >/dev/null 2>&1 || fail 'user nexloop-backup missing'
  keep "$ENV2"; keep "$MAINT"
  put "$REPOFILE" nexloop-backup nexloop-backup 0600; put "$PASSFILE" nexloop-backup nexloop-backup 0600
  put "$ENV2" root nexloop-backup 0600; put "$MAINT" root nexloop-backup 0600
  ;;
files)
  CONF=$ETC/file-backup.env
  [ -f "$CONF" ] || fail "$CONF missing (repo1 first)"
  get() { sed -n "s/^$1=//p" "$CONF" | head -1; }
  REPOFILE=$(get RESTIC_REPO2_REPOSITORY_FILE); PASSFILE=$(get RESTIC_REPO2_PASSWORD_FILE); OFFSITE=$(get OFFSITE_ENVIRONMENT_FILE)
  [ -n "$REPOFILE" ] && [ -n "$PASSFILE" ] && [ -n "$OFFSITE" ] || fail "$CONF lacks RESTIC_REPO2_REPOSITORY_FILE / RESTIC_REPO2_PASSWORD_FILE / OFFSITE_ENVIRONMENT_FILE"
  if holds_repo2 "$OFFSITE" '^AWS_'; then [ "${REPLACE:-}" = 1 ] || fail "$OFFSITE already configured (REPLACE=1 to rewrite)"; fi
  id nexloop-file-backup >/dev/null 2>&1 || fail 'user nexloop-file-backup missing'
  printf 's3:https://%s/%s/nexloop/restic-files\n' "$ENDPOINT" "$TOS_BUCKET" > "$T/$(basename "$REPOFILE")"
  printf '%s\n' "$PASS" > "$T/$(basename "$PASSFILE")"
  printf 'AWS_ACCESS_KEY_ID=%s\nAWS_SECRET_ACCESS_KEY=%s\nAWS_DEFAULT_REGION=%s\n' "$AK" "$SK" "$TOS_REGION" > "$T/$(basename "$OFFSITE")"
  keep "$OFFSITE"
  for f in "$REPOFILE" "$PASSFILE" "$OFFSITE"; do put "$f" nexloop-file-backup nexloop-file-backup 0600; done
  # The unit's repo2 restic option (empty = restic's default addressing).
  grep -v '^FILE_BACKUP_REPO2_OPTIONS=' "$CONF" > "$T/file-backup.env"
  [ -z "${BUCKET_LOOKUP:-}" ] || printf 'FILE_BACKUP_REPO2_OPTIONS=--s3-bucket-lookup %s\n' "$BUCKET_LOOKUP" >> "$T/file-backup.env"
  keep "$CONF"; put "$CONF" root root 0600
  ;;
esac
unset AK SK PASS
