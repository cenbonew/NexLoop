#!/usr/bin/env python3
"""Stage release runner: the routine development-and-test deployment as checkpointed steps (owner authorization: the dispatcher runs
them; replaces hand-typed ssh chains - and keeps the gate and promote in separate steps).

    stage_release.py --config PRIVATE.json --step gate [--dry-run]
    stage_release.py --config PRIVATE.json --from build --to post [--dry-run]

Steps, in order (each runnable alone or as a range; each stops the run at the first unexpected result, exit 1, no later step runs):
  build     CI host, as the CI user: the release build; its JSON must be ok and name the release id. The CI result's sha256 is recorded.
  sign      release user: the CI result's sha256 must equal the one recorded at build, and the commit must be on main (a configured check
            that exits 0); then sign, seal and check, each JSON ok.
  backup    DATA_HOST: the differential backup unit (e.g. sudo -n systemctl start nexloop-pg-diff.service); exit 0.
  precheck  the audit's read-only SQL list (a JSON file named in the configuration: [{"name", "sql", "expect"?}]) through the
            configured read-only SQL command; an `expect` that differs stops the run.
  migrate   the migration gate must answer allowed (JSON allowed: true); then the migration (JSON ok); then the definer check, whose
            output must match the configured pattern (default: 0 problems).
  publish   the catalog publish dry run (JSON ok), then the publish (JSON ok).
  roles     workbench_roles apply (JSON ok).
  rebuild   docker load; the loaded image digests (JSON {image: digest}) must equal the signed manifest's (JSON {image: digest});
            env backup; compose config -q; up -d.
  gate      gate_dir, then the doctor deploy gate: its JSON `checks` (name: bool, nested allowed); the failed names are recorded.
            A failure not listed in expected_gate_failures stops the run.
  promote   only when the latest gate of this release in the log failed on expected items alone (and ran); then promote (JSON ok).
  post      post_deploy_check (JSON pass: true) and the log error statistics (JSON counts; any above the configured maximum stops).

Every host, address, path, user, tenant id and command lives in the private configuration (deploy/examples/stage-release.example.json
shows the shape with placeholders); nothing private is in this file. Commands are argv lists with {placeholders} from `vars`, the
release (sha, release_id) and earlier steps; a remote command runs as `ssh <host alias> -- [sudo -n -u <user> --] <argv>`.
Each step appends one JSON line to the configured local log: step, time, dry run, per command its name, exit code, the sha256 of its
output and the checked facts - with every private configuration value and anything DSN- or password-shaped replaced by a marker.
--dry-run prints the commands each step would run (redacted) and runs nothing. Exit: 0 done, 1 stopped, 2 input refused.
Standard library only.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
from pathlib import Path

STEPS = ('build', 'sign', 'backup', 'precheck', 'migrate', 'publish', 'roles', 'rebuild', 'gate', 'promote', 'post')
HOSTS = ('ci', 'app', 'data', 'local')
# The commands each step needs from the configuration (`commands.<step>.<name>`), in the order they run.
REQUIRED = {
    'build': ('ci_result_sha256', 'build'),
    'sign': ('ci_result_sha256', 'on_main', 'sign', 'seal', 'check'),
    'backup': ('backup',),
    'precheck': ('sql',),
    'migrate': ('gate', 'migrate', 'definer'),
    'publish': ('dry_run', 'publish'),
    'roles': ('apply',),
    'rebuild': ('load', 'loaded_digests', 'manifest_digests', 'env_backup', 'config_check', 'up'),
    'gate': ('gate_dir', 'doctor'),
    'promote': ('promote',),
    'post': ('post_deploy_check', 'log_errors'),
}
_SECRETISH = re.compile(r'(postgres(?:ql)?://\S+|(?:password|passwd|pwd|secret|token)\s*[=:]\s*\S+|\b(?:host|user|dbname|sslkey|sslcert)=\S+)', re.I)
_PLACEHOLDER = re.compile(r'\{([a-z_][a-z0-9_]*)\}')


class Refused(ValueError):
    """Input refused (exit 2)."""


class Stop(RuntimeError):
    """An unexpected result: the run stops here (exit 1)."""


def read_private(path, maximum=262144):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise Refused('private file unreadable') from None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise Refused('private file must be a regular 0600 file of the caller')
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise Refused('private file too large')
    return data.decode('utf-8')


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


class Redactor:
    """Replaces every private configuration value (hosts, users, paths, tenant ids, vars, command words of 4+ characters that are not
    generic) and anything DSN- or password-shaped with a marker."""

    GENERIC = {'sudo', 'python3', 'python', 'systemctl', 'start', 'docker', 'compose', 'load', 'config', 'true', 'false', 'null', 'json',
               'check', 'sign', 'seal', 'build', 'promote', 'publish', 'apply', 'gate', 'real', 'test', 'main', 'stage', 'release'}

    def __init__(self, config):
        private = {**{f'host.{k}': v.get('ssh') for k, v in (config.get('hosts') or {}).items() if isinstance(v, dict)},
                   **{f'user.{k}': v for k, v in (config.get('users') or {}).items()},
                   **{f'var.{k}': v for k, v in (config.get('vars') or {}).items()}}
        values = {k: v for k, v in private.items() if isinstance(v, str) and len(v) >= 3}
        # Any other configured string (paths in commands, the log path, the SQL list path) is private too.
        for s in _strings({k: v for k, v in config.items() if k not in ('hosts', 'users', 'vars', 'expected_gate_failures', 'max_log_errors')}):
            for word in re.split(r'[\s{}]+', s):
                if len(word) >= 4 and word.lower() not in self.GENERIC and not _PLACEHOLDER.fullmatch(word) and ('/' in word or '.' in word or '@' in word):
                    values.setdefault(f'path:{hashlib.sha256(word.encode()).hexdigest()[:8]}', word)
        self.pairs = sorted(((v, k) for k, v in values.items()), key=lambda p: -len(p[0]))

    def __call__(self, value):
        if isinstance(value, str):
            for secret, name in self.pairs:
                value = value.replace(secret, f'<{name}>')
            return _SECRETISH.sub('<redacted>', value)
        if isinstance(value, dict):
            return {self(k): self(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self(v) for v in value]
        return value


class SshExecutor:
    """run(host_alias or None, user or None, argv) -> (exit code, stdout text). None host: this machine."""

    def __init__(self, timeout=3600):
        self.timeout = timeout

    def run(self, host, user, argv):
        command = (['sudo', '-n', '-u', user, '--'] if user else []) + list(argv)
        full = ['ssh', '-o', 'BatchMode=yes', host, '--', shlex.join(command)] if host else command
        done = subprocess.run(full, capture_output=True, text=True, timeout=self.timeout)
        return done.returncode, done.stdout


def last_json(text):
    for line in reversed((text or '').strip().splitlines()):
        line = line.strip()
        if line.startswith('{') or line.startswith('['):
            try:
                return json.loads(line)
            except ValueError:
                continue
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def failed_checks(doctor):
    """Names of the false checks of a doctor JSON (`checks`, nested sections allowed: section.name)."""
    out = []

    def walk(prefix, value):
        if isinstance(value, bool):
            if not value:
                out.append(prefix)
        elif isinstance(value, dict):
            for k, v in value.items():
                walk(f'{prefix}.{k}' if prefix else k, v)
    walk('', (doctor or {}).get('checks') if isinstance(doctor, dict) else None)
    return sorted(out)


def load_config(text):
    try:
        config = json.loads(text)
    except ValueError:
        raise Refused('configuration is not JSON') from None
    if type(config) is not dict or config.get('schema') != 'nexloop-stage-release/1':
        raise Refused('configuration schema: nexloop-stage-release/1')
    for key in ('hosts', 'users', 'release', 'commands', 'log'):
        if key not in config:
            raise Refused(f'configuration: {key} required')
    if not isinstance(config['release'], dict) or not re.fullmatch(r'[0-9a-f]{7,40}', str(config['release'].get('sha', ''))):
        raise Refused('release.sha: a commit')
    for step, names in REQUIRED.items():
        spec = config['commands'].get(step)
        if spec is None:
            continue
        for name in names:
            c = spec.get(name)
            if not isinstance(c, dict) or c.get('host') not in HOSTS or not isinstance(c.get('argv'), list) or not c['argv'] \
                    or not all(isinstance(a, str) for a in c['argv']) or (c.get('user') is not None and c['user'] not in config['users']):
                raise Refused(f'commands.{step}.{name}: {{host: ci|app|data|local, user?: <users key>, argv: [..]}}')
            if c['host'] != 'local' and not isinstance((config['hosts'].get(c['host']) or {}).get('ssh'), str):
                raise Refused(f'hosts.{c["host"]}.ssh required')
    if not isinstance(config.get('expected_gate_failures', []), list):
        raise Refused('expected_gate_failures: a list of check names')
    return config


class Runner:
    def __init__(self, config, executor, *, dry_run=False, out=sys.stdout):
        self.config, self.executor, self.dry_run, self.out = config, executor, dry_run, out
        self.redact = Redactor(config)
        self.log_path = Path(config['log'])
        self.facts = {'sha': config['release']['sha'], 'release_id': config['release'].get('release_id')}
        # Facts of earlier steps of this release, latest last (release id and CI digest from build; the gate's failures): a later step
        # run on its own continues from them. A gate that stopped the run is recorded too (its unexpected failures block promote).
        for entry in self.history():
            if entry.get('sha') == self.facts['sha'] and not entry.get('dry_run') and (entry.get('ok') or entry.get('step') == 'gate'):
                self.facts.update(entry.get('carry') or {})
        if config['release'].get('release_id'):
            self.facts['release_id'] = config['release']['release_id']

    def history(self):
        if not self.log_path.exists():
            return []
        out = []
        for line in self.log_path.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def expand(self, argv):
        values = {**(self.config.get('vars') or {}), **{k: v for k, v in self.facts.items() if isinstance(v, (str, int))}}

        def one(arg):
            def sub(m):
                if m.group(1) not in values and self.dry_run:
                    return m.group(0)  # a dry run shows what it would fill in
                if m.group(1) not in values:
                    raise Stop(f'placeholder {{{m.group(1)}}} has no value (run the step that produces it first)')
                return str(values[m.group(1)])
            return _PLACEHOLDER.sub(sub, arg)
        return [one(a) for a in argv]

    def command(self, step, name, record, **extra):
        spec = self.config['commands'][step][name]
        argv = self.expand(spec['argv'] + extra.get('append', []))
        host = None if spec['host'] == 'local' else self.config['hosts'][spec['host']]['ssh']
        user = self.config['users'][spec['user']] if spec.get('user') else None
        item = {'name': name, 'host': spec['host'], 'user': spec.get('user'), 'argv': self.redact(argv)}
        record['commands'].append(item)
        if self.dry_run:
            return None
        code, stdout = self.executor.run(host, user, argv)
        item.update(exit=code, output_sha256=hashlib.sha256((stdout or '').encode()).hexdigest())
        if code != 0:
            raise Stop(f'{step}.{name} exited {code}')
        return stdout or ''

    def ok_json(self, step, name, record, key='ok', **extra):
        out = self.command(step, name, record, **extra)
        if out is None:
            return None
        value = last_json(out)
        if not isinstance(value, dict) or value.get(key) is not True:
            raise Stop(f'{step}.{name}: no JSON with {key}: true')
        return value

    # ---- the steps -----------------------------------------------------------------------------------------------------------------
    def step_build(self, r):
        digest = self.command('build', 'ci_result_sha256', r)
        out = self.ok_json('build', 'build', r)
        if out is None:
            return
        d = (digest or '').strip().split()[0] if (digest or '').strip() else ''
        if not re.fullmatch(r'[0-9a-f]{64}', d):
            raise Stop('build: the CI result digest is not a sha256')
        rid = out.get('release_id')
        if not isinstance(rid, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', rid):
            raise Stop('build: no release id')
        r['carry'] = {'release_id': rid, 'ci_result_sha256': d}
        self.facts.update(r['carry'])
        r['facts'] = {'release_id': rid, 'images': sorted((out.get('images') or {}).keys())}

    def step_sign(self, r):
        digest = self.command('sign', 'ci_result_sha256', r)
        if digest is not None:
            d = digest.strip().split()[0] if digest.strip() else ''
            if not self.facts.get('ci_result_sha256'):
                raise Stop('sign: no CI result digest recorded by build')
            if d != self.facts['ci_result_sha256']:
                raise Stop('sign: the CI result digest differs from the one recorded at build')
            r['facts'] = {'ci_result_sha256': d}
        self.command('sign', 'on_main', r)  # exit 0: the commit is on main
        for name in ('sign', 'seal', 'check'):
            self.ok_json('sign', name, r)

    def step_backup(self, r):
        self.command('backup', 'backup', r)

    def step_precheck(self, r):
        path = self.config.get('precheck_sql')
        if not path:
            raise Refused('precheck_sql: the audit SQL list file')
        items = json.loads(read_private(path))
        if not isinstance(items, list) or not all(isinstance(i, dict) and isinstance(i.get('name'), str) and isinstance(i.get('sql'), str) for i in items):
            raise Refused('precheck_sql: [{name, sql, expect?}]')
        results = {}
        for i in items:
            if not re.match(r'^\s*(select|with|show)\b', i['sql'], re.I) or ';' in i['sql'].strip().rstrip(';'):
                raise Refused(f'precheck {i["name"]}: one read-only statement')
            out = self.command('precheck', 'sql', r, append=[i['sql']])
            if out is None:
                continue
            got = out.strip()
            results[i['name']] = {'output_sha256': hashlib.sha256(got.encode()).hexdigest(), 'expected': 'expect' in i,
                                  'matched': None if 'expect' not in i else got == str(i['expect']).strip()}
            if 'expect' in i and got != str(i['expect']).strip():
                r['facts'] = {'precheck': results}
                raise Stop(f'precheck {i["name"]}: the output differs from the expected value')
        r['facts'] = {'precheck': results}

    def step_migrate(self, r):
        self.ok_json('migrate', 'gate', r, key='allowed')
        self.ok_json('migrate', 'migrate', r)
        out = self.command('migrate', 'definer', r)
        if out is not None and not re.search(self.config.get('definer_ok_pattern', r'\b0 problems\b'), out):
            raise Stop('migrate: the definer check does not report 0 problems')

    def step_publish(self, r):
        self.ok_json('publish', 'dry_run', r)
        self.ok_json('publish', 'publish', r)

    def step_roles(self, r):
        self.ok_json('roles', 'apply', r)

    def step_rebuild(self, r):
        self.command('rebuild', 'load', r)
        loaded, manifest = self.command('rebuild', 'loaded_digests', r), self.command('rebuild', 'manifest_digests', r)
        if loaded is not None:
            a, b = last_json(loaded), last_json(manifest)
            if not isinstance(a, dict) or not isinstance(b, dict) or not b or a != b:
                raise Stop('rebuild: the loaded image digests differ from the signed manifest')
            r['facts'] = {'images': sorted(b), 'digests_match': True}
        for name in ('env_backup', 'config_check', 'up'):
            self.command('rebuild', name, r)

    def step_gate(self, r):
        self.command('gate', 'gate_dir', r)
        out = self.command('gate', 'doctor', r)
        if out is None:
            return
        doctor = last_json(out)
        if not isinstance(doctor, dict) or not isinstance(doctor.get('checks'), dict):
            raise Stop('gate: the doctor output has no checks')
        failed = failed_checks(doctor)
        unexpected = sorted(set(failed) - set(self.config.get('expected_gate_failures') or []))
        r['carry'] = {'gate_failed': failed, 'gate_unexpected': unexpected}
        self.facts.update(r['carry'])  # written even when the gate stops the run, so a later promote sees it
        r['facts'] = {'failed': failed, 'unexpected': unexpected, 'expected': sorted(self.config.get('expected_gate_failures') or [])}
        if unexpected:
            raise Stop('gate: unexpected failures ' + ', '.join(unexpected))

    def step_promote(self, r):
        if not self.dry_run:
            if 'gate_unexpected' not in self.facts:
                raise Stop('promote: no gate result of this release in the log (run gate first)')
            if self.facts['gate_unexpected']:
                raise Stop('promote: the latest gate had unexpected failures')
        r['facts'] = {'gate_failed': self.facts.get('gate_failed')}
        self.ok_json('promote', 'promote', r)

    def step_post(self, r):
        self.ok_json('post', 'post_deploy_check', r, key='pass')
        out = self.command('post', 'log_errors', r)
        if out is None:
            return
        counts = last_json(out)
        if not isinstance(counts, dict) or not all(isinstance(v, int) for v in counts.values()):
            raise Stop('post: the log error statistics are not JSON counts')
        limit = int(self.config.get('max_log_errors', 0))
        r['facts'] = {'log_errors': counts}
        if any(v > limit for v in counts.values()):
            raise Stop('post: log errors above the configured maximum')

    def run(self, steps):
        for step in steps:
            if step not in self.config['commands']:
                raise Refused(f'commands.{step} missing')
            record = {'step': step, 'at': datetime.datetime.now(datetime.UTC).isoformat(), 'sha': self.facts['sha'], 'dry_run': self.dry_run,
                      'commands': [], 'ok': False}
            try:
                getattr(self, 'step_' + step)(record)
                record['ok'] = True
            except Stop as error:
                record['error'] = str(error)
            finally:
                self.write(record)
            if not record['ok']:
                return 1
        return 0

    def write(self, record):
        record = self.redact(record)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        print(line, file=self.out)
        if self.dry_run:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a') as f:
            f.write(line + '\n')


def select(args):
    if args.step:
        return [args.step]
    start = STEPS.index(args.from_step) if args.from_step else 0
    end = STEPS.index(args.to_step) if args.to_step else len(STEPS) - 1
    if start > end:
        raise Refused('--from comes after --to')
    return list(STEPS[start:end + 1])


def main(argv=None, *, executor=None, out=sys.stdout):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--config', required=True)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--step', choices=STEPS)
    group.add_argument('--from', dest='from_step', choices=STEPS)
    parser.add_argument('--to', dest='to_step', choices=STEPS)
    parser.add_argument('--dry-run', action='store_true')
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2
    try:
        if args.to_step and args.step:
            raise Refused('--to goes with --from, not --step')
        config = load_config(read_private(args.config))
        steps = select(args)
        return Runner(config, executor or SshExecutor(), dry_run=args.dry_run, out=out).run(steps)
    except Refused as error:
        print(json.dumps({'ok': False, 'refused': str(error)}), file=out)
        return 2


if __name__ == '__main__':
    sys.exit(main())
