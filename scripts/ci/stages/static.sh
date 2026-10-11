#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
uv run --frozen python scripts/check_publication.py
uv run --frozen python scripts/check_pi_source.py
uv run --frozen python scripts/check_definer_search_path.py
uv run --frozen python scripts/generate_contracts.py --check
uv run --frozen python docs/handoff/tools/validate_handoff.py --planning planning
# NX-036: hosted workflows never run unreviewed code with secrets or write access (AT-054 static half); public files present.
uv run --frozen python scripts/check_ci_isolation.py
uv run --frozen python scripts/check_public_files.py
