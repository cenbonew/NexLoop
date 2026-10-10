"""ADR-025 §3: workbench members read message bodies and Consumer properties by role, every read audited.

Real PostgreSQL, real workbench login cookies, synthetic data. Reuses the slice 1 fixture (test_workbench_read_pg): the
`conversations` Human is the customer; owner/operator/outsider are separate browser Humans of the workbench application.
The Consumer type version with property groups and the owner restriction fact are configuration stand-ins written by admin.
"""
import hashlib
import uuid
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg.types.json import Jsonb

from nexloop_eios.browser_authorization import authenticate_browser_business
from nexloop_eios.workbench_reads import WorkbenchReader
from test_workbench_read_pg import (ROLES, TENANT, WORKBENCH_APP, browser_business, configure, conversations, identity,
    provision, published_action, revoke_member, uow, workbench, workbench_login, write_facts)

BODY = '请不要再给我发短信了'


def staff_session(w, member):
    """The member's live workbench Human session (the same inspection the HTTP routes do)."""
    from eios.identity.ports import TrustedIdentityOperator
    from eios.identity.sessions import BrowserSessionService
    from nexloop_eios.browser_identity import open_browser_identity
    from nexloop_eios.browser_sessions import PostgresBrowserSessionUnitOfWork
    client = w['client']().__enter__()
    assert workbench_login(client, member).status_code == 200
    token = client.cookies.get('__Host-nexloop_workbench')
    with open_browser_identity(w['f']['base']['identity'][0]) as pool:
        store = PostgresBrowserSessionUnitOfWork(pool, tenant_id=TENANT, application_id=WORKBENCH_APP)
        op = TrustedIdentityOperator(operator_principal_id='nexloop_identity', request_id=str(uuid.uuid4()), trace_id=str(uuid.uuid4()))
        inspected = BrowserSessionService(store, store, application_id=WORKBENCH_APP, operator=op).inspect(token)
    return client, inspected


def reader_for(w, inspected):
    r = w['f']['reader']
    return WorkbenchReader(r.pool, authenticate_browser_business(r.pool, inspected, world='real'), r.signer)


def audit_rows(admin):
    return admin.execute('select principal_id,role,object_kind,target_resource,read_purpose from runtime.nexloop_workbench_read_audit order by audit_id').fetchall()


def copy_object(admin, object_id, new_id, *, tenant=TENANT, world='real'):
    cols = admin.execute("select array_agg(column_name::text order by ordinal_position) from information_schema.columns where table_schema='ontology' and table_name='objects'").fetchone()[0]
    picked = ','.join('%s' if c in ('object_id', 'tenant_id', 'world') else c for c in cols)
    params = [{'object_id': new_id, 'tenant_id': tenant, 'world': world}[c] for c in cols if c in ('object_id', 'tenant_id', 'world')]
    admin.execute(f'insert into ontology.objects({",".join(cols)}) select {picked} from ontology.objects where object_id=%s', (*params, object_id))


def consumer_with_groups(w):
    """Consumer type v99 with two property groups and values (configuration stand-in)."""
    admin, consumer = w['admin'], w['f']['consumer']
    definition = {'type_name': 'Consumer', 'version': 99, 'properties': [{'property_name': 'favorite_sport', 'value_type': 'string'},
        {'property_name': 'income_band', 'value_type': 'string'}],
        'property_groups': [{'group_name': 'preference', 'property_names': ['favorite_sport']}, {'group_name': 'spending_power', 'property_names': ['income_band']}]}
    admin.execute('insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,%s,99,%s)', (TENANT, 'Consumer', Jsonb(definition)))
    admin.execute("update ontology.objects set schema_version=99,properties=%s where tenant_id=%s and type_name='Consumer' and object_id=%s",
        (Jsonb({'favorite_sport': '网球', 'income_band': 'high'}), TENANT, consumer))
    return consumer


