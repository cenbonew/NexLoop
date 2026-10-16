#!/usr/bin/env python3
"""The owner's real dispatch switches from a private configuration (owner 2026-10-17: "configuration instead of the console").

Logs in to the workbench as the owner (its own login realm), reads the current tenant and Action switches (GET
/api/v1/workbench/metrics, `real_dispatch`: states and revisions only, 0153), and calls the governed owner Action
set_real_dispatch (POST /api/v1/workbench/actions/set_real_dispatch, Action nexloop.real_dispatch.set:1, owner only) once for
every switch whose state differs from the configuration; nothing else. It never connects to PostgreSQL, never writes a switch
table, and holds no credential beyond the owner's own workbench login for the duration of the run (logged out at the end).

    real_dispatch_switch.py --origin https://<APP_HOST_NAME>:<PORT> [--ca-file CA] --username-file F \
        (--password-stdin | --password-file F) (--config F | --close-all --reason TEXT) [--dry-run]

    security find-generic-password -s <keychain service> -a <account> -w | real_dispatch_switch.py ... --password-stdin

Configuration (private JSON, 0600, owned by the caller; deploy/examples/real-dispatch-switch.example.json):
    {"schema": "nexloop-real-dispatch-switch/1", "reason": "...",
     "tenant": {"enabled": true, "data_mode": "real"},
     "actions": [{"action_type": "eios:action:nexloop.enterprise.deliver:1", "enabled": true}]}
An Action the configuration does not name is left as it is. --close-all closes the tenant switch and every open Action switch.

The deployment layer (REAL_DISPATCH_ENABLED in the APP_HOST compose env, run sheet run-calibration-20261017 §1.4) is not a
workbench switch: the tool only prints a reminder of it. Each change is its own governed request: its Idempotency-Key is
derived from the target state and the configuration's reason, so rerunning the same configuration replays instead of repeating.

Output: one JSON object (states, revisions and codes; never the username, password, cookie or CSRF token).
Exit: 0 done (or nothing to change); 1 a request was refused or the state after differs from the plan; 2 input refused.
Standard library only.
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

SCHEMA = 'nexloop-real-dispatch-switch/1'
ACTION = re.compile(r'eios:action:[A-Za-z][A-Za-z0-9_.]{0,159}:[1-9][0-9]{0,8}')
COOKIE = '__Host-nexloop_workbench'
_KEY_FORMAT = re.compile(r'[A-Za-z0-9._~-]{16,160}')  # the workbench actions entry's Idempotency-Key
DEPLOYMENT_NOTE = ('the deployment layer REAL_DISPATCH_ENABLED is the APP_HOST compose env (run-calibration-20261017 §1.4, B class): '
                   'this tool does not change it')


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


def _reason(value):
    if type(value) is not str or not 1 <= len(value.strip()) <= 500 or value != value.strip():
        raise Refused('reason: 1-500 characters')
    return value


def load_config(text):
    try:
        value = json.loads(text)
    except ValueError:
        raise Refused('configuration is not JSON') from None
    if type(value) is not dict or value.get('schema') != SCHEMA or set(value) - {'schema', 'reason', 'tenant', 'actions'}:
        raise Refused('configuration schema')
    tenant = value.get('tenant')
    if tenant is not None and (type(tenant) is not dict or set(tenant) != {'enabled', 'data_mode'} or type(tenant['enabled']) is not bool
                               or tenant['data_mode'] not in ('real', 'test')):
        raise Refused('tenant: {enabled, data_mode real|test}')
    actions = value.get('actions', [])
    if type(actions) is not list or any(type(a) is not dict or set(a) != {'action_type', 'enabled'} or type(a['enabled']) is not bool
                                        or type(a['action_type']) is not str or not ACTION.fullmatch(a['action_type']) for a in actions):
        raise Refused('actions: [{action_type eios:action:<name>:<version>, enabled}]')
    if len({a['action_type'] for a in actions}) != len(actions):
        raise Refused('actions: each action_type once')
    if tenant is None and not actions:
        raise Refused('configuration names no switch')
    return {'reason': _reason(value.get('reason')), 'tenant': tenant, 'actions': actions}


def plan(current, desired=None, *, close_all=False):
    """The changes from the current switches (the workbench's real_dispatch view) to the desired ones: each one request body."""
    tenant = current.get('tenant') or {'enabled': False, 'data_mode': None, 'revision': 0}
    actions = {a['action_type']: a for a in current.get('actions') or []}
    changes = []
    # from_revision: the scope's revision the change starts from (0: never set); it keys the request (idempotency_key), not the body.
    if close_all:
        if tenant['enabled']:
            changes.append({'scope_kind': 'tenant', 'enabled': False, 'action_type': None, 'data_mode': tenant['data_mode'] or 'test',
                            'from_revision': tenant.get('revision', 0)})
        changes += [{'scope_kind': 'action_type', 'enabled': False, 'action_type': name, 'data_mode': None, 'from_revision': a.get('revision', 0)}
                    for name, a in sorted(actions.items()) if a['enabled']]
        return changes
    want = desired.get('tenant')
    if want is not None and (want['enabled'] != tenant['enabled'] or (want['enabled'] and want['data_mode'] != tenant['data_mode'])):
        changes.append({'scope_kind': 'tenant', 'enabled': want['enabled'], 'action_type': None, 'data_mode': want['data_mode'],
                        'from_revision': tenant.get('revision', 0)})
    for item in desired.get('actions') or []:
        now = actions.get(item['action_type'], {'enabled': False, 'revision': 0})
        if now['enabled'] != item['enabled']:
            changes.append({'scope_kind': 'action_type', 'enabled': item['enabled'], 'action_type': item['action_type'], 'data_mode': None,
                            'from_revision': now.get('revision', 0)})
    return changes


def idempotency_key(change, reason):
    """Stable for the same target state, starting revision and reason, so a retry of an unfinished run replays its request; a later
    change of the same switch starts from another revision and is a new request. [A-Za-z0-9._~-]{16,160}."""
    digest = hashlib.sha256(json.dumps({**change, 'reason': reason}, sort_keys=True).encode()).hexdigest()
    return 'switch-' + digest[:48]


def view(snapshot):
    tenant = snapshot.get('tenant')
    return {'tenant': None if tenant is None else {k: tenant[k] for k in ('enabled', 'data_mode', 'revision')},
            'actions': [{k: a[k] for k in ('action_type', 'enabled', 'revision')} for a in snapshot.get('actions') or []]}


class HttpsTransport:
    """request(method, path, headers, body) -> (status, headers list, body bytes) over one TLS connection per request."""

    def __init__(self, origin, ca_file=None, timeout=15):
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

    def switches(self):
        status, body = self._call('GET', '/api/v1/workbench/metrics')
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get('real_dispatch'), dict):
            raise PermissionError(f'switch state unreadable ({status})')
        return body['real_dispatch']

    def set(self, change, reason):
        request = {k: v for k, v in change.items() if k != 'from_revision'}
        status, body = self._call('POST', '/api/v1/workbench/actions/set_real_dispatch', {**request, 'reason': reason},
                                  {'X-CSRF-Token': self.csrf, 'Idempotency-Key': idempotency_key(change, reason)})
        return status, body

    def logout(self):
        if self.cookie and self.csrf:
            self._call('POST', '/api/v1/workbench/auth/logout', extra={'X-CSRF-Token': self.csrf})
        self.cookie = self.csrf = None


