#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
uv sync --frozen
# Dependency phase (network allowed): warm the PEP 517 build backend once, so the offline
# wheel/packaging tests below never depend on a pre-populated developer uv cache.
# Also warm the registry metadata used by the offline hashed install in tests/test_wheel_install.py;
# `uv sync` fetches locked file URLs only and does not cache the index pages.
warm_dir=$(mktemp -d)
uv build --package nexloop-eios-core --wheel --out-dir "$warm_dir/wheels" >/dev/null
uv export --frozen --no-dev --no-emit-workspace --format requirements-txt --output-file "$warm_dir/requirements.txt" >/dev/null
uv venv --python .venv/bin/python "$warm_dir/venv" >/dev/null
uv pip install --python "$warm_dir/venv/bin/python" --no-deps --require-hashes -r "$warm_dir/requirements.txt" >/dev/null
rm -rf "$warm_dir"
