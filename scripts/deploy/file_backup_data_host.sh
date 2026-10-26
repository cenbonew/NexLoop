#!/usr/bin/env bash
# NX-035 file backup, step 1 on DATA_HOST (owner, root; docs/implementation/NX-035-file-backup.md §6.2); see file_backup_data_host.py.
#   sudo bash file_backup_data_host.sh <private config.json> [--uninstall]
# FILE_BACKUP_PYTHON: the interpreter to run it with (a real file, root's; default python3). No secret is read or written here.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { printf '%s\n' 'FAILED: run with sudo' >&2; exit 2; }
exec "${FILE_BACKUP_PYTHON:-python3}" -I -B "$(cd "$(dirname "$0")" && pwd)/file_backup_data_host.py" "$@" </dev/null
