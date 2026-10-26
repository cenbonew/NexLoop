#!/usr/bin/env python3
"""NX-054 P4a: install the daily relation read gap check on DATA_HOST, one command (docs/design/relation-read-p4.md §2.5;
docs/implementation/NX-054-stage-publish.md §9). Run as root through relation_gaps_install.sh:

    sudo bash relation_gaps_install.sh /path/relation-gaps.json                 the metrics DSN read from metrics_dsn_file
    <Keychain> | sudo bash relation_gaps_install.sh /path/relation-gaps.json --dsn-stdin    the DSN line on stdin (never argv)
    sudo bash relation_gaps_install.sh /path/relation-gaps.json --drill-arm 3   the timer fires once in 3 minutes (drill)
    sudo bash relation_gaps_install.sh /path/relation-gaps.json --drill-clear   the drill drop-in removed
    sudo bash relation_gaps_install.sh /path/relation-gaps.json --uninstall

Private configuration (JSON, 0600 root; deploy/examples/relation-gaps-install.data-host.example.json): schema
nexloop-relation-gaps-install/1, release_checkout + release_sha (the git checkout and the 40-hex release commit), python (the
interpreter of the unit: the host-health venv's bin/python, which has psycopg - a symbolic link chain is fine, see below), tenant_id,
evidence_dir, metrics_dsn_file (the existing G-METRICS-DSN file: nexloop_metrics over the host's socket; or --dsn-stdin),
host_health_config (the probe's /etc/nexloop/host-health.json) and host_health_install_config (the host-health install
configuration, so a later host-health reinstall keeps the timer).

Trust (root runs this code; L6 review of 861be53e): stdlib only, nothing imported from outside it. Before anything, the release
checkout and its .git (every directory, and every file outside objects/) must be root's alone, and .git a directory without
objects/info/alternates or commondir; `git show` runs with --no-replace-objects in a minimal environment (no GIT_* variable, no
system or global configuration). Root never executes the unit's interpreter (the service user does, in the sandbox), so `python` may be a link
chain: its real file must be root's and not group/other-writable, and so must every directory on its path and on the real path.

Steps (each idempotent):
  1 the system user nexloop-relation-gaps (no home, no shell; --uninstall keeps it)
  2 release: /opt/nexloop/relation-gaps/<sha>/ with relation_read_gaps.py, relation_reads.py and nexloop_eios/private_configuration.py
    from the release commit; root:root, read-only; a reused release is checked file by file against the commit; `current` -> <sha>
  3 /etc/nexloop/relation-gaps/ (root, 0711) with metrics.dsn (0600, the service user): one DSN line with exactly one user=, that user
    nexloop_metrics; written only when it changed
  4 the evidence directory (0750, the service user)
  5 the unit and timer from the release's deploy/stage/systemd/nexloop-relation-read-gaps.*.example (tokens filled); daemon-reload
  6 relation-read-gaps in the host-health timers (both files above; 0600 kept; the second write failing restores the first)
  7 one dry run in the unit's own sandbox (its [Service] settings as systemd-run properties, the evidence in a scratch directory
    under /var/lib/nexloop/relation-gaps-dry - not /tmp, which the unit's PrivateTmp hides):
    exit 0 or 1 (gaps) go on, 2 (refused, unwritten evidence) stops; then `systemctl enable --now nexloop-relation-read-gaps.timer`
--drill-arm N: a drop-in nexloop-relation-read-gaps.timer.d/50-drill.conf adds one OnCalendar N minutes from now (the timer itself
fires, so host-health sees a scheduled run); --drill-clear removes it. --uninstall: timer, units, drop-ins, DSN and release removed,
the host-health entries taken out; the evidence and the system user stay.
Output: step lines and the dry run's counts; never the DSN or a password. Exit 0 done, 1 a step failed, 2 refused.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

SCHEMA = 'nexloop-relation-gaps-install/1'
USER = 'nexloop-relation-gaps'
SHA = re.compile(r'[0-9a-f]{40}')
PATH = re.compile(r'/[A-Za-z0-9._/@-]+')
TENANT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}')
FILES = ('scripts/acceptance/relation_read_gaps.py', 'scripts/acceptance/relation_reads.py',
         'packages/eios-core/src/nexloop_eios/__init__.py', 'packages/eios-core/src/nexloop_eios/private_configuration.py')
UNITS = {'nexloop-relation-read-gaps.service': 'deploy/stage/systemd/nexloop-relation-read-gaps.service.example',
         'nexloop-relation-read-gaps.timer': 'deploy/stage/systemd/nexloop-relation-read-gaps.timer.example'}
ENTRY = {'name': 'relation-read-gaps', 'timer': 'nexloop-relation-read-gaps.timer', 'service': 'nexloop-relation-read-gaps.service'}
USER_KEY = re.compile(r"""(?:^|\s)user\s*=\s*('?)([^'\s]*)\1(?=\s|$)""")
SANDBOX_SKIP = {'Type', 'ExecStart', 'ExecStartPre', 'ExecStartPost', 'TimeoutStartSec', 'ReadWritePaths'}


class Refused(ValueError):
    pass


class Failed(RuntimeError):
    pass


def run(command, *, stdin=None, check=True):
    done = subprocess.run(command, input=stdin, capture_output=True, text=True, check=False)
    if check and done.returncode != 0:
        raise Failed(f'{Path(command[0]).name} exited {done.returncode}')
    return done.stdout


def _owned(info, owners):
    return info.st_uid in owners and not info.st_mode & 0o022


def root_owned(path, *, what, owners=(0,)):
    """Root's alone: owner root, nothing group/other-writable, no symbolic link on the way (lstat, never followed)."""
    path = Path(os.path.abspath(path))
    for part in [path, *path.parents]:
        try:
            info = os.lstat(part)
        except OSError:
            raise Refused(f'{what}: {part} unreadable') from None
        if stat.S_ISLNK(info.st_mode):
            raise Refused(f'{what}: {part} is a symbolic link (give the real path)')
        if not _owned(info, owners):
            raise Refused(f'{what}: {part} must be owned by root and not group/other-writable')


