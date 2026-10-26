#!/usr/bin/env python3
"""NX-035 slice 3 on APP_HOST: the entry of nexloop-file-backup.service (docs/implementation/NX-035-file-backup.md §6).

The unit runs `<host-health venv python> -I -B /opt/nexloop/file-backup/current/scripts/deploy/file_backup_run.py --kind ...`: the
release directory scripts/deploy/file_backup_install.py builds holds the executor's four modules at their repository paths, and -I
adds no directory to the module path, so this entry adds the release's packages/eios-core/src and nothing else. The arguments are
nexloop_eios.file_backup's own (one kind, one repository per run).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'packages/eios-core/src'))

from nexloop_eios.file_backup import main  # noqa: E402

if __name__ == '__main__':
    raise SystemExit(main())
