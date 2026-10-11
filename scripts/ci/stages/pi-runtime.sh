#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
# Frozen Pi source and actual SQLite are required. Missing files/tools fail.
mkdir -p .ci-results
[[ -f vendor/pi/packages/durable/test/nexloop-sqlite-conformance.test.ts ]]
[[ -f pnpm-lock.yaml ]]
pnpm exec vitest run vendor/pi/packages/durable/test/nexloop-sqlite-conformance.test.ts --exclude 'docs/tmp/**' --reporter=default --reporter=junit --outputFile=.ci-results/pi.xml
uv run --frozen python scripts/check_test_report.py .ci-results/pi.xml

pnpm exec vitest run apps/agent-host/test --exclude 'docs/tmp/**' --reporter=default --reporter=junit --outputFile=.ci-results/runtime.xml
uv run --frozen python scripts/check_test_report.py .ci-results/runtime.xml
