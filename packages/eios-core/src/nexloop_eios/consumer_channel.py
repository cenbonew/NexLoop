"""Consumer channel client (phase 1): a consumer's view of NexLoop over the existing WebChat HTTP API.

docs/implementation/consumer-channel-cli-mcp.md. No server change: this is the same browser path a person uses in WebChat
(nexloop_eios.browser_http login, cookie session and CSRF; nexloop_eios.web_chat_http conversations, messages, receipts and
the SSE stream), so every message still enters through the governed Conversation Actions with the consumer's own authority,
contact restrictions and the reply guarantee unchanged. It is meant for test drivers (for example Codex playing a test-persona
consumer) and never bypasses the API.

Secrets: the password comes from a private file or stdin (never argv, never logged); the session cookie and CSRF token are kept
only in memory or in a private session file (0600, owned) and are never printed. Every result is a JSON-ready dict without them.
"""
from dataclasses import dataclass
import http.client
import json
import os
import re
import secrets
import ssl
import stat
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from nexloop_eios.private_configuration import read_private_text

COOKIE = '__Host-nexloop_session'
_ID = re.compile(r'[0-9a-f]{64}')
SESSION_SCHEMA = 'nexloop-consumer-session/1'


class ConsumerChannelError(Exception):
    """A failed call reduced to a code (never a body, cookie or password)."""
    def __init__(self, code, status=None, retry_after=None):
        super().__init__(code)
        self.code, self.status, self.retry_after = code, status, retry_after

    def as_dict(self):
        out = {'ok': False, 'code': self.code}
        if self.status is not None:
            out['status'] = self.status
        if self.retry_after is not None:
            out['retry_after'] = self.retry_after
        return out


def https_transport(ca_file=None, timeout=15.0):
    """(method, origin, path, headers, body) -> (status, headers{lower: value}, body bytes) over TLS (system CA or --ca-file)."""
    context = ssl.create_default_context(cafile=str(ca_file) if ca_file else None)

    def send(method, origin, path, headers, body):
        parts = urlsplit(origin)
        connection = http.client.HTTPSConnection(parts.hostname, parts.port or 443, timeout=timeout, context=context)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            data = response.read(1 << 22)
            return response.status, {k.lower(): v for k, v in response.getheaders()}, data
        except (OSError, http.client.HTTPException):
            raise ConsumerChannelError('transport_unavailable') from None
        finally:
            connection.close()
    return send


def _cookie_from(headers):
    raw = headers.get('set-cookie', '')
    match = re.search(re.escape(COOKIE) + r'=([^;,\s]+)', raw)
    return match.group(1) if match else None


@dataclass
class Session:
    origin: str
    principal_id: str | None = None
    tenant_id: str | None = None
    cookie: str | None = None
    csrf: str | None = None
    conversation_id: str | None = None

    def public(self):
        return {'origin': self.origin, 'principal_id': self.principal_id, 'tenant_id': self.tenant_id, 'conversation_id': self.conversation_id,
                'authenticated': bool(self.cookie)}


