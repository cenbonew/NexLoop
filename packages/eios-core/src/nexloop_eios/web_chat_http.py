"""Same-origin Conversation HTTP boundary; authority comes from a trusted factory.

The factory must authenticate the actual BrowserSession/current Consumer binding
and return governed Conversation ports. It is never a service impersonation hook.
No router registration or fallback provider is installed by this module.
"""
import asyncio
import json
import re
import uuid
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from eios.identity.errors import CredentialInvalid
from eios.identity.ports import TrustedIdentityOperator
from eios.identity.sessions import BrowserSessionService
from nexloop_eios.browser_http import COOKIE
from nexloop_eios.conversation_messages import ConversationConflict


def router(config, *, ports_for_browser, execution_profile=None, stream_seconds=180):
    """ports_for_browser(request, actual_session) must recheck live authority."""
    if not callable(ports_for_browser):
        raise ValueError('governed browser ports required')
    if execution_profile is not None and (type(execution_profile) is not str or execution_profile not in ('deterministic-test', 'real-provider', 'disabled')):
        raise ValueError('trusted execution profile required')
    if type(stream_seconds) is not int or not 1 <= stream_seconds <= 180:
        raise ValueError('bounded stream lifetime required')
    routes = APIRouter(prefix='/api/v1')
    origin = urlsplit(config.origin)

    def error(code, status):
        return JSONResponse({'code': code, 'message': '请求未完成',
            'trace_id': str(uuid.uuid4()), 'retryable': status == 503, 'details': {}},
            status_code=status, headers={'Cache-Control': 'no-store'})

    def authenticated(request, *, write=False):
        if request.headers.get('host') != origin.netloc:
            raise PermissionError()
        if write and request.headers.get('origin') != config.origin:
            raise PermissionError()
        if not write and request.headers.get('origin') not in (None, config.origin):
            raise PermissionError()
        store = getattr(request.app.state, 'browser_store', None)
        if store is None:
            raise RuntimeError()
        op = TrustedIdentityOperator(operator_principal_id='nexloop_identity',
            request_id=str(uuid.uuid4()), trace_id=str(uuid.uuid4()))
        sessions = BrowserSessionService(store, store,
            application_id=config.application_id, operator=op)
        session = sessions.inspect(request.cookies.get(COOKIE, ''))
        if write:
            sessions.verify_csrf(session, request.headers.get('x-csrf-token', ''))
            if session.restricted:
                raise PermissionError()
        return ports_for_browser(request, session)

    async def invoke(request, method, *, write=False, **arguments):
        try:
            def work():
                ports = authenticated(request, write=write)
                return getattr(ports, method)(**arguments)
            value = await run_in_threadpool(work)
            if execution_profile is not None:
                if method == 'create_conversation':
                    value = {**value, 'execution_profile': execution_profile}
                elif method == 'list_conversations':
                    value = {**value, 'items': [{**item, 'execution_profile': execution_profile} for item in value['items']]}
            return JSONResponse(value, status_code=202 if write and method in ('accept_message','accept_native_message') else 200,
                headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
        except ConversationConflict:
            return error('conversation_payload_conflict', 409)
        except CredentialInvalid:
            return error('unauthenticated', 401)
        except PermissionError:
            return error('forbidden', 403)
        except Exception:
            return error('dependency_unavailable', 503)

    async def body(request, fields):
        if request.headers.get('content-type', '').split(';')[0] != 'application/json':
            raise ValueError()
        data = b''
        async with asyncio.timeout(5):
            async for part in request.stream():
                data += part
                if len(data) > 32768:
                    raise ValueError()
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError()
                result[key] = value
            return result
        value = json.loads(data, object_pairs_hook=pairs)
        if type(value) is not dict or (set(value) != fields if isinstance(fields, (set, frozenset)) else set(value) not in fields):
            raise ValueError()
        return value

    def key(request):
        value = request.headers.get('idempotency-key', '')
        if not re.fullmatch(r'[A-Za-z0-9._~-]{16,160}', value):
            raise ValueError()
        return value

    @routes.get('/conversations')
    async def conversations(request: Request, after: str = '', limit: int = 50):
        if (after and not re.fullmatch(r'[0-9a-f]{64}', after)) or not 1 <= limit <= 100:
            return error('invalid_request', 422)
        return await invoke(request, 'list_conversations', after=after, limit=limit)

    @routes.post('/conversations')
    async def create(request: Request):
        try:
            await body(request, set())
            return await invoke(request, 'create_conversation', write=True, idempotency_key=key(request))
        except TimeoutError:
            return error('request_timeout', 408)
        except (ValueError, UnicodeError):
            return error('invalid_request', 422)

    @routes.get('/conversations/{conversation_id}/messages')
    async def messages(conversation_id: str, request: Request, after: int = 0, limit: int = 50):
        if after < 0 or after > 9223372036854775807 or not 1 <= limit <= 100:
            return error('invalid_request', 422)
        return await invoke(request, 'read_messages', conversation_id=conversation_id, after_sequence=after, limit=limit)

    @routes.get('/messages/{message_id}/run')
    async def message_run(message_id: str, request: Request):
        if not re.fullmatch(r'[0-9a-f]{64}', message_id):
            return error('invalid_request', 422)
        return await invoke(request, 'read_message_run', message_id=message_id)

    @routes.get('/messages/{message_id}/scope-denial')
    async def scope_denial(message_id: str, request: Request):
        return await invoke(request, 'read_message_scope_denial', message_id=message_id)

    @routes.get('/messages/{message_id}/receipt')
    async def message_receipt(message_id: str, request: Request):
        if not re.fullmatch(r'[0-9a-f]{64}', message_id):
            return error('invalid_request', 422)
        return await invoke(request, 'read_message_service_receipt', message_id=message_id)

    @routes.post('/conversations/{conversation_id}/messages')
    async def send(conversation_id: str, request: Request):
        try:
            value = await body(request, {'body'})
            if type(value['body']) is not str or not 0 < len(value['body']) <= 8192 or not value['body'].strip():
                raise ValueError()
            return await invoke(request, 'accept_message', write=True,
                conversation_id=conversation_id, body=value['body'], idempotency_key=key(request))
        except TimeoutError:
            return error('request_timeout', 408)
        except (ValueError, UnicodeError):
            return error('invalid_request', 422)

    @routes.post('/conversations/{conversation_id}/native-messages')
    async def native_send(conversation_id: str, request: Request):
        try:
            v1 = {'schema_version','provider_event_id','body'}
            # NX-051 v2: optional client-level order/time/reply evidence (never ordering, never a time anchor).
            v2 = frozenset(v1 | {'client_sequence','client_sent_at','reply_to'})
            value = await body(request, (frozenset(v1), v2))
            expected = 'nexloop.native-message.v2' if set(value) == v2 else 'nexloop.native-message.v1'
            if value['schema_version'] != expected or type(value['provider_event_id']) is not str or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',value['provider_event_id']):
                raise ValueError()
            if type(value['body']) is not str or not 0 < len(value['body']) <= 8192 or not value['body'].strip():
                raise ValueError()
            client = {name: value[name] for name in ('client_sequence','client_sent_at','reply_to')} if set(value) == v2 else {}
            from nexloop_eios.native_web_inbound import _client_fields
            _client_fields(client.get('client_sequence'), client.get('client_sent_at'), client.get('reply_to'))  # shape before any write
            return await invoke(request, 'accept_native_message', write=True,conversation_id=conversation_id,
                body=value['body'],provider_event_id=value['provider_event_id'],idempotency_key=key(request),**client)
        except TimeoutError:
            return error('request_timeout',408)
        except (ValueError,UnicodeError):
            return error('invalid_request',422)

    @routes.get('/conversations/{conversation_id}/events')
    async def events(conversation_id: str, request: Request):
        raw = request.headers.get('last-event-id', request.query_params.get('after', '0'))
        if not re.fullmatch(r'0|[1-9][0-9]{0,18}', raw):
            return error('invalid_request', 422)
        try:
            def initial_read():
                return authenticated(request).read_events(
                    conversation_id=conversation_id, after_sequence=int(raw))
            first = await run_in_threadpool(initial_read)
        except ConversationConflict:
            return error('conversation_payload_conflict', 409)
        except CredentialInvalid:
            return error('unauthenticated', 401)
        except PermissionError:
            return error('forbidden', 403)
        except Exception:
            return error('dependency_unavailable', 503)

        async def committed():
            cursor = int(raw)
            first_page = first
            for _ in range(stream_seconds):
                if await request.is_disconnected():
                    return
                try:
                    def read():
                        return authenticated(request).read_events(
                            conversation_id=conversation_id, after_sequence=cursor)
                    if first_page is not None:
                        result, first_page = first_page, None
                    else:
                        result = await run_in_threadpool(read)
                    for item in result['items']:
                        event_id = item['id']
                        if type(event_id) is not str or not re.fullmatch(r'[1-9][0-9]{0,18}', event_id):
                            raise ValueError()
                        sequence = int(event_id)
                        if type(sequence) is not int or sequence <= cursor:
                            raise ValueError()
                        encoded = json.dumps(item, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
                        yield f'id: {sequence}\nevent: committed\ndata: {encoded}\n\n'
                        cursor = sequence
                    yield ': heartbeat\n\n'
                except Exception:
                    yield 'event: unavailable\ndata: {"code":"stream_unavailable"}\n\n'
                    return
                await asyncio.sleep(1)
        return StreamingResponse(committed(), media_type='text/event-stream',
            headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})
    return routes
