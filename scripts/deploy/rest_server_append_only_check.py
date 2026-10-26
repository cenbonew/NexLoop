#!/usr/bin/env python3
"""NX-035 repo1 append-only (L6 I1 of the rest-server review): does the installed rest-server binary refuse every delete and overwrite
with --append-only? Checked on a throwaway repository on DATA_HOST, never on a real one (if an overwrite were not refused it would
damage what it hit). Owner, root, a run sheet step after rest_server_data_host.sh (NX-035-file-backup.md §6.5):

    sudo bash rest_server_append_only_check.sh --rest-server /opt/nexloop/rest-server/<version>/rest-server --restic /usr/local/bin/restic

  1 a scratch directory /var/lib/nexloop/rest-append-only-check/<random>/ (root's chain checked; handed to nexloop-backup), a random
    password, `restic init` and one `restic backup` of a one-file directory as nexloop-backup: config, keys, data, index and a snapshot
  2 a second rest-server process of that binary, as nexloop-backup, --append-only --no-auth, listening on 127.0.0.1 only
  3 with curl, for each of data, index, keys and snapshots one existing object: DELETE and an overwriting POST (restic's write; rest-server
    has no PUT) must answer 403; config: an overwriting POST must answer 403 and DELETE must not succeed (403, or 405 where the server
    has no delete route for it); locks: a new lock POSTed and DELETEd must succeed (200) - the one deletion append-only allows; and every
    object that existed before must be byte for byte the same afterwards
  4 the server stopped, the scratch directory removed - also on failure
One JSON line: each check's status code and the verdict. Exit 0 every check as expected, 1 otherwise (or a step failed), 2 refused.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import file_backup_install as A  # noqa: E402  (trust checks)

Refused, Failed, run = A.Refused, A.Failed, A.run
USER = 'nexloop-backup'
SCRATCH = '/var/lib/nexloop/rest-append-only-check'
KINDS = ('data', 'index', 'keys', 'snapshots')


def curl(runner, method, url, body_file=None):
    out = runner(['curl', '-s', '-o', '/dev/null', '-w', '%{http_code}', '-X', method, *(['--data-binary', f'@{body_file}'] if body_file else []), url],
                 check=False).strip()
    return int(out) if out.isdigit() else 0


def objects(repo):
    """{relative path: sha256} of every file of the repository but its locks."""
    out = {}
    for path in sorted(Path(repo).rglob('*')):
        rel = path.relative_to(repo).as_posix()
        if path.is_file() and not rel.startswith('locks/'):
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def first(repo, kind):
    for path in sorted((Path(repo) / kind).rglob('*')):
        if path.is_file():
            return path.name
    return None


def checks(runner, base, repo, body):
    """(results, ok): every probe's status code and whether all are what --append-only must answer."""
    results, ok = {}, True
    for kind in KINDS:
        name = first(repo, kind)
        if name is None:
            results[kind] = {'error': 'no object to probe'}
            ok = False
            continue
        delete, overwrite = curl(runner, 'DELETE', f'{base}/{kind}/{name}'), curl(runner, 'POST', f'{base}/{kind}/{name}', body)
        results[kind] = {'delete': delete, 'overwrite': overwrite}
        ok = ok and delete == 403 and overwrite == 403
    delete, overwrite = curl(runner, 'DELETE', f'{base}/config'), curl(runner, 'POST', f'{base}/config', body)
    results['config'] = {'delete': delete, 'overwrite': overwrite}
    ok = ok and delete in (403, 405) and overwrite == 403
    lock = secrets.token_hex(32)
    created, removed = curl(runner, 'POST', f'{base}/locks/{lock}', body), curl(runner, 'DELETE', f'{base}/locks/{lock}')
    results['locks'] = {'create': created, 'delete': removed}
    ok = ok and created == 200 and removed == 200
    return results, ok


def main(argv=None, out=sys.stdout, runner=run, spawn=subprocess.Popen, root=Path('/'), trust=A.root_owned, trust_python=A.interpreter_owned,
         sleep=time.sleep):
    p = argparse.ArgumentParser(description='rest-server --append-only check on a throwaway repository (DATA_HOST, owner, root)')
    p.add_argument('--rest-server', required=True)
    p.add_argument('--restic', required=True)
    p.add_argument('--port', type=int, default=18765)
    a = p.parse_args(argv)
    os.umask(0o077)
    work = server = None
    try:
        if not 1024 <= a.port <= 65535:
            raise Refused('--port: 1024-65535 (127.0.0.1 only)')
        for path, what in ((a.rest_server, 'rest-server'), (a.restic, 'restic')):
            trust_python(path, what=what)
        if runner(['id', '-u', USER], check=False).strip() == '':
            raise Refused(f'user {USER} missing')
        base_dir = A.under(root, SCRATCH)
        if os.path.islink(base_dir):
            raise Refused(f'{SCRATCH} is a symbolic link')
        base_dir.mkdir(parents=True, exist_ok=True)
        trust(base_dir, what='scratch directory')  # never a real repository: a new directory of root's own, under /var/lib/nexloop
        os.chmod(base_dir, 0o711)
        work = Path(tempfile.mkdtemp(dir=base_dir, prefix='run-'))
        repo, password, source, body = work / 'repo', work / 'pass', work / 'src', work / 'body'
        password.write_text(secrets.token_hex(24) + '\n')
        source.mkdir()
        (source / 'append-only-check').write_text('throwaway\n')
        body.write_text('overwrite attempt\n')
        runner(['chown', '-R', f'{USER}:{USER}', str(work)])
        as_user = ['runuser', '-u', USER, '--', a.restic, '--no-cache', '-r', str(repo), '--password-file', str(password)]
        runner([*as_user, 'init'])
        runner([*as_user, 'backup', '--tag', 'append-only-check', str(source)])
        before = objects(repo)
        server = spawn(['runuser', '-u', USER, '--', a.rest_server, '--path', str(repo), '--listen', f'127.0.0.1:{a.port}', '--append-only', '--no-auth'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = f'http://127.0.0.1:{a.port}'
        for _ in range(40):
            if curl(runner, 'GET', f'{base}/config') == 200:
                break
            sleep(0.5)
        else:
            raise Failed('the throwaway rest-server did not answer on 127.0.0.1')
        results, ok = checks(runner, base, repo, body)
        unchanged = objects(repo) == before
        print(json.dumps({'ok': ok and unchanged, 'unchanged': unchanged, 'checks': results}, sort_keys=True), file=out)
        return 0 if ok and unchanged else 1
    except Refused as error:
        print(json.dumps({'refused': str(error)}), file=out)
        return 2
    except (Failed, OSError) as error:
        print(json.dumps({'failed': str(error) if isinstance(error, Failed) else type(error).__name__}), file=out)
        return 1
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
