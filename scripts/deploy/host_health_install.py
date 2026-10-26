#!/usr/bin/env python3
"""Host health (E2/E3) install on one host, one command (docs/implementation/host-health.md §4). Run as root through
host_health_install.sh, with the recorder password(s) on stdin:

    APP_HOST (dispatcher):  printf 'app_host\\t%s\\n' "$APP_PW" | sudo bash host_health_install.sh /path/app-host.json
    DATA_HOST (owner):      printf 'data_host\\t%s\\napp_host\\t%s\\n' "$DATA_PW" "$APP_PW" | sudo bash host_health_install.sh /path/data-host.json
    either:                 sudo bash host_health_install.sh /path/config.json --uninstall

Private configuration (JSON, 0600 root; deploy/examples/host-health-install.example.json): schema nexloop-host-health-install/1,
host_role (app_host | data_host), release_checkout + release_sha (the git checkout and the 40-hex release commit), python (the
interpreter the venv is made from), pip_index_url (the PyPI mirror), database {host, port, dbname, sslmode, sslrootcert}, probe
{containers, services, timers, grace_seconds, local_window_seconds, local_min_restarts}, and on DATA_HOST bootstrap_dsn_file plus
hba {nexloop_root, stage_env} (the pg_hba lines and both role passwords are set there).

Steps (each idempotent; a rerun converges, nothing done twice):
  1 data_host only: the pg_hba lines (host_health_hba_data_host.sh) and both recorder passwords (host_recorder_password.py; the
    app_host line on stdin is used only here)
  2 release: /opt/nexloop/host-health/<sha>/ with the probe from `git show <sha>:...` and a venv with psycopg only
    (deploy/host-health/requirements.txt, pip --require-hashes --no-deps from the mirror); root:root, nothing group/other-writable,
    made read-only; `current` -> <sha> (an existing complete <sha> is reused)
  3 /etc/nexloop/host-health.json (0600 root) from the configuration's probe block
  4 /etc/nexloop/host-recorder.dsn (0600 root): this host's role and password - written only when it changed
  5 the unit and timer from the release's deploy/stage/systemd/nexloop-host-health.*.example; daemon-reload
  5b memory_deny_write_execute (default true, the unit's W^X): only an explicit false writes
     nexloop-host-health.service.d/50-installer-mdwe.conf (MemoryDenyWriteExecute=no); true removes it
  6 one --dry-run of the probe inside the unit's own sandbox (systemd-run with the unit's [Service] settings and drop-ins as
    properties; its summary printed), then `systemctl enable --now nexloop-host-health.timer`
--uninstall: timer disabled and stopped, units and their drop-in directory, /etc/nexloop/host-health.json and host-recorder.dsn, /opt/nexloop/host-health and
the state directory removed (the database roles and pg_hba lines stay: without a DSN nobody connects).
Output: step lines and the probe's dry-run summary; never a password or the DSN. Exit 0 done, 1 a step failed, 2 refused.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

SCHEMA = 'nexloop-host-health-install/1'
ROLES = {'data_host': 'nexloop_host_recorder_data_host', 'app_host': 'nexloop_host_recorder_app_host'}
SHA = re.compile(r'[0-9a-f]{40}')
PATH = re.compile(r'/[A-Za-z0-9._/-]+')
PROBE = 'scripts/deploy/host_health_probe.py'
REQUIREMENTS = 'deploy/host-health/requirements.txt'
UNITS = {'nexloop-host-health.service': 'deploy/stage/systemd/nexloop-host-health.service.example',
         'nexloop-host-health.timer': 'deploy/stage/systemd/nexloop-host-health.timer.example'}
HERE = Path(__file__).resolve().parent


class Refused(ValueError):
    pass


class Failed(RuntimeError):
    pass


def root_owned(path, *, what):
    """L6 rollout review I1/I2: root runs this code, so it and every directory above it must be root's alone - owner uid 0, nothing
    group/other-writable, no symbolic link on the way (lstat, never followed). Anything else could let another account choose
    what root executes."""
    path = Path(os.path.abspath(path))
    for part in [path, *path.parents]:
        try:
            info = os.lstat(part)
        except OSError:
            raise Refused(f'{what}: {part} unreadable') from None
        if stat.S_ISLNK(info.st_mode):
            raise Refused(f'{what}: {part} is a symbolic link (give the real path)')
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise Refused(f'{what}: {part} must be owned by root and not group/other-writable')


def trusted_code(config, trust):
    """Everything root will execute from outside the release it builds: the checkout (git show reads it; the DATA_HOST steps run its
    scripts), the interpreter the venv is made from, and this installer's own directory with the scripts it calls."""
    trust(config['release_checkout'], what='release_checkout')
    trust_git(config['release_checkout'], trust)
    trust(config['python'], what='python')
    # R2: whole trees - root imports nexloop_eios (host_recorder_password.py, render.py) and runs scripts/deploy, on both hosts.
    checkout = Path(config['release_checkout'])
    for tree, what in ((HERE, 'installer'), (checkout / 'scripts/deploy', 'scripts/deploy'), (checkout / 'packages/eios-core/src', 'packages/eios-core/src')):
        trust(tree, what=what)
        for path in sorted(tree.rglob('*')):  # N1: __pycache__ too - root's interpreters also run with -B and never write one
            trust(path, what=what)


