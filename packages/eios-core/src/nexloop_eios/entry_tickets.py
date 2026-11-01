"""S81 / ADR-043: entry tickets - the owner opens the WebChat as the pinned synthetic test customer, or the reference endpoint staff
page as the read-only test staff, from the workbench 入口 page, without a password (design: notes l2-s81-entry-autologin v2).

``POST /api/v1/workbench/entry-tickets`` {"target": "webchat" | "reference_staff"} (workbench realm only: its cookie, same origin,
CSRF): the target account is never the client's - it is the deployment's pinned ENTRY_TEST_WEBCHAT_PRINCIPAL or staff label. SQL
(0392 control.nexloop_entry_ticket_issue) re-reads the workbench session and the member's current role (owner only), and for the
WebChat checks the pinned principal is a synthetic test customer (persona simulated_agent, one Consumer, not erasing, active), then
records the ticket's sha256 and an audit event; a refusal is recorded too. The answer carries the ticket once, with the form action
to post it to (never in a URL): the WebChat's /api/v1/auth/entry, or the reference endpoint's /staff/entry for an HMAC ticket the
endpoint checks with the shared entry ticket key. Off unless the process environment says NEXLOOP_ENTRY_TICKETS set to stage (404).
"""
import base64
import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from urllib.parse import urlsplit

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from eios.identity.errors import CredentialInvalid
from eios.identity.ports import TrustedIdentityOperator
from eios.identity.sessions import BrowserSessionService

TARGETS = ('webchat', 'reference_staff')
SWITCH = 'NEXLOOP_ENTRY_TICKETS'
ENABLED = 'stage'
HMAC_CONTEXT = b'nexloop-entry-ticket/1|'
STAFF_LABEL = re.compile(r'[a-z][a-z0-9-]{0,31}')
HEADERS = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}


def switch_enabled(environ):
    """The process switch: 'stage' turns entry tickets on; unset, empty or 'off' leaves them off; anything else refuses the start."""
    value = environ.get(SWITCH, '')
    if value not in ('', 'off', ENABLED):
        raise ValueError(SWITCH + ' must be stage or off')
    return value == ENABLED


def digest(ticket):
    return hashlib.sha256(ticket.encode('ascii')).hexdigest()


def load_key(path):
    from nexloop_eios.private_configuration import read_private_text
    key = bytes.fromhex(read_private_text(path, maximum=128).strip())
    if len(key) != 32:
        raise ValueError('entry ticket key: 32 bytes')
    return key


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def sign_staff_ticket(key, *, label, ttl, now=None):
    """The reference endpoint's ticket: base64url(payload) '.' base64url(HMAC-SHA256(key, context | payload))."""
    issued = int(now if now is not None else time.time())
    payload = json.dumps({'v': 1, 'aud': 'reference_staff', 'sub': label, 'jti': secrets.token_hex(16), 'iat': issued, 'exp': issued + ttl},
                         separators=(',', ':'), sort_keys=True).encode()
    return _b64(payload) + '.' + _b64(hmac.new(key, HMAC_CONTEXT + payload, 'sha256').digest())


def client_of(request, config):
    """The client digest kept with a ticket and its events: the login rate limit's keyed digest of the client address (never the address)."""
    from nexloop_eios.browser_http import client_ip_resolver
    from nexloop_eios.browser_rate_limits import client_digest
    return client_digest(request.app.state.browser_rate_key, canonical_origin=config.origin,
                         client_identifier='entry:' + client_ip_resolver(config).resolve(request)).get_secret_value().hex()


def router(config):
    """config: the workbench BrowserConfiguration (entry_tickets, entry_test_webchat_principal, entry_test_staff_label,
    entry_ticket_key_file, entry_ticket_ttl, reference_endpoint_staff_url)."""
    routes = APIRouter(prefix='/api/v1/workbench/entry-tickets')
    origin = urlsplit(config.origin)

    def error(code, status):
        return JSONResponse({'code': code, 'message': '请求未完成', 'trace_id': str(uuid.uuid4()), 'retryable': status == 503, 'details': {}},
                            status_code=status, headers={'Cache-Control': 'no-store'})

    def issue(request, target):
        from nexloop_eios.browser_identity import verify_identity_role
        if request.headers.get('host') != origin.netloc or request.headers.get('origin') != config.origin:
            raise PermissionError()
        store = getattr(request.app.state, 'workbench_store', None)
        if store is None:
            raise RuntimeError()
        from nexloop_eios.browser_http import WORKBENCH_COOKIE
        op = TrustedIdentityOperator(operator_principal_id='nexloop_identity', request_id=str(uuid.uuid4()), trace_id=str(uuid.uuid4()))
        sessions = BrowserSessionService(store, store, application_id=config.application_id, operator=op)
        session = sessions.inspect(request.cookies.get(WORKBENCH_COOKIE, ''))
        sessions.verify_csrf(session, request.headers.get('x-csrf-token', ''))
        if session.restricted:
            raise PermissionError()
        if target == 'webchat':
            pinned = config.entry_test_webchat_principal
            ticket = secrets.token_urlsafe(32)
            action = '/api/v1/auth/entry'
        else:
            pinned = config.entry_test_staff_label
            ticket = sign_staff_ticket(load_key(config.entry_ticket_key_file), label=pinned, ttl=config.entry_ticket_ttl)
            staff = urlsplit(config.reference_endpoint_staff_url)
            action = f'{staff.scheme}://{staff.netloc}/staff/entry'
        client = client_of(request, config)
        with store.pool.connection() as c, c.transaction():
            verify_identity_role(c)
            result = c.execute('select control.nexloop_entry_ticket_issue(%s,%s,%s,%s,%s,%s,%s,%s)',
                               (config.tenant_id, config.application_id, session.session_token_digest.get_secret_value(), target, pinned,
                                digest(ticket), client, config.entry_ticket_ttl)).fetchone()[0]
        if not result['issued']:
            return None, result['code']
        return {'target': target, 'action': action, 'ticket': ticket, 'expires_at': result['expires_at']}, None

    @routes.post('')
    async def entry_ticket(request: Request):
        from nexloop_eios.browser_http import WORKBENCH_COOKIE
        if not request.cookies.get(WORKBENCH_COOKIE):
            return error('unauthenticated', 401)  # no credential, no answer about the switch (G5)
        if not config.entry_tickets:
            return error('not_found', 404)
        try:
            if request.query_params or request.headers.get('content-type', '').split(';')[0] != 'application/json':
                raise ValueError()
            raw = await request.body()
            if len(raw) > 256:
                raise ValueError()
            body = json.loads(raw)
            if type(body) is not dict or set(body) != {'target'} or body['target'] not in TARGETS:
                raise ValueError()
            target = body['target']
        except (ValueError, UnicodeError):
            return error('invalid_request', 422)
        if target == 'webchat' and not config.entry_test_webchat_principal or target == 'reference_staff' and not (
                config.entry_ticket_key_file and config.reference_endpoint_staff_url):
            return error('not_found', 404)
        try:
            value, refused = await run_in_threadpool(lambda: issue(request, target))
        except CredentialInvalid:
            return error('unauthenticated', 401)
        except (PermissionError, psycopg.errors.InsufficientPrivilege):
            return error('forbidden', 403)
        except Exception:
            return error('dependency_unavailable', 503)
        if refused is not None:
            return error('not_owner' if refused == 'not_owner' else 'entry_target_refused', 403)
        return JSONResponse(value, headers=HEADERS)

    return routes
