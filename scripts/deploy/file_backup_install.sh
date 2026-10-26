#!/usr/bin/env bash
# NX-035 file backup install on APP_HOST, one command, as root (steps 2-6; docs/implementation/NX-035-file-backup.md §6); see
# file_backup_install.py.
#   sudo bash file_backup_install.sh <private config.json> --prepare
#   printf 'recorder\t%s\nrepo1\t%s\n' "$RECORDER_PW" "$REPO1_PW" | sudo bash file_backup_install.sh <private config.json>
#   sudo bash file_backup_install.sh <private config.json> [--uninstall]
# FILE_BACKUP_PYTHON: the interpreter to run the installer with (a real file, root's; default python3).
# The passwords come on stdin only (a terminal on stdin counts as none); this wrapper checks root and hands everything to the
# Python installer (stdlib only).
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { printf '%s\n' 'FAILED: run with sudo' >&2; exit 2; }
[ ! -t 0 ] || exec </dev/null
exec "${FILE_BACKUP_PYTHON:-python3}" -I -B "$(cd "$(dirname "$0")" && pwd)/file_backup_install.py" "$@"
