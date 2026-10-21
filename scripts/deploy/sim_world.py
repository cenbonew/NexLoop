#!/usr/bin/env python3
"""The owner's simulation world Actions from a private configuration (owner rule: "a configuration file and one command").

Four subcommands, each one governed owner Action through the workbench actions entry (POST /api/v1/workbench/actions/<operation>,
owner only), after the owner's workbench login (its own login realm; logged out at the end):
    create    create_sim_world         nexloop.sim_world.create      a simulation or shadow world
    delete    delete_sim_world         nexloop.sim_world.delete      the whole deletion of one world (a 30-day summary stays)
    generate  generate_sim_world_data  nexloop.sim_world.generate    synthetic or structural data in a simulation world
    clock     control_sim_clock        nexloop.sim_clock.control     advance, pause, resume or set the rate of a world's clock
    publish-catalog  publish_sim_catalog  nexloop.sim_world.publish_catalog  the world's Action catalog: the real Actions with a stand-in
                                                                   (S4), definitions only; repeatable
It never connects to PostgreSQL and holds no credential beyond the owner's own workbench login for the duration of the run.

    sim_world.py <create|delete|generate|clock|publish-catalog> --config F --dry-run
    security find-generic-password -s <keychain service> -a <account> -w | \
        sim_world.py <create|delete|generate|clock|publish-catalog> --config F --origin https://<APP_HOST_NAME>:<PORT> [--ca-file CA] \
        --username-file F --password-stdin

--dry-run checks the configuration and prints the request it would send; it reads no password and sends nothing. The password is
read from standard input only (one line), never from an argument. The configuration is a private JSON file (0600, owned by the
caller); examples in deploy/examples/sim-world-*.example.json. Fields and ranges are those of the workbench entry
(nexloop_eios.workbench_actions_http.OPERATIONS); nullable fields may be left out.

The request's Idempotency-Key is derived from the operation and the configuration, so rerunning the same configuration replays
(a create gives the same world) instead of repeating; change a field (e.g. the purpose or the reason) for a new request.

Output: one JSON object (operation, HTTP status, and the result's world, status, revision and counts; never the username,
password, cookie, CSRF token or a DSN). Exit: 0 done; 1 refused or unreachable; 2 input refused. Standard library only.
"""
import argparse
import hashlib
import http.client
import json
import os
import re
import ssl
import stat
import sys
from urllib.parse import urlsplit

SCHEMA = 'nexloop-sim-world/1'
COOKIE = '__Host-nexloop_workbench'
_UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
ANY_WORLD = re.compile(r'(sim|shadow):' + _UUID)
SIM_WORLD = re.compile(r'sim:' + _UUID)
SEED = re.compile(r'[A-Za-z0-9_.-]{1,64}')
# Result keys printed (scalars only); anything else in the response is left out.
SUMMARY = ('copied', 'already_present', 'world', 'status', 'created', 'replayed', 'changed', 'expires_at', 'deleted', 'remaining', 'credentials_revoked',
           'generation_id', 'generator_version', 'counts', 'revision', 'action', 'world_time', 'paused', 'rate', 'requeued', 'operation', 'outcome_id')


class Refused(ValueError):
    pass


