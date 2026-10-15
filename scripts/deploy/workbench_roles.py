#!/usr/bin/env python3
"""G-WORKBENCH-AUTH: nexloop_eios.workbench_roles with the stage's configurator login window (--bootstrap-dsn-file, configurator_window.py,
NX-032-stage-secrets.md §3b). Same arguments as `python -m nexloop_eios.workbench_roles`; the window is NOLOGIN again afterwards.

    workbench_roles.py --roles deploy/authorization/workbench-roles.v1.json --members <private members file> --apply \\
        --bootstrap-dsn-file F --signing-key-file <management/signing.key.hex> --signing-key-id ID
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
sys.path.insert(0, str(ROOT / 'packages/eios-core/src'))
from configurator_window import configurator_window  # noqa: E402
from nexloop_eios import workbench_roles  # noqa: E402


def window(bootstrap_dsn_file):
    return configurator_window(bootstrap_dsn_file, refused=workbench_roles.WorkbenchRolesRejected)


if __name__ == '__main__':
    sys.exit(workbench_roles.main(window=window))
