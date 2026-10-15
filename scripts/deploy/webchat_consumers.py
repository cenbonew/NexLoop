#!/usr/bin/env python3
"""G-WEBCHAT-AUTH: nexloop_eios.webchat_consumers with the stage's configurator login window (--bootstrap-dsn-file, configurator_window.py,
NX-032-stage-secrets.md §3b). Same arguments as `python -m nexloop_eios.webchat_consumers`; the window is NOLOGIN again afterwards.

    webchat_consumers.py --role deploy/authorization/webchat-consumer-role.v1.json --recipe <private recipe> --apply \\
        --bootstrap-dsn-file F --signing-key-file <management/signing.key.hex> --signing-key-id ID
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
sys.path.insert(0, str(ROOT / 'packages/eios-core/src'))
from configurator_window import configurator_window  # noqa: E402
from nexloop_eios import webchat_consumers  # noqa: E402


def window(bootstrap_dsn_file):
    return configurator_window(bootstrap_dsn_file, refused=webchat_consumers.WebchatConsumersRejected)


if __name__ == '__main__':
    sys.exit(webchat_consumers.main(window=window))
