#!/usr/bin/env bash
# NX-054 P4a: the DATA_HOST daily relation read gap check, one command, as root (see relation_gaps_install.py).
#   sudo bash relation_gaps_install.sh <private config.json>                       install (the metrics DSN from the configured file)
#   security find-generic-password ... -w | sudo bash relation_gaps_install.sh <config.json> --dsn-stdin   (the DSN on stdin instead)
#   sudo bash relation_gaps_install.sh <config.json> --drill-arm MINUTES | --drill-clear | --uninstall
# RELATION_GAPS_PYTHON: the interpreter to run the installer with (a real file, root's; default python3).
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { printf '%s\n' 'FAILED: run with sudo' >&2; exit 2; }
exec "${RELATION_GAPS_PYTHON:-python3}" -I -B "$(cd "$(dirname "$0")" && pwd)/relation_gaps_install.py" "$@"
