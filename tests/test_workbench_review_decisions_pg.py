"""Review decisions move into the workbench: the reviewer decides on the knowledge review page in the workbench login realm.

Real PG, real workbench login cookies and the production create_app. The same governed human review Action (NX-044) as the
browser route, decided by the member's current ontology.schema.review grant, which the role manifest gives the reviewer only.
Owner and operator (no review grant) get 403 and change nothing; a customer session from the customer realm does not reach the
workbench routes. The browser review routes stay (test_review_http). Synthetic data.
"""
from nexloop_eios.workbench_roles import compile_members
from test_browser_http import ORIGIN, login
from test_workbench_read_pg import ROLES, members_file, workbench, workbench_login, write_facts, configure  # noqa: F401
from test_workbench_read_pg import TENANT, browser_business, conversations, identity, published_action, uow  # noqa: F401
from test_workbench_review_evidence_pg import pending_item

BASE = '/api/v1/workbench/review-decisions'


def csrf_of(client):
    return client.post('/api/v1/workbench/auth/csrf', headers={'Origin': ORIGIN}).json()['csrf_token']


def test_reviewer_decides_in_the_workbench_and_others_get_403(workbench):
    w = workbench
    admin = w['admin']
    members = ((w['owner'], 'owner'), (w['operator'], 'operator'), (w['outsider'], 'reviewer'))
    configure(w, *members)
    write_facts(admin, compile_members(ROLES, members_file(*members)))
    candidate = pending_item(w)
    path = f'{BASE}/candidates/{candidate}/decisions'
    body = {'decision': 'reject', 'expected_revision': 1, 'rationale': '不是业务概念'}
    # Owner and operator hold no ontology.schema.review: no detail, no decision, nothing written.
    for member in (w['owner'], w['operator']):
        with w['client']() as client:
            assert workbench_login(client, member).status_code == 200
            assert client.get(f'{BASE}/candidates/{candidate}').status_code == 403
            headers = {'Origin': ORIGIN, 'X-CSRF-Token': csrf_of(client), 'Idempotency-Key': 'synthetic-wb-denied-0001'}
            denied = client.post(path, headers=headers, json=body)
            assert denied.status_code == 403 and denied.json()['code'] == 'forbidden'
    assert admin.execute('select count(*) from ontology.nexloop_review_decisions').fetchone()[0] == 0
    # The customer realm's cookie is not a workbench session.
    with w['client']() as client:
        assert login(client, w['f']['base']['identity']).status_code == 200
        assert client.get(f'{BASE}/candidates/{candidate}').status_code == 401
    # Even logged into the workbench realm, a customer is never a member and holds no review grant.
    with w['client']() as client:
        assert workbench_login(client, w['customer']).status_code == 200
        assert client.get(f'{BASE}/candidates/{candidate}').status_code == 403
        headers = {'Origin': ORIGIN, 'X-CSRF-Token': csrf_of(client), 'Idempotency-Key': 'synthetic-wb-customer-0001'}
        assert client.post(path, headers=headers, json=body).status_code == 403
    # The reviewer: detail with the decisions enabled, then a real reject with CSRF, idempotency and CAS.
    with w['client']() as client:
        assert workbench_login(client, w['outsider']).status_code == 200
        detail = client.get(f'{BASE}/candidates/{candidate}')
        assert detail.status_code == 200 and detail.json()['revision'] == 1 and detail.json()['decisions']['enabled'] is True
        headers = {'Origin': ORIGIN, 'X-CSRF-Token': csrf_of(client), 'Idempotency-Key': 'synthetic-wb-reject-0001'}
        assert client.post(path, headers={**headers, 'X-CSRF-Token': 'wrong'}, json=body).status_code == 401
        assert client.post(path, headers=headers, json={**body, 'decision': 'publish'}).status_code == 422
        decided = client.post(path, headers=headers, json=body)
        assert decided.status_code == 200, decided.text
        assert decided.json()['outcome'] == 'rejected' and decided.json()['record']['reviewer_ref'] == 'human:' + w['outsider']['principal_id']
        assert client.post(path, headers=headers, json=body).json()['replay'] is True
        assert client.post(path, headers={**headers, 'Idempotency-Key': 'synthetic-wb-reject-0002'}, json=body).status_code == 409
        # The evidence page now says the review ended.
        assert client.get('/api/v1/workbench/review/' + candidate).json()['status'] == 'ended'
    assert admin.execute('select status from ontology.nexloop_candidate_definitions where candidate_id=%s', (candidate,)).fetchone()[0] == 'rejected'
    assert admin.execute('select count(*) from ontology.nexloop_review_decisions').fetchone()[0] == 1