def run(command, *, stdin=None, check=True):
    done = subprocess.run(command, input=stdin, capture_output=True, text=True, check=False)
    if check and done.returncode != 0:
        raise Failed(f'{Path(command[0]).name} exited {done.returncode}' + _refusal(done.stdout))
    return done.stdout


def _refusal(stdout):
    """The refusal a called tool printed as its last line ({"refused": ...} or {"failed": ...}: codes and reasons, never a secret -
    host_recorder_password.py and the probe print no DSN or password), so a failed step says why."""
    lines = (stdout or '').strip().splitlines()
    try:
        doc = json.loads(lines[-1]) if lines else None
    except ValueError:
        return ''
    if type(doc) is not dict:
        return ''
    reason = doc.get('refused') or doc.get('failed')
    return f': {reason}'[:400] if type(reason) is str else ''


def read_private_json(path, *, owner_root=True):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or (owner_root and info.st_uid != os.geteuid()):
            raise Refused('configuration: a regular 0600 file of the caller')
        try:
            return json.loads(stream.read(1 << 16))
        except ValueError:
            raise Refused('configuration: not JSON') from None


def load_config(path):
    c = read_private_json(path)
    if type(c) is not dict or c.get('schema') != SCHEMA or c.get('host_role') not in ROLES:
        raise Refused(f'configuration: schema {SCHEMA}, host_role app_host | data_host')
    for key in ('release_checkout', 'python'):
        if type(c.get(key)) is not str or not PATH.fullmatch(c[key]) or '..' in c[key]:
            raise Refused(f'configuration: {key} (absolute path)')
    if type(c.get('release_sha')) is not str or not SHA.fullmatch(c['release_sha']):
        raise Refused('configuration: release_sha (40 hex)')
    if type(c.get('pip_index_url')) is not str or not re.fullmatch(r'https://[A-Za-z0-9.:/_-]+', c['pip_index_url']):
        raise Refused('configuration: pip_index_url (https)')
    db = c.get('database')
    if (type(db) is not dict or set(db) - {'host', 'port', 'dbname', 'sslmode', 'sslrootcert'} or type(db.get('host')) is not str
            or not re.fullmatch(r'[A-Za-z0-9._/-]+', db['host']) or type(db.get('port', 5432)) is not int
            or type(db.get('dbname')) is not str or not re.fullmatch(r'[a-z_][a-z0-9_]{0,62}', db['dbname'])):
        raise Refused('configuration: database {host, port, dbname, sslmode, sslrootcert}')
    if c['host_role'] == 'data_host':
        if not db['host'].startswith('/'):
            raise Refused('configuration: DATA_HOST connects over the local socket (database.host is its directory)')
        hba = c.get('hba')
        if (type(hba) is not dict or set(hba) != {'nexloop_root', 'stage_env'} or any(type(v) is not str or not PATH.fullmatch(v) for v in hba.values())
                or type(c.get('bootstrap_dsn_file')) is not str or not PATH.fullmatch(c['bootstrap_dsn_file'])):
            raise Refused('configuration: DATA_HOST needs hba {nexloop_root, stage_env} and bootstrap_dsn_file')
    else:
        if db['host'].startswith('/') or db.get('sslmode') != 'verify-full' or type(db.get('sslrootcert')) is not str or not PATH.fullmatch(db['sslrootcert']):
            raise Refused('configuration: APP_HOST connects over hostssl with sslmode verify-full and sslrootcert (the server CA)')
    if type(c.get('memory_deny_write_execute', True)) is not bool:
        raise Refused('configuration: memory_deny_write_execute true | false (default true)')
    probe = c.get('probe')
    if type(probe) is not dict or set(probe) - {'containers', 'services', 'timers', 'grace_seconds', 'local_window_seconds', 'local_min_restarts'}:
        raise Refused('configuration: probe {containers, services, timers, ...}')
    return c


