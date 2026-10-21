#!/usr/bin/env python3
"""Simulation stage enablement on DATA_HOST: put files into the sim-issue input directory, which belongs to NEXLOOP_PG_UID (0700) so
the dispatcher's account cannot write it (stage finding 2026-10-22, steps 7, 12 and 14: configurations, DSN files, the AT-046 part
results). Runs in the sim-issue-input one-off container (compose.sim-issue.example.yaml) as that uid; the files come as a tar stream
on stdin, e.g. from the dispatcher's machine:

    tar -C <local private dir> -cf - issue.json at046-probe-before.json | ssh DATA_HOST 'cd <release export> && docker compose -p
        nexloop-sim-issue -f deploy/stage/compose.sim-issue.example.yaml --env-file <env> --profile sim-issue run --rm -T sim-issue-input
        scripts/deploy/sim_input_put.py'

Only regular files with a plain name ([A-Za-z0-9][A-Za-z0-9._-]{0,127}, no directory part), at most 1 MiB each and 64 files, are
written: 0600, atomically (a file of the same name is replaced; never through a link). A directory, a link, a device, a path, '..' or
an oversized member refuses the whole stream before anything is written. Output: one JSON line with the names and sizes; never content.
    sim_input_put.py [--dir /sim-input]
Exit: 0 written, 2 refused.
"""
import argparse
import json
import os
import re
import stat
import sys
import tarfile
import tempfile
from pathlib import Path

NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}')
MAX_FILE, MAX_FILES = 1 << 20, 64


class Refused(ValueError):
    pass


def read_members(stream):
    files = {}
    try:
        with tarfile.open(fileobj=stream, mode='r|') as archive:
            for member in archive:
                if not member.isreg() or not NAME.fullmatch(member.name) or member.name in ('.', '..'):
                    raise Refused('only regular files with a plain name (no directory, link or path)')
                if member.size > MAX_FILE or len(files) >= MAX_FILES or member.name in files:
                    raise Refused(f'at most {MAX_FILES} distinct files of at most {MAX_FILE} bytes')
                files[member.name] = archive.extractfile(member).read()
    except tarfile.TarError:
        raise Refused('stdin: a tar stream') from None
    if not files:
        raise Refused('stdin: no file')
    return files


def write(directory, files):
    d = Path(directory)
    info = os.lstat(d)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise Refused('the input directory: a 0700 directory of this uid (run sim-issue-dir first)')
    for name, data in files.items():
        target = d / name
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise Refused(f'{name}: exists and is not a regular file')
        fd, tmp = tempfile.mkstemp(dir=d, prefix='.put-')
        try:
            with os.fdopen(fd, 'wb') as out:
                out.write(data)
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    return {'ok': True, 'written': sorted(files), 'bytes': {n: len(v) for n, v in sorted(files.items())}}


def main(argv=None, *, stdin=None, out=sys.stdout):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.error = lambda message: (_ for _ in ()).throw(SystemExit(2))
    parser.add_argument('--dir', default='/sim-input')
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2
    try:
        result = write(args.dir, read_members(stdin or sys.stdin.buffer))
        print(json.dumps(result, sort_keys=True), file=out)
        return 0
    except (Refused, OSError) as error:
        print(json.dumps({'ok': False, 'detail': str(error) if isinstance(error, Refused) else type(error).__name__}), file=out)
        return 2


if __name__ == '__main__':
    sys.exit(main())
