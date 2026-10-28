#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
pnpm --filter @nexloop/web build
# NX-062 C8 G-6: only publishable files in the served web root. NEXLOOP_WEB_ROOT is for tests only (tests/test_ci_stages.py
# stubs the pnpm build, so there is no fresh dist to check); CI never sets it and checks the dist the build just wrote.
python3 scripts/check_web_build.py "${NEXLOOP_WEB_ROOT:-apps/web/dist}"
pnpm build:pi
pnpm --filter @nexloop/agent-host build
