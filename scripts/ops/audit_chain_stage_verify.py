#!/usr/bin/env python3
"""NX-059 stage acceptance (docs/release/run-nx059-stage-verify.md): one configuration file, one command, on the owner's management
machine. The seal key comes from the macOS Keychain on stdin and is never an argument, never printed, never left on a disk:

  security find-generic-password -a nexloop -s nexloop/stage/audit-seal-key -w \\
    | python3 scripts/ops/audit_chain_stage_verify.py --config <file>         (add --dry-run to print the plan only)

Steps (every database step runs on DATA_HOST in a one-shot backend container as UID 999, as the other data-host tools):
  0  preflight   the release tree on DATA_HOST ($RUN/src) has the same audit chain tool and module as this checkout
  1  verify      scripts/ops/audit_chain.py verify --key-file <tmpfs file> --tenant T: the database's verification plus every seal's MAC;
                 the key reaches the container on stdin and lives only in its tmpfs (/tmp, 0600, owned by 999)
  2  export      audit_chain.export of the tenant into the container's tmpfs, streamed back as a tar into a new local directory;
                 nothing is written on DATA_HOST
  3  offline     scripts/ops/audit_chain.py verify-export on this machine (standard library only), with the key in a private
                 temporary file (0600, removed at exit)
  4  agreement   both valid; the export's head is not behind the verified head; the export has at least as many seals

Output: one JSON line per step (codes, numbers, key ids only), then a summary line. Exit 0 = all valid, 1 = a problem was found
(the step's JSON names it), 2 = configuration, usage or connection error.
"""
import argparse
import hashlib
import io
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile

SCHEMA = 'nexloop-audit-chain-stage-verify/1'
FIELDS = {'schema', 'data_host', 'run_dir', 'nexloop_root', 'backend_image', 'tenant_id', 'out_dir'}
TOOL = 'scripts/ops/audit_chain.py'
MODULE = 'packages/eios-core/src/nexloop_eios/audit_chain.py'
ROOT = pathlib.Path(__file__).resolve().parents[2]
KEY_LINE = re.compile(r'[0-9a-f]{64}')

# Inside the container: the key from stdin into the tmpfs (0600, created exclusively), then the unchanged CLI.
VERIFY = """import os,sys,runpy
data=sys.stdin.buffer.read();fd=os.open('/tmp/seal.key',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
os.write(fd,data);os.close(fd);del data
sys.argv=['audit_chain.py','verify','--database-url-file','/run/bootstrap-dsn','--key-file','/tmp/seal.key','--tenant',sys.argv[1]]
try:runpy.run_path('/opt/release/scripts/ops/audit_chain.py',run_name='__main__')
finally:os.unlink('/tmp/seal.key')
"""
# Inside the container: the unchanged CLI export into the tmpfs (its manifest line to stderr), then a tar of it on stdout.
EXPORT = """import contextlib,runpy,sys,tarfile
cli=runpy.run_path('/opt/release/scripts/ops/audit_chain.py',run_name='audit_chain_cli')
with contextlib.redirect_stdout(sys.stderr):
    code=cli['main'](['export','--database-url-file','/run/bootstrap-dsn','--tenant',sys.argv[1],'--out','/export/audit-export'])
if code:sys.exit(code)
with tarfile.open(fileobj=sys.stdout.buffer,mode='w|') as t:t.add('/export/audit-export',arcname='audit-export')
"""
DIGEST = """import hashlib,sys
for p in sys.argv[1:]:print(hashlib.sha256(open('/opt/release/'+p,'rb').read()).hexdigest(),p)
"""


class Stop(Exception):
    pass


def load(path):
    try:
        config = json.loads(pathlib.Path(path).read_text())
    except (OSError, ValueError):
        raise Stop('configuration unreadable') from None
    if type(config) is not dict or set(config) != FIELDS or config['schema'] != SCHEMA:
        raise Stop(f'configuration: exactly the fields {sorted(FIELDS)} with schema {SCHEMA}')
    for name in ('data_host', 'run_dir', 'nexloop_root', 'backend_image', 'tenant_id', 'out_dir'):
        if type(config[name]) is not str or not config[name] or '\n' in config[name]:
            raise Stop(f'configuration: {name}')
    if not config['backend_image'].count('@sha256:') == 1:
        raise Stop('configuration: backend_image must be pinned by digest (name@sha256:...)')
    return config


def docker(config, script, *args, tmpfs_export=False):
    """The data-host one-shot container: no network, read-only, UID 999, the release tree and the bootstrap socket DSN read-only."""
    run, root = config['run_dir'], config['nexloop_root']
    command = ['docker', 'run', '-i', '--rm', '--network', 'none', '--read-only', '--user', '999:999', '--cap-drop', 'ALL',
               '--security-opt', 'no-new-privileges', '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m,mode=0700,uid=999,gid=999']
    if tmpfs_export:
        command += ['--tmpfs', '/export:rw,noexec,nosuid,size=512m,mode=0700,uid=999,gid=999']
    command += ['-v', f'{run}/src:/opt/release:ro', '-v', f'{root}/run/postgresql:/var/run/postgresql', '-v', f'{run}/boot.dsn:/run/bootstrap-dsn:ro',
                '-e', 'PYTHONPATH=/opt/release/packages/eios-core/src', '--entrypoint', 'python', config['backend_image'], '-c', script, *args]
    return shlex.join(command)


