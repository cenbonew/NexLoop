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


# ---- C2: the WebChat redemption (consumer realm) ------------------------------------------------------------------------------------
# One fixed page for every refusal (unknown, used, expired, wrong target, not synthetic, rate limited...): the reason goes to the
# audit only (0392 events), never back to the browser.
FAILED_PAGE = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>自动登录失败</title></head>'
               '<body><p>自动登录失败，请手动登录。</p><p><a href="/">返回登录页</a></p></body></html>').encode()
_TICKET = re.compile(r'[A-Za-z0-9_-]{43}')


def same_origin_navigation(headers, origin):
    """The redemption form is posted from the workbench document, which is served with Referrer-Policy: no-referrer; under that
    policy a browser serializes the Origin of a navigating non-GET form post as "null" (Fetch, "serializing a request origin";
    R67). So the post is accepted when Host is the canonical origin's and either Origin is exactly the canonical origin, or Origin
    is "null" and the browser itself says the navigation is same-origin (Sec-Fetch-Site / Sec-Fetch-Mode are forbidden header names
    a page cannot set). A cross-site page gets Sec-Fetch-Site cross-site or same-site; a browser sending no Sec-Fetch-Site with
    Origin null is refused. "null" alone is never enough."""
    if headers.get('host') != urlsplit(origin).netloc:
        return False
    if headers.get('origin') == origin:
        return True
    return headers.get('origin') == 'null' and headers.get('sec-fetch-site') == 'same-origin' and headers.get('sec-fetch-mode') == 'navigate'


def redemption_routes(routes, config, *, cookie, services, resolver):
    """POST <auth prefix>/entry (form body ticket=..., same origin): take the ticket once in SQL, open the pinned test customer's
    session (authentication_methods [entry_ticket]) and go to the WebChat (303 /); any refusal is FAILED_PAGE (401)."""
    from fastapi.responses import RedirectResponse, Response
    from starlette.concurrency import run_in_threadpool as threadpool

    def failed():
        return Response(FAILED_PAGE, status_code=401, media_type='text/html; charset=utf-8',
                        headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff'})

    def work(request, ticket):
        from types import SimpleNamespace
        from eios.identity.models import TenantMembership
        from nexloop_eios.browser_identity import verify_identity_role
        from nexloop_eios.browser_rate_limits import PostgresBrowserRateLimiter
        store, op, sessions = services(request)
        client = client_of(request, config)
        from nexloop_eios.browser_rate_limits import client_digest
        decision = PostgresBrowserRateLimiter(store.pool, identity_operator=True).consume(config.tenant_id, 'local_login', client_digest(
            request.app.state.browser_rate_key, canonical_origin=config.origin, client_identifier='ip:' + resolver.resolve(request)), operator=op)
        if not decision.allowed:
            return None
        with store.pool.connection() as c, c.transaction():
            verify_identity_role(c)
            taken = c.execute('select control.nexloop_entry_ticket_take(%s,%s,%s,%s)', (config.tenant_id, config.application_id, digest(ticket), client)).fetchone()[0]
        if not taken['taken']:
            return None
        facts = taken['facts']
        membership = TenantMembership.model_validate_json(json.dumps(facts['membership']))
        binding = SimpleNamespace(application_revision=facts['application_revision'], subject_revision=facts['subject_revision'],
                                  credential_kind='local_account', tenant_id=config.tenant_id, credential_id=facts['credential_id'],
                                  credential_revision=facts['credential_revision'], credential_session_epoch=facts['credential_session_epoch'],
                                  provider_id=None, provider_revision=None, provider_configuration_fingerprint=None, restricted=facts['restricted'])
        issued = sessions._new_session(membership, application_id=config.application_id, authentication_methods=('entry_ticket',),
                                       identity_binding=binding, now=store.current_time())
        body = issued.session.model_dump(mode='json')
        from psycopg.types.json import Jsonb
        with store.pool.connection() as c, c.transaction():
            verify_identity_role(c)
            created = c.execute('select control.nexloop_entry_ticket_session(%s,%s,%s,%s,%s,%s,%s,%s)',
                                (config.tenant_id, config.application_id, digest(ticket), client, Jsonb(body),
                                 issued.session.session_token_digest.get_secret_value(), issued.session.csrf_token_digest.get_secret_value(),
                                 str(uuid.uuid4()))).fetchone()[0]
        if created != body:
            return None
        return issued.session_token.get_secret_value()

    @routes.post('/entry')
    async def entry(request: Request):
        if not config.entry_tickets:
            return failed()
        if not same_origin_navigation(request.headers, config.origin):
            return failed()
        try:
            if request.query_params or request.headers.get('content-type', '').split(';')[0] != 'application/x-www-form-urlencoded':
                return failed()
            raw = await request.body()
            if len(raw) > 128:
                return failed()
            from urllib.parse import parse_qs
            form = parse_qs(raw.decode('ascii'), strict_parsing=True, keep_blank_values=True)
            if set(form) != {'ticket'} or len(form['ticket']) != 1 or not _TICKET.fullmatch(form['ticket'][0]):
                return failed()
            token = await threadpool(lambda: work(request, form['ticket'][0]))
        except Exception:
            return failed()
        if token is None:
            return failed()
        result = RedirectResponse('/', status_code=303, headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff'})
        result.set_cookie(cookie, token, secure=True, httponly=True, samesite='strict', path='/')
        return result
