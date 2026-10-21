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
    'compose.simulation.example.yaml/sim-setup-dir': ('/sim-secrets', '/sim-tokens', '/sim-runtime', '/sim-artifacts'),  # runtime and artifact roots too
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


LAUNCHERS = {'compose.simulation.example.yaml': 'sim-setup', 'compose.sim-issue.example.yaml': 'sim-issue'}


def launcher(compose, release):
    spec = yaml.safe_load((ROOT / 'deploy/stage' / compose).read_text())['services'][LAUNCHERS[compose]]
    assert spec['working_dir'] == '/' and spec['entrypoint'][:2] == ['python', '-c'] and len(spec['entrypoint']) == 3
    return spec['entrypoint'][2].replace('/opt/nexloop-release', str(release))


@pytest.mark.parametrize('compose', sorted(LAUNCHERS))
def test_the_tool_containers_check_that_the_release_export_is_readable_first(compose, tmp_path):
    """Stage finding 2026-10-22: the release checkout must be readable by the container's uid (NEXLOOP_SIM_UID for sim-setup,
    NEXLOOP_PG_UID for sim-issue). The launcher checks it before anything runs and says how to fix it; a readable export runs the named
    script of the checkout as __main__, in the checkout, with its arguments; a name outside scripts/ is refused."""
    import json
    import os
    import subprocess
    import sys
    release = tmp_path / 'release'
    for d in ('scripts/deploy', 'scripts/acceptance', 'packages/eios-core/src'):
        (release / d).mkdir(parents=True)
    (release / 'scripts/deploy/probe.py').write_text('import json,os,sys\nif __name__ == "__main__":\n    print(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()}))\n    sys.exit(3)\n')
    code = launcher(compose, release)
    ran = subprocess.run([sys.executable, '-c', code, 'scripts/deploy/probe.py', '--config', '/setup/x.json'], capture_output=True, text=True, cwd='/')
    assert ran.returncode == 3 and json.loads(ran.stdout) == {'argv': ['--config', '/setup/x.json'], 'cwd': os.path.realpath(release)}, ran.stderr
    for bad in ([], ['../etc/passwd'], ['/etc/passwd'], ['scripts/../../x.py']):
        out = subprocess.run([sys.executable, '-c', code, *bad], capture_output=True, text=True, cwd='/')
        assert out.returncode == 2 and json.loads(out.stdout)['ok'] is False, bad
    # The deployment's root-only checkout: not readable by this uid (modelled by removing the read and search bits).
    (release / 'scripts/deploy').chmod(0)
    try:
        out = subprocess.run([sys.executable, '-c', code, 'scripts/deploy/probe.py'], capture_output=True, text=True, cwd='/')
    finally:
        (release / 'scripts/deploy').chmod(0o755)
    detail = json.loads(out.stdout)['detail']
    assert out.returncode == 2 and f'uid {os.getuid()}' in detail and 'scripts/deploy' in detail and 'git archive' in detail


def test_the_runtime_and_artifact_directories_are_the_ones_the_services_mount():
    """Stage finding 2026-10-22: the Host's runtime root (NEXLOOP_SIM_RUNTIME_DIR) and the worker's artifact root (NEXLOOP_SIM_ARTIFACT_DIR)
    must be the simulation uid's 0700; sim-setup-dir prepares exactly the host directories those services mount."""
    services = yaml.safe_load((ROOT / 'deploy/stage/compose.simulation.example.yaml').read_text())['services']
    prepared = {v.split(':')[0] for v in services['sim-setup-dir']['volumes']}
    for service, target in (('sim-agent-host', '/private/runtime'), ('sim-runtime-worker', '/var/lib/nexloop/artifacts')):
        source = next(v.rsplit(':', 1)[0] for v in services[service]['volumes'] if v.endswith(':' + target))
        assert source.split(':?')[0].removesuffix('}') in {p.split(':?')[0].removesuffix('}') for p in prepared}, (service, source)


