"""scripts/deploy/rest_server_append_only_check.py (L6 I1): rest-server --append-only checked on a throwaway repository only. A scripted
runner stands in for restic (it lays out a repository), for the throwaway server (a model of append-only answering curl) and for
runuser/chown; the verdicts for a server that keeps every object, and for servers that let one delete or overwrite through."""
import functools
import io
import json
import os
import stat as st
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import rest_server_append_only_check as C  # noqa: E402
from support.writable_tree import make_writable  # noqa: E402

OWNERS = (0, os.getuid())


@pytest.fixture(autouse=True)
def trust_root_at_tmp_path(tmp_path, monkeypatch):
    above = {str(p) for p in Path(tmp_path).parents} | {str(p) for p in Path(os.path.realpath(tmp_path)).parents}
    lstat = os.lstat

    def fake(path, *a, **k):
        info = lstat(path, *a, **k)
        if str(path) in above:
            return os.stat_result((st.S_IFDIR | 0o755, info.st_ino, info.st_dev, info.st_nlink, 0, 0, info.st_size,
                                   int(info.st_atime), int(info.st_mtime), int(info.st_ctime)))
        return info
    monkeypatch.setattr(os, 'lstat', fake)
    yield
    make_writable(tmp_path)


class Server:
    """The throwaway server: append-only unless told to let something through."""
    def __init__(self, repo_of, allow=(), config_delete=405, lock_delete=200, up=True, exited=False, foreign=False):
        self.repo_of, self.allow, self.config_delete, self.lock_delete, self.up = repo_of, set(allow), config_delete, lock_delete, up
        self.exited, self.foreign, self.stopped = exited, foreign, False

    def poll(self):
        return 1 if self.exited else None

    def answer(self, method, url, output):
        if not self.up:
            return 0
        path = url.split('127.0.0.1:', 1)[1].split('/', 1)[1]
        repo = self.repo_of()
        kind = path.split('/')[0]
        if method == 'GET':  # another server on the port answers with its own repository's config
            if output != '/dev/null':
                Path(output).write_bytes(b'another repository' if self.foreign else (repo / 'config').read_bytes())
            return 200
        if kind == 'locks':
            return 200 if method == 'POST' else self.lock_delete
        if kind == 'config':
            if method == 'DELETE':
                return self.config_delete
            if 'config-overwrite' in self.allow:
                (repo / 'config').write_text('overwritten')
                return 200
            return 403
        if f'{kind}-lie' in self.allow or f'{kind}-lie-{method}' in self.allow:  # answers success, changes nothing: only the codes tell
            return 200
        if f'{kind}-sneak' in self.allow and method == 'POST':  # answers 403, overwrites anyway: only the bytes can tell
            next((p for p in (repo / kind).rglob(path.split('/')[1]))).write_text('overwritten')
            return 403
        target = next((p for p in (repo / kind).rglob(path.split('/')[1])), None)
        if target is None:  # deleted by an earlier probe a broken server let through: creating it again is not refused either
            return 200
        if method == 'DELETE' and f'{kind}-delete' in self.allow:
            target.unlink()
            return 200
        if method == 'POST' and f'{kind}-overwrite' in self.allow:
            target.write_text('overwritten')
            return 200
        return 403

    def terminate(self):
        self.stopped = True

    def wait(self, timeout=None):
        return 0


