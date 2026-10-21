"""S6 stage acceptance: what every S6 stage script shares (NX-038/039 parts and NX-040 parts; L2 and L3 lines).

Configuration: one private JSON file (0600, owned by the caller), schema `nexloop-s6-stage/1`:
    {"schema": "nexloop-s6-stage/1", "executor": "<who runs it>", "release": "<release id>", "tenant_id": "<uuid>",
     "results_dir": "<local dir>",
     "roles": {"writer":    {"dsn_file": "...", "token_file": "..."},      # nexloop_evo_writer + the writer's service credential
               "evaluator": {"dsn_file": "...", "token_file": "..."},      # nexloop_evo_evaluator + the evaluator's credential
               "verify":    {"dsn_file": "..."}},                          # any application role that reads control.schema_migrations
     "owner": {"origin": "https://<APP_HOST_NAME>:<PORT>", "ca_file": "...", "username_file": "..."},   # password from stdin only
     "inputs": {...}}                                                       # per script (see each script's doc)
Every path, DSN, token, host and tenant lives there; a DSN is read from its file into the connection, never into an argv or output.

Result (one JSON object per check, printed as one line and written 0600 to results_dir; evidence-ready), schema `nexloop-s6-stage-result/1`:
    {"schema", "check", "acceptance": [AT / S6-R ids], "release", "executor", "executed_at", "dry_run", "world": "test",
     "passed": bool | null, "blocked": [reasons], "steps": [{"name", "expected", "observed", "passed", "evidence": {...}}]}
`passed` is false when any step failed (a blocked step never hides a failure), else null when a step is blocked (a prerequisite is
missing; never counted as a pass), else true. `world` is the check's world kind (test for the S6 checks, simulation for AT-046). Observed values are codes, counts,
versions and digests; anything private is replaced by a marker. Exit: 0 passed, 1 failed or blocked, 2 input refused.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import stat
from pathlib import Path

CONFIG_SCHEMA = 'nexloop-s6-stage/1'
RESULT_SCHEMA = 'nexloop-s6-stage-result/1'
_SECRETISH = re.compile(r'(postgres(?:ql)?://\S+|(?:password|passwd|secret|token)\s*[=:]\s*\S+|\b(?:host|user|dbname|sslkey|sslcert)=\S+)', re.I)


class Refused(ValueError):
    """Input refused (exit 2)."""


class Blocked(RuntimeError):
    """A prerequisite of the step is missing on this deployment (the step is not a pass)."""


def read_private(path, maximum=1048576):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except (OSError, TypeError):
        raise Refused('private file unreadable') from None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise Refused('private file must be a regular 0600 file of the caller')
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise Refused('private file too large')
    return data.decode('utf-8')


def load_config(path):
    try:
        config = json.loads(read_private(path))
    except ValueError:
        raise Refused('configuration is not JSON') from None
    if type(config) is not dict or config.get('schema') != CONFIG_SCHEMA:
        raise Refused(f'configuration schema: {CONFIG_SCHEMA}')
    for key in ('executor', 'release', 'tenant_id', 'results_dir'):
        if not isinstance(config.get(key), str) or not config[key]:
            raise Refused(f'configuration: {key} required')
    return config


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
    """Every private configuration string of 6+ characters (paths, origin, tenant, DSN files) and DSN/password shapes become markers."""

    def __init__(self, config):
        keep = {config.get('schema'), config.get('release'), config.get('executor')}
        values = {v for v in _strings({k: v for k, v in config.items() if k not in ('inputs',)}) if isinstance(v, str) and len(v) >= 6 and v not in keep}
        self.values = sorted(values, key=len, reverse=True)

    def __call__(self, value):
        if isinstance(value, str):
            for v in self.values:
                value = value.replace(v, '<private>')
            return _SECRETISH.sub('<redacted>', value)
        if isinstance(value, dict):
            return {self(k): self(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self(v) for v in value]
        return value


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


def token_digest(path) -> str:
    """The service credential's digest (sha256 of the token in its private file); the token itself is never kept."""
    return hashlib.sha256(read_private(path, 4096).strip().encode()).hexdigest()


def connect_from(path):
    """A psycopg connection factory whose DSN comes from a private file (never an argv)."""
    import psycopg
    dsn = read_private(path, 8192).strip()
    return lambda: psycopg.connect(dsn, autocommit=True, connect_timeout=10)