def read_passwords(stream, host_role):
    out = {}
    for line in stream.read(1 << 14).splitlines():
        if not line.strip():
            continue
        host, sep, password = line.partition('\t')
        if not sep or host not in ROLES or host in out or not 16 <= len(password) <= 256 or any(c in password for c in '\r\n\0'):
            raise Refused('stdin: <host_role><TAB>password lines (16-256 characters)')
        out[host] = password
    needed = {'data_host', 'app_host'} if host_role == 'data_host' else {'app_host'}
    if set(out) != needed:
        raise Refused('stdin: ' + ' and '.join(sorted(needed)) + ' password line(s), no other')
    return out


def _quote(value):
    return "'" + str(value).replace('\\', '\\\\').replace("'", "\\'") + "'"


def recorder_dsn(config, password):
    db = config['database']
    parts = {'host': db['host'], 'port': db.get('port', 5432), 'dbname': db['dbname'], 'user': ROLES[config['host_role']], 'password': password,
             'connect_timeout': 10}
    if db.get('sslmode'):
        parts.update(sslmode=db['sslmode'], sslrootcert=db['sslrootcert'])
    return ' '.join(f'{k}={_quote(v)}' for k, v in parts.items()) + '\n'


def probe_config(config):
    return {'schema': 'nexloop-host-health/1', 'host_role': config['host_role'], 'recorder_dsn_file': '/etc/nexloop/host-recorder.dsn',
            'state_file': '/var/lib/nexloop-host-health/state.json', **{k: v for k, v in config['probe'].items()}}


