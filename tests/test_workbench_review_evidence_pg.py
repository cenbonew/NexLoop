"""NX-028 known limitation closed: the reviewer's evidence page in the workbench (read-only).

Real PG and real workbench login cookies. A reviewer member (role grant ontology.schema.review, compiled from the role
manifest) sees the pending review items and the evidence Message bodies through the ADR-025 reviewer derivation, each read
audited with purpose review_evidence; once the item is decided the page says the review ended and reads nothing. An owner
without the review grant and the customer get no queue. Synthetic data.
"""
import hashlib
from datetime import UTC, datetime

from psycopg.types.json import Jsonb

from nexloop_eios.workbench_roles import compile_members
from test_review_http import put_candidate
from test_workbench_member_read_pg import BODY, audit_rows
from test_workbench_read_pg import (ROLES, TENANT, browser_business, configure, conversations, identity, members_file, published_action, uow,
    workbench, workbench_login, write_facts)


def pending_item(w):
    admin, f = w['admin'], w['f']
    claim = hashlib.sha256(b'nx028-review-page-claim').hexdigest()
    admin.execute("""insert into ontology.nexloop_claims(tenant_id,world,claim_id,conversation_id,consumer_id,first_input_digest,topic_key,subject_kind,subject_ref,subject_text,
        predicate,value,speaker,polarity,modality,condition_text,time_expression,valid_time,source_message_id,source_sequence,span_start,span_end,source_content_hash,quote,
        extractor_version,confidence,epistemic_kind,resolution_state,derived_from,correlation_key,guard_flags)
        values(%s,'real',%s,%s,%s,%s,%s,'consumer',%s,'','联系偏好',%s,'consumer','affirmed','asserted','','',%s,%s,1,0,6,%s,'请不要再给我','nx019-extractor/1',0.9,
        'preference','awaiting_definition','[]',%s,'[]')""",
        (TENANT, claim, w['conversation']['id'], f['consumer'], '0' * 64, '1' * 64, f['consumer'], Jsonb({'type': 'string', 'value': '不要短信'}),
         Jsonb({'kind': 'none', 'status': 'absent', 'start': None, 'end': None, 'anchor': datetime.now(UTC).isoformat(), 'timezone': 'Asia/Shanghai', 'expression': ''}),
         w['refusal']['id'], hashlib.sha256(BODY.encode()).hexdigest(), hashlib.sha256(b'corr-page').hexdigest()))
    return put_candidate(admin, 'real', '联系偏好', [claim])


def test_reviewer_page_shows_pending_evidence_and_ends_with_the_review(workbench):
    w = workbench
    admin = w['admin']
    members = members_file((w['owner'], 'owner'), (w['outsider'], 'reviewer'))
    configure(w, (w['owner'], 'owner'), (w['outsider'], 'reviewer'))
    write_facts(admin, compile_members(ROLES, members))
    candidate = pending_item(w)
    with w['client']() as client:
        assert workbench_login(client, w['outsider']).status_code == 200
        queue = client.get('/api/v1/workbench/review')
        assert queue.status_code == 200 and [(x['candidate_id'], x['display_name'], x['evidence_count']) for x in queue.json()['items']] == [(candidate, '联系偏好', 1)]
        page = client.get('/api/v1/workbench/review/' + candidate)
        assert page.status_code == 200
        assert page.json()['status'] == 'pending_review' and page.json()['evidence'][0]['content'] == {'status': 'ok', 'body': BODY}
        assert audit_rows(admin)[-1][1:] == ('reviewer', 'message', 'eios:object:Message/' + w['refusal']['id'], 'review_evidence')
        # The reviewer's role carries no workbench read: the other workbench pages stay closed.
        assert client.get('/api/v1/workbench/contact').status_code == 403
        assert client.get('/api/v1/workbench/conversations/' + w['conversation']['id']).status_code == 403
        assert client.get('/api/v1/workbench/review/not-a-uuid').status_code == 422
        # The review ends: nothing readable any more, and no new read is audited.
        with admin.transaction():
            admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT,))
            admin.execute("update ontology.nexloop_candidate_definitions set status='rejected' where candidate_id=%s", (candidate,))
        before = len(audit_rows(admin))
        ended = client.get('/api/v1/workbench/review/' + candidate)
        assert ended.status_code == 200 and ended.json() == {'candidate_id': candidate, 'status': 'ended', 'evidence': []} and BODY not in ended.text
        assert len(audit_rows(admin)) == before
        assert client.get('/api/v1/workbench/review').json()['items'] == []


def test_review_queue_needs_the_review_grant(workbench):
    w = workbench
    admin = w['admin']
    configure(w, (w['owner'], 'owner'), (w['outsider'], 'reviewer'))
    write_facts(admin, compile_members(ROLES, members_file((w['owner'], 'owner'))))  # the owner's role has no ontology.schema.review
    candidate = pending_item(w)
    with w['client']() as client:
        assert client.get('/api/v1/workbench/review').status_code == 401
        assert workbench_login(client, w['owner']).status_code == 200
        assert client.get('/api/v1/workbench/review').status_code == 403
        denied = client.get('/api/v1/workbench/review/' + candidate)
        assert denied.status_code == 403 and BODY not in denied.text
    with w['client']() as client:
        assert workbench_login(client, w['customer']).status_code == 200
        assert client.get('/api/v1/workbench/review').status_code == 403
    assert audit_rows(admin) == []
