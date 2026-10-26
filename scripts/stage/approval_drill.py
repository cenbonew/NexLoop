#!/usr/bin/env python3
"""NX-056 stage approval drill driver (docs/release/run-nx056-approvals-enable.md, day 1 step 8).

One invocation = one synthetic test owner logging in to the stage workbench over HTTPS and doing one thing:

  list                                   the three approval columns (ids, statuses, counts only)
  request --operation OP --body-json J   submit one workbench operation (e.g. set_real_dispatch); answers waiting_approval
  decide  --approval-id ID --decision approve|reject --reason R
  cancel  --approval-id ID --reason R
  execute --approval-id ID               resubmit the approved request with the frozen inputs from 待我执行 (a new request key)

Secrets: the password is read from --password-file (a regular file, mode 0600, owned by the caller; the operator writes it
from Keychain through a pipe and shreds it afterwards). It never appears in argv, the environment, output or the evidence
file. The session cookie, the CSRF token and the Idempotency-Key are kept in memory only and never printed.

Target: --base-url is the stage workbench origin given by the operator (https, no path, query, fragment or user info);
nothing is hard-coded and redirects are never followed. Every step prints one JSON line (and appends it to --evidence-file
when given): step, account label, HTTP status, approval_id, status and the answer's fixed codes; never a body field beyond
those. --dry-run validates everything and prints the requests it would make without opening a connection.
Needs Python 3.11 or newer (run it with the repository's .venv/bin/python). Environment proxies are ignored.
Exit codes: 0 done (the step's HTTP status was 2xx), 1 the server refused or the step could not be done, 2 bad arguments.
"""
import argparse
import http.cookiejar
import json
import os
import re
import secrets
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime

API = '/api/v1/workbench'
_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
_OPERATION = re.compile(r'[a-z][a-z0-9_]{1,63}')
_USERNAME = re.compile(r'[a-z0-9][a-z0-9._-]{2,63}')
_LABEL = re.compile(r'[a-z0-9][a-z0-9._-]{0,31}')
# The answer fields that are safe to show: ids, statuses and fixed codes. Anything else (a code or token an execution may show,
# a request body, a reason text) is dropped; a string that is not a fixed code is shown as '<omitted>'.
ANSWER_KEYS = ('approval_id', 'status', 'approval', 'reason', 'code', 'replayed', 'result', 'operation')
ROW_KEYS = ('approval_id', 'capability', 'operation', 'status', 'approvals', 'quorum', 'policy_version', 'due_by')
_CODE = re.compile(r'[A-Za-z0-9_.:+-]{1,80}')


class DrillError(Exception):
    pass


def check_base_url(value):
    u = urllib.parse.urlsplit(value)
    if (u.scheme != 'https' or not u.hostname or u.username or u.password or u.path not in ('', '/') or u.query or u.fragment):
        raise DrillError('base url must be an https origin without path, query, fragment or user info')
    return f'https://{u.netloc}'


def read_password(path):
    try:
        st = os.lstat(path)
    except OSError:
        raise DrillError('password file unreadable') from None
    if not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) & 0o077 or st.st_uid != os.getuid():
        raise DrillError('password file must be a regular file of the caller with mode 0600')
    with open(path, encoding='utf-8') as f:
        value = f.read().rstrip('\r\n')
    if not value or '\n' in value or len(value) > 512:
        raise DrillError('password file must hold one line')
    return value


def _fixed(value):
    """A value shown only if it is a fixed code, number or boolean (never free text)."""
    if isinstance(value, (bool, int)) or value is None:
        return value
    if isinstance(value, str) and _CODE.fullmatch(value):
        return value
    return '<omitted>'


def answer_view(body):
    if not isinstance(body, dict):
        return {}
    view = {k: _fixed(body[k]) for k in ANSWER_KEYS if k in body}
    details = body.get('details')
    if isinstance(details, dict) and 'reason' in details:
        view['details_reason'] = _fixed(details['reason'])
    return view


