#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
mkdir -p .ci-results
node --test --test-reporter=junit --test-reporter-destination=.ci-results/deepseek-sdk.xml tests/offline-deepseek-request.test.mjs tests/offline-deepseek-alias.test.mjs
uv run --frozen python scripts/check_test_report.py .ci-results/deepseek-sdk.xml
pnpm exec vitest run apps/web/test --exclude 'docs/tmp/**' --reporter=default --reporter=junit --outputFile=.ci-results/web.xml
uv run --frozen python scripts/check_test_report.py .ci-results/web.xml
node --test --test-reporter=junit --test-reporter-destination=.ci-results/scope-ui.xml apps/agent-host/test/synthetic-scope-profile.node-test.mjs apps/web/test/scope-denial.node-test.mjs
uv run --frozen python scripts/check_test_report.py .ci-results/scope-ui.xml