class Runner:
    def __init__(self, server):
        self.calls, self.server, self.repo = [], server, None

    def __call__(self, command, *, stdin=None, check=True):
        self.calls.append(list(command))
        if command[:2] == ['id', '-u']:
            return '998\n'
        if command[:3] == ['runuser', '-u', 'nexloop-backup'] and command[-1] == 'init':
            self.repo = Path(command[command.index('-r') + 1])
            for rel, text in (('config', 'c'), ('keys/' + 'a' * 64, 'k'), ('data/ab/' + 'b' * 64, 'd'), ('index/' + 'c' * 64, 'i')):
                (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
                (self.repo / rel).write_text(text)
            return ''
        if command[:3] == ['runuser', '-u', 'nexloop-backup'] and 'backup' in command:
            (self.repo / 'snapshots').mkdir(exist_ok=True)
            (self.repo / 'snapshots' / ('d' * 64)).write_text('s')
            return ''
        if command[0] == 'curl':
            return str(self.server.answer(command[command.index('-X') + 1], command[-1], command[command.index('-o') + 1]))
        return ''


def main(tmp_path, server_kw=None, *args):
    root = tmp_path / 'root'
    (root / 'var/lib').mkdir(parents=True, exist_ok=True)
    binary = tmp_path / 'bin/rest-server'
    binary.parent.mkdir(exist_ok=True)
    binary.write_text('#!/bin/sh\n')
    binary.chmod(0o755)
    holder = {}
    server = Server(lambda: holder['runner'].repo, **(server_kw or {}))
    runner = Runner(server)
    holder['runner'] = runner
    spawned = []

    def spawn(argv, **kw):
        spawned.append(argv)
        return server
    out = io.StringIO()
    code = C.main(['--rest-server', str(binary), '--restic', str(binary), *args], out=out, runner=runner, spawn=spawn, root=root,
                  trust=functools.partial(C.A.root_owned, owners=OWNERS), trust_python=functools.partial(C.A.interpreter_owned, owners=OWNERS),
                  sleep=lambda s: None)
    return code, json.loads(out.getvalue().strip().splitlines()[-1]), runner, spawned, server, root


def test_an_append_only_server_refuses_every_delete_and_overwrite_but_a_locks_and_the_scratch_goes(tmp_path):
    code, doc, runner, spawned, server, root = main(tmp_path)
    assert code == 0 and doc['ok'] is True and doc['unchanged'] is True, doc
    assert doc['checks'] == {**{k: {'delete': 403, 'overwrite': 403} for k in C.KINDS}, 'config': {'delete': 405, 'overwrite': 403},
                             'locks': {'create': 200, 'delete': 200}}
    # A throwaway repository under the scratch directory, a server of that binary on 127.0.0.1 only, as nexloop-backup.
    [argv] = spawned
    assert argv[:4] == ['runuser', '-u', 'nexloop-backup', '--'] and '--append-only' in argv and argv[argv.index('--listen') + 1] == '127.0.0.1:18765'
    assert argv[argv.index('--path') + 1].startswith(str(root / 'var/lib/nexloop/rest-append-only-check/run-'))
    assert server.stopped and list((root / 'var/lib/nexloop/rest-append-only-check').iterdir()) == []
    assert all(c[-1].startswith('http://127.0.0.1:18765/') for c in runner.calls if c[0] == 'curl')


@pytest.mark.parametrize('allow', [['data-delete'], ['index-overwrite'], ['keys-delete'], ['snapshots-overwrite'], ['config-overwrite'],
                                   ['data-lie'], ['snapshots-sneak']])
def test_a_server_that_lets_one_delete_or_overwrite_through_fails(tmp_path, allow):
    code, doc, runner, spawned, server, root = main(tmp_path, {'allow': allow})
    assert code == 1 and doc['ok'] is False and server.stopped and list((root / 'var/lib/nexloop/rest-append-only-check').iterdir()) == []


@pytest.mark.parametrize('kw', [{'config_delete': 200}, {'lock_delete': 403}])
def test_a_deletable_config_or_an_undeletable_lock_fails(tmp_path, kw):
    code, doc, *_ = main(tmp_path, kw)
    assert code == 1 and doc['ok'] is False


def test_a_server_that_never_answers_fails_and_still_cleans_up(tmp_path):
    code, doc, runner, spawned, server, root = main(tmp_path, {'up': False})
    assert code == 1 and 'did not answer' in doc['failed'] and server.stopped
    assert list((root / 'var/lib/nexloop/rest-append-only-check').iterdir()) == []


@pytest.mark.parametrize('kw, why', [({'exited': True}, 'rest-server exited'), ({'foreign': True}, 'does not serve the throwaway repository')])
def test_a_server_that_exited_or_is_not_the_throwaway_ones_is_never_probed(tmp_path, kw, why):
    """L6 S4: an answer on the port is not enough - the process started here must be running and serve the repository made here."""
    code, doc, runner, spawned, server, root = main(tmp_path, kw)
    assert code == 1 and why in doc['failed'] and server.stopped, doc
    assert [c[c.index('-X') + 1] for c in runner.calls if c[0] == 'curl'] in ([], ['GET'])  # no DELETE or POST was sent
    assert list((root / 'var/lib/nexloop/rest-append-only-check').iterdir()) == []


def test_a_linked_scratch_directory_or_a_low_port_is_refused(tmp_path):
    (tmp_path / 'root/var/lib/nexloop').mkdir(parents=True)
    os.symlink(tmp_path / 'elsewhere', tmp_path / 'root/var/lib/nexloop/rest-append-only-check')
    code, doc, runner, spawned, *_ = main(tmp_path)
    assert code == 2 and 'symbolic link' in doc['refused'] and spawned == []
    code, doc, *_ = main(tmp_path / 'p', None, '--port', '80')
    assert code == 2 and '--port' in doc['refused']



def test_the_status_codes_and_the_bytes_are_each_checked(tmp_path):
    code, doc, *_ = main(tmp_path, {'allow': ['index-lie']})
    assert code == 1 and doc['unchanged'] is True and doc['checks']['index'] == {'delete': 200, 'overwrite': 200}
    for n, lie in enumerate(('data-lie-DELETE', 'data-lie-POST')):  # each of the two codes is checked on its own
        code, doc, *_ = main(tmp_path / f'l{n}', {'allow': [lie]})
        assert code == 1 and doc['unchanged'] is True, lie
    code, doc, *_ = main(tmp_path / 's', {'allow': ['keys-sneak']})
    assert code == 1 and doc['unchanged'] is False and doc['checks']['keys'] == {'delete': 403, 'overwrite': 403}


def test_a_writable_scratch_parent_is_refused(tmp_path):
    (tmp_path / 'root/var/lib/nexloop').mkdir(parents=True)
    os.chmod(tmp_path / 'root/var/lib/nexloop', 0o777)
    code, doc, runner, spawned, *_ = main(tmp_path)
    assert code == 2 and 'scratch directory' in doc['refused'] and spawned == []