def refusal(error) -> dict:
    """What a refused call says: SQLSTATE and the server's primary message (a fixed text of the governed function), never a context."""
    diag = getattr(error, 'diag', None)
    return {'error': type(error).__name__, 'sqlstate': getattr(diag, 'sqlstate', None) or getattr(error, 'sqlstate', None),
            'message': (getattr(diag, 'message_primary', None) or str(error).split('\n')[0])[:200]}


class Result:
    def __init__(self, check, acceptance, config, *, dry_run=False, redact=None, world='test', nonce=None):
        self.check, self.acceptance, self.config, self.dry_run, self.world = check, list(acceptance), config, dry_run, world
        self.nonce = nonce  # a run nonce (run_nonce): the parts of one run carry the same one (load_chain)
        self.steps, self.blocked = [], []
        self.redact = redact or Redactor(config)

    def step(self, name, expected, observed, passed, **evidence):
        self.steps.append({'name': name, 'expected': expected, 'observed': observed, 'passed': bool(passed), 'evidence': evidence})
        return passed

    def block(self, name, reason):
        self.blocked.append(f'{name}: {reason}')
        self.steps.append({'name': name, 'expected': None, 'observed': {'blocked': reason}, 'passed': None, 'evidence': {}})

    def document(self):
        # A failed step is a failure whatever else is blocked (L6 sim-stage-enable-review 5.3); blocked alone is not a pass (null).
        if any(s['passed'] is False for s in self.steps):
            passed = False
        elif self.blocked or self.dry_run:
            passed = None
        else:
            passed = bool(self.steps)
        doc = self.redact({'schema': RESULT_SCHEMA, 'check': self.check, 'acceptance': self.acceptance, 'release': self.config['release'],
                           'executor': self.config['executor'], 'executed_at': datetime.datetime.now(datetime.UTC).isoformat(),
                           'dry_run': self.dry_run, 'world': self.world, 'passed': passed, 'blocked': self.blocked, 'steps': self.steps})
        if self.nonce is not None:
            doc['run_nonce'] = self.nonce
        return doc

    def write(self, out):
        doc = self.document()
        line = json.dumps(doc, ensure_ascii=False, sort_keys=True)
        print(line, file=out)
        if not self.dry_run:
            directory = Path(self.config['results_dir'])
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            name = f"{self.check}-{doc['executed_at'].replace(':', '').replace('+', 'Z')[:22]}.json"
            fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as f:
                f.write(line + '\n')
        return 0 if doc['passed'] is True or (self.dry_run and not self.blocked) else 1


NONCE = re.compile(r'[a-z0-9]{8,64}')


def new_nonce() -> str:
    """A run nonce: letters and digits only (no identifier, no secret); every part of one run writes it as run_nonce."""
    import secrets
    return secrets.token_hex(12)


def load_chain(paths, *, prefix, order, nonce=None):
    """The earlier parts of one run (--from), checked as a chain (L6 AT-046 review 1.1 / NX-043 X7 review 3.1): each a result document
    of this script (check starting with prefix, never a dry run) with the same run_nonce (and the expected one when given), each part
    once, in the given order of checks, executed in that order. Returns (steps, blocked, nonce) - the steps and blocks of all parts."""
    docs = []
    for path in paths or []:
        try:
            doc = json.loads(Path(path).read_text().strip().splitlines()[-1])
        except (OSError, ValueError, IndexError):
            raise Refused('--from: a result document of this script') from None
        if doc.get('schema') != RESULT_SCHEMA or not str(doc.get('check', '')).startswith(prefix) or doc.get('dry_run'):
            raise Refused('--from: a result document of this script (not a dry run)')
        docs.append(doc)
    nonces = {d.get('run_nonce') for d in docs}
    if docs and (len(nonces) != 1 or not isinstance(next(iter(nonces)), str) or not NONCE.fullmatch(next(iter(nonces)))):
        raise Refused('--from: the parts of one run (the same run_nonce)')
    if docs and nonce is not None and next(iter(nonces)) != nonce:
        raise Refused('--from: a result of another run (run_nonce)')
    checks = [d['check'] for d in docs]
    if len(set(checks)) != len(checks):
        raise Refused('--from: each part once')
    if any(c not in order for c in checks):
        raise Refused('--from: parts of this chain only: ' + ', '.join(order))
    by = {d['check']: d for d in docs}
    times = [by[c]['executed_at'] for c in order if c in by]  # in chain order, the executions must not go back in time
    if times != sorted(times):
        raise Refused('--from: the parts out of order (executed_at)')
    steps, blocked = [], []
    for c in order:
        if c in by:
            steps += by[c].get('steps') or []
            blocked += by[c].get('blocked') or []
    return steps, blocked, (next(iter(nonces)) if docs else None)