def remote(config, command, *, stdin=b'', dry):
    if dry:
        print(json.dumps({'step': 'plan', 'host': 'DATA_HOST', 'command': command}))
        return 0, b'', b''
    done = subprocess.run(['ssh', '-o', 'BatchMode=yes', config['data_host'], command], input=stdin, capture_output=True, timeout=1800)
    return done.returncode, done.stdout, done.stderr


def report(step, value):
    print(json.dumps({'step': step, **value}, ensure_ascii=False), flush=True)


def read_key():
    if sys.stdin.isatty():
        raise Stop('the key is read from a pipe (security find-generic-password ... -w | ...), not a terminal')
    text = sys.stdin.read(4096)
    lines = text.split()
    if not lines or len(lines) > 8 or any(not KEY_LINE.fullmatch(line) for line in lines):
        raise Stop('seal key on stdin: one to eight 64-hex lines, the first the active key')
    return ('\n'.join(lines) + '\n').encode()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--config', required=True)
    parser.add_argument('--dry-run', action='store_true', help='print the DATA_HOST commands; read no key, connect nowhere')
    args = parser.parse_args(argv)
    try:
        config = load(args.config)
        tenant, dry = config['tenant_id'], args.dry_run
        key = b'' if dry else read_key()
        # 0 preflight: the same tool and module on DATA_HOST as in this checkout (the commands below were checked against it).
        local = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in (TOOL, MODULE)}
        code, out, _ = remote(config, docker(config, DIGEST, TOOL, MODULE), dry=dry)
        if not dry:
            found = dict(reversed(line.split(' ', 1)) for line in out.decode().splitlines() if ' ' in line)
            if code != 0 or found != local:
                report('preflight', {'ok': False, 'code': 'release_tree_differs' if code == 0 else 'release_tree_unreadable'})
                return 2
            report('preflight', {'ok': True, 'files': sorted(local)})
        # 1 verify with the key (on stdin to the container only).
        code, out, err = remote(config, docker(config, VERIFY, tenant), stdin=key, dry=dry)
        if not dry:
            if code not in (0, 1) or not out.strip():
                report('verify', {'ok': False, 'code': 'verify_unavailable', 'exit': code, 'error': err.decode()[-200:].strip()})
                return 2
            verified = json.loads(out.decode().strip().splitlines()[-1])
            report('verify', verified)
        # 2 export, streamed back; nothing stays on DATA_HOST.
        code, out, err = remote(config, docker(config, EXPORT, tenant, tmpfs_export=True), dry=dry)
        if dry:
            report('plan', {'host': 'local', 'then': ['offline verify-export with the key in a private temporary file', 'agreement checks']})
            return 0
        if code != 0:
            report('export', {'ok': False, 'code': 'export_unavailable', 'exit': code, 'error': err.decode()[-200:].strip()})
            return 2
        target = pathlib.Path(config['out_dir']) / f'audit-export-{tenant[:12]}-{os.getpid()}'
        target.mkdir(mode=0o700, parents=False)
        with tarfile.open(fileobj=io.BytesIO(out), mode='r|') as archive:
            archive.extractall(target, filter='data')
        directory = target / 'audit-export'
        manifest = json.loads((directory / 'manifest.json').read_text())
        report('export', {'directory': str(directory), 'entries': manifest['entries'], 'head_seq': manifest['head_seq'], 'files': manifest['files']})
        # 3 offline verification here, with the key in a private temporary file.
        with tempfile.TemporaryDirectory() as private:
            path = pathlib.Path(private) / 'seal.key'
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(fd, key)
            os.close(fd)
            done = subprocess.run([sys.executable, str(ROOT / TOOL), 'verify-export', '--key-file', str(path), '--dir', str(directory)],
                                  capture_output=True, env={**os.environ, 'PYTHONPATH': str(ROOT / 'packages/eios-core/src')})
        if done.returncode not in (0, 1) or not done.stdout.strip():
            report('offline', {'ok': False, 'code': 'offline_unavailable', 'exit': done.returncode, 'error': done.stderr.decode()[-200:].strip()})
            return 2
        offline = json.loads(done.stdout.decode().strip().splitlines()[-1])
        report('offline', offline)
        # 4 agreement: the export was taken after the verification (the seal cron may append and seal in between).
        problems = []
        if offline['tenant_id'] != tenant or verified['tenant_id'] != tenant:
            problems.append('tenant_differs')
        if manifest['head_seq'] < verified['head_seq']:
            problems.append('export_behind_verified_head')
        if offline['seals'] < verified['seals']:
            problems.append('export_has_fewer_seals')
        valid = verified['valid'] and offline['valid'] and not problems
        report('summary', {'valid': valid, 'tenant_id': tenant, 'verified_head_seq': verified['head_seq'], 'export_head_seq': manifest['head_seq'],
                           'verified_seals': verified['seals'], 'export_seals': offline['seals'], 'agreement_problems': problems})
        return 0 if valid else 1
    except Stop as stop:
        print(f'audit chain stage verify: {stop}', file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, tarfile.TarError) as error:
        print(f'audit chain stage verify: {type(error).__name__}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