def write_private(path, text):
    """0600 root, atomically; returns whether the content changed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text() == text:
        return False
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    with os.fdopen(fd, 'w') as stream:
        stream.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return True


# A minimal environment (no GIT_* variable of root's session, no system or global configuration) and no replace refs: whoever could
# set those, or write .git, would choose what `show` returns (L6 relation gaps install review I2; follow-up, same as that installer).
GIT = ['env', '-i', 'PATH=/usr/bin:/bin', 'HOME=/nonexistent', 'GIT_CONFIG_NOSYSTEM=1', 'GIT_CONFIG_GLOBAL=/dev/null', 'git', '--no-replace-objects']


def git_show(config, path, runner):
    return runner([*GIT, '-C', config['release_checkout'], 'show', f'{config["release_sha"]}:{path}'])


def trust_git(checkout, trust):
    """The checkout's .git: every directory and every file outside objects/pack data must be root's alone (refs, config, hooks
    and packed-refs decide what `git show` reads)."""
    git = Path(checkout) / '.git'
    if os.path.islink(git) or not os.path.isdir(git):
        raise Refused('release .git: a directory (a worktree pointer file or a link is refused)')
    if os.path.lexists(git / 'objects/info/alternates') or os.path.lexists(git / 'commondir'):
        raise Refused('release .git: objects from elsewhere (objects/info/alternates, commondir) are refused')
    trust(git, what='release .git')
    for path in sorted(git.rglob('*')):
        if path.is_dir() or not str(path.relative_to(git)).startswith('objects/'):
            trust(path, what='release .git')


def release(root, config, runner, trust):
    base = root / 'opt/nexloop/host-health'
    target = base / config['release_sha']
    base.mkdir(parents=True, exist_ok=True)
    trust(base, what='release base')  # I2: a base another account created or can write is refused, never reused
    if (target / '.complete').exists():
        for path in [target, *target.rglob('*')]:  # I2: the reused release, every file in it
            trust(path, what='release') if not path.is_symlink() else trust(path.parent, what='release')
        reused = True
    else:
        reused = False
        if target.exists():
            shutil.rmtree(target)  # an interrupted build of the same sha
        work = Path(tempfile.mkdtemp(dir=base, prefix='.build-'))
        (work / 'host_health_probe.py').write_text(git_show(config, PROBE, runner))
        (work / 'requirements.txt').write_text(git_show(config, REQUIREMENTS, runner))
        for unit, source in UNITS.items():
            (work / unit).write_text(git_show(config, source, runner))
        runner([config['python'], '-m', 'venv', str(work / 'venv')])
        runner([str(work / 'venv/bin/pip'), 'install', '--require-hashes', '--no-deps', '--disable-pip-version-check', '--no-cache-dir',
                '--index-url', config['pip_index_url'], '-r', str(work / 'requirements.txt')])
        runner([str(work / 'venv/bin/python'), '-I', '-B', '-c', 'import psycopg'])
        (work / '.complete').write_text(config['release_sha'] + '\n')
        _read_only(work)
        os.rename(work, target)
    current = base / 'current'
    tmp = base / '.current.tmp'
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(config['release_sha'], tmp)
    os.replace(tmp, current)
    return target, reused


def _read_only(directory):
    """root:root (when root): directories 0755, files 0644 or 0755 - nothing group/other-writable, nothing writable but by root."""
    for path in [directory, *directory.rglob('*')]:
        if path.is_symlink():
            continue
        if os.geteuid() == 0:
            os.chown(path, 0, 0)
        mode = path.stat().st_mode
        os.chmod(path, 0o755 if path.is_dir() or mode & 0o111 else 0o644)


def install(root, config, passwords, runner, out, trust=root_owned):
    step = lambda n, text: print(f'step {n}: {text}', file=out, flush=True)  # noqa: E731
    host = config['host_role']
    trusted_code(config, trust)
    target, reused = release(root, config, runner, trust)
    step(1, f'release {config["release_sha"][:12]} ' + ('reused' if reused else 'built (probe + psycopg venv, read-only)'))
    venv_python = str(root / 'opt/nexloop/host-health/current/venv/bin/python')
    if host == 'data_host':
        step(2, 'pg_hba lines and recorder passwords (DATA_HOST)')
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / 'hba.conf'
            conf.write_text(f'NEXLOOP_ROOT={config["hba"]["nexloop_root"]}\nSTAGE_ENV={config["hba"]["stage_env"]}\nRELEASE_CHECKOUT={config["release_checkout"]}\n')
            os.chmod(conf, 0o600)
            # S2: the hba script renders with the configured interpreter, isolated (-I).
            print(runner(['env', f'HOST_HEALTH_PYTHON={config["python"]}', 'bash', str(HERE / 'host_health_hba_data_host.sh'), str(conf)]).strip(), file=out)
        lines = ''.join(f'{h}\t{p}\n' for h, p in sorted(passwords.items()))
        set_out = runner([venv_python, '-I', '-B', str(HERE / 'host_recorder_password.py'), '--bootstrap-dsn-file', config['bootstrap_dsn_file']], stdin=lines).strip()
        if '"set"' not in set_out:
            raise Failed('recorder passwords not set: ' + set_out[:200])
        print(set_out, file=out)
    else:
        step(2, 'pg_hba and passwords are set on DATA_HOST (nothing here)')
    changed = write_private(root / 'etc/nexloop/host-health.json', json.dumps(probe_config(config), indent=2, sort_keys=True) + '\n')
    step(3, '/etc/nexloop/host-health.json ' + ('written' if changed else 'unchanged'))
    changed = write_private(root / 'etc/nexloop/host-recorder.dsn', recorder_dsn(config, passwords[host]))
    step(4, '/etc/nexloop/host-recorder.dsn ' + ('written' if changed else 'unchanged'))
    units = root / 'etc/systemd/system'
    units.mkdir(parents=True, exist_ok=True)
    for unit in UNITS:
        text = (target / unit).read_text()
        if not (units / unit).exists() or (units / unit).read_text() != text:
            (units / unit).write_text(text)
            os.chmod(units / unit, 0o644)
    dropins = units / 'nexloop-host-health.service.d'
    mdwe = dropins / MDWE_DROPIN
    # W^X on unless the configuration says false explicitly (a standalone CPython that dies under it): then the drop-in turns it off.
    if config.get('memory_deny_write_execute', True) is False:
        dropins.mkdir(exist_ok=True)
        mdwe.write_text('[Service]\nMemoryDenyWriteExecute=no\n')
        os.chmod(mdwe, 0o644)
    elif mdwe.exists():
        mdwe.unlink()
    runner(['systemctl', 'daemon-reload'])
    step(5, 'units installed' + (' (MemoryDenyWriteExecute OFF for this host)' if config.get('memory_deny_write_execute', True) is False else ''))
    probe = root / 'opt/nexloop/host-health/current'
    # The dry run in the unit's own sandbox (its [Service] settings and every drop-in, in order, as transient properties): what
    # fails under the timer - a hardening the interpreter does not survive, an unreadable HOME - fails here, before enabling it.
    summary = runner(['systemd-run', '--quiet', '--wait', '--pipe', '--collect', '--service-type=exec',
                      *[a for key, value in sandbox(units) for a in ('-p', f'{key}={value}')],
                      str(probe / 'venv/bin/python'), '-I', '-B', str(probe / 'host_health_probe.py'), '--config', '/etc/nexloop/host-health.json', '--dry-run'])
    try:
        doc = json.loads(summary.splitlines()[-1])
        doc.pop('entry', None)
    except (ValueError, IndexError):
        raise Failed('probe dry run gave no summary') from None
    print('dry-run: ' + json.dumps(doc, sort_keys=True), file=out)
    if doc.get('blind'):
        raise Failed('probe dry run is blind (docker or systemd unreadable, or the configuration names other units): timer not enabled')
    if doc.get('trigger_unparsed'):
        # Unreadable LastTriggerUSec counts every run as manual and the timers as stale: a false alarm per round, not health.
        raise Failed('probe dry run could not read the last trigger of ' + ', '.join(doc['trigger_unparsed']) + ': timer not enabled')
    runner(['systemctl', 'enable', '--now', 'nexloop-host-health.timer'])
    step(6, 'timer enabled')


MDWE_DROPIN = '50-installer-mdwe.conf'
SANDBOX_SKIP = {'Type', 'ExecStart', 'ExecStartPre', 'ExecStartPost', 'TimeoutStartSec'}


def sandbox(units):
    """(key, value) of the installed service's [Service] section and of its drop-ins (sorted, as systemd applies them), in order."""
    texts = [(units / 'nexloop-host-health.service').read_text()]
    dropins = units / 'nexloop-host-health.service.d'
    if dropins.is_dir():
        texts += [p.read_text() for p in sorted(dropins.glob('*.conf'))]
    out = []
    for text in texts:
        section = None
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith(('#', ';')):
                continue
            if line.endswith('\\') or '"' in line or "'" in line:
                # Continuation lines and quoted values change what systemd reads; the sandbox would differ from the unit.
                raise Failed('the unit or a drop-in uses a continuation line or a quoted value: the dry-run sandbox cannot mirror it')
            if line.startswith('[') and line.endswith(']'):
                section = line[1:-1]
                continue
            key, sep, value = line.partition('=')
            if section == 'Service' and sep and key.strip() not in SANDBOX_SKIP:
                out.append((key.strip(), value.strip()))
    return out


