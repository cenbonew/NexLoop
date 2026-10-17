#!/usr/bin/env python3
"""Reference enterprise delivery endpoint for the NX-037 controlled trial (a stand-in for an external system).

**This is not part of NexLoop.** It plays the enterprise's own idempotent delivery service that NexLoop reaches only through
the service effect HTTP protocol (`nexloop-service-effect-http-v1`, docs/CONNECTORS.md §3). It never connects to a NexLoop
database and uses none of NexLoop's credentials. It keeps its own state in its own SQLite file (WAL, synchronous=FULL, its
own volume); the rule "PostgreSQL holds the business state" is NexLoop's and does not apply to this external stand-in.

    python apps/reference-endpoint/reference_endpoint.py serve --data-dir <DIR> --listen <IP>:<PORT> \\
        --tls-cert-file <CERT> --tls-key-file <KEY> --service-token-file <FILE> --staff-token-file <FILE>
    python apps/reference-endpoint/reference_endpoint.py drill --data-dir <DIR> (--delay-next-posts N [--delay-seconds S] |
        --get-unavailable-seconds S | --clear | --status)

Protocol (service token, `Authorization: Bearer`):
- POST /v1/effects  {intent_id, payload_digest, parameters}; `Idempotency-Key` must equal intent_id; payload_digest must be
  the sha256 of the canonical parameters. A new intent is persisted (fsync) before the answer 200; a replay answers 200 with
  the same receipt; the same key with a different payload answers 409 with the original receipt (nothing is overwritten).
- parameters are those of nexloop.enterprise.deliver:1: summary (required, 1-200 characters), details (optional, at most 2000),
  consumer_ref (optional, NexLoop's opaque consumer reference, at most 200). Any other key, or a wrong type or length, is
  rejected with 400 parameters_invalid.
- GET /v1/effects/<intent_id>  200 with the receipt, or 404 {state: not_found, payload_digest: null, provider_reference: null}.
- Receipt: {intent_id, payload_digest, state, provider_reference}. The effect is "the ticket is durably created in the
  enterprise system", so state is always fulfilled once persisted (POST and GET alike, except under the drill).

Delivery semantics (owner decisions 2026-10-13): a service ticket. Each customer request becomes a ticket that staff handle in
working hours. Staff page (staff token, separate from the service token): GET /staff lists the tickets with the request
summary and details, the consumer reference, the source tenant (this endpoint's configured tenant label: one endpoint serves
one enterprise) and the intent id, the received time, the handler, the result and a note; staff mark each one "handled" or
"failed" with their name and an optional note. That result is the enterprise's internal business outcome, not an effect
state: it is kept only on the staff page and in this endpoint's own store, and GET does not return it (passing it back to
NexLoop, e.g. as an inbound event, is a follow-up after v0.1). Login sets an HttpOnly, Secure, SameSite=Strict
session cookie; marks need the session's CSRF token.

Drill ("result temporarily unknown", for the 3 unknown_reconciliation interactions): only the local `drill` command can switch
it, by writing a private control file in the data directory; no API route can. `--delay-next-posts N` makes the next N new
POSTs persist first and answer only after --delay-seconds (default 5, beyond NexLoop's 2 s deadline); `--get-unavailable-seconds
S` makes GET answer 503 for S seconds. Both check that NexLoop queries and reconciles instead of resending.

Standard library only. Credentials, keys and tokens are read from private files (0600, owned by the service user) and are
never logged or echoed.
"""
import argparse
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import ssl
import stat
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

MAX_BODY = 262144
TOKEN = re.compile(r'[A-Za-z0-9._~-]{16,4096}')
SESSION_SECONDS = 8 * 3600
CONTROL = 'drill-control.json'
DATABASE = 'reference-endpoint.sqlite'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(parameters):
    return hashlib.sha256(canonical(parameters).encode()).hexdigest()


def private_text(path, maximum=4096):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise PermissionError('private file required')
        value = stream.read(maximum + 1)
    if not value or len(value) > maximum:
        raise ValueError('file length')
    return value.decode('utf-8').strip()


