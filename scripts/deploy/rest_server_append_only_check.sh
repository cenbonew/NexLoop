#!/usr/bin/env bash
# NX-035 repo1 append-only (L6 I1): the installed rest-server binary's --append-only checked on a throwaway repository on DATA_HOST
# (owner, root; never on a real repository); see rest_server_append_only_check.py.
#   sudo bash rest_server_append_only_check.sh --rest-server /opt/nexloop/rest-server/<version>/rest-server --restic /usr/local/bin/restic
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { printf '%s\n' 'FAILED: run with sudo' >&2; exit 2; }
exec "${REST_SERVER_PYTHON:-python3}" -I -B "$(cd "$(dirname "$0")" && pwd)/rest_server_append_only_check.py" "$@" </dev/null
