#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
[[ "$(node -p 'process.versions.node.split(".")[0]')" == 24 ]]
pnpm install --frozen-lockfile