def read_private(path, maximum=65536):
    """A regular, owner-only (0600 or 0400) file of the caller, not followed through a link."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise Refused('private file unreadable') from None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise Refused('private file must be a regular 0600 file of the caller')
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise Refused('private file too large')
    return data.decode('utf-8')


def _int(name, low, high):
    def check(v):
        if type(v) is not int or not low <= v <= high:
            raise Refused(f'{name}: integer {low}-{high}')
        return v
    return check


def _text(name):
    def check(v):
        if type(v) is not str or not 1 <= len(v.strip()) <= 500:
            raise Refused(f'{name}: 1-500 characters')
        return v
    return check


def _match(name, pattern):
    def check(v):
        if type(v) is not str or not pattern.fullmatch(v):
            raise Refused(f'{name}: {pattern.pattern}')
        return v
    return check


def _one_of(name, values):
    def check(v):
        if v not in values:
            raise Refused(f'{name}: one of {", ".join(values)}')
        return v
    return check


def _nullable(check):
    return lambda v: None if v is None else check(v)


def _events(v):
    if type(v) is not dict or set(v) != {'min', 'max'} or any(type(x) is not int or not 0 <= x <= 50 for x in v.values()) or v['min'] > v['max']:
        raise Refused('events_per_consumer: {min, max} integers 0-50, min <= max')
    return v


def _segments(v):
    if v is not None and (type(v) is not dict or not 1 <= len(v) <= 10):
        raise Refused('segments: an object with 1-10 segments')
    return v


def _rate(v):
    if v is not None and (type(v) not in (int, float) or not 0 < v <= 100000):
        raise Refused('rate: a number above 0, at most 100000')
    return v


# Subcommand -> (operation, {field: (check, optional)}). Optional fields are nullable and sent as null when left out.
OPERATIONS = {
    'create': ('create_sim_world', {'kind': (_one_of('kind', ('simulation', 'shadow')), False), 'purpose': (_text('purpose'), False),
                                    'expires_in_days': (_int('expires_in_days', 1, 30), False)}),
    'delete': ('delete_sim_world', {'world': (_match('world', ANY_WORLD), False), 'reason': (_text('reason'), False)}),
    'publish-catalog': ('publish_sim_catalog', {'world': (_match('world', ANY_WORLD), False), 'reason': (_text('reason'), False)}),
    'generate': ('generate_sim_world_data', {'world': (_match('world', SIM_WORLD), False), 'kind': (_one_of('kind', ('synthetic', 'structural')), False),
                                             'seed': (_match('seed', SEED), False), 'consumers': (_int('consumers', 1, 2000), False),
                                             'events_per_consumer': (_events, False), 'segments': (_segments, True),
                                             'max_objects': (_nullable(_int('max_objects', 0, 10000)), True)}),
    'clock': ('control_sim_clock', {'world': (_match('world', ANY_WORLD), False), 'action': (_one_of('action', ('advance', 'pause', 'resume', 'set_rate')), False),
                                    'seconds': (_nullable(_int('seconds', 1, 315360000)), True), 'rate': (_rate, True), 'reason': (_text('reason'), False)}),
}


def load_config(command, text):
    """The request body for one subcommand: exactly the entry's fields, checked as the entry checks them."""
    try:
        value = json.loads(text)
    except ValueError:
        raise Refused('configuration is not JSON') from None
    operation, spec = OPERATIONS[command]
    if type(value) is not dict or value.get('schema') != SCHEMA:
        raise Refused(f'configuration schema: {SCHEMA}')
    fields = {k: v for k, v in value.items() if k != 'schema'}
    unknown = set(fields) - set(spec)
    missing = {k for k, (_, optional) in spec.items() if not optional and k not in fields}
    if unknown or missing:
        raise Refused(f'{command} fields: unknown {sorted(unknown)}, missing {sorted(missing)}')
    body = {k: check(fields.get(k)) for k, (check, _) in spec.items()}
    if command == 'generate' and ((body['kind'] == 'synthetic') != (body['segments'] is not None) or (body['segments'] is None) == (body['max_objects'] is None)):
        raise Refused('generate: synthetic takes segments only, structural takes max_objects only')
    if command == 'clock' and ((body['action'] == 'advance') != (body['seconds'] is not None) or (body['action'] == 'set_rate') != (body['rate'] is not None)):
        raise Refused('clock: seconds only with advance, rate only with set_rate')
    return operation, body


def idempotency_key(operation, body):
    """Stable for the same operation and configuration ([A-Za-z0-9._~-]{16,160})."""
    return 'simcli-' + hashlib.sha256(json.dumps({'operation': operation, **body}, sort_keys=True).encode()).hexdigest()[:48]


def summary(result):
    if not isinstance(result, dict):
        return {}
    scalar = lambda v: v is None or type(v) in (str, int, float, bool)  # noqa: E731
    return {k: result[k] for k in SUMMARY if k in result and (scalar(result[k]) or (type(result[k]) is dict and all(map(scalar, result[k].values()))))}


class HttpsTransport:
    """request(method, path, headers, body) -> (status, headers list, body bytes) over one TLS connection per request."""

    def __init__(self, origin, ca_file=None, timeout=60):
        parts = urlsplit(origin)
        if parts.scheme != 'https' or not parts.hostname or parts.path not in ('', '/') or parts.query or parts.fragment:
            raise Refused('origin: https://<host>[:port]')
        self.host, self.port, self.timeout = parts.hostname, parts.port or 443, timeout
        self.context = ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()

    def request(self, method, path, headers, body=None):
        connection = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=self.context)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, response.getheaders(), response.read(1048576)
        finally:
            connection.close()


