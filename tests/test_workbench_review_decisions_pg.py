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


def test_reviewer_decides_in_the_workbench_and_the_operator_gets_403(workbench):
    w = workbench
    admin = w['admin']
    members = ((w['owner'], 'owner'), (w['operator'], 'operator'), (w['outsider'], 'reviewer'))
    configure(w, *members)
    write_facts(admin, compile_members(ROLES, members_file(*members)))
    candidate = pending_item(w)
    path = f'{BASE}/candidates/{candidate}/decisions'
    body = {'decision': 'reject', 'expected_revision': 1, 'rationale': '不是业务概念'}
    # The operator holds no ontology.schema.review: no detail, no decision, nothing written. (The owner holds it since NX-028 D2
    # revision 2, owner 2026-10-20: test_the_owner_decides_through_the_same_governed_action.)
    for member in (w['operator'],):
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


def test_the_owner_decides_through_the_same_governed_action(workbench):
    """NX-028 D2 revision 2 (owner 2026-10-20, "reviewer: option A"): the tenant's only member is the owner, so the owner role carries
    ontology.schema.review (roles v18). The decision is the same governed Action: CSRF, idempotency, expected_revision, a required
    rationale, and the audit records the owner's own principal; the operator still cannot decide."""
    w = workbench
    admin = w['admin']
    members = ((w['owner'], 'owner'), (w['operator'], 'operator'))
    configure(w, *members)
    write_facts(admin, compile_members(ROLES, members_file(*members)))
    candidate = pending_item(w)
    path = f'{BASE}/candidates/{candidate}/decisions'
    body = {'decision': 'reject', 'expected_revision': 1, 'rationale': '不是业务概念'}
    with w['client']() as client:
        assert workbench_login(client, w['operator']).status_code == 200
        headers = {'Origin': ORIGIN, 'X-CSRF-Token': csrf_of(client), 'Idempotency-Key': 'synthetic-wb-op-denied-0001'}
        assert client.post(path, headers=headers, json=body).status_code == 403
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        detail = client.get(f'{BASE}/candidates/{candidate}')
        assert detail.status_code == 200 and detail.json()['decisions']['enabled'] is True
        headers = {'Origin': ORIGIN, 'X-CSRF-Token': csrf_of(client), 'Idempotency-Key': 'synthetic-wb-owner-reject-0001'}
        assert client.post(path, headers=headers, json={**body, 'rationale': ''}).status_code == 422
        stale = client.post(path, headers={**headers, 'Idempotency-Key': 'synthetic-wb-owner-stale-0001'}, json={**body, 'expected_revision': 9})
        assert stale.status_code == 409
        decided = client.post(path, headers=headers, json=body)
        assert decided.status_code == 200, decided.text
        assert decided.json()['outcome'] == 'rejected' and decided.json()['record']['reviewer_ref'] == 'human:' + w['owner']['principal_id']
        assert client.post(path, headers=headers, json=body).json()['replay'] is True
    row = admin.execute('select reviewer_principal,rationale from ontology.nexloop_review_decisions').fetchall()
    assert row == [(w['owner']['principal_id'], '不是业务概念')]