def uninstall(root, runner, out):
    runner(['systemctl', 'disable', '--now', 'nexloop-host-health.timer'], check=False)
    dropins = root / 'etc/systemd/system/nexloop-host-health.service.d'
    if dropins.is_dir():
        shutil.rmtree(dropins)  # the installer's and any hand-made drop-in of this unit
    for path in (root / 'etc/systemd/system/nexloop-host-health.timer', root / 'etc/systemd/system/nexloop-host-health.service',
                 root / 'etc/nexloop/host-health.json', root / 'etc/nexloop/host-recorder.dsn'):
        if path.exists() or path.is_symlink():
            path.unlink()
    for directory in (root / 'opt/nexloop/host-health', root / 'var/lib/nexloop-host-health'):
        if directory.exists():
            for path in [directory, *directory.rglob('*')]:
                if not path.is_symlink():
                    os.chmod(path, 0o700 if path.is_dir() else 0o600)
            shutil.rmtree(directory)
    runner(['systemctl', 'daemon-reload'], check=False)
    print('uninstalled: timer, units and their drop-ins, configuration, recorder DSN, release and state removed (roles and pg_hba lines stay)', file=out)


def main(argv=None, stdin=sys.stdin, out=sys.stdout, runner=run, root=Path('/'), trust=root_owned):
    p = argparse.ArgumentParser(description='Host health probe install (E2/E3), root, one command')
    p.add_argument('config')
    p.add_argument('--uninstall', action='store_true')
    a = p.parse_args(argv)
    os.umask(0o022)  # I2: nothing this run creates is group/other-writable
    try:
        if a.uninstall:
            uninstall(root, runner, out)
            return 0
        config = load_config(a.config)
        passwords = read_passwords(stdin, config['host_role'])
        install(root, config, passwords, runner, out, trust)
    except Refused as error:
        print(json.dumps({'refused': str(error)}), file=out)
        return 2
    except (Failed, OSError) as error:
        print(json.dumps({'failed': str(error) if isinstance(error, Failed) else type(error).__name__}), file=out)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