def run(workbench, username, password, *, desired=None, close_all=False, reason, dry_run=False):
    workbench.login(username, password)
    try:
        before = workbench.switches()
        changes = plan(before, desired, close_all=close_all)
        out = {'before': view(before), 'changes': [{k: v for k, v in c.items() if v is not None and k != 'from_revision'} for c in changes], 'dry_run': dry_run,
               'deployment_layer': DEPLOYMENT_NOTE}
        if dry_run or not changes:
            return out, 0
        results, failed = [], False
        for change in changes:
            status, body = workbench.set(change, reason)
            item = {'scope_kind': change['scope_kind'], 'action_type': change['action_type'], 'enabled': change['enabled'], 'status': status}
            if status == 200 and isinstance(body, dict):
                item['control_revision'] = body.get('control_revision')
            else:
                item['code'] = body.get('code') if isinstance(body, dict) else None
                failed = True
            results.append({k: v for k, v in item.items() if v is not None})
            if failed:
                break  # the switches stay as far as the governed entry took them; the state after is read below
        after = workbench.switches()
        remaining = plan(after, desired, close_all=close_all)
        out.update(results=results, after=view(after), converged=not remaining)
        return out, 0 if not failed and not remaining else 1
    finally:
        workbench.logout()


def main(argv=None, *, stdin=None, transport=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--origin', required=True)
    parser.add_argument('--ca-file')
    parser.add_argument('--username-file', required=True)
    secret = parser.add_mutually_exclusive_group(required=True)
    secret.add_argument('--password-stdin', action='store_true')
    secret.add_argument('--password-file')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--config')
    mode.add_argument('--close-all', action='store_true')
    parser.add_argument('--reason', help='with --close-all (otherwise the configuration carries it)')
    parser.add_argument('--dry-run', action='store_true')
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2

    def emit(value, code):
        print(json.dumps(value, sort_keys=True, ensure_ascii=False))
        return code

    try:
        if args.close_all:
            desired, reason = None, _reason(args.reason)
        else:
            if args.reason:
                raise Refused('--reason only with --close-all (the configuration carries it)')
            desired = load_config(read_private(args.config))
            reason = desired['reason']
        username = read_private(args.username_file, 1024).strip()
        password = (stdin or sys.stdin).readline().rstrip('\n') if args.password_stdin else read_private(args.password_file, 1024).rstrip('\n')
        if not username or not password:
            raise Refused('username and password required')
        workbench = Workbench(transport or HttpsTransport(args.origin, args.ca_file), args.origin)
    except Refused as error:
        return emit({'ok': False, 'detail': str(error)}, 2)
    try:
        out, code = run(workbench, username, password, desired=desired, close_all=args.close_all, reason=reason, dry_run=args.dry_run)
    except PermissionError as error:
        return emit({'ok': False, 'detail': str(error)}, 1)
    except (OSError, ssl.SSLError, http.client.HTTPException) as error:
        return emit({'ok': False, 'detail': type(error).__name__}, 1)
    return emit({'ok': code == 0, **out}, code)


if __name__ == '__main__':
    sys.exit(main())