def row_view(row):
    return {k: _fixed(row.get(k)) for k in ROW_KEYS}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def default_opener():
    # No proxy from the environment (ProxyHandler({})): the session goes straight to the stage workbench given on the command line.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect, urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


class Workbench:
    """The workbench over HTTP for one account. `opener` is injectable (tests)."""

    def __init__(self, origin, opener=None, timeout=20):
        self.origin, self.opener, self.timeout, self.csrf = origin, opener or default_opener(), timeout, None

    def _call(self, method, path, body=None, *, csrf=False, key=False):
        headers = {'Origin': self.origin, 'Accept': 'application/json'}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode()
            headers['Content-Type'] = 'application/json'
        if csrf:
            headers['X-CSRF-Token'] = self.csrf
        if key:
            headers['Idempotency-Key'] = 'drill-' + secrets.token_hex(16)
        request = urllib.request.Request(self.origin + path, data=data, method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as failure:
            status, raw = failure.code, failure.read()
        except (urllib.error.URLError, OSError) as failure:
            raise DrillError('workbench unreachable: ' + type(failure).__name__) from None
        try:
            parsed = json.loads(raw or b'{}')
        except ValueError:
            parsed = {}
        return status, parsed

    def login(self, username, password):
        status, body = self._call('POST', API + '/auth/login', {'username': username, 'password': password})
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get('csrf_token'), str):
            raise DrillError(f'login refused (HTTP {status}, code {_fixed(body.get("code")) if isinstance(body, dict) else None})')
        self.csrf = body['csrf_token']

    def logout(self):
        if self.csrf:
            self._call('POST', API + '/auth/logout', {}, csrf=True)
            self.csrf = None

    def approvals(self):
        status, body = self._call('GET', API + '/approvals')
        if status != 200 or not isinstance(body, dict):
            raise DrillError(f'approvals read refused (HTTP {status})')
        return body

    def act(self, operation, body):
        return self._call('POST', f'{API}/actions/{operation}', body, csrf=True, key=True)


def parse(argv):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--base-url', required=True)
    p.add_argument('--username', required=True)
    p.add_argument('--password-file', required=True)
    p.add_argument('--account-label', required=True, help='a name for the evidence (e.g. approver3); never the username')
    p.add_argument('--evidence-file')
    p.add_argument('--dry-run', action='store_true')
    sub = p.add_subparsers(dest='step', required=True)
    sub.add_parser('list')
    r = sub.add_parser('request'); r.add_argument('--operation', required=True); r.add_argument('--body-json', required=True)
    d = sub.add_parser('decide'); d.add_argument('--approval-id', required=True); d.add_argument('--decision', required=True, choices=('approve', 'reject'))
    d.add_argument('--reason', required=True)
    c = sub.add_parser('cancel'); c.add_argument('--approval-id', required=True); c.add_argument('--reason', required=True)
    e = sub.add_parser('execute'); e.add_argument('--approval-id', required=True)
    try:
        a = p.parse_args(argv)
    except SystemExit:
        raise DrillError('arguments malformed') from None
    a.base_url = check_base_url(a.base_url)
    if not _USERNAME.fullmatch(a.username) or not _LABEL.fullmatch(a.account_label) or a.account_label == a.username:
        raise DrillError('username or account label malformed (the label must not be the username)')
    if getattr(a, 'approval_id', None) is not None and not _UUID.fullmatch(a.approval_id):
        raise DrillError('approval id must be a uuid')
    if getattr(a, 'reason', None) is not None and not 1 <= len(a.reason.strip()) <= 500:
        raise DrillError('reason must be 1-500 characters')
    if a.step == 'request':
        if not _OPERATION.fullmatch(a.operation):
            raise DrillError('operation malformed')
        try:
            a.body = json.loads(a.body_json)
        except ValueError:
            raise DrillError('body json malformed') from None
        if not isinstance(a.body, dict):
            raise DrillError('body json must be an object')
    return a