def test_owner_and_operator_read_bodies_and_properties_with_one_audit_row_per_read(workbench):
    """AT-003 positive case restored, through HTTP; each read leaves its audit row with the page as purpose."""
    w = workbench
    provision(w)
    consumer = consumer_with_groups(w)
    for member, role in ((w['owner'], 'owner'), (w['operator'], 'operator')):
        with w['client']() as client:
            assert workbench_login(client, member).status_code == 200
            before = len(audit_rows(w['admin']))
            message = client.get('/api/v1/workbench/conversations/' + w['conversation']['id']).json()['messages'][0]
            assert message['content'] == {'status': 'ok', 'actor': w['refusal']['actor'], 'body': BODY}
            restriction = client.get('/api/v1/workbench/contact').json()['restrictions'][0]
            assert restriction['matched_text_status'] == 'ok' and restriction['matched_text'] in BODY
            props = client.get('/api/v1/workbench/consumers/' + consumer).json()['properties']
            assert props == {'status': 'ok', 'values': {'favorite_sport': '网球', 'income_band': 'high'}, 'withheld': []}
            rows = audit_rows(w['admin'])[before:]
            # One row per object and request: the conversation read, the contact read (matched text + hit, same message) and the Consumer.
            assert [(r[0], r[1], r[2], r[4]) for r in rows] == [(member['principal_id'], role, 'message', 'conversation'), (member['principal_id'], role, 'message', 'contact'),
                (member['principal_id'], role, 'consumer', 'consumer')]
            assert rows[0][3] == 'eios:object:Message/' + w['refusal']['id'] and rows[2][3] == 'eios:object:Consumer/' + consumer


def test_owner_restricted_groups_are_never_read(workbench):
    w = workbench
    provision(w)
    consumer = consumer_with_groups(w)
    w['admin'].execute('insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) values(%s,%s,%s,%s)',
        (TENANT, 'property_group_restriction', ['Consumer'], Jsonb({'tenant_id': TENANT, 'type_name': 'Consumer', 'restricted_groups': ['spending_power'], 'decision': 'owner 2026-10-10 (synthetic)'})))
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        props = client.get('/api/v1/workbench/consumers/' + consumer)
        assert props.json()['properties'] == {'status': 'ok', 'values': {'favorite_sport': '网球'}, 'withheld': ['income_band']} and 'high' not in props.text
    _, inspected = staff_session(w, w['owner'])
    reader = reader_for(w, inspected)
    # Asking for the restricted field directly is refused as a whole by SQL.
    assert reader.read_object('Consumer', consumer, ('income_band',), 'consumer') is None
    assert reader.read_object('Consumer', consumer, ('favorite_sport',), 'consumer') == {'favorite_sport': '网球'}


def test_only_this_tenant_and_world(workbench):
    w = workbench
    provision(w)
    admin, message = w['admin'], w['refusal']['id']
    copy_object(admin, message, 'e' * 64, world='simulation')
    copy_object(admin, message, 'f' * 64, tenant='synthetic-b')
    _, inspected = staff_session(w, w['owner'])
    reader = reader_for(w, inspected)
    assert reader.message_fields(message, ('body',))['body'] == BODY
    assert reader.message_fields('e' * 64, ('body',)) is None
    assert reader.message_fields('f' * 64, ('body',)) is None
    # A forged claim for another tenant is refused before any read.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with reader.pool.connection() as db:
            claims = reader._derived('object', 'Message/' + message, 'conversation') | {'tenant_id': 'synthetic-b'}
            db.execute('select authz.nexloop_assert_read_authority(%s,%s,%s)', (reader.session.token_digest, 'real', Jsonb(claims)))


