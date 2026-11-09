"""NX-062 M5c: the machine write routes over the real HTTP application and real PostgreSQL - POST /machine/v1/prepare, POST
/machine/v1/commit, GET /machine/v1/intents/{intent_id} (machine_key realm, admission first, parameters in the body only, the API signs
the commit, codes and ids only). Every answer is validated against its published contract. Keys are generated at run time."""
import dataclasses
import json

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker

from nexloop_eios.http_api import create_app
from support.tenants import TENANT_A as TENANT
from test_machine_lifecycle_pg import desk, owner  # noqa: F401
from test_machine_reads_pg import commitment, hexid, put
from test_machine_realm_pg import ROOT, bearer
from test_object_notes_pg import CAP, limits, notes  # noqa: F401
from test_workbench_actions_http import act
from test_workbench_read_pg import ORIGIN, browser_business, conversations, identity, published_action, uow, workbench  # noqa: F401


def check(response, name):
    name = name if response.status_code in (200, 202) else 'machine-error'
    Draft202012Validator(json.loads((ROOT / f'packages/contracts/{name}.schema.json').read_text()), format_checker=FormatChecker()).validate(response.json())
    assert response.headers['cache-control'] == 'no-store'
    return response


@pytest.fixture
def http(notes):
    w = notes
    base = w['config']
    config = dataclasses.replace(base, machine_key_peppers_file=w['peppers_file'],
                                 workbench=dataclasses.replace(base.workbench, machine_key_peppers_file=w['peppers_file']))
    return {**w, 'app': lambda: TestClient(create_app(config), base_url=ORIGIN)}


def body(w, intent, text='一条备注', **extra):
    return {'capability': CAP, 'intent_id': intent, 'params': {'type_name': 'Commitment', 'object_id': w['object'], 'text': text}, **extra}


def test_prepare_commit_query_over_http(http):
    w = http
    limits(w['admin'])
    with w['app']() as c:
        h = bearer(w['key'])
        p = check(c.post('/machine/v1/prepare', headers=h, json=body(w, 'http-intent-1')), 'machine-prepare')
        assert p.status_code == 200 and p.json()['normalized']['text'] == '一条备注'
        done = check(c.post('/machine/v1/commit', headers=h, json=body(w, 'http-intent-1', prepare_digest=p.json()['prepare_digest'])), 'machine-intent')
        assert done.status_code == 200 and done.json()['phase'] == 'committed'
        again = check(c.post('/machine/v1/commit', headers=h, json=body(w, 'http-intent-1', prepare_digest=p.json()['prepare_digest'])), 'machine-intent')
        assert again.json() == {**done.json(), 'replayed': True}
        q = check(c.get('/machine/v1/intents/http-intent-1', headers=h), 'machine-intent')
        assert q.json()['phase'] == 'committed' and q.json()['outcome_id'] == done.json()['outcome_id']
        # Errors: codes only.
        cases = [
            (c.post('/machine/v1/prepare', headers=h, json=body(w, 'http-intent-1', '另一条')), 409, 'intent_committed'),
            (c.post('/machine/v1/commit', headers=h, json=body(w, 'http-intent-1', '另一条', prepare_digest=p.json()['prepare_digest'])), 409, 'digest_mismatch'),
            (c.post('/machine/v1/prepare', headers=h, json={**body(w, 'http-intent-2'), 'params': {**body(w, 'x')['params'], 'tenant_id': TENANT}}), 422, 'invalid_request'),
            (c.post('/machine/v1/prepare', headers=h, json={**body(w, 'http-intent-2'), 'extra': 1}), 422, 'invalid_request'),
            (c.post('/machine/v1/prepare', headers=h, content=b'{not json'), 422, 'invalid_request'),
            (c.post('/machine/v1/prepare?key=x', headers=h, json=body(w, 'http-intent-2')), 400, 'query_not_allowed'),
            (c.post('/machine/v1/prepare', json=body(w, 'http-intent-2')), 401, 'unauthenticated'),
            (c.post('/machine/v1/prepare', headers=h, json={**body(w, 'http-intent-2'), 'params': {**body(w, 'x')['params'], 'object_id': hexid()}}), 404, 'not_found'),
            (c.post('/machine/v1/prepare', headers=h, json={**body(w, 'http-intent-2'), 'capability': 'object.get'}), 403, 'not_permitted'),
            (c.get('/machine/v1/intents/unknown-intent', headers=h), 404, 'not_found'),
            (c.get('/machine/v1/intents/x', headers=h), 422, 'invalid_request'),
            (c.post('/machine/v1/commit', headers=h, json=body(w, 'http-intent-2')), 422, 'invalid_request'),  # no prepare_digest
        ]
        for response, status, code in cases:
            check(response, 'machine-error')
            assert (response.status_code, response.json()['code']) == (status, code), response.text
        assert '另一条' not in json.dumps([r.json() for r, _, _ in cases], ensure_ascii=False)
        # Expired prepare: 410; the cap: 429 with Retry-After.
        p3 = c.post('/machine/v1/prepare', headers=h, json=body(w, 'http-intent-3')).json()
        w['admin'].execute("select set_config('eios.tenant_id',%s,false)", (TENANT,))
        w['admin'].execute("update runtime.nexloop_machine_intents set expires_at=clock_timestamp()-interval '1 second' where intent_id='http-intent-3'")
        expired = check(c.post('/machine/v1/commit', headers=h, json=body(w, 'http-intent-3', prepare_digest=p3['prepare_digest'])), 'machine-error')
        assert (expired.status_code, expired.json()['code']) == (410, 'prepare_expired')
        limits(w['admin'], per_client=1, per_object=1)
        p4 = c.post('/machine/v1/prepare', headers=h, json=body(w, 'http-intent-4')).json()
        capped = check(c.post('/machine/v1/commit', headers=h, json=body(w, 'http-intent-4', prepare_digest=p4['prepare_digest'])), 'machine-error')
        assert (capped.status_code, capped.json()['code']) == (429, 'note_limit') and capped.headers['retry-after'] == '3600'
