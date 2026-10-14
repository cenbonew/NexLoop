"""Test stand-in for the restic command line used by nexloop-file-backup (NX-035 slice 3).

Speaks just the part of restic the executor calls: ``--no-cache --repository-file F --password-file F backup --json --tag T
--host H .`` and ``... check``. The repository file holds a local directory; a snapshot is a plain copy of the working
directory under ``<repo>/snapshots/<id>/``, and the summary line has restic's ``message_type`` and ``snapshot_id`` shape.
Each call appends {args without file contents, environment key names} to ``<repo>/calls.jsonl`` so tests can check what
the executor passed. Files ``<repo>/FAIL_BACKUP`` / ``<repo>/FAIL_CHECK`` make the matching command fail. Not encryption,
not deduplication: it stands in for the interface only.

    write_executable(path) -> a script that runs this module with the current interpreter
"""
import json
import os
import secrets
import shutil
import sys
from pathlib import Path


def main(argv):
    args = list(argv)
    if args[:1] != ['--no-cache'] or args[1] != '--repository-file' or args[3] != '--password-file':
        print('unexpected arguments', file=sys.stderr)
        return 2
    repo = Path(Path(args[2]).read_text().strip())
    password = Path(args[4]).read_text().strip()
    command = args[5:]
    if not password:
        return 1
    repo.mkdir(parents=True, exist_ok=True)
    with open(repo / 'calls.jsonl', 'a') as log:
        log.write(json.dumps({'command': command, 'environment_keys': sorted(os.environ), 'cwd_is_staging': Path.cwd().name.startswith('stage-')}) + '\n')
    if command[:1] == ['backup']:
        if (repo / 'FAIL_BACKUP').exists():
            print('Fatal: unable to save snapshot', file=sys.stderr)
            return 1
        snapshot = secrets.token_hex(32)
        shutil.copytree(Path.cwd(), repo / 'snapshots' / snapshot)
        (repo / 'snapshots' / snapshot / '.tags').write_text(json.dumps(command))
        print(json.dumps({'message_type': 'status', 'percent_done': 1}))
        print(json.dumps({'message_type': 'summary', 'snapshot_id': snapshot, 'files_new': 1}))
        return 0
    if command[:1] == ['check']:
        return 1 if (repo / 'FAIL_CHECK').exists() else 0
    return 2


def write_executable(path):
    path = Path(path)
    path.write_text(f'#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(Path(__file__).parent)!r})\n'
                    'import restic_stand_in\nraise SystemExit(restic_stand_in.main(sys.argv[1:]))\n')
    path.chmod(0o700)
    return path


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
