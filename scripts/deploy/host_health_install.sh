#!/usr/bin/env bash
# Host health (E2/E3) install on this host, one command, as root (docs/implementation/host-health.md §4); see host_health_install.py.
#   printf 'app_host\t%s\n' "$PW" | sudo bash host_health_install.sh <private config.json> [--uninstall]
# The recorder password(s) come on stdin only; this wrapper checks root and hands everything to the Python installer (stdlib only).
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { printf '%s\n' 'FAILED: run with sudo' >&2; exit 2; }
exec python3 "$(cd "$(dirname "$0")" && pwd)/host_health_install.py" "$@"
