# Shared by scripts/ci/check and every stage script (NX-033 §4.1): strict mode, the repository root, and the lists more than
# one stage needs. Sourced, never executed.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."
# Tests that rebuild apps/web/dist, apps/agent-host/dist, vendor/pi dist or install the core wheel. They rewrite shared
# workspace files, so nothing may run beside them (serial-build runs them; integration ignores them).
WORKSPACE_BUILD_TESTS=(tests/test_build_provider_environment.py tests/test_community_container_contract.py
  tests/test_community_launcher.py tests/test_wheel_install.py)
