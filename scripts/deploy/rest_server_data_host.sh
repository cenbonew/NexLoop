#!/usr/bin/env bash
# NX-035 repo1 append-only on DATA_HOST (owner, root; NX-035-file-backup.md §6.5); see rest_server_data_host.py.
#   printf 'rest\t%s\n' "$REST_PW" | sudo bash rest_server_data_host.sh <private config.json> [--uninstall]
# REST_SERVER_PYTHON: the interpreter to run it with (a real file, root's; default python3). The htpasswd password comes on stdin
# only (a terminal on stdin counts as none); this wrapper checks root and hands everything to the Python installer (stdlib only).
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { printf '%s\n' 'FAILED: run with sudo' >&2; exit 2; }
[ ! -t 0 ] || exec </dev/null
exec "${REST_SERVER_PYTHON:-python3}" -I -B "$(cd "$(dirname "$0")" && pwd)/rest_server_data_host.py" "$@"
