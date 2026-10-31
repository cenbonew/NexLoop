"""NX-028 slice 1: same-origin owner workbench reads, GET /api/v1/workbench/* (design §3, §9, §15.2).

Only an actual, unrestricted Human session of the workbench browser application (its own login realm and cookie) reaches
the ports; tenant, world and principal come from that session, never from the request. Every request re-authenticates in
PostgreSQL, so a revoked grant or membership is a 403 on the very next request. Status codes are exact (AT-045):
401 no/expired session, 403 no grant or not a member, 404 unknown object, 422 malformed request, 503 dependency unavailable.
"""
import re
import uuid
from urllib.parse import urlsplit

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from eios.identity.errors import CredentialInvalid
from eios.identity.ports import TrustedIdentityOperator
from eios.identity.sessions import BrowserSessionService

from nexloop_eios.browser_http import WORKBENCH_COOKIE

_ID = re.compile(r'[a-f0-9]{64}')
_CURSOR = re.compile(r'[0-9][0-9T:. +\-]{9,39}\|[a-f0-9]{64}')
_STATES = ('accepted', 'dispatching', 'unknown', 'fulfilled', 'failed', 'confirmed')
_REMEDIATION_STATES = ('proposed', 'awaiting_approval', 'executing', 'succeeded', 'refused', 'failed', 'escalated', 'handled', 'declined', 'expired',
                       'not_applicable')
_TYPE = re.compile(r'[A-Z][A-Za-z0-9_]{0,63}')
_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
_OBJECT_SET = re.compile(r'[a-z][a-z0-9_]{0,31}(\.[a-z][a-z0-9_]{0,31}){0,3}')  # ADR-036
_OBJECT = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,159}')
_ARTIFACT_CURSOR = re.compile(r'[0-9][0-9T:. +\-]{9,39}\|[a-f0-9]{32}')
_OFFSET = re.compile(r'0|[1-9][0-9]{0,5}')
_INSTANT = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})')
_CONSUMER_SORTS = ('id', 'open_commitments', 'conversations', 'active_plans', 'last_conversation_at')  # X3: the latest message time
HEADERS = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}


