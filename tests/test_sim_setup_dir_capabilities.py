"""The one-off directory steps of the simulation compose projects (compose.simulation sim-setup-dir, compose.sim-issue sim-issue-dir)
run as root with CAP_CHOWN only (no FOWNER): root may chown anything but chmod only what it owns. Their actual entrypoint code from the
compose files runs here against a model of that kernel rule (stage finding 2026-10-22: chown to the target uid first, then chmod, was
refused). Starting states: the directories docker's bind mount creates (0755 root), the dispatcher's input files (the deploy
account's, 0644), and a rerun (everything already the target uid's). The result: directories 0700 and files 0600 of the target uid."""
import builtins
import stat as real_stat
import types
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
TARGET = (10031, 10031)
DEPLOY = 1000


class Kernel:
    """Paths with (uid, gid, mode, kind); root with CAP_CHOWN only."""

    def __init__(self, paths):
        self.paths = dict(paths)

    def os_module(self):
        k = self

        def chown(p, u, g):
            uid, gid, mode, kind = k.paths[p]
            k.paths[p] = (u, g, mode, kind)  # CAP_CHOWN: any owner

        def chmod(p, mode):
            uid, gid, _, kind = k.paths[p]
            if uid != 0:  # no CAP_FOWNER: only the owner may chmod
                raise PermissionError(1, 'Operation not permitted', p)
            k.paths[p] = (uid, gid, mode, kind)

        def makedirs(p, mode=0o777, exist_ok=False):
            if p not in k.paths:
                k.paths[p] = (0, 0, mode, 'dir')

        def lstat(p):
            return types.SimpleNamespace(st_mode=(real_stat.S_IFREG if k.paths[p][3] == 'file' else real_stat.S_IFDIR) | k.paths[p][2])

        def listdir(d):
            return [p.rsplit('/', 1)[1] for p in k.paths if p.startswith(d + '/')]
        return types.SimpleNamespace(chown=chown, chmod=chmod, makedirs=makedirs, lstat=lstat, listdir=listdir,
                                     path=types.SimpleNamespace(join=lambda a, b: a + '/' + b))

    def run(self, entrypoint):
        code, uid, gid = entrypoint[2], *TARGET
        fake_os, fake_sys = self.os_module(), types.SimpleNamespace(argv=['-c', str(uid), str(gid)])

        def importer(name, *a, **kw):
            return {'os': fake_os, 'sys': fake_sys, 'stat': real_stat}[name]
        exec(code, {'__builtins__': {**vars(builtins), '__import__': importer}})


def entrypoint(compose, service):
    spec = yaml.safe_load((ROOT / 'deploy/stage' / compose).read_text())['services'][service]
    assert spec['user'] == '0:0' and spec['cap_add'] == ['CHOWN'] and spec['cap_drop'] == ['ALL'] and spec['network_mode'] == 'none'
    ep = list(spec['entrypoint'])
    if '${' in ep[3]:
        ep[3:5] = [str(TARGET[0]), str(TARGET[1])]
    return ep


CASES = {
    'compose.simulation.example.yaml/sim-setup-dir': ('/sim-secrets', '/sim-tokens'),
    'compose.sim-issue.example.yaml/sim-issue-dir': ('/sim-tokens', '/sim-input'),
}


@pytest.mark.parametrize('case', sorted(CASES))
def test_with_chown_only_the_directories_end_0700_and_the_files_0600_of_the_target_uid(case):
    compose, service = case.split('/')
    dirs = CASES[case]
    ep = entrypoint(compose, service)
    files = {'/sim-input/issue.json': (DEPLOY, DEPLOY, 0o644, 'file'), '/sim-input/bootstrap-dsn': (DEPLOY, DEPLOY, 0o600, 'file')} if '/sim-input' in dirs else {}
    for start in ({d: (0, 0, 0o755, 'dir') for d in dirs},  # docker created them on the bind mount
                  {},  # absent: the step creates them
                  {d: (*TARGET, 0o700, 'dir') for d in dirs}):  # a rerun
        kernel = Kernel({**start, **files} if start or not files else {**{'/sim-input': (0, 0, 0o755, 'dir')}, **files})
        kernel.run(ep)
        for d in dirs:
            assert kernel.paths[d] == (*TARGET, 0o700, 'dir'), (case, d, kernel.paths[d])
        for f in files:
            assert kernel.paths[f] == (*TARGET, 0o600, 'file'), (case, f, kernel.paths[f])


def test_the_old_order_is_refused_by_the_model():
    """The model reproduces the stage failure: chown to the target first, then chmod, on a 0755 root directory."""
    kernel = Kernel({'/sim-secrets': (0, 0, 0o755, 'dir')})
    with pytest.raises(PermissionError):
        kernel.run(['python', '-c', 'import os,sys;u,g=int(sys.argv[1]),int(sys.argv[2]);os.chown("/sim-secrets",u,g);os.chmod("/sim-secrets",0o700)'])