class Workbench:
    def __init__(self, transport, origin):
        self.transport, self.origin, self.cookie, self.csrf = transport, origin.rstrip('/'), None, None

    def _call(self, method, path, body=None, extra=None):
        headers = {'Origin': self.origin, 'Accept': 'application/json', **(extra or {})}
        if self.cookie:
            headers['Cookie'] = f'{COOKIE}={self.cookie}'
        if body is not None:
            headers['Content-Type'] = 'application/json'
            body = json.dumps(body).encode()
        status, headers_out, data = self.transport.request(method, path, headers, body)
        for name, value in headers_out:
            if name.lower() == 'set-cookie' and value.startswith(COOKIE + '='):
                self.cookie = value.split(';', 1)[0].split('=', 1)[1] or None
        try:
            parsed = json.loads(data) if data else None
        except ValueError:
            parsed = None
        return status, parsed

    def login(self, username, password):
        status, body = self._call('POST', '/api/v1/workbench/auth/login', {'username': username, 'password': password})
        if status != 200 or not isinstance(body, dict) or not body.get('csrf_token') or not self.cookie:
            raise PermissionError(f'login refused ({status})')
        if body.get('restricted'):
            raise PermissionError('login session restricted')
        self.csrf = body['csrf_token']

    def act(self, operation, body):
        return self._call('POST', '/api/v1/workbench/actions/' + operation, body,
                          {'X-CSRF-Token': self.csrf, 'Idempotency-Key': idempotency_key(operation, body)})

    def logout(self):
        if self.cookie and self.csrf:
            self._call('POST', '/api/v1/workbench/auth/logout', extra={'X-CSRF-Token': self.csrf})
        self.cookie = self.csrf = None


class _Parser(argparse.ArgumentParser):
    """A usage error never echoes the arguments (a password typed on the command line by mistake stays out of the output)."""

    def error(self, message):
        self.exit(2, f'{self.prog}: usage error (see --help); arguments are not echoed\n')


def main(argv=None, *, stdin=None, transport=None):
    parser = _Parser(description=__doc__.split('\n\n')[0])
    parser.add_argument('command', choices=sorted(OPERATIONS))
    parser.add_argument('--config', required=True)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--origin')
    parser.add_argument('--ca-file')
    parser.add_argument('--username-file')
    parser.add_argument('--password-stdin', action='store_true')
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2

    def emit(value, code):
        print(json.dumps(value, sort_keys=True, ensure_ascii=False))
        return code

    try:
        operation, body = load_config(args.command, read_private(args.config))
        planned = {'operation': operation, 'request': body, 'idempotency_key': idempotency_key(operation, body)}
        if args.dry_run:
            return emit({'ok': True, 'dry_run': True, **planned}, 0)
        if not args.origin or not args.username_file or not args.password_stdin:
            raise Refused('--origin, --username-file and --password-stdin required (or --dry-run)')
        username = read_private(args.username_file, 1024).strip()
        password = (stdin or sys.stdin).readline().rstrip('\n')
        if not username or not password:
            raise Refused('username and password required')
        workbench = Workbench(transport or HttpsTransport(args.origin, args.ca_file), args.origin)
    except Refused as error:
        return emit({'ok': False, 'detail': str(error)}, 2)
    try:
        workbench.login(username, password)
        try:
            status, result = workbench.act(operation, body)
        finally:
            workbench.logout()
    except PermissionError as error:
        return emit({'ok': False, 'detail': str(error)}, 1)
    except (OSError, ssl.SSLError, http.client.HTTPException) as error:
        return emit({'ok': False, 'detail': type(error).__name__}, 1)
    out = {'ok': status == 200, 'dry_run': False, 'operation': operation, 'status': status}
    if status == 200:
        out['result'] = summary(result)
    elif isinstance(result, dict):
        out['code'] = result.get('code')
        reason = (result.get('details') or {}).get('reason')
        if reason:
            out['reason'] = reason
    return emit(out, 0 if status == 200 else 1)


if __name__ == '__main__':
    sys.exit(main())