def router(config, *, ports_for_workbench):
    """ports_for_workbench(request, inspected_session) must re-authenticate in PostgreSQL."""
    if not callable(ports_for_workbench):
        raise ValueError('governed workbench ports required')
    routes = APIRouter(prefix='/api/v1/workbench')
    origin = urlsplit(config.origin)

    def error(code, status):
        return JSONResponse({'code': code, 'message': '请求未完成', 'trace_id': str(uuid.uuid4()),
            'retryable': status == 503, 'details': {}}, status_code=status, headers={'Cache-Control': 'no-store'})

    def ports(request):
        if request.headers.get('host') != origin.netloc or request.headers.get('origin') not in (None, config.origin):
            raise PermissionError()
        store = getattr(request.app.state, 'workbench_store', None)
        if store is None:
            raise RuntimeError()
        op = TrustedIdentityOperator(operator_principal_id='nexloop_identity', request_id=str(uuid.uuid4()), trace_id=str(uuid.uuid4()))
        session = BrowserSessionService(store, store, application_id=config.application_id, operator=op).inspect(request.cookies.get(WORKBENCH_COOKIE, ''))
        if session.restricted:
            raise PermissionError()
        return ports_for_workbench(request, session)

    async def invoke(request, method, **arguments):
        from nexloop_eios.workbench_reads import WorkbenchForbidden
        try:
            value = await run_in_threadpool(lambda: getattr(ports(request), method)(**arguments))
            if value is None:
                return error('not_found', 404)
            return JSONResponse(value, headers=HEADERS)
        except CredentialInvalid:
            return error('unauthenticated', 401)
        except (WorkbenchForbidden, PermissionError, psycopg.errors.InsufficientPrivilege):
            return error('forbidden', 403)
        except (ValueError, psycopg.errors.InvalidParameterValue):
            return error('invalid_request', 422)
        except Exception:
            return error('dependency_unavailable', 503)

    def limit_of(raw):
        if not re.fullmatch(r'[1-9][0-9]{0,2}', raw) or not 1 <= int(raw) <= 200:
            raise ValueError()
        return int(raw)

    def instant(raw):
        if not _INSTANT.fullmatch(raw):
            raise ValueError()
        from datetime import datetime
        value = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        if value.tzinfo is None:
            raise ValueError()
        return value

    def flag(raw):
        if raw not in ('true', 'false'):
            raise ValueError()
        return raw == 'true'

    def query(request, allowed):
        if set(request.query_params) - set(allowed) or any(len(request.query_params.getlist(k)) > 1 for k in request.query_params):
            raise ValueError()
        return request.query_params

    @routes.get('/overview')
    async def overview(request: Request):
        try:
            query(request, ())
        except ValueError:
            return error('invalid_request', 422)
        return await invoke(request, 'overview')

    @routes.get('/goals')
    async def goals(request: Request):
        try:
            q = query(request, ('limit',))
            arguments = {'limit': limit_of(q['limit'])} if 'limit' in q else {}
        except ValueError:
            return error('invalid_request', 422)
        return await invoke(request, 'goals', **arguments)

    @routes.get('/consumers')
    async def consumers(request: Request):
        # F09: filters on the list's own counts, sort, total and an offset cursor; `after` (id cursor) stays for the plain list.
        try:
            q = query(request, ('after', 'limit', 'restricted', 'has_open_commitments', 'sort', 'order', 'cursor'))
            if 'after' in q and not _ID.fullmatch(q['after']) or 'sort' in q and q['sort'] not in _CONSUMER_SORTS or 'order' in q and q['order'] not in ('asc', 'desc') \
                    or 'cursor' in q and not _OFFSET.fullmatch(q['cursor']) or 'after' in q and len(set(q) & {'restricted', 'has_open_commitments', 'sort', 'cursor'}):
                raise ValueError()
            limit = limit_of(q['limit']) if 'limit' in q else 50
            if limit > 100 and set(q) & {'restricted', 'has_open_commitments', 'sort', 'cursor'}:
                raise ValueError()
            arguments = {'after': q.get('after'), 'limit': limit,
                         **{k: flag(q[k]) for k in ('restricted', 'has_open_commitments') if k in q},
                         **{k: q[k] for k in ('sort', 'order', 'cursor') if k in q}}
        except ValueError:
            return error('invalid_request', 422)
        return await invoke(request, 'consumers', **arguments)

    @routes.get('/consumers/{consumer_id}')
    async def consumer(consumer_id: str, request: Request):
        if not _ID.fullmatch(consumer_id) or request.query_params:
            return error('invalid_request', 422)
        return await invoke(request, 'consumer', consumer_id=consumer_id)

    @routes.get('/consumers/{consumer_id}/history')
    async def consumer_history(consumer_id: str, request: Request):
        # The customer's recent exchanges across conversations, newest first; cursor '<accepted_at>|<message id>'.
        try:
            q = query(request, ('before', 'limit'))
            if not _ID.fullmatch(consumer_id) or 'before' in q and not _CURSOR.fullmatch(q['before']):
                raise ValueError()
            limit = limit_of(q['limit']) if 'limit' in q else 10
            if limit > 50:
                raise ValueError()
        except ValueError:
            return error('invalid_request', 422)
        return await invoke(request, 'consumer_history', consumer_id=consumer_id, before=q.get('before'), limit=limit)

    @routes.get('/conversations/{conversation_id}')
    async def conversation(conversation_id: str, request: Request):
        if not _ID.fullmatch(conversation_id) or request.query_params:
            return error('invalid_request', 422)
        return await invoke(request, 'conversation', conversation_id=conversation_id)

    @routes.get('/plans')
    async def plans(request: Request):
        try:
            q = query(request, ('consumer_id',))
            if 'consumer_id' in q and not _ID.fullmatch(q['consumer_id']):
                raise ValueError()
        except ValueError:
            return error('invalid_request', 422)
        return await invoke(request, 'plans', consumer_id=q.get('consumer_id'))

    @routes.get('/actions')
    async def actions(request: Request):
        try:
            q = query(request, ('state', 'consumer_id', 'limit'))
            if 'state' in q and q['state'] not in _STATES or 'consumer_id' in q and not _ID.fullmatch(q['consumer_id']):
                raise ValueError()
            arguments = {'state': q.get('state'), 'consumer_id': q.get('consumer_id'), 'limit': limit_of(q['limit']) if 'limit' in q else 50}
        except ValueError:
            return error('invalid_request', 422)
        return await invoke(request, 'actions', **arguments)

    @routes.get('/remediations')
    async def remediations(request: Request):
        # NX-057 R1: remediation intents (state filter, limit); counts and codes only, no reason text.
        try:
            q = query(request, ('state', 'limit'))
            if 'state' in q and q['state'] not in _REMEDIATION_STATES:
                raise ValueError()
            arguments = {'state': q.get('state'), 'limit': limit_of(q['limit']) if 'limit' in q else 50}
        except ValueError:
            return error('invalid_request', 422)
        return await invoke(request, 'remediations', **arguments)

    @routes.get('/remediations/{remediation_id}')
    async def remediation_evidence(remediation_id: str, request: Request):
        # NX-057 R4: the evidence chain of one remediation (404 when unknown or of a Consumer being erased).
        if not _UUID.fullmatch(remediation_id):
            return error('invalid_request', 422)
        return await invoke(request, 'remediation_evidence', remediation_id=remediation_id)

    @routes.get('/commitments')
    async def commitments(request: Request):
        # F05/F09: status group (open / ended / all; `all=true` is the legacy group all), late flag, due window, sort, offset cursor.
        try:
            q = query(request, ('consumer_id', 'all', 'status_group', 'late', 'due_before', 'due_after', 'sort', 'order', 'cursor', 'limit'))
            if 'consumer_id' in q and not _ID.fullmatch(q['consumer_id']) or 'status_group' in q and q['status_group'] not in ('open', 'ended', 'all') \
                    or 'all' in q and 'status_group' in q or 'sort' in q and q['sort'] not in ('due_at', 'registered_at', 'status') \
                    or 'order' in q and q['order'] not in ('asc', 'desc') or 'cursor' in q and not _OFFSET.fullmatch(q['cursor']):
                raise ValueError()
            limit = limit_of(q['limit']) if 'limit' in q else 200
            arguments = {'consumer_id': q.get('consumer_id'), 'include_closed': flag(q['all']) if 'all' in q else False,
                         'limit': limit, **({'late': flag(q['late'])} if 'late' in q else {}),
                         **{k: instant(q[k]) for k in ('due_before', 'due_after') if k in q},
                         **{k: q[k] for k in ('status_group', 'sort', 'order', 'cursor') if k in q}}
        except ValueError:
            return error('invalid_request', 422)
        return await invoke(request, 'commitments', **arguments)

    @routes.get('/commitments/{commitment_id}')
    async def commitment(commitment_id: str, request: Request):
        if not _ID.fullmatch(commitment_id) or request.query_params:
            return error('invalid_request', 422)
        return await invoke(request, 'commitment', commitment_id=commitment_id)

    @routes.get('/contact')
    async def contact(request: Request):
        try:
            q = query(request, ('consumer_id', 'all'))
            if 'consumer_id' in q and not _ID.fullmatch(q['consumer_id']):
                raise ValueError()
            arguments = {'consumer_id': q.get('consumer_id'), 'include_released': flag(q['all']) if 'all' in q else False}
        except ValueError:
            return error('invalid_request', 422)
        return await invoke(request, 'contact', **arguments)

    @routes.get('/takeovers')
    async def takeovers(request: Request):
        if request.query_params:
            return error('invalid_request', 422)
        return await invoke(request, 'takeovers')

    @routes.get('/me')
    async def me(request: Request):
        # NX-028 page integration: the member's role and its Actions (display only; writes are decided by the governed entry).
        if request.query_params:
            return error('invalid_request', 422)
        return await invoke(request, 'me')

    @routes.get('/entries')
    async def entries(request: Request):
        # Owner S73: the deployment's links for the 入口 page under 管理 - the reference endpoint staff page URL, or null when the
        # deployment gives none. Any member's own session (re-authenticated like every read); no tenant data, nothing changes.
        if request.query_params:
            return error('invalid_request', 422)
        try:
            await run_in_threadpool(lambda: ports(request))
        except CredentialInvalid:
            return error('unauthenticated', 401)
        except (PermissionError, psycopg.errors.InsufficientPrivilege):
            return error('forbidden', 403)
        except Exception:
            return error('dependency_unavailable', 503)
        return JSONResponse({'reference_endpoint_staff_url': config.reference_endpoint_staff_url}, headers=HEADERS)

    @routes.get('/capabilities')
    async def capabilities(request: Request):
        # World model W3: read-only capability catalog (governed capabilities, business Actions, effect categories, channels,
        # delivery routes, real dispatch switches); no parameters.
        if request.query_params:
            return error('invalid_request', 422)
        return await invoke(request, 'capabilities')

    @routes.get('/objects/{type_name}')
    async def objects(type_name: str, request: Request):
        # World model W2: a type's instances, ids/versions/times only; unreadable ones counted, never listed.
        try:
            q = query(request, ('after', 'limit'))
            if not _TYPE.fullmatch(type_name) or 'after' in q and not _OBJECT.fullmatch(q['after']):
                raise ValueError()
            limit = limit_of(q['limit']) if 'limit' in q else 50
            if limit > 100:
                raise ValueError()
        except ValueError:
            return error('invalid_request', 422)
        return await invoke(request, 'objects', type_name=type_name, after=q.get('after'), limit=limit)

    @routes.get('/objects/{type_name}/{object_id}')
    async def object_values(type_name: str, object_id: str, request: Request):
        # D-G1 (owner 2026-10-20): one object's property values; restricted ones named only (withheld); each read audited.
        if not _TYPE.fullmatch(type_name) or not _OBJECT.fullmatch(object_id) or request.query_params:
            return error('invalid_request', 422)
        return await invoke(request, 'object_values', type_name=type_name, object_id=object_id)

    @routes.get('/semantic-review')
    async def semantic_review(request: Request):
        # NX-039 V6: the review queue's evaluation reports, the stops and the release watch (owner only).
        if request.query_params:
            return error('invalid_request', 422)
        return await invoke(request, 'semantic_review')

    @routes.get('/semantic-releases')
    async def semantic_releases(request: Request):
        # NX-039 V4: the owner's semantic release view (current version, history, publishable candidates); no parameters.
        if request.query_params:
            return error('invalid_request', 422)
        return await invoke(request, 'semantic_releases')

    @routes.get('/object-views/{type_name}')
    async def object_view(type_name: str, request: Request):
        # Capability gap P2: the layout an object detail of this type uses (every member; null when the type has no View).
        if request.query_params or not re.fullmatch(r'[A-Z][A-Za-z0-9_]{0,63}', type_name):
            return error('invalid_request', 422)
        return await invoke(request, 'object_view', type_name=type_name)

    @routes.get('/object-sets')
    async def object_sets(request: Request):
        # ADR-036: the tenant's Object Sets (latest version of each, conditions and projection); every member, read only.
        if request.query_params:
            return error('invalid_request', 422)
        return await invoke(request, 'object_sets')

    @routes.get('/object-sets/{stable_name}/members')
    async def object_set_members(stable_name: str, request: Request):
        # ADR-036: one preview page evaluated at read time; each object's READ and each projected property's READ checked.
        try:
            q = query(request, ('version', 'offset', 'limit'))
            if not _OBJECT_SET.fullmatch(stable_name):
                raise ValueError()
            version = limit_of(q['version']) if 'version' in q else None
            offset = int(q['offset']) if 'offset' in q and q['offset'].isdigit() and len(q['offset']) <= 4 else (0 if 'offset' not in q else None)
            limit = limit_of(q['limit']) if 'limit' in q else 25
            if offset is None or offset > 1000 or limit > 50:
                raise ValueError()
        except ValueError:
            return error('invalid_request', 422)
        return await invoke(request, 'object_set_members', stable_name=stable_name, version=version, offset=offset, limit=limit)

    @routes.get('/objects/{type_name}/{object_id}/neighbors')
    async def object_neighbors(type_name: str, object_id: str, request: Request):
        # World model W2: depth 1, 25 a group; every neighbour's READ checked.
        if not _TYPE.fullmatch(type_name) or not _OBJECT.fullmatch(object_id) or request.query_params:
            return error('invalid_request', 422)
        return await invoke(request, 'object_neighbors', type_name=type_name, object_id=object_id)

    @routes.get('/artifacts')
    async def artifacts(request: Request):
        # World model W4: Artifact metadata (never content), newest first; cursor '<created_at>|<artifact id>'.
        try:
            q = query(request, ('before', 'limit'))
            if 'before' in q and not _ARTIFACT_CURSOR.fullmatch(q['before']):
                raise ValueError()
            limit = limit_of(q['limit']) if 'limit' in q else 50
            if limit > 100:
                raise ValueError()
        except ValueError:
            return error('invalid_request', 422)
        return await invoke(request, 'artifacts', before=q.get('before'), limit=limit)

    @routes.get('/settings')
    async def settings(request: Request):
        if request.query_params:
            return error('invalid_request', 422)
        return await invoke(request, 'settings')

    @routes.get('/world-model')
    async def world_model(request: Request):
        # Enterprise world model W1 (0230): the tenant's published model metadata; no instance content, no edit path.
        if request.query_params:
            return error('invalid_request', 422)
        return await invoke(request, 'world_model')

    @routes.get('/metrics')
    async def metrics(request: Request):
        # NX-030: the SQL metrics snapshot of this tenant and world (codes, counts, ages; no raw text).
        if request.query_params:
            return error('invalid_request', 422)
        return await invoke(request, 'metrics')

    @routes.get('/alerts')
    async def alerts(request: Request):
        try:
            q = query(request, ('limit',))
            limit = limit_of(q['limit']) if 'limit' in q else 100
        except ValueError:
            return error('invalid_request', 422)
        return await invoke(request, 'alerts', limit=limit)

    @routes.get('/human-actions')
    async def human_actions(request: Request):
        # NX-030 §4: who did which governed human Action when, plus ADR-025 reads; owner only.
        try:
            q = query(request, ('limit',))
            limit = limit_of(q['limit']) if 'limit' in q else 100
        except ValueError:
            return error('invalid_request', 422)
        return await invoke(request, 'human_actions', limit=limit)

    @routes.get('/effect-determination/{intent_id}')
    async def effect_determination(intent_id: str, request: Request):
        # 0210: the owner's read-only precheck before confirming an unknown effect did not happen (codes and times; owner only).
        if request.query_params or not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', intent_id):
            return error('invalid_request', 422)
        return await invoke(request, 'effect_determination', intent_id=intent_id)

    @routes.get('/erasure-precheck/{consumer_id}')
    async def erasure_precheck(consumer_id: str, request: Request):
        # 0220: the owner's read-only precheck before and after erasing a synthetic Consumer (persona, erasure state, counts).
        if request.query_params or not _ID.fullmatch(consumer_id):
            return error('invalid_request', 422)
        return await invoke(request, 'erasure_precheck', consumer_id=consumer_id)

    @routes.get('/operator-personas')
    async def operator_personas(request: Request):
        # 0227: the owner's operator persona overview; ?consumer_id= adds that Consumer's desired and settled persona.
        try:
            q = query(request, ('consumer_id',))
            if 'consumer_id' in q and not _ID.fullmatch(q['consumer_id']):
                raise ValueError()
        except ValueError:
            return error('invalid_request', 422)
        return await invoke(request, 'operator_personas', consumer_id=q.get('consumer_id'))

    @routes.get('/consumers/{consumer_id}/contact-preferences')
    async def contact_preferences(consumer_id: str, request: Request):
        # 0228: the owner's view of a consumer's communication preferences and the owner's dismissals.
        if request.query_params or not _ID.fullmatch(consumer_id):
            return error('invalid_request', 422)
        return await invoke(request, 'contact_preferences', consumer_id=consumer_id)

    @routes.get('/outreach-candidates')
    async def outreach_candidates(request: Request):
        # 0231: the owner's view of proactive outreach candidates and their dispatch dry run (ids, purposes, gate results).
        if request.query_params:
            return error('invalid_request', 422)
        return await invoke(request, 'outreach_candidates')

    @routes.get('/outreach-controls')
    async def outreach_controls(request: Request):
        # 0232: the owner's view of the tenant outreach controls (settings, revision, last changes without reasons).
        if request.query_params:
            return error('invalid_request', 422)
        return await invoke(request, 'outreach_controls')

    @routes.get('/outreach-purposes')
    async def outreach_purposes(request: Request):
        # Outreach slice 3: the owner's view of the purpose list and the tenant's enabled purposes.
        if request.query_params:
            return error('invalid_request', 422)
        return await invoke(request, 'outreach_purposes')

    @routes.get('/approvals')
    async def approvals(request: Request):
        # NX-056 A4: the unified approvals of the caller (every workbench member; the rows are their own).
        if request.query_params:
            return error('invalid_request', 422)
        return await invoke(request, 'approvals')

    @routes.get('/members')
    async def members(request: Request):
        # NX-058 workbench UI: the owner's members view (members, unexpired invitations, the last 90 days of reset codes).
        if request.query_params:
            return error('invalid_request', 422)
        return await invoke(request, 'members')

    @routes.get('/experiments')
    async def experiments(request: Request):
        # Experiment space slice 1 (0370): the owner's experiments view, read only.
        if request.query_params:
            return error('invalid_request', 422)
        return await invoke(request, 'experiments')

    @routes.get('/outreach-approvals')
    async def outreach_approvals(request: Request):
        # Outreach slice 6: the owner's pending approvals (ids, purpose, times, requester).
        if request.query_params:
            return error('invalid_request', 422)
        return await invoke(request, 'outreach_approvals')

    @routes.get('/data-health')
    async def data_health(request: Request):
        # D-G5: the health of NexLoop's own data flows (counts, ratios, ages, codes; owner only).
        if request.query_params:
            return error('invalid_request', 422)
        return await invoke(request, 'data_health')

    @routes.get('/outreach-metrics')
    async def outreach_metrics(request: Request):
        # Outreach slice 8: the owner's outreach metrics.
        if request.query_params:
            return error('invalid_request', 422)
        return await invoke(request, 'outreach_metrics')

    @routes.get('/outreach-evidence')
    async def outreach_evidence(request: Request):
        # Outreach slice 8: one send's evidence chain (?intent_id=) or a list (?consumer_id=&synthetic_only=true&limit=).
        params = request.query_params
        query = {}
        if set(params) - {'intent_id', 'consumer_id', 'synthetic_only', 'limit'} or any(len(params.getlist(k)) != 1 for k in params):
            return error('invalid_request', 422)
        if 'intent_id' in params:
            if set(params) != {'intent_id'} or not re.fullmatch(r'outreach:[0-9a-f]{64}', params['intent_id']):
                return error('invalid_request', 422)
            query['intent_id'] = params['intent_id']
        if 'consumer_id' in params:
            if not re.fullmatch(r'[a-f0-9]{64}', params['consumer_id']):
                return error('invalid_request', 422)
            query['consumer_id'] = params['consumer_id']
        if 'synthetic_only' in params:
            if params['synthetic_only'] not in ('true', 'false'):
                return error('invalid_request', 422)
            query['synthetic_only'] = params['synthetic_only'] == 'true'
        if 'limit' in params:
            if not re.fullmatch(r'[1-9][0-9]{0,2}', params['limit']) or int(params['limit']) > 200:
                return error('invalid_request', 422)
            query['limit'] = int(params['limit'])
        return await invoke(request, 'outreach_evidence', **query)

    @routes.get('/review')
    async def review_queue(request: Request):
        # The reviewer's evidence page: pending review items (ontology.schema.review), newest first.
        try:
            q = query(request, ('limit',))
            limit = limit_of(q['limit']) if 'limit' in q else 50
        except ValueError:
            return error('invalid_request', 422)
        return await invoke(request, 'review_queue', limit=limit)

    @routes.get('/review/{candidate_id}')
    async def review_evidence(candidate_id: str, request: Request):
        if not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', candidate_id) or request.query_params:
            return error('invalid_request', 422)
        return await invoke(request, 'review_evidence', candidate_id=candidate_id)

    @routes.get('/audit')
    async def audit(request: Request):
        # ADR-025 §2.3: the member-read audit, owner only (SQL refuses everyone else with 403).
        try:
            q = query(request, ('limit', 'before'))
            if 'before' in q and not re.fullmatch(r'[1-9][0-9]{0,18}', q['before']):
                raise ValueError()
            limit = limit_of(q['limit']) if 'limit' in q else 100
        except ValueError:
            return error('invalid_request', 422)
        return await invoke(request, 'audit', limit=limit, before=q.get('before'))

    return routes
