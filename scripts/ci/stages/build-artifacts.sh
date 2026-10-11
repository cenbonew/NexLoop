#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
pnpm --filter @nexloop/web build
pnpm build:pi
pnpm --filter @nexloop/agent-host build