def token_file(path):
    value = private_text(path)
    if not TOKEN.fullmatch(value):
        raise ValueError('token must be 16-4096 characters of [A-Za-z0-9._~-]')
    return value


def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


# ---- storage ---------------------------------------------------------------------------------------------------------------

class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.directory / DATABASE
        self.lock = threading.Lock()
        with self.connect() as db:
            db.execute('create table if not exists effects(intent_id text primary key, payload_digest text not null, '
                       'parameters text not null, state text not null, provider_reference text not null unique, '
                       'created_at text not null, updated_at text not null)')
            columns = {row[1] for row in db.execute('pragma table_info(effects)')}
            for column in ('handled_by', 'note', 'handled_at'):
                if column not in columns:
                    db.execute(f'alter table effects add column {column} text')
            db.execute('create table if not exists events(sequence integer primary key, intent_id text not null, '
                       'kind text not null, at text not null)')
        os.chmod(self.path, 0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.execute('pragma journal_mode=wal')
        db.execute('pragma synchronous=FULL')
        if db.execute('pragma synchronous').fetchone()[0] != 2:
            raise RuntimeError('synchronous FULL required')
        return db

    @staticmethod
    def receipt(row):
        # The stored ticket status (open / handled / failed) is internal; the effect state is fulfilled once persisted.
        return {'intent_id': row[0], 'payload_digest': row[1], 'state': 'fulfilled', 'provider_reference': row[3]}

    def submit(self, intent, payload_digest, parameters):
        """Returns (status, receipt, created)."""
        with self.lock, self.connect() as db:
            db.execute('begin immediate')
            row = db.execute('select intent_id,payload_digest,state,provider_reference from effects where intent_id=?', (intent,)).fetchone()
            if row is None:
                stamp = now()
                db.execute('insert into effects(intent_id,payload_digest,parameters,state,provider_reference,created_at,updated_at) '
                           'values(?,?,?,?,?,?,?)', (intent, payload_digest, canonical(parameters), 'accepted',
                                                                         'ref-' + uuid.uuid4().hex, stamp, stamp))
                db.execute('insert into events(intent_id,kind,at) values(?,?,?)', (intent, 'accepted', stamp))
                db.execute('commit')
                row = db.execute('select intent_id,payload_digest,state,provider_reference from effects where intent_id=?', (intent,)).fetchone()
                return 200, self.receipt(row), True
            db.execute('commit')
            return (200 if row[1] == payload_digest else 409), self.receipt(row), False

    def get(self, intent):
        with self.connect() as db:
            row = db.execute('select intent_id,payload_digest,state,provider_reference from effects where intent_id=?', (intent,)).fetchone()
        return None if row is None else self.receipt(row)

    def listing(self):
        with self.connect() as db:
            return db.execute('select intent_id,state,parameters,created_at,handled_by,note,handled_at from effects '
                              'order by created_at desc limit 500').fetchall()

    def mark(self, intent, state, handled_by, note=''):
        if state not in ('fulfilled', 'failed') or not 1 <= len(handled_by) <= 64 or len(note) > 500:
            raise ValueError('mark')
        with self.lock, self.connect() as db:
            db.execute('begin immediate')
            row = db.execute('select state from effects where intent_id=?', (intent,)).fetchone()
            if row is None or row[0] != 'accepted':
                db.execute('rollback')
                return False
            stamp = now()
            db.execute('update effects set state=?, updated_at=?, handled_by=?, note=?, handled_at=? where intent_id=?',
                       (state, stamp, handled_by, note, stamp, intent))
            db.execute('insert into events(intent_id,kind,at) values(?,?,?)', (intent, state, stamp))
            db.execute('commit')
            return True


PARAMETER_LIMITS = {'summary': 200, 'details': 2000, 'consumer_ref': 200}


def parameters_valid(parameters):
    """nexloop.enterprise.deliver:1 parameters: summary required, details and consumer_ref optional, nothing else."""
    if type(parameters) is not dict or 'summary' not in parameters or not set(parameters) <= set(PARAMETER_LIMITS):
        return False
    return all(type(value) is str and value.strip() and len(value) <= PARAMETER_LIMITS[key] for key, value in parameters.items())


def ticket_fields(parameters_text):
    """(summary, details, consumer_ref) for the staff page; never raises."""
    try:
        value = json.loads(parameters_text)
    except ValueError:
        value = None
    value = value if isinstance(value, dict) else {}
    text = {key: value.get(key) if type(value.get(key)) is str else '' for key in PARAMETER_LIMITS}
    if not text['summary']:
        text['summary'] = parameters_text[:200] + ('…' if len(parameters_text) > 200 else '')
    return text['summary'], text['details'], text['consumer_ref']


# ---- drill control (local CLI only) ------------------------------------------------------------------------------------

def _control_path(directory):
    return Path(directory) / CONTROL


def _read_control(directory):
    path = _control_path(directory)
    try:
        text = private_text(path, maximum=4096)
    except FileNotFoundError:
        return {'delay_posts': 0, 'delay_seconds': 5.0, 'get_unavailable_until': 0.0}
    value = json.loads(text)
    if type(value) is not dict or set(value) != {'delay_posts', 'delay_seconds', 'get_unavailable_until'}:
        raise ValueError('control file')
    return value


def _write_control(directory, value):
    path = _control_path(directory)
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(json.dumps(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


_control_lock = threading.Lock()


def take_post_delay(directory):
    """Called by the server for each new POST: returns the delay to apply (0 when the drill is off)."""
    with _control_lock:
        value = _read_control(directory)
        if value['delay_posts'] <= 0:
            return 0.0
        value['delay_posts'] -= 1
        _write_control(directory, value)
        return float(value['delay_seconds'])


def get_unavailable(directory):
    return time.time() < _read_control(directory)['get_unavailable_until']


def drill(directory, *, delay_next_posts=None, delay_seconds=5.0, get_unavailable_seconds=None, clear=False):
    with _control_lock:
        value = _read_control(directory)
        if clear:
            value = {'delay_posts': 0, 'delay_seconds': 5.0, 'get_unavailable_until': 0.0}
        if delay_next_posts is not None:
            if not 1 <= delay_next_posts <= 10 or not 2.5 <= delay_seconds <= 60:
                raise ValueError('drill bounds')
            value['delay_posts'], value['delay_seconds'] = value['delay_posts'] + delay_next_posts, delay_seconds
        if get_unavailable_seconds is not None:
            if not 1 <= get_unavailable_seconds <= 3600:
                raise ValueError('drill bounds')
            value['get_unavailable_until'] = time.time() + get_unavailable_seconds
        _write_control(directory, value)
        return value


# ---- HTTP --------------------------------------------------------------------------------------------------------------

# The staff page shows times in Asia/Shanghai (fixed +08:00: no daylight saving); storage stays UTC (owner 2026-10-17).
SHANGHAI = timezone(timedelta(hours=8))


def display_time(value):
    """(Beijing time `YYYY-MM-DD HH:MM:SS`, the stored UTC ISO time for a tooltip). Empty stays empty; an unparsable value is
    shown as stored, never guessed."""
    if not value:
        return '', ''
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return value, ''
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(SHANGHAI).strftime('%Y-%m-%d %H:%M:%S'), moment.astimezone(timezone.utc).isoformat(timespec='seconds')


def time_cell(value):
    shown, utc = display_time(value)
    return f'<span title="UTC {html.escape(utc)}">{html.escape(shown)}</span>' if utc else html.escape(shown)


# Automatic tickets of the endpoint conformance check (scripts/nx037/endpoint_conformance.py: its default summary, and the no-op
# parameters the run sheets use): kept apart from customer tickets on the staff page.
CHECK_SUMMARIES = ('NexLoop conformance check', '端点一致性检查')


def is_check(summary):
    return summary.startswith(CHECK_SUMMARIES)


# Staff page views: ?view=pending (default: customer tickets still to handle), all (every customer ticket), checks (conformance).
VIEWS = {'pending': '待处理的客户工单', 'all': '全部客户工单', 'checks': '一致性检查工单'}


def in_view(view, state, summary):
    if view == 'checks':
        return is_check(summary)
    return not is_check(summary) and (view == 'all' or state == 'accepted')


# The staff page on a desktop browser (2026-10-17: text too small, the table wider than the window with squeezed columns):
# readable base size, a full-width fixed-layout table whose columns share the width (long ids wrap instead of widening it), the
# mark form stacked inside its cell. Inline only (the page's CSP allows inline style and nothing else).
PAGE_STYLE = (
    # Colours as variables, redefined for a dark system theme (layout audit 2026-10-18, batch 3).
    ':root{--bg:#f6f8fa;--surface:#fff;--text:#1f2328;--muted:#57606a;--border:#d0d7de;--head:#eef1f4;--link:#0969da;'
    '--pending-bg:#fff8c5;--pending:#7d4e00;--done-bg:#dafbe1;--done:#116329;--fail-bg:#ffebe9;--fail:#a40e26;color-scheme:light}'
    '@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--surface:#161b22;--text:#e6edf3;--muted:#9198a1;--border:#30363d;'
    '--head:#21262d;--link:#4493f8;--pending-bg:#3b2e00;--pending:#e3b341;--done-bg:#0f3d1f;--done:#56d364;--fail-bg:#4c1219;'
    '--fail:#ff7b72;color-scheme:dark}}'
    'body{font:16px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;margin:0;padding:24px 32px;'
    'color:var(--text);background:var(--bg)}a{color:var(--link)}'
    'h1{font-size:24px;margin:0 0 8px}p{max-width:72em}'
    'table{width:100%;border-collapse:collapse;table-layout:fixed;background:var(--surface);border:1px solid var(--border)}'
    'th,td{padding:10px 12px;border-bottom:1px solid var(--border);text-align:left;vertical-align:top;overflow-wrap:anywhere}'
    'th{background:var(--head);font-size:14px;font-weight:600;white-space:nowrap}'
    'th:nth-child(1){width:22%}th:nth-child(2){width:14%}th:nth-child(3){width:12%}th:nth-child(4){width:7%}'
    'th:nth-child(5){width:8%}th:nth-child(6){width:37%}'
    'code{font-size:13px;word-break:break-all}small{font-size:14px;color:var(--muted)}'
    'input{background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:6px}'
    'td form input{display:block;width:100%;box-sizing:border-box;margin:0 0 6px;padding:6px 8px;font-size:15px}'
    'button{font-size:15px;padding:6px 12px;margin:0 6px 4px 0;cursor:pointer}'
    '.views{margin:12px 0;display:flex;flex-wrap:wrap;gap:4px 16px}.views a.on{font-weight:600;color:var(--text);text-decoration:none}'
    '.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:13px;white-space:nowrap}'
    '.s-accepted{background:var(--pending-bg);color:var(--pending)}.s-fulfilled{background:var(--done-bg);color:var(--done)}'
    '.s-failed{background:var(--fail-bg);color:var(--fail)}'
    'form.mark{display:flex;gap:6px;align-items:center;flex-wrap:wrap}form.mark input{display:inline-block;margin:0;width:auto;min-width:3.5em;flex:1 1 4em}'
    'form.mark button{margin:0;padding:6px 10px;white-space:nowrap}'
    'form label input{font-size:15px;padding:6px 8px;max-width:100%;box-sizing:border-box}'
    # Phone and narrow tablet: one card per ticket; each cell names its column (data-label); the mark form wraps; 40 px targets.
    '@media (max-width:800px){body{padding:16px}table,tbody,tr,td{display:block;width:100%;box-sizing:border-box}'
    'table{border:0;background:none}tr:first-child{display:none}'
    'tr{background:var(--surface);border:1px solid var(--border);border-radius:10px;margin:0 0 12px;padding:4px 0}'
    'td{border:0;padding:6px 14px}td[data-label]::before{content:attr(data-label);display:block;font-size:13px;color:var(--muted)}'
    'form.mark input{flex:1 1 100%;min-height:40px}form.mark button{flex:1 1 auto;min-height:40px}'
    'form label{display:block}form label input{display:block;width:100%;min-height:40px;margin:6px 0 12px}button{min-height:40px}}'
)


def make_server(*, data_dir, listen, service_token, staff_token, tls_cert_file=None, tls_key_file=None, secure_cookie=True,
                tenant_label=''):
    if len(tenant_label) > 100:
        raise ValueError('tenant label')
    tenant_label = tenant_label.strip() or '未配置'
    store = Store(data_dir)
    sessions = {}
    sessions_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = 'reference-endpoint'
        sys_version = ''

        def log_message(self, *args):
            pass

        # -- helpers
        def send_json(self, status, value):
            content = canonical(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(content)

        def send_html(self, status, body, extra=()):
            content = ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                       '<title>服务工单</title><style>' + PAGE_STYLE + '</style>' + body).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
            self.send_header('X-Frame-Options', 'DENY')
            for name, value in extra:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(content)

        def service_authorized(self):
            header = self.headers.get('Authorization') or ''
            return header.startswith('Bearer ') and hmac.compare_digest(header[7:].encode(), service_token.encode())

        def body(self):
            length = self.headers.get('Content-Length')
            if length is None or not length.isdecimal() or not 0 < int(length) <= MAX_BODY:
                raise ValueError('length')
            return self.rfile.read(int(length))

        def session(self):
            cookie = self.headers.get('Cookie') or ''
            match = re.search(r'(?:^|;\s*)re_session=([A-Za-z0-9_-]{43})', cookie)
            if not match:
                return None
            with sessions_lock:
                found = sessions.get(match.group(1))
                if found is None or found['expires'] < time.time():
                    sessions.pop(match.group(1), None)
                    return None
                return found

        # -- routes
        def do_POST(self):
            if self.path == '/v1/effects':
                return self.post_effect()
            if self.path == '/staff/login':
                return self.staff_login()
            if self.path == '/staff/mark':
                return self.staff_mark()
            self.send_json(404, {'error': 'not_found'})

        def do_GET(self):
            if self.path.startswith('/v1/effects/'):
                return self.get_effect()
            if urlsplit(self.path).path == '/staff':
                return self.staff_page()
            self.send_json(404, {'error': 'not_found'})

        def post_effect(self):
            if not self.service_authorized():
                return self.send_json(401, {'error': 'unauthorized'})
            try:
                value = json.loads(self.body())
                if type(value) is not dict or set(value) != {'intent_id', 'payload_digest', 'parameters'}:
                    raise ValueError()
                intent, payload, parameters = value['intent_id'], value['payload_digest'], value['parameters']
                if type(intent) is not str or str(uuid.UUID(intent)) != intent or self.headers.get('Idempotency-Key') != intent:
                    raise ValueError()
                if type(parameters) is not dict or type(payload) is not str or payload != digest(parameters):
                    raise ValueError()
            except (ValueError, TypeError, UnicodeDecodeError):
                return self.send_json(400, {'error': 'request_invalid'})
            if not parameters_valid(parameters):
                return self.send_json(400, {'error': 'parameters_invalid'})
            status, receipt, created = store.submit(intent, payload, parameters)
            if created:
                delay = take_post_delay(data_dir)
                if delay:
                    # Drill: the request is persisted; the answer comes only after NexLoop's deadline.
                    time.sleep(delay)
            try:
                self.send_json(status, receipt)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def get_effect(self):
            if not self.service_authorized():
                return self.send_json(401, {'error': 'unauthorized'})
            intent = self.path[len('/v1/effects/'):]
            try:
                if str(uuid.UUID(intent)) != intent:
                    raise ValueError()
            except ValueError:
                return self.send_json(400, {'error': 'request_invalid'})
            if get_unavailable(data_dir):
                return self.send_json(503, {'error': 'temporarily_unavailable'})
            receipt = store.get(intent)
            if receipt is None:
                return self.send_json(404, {'intent_id': intent, 'payload_digest': None, 'state': 'not_found', 'provider_reference': None})
            self.send_json(200, receipt)

        def staff_login(self):
            try:
                form = parse_qs(self.body().decode(), strict_parsing=True)
                given = form.get('token', [''])[0]
            except (ValueError, UnicodeDecodeError):
                given = ''
            if not given or not hmac.compare_digest(given.encode(), staff_token.encode()):
                return self.send_html(401, '<p>登录失败。</p><p><a href="/staff">返回</a></p>')
            session_id, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            with sessions_lock:
                sessions[session_id] = {'csrf': csrf, 'expires': time.time() + SESSION_SECONDS}
            flags = 'HttpOnly; SameSite=Strict; Path=/staff' + ('; Secure' if secure_cookie else '')
            self.send_response(303)
            self.send_header('Location', '/staff')
            self.send_header('Set-Cookie', f're_session={session_id}; {flags}; Max-Age={SESSION_SECONDS}')
            self.send_header('Content-Length', '0')
            self.end_headers()

        def staff_page(self):
            current = self.session()
            if current is None:
                return self.send_html(200, '<h1>服务工单（员工）</h1><form method="post" action="/staff/login">'
                                           '<label>员工凭据 <input type="password" name="token" autocomplete="off"></label> '
                                           '<button>登录</button></form>')
            query = parse_qs(urlsplit(self.path).query)
            view = (query.get('view') or ['pending'])[0]
            if view not in VIEWS:
                view = 'pending'
            tickets = [(t, *ticket_fields(t[2])) for t in store.listing()]  # newest first (created_at desc)
            counts = {v: sum(1 for t, summary, *_ in tickets if in_view(v, t[1], summary)) for v in VIEWS}
            rows = []
            for (intent, state, parameters, created, handled_by, note, handled_at), summary, details, consumer_ref in tickets:
                if not in_view(view, state, summary):
                    continue
                label = {'accepted': '待处理', 'fulfilled': '已处理', 'failed': '处理失败'}[state]
                if state == 'accepted':
                    result = (f'<form class="mark" method="post" action="/staff/mark">'
                              f'<input type="hidden" name="csrf" value="{html.escape(current["csrf"])}">'
                              f'<input type="hidden" name="intent_id" value="{html.escape(intent)}">'
                              f'<input type="hidden" name="view" value="{view}">'
                              f'<input name="handled_by" placeholder="处理人" maxlength="64" required>'
                              f'<input name="note" placeholder="备注（可选）" maxlength="500">'
                              f'<button name="state" value="fulfilled">已处理</button>'
                              f'<button name="state" value="failed">处理失败</button></form>')
                else:
                    result = f'处理于 {time_cell(handled_at)}' + (f'<br>{html.escape(note)}' if note else '')
                request_text = html.escape(summary) + (f'<br><small>{html.escape(details)}</small>' if details else '')
                if consumer_ref:
                    request_text += f'<br><small>客户引用：<code>{html.escape(consumer_ref)}</code></small>'
                rows.append(f'<tr><td data-label="请求摘要">{request_text}</td>'
                            f'<td data-label="来源租户 / 意图 id">{html.escape(tenant_label)}<br><code>{html.escape(intent)}</code></td>'
                            f'<td data-label="收到时间（北京时间）">{time_cell(created)}</td><td data-label="处理人">{html.escape(handled_by or "") or "—"}</td>'
                            f'<td data-label="状态"><span class="badge s-{state}">{label}</span></td><td data-label="处理结果与备注">{result}</td></tr>')
            links = ''.join(f'<a href="/staff?view={v}"{" class=on" if v == view else ""}>{text}（{counts[v]}）</a>' for v, text in VIEWS.items())
            body = ''.join(rows) or '<tr><td colspan="6">没有工单。</td></tr>'
            self.send_html(200, '<h1>服务工单（员工）</h1><p>这是企业侧系统的替身，不属于 NexLoop。请在工作时间内处理，并标记处理结果。'
                                '处理结果只保存在本系统，不回传 NexLoop。时间为北京时间，鼠标悬停可看 UTC。</p>'
                                f'<nav class="views">{links}</nav>'
                                '<table><tr><th>请求摘要</th><th>来源租户 / 意图 id</th><th>收到时间（北京时间）</th><th>处理人</th><th>状态</th>'
                                '<th>处理结果与备注</th></tr>' + body + '</table>')

        def staff_mark(self):
            current = self.session()
            if current is None:
                return self.send_html(401, '<p>请先登录。</p>')
            try:
                form = parse_qs(self.body().decode(), strict_parsing=True)
                csrf, intent, state = form['csrf'][0], form['intent_id'][0], form['state'][0]
                handled_by, note = form.get('handled_by', [''])[0].strip(), form.get('note', [''])[0].strip()
            except (ValueError, KeyError, UnicodeDecodeError):
                return self.send_html(400, '<p>请求无效。</p>')
            if not hmac.compare_digest(csrf.encode(), current['csrf'].encode()) or state not in ('fulfilled', 'failed'):
                return self.send_html(403, '<p>请求无效。</p>')
            try:
                store.mark(intent, state, handled_by, note)
            except ValueError:
                return self.send_html(400, '<p>请填写处理人（最多 64 字），备注最多 500 字。</p>')
            back = form.get('view', ['pending'])[0]
            self.send_response(303)
            self.send_header('Location', '/staff?view=' + (back if back in VIEWS else 'pending'))
            self.send_header('Content-Length', '0')
            self.end_headers()

    host, _, port = listen.rpartition(':')
    server = ThreadingHTTPServer((host.strip('[]'), int(port)), Handler)
    server.daemon_threads = True
    if tls_cert_file is not None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        private_text(tls_key_file, maximum=65536)  # refuses a non-private key file before loading it
        context.load_cert_chain(tls_cert_file, tls_key_file)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    server.store = store
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description='Reference enterprise delivery endpoint (external stand-in, not NexLoop)')
    sub = parser.add_subparsers(dest='command', required=True)
    s = sub.add_parser('serve')
    s.add_argument('--data-dir', required=True)
    s.add_argument('--listen', required=True)
    s.add_argument('--tls-cert-file', required=True)
    s.add_argument('--tls-key-file', required=True)
    s.add_argument('--service-token-file', required=True)
    s.add_argument('--staff-token-file', required=True)
    s.add_argument('--tenant-label', default=os.environ.get('ENDPOINT_TENANT_LABEL', ''),
                   help='source tenant shown on the staff page (one endpoint serves one enterprise)')
    d = sub.add_parser('drill')
    d.add_argument('--data-dir', required=True)
    group = d.add_mutually_exclusive_group(required=True)
    group.add_argument('--delay-next-posts', type=int)
    group.add_argument('--get-unavailable-seconds', type=int)
    group.add_argument('--clear', action='store_true')
    group.add_argument('--status', action='store_true')
    d.add_argument('--delay-seconds', type=float, default=5.0)
    args = parser.parse_args(argv)
    try:
        if args.command == 'drill':
            if args.status:
                value = _read_control(args.data_dir)
            else:
                value = drill(args.data_dir, delay_next_posts=args.delay_next_posts, delay_seconds=args.delay_seconds,
                              get_unavailable_seconds=args.get_unavailable_seconds, clear=args.clear)
            print(json.dumps({'delay_posts': value['delay_posts'], 'delay_seconds': value['delay_seconds'],
                              'get_unavailable_remaining_seconds': max(0, round(value['get_unavailable_until'] - time.time()))}))
            return 0
        service, staff = token_file(args.service_token_file), token_file(args.staff_token_file)
        if hmac.compare_digest(service.encode(), staff.encode()):
            raise ValueError('service and staff tokens must differ')
        server = make_server(data_dir=args.data_dir, listen=args.listen, service_token=service, staff_token=staff,
                             tls_cert_file=args.tls_cert_file, tls_key_file=args.tls_key_file, tenant_label=args.tenant_label)
        print(json.dumps({'ready': True}), flush=True)
        server.serve_forever()
        return 0
    except (ValueError, OSError) as error:
        print(json.dumps({'ok': False, 'error': type(error).__name__}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