def interpreter_owned(path, *, what, owners=(0,)):
    """L6 C1 / I3: the unit's interpreter, run by the service user and never by root, may be a link chain (the host-health venv's
    bin/python -> python3 -> /usr/bin/python3.12, current -> <sha>). The path is resolved hop by hop: every directory entered must be
    root's and not group/other-writable, every link on the way must be root's (and lies in such a directory), and the real file must
    be a regular file, root's, not group/other-writable."""
    parts = [x for x in Path(os.path.abspath(path)).parts[1:]]
    current, hops = Path('/'), 0
    if not _owned(os.lstat(current), owners):
        raise Refused(f'{what}: / must be owned by root and not group/other-writable')
    while parts:
        name = parts.pop(0)
        if name in ('', '.'):
            continue
        if name == '..':
            current = current.parent
            continue
        entry = current / name
        try:
            info = os.lstat(entry)
        except OSError:
            raise Refused(f'{what}: {entry} unreadable') from None
        if stat.S_ISLNK(info.st_mode):
            hops += 1
            if hops > 40 or info.st_uid not in owners:
                raise Refused(f'{what}: {entry} (a link) must be owned by root')
            target = Path(os.readlink(entry))
            if target.is_absolute():
                current = Path('/')
                parts = list(target.parts[1:]) + parts
            else:
                parts = list(target.parts) + parts
            continue
        if not _owned(info, owners):
            raise Refused(f'{what}: {entry} must be owned by root and not group/other-writable')
        if parts:
            if not stat.S_ISDIR(info.st_mode):
                raise Refused(f'{what}: {entry} is not a directory')
            current = entry
        elif not stat.S_ISREG(info.st_mode):
            raise Refused(f'{what}: {entry} is not a regular file')


