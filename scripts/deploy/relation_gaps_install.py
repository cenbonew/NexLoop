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
interpreter of the unit: the host-health venv, which has psycopg), tenant_id, evidence_dir, metrics_dsn_file (the existing
G-METRICS-DSN file: nexloop_metrics over the host's socket; or --dsn-stdin), host_health_config (the probe's
/etc/nexloop/host-health.json) and host_health_install_config (the host-health install configuration, so a later host-health
reinstall keeps the timer).

Steps (each idempotent):
  1 the system user nexloop-relation-gaps (no home, no shell)
  2 release: /opt/nexloop/relation-gaps/<sha>/ with relation_read_gaps.py, relation_reads.py and nexloop_eios/private_configuration.py
    from `git show <sha>:...` (stdlib only besides psycopg from the interpreter); root:root, read-only; `current` -> <sha>
  3 /etc/nexloop/relation-gaps/metrics.dsn (0600, the unit's user): a DSN naming user nexloop_metrics, written only when it changed
  4 the evidence directory (0750, the unit's user)
  5 the unit and timer from the release's deploy/stage/systemd/nexloop-relation-read-gaps.*.example (tokens filled); daemon-reload
  6 relation-read-gaps in the host-health timers (both files above; 0600 kept), so each scheduled run is recorded and
    timer_scheduled_failure / timer_stale_relation_read_gaps apply
  7 one dry run as the unit's user with the unit's sandbox (systemd-run), its evidence in a scratch directory: exit 0 or 1 (gaps) go
    on, 2 (refused, unwritten evidence) stops; then `systemctl enable --now nexloop-relation-read-gaps.timer`
--drill-arm N: a drop-in nexloop-relation-read-gaps.timer.d/50-drill.conf adds one OnCalendar N minutes from now (the timer itself
fires, so host-health sees a scheduled run); --drill-clear removes it. --uninstall: timer, units, drop-ins, DSN and release removed,
the host-health entries taken out; the evidence stays.
Output: step lines and the dry run's counts; never the DSN or a password. Exit 0 done, 1 a step failed, 2 refused.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from host_health_install import Failed, Refused, read_private_json, root_owned, run, write_private  # noqa: E402

SCHEMA = 'nexloop-relation-gaps-install/1'
USER = 'nexloop-relation-gaps'
SHA = re.compile(r'[0-9a-f]{40}')
PATH = re.compile(r'/[A-Za-z0-9._/@-]+')
TENANT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}')
FILES = {'scripts/acceptance/relation_read_gaps.py': 'scripts/acceptance/relation_read_gaps.py',
         'scripts/acceptance/relation_reads.py': 'scripts/acceptance/relation_reads.py',
         'packages/eios-core/src/nexloop_eios/__init__.py': 'packages/eios-core/src/nexloop_eios/__init__.py',
         'packages/eios-core/src/nexloop_eios/private_configuration.py': 'packages/eios-core/src/nexloop_eios/private_configuration.py'}
UNITS = {'nexloop-relation-read-gaps.service': 'deploy/stage/systemd/nexloop-relation-read-gaps.service.example',
         'nexloop-relation-read-gaps.timer': 'deploy/stage/systemd/nexloop-relation-read-gaps.timer.example'}
ENTRY = {'name': 'relation-read-gaps', 'timer': 'nexloop-relation-read-gaps.timer', 'service': 'nexloop-relation-read-gaps.service'}
DSN_USER = re.compile(r"""(^|\s)user\s*=\s*'?nexloop_metrics'?(\s|$)""")


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
    if len(lines) != 1 or not DSN_USER.search(lines[0]) or any(c in lines[0] for c in '\r\0'):
        raise Refused('the metrics DSN: one line naming user nexloop_metrics')  # the DSN itself is never printed
    return lines[0].strip() + '\n'


def git_show(config, path, runner):
    return runner(['git', '-C', config['release_checkout'], 'show', f'{config["release_sha"]}:{path}'])


def release(root, config, runner, trust):
    base = root / 'opt/nexloop/relation-gaps'
    target = base / config['release_sha']
    base.mkdir(parents=True, exist_ok=True)
    trust(base, what='release base')
    if not (target / '.complete').exists():
        if target.exists():
            shutil.rmtree(target)
        work = Path(tempfile.mkdtemp(dir=base, prefix='.build-'))
        for published, source in FILES.items():
            (work / published).parent.mkdir(parents=True, exist_ok=True)
            (work / published).write_text(git_show(config, source, runner))
        for unit, source in UNITS.items():
            (work / 'units' / unit).parent.mkdir(parents=True, exist_ok=True)
            (work / 'units' / unit).write_text(git_show(config, source, runner))
        (work / '.complete').write_text(config['release_sha'] + '\n')
        for path in sorted(work.rglob('*'), reverse=True):
            os.chmod(path, 0o555 if path.is_dir() else 0o444)
        os.chmod(work, 0o555)
        os.replace(work, target)
    current = base / 'current'
    if current.is_symlink() and os.readlink(current) == config['release_sha']:
        return target
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


def add_timer(path, *, nested):
    """relation-read-gaps in a host-health timers list (probe config, or install config's probe block); 0600 kept."""
    value = read_private_json(path)
    probe = value.get('probe') if nested else value
    if type(probe) is not dict:
        raise Refused(f'{path}: no probe block')
    timers = probe.setdefault('timers', [])
    if any(t.get('name') == ENTRY['name'] for t in timers):
        return False
    timers.append(dict(ENTRY))
    write_private(Path(path), json.dumps(value, indent=2) + '\n')
    return True


def remove_timer(path, *, nested):
    try:
        value = read_private_json(path)
    except (Refused, OSError):
        return False
    probe = value.get('probe') if nested else value
    if type(probe) is not dict or not any(t.get('name') == ENTRY['name'] for t in probe.get('timers', [])):
        return False
    probe['timers'] = [t for t in probe['timers'] if t.get('name') != ENTRY['name']]
    write_private(Path(path), json.dumps(value, indent=2) + '\n')
    return True


def install(root, config, dsn, runner, out, trust):
    step = lambda n, text: print(f'step {n}: {text}', file=out, flush=True)  # noqa: E731
    trust(config['release_checkout'], what='release_checkout')
    trust(config['python'], what='python')
    trust(HERE, what='installer')
    if runner(['id', '-u', USER], check=False) == '':
        runner(['useradd', '--system', '--no-create-home', '--home-dir', '/nonexistent', '--shell', '/usr/sbin/nologin', USER])
    step(1, f'user {USER}')
    target = release(root, config, runner, trust)
    step(2, 'release ' + config['release_sha'][:12])
    secrets = root / 'etc/nexloop/relation-gaps'
    secrets.mkdir(parents=True, exist_ok=True)
    os.chmod(secrets, 0o700)
    changed = write_private(secrets / 'metrics.dsn', dsn)
    runner(['chown', f'{USER}:{USER}', str(secrets), str(secrets / 'metrics.dsn')])
    step(3, 'metrics DSN ' + ('written' if changed else 'unchanged'))
    evidence = root / config['evidence_dir'].lstrip('/')
    evidence.mkdir(parents=True, exist_ok=True)
    os.chmod(evidence, 0o750)
    runner(['chown', f'{USER}:{USER}', str(evidence)])
    step(4, 'evidence directory')
    systemd = root / 'etc/systemd/system'
    systemd.mkdir(parents=True, exist_ok=True)
    service = render_service((target / 'units/nexloop-relation-read-gaps.service').read_text(), config)
    timer = (target / 'units/nexloop-relation-read-gaps.timer').read_text()
    for name, text in (('nexloop-relation-read-gaps.service', service), ('nexloop-relation-read-gaps.timer', timer)):
        (systemd / name).write_text(text)
        os.chmod(systemd / name, 0o644)
    runner(['systemctl', 'daemon-reload'])
    step(5, 'units installed')
    added = [str(p) for p, nested in ((config['host_health_config'], False), (config['host_health_install_config'], True))
             if add_timer(root / p.lstrip('/'), nested=nested)]
    step(6, 'host-health timers: ' + ('added to ' + ', '.join(Path(p).name for p in added) if added else 'already listed'))
    scratch = Path(tempfile.mkdtemp(dir=root / 'tmp' if (root / 'tmp').is_dir() else None, prefix='relation-gaps-dry-'))
    runner(['chown', f'{USER}:{USER}', str(scratch)])
    try:
        answer = runner(['systemd-run', '--quiet', '--wait', '--pipe', '--collect', '--service-type=exec', '-p', f'User={USER}', '-p', f'Group={USER}',
                         '-p', 'NoNewPrivileges=true', '-p', 'ProtectSystem=strict', '-p', 'ProtectHome=true', '-p', f'ReadWritePaths={scratch}',
                         config['python'], '-I', '-B', str(root / 'opt/nexloop/relation-gaps/current/scripts/acceptance/relation_read_gaps.py'),
                         '--dsn-file', '/etc/nexloop/relation-gaps/metrics.dsn', '--tenant', config['tenant_id'], '--evidence-dir', str(scratch)], check=False)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    try:
        doc = json.loads(answer.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise Failed('dry run gave no answer (the metrics login, the DSN or the interpreter): timer not enabled') from None
    if 'refused' in doc or doc.get('evidence') is None:
        raise Failed('dry run refused or wrote no evidence: timer not enabled')
    print('dry-run: ' + json.dumps({k: doc.get(k) for k in ('gaps', 'gaps_erasing', 'dangling', 'relation_only')}, sort_keys=True), file=out)
    runner(['systemctl', 'enable', '--now', 'nexloop-relation-read-gaps.timer'])
    step(7, 'timer enabled' + (' (gaps > 0: the first scheduled run will fail and alert)' if doc.get('gaps') else ''))


def drill_arm(root, minutes, runner, out, clock=datetime.now):
    if not 1 <= minutes <= 60:
        raise Refused('--drill-arm MINUTES: 1-60')
    when = (clock() + timedelta(minutes=minutes)).strftime('%Y-%m-%d %H:%M:00')
    dropins = root / 'etc/systemd/system/nexloop-relation-read-gaps.timer.d'
    dropins.mkdir(parents=True, exist_ok=True)
    (dropins / '50-drill.conf').write_text(f'# NX-054 drill: one scheduled run of this timer (relation_gaps_install.py --drill-clear removes it)\n[Timer]\nOnCalendar={when}\n')
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
    runner(['systemctl', 'disable', '--now', 'nexloop-relation-read-gaps.timer'], check=False)
    systemd = root / 'etc/systemd/system'
    for d in ('nexloop-relation-read-gaps.timer.d', 'nexloop-relation-read-gaps.service.d'):
        if (systemd / d).is_dir():
            shutil.rmtree(systemd / d)
    for path in (systemd / 'nexloop-relation-read-gaps.timer', systemd / 'nexloop-relation-read-gaps.service', root / 'etc/nexloop/relation-gaps/metrics.dsn'):
        if path.exists() or path.is_symlink():
            path.unlink()
    base = root / 'opt/nexloop/relation-gaps'
    if base.exists():
        for path in [base, *base.rglob('*')]:
            if not path.is_symlink():
                os.chmod(path, 0o700 if path.is_dir() else 0o600)
        shutil.rmtree(base)
    for p, nested in ((config['host_health_config'], False), (config['host_health_install_config'], True)):
        remove_timer(root / p.lstrip('/'), nested=nested)
    runner(['systemctl', 'daemon-reload'], check=False)
    print('uninstalled: timer, units, drop-ins, DSN and release removed; host-health entries taken out; the evidence stays', file=out)


def main(argv=None, stdin=sys.stdin, out=sys.stdout, runner=run, root=Path('/'), trust=root_owned, clock=datetime.now):
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
            install(root, config, read_dsn(config, a.dsn_stdin, stdin), runner, out, trust)
    except Refused as error:
        print(json.dumps({'refused': str(error)}), file=out)
        return 2
    except (Failed, OSError) as error:
        print(json.dumps({'failed': str(error) if isinstance(error, Failed) else type(error).__name__}), file=out)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
