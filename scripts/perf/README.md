# Authorization performance measurement (measurement only)

No product code is changed: `hooks/sitecustomize.py` monkeypatches timing wrappers
into test processes (and their Python subprocesses) only when `hooks/` is on
PYTHONPATH; `hooks/perf_plugin.py` adds PL/pgSQL function stats and wait sampling
on the disposable test cluster. Output goes to the ignored `out/<label>/`.

    scripts/perf/run_profile.sh micro scripts/perf/test_micro_authz.py
    scripts/perf/run_profile.sh <label> <pytest node ids...>
    uv run --frozen python scripts/perf/slowdown.py scripts/perf/out/<label> [limit_ms]

Prerequisite for Pi/Host tests: built `apps/agent-host/dist` and vendor Pi
(`pnpm install --frozen-lockfile && pnpm build:pi && pnpm --filter @nexloop/agent-host build`).
Findings: docs/implementation/perf-authz-diagnosis.md.
