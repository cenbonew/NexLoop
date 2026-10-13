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
  the sha256 of the canonical parameters. A new intent is persisted (fsync) before the answer 202; a replay answers 200 with
  the same receipt; the same key with a different payload answers 409 with the original receipt (nothing is overwritten).
- GET /v1/effects/<intent_id>  200 with the receipt, or 404 {state: not_found, payload_digest: null, provider_reference: null}.
- Receipt: {intent_id, payload_digest, state, provider_reference}; state accepted (waiting for staff), fulfilled (staff marked
  it handled) or failed (staff marked it failed).

Staff page (staff token, separate from the service token): GET /staff lists the delivery requests; staff mark each one
"handled" (fulfilled) or "failed"; the mark is what GET returns afterwards. Login sets an HttpOnly, Secure, SameSite=Strict
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
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

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
        return dict(zip(('intent_id', 'payload_digest', 'state', 'provider_reference'), row[:4]))

    def submit(self, intent, payload_digest, parameters):
        """Returns (status, receipt, created)."""
        with self.lock, self.connect() as db:
            db.execute('begin immediate')
            row = db.execute('select intent_id,payload_digest,state,provider_reference from effects where intent_id=?', (intent,)).fetchone()
            if row is None:
                stamp = now()
                db.execute('insert into effects values(?,?,?,?,?,?,?)', (intent, payload_digest, canonical(parameters), 'accepted',
                                                                         'ref-' + uuid.uuid4().hex, stamp, stamp))
                db.execute('insert into events(intent_id,kind,at) values(?,?,?)', (intent, 'accepted', stamp))
                db.execute('commit')
                row = db.execute('select intent_id,payload_digest,state,provider_reference from effects where intent_id=?', (intent,)).fetchone()
                return 202, self.receipt(row), True
            db.execute('commit')
            return (200 if row[1] == payload_digest else 409), self.receipt(row), False

    def get(self, intent):
        with self.connect() as db:
            row = db.execute('select intent_id,payload_digest,state,provider_reference from effects where intent_id=?', (intent,)).fetchone()
        return None if row is None else self.receipt(row)

    def listing(self):
        with self.connect() as db:
            return db.execute('select intent_id,state,parameters,created_at,updated_at from effects order by created_at desc limit 500').fetchall()

    def mark(self, intent, state):
        if state not in ('fulfilled', 'failed'):
            raise ValueError('state')
        with self.lock, self.connect() as db:
            db.execute('begin immediate')
            row = db.execute('select state from effects where intent_id=?', (intent,)).fetchone()
            if row is None or row[0] != 'accepted':
                db.execute('rollback')
                return False
            stamp = now()
            db.execute('update effects set state=?, updated_at=? where intent_id=?', (state, stamp, intent))
            db.execute('insert into events(intent_id,kind,at) values(?,?,?)', (intent, state, stamp))
            db.execute('commit')
            return True


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

def make_server(*, data_dir, listen, service_token, staff_token, tls_cert_file=None, tls_key_file=None, secure_cookie=True):
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
            content = ('<!doctype html><meta charset="utf-8"><title>交付请求</title>'
                       '<style>body{font-family:sans-serif;margin:2em}td,th{padding:4px 8px;border-bottom:1px solid #ddd;text-align:left}'
                       'code{font-size:12px}</style>' + body).encode()
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
            if self.path == '/staff':
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
                return self.send_html(200, '<h1>交付请求（员工）</h1><form method="post" action="/staff/login">'
                                           '<label>员工凭据 <input type="password" name="token" autocomplete="off"></label> '
                                           '<button>登录</button></form>')
            rows = []
            for intent, state, parameters, created, updated in store.listing():
                label = {'accepted': '待处理', 'fulfilled': '已处理', 'failed': '处理失败'}[state]
                actions = ''
                if state == 'accepted':
                    actions = ''.join(
                        f'<form method="post" action="/staff/mark" style="display:inline">'
                        f'<input type="hidden" name="csrf" value="{html.escape(current["csrf"])}">'
                        f'<input type="hidden" name="intent_id" value="{html.escape(intent)}">'
                        f'<input type="hidden" name="state" value="{value}"><button>{text}</button></form> '
                        for value, text in (('fulfilled', '标记已处理'), ('failed', '标记处理失败')))
                rows.append(f'<tr><td><code>{html.escape(intent)}</code></td><td>{html.escape(created)}</td><td>{label}</td>'
                            f'<td><code>{html.escape(parameters[:500])}</code></td><td>{actions}</td></tr>')
            self.send_html(200, '<h1>交付请求（员工）</h1><p>这是企业侧系统的替身，不属于 NexLoop。</p>'
                                '<table><tr><th>请求</th><th>收到时间</th><th>状态</th><th>内容</th><th>操作</th></tr>'
                           + ''.join(rows) + '</table>')

        def staff_mark(self):
            current = self.session()
            if current is None:
                return self.send_html(401, '<p>请先登录。</p>')
            try:
                form = parse_qs(self.body().decode(), strict_parsing=True)
                csrf, intent, state = form['csrf'][0], form['intent_id'][0], form['state'][0]
            except (ValueError, KeyError, UnicodeDecodeError):
                return self.send_html(400, '<p>请求无效。</p>')
            if not hmac.compare_digest(csrf.encode(), current['csrf'].encode()) or state not in ('fulfilled', 'failed'):
                return self.send_html(403, '<p>请求无效。</p>')
            store.mark(intent, state)
            self.send_response(303)
            self.send_header('Location', '/staff')
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
                             tls_cert_file=args.tls_cert_file, tls_key_file=args.tls_key_file)
        print(json.dumps({'ready': True}), flush=True)
        server.serve_forever()
        return 0
    except (ValueError, OSError) as error:
        print(json.dumps({'ok': False, 'error': type(error).__name__}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