def test_reviewer_reads_only_pending_review_evidence_until_the_review_ends(workbench):
    w = workbench
    admin, f = w['admin'], w['f']
    other = f['port'].accept_message(conversation_id=w['conversation']['id'], idempotency_key='nx028-reviewer-other', body='别的消息')['message']['id']
    configure(w, (w['owner'], 'owner'), (w['outsider'], 'reviewer'))
    from nexloop_eios.workbench_roles import compile_members
    from test_workbench_read_pg import members_file
    write_facts(admin, [x for x in compile_members(ROLES, members_file((w['owner'], 'owner'))) if x['kind'] == 'application'])  # the workbench application is published
    claim = hashlib.sha256(b'nx028-reviewer-claim').hexdigest()
    admin.execute("""insert into ontology.nexloop_claims(tenant_id,world,claim_id,conversation_id,consumer_id,first_input_digest,topic_key,subject_kind,subject_ref,subject_text,
        predicate,value,speaker,polarity,modality,condition_text,time_expression,valid_time,source_message_id,source_sequence,span_start,span_end,source_content_hash,quote,
        extractor_version,confidence,epistemic_kind,resolution_state,derived_from,correlation_key,guard_flags)
        values(%s,'real',%s,%s,%s,%s,%s,'consumer',%s,'','联系偏好',%s,'consumer','affirmed','asserted','','',%s,%s,1,0,6,%s,'请不要再给我','nx019-extractor/1',0.9,
        'preference','awaiting_definition','[]',%s,'[]')""",
        (TENANT, claim, w['conversation']['id'], f['consumer'], '0' * 64, '1' * 64, f['consumer'], Jsonb({'type': 'string', 'value': '不要短信'}),
         Jsonb({'kind': 'none', 'status': 'absent', 'start': None, 'end': None, 'anchor': datetime.now(UTC).isoformat(), 'timezone': 'Asia/Shanghai', 'expression': ''}),
         w['refusal']['id'], hashlib.sha256(BODY.encode()).hexdigest(), hashlib.sha256(b'corr').hexdigest()))
    from test_review_http import put_candidate
    candidate = put_candidate(admin, 'real', '联系偏好', [claim])
    _, inspected = staff_session(w, w['outsider'])
    reader = reader_for(w, inspected)
    assert reader.message_fields(w['refusal']['id'], ('body',), 'review_evidence')['body'] == BODY
    assert reader.message_fields(other, ('body',), 'review_evidence') is None
    assert reader.read_object('Consumer', f['consumer'], (), 'review_evidence') is None
    assert audit_rows(admin)[-1][1:] == ('reviewer', 'message', 'eios:object:Message/' + w['refusal']['id'], 'review_evidence')
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT,))
        admin.execute("update ontology.nexloop_candidate_definitions set status='rejected' where candidate_id=%s", (candidate,))
    assert reader.message_fields(w['refusal']['id'], ('body',), 'review_evidence') is None


def test_membership_role_and_session_revocation_end_reads_on_the_next_request(workbench):
    w = workbench
    provision(w)
    admin = w['admin']
    client, inspected = staff_session(w, w['operator'])
    reader = reader_for(w, inspected)
    assert reader.message_fields(w['refusal']['id'], ('body',))['body'] == BODY
    # Role changed to reviewer (grants revoked first, as 0140 requires): no longer derived.
    write_facts(admin, revoke_member(ROLES, TENANT, w['operator']['principal_id']))
    configure(w, (w['owner'], 'owner'), (w['operator'], 'reviewer'))
    assert reader.message_fields(w['refusal']['id'], ('body',)) is None
    # Membership removed.
    configure(w, (w['owner'], 'owner'))
    assert reader.message_fields(w['refusal']['id'], ('body',)) is None
    # Session revoked: the owner's reader stops at once.
    owner_client, owner_inspected = staff_session(w, w['owner'])
    owner = reader_for(w, owner_inspected)
    assert owner.message_fields(w['refusal']['id'], ('body',))['body'] == BODY
    csrf = owner_client.post('/api/v1/workbench/auth/csrf', headers={'Origin': 'https://synthetic.example'}).json()['csrf_token']
    assert owner_client.post('/api/v1/workbench/auth/logout', headers={'Origin': 'https://synthetic.example', 'X-CSRF-Token': csrf}).status_code == 200
    assert owner.message_fields(w['refusal']['id'], ('body',)) is None
    client.__exit__(None, None, None)
    owner_client.__exit__(None, None, None)


