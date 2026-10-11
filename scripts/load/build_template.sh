#!/usr/bin/env bash
# NX-031 slice 0: build the load template (synthetic data only, disposable PostgreSQL, never a persistent database).
#
#   scripts/load/build_template.sh [--seed 31] [--consumers 1000] [--out .ci-results/load/template]
#
# The output directory must not exist (it is created 0700). Secrets (synthetic passwords, the template's signing key) are only in
# <out>/private/ (0600); nothing secret is printed. The disposable build cluster is stopped and removed when the build ends.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$ROOT"
SEED=31 CONSUMERS=1000 OUT="$ROOT/.ci-results/load/template"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --seed) SEED=$2; shift 2;; --consumers) CONSUMERS=$2; shift 2;; --out) OUT=$2; shift 2;;
    *) echo "usage: $0 [--seed N] [--consumers N] [--out DIR]" >&2; exit 2;;
  esac
done
[[ -e "$OUT" ]] && { echo "build template: $OUT exists; choose a new --out" >&2; exit 1; }
mkdir -p "$(dirname "$OUT")" && mkdir -m 700 "$OUT" "$OUT/tmp"
export LC_ALL=C.UTF-8 LANG=C.UTF-8 REAL_DISPATCH_ENABLED=false
export PYTHONPATH="$ROOT/packages/eios-core/src:$ROOT/tests" NEXLOOP_LOAD_OUT="$OUT" NEXLOOP_LOAD_SEED="$SEED" NEXLOOP_LOAD_CONSUMERS="$CONSUMERS"
PYTHON=$(uv run --frozen python -c 'import sys; print(sys.executable)')
status=0
"$PYTHON" -m pytest -q -s -p no:xdist -p no:randomly -p no:cacheprovider --basetemp "$OUT/tmp" scripts/load/build_template_hold.py || status=$?
rm -rf "$OUT/tmp"
[[ $status -eq 0 ]] || { echo "build template: failed (exit $status)" >&2; exit "$status"; }
echo "build template: $OUT"
cat "$OUT/template.json"