def save_session(path, session):
    """A private file (0600, owned); replaced atomically."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + secrets.token_hex(4))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump({'schema': SESSION_SCHEMA, **session.__dict__}, stream)
    os.replace(temporary, path)


def load_session(path):
    body = json.loads(read_private_text(path, maximum=16384))
    if type(body) is not dict or body.pop('schema', None) != SESSION_SCHEMA:
        raise ConsumerChannelError('session_file_invalid')
    return Session(**{k: body.get(k) for k in Session.__dataclass_fields__})


def read_password(*, password_file=None, stdin=None):
    """From a private file, or the first line of stdin (e.g. a keychain command piped in). Never from argv or the environment."""
    if (password_file is None) == (stdin is None):
        raise ConsumerChannelError('password_source_required')
    value = read_private_text(password_file, maximum=1024) if password_file is not None else stdin.readline().rstrip('\r\n')
    if not 0 < len(value) <= 1024:
        raise ConsumerChannelError('password_invalid')
    return value


class ConsumerClient:
    def __init__(self, origin, *, transport=None, ca_file=None, session=None, clock=time.monotonic, sleep=time.sleep):
        parts = urlsplit(origin)
        if parts.scheme != 'https' or not parts.hostname or parts.path not in ('', '/') or parts.query or parts.fragment or parts.username:
            raise ConsumerChannelError('origin_invalid')
        self.origin = f'{parts.scheme}://{parts.netloc}'
        self.transport = transport or https_transport(ca_file)
        self.session = session or Session(origin=self.origin)
        if self.session.origin != self.origin:
            raise ConsumerChannelError('session_origin_mismatch')
        self.clock, self.sleep = clock, sleep

    # transport --------------------------------------------------------------------------------------------------------
    def _request(self, method, path, *, body=None, write=False, idempotency_key=None, extra=None):
        headers = {'Origin': self.origin, 'Host': urlsplit(self.origin).netloc, 'Accept': 'application/json'}
        if self.session.cookie:
            headers['Cookie'] = f'{COOKIE}={self.session.cookie}'
        if write and self.session.csrf:
            headers['X-CSRF-Token'] = self.session.csrf
        if idempotency_key:
            headers['Idempotency-Key'] = idempotency_key
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode()
            headers['Content-Type'] = 'application/json'
        headers.update(extra or {})
        status, response_headers, raw = self.transport(method, self.origin, path, headers, data)
        cookie = _cookie_from(response_headers)
        if cookie:
            self.session.cookie = cookie
        if status >= 400:
            try:
                code = json.loads(raw).get('code') or 'error'
            except (ValueError, AttributeError):
                code = 'error'
            if status == 401:
                code = 'session_expired' if self.session.cookie and path != '/api/v1/auth/login' else 'unauthenticated'
            retry = response_headers.get('retry-after')
            raise ConsumerChannelError(code, status, int(retry) if retry and retry.isdigit() else None)
        return status, response_headers, raw

    def _json(self, *args, **kwargs):
        _, _, raw = self._request(*args, **kwargs)
        try:
            return json.loads(raw)
        except ValueError:
            raise ConsumerChannelError('response_invalid') from None

    @staticmethod
    def _key(prefix):
        return f'{prefix}-{secrets.token_hex(12)}'

    @staticmethod
    def _id(value, what):
        if type(value) is not str or not _ID.fullmatch(value):
            raise ConsumerChannelError(f'{what}_invalid')
        return value

    # session ----------------------------------------------------------------------------------------------------------
    def login(self, username, password):
        if type(username) is not str or not 0 < len(username) <= 1024:
            raise ConsumerChannelError('username_invalid')
        self.session.cookie = None
        value = self._json('POST', '/api/v1/auth/login', body={'username': username, 'password': password})
        self.session.csrf = value.get('csrf_token')
        self.session.principal_id, self.session.tenant_id = value.get('principal_id'), value.get('tenant_id')
        if not self.session.cookie or not self.session.csrf:
            raise ConsumerChannelError('login_incomplete')
        return {'ok': True, **self.session.public(), 'restricted': value.get('restricted')}

    def refresh_csrf(self):
        value = self._json('POST', '/api/v1/auth/csrf', body={})
        self.session.csrf = value.get('csrf_token')
        return {'ok': True, **self.session.public()}

    def logout(self):
        self._request('POST', '/api/v1/auth/logout', write=True)
        self.session.cookie = self.session.csrf = None
        return {'ok': True, 'authenticated': False}

    def whoami(self):
        return {'ok': True, **self._json('GET', '/api/v1/auth/session')}

    # conversations and messages ---------------------------------------------------------------------------------------
    def create_conversation(self, idempotency_key=None):
        value = self._json('POST', '/api/v1/conversations', body={}, write=True, idempotency_key=idempotency_key or self._key('cli-conversation'))
        self.session.conversation_id = value['id']
        return {'ok': True, 'conversation': value}

    def list_conversations(self, after='', limit=50):
        query = urlencode({k: v for k, v in (('after', after), ('limit', limit)) if v})
        return {'ok': True, **self._json('GET', '/api/v1/conversations' + ('?' + query if query else ''))}

    def conversation(self, conversation_id=None):
        """The given conversation, else the session's last one, else a new one."""
        if conversation_id:
            return self._id(conversation_id, 'conversation_id')
        if self.session.conversation_id:
            return self.session.conversation_id
        return self.create_conversation()['conversation']['id']

    def send(self, text, *, conversation_id=None, idempotency_key=None):
        if type(text) is not str or not text.strip() or len(text) > 8192:
            raise ConsumerChannelError('text_invalid')
        conversation = self.conversation(conversation_id)
        value = self._json('POST', f'/api/v1/conversations/{conversation}/messages', body={'body': text}, write=True,
                           idempotency_key=idempotency_key or self._key('cli-message'))
        self.session.conversation_id = conversation
        return {'ok': True, 'conversation_id': conversation, 'created': value.get('created'), 'message': value.get('message')}

    def history(self, conversation_id=None, *, after=0, limit=50):
        conversation = self.conversation(conversation_id) if conversation_id or self.session.conversation_id else None
        if conversation is None:
            raise ConsumerChannelError('conversation_required')
        value = self._json('GET', f'/api/v1/conversations/{conversation}/messages?' + urlencode({'after': int(after), 'limit': int(limit)}))
        return {'ok': True, 'conversation_id': conversation, **value}

    def receipt(self, message_id):
        message_id = self._id(message_id, 'message_id')
        out = {'ok': True, 'message_id': message_id}
        for name, path in (('receipt', 'receipt'), ('run', 'run')):
            try:
                out[name] = self._json('GET', f'/api/v1/messages/{message_id}/{path}')
            except ConsumerChannelError as error:
                out[name] = error.as_dict()
        return out

    def wait_reply(self, conversation_id=None, *, after_sequence=0, timeout=120.0, poll=2.0):
        """Poll the conversation until a message from someone other than this consumer appears after `after_sequence`."""
        conversation = self.conversation(conversation_id)
        deadline, cursor, seen = self.clock() + float(timeout), int(after_sequence), []
        while True:
            page = self._json('GET', f'/api/v1/conversations/{conversation}/messages?' + urlencode({'after': cursor, 'limit': 100}))
            for item in page.get('items', []):
                cursor = max(cursor, int(item.get('sequence', cursor)))
                if item.get('actor') != self.session.principal_id:
                    seen.append(item)
            if seen:
                return {'ok': True, 'replied': True, 'conversation_id': conversation, 'replies': seen, 'last_sequence': cursor}
            if self.clock() >= deadline:
                return {'ok': True, 'replied': False, 'timed_out': True, 'conversation_id': conversation, 'last_sequence': cursor}
            self.sleep(min(poll, max(0.0, deadline - self.clock())))

    def watch(self, conversation_id=None, *, after=0):
        """One SSE request (the server ends it after its stream lifetime); returns the committed events it carried."""
        conversation = self.conversation(conversation_id)
        _, _, raw = self._request('GET', f'/api/v1/conversations/{conversation}/events', extra={'Accept': 'text/event-stream',
                                                                                                'Last-Event-ID': str(int(after))})
        events, unavailable = [], False
        for line in raw.decode('utf-8', 'replace').splitlines():
            if line.startswith('data: '):
                try:
                    payload = json.loads(line[6:])
                except ValueError:
                    continue
                if payload.get('code') == 'stream_unavailable':
                    unavailable = True
                else:
                    events.append(payload)
        return {'ok': True, 'conversation_id': conversation, 'events': events, 'unavailable': unavailable,
                'last_event_id': events[-1]['id'] if events else str(int(after))}


def private_session_path(path):
    """A session file must be a private regular file when it exists."""
    path = Path(path)
    if path.exists():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise ConsumerChannelError('session_file_not_private')
    return path
