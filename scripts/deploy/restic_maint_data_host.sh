#!/usr/bin/env bash
# NX-035 on DATA_HOST (owner, root): restic retention per tag in the installed nexloop-restic-maint.service; see
# restic_maint_data_host.py (docs/implementation/NX-035-restic-maint-groupby.md).
#   sudo bash restic_maint_data_host.sh [--revert]
# RESTIC_MAINT_PYTHON: the interpreter to run it with (a real file, root's; default python3). No secret is read or written here.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { printf '%s\n' 'FAILED: run with sudo' >&2; exit 2; }
exec "${RESTIC_MAINT_PYTHON:-python3}" -I -B "$(cd "$(dirname "$0")" && pwd)/restic_maint_data_host.py" "$@" </dev/null
