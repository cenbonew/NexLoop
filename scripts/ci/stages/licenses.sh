#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
# NX-036: license inventory of both lock files, vendored sources and assets; THIRD_PARTY_NOTICES.md drift (offline, after install).
uv run --frozen python scripts/check_licenses.py --check
