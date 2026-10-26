#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
pnpm --filter @nexloop/web build
python3 scripts/check_web_build.py apps/web/dist  # NX-062 C8 G-6: only publishable files in the served web root
pnpm build:pi
pnpm --filter @nexloop/agent-host build