def test_the_host_launcher_names_why_a_runtime_root_is_refused(tmp_path):
    """Stage finding 2026-10-22: with a runtime root of another owner the Host only said 'startup refused or unavailable' and restarted.
    The launcher now names the reason - uids and modes only."""
    import os
    import shutil
    import subprocess
    import sys
    node = shutil.which('node')
    if not node or not (ROOT / 'apps/agent-host/dist/main.js').is_file():
        pytest.fail('Node and the built Agent Host are required')
    def start(root):
        return subprocess.run([sys.executable, str(ROOT / 'scripts/agent_host.py'), '--node', node, '--runtime-root', str(root), '--internal-key-file',
                               str(tmp_path / 'k'), '--port', '18999', '--tls-certificate-file', str(tmp_path / 'c'), '--tls-key-file', str(tmp_path / 'k')],
                              capture_output=True, text=True, timeout=30)
    wrong = tmp_path / 'runtime'
    wrong.mkdir(mode=0o755)
    wrong.chmod(0o755)
    out = start(wrong)
    assert out.returncode == 1 and f'a directory of uid {os.getuid()} with mode 0700 (found uid {os.getuid()}, mode 0755)' in out.stderr
    assert 'startup refused or unavailable' in out.stderr
    assert 'missing' in start(tmp_path / 'absent').stderr


def _tar(entries):
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w') as archive:
        for name, kind, data in entries:
            info = tarfile.TarInfo(name)
            if kind == 'file':
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            elif kind == 'dir':
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            else:
                info.type, info.linkname = tarfile.SYMTYPE, '/etc/passwd'
                archive.addfile(info)
    buf.seek(0)
    return buf


def test_files_reach_the_pg_uid_input_directory_as_a_tar_stream(tmp_path):
    """Stage finding 2026-10-22 (steps 7, 12, 14): DATA_HOST's input directory is NEXLOOP_PG_UID's 0700, so the dispatcher sends files
    as a tar stream to sim_input_put.py in the sim-issue-input container (that uid, input directory writable, no network, no socket).
    Regular files with plain names only, written 0600 and replaced atomically; anything else refuses the whole stream unwritten."""
    import io
    import json
    import os
    import sys
    sys.path.insert(0, str(ROOT / 'scripts/deploy'))
    import sim_input_put as P
    spec = yaml.safe_load((ROOT / 'deploy/stage/compose.sim-issue.example.yaml').read_text())['services']['sim-issue-input']
    assert spec['network_mode'] == 'none' and spec['user'] == '${NEXLOOP_PG_UID}:${NEXLOOP_PG_GID}' and spec['cap_drop'] == ['ALL']
    assert [v.split(':')[-1] for v in spec['volumes']] == ['ro', '/sim-input'] and not any('postgresql' in v or 'sim-tokens' in v for v in spec['volumes'])
    d = tmp_path / 'input'
    d.mkdir(mode=0o700)
    (d / 'issue.json').write_text('old')

    def put(entries, directory=d):
        out = io.StringIO()
        code = P.main(['--dir', str(directory)], stdin=_tar(entries), out=out)
        return code, json.loads(out.getvalue())
    code, out = put([('issue.json', 'file', b'{"new": 1}'), ('at046-probe-before.json', 'file', b'{}')])
    assert code == 0 and out['written'] == ['at046-probe-before.json', 'issue.json'] and '"new"' not in json.dumps(out)
    assert (d / 'issue.json').read_text() == '{"new": 1}' and all(p.stat().st_mode & 0o777 == 0o600 for p in d.iterdir())
    for bad in ([('sub', 'dir', b'')], [('link', 'link', b'')], [('a/b.json', 'file', b'x')], [('../x', 'file', b'x')],
                [('big', 'file', b'x' * (P.MAX_FILE + 1))], [('ok.json', 'file', b'x'), ('ok.json', 'file', b'y')], []):
        before = sorted(p.name for p in d.iterdir())
        code, out = put([('fresh.json', 'file', b'x'), *bad] if bad else [])
        assert code == 2 and out['ok'] is False and sorted(p.name for p in d.iterdir()) == before, bad
    loose = tmp_path / 'loose'
    loose.mkdir(mode=0o755)
    loose.chmod(0o755)
    assert put([('a.json', 'file', b'x')], loose)[0] == 2 and not os.listdir(loose)