def test_audit_failure_fails_the_read_and_only_the_owner_reads_the_audit(workbench):
    w = workbench
    provision(w)
    admin = w['admin']
    admin.execute("create function pg_temp.nx028_audit_down() returns trigger language plpgsql as $$begin raise exception 'synthetic audit outage';end$$")
    admin.execute('create trigger nx028_audit_down before insert on runtime.nexloop_workbench_read_audit for each row execute function pg_temp.nx028_audit_down()')
    try:
        with w['client']() as client:
            assert workbench_login(client, w['owner']).status_code == 200
            down = client.get('/api/v1/workbench/conversations/' + w['conversation']['id'])
            assert down.status_code == 503 and BODY not in down.text
    finally:
        admin.execute('drop trigger nx028_audit_down on runtime.nexloop_workbench_read_audit')
    assert audit_rows(admin) == []
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        assert client.get('/api/v1/workbench/conversations/' + w['conversation']['id']).status_code == 200
        audit = client.get('/api/v1/workbench/audit')
        assert audit.status_code == 200 and [r['read_purpose'] for r in audit.json()['items']] == ['conversation']
        assert client.get('/api/v1/workbench/audit?limit=0').status_code == 422
    with w['client']() as client:
        assert workbench_login(client, w['operator']).status_code == 200
        assert client.get('/api/v1/workbench/conversations/' + w['conversation']['id']).status_code == 200
        denied = client.get('/api/v1/workbench/audit')
        assert denied.status_code == 403 and w['owner']['principal_id'] not in denied.text
    from psycopg.conninfo import make_conninfo
    with psycopg.connect(make_conninfo(w['f']['pg'], user='nexloop_api')) as db, pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute('select * from runtime.nexloop_workbench_read_audit')
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        admin.execute('delete from runtime.nexloop_workbench_read_audit')


def test_customer_service_and_forged_claims_keep_their_previous_results(workbench):
    """Regression: every read path other than workbench-member-v1 gives exactly the result it gave before the members were
    configured (same value, or the same refusal); a workbench-member-v1 claim from anyone but a member fails and leaves no audit."""
    w = workbench
    f = w['f']
    from nexloop_eios.object_reads import AuthorizedObjectReader
    def customer():
        session = authenticate_browser_business(f['reader'].pool, f['base']['issued'].session, world='real')
        return AuthorizedObjectReader(f['reader'].pool, session, f['reader'].signer), session
    def outcome(read):
        try:
            return ('ok', read())
        except Exception as error:
            return ('refused', type(error).__name__)
    def observe():
        from nexloop_eios.conversation_messages import ConversationMessagePort
        reader, session = customer()  # re-authenticated, as every HTTP request is
        port = ConversationMessagePort(f['reader'].pool, session, f['reader'].signer)
        service = AuthorizedObjectReader(f['reader'].pool, f['reader'].session, f['reader'].signer)
        return [outcome(lambda: reader.get('Message', w['refusal']['id'], fields=('body',))),
                outcome(lambda: service.get('Message', w['refusal']['id'], fields=('body',))),
                outcome(lambda: reader.get('Conversation', w['conversation']['id'])),
                outcome(lambda: [m['body'] for m in port.read_messages(conversation_id=w['conversation']['id'])['items']])]
    before = observe()
    provision(w)
    assert observe() == before and before[3] == ('ok', [BODY])
    _, human = customer()
    # The customer (WebChat session) and a service credential presenting the member derivation: refused by SQL, no audit row.
    for session in (human, f['reader'].session):
        forged = WorkbenchReader(f['reader'].pool, session, f['reader'].signer)
        assert forged.message_fields(w['refusal']['id'], ('body',)) is None
    assert audit_rows(w['admin']) == []
