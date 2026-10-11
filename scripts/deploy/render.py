#!/usr/bin/env python3
"""NX-032 slice 0: whitelist renderer of the stage templates (the logic is nexloop_eios.stage_render, shared with the doctor)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'packages/eios-core/src'))
from nexloop_eios.stage_render import main  # noqa: E402

if __name__ == '__main__':
    sys.exit(main())