def trust_release(config, trust):
    """The checkout and its .git (L6 I2): every directory, and every file outside objects/ (refs, config, hooks, packed-refs)."""
    checkout = Path(config['release_checkout'])
    trust(checkout, what='release_checkout')
    git = checkout / '.git'
    if os.path.islink(git) or not os.path.isdir(git):
        raise Refused('release .git: a directory (a worktree pointer file or a link is refused)')
    if os.path.lexists(git / 'objects/info/alternates') or os.path.lexists(git / 'commondir'):
        raise Refused('release .git: objects from elsewhere (objects/info/alternates, commondir) are refused')
    trust(git, what='release .git')
    for path in sorted(git.rglob('*')):
        if path.is_dir() or not str(path.relative_to(git)).startswith('objects/'):
            trust(path, what='release .git')


def read_private_json(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise Refused(f'{path}: unreadable') from None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise Refused(f'{path}: a regular 0600 file of the caller')
        try:
            return json.loads(stream.read(1 << 16))
        except ValueError:
            raise Refused(f'{path}: not JSON') from None


def write_private(path, text, mode=0o600):
    """Atomically; returns whether the content changed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text() == text:
        return False
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    with os.fdopen(fd, 'w') as stream:
        stream.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    return True


def load_config(path):
    c = read_private_json(path)
    keys = {'schema', 'release_checkout', 'release_sha', 'python', 'tenant_id', 'evidence_dir', 'metrics_dsn_file', 'host_health_config',
            'host_health_install_config', '_comment'}
    if type(c) is not dict or c.get('schema') != SCHEMA or set(c) - keys:
        raise Refused(f'configuration: schema {SCHEMA} with ' + ', '.join(sorted(keys - {'schema', '_comment'})))
    for key in ('release_checkout', 'python', 'evidence_dir', 'host_health_config', 'host_health_install_config') + (('metrics_dsn_file',) if 'metrics_dsn_file' in c else ()):
        if type(c.get(key)) is not str or not PATH.fullmatch(c[key]) or '..' in c[key]:
            raise Refused(f'configuration: {key} (absolute path)')
    if type(c.get('release_sha')) is not str or not SHA.fullmatch(c['release_sha']):
        raise Refused('configuration: release_sha (40 hex)')
    if type(c.get('tenant_id')) is not str or not TENANT.fullmatch(c['tenant_id']):
        raise Refused('configuration: tenant_id')
    return c


def read_dsn(config, dsn_stdin, stdin):
    if dsn_stdin:
        text = stdin.read(8192)
    elif 'metrics_dsn_file' in config:
        try:
            text = Path(config['metrics_dsn_file']).read_text()
        except OSError:
            raise Refused('metrics_dsn_file: unreadable') from None
    else:
        raise Refused('the metrics DSN: metrics_dsn_file in the configuration, or --dsn-stdin')
    lines = [line for line in text.splitlines() if line.strip()]
    users = USER_KEY.findall(lines[0]) if len(lines) == 1 else []
    if len(lines) != 1 or len(users) != 1 or users[0][1] != 'nexloop_metrics' or len(re.findall(r'(?:^|\s)user\s*=', lines[0])) != 1 \
            or any(c in lines[0] for c in '\r\0'):
        raise Refused('the metrics DSN: one line with exactly one user=, nexloop_metrics')  # the DSN itself is never printed
    return lines[0].strip() + '\n'


GIT = ['env', '-i', 'PATH=/usr/bin:/bin', 'HOME=/nonexistent', 'GIT_CONFIG_NOSYSTEM=1', 'GIT_CONFIG_GLOBAL=/dev/null', 'git', '--no-replace-objects']


def git_show(config, path, runner):
    # A minimal environment: no GIT_* variable of root's session, no system or global configuration (L6 review of e446f673).
    return runner([*GIT, '-C', config['release_checkout'], 'show', f'{config["release_sha"]}:{path}'])


def release(root, config, runner, trust):
    base = root / 'opt/nexloop/relation-gaps'
    target = base / config['release_sha']
    base.mkdir(parents=True, exist_ok=True)
    trust(base, what='release base')
    wanted = {**{f: git_show(config, f, runner) for f in FILES}, **{'units/' + u: git_show(config, s, runner) for u, s in UNITS.items()}}
    if (target / '.complete').exists():
        # S3: a reused release, file by file against the commit (and root's alone).
        for name, text in wanted.items():
            trust(target / name, what='release')
            if (target / name).read_text() != text:
                raise Failed(f'release {config["release_sha"][:12]}: {name} differs from the commit (remove {target} and rerun)')
    else:
        if target.exists():
            for path in [target, *target.rglob('*')]:
                os.chmod(path, 0o700 if path.is_dir() else 0o600)
            shutil.rmtree(target)
        work = Path(tempfile.mkdtemp(dir=base, prefix='.build-'))
        for name, text in wanted.items():
            (work / name).parent.mkdir(parents=True, exist_ok=True)
            (work / name).write_text(text)
        (work / '.complete').write_text(config['release_sha'] + '\n')
        for path in sorted(work.rglob('*'), reverse=True):
            os.chmod(path, 0o555 if path.is_dir() else 0o444)
        os.chmod(work, 0o555)
        os.replace(work, target)
    current = base / 'current'
    if not (current.is_symlink() and os.readlink(current) == config['release_sha']):
        tmp = base / '.current'
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        os.symlink(config['release_sha'], tmp)
        os.replace(tmp, current)
    return target


def render_service(text, config):
    for token, value in (('@PYTHON@', config['python']), ('@TENANT_ID@', config['tenant_id']), ('@EVIDENCE_DIR@', config['evidence_dir'])):
        text = text.replace(token, value)
    if '@' in ''.join(line for line in text.splitlines() if not line.startswith('#')):
        raise Failed('service template: an unfilled token')
    return text


def sandbox(service_text):
    """S2: (key, value) of the unit's [Service] section, as the dry run's transient properties (ReadWritePaths: the scratch one)."""
    out, section = [], None
    for line in service_text.splitlines():
        line = line.strip()
        if not line or line.startswith(('#', ';')):
            continue
        if line.endswith('\\') or '"' in line or "'" in line:
            raise Failed('the unit uses a continuation line or a quoted value: the dry-run sandbox cannot mirror it')
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1]
            continue
        key, sep, value = line.partition('=')
        if section == 'Service' and sep and key.strip() not in SANDBOX_SKIP:
            out.append((key.strip(), value.strip()))
    return out


def timers_of(value, nested, path):
    probe = value.get('probe') if nested else value
    if type(probe) is not dict or type(probe.get('timers', [])) is not list or not all(type(t) is dict for t in probe.get('timers', [])):
        raise Refused(f'{path}: no probe timers list')  # S5: a malformed file is refused, never a traceback
    return probe


def host_health_files(root, config):
    return [(root / config['host_health_config'].lstrip('/'), False), (root / config['host_health_install_config'].lstrip('/'), True)]


def rewrite_timers(files, change):
    """Both host-health files or neither (S5): the first is restored when the second cannot be written."""
    plans = []
    for path, nested in files:
        before = path.read_text() if path.exists() else None
        value = read_private_json(path)
        probe = timers_of(value, nested, path)
        if change(probe):
            plans.append((path, before, json.dumps(value, indent=2) + '\n'))
    done = []
    try:
        for path, before, text in plans:
            write_private(path, text)
            done.append((path, before))
    except OSError:
        for path, before in done:
            if before is not None:
                write_private(path, before)
        raise
    return [path for path, _, _ in plans]


def add_entry(probe):
    timers = probe.setdefault('timers', [])
    if any(t.get('name') == ENTRY['name'] for t in timers):
        return False
    timers.append(dict(ENTRY))
    return True


def remove_entry(probe):
    timers = probe.get('timers', [])
    if not any(t.get('name') == ENTRY['name'] for t in timers):
        return False
    probe['timers'] = [t for t in timers if t.get('name') != ENTRY['name']]
    return True


def install(root, config, dsn, runner, out, trust, trust_python):
    step = lambda n, text: print(f'step {n}: {text}', file=out, flush=True)  # noqa: E731
    trust_release(config, trust)
    trust_python(config['python'], what='python')
    if runner(['id', '-u', USER], check=False) == '':
        runner(['useradd', '--system', '--no-create-home', '--home-dir', '/nonexistent', '--shell', '/usr/sbin/nologin', USER])
    step(1, f'user {USER}')
    target = release(root, config, runner, trust)
    step(2, 'release ' + config['release_sha'][:12])
    secrets = root / 'etc/nexloop/relation-gaps'
    secrets.mkdir(parents=True, exist_ok=True)
    os.chmod(secrets, 0o711)  # S1: root's; the service user reaches its file, owns nothing in the path
    changed = write_private(secrets / 'metrics.dsn', dsn)
    runner(['chown', f'{USER}:{USER}', str(secrets / 'metrics.dsn')])
    step(3, 'metrics DSN ' + ('written' if changed else 'unchanged'))
    evidence = root / config['evidence_dir'].lstrip('/')
    evidence.mkdir(parents=True, exist_ok=True)
    os.chmod(evidence, 0o750)
    runner(['chown', f'{USER}:{USER}', str(evidence)])
    step(4, 'evidence directory')
    systemd = root / 'etc/systemd/system'
    systemd.mkdir(parents=True, exist_ok=True)
    service = render_service((target / 'units/nexloop-relation-read-gaps.service').read_text(), config)
    for name, text in (('nexloop-relation-read-gaps.service', service),
                       ('nexloop-relation-read-gaps.timer', (target / 'units/nexloop-relation-read-gaps.timer').read_text())):
        write_private(systemd / name, text, mode=0o644)
    runner(['systemctl', 'daemon-reload'])
    step(5, 'units installed')
    added = rewrite_timers(host_health_files(root, config), add_entry)
    step(6, 'host-health timers: ' + ('added to ' + ', '.join(p.name for p in added) if added else 'already listed'))
    # L6 C2: outside /tmp - the sandbox has the unit's PrivateTmp, a host /tmp directory would not be there.
    dry = root / 'var/lib/nexloop/relation-gaps-dry'
    dry.mkdir(parents=True, exist_ok=True)
    os.chmod(dry, 0o755)
    scratch = Path(tempfile.mkdtemp(dir=dry, prefix='run-'))
    runner(['chown', f'{USER}:{USER}', str(scratch)])
    try:
        answer = runner(['systemd-run', '--quiet', '--wait', '--pipe', '--collect', '--service-type=exec',
                         *[a for key, value in sandbox(service) for a in ('-p', f'{key}={value}')], '-p', f'ReadWritePaths={scratch}',
                         config['python'], '-I', '-B', '/opt/nexloop/relation-gaps/current/scripts/acceptance/relation_read_gaps.py',
                         '--dsn-file', '/etc/nexloop/relation-gaps/metrics.dsn', '--tenant', config['tenant_id'], '--evidence-dir', str(scratch)], check=False)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    try:
        doc = json.loads(answer.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise Failed('dry run gave no answer (the metrics login, the DSN or the interpreter): timer not enabled') from None
    if type(doc) is not dict or 'refused' in doc or doc.get('evidence') is None:
        raise Failed('dry run refused or wrote no evidence: timer not enabled')
    print('dry-run: ' + json.dumps({k: doc.get(k) for k in ('gaps', 'gaps_erasing', 'dangling', 'relation_only')}, sort_keys=True), file=out)
    runner(['systemctl', 'enable', '--now', 'nexloop-relation-read-gaps.timer'])
    step(7, 'timer enabled' + (' (gaps > 0: the first scheduled run will fail and alert)' if doc.get('gaps') else ''))


def drill_arm(root, minutes, runner, out, clock=datetime.now):
    if not 1 <= minutes <= 60:
        raise Refused('--drill-arm MINUTES: 1-60')
    when = (clock() + timedelta(minutes=minutes)).strftime('%Y-%m-%d %H:%M:00')
    dropin = root / 'etc/systemd/system/nexloop-relation-read-gaps.timer.d/50-drill.conf'
    write_private(dropin, f'# NX-054 drill: one scheduled run of this timer (relation_gaps_install.py --drill-clear removes it)\n[Timer]\nOnCalendar={when}\n',
                  mode=0o644)
    runner(['systemctl', 'daemon-reload'])
    runner(['systemctl', 'restart', 'nexloop-relation-read-gaps.timer'])
    print(f'drill: the timer fires at {when} (host time); then --drill-clear', file=out)


def drill_clear(root, runner, out):
    path = root / 'etc/systemd/system/nexloop-relation-read-gaps.timer.d/50-drill.conf'
    if path.exists():
        path.unlink()
    runner(['systemctl', 'daemon-reload'])
    runner(['systemctl', 'restart', 'nexloop-relation-read-gaps.timer'], check=False)
    print('drill: drop-in removed', file=out)


def uninstall(root, config, runner, out):
    files = [(p, n) for p, n in host_health_files(root, config) if p.exists()]
    for path, nested in files:  # a malformed host-health file is refused before anything is removed
        timers_of(read_private_json(path), nested, path)
    runner(['systemctl', 'disable', '--now', 'nexloop-relation-read-gaps.timer'], check=False)
    systemd = root / 'etc/systemd/system'
    for d in ('nexloop-relation-read-gaps.timer.d', 'nexloop-relation-read-gaps.service.d'):
        if (systemd / d).is_dir():
            shutil.rmtree(systemd / d)
    for path in (systemd / 'nexloop-relation-read-gaps.timer', systemd / 'nexloop-relation-read-gaps.service', root / 'etc/nexloop/relation-gaps/metrics.dsn'):
        if path.exists() or path.is_symlink():
            path.unlink()
    shutil.rmtree(root / 'var/lib/nexloop/relation-gaps-dry', ignore_errors=True)
    base = root / 'opt/nexloop/relation-gaps'
    if base.exists():
        for path in [base, *base.rglob('*')]:
            if not path.is_symlink():
                os.chmod(path, 0o700 if path.is_dir() else 0o600)
        shutil.rmtree(base)
    rewrite_timers(files, remove_entry)
    runner(['systemctl', 'daemon-reload'], check=False)
    print(f'uninstalled: timer, units, drop-ins, DSN and release removed; host-health entries taken out; the evidence and the user {USER} stay', file=out)


def main(argv=None, stdin=sys.stdin, out=sys.stdout, runner=run, root=Path('/'), trust=root_owned, trust_python=interpreter_owned,
         clock=datetime.now):
    p = argparse.ArgumentParser(description='NX-054 relation read gap check install (DATA_HOST, root, one command)')
    p.add_argument('config')
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--dsn-stdin', action='store_true')
    mode.add_argument('--drill-arm', type=int, metavar='MINUTES')
    mode.add_argument('--drill-clear', action='store_true')
    mode.add_argument('--uninstall', action='store_true')
    a = p.parse_args(argv)
    os.umask(0o022)
    try:
        config = load_config(a.config)
        if a.uninstall:
            uninstall(root, config, runner, out)
        elif a.drill_arm is not None:
            drill_arm(root, a.drill_arm, runner, out, clock)
        elif a.drill_clear:
            drill_clear(root, runner, out)
        else:
            install(root, config, read_dsn(config, a.dsn_stdin, stdin), runner, out, trust, trust_python)
    except Refused as error:
        print(json.dumps({'refused': str(error)}), file=out)
        return 2
    except (Failed, OSError) as error:
        print(json.dumps({'failed': str(error) if isinstance(error, Failed) else type(error).__name__}), file=out)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