def plan(a):
    """What the step sends (method, path, body keys): the dry-run output, without values."""
    steps = [('POST', API + '/auth/login', ['password', 'username'])]
    if a.step == 'list':
        steps.append(('GET', API + '/approvals', []))
    elif a.step == 'request':
        steps.append(('POST', f'{API}/actions/{a.operation}', sorted(a.body)))
    elif a.step == 'decide':
        steps.append(('POST', API + '/actions/decide_approval', ['approval_id', 'decision', 'reason']))
    elif a.step == 'cancel':
        steps.append(('POST', API + '/actions/cancel_approval', ['approval_id', 'reason']))
    else:
        steps += [('GET', API + '/approvals', []), ('POST', API + '/actions/<operation of the approval>', ['<frozen inputs>'])]
    steps.append(('POST', API + '/auth/logout', []))
    return [{'method': m, 'path': path, 'body_keys': keys} for m, path, keys in steps]


def run_step(a, wb):
    """Logged in already. Returns (record, ok)."""
    if a.step == 'list':
        body = wb.approvals()
        columns = {k: [row_view(r) for r in body.get(k, []) if isinstance(r, dict)] for k in ('to_decide', 'to_execute', 'mine')}
        solo = body.get('solo_mode') if isinstance(body.get('solo_mode'), dict) else {}
        return {'http': 200, 'columns': columns, 'solo_mode': {k: _fixed(solo.get(k)) for k in ('enabled', 'others_eligible')}}, True
    if a.step == 'request':
        status, body = wb.act(a.operation, a.body)
    elif a.step == 'decide':
        status, body = wb.act('decide_approval', {'approval_id': a.approval_id, 'decision': a.decision, 'reason': a.reason})
    elif a.step == 'cancel':
        status, body = wb.act('cancel_approval', {'approval_id': a.approval_id, 'reason': a.reason})
    else:
        rows = [r for r in wb.approvals().get('to_execute', []) if isinstance(r, dict) and r.get('approval_id') == a.approval_id]
        if not rows:
            return {'http': None, 'approval_id': a.approval_id, 'error': 'not_in_to_execute'}, False
        row = rows[0]
        if not isinstance(row.get('inputs'), dict) or not _OPERATION.fullmatch(str(row.get('operation'))):
            return {'http': None, 'approval_id': a.approval_id, 'error': 'not_executable_from_workbench'}, False
        status, body = wb.act(row['operation'], row['inputs'])
        body = {**(body if isinstance(body, dict) else {}), 'operation': row['operation']}
        body.setdefault('approval_id', a.approval_id)
    record = {'http': status, **answer_view(body)}
    if a.step in ('decide', 'cancel'):
        record.setdefault('approval_id', a.approval_id)
    return record, 200 <= status < 300


def emit(a, record):
    line = json.dumps({'at': datetime.now(UTC).isoformat(timespec='seconds'), 'step': a.step, 'account': a.account_label, **record},
                      ensure_ascii=False, sort_keys=True)
    print(line)
    if a.evidence_file:
        fd = os.open(a.evidence_file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as f:
            f.write(line + '\n')


def main(argv=None, opener=None):
    try:
        a = parse(sys.argv[1:] if argv is None else argv)
        password = read_password(a.password_file)
    except DrillError as failure:
        print(json.dumps({'error': str(failure)}, ensure_ascii=False), file=sys.stderr)
        return 2
    if a.dry_run:
        del password
        emit(a, {'dry_run': True, 'base_url': a.base_url, 'requests': plan(a)})
        return 0
    wb = Workbench(a.base_url, opener)
    try:
        wb.login(a.username, password)
        del password
        record, ok = run_step(a, wb)
    except DrillError as failure:
        emit(a, {'error': str(failure)})
        return 1
    finally:
        try:
            wb.logout()
        except DrillError:
            pass
    emit(a, record)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
