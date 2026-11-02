"""K5 (docs/design/k5-consumer-evidence-views.md v2; 0406): the evidence graph, the message trace and the conversation metrics.

Real PostgreSQL and workbench login cookies on the slice 1 fixture (test_workbench_read_pg), synthetic data. Admin only seeds Claims,
match conclusions and candidates (as NX-019/NX-020 would leave them), the owner's group restriction fact and an erasure row, and
probes the read audit. Checked: the contracts; Q1 outreach and Q3 cost for the owner only; Q5 the quote for the reviewer roles
only; L6 condition 2 (matched property, else any withheld Consumer property hides a Claim, and what is derived from it); erased
Claims counted for the owner only; corrected Claims without predicate or quote; references and edges naming listed Claims only;
the nine trace steps; an erasing consumer 404; world real only; the read audit with the views' purposes.
"""
import hashlib
import json
import secrets
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
import pytest
from psycopg.types.json import Jsonb

from test_workbench_read_pg import (TENANT, browser_business, conversations, identity, provision, published_action, uow,  # noqa: F401
    workbench, workbench_login)


def contract(name):
    return json.loads((Path(__file__).resolve().parents[1] / f'packages/contracts/{name}.schema.json').read_text())


def hexid(label):
    return hashlib.sha256(label.encode()).hexdigest()


QUOTE = '请不要再给我'


def claim(w, label, *, predicate='联系偏好', kind='preference', corrects=None, erased=False, resolution='awaiting_definition'):
    admin, f = w['admin'], w['f']
    claim_id = hexid('k5-' + label)
    value = {'type': 'erased', 'value': None, 'erased': True} if erased else {'type': 'string', 'value': 'synthetic'}
    valid_time = {'expression': '', 'kind': 'none', 'status': 'absent', 'start': None, 'end': None, 'timezone': 'Asia/Shanghai',
                  'anchor': datetime.now(UTC).isoformat()}
    admin.execute("""insert into ontology.nexloop_claims(tenant_id,world,claim_id,conversation_id,consumer_id,first_input_digest,topic_key,subject_kind,subject_ref,subject_text,
        predicate,value,speaker,polarity,modality,condition_text,time_expression,valid_time,source_message_id,source_sequence,span_start,span_end,source_content_hash,quote,
        extractor_version,confidence,epistemic_kind,resolution_state,derived_from,correlation_key,guard_flags,corrects_claim_id)
        values(%s,'real',%s,%s,%s,%s,%s,'consumer',%s,'',%s,%s,'consumer','affirmed','asserted','','',%s,%s,1,0,6,%s,%s,'nx019-extractor/1',0.9,
        %s,%s,'[]',%s,'[]',%s)""",
        (TENANT, claim_id, w['conversation']['id'], f['consumer'], 'e' * 64, 'k' * 64, 'consumer:' + f['consumer'], predicate, Jsonb(value),
         Jsonb(valid_time), w['refusal']['id'], hashlib.sha256(QUOTE.encode()).hexdigest(), '' if erased else QUOTE, kind, resolution,
         secrets.token_hex(32), corrects))
    return claim_id


def matched(w, claim_id, prop):
    w['admin'].execute("""insert into ontology.nexloop_claim_matches(tenant_id,world,claim_id,matcher_version,outcome,decision,recall,reason)
        values(%s,'real',%s,'synthetic-matcher/1','needs_resolution',%s,'[]','property_not_recalled')""",
        (TENANT, claim_id, Jsonb({'type': {'ref': 'eios:object_type:Consumer'}, 'instance': {}, 'property': {'ref': prop}, 'value': {}, 'rationale': ''})))


def candidate(w, label, claims):
    candidate_id = str(__import__('uuid').UUID(hexid('k5-candidate-' + label)[:32]))
    w['admin'].execute("""insert into ontology.nexloop_candidate_definitions(tenant_id,world,candidate_id,kind,dedupe_key,candidate,dependent_claims)
        values(%s,'real',%s,'property',%s,'{}',%s)""", (TENANT, candidate_id, hexid('k5-dedupe-' + label), claims))
    return candidate_id


def seed(w):
    """Consumer version 96 (group 'preference': referrer, favorite_sport); four Claims of the fixture's refusal message - one unmatched,
    one matched to Consumer/favorite_sport, one correcting the first, one erased - and two candidates."""
    admin, consumer = w['admin'], w['f']['consumer']
    definition = {'type_name': 'Consumer', 'version': 96, 'properties': [{'property_name': 'referrer', 'value_type': 'string'},
        {'property_name': 'favorite_sport', 'value_type': 'string'}],
        'property_groups': [{'group_name': 'preference', 'property_names': ['referrer', 'favorite_sport']}]}
    admin.execute("insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,'Consumer',96,%s)", (TENANT, Jsonb(definition)))
    admin.execute("update ontology.objects set schema_version=96,properties=%s where tenant_id=%s and type_name='Consumer' and object_id=%s",
                  (Jsonb({'favorite_sport': '网球'}), TENANT, consumer))
    c = {'plain': claim(w, 'plain'), 'sport': claim(w, 'sport', predicate='喜欢的运动')}
    matched(w, c['sport'], 'eios:property:Consumer/favorite_sport')
    c['fix'] = claim(w, 'fix', kind='correction', corrects=c['plain'])
    c['gone'] = claim(w, 'gone', erased=True)
    cands = {'plain': candidate(w, 'plain', [c['plain']]), 'both': candidate(w, 'both', [c['plain'], c['sport']])}
    return c, cands


def restrict(w):
    w['admin'].execute('insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) values(%s,%s,%s,%s)',
        (TENANT, 'property_group_restriction', ['Consumer'], Jsonb({'tenant_id': TENANT, 'type_name': 'Consumer', 'restricted_groups': ['preference'],
                                                                   'decision': 'owner 2026-11-02 (synthetic)'})))


def audited(admin, purpose):
    return admin.execute('select count(*) from runtime.nexloop_workbench_read_audit where read_purpose=%s', (purpose,)).fetchone()[0]


def groups_of(view):
    assert [g['kind'] for g in view['groups']] == ['claim', 'commitment', 'action', 'outreach', 'candidate']
    return {g['kind']: g for g in view['groups']}


def assert_references_listed(view):
    """L6 condition 1: every claim id a response names is a claim item of that response; every edge joins two listed items."""
    groups = groups_of(view)
    listed = {c['claim_id'] for c in groups['claim']['items']}
    key = {'claim': 'claim_id', 'commitment': 'commitment_id', 'action': 'intent_id', 'outreach': 'intent_id', 'candidate': 'candidate_id'}
    nodes = {f'{g["kind"]}:{x[key[g["kind"]]]}' for g in view['groups'] for x in g['items']}
    for c in groups['claim']['items']:
        assert set(c['successor_claim_ids']) <= listed
    for d in groups['candidate']['items']:
        assert set(d['dependent_claim_ids']) <= listed
    for r in groups['commitment']['items']:
        assert r['claim_id'] is None or r['claim_id'] in listed
    for e in view['edges']:
        assert e['source'] in nodes and e['target'] in nodes


def evidence(client, consumer):
    got = client.get(f'/api/v1/workbench/consumers/{consumer}/evidence-graph')
    assert got.status_code == 200, got.text
    view = got.json()
    jsonschema.validate(view, contract('workbench-consumer-evidence'))
    assert_references_listed(view)
    return view


def test_owner_and_operator_see_what_their_role_derives(workbench):
    w = workbench
    provision(w)
    admin, consumer = w['admin'], w['f']['consumer']
    c, cands = seed(w)
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        before = audited(admin, 'consumer_evidence')
        g = groups_of(evidence(client, consumer))
        claims = {x['claim_id']: x for x in g['claim']['items']}
        assert set(claims) == {c['plain'], c['sport'], c['fix']}
        assert (g['claim']['total'], g['claim']['invisible'], g['claim']['erased']) == (4, 0, 1)  # the owner learns of the erased one
        assert claims[c['plain']]['state'] == 'corrected' and claims[c['plain']]['predicate'] is None
        assert claims[c['plain']]['source']['quote'] is None and claims[c['plain']]['source']['quote_status'] == 'corrected'
        assert claims[c['plain']]['successor_claim_ids'] == [c['fix']]
        # Q5: the owner holds ontology.schema.review and reads the source message: the quote.
        assert claims[c['sport']]['source'] == {'conversation_id': w['conversation']['id'], 'sequence': 1, 'speaker': 'consumer', 'quote': QUOTE, 'quote_status': 'ok'}
        assert {x['candidate_id'] for x in g['candidate']['items']} == set(cands.values())
        assert g['outreach']['restricted_reason'] is None and g['candidate']['total'] == 2
        assert audited(admin, 'consumer_evidence') > before  # the consumer and each quoted message, with the view's purpose
    with w['client']() as client:
        assert workbench_login(client, w['operator']).status_code == 200
        view = evidence(client, consumer)
        g = groups_of(view)
        # The erased Claim is folded into invisible for every role but the owner; the quote is the reviewers' (Q5).
        assert (g['claim']['total'], g['claim']['invisible'], g['claim']['erased']) == (4, 1, None)
        assert all(x['source']['quote'] is None for x in g['claim']['items'])
        assert {x['source']['quote_status'] for x in g['claim']['items'] if x['state'] == 'active'} == {'reviewer_only'}
        assert QUOTE not in json.dumps(view, ensure_ascii=False)
        # Q1: outreach is counted, never listed, for a non-owner.
        assert g['outreach']['items'] == [] and g['outreach']['restricted_reason'] == 'owner_only' and g['outreach']['invisible'] == g['outreach']['total']
        assert g['action']['items'] == [] and g['action']['invisible'] == g['action']['total']


def test_a_restricted_group_hides_matched_and_unmatched_claims_and_what_derives_from_them(workbench):
    w = workbench
    provision(w)
    consumer = w['f']['consumer']
    c, cands = seed(w)
    restrict(w)
    for member in (w['owner'], w['operator']):
        with w['client']() as client:
            assert workbench_login(client, member).status_code == 200
            view = evidence(client, consumer)
            g = groups_of(view)
            # favorite_sport is in the restricted group (matched Claim), and the Consumer now withholds a property, so the unmatched
            # Claims are hidden too (L6 condition 2); the candidates depending on them follow.
            assert g['claim']['items'] == [] and g['candidate']['items'] == [] and g['candidate']['invisible'] == 2
            assert view['edges'] == []
            text = json.dumps(view, ensure_ascii=False)
            for claim_id in c.values():
                assert claim_id not in text
            assert '喜欢的运动' not in text and QUOTE not in text


def test_the_trace_has_nine_steps_and_the_metrics_withhold_cost_from_non_owners(workbench):
    w = workbench
    provision(w)
    admin = w['admin']
    c, _ = seed(w)
    message, conversation = w['refusal']['id'], w['conversation']['id']
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        got = client.get(f'/api/v1/workbench/messages/{message}/trace')
        assert got.status_code == 200, got.text
        trace = got.json()
        jsonschema.validate(trace, contract('workbench-message-trace'))
        steps = {s['step']: s for s in trace['steps']}
        assert [s['step'] for s in trace['steps']] == ['message', 'relay', 'context', 'run', 'action', 'receipt', 'extraction', 'matching', 'ontology_update']
        assert steps['message']['status'] == 'done' and steps['message']['refs'] == ['message:' + message]
        assert steps['action']['status'] == 'skipped' and steps['receipt']['status'] == 'skipped'  # no Action from this message
        assert steps['matching']['status'] == 'done' and steps['matching']['refs'] == ['match:' + c['sport']]
        assert steps['ontology_update']['status'] == 'none'  # no proposal: expected but not recorded, never omitted
        assert c['gone'] not in json.dumps(trace)
        assert audited(admin, 'message_trace') >= 1
        got = client.get(f'/api/v1/workbench/conversations/{conversation}/metrics')
        assert got.status_code == 200, got.text
        metrics = got.json()
        jsonschema.validate(metrics, contract('workbench-conversation-metrics'))
        assert metrics['model_cost'] == {'runs': 0, 'totals': [], 'unpriced_entries': 0} and metrics['withheld'] == []
        assert metrics['claims'] == 3  # visible, erased not counted
    restrict(w)
    with w['client']() as client:
        assert workbench_login(client, w['operator']).status_code == 200
        trace = client.get(f'/api/v1/workbench/messages/{message}/trace').json()
        jsonschema.validate(trace, contract('workbench-message-trace'))
        steps = {s['step']: s for s in trace['steps']}
        assert steps['action']['status'] == 'restricted' and steps['receipt']['status'] == 'restricted'
        assert steps['matching']['status'] == 'none' and steps['matching']['refs'] == []  # the matched Claim is hidden now
        metrics = client.get(f'/api/v1/workbench/conversations/{conversation}/metrics').json()
        jsonschema.validate(metrics, contract('workbench-conversation-metrics'))
        assert metrics['model_cost'] is None and metrics['withheld'] == ['model_cost'] and metrics['claims'] == 0


def test_an_erasing_consumer_is_404_and_bad_queries_422(workbench):
    w = workbench
    provision(w)
    admin, consumer = w['admin'], w['f']['consumer']
    seed(w)
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        assert client.get(f'/api/v1/workbench/consumers/{consumer}/evidence-graph?kind=claim').status_code == 422
        assert client.get(f'/api/v1/workbench/consumers/{consumer}/evidence-graph?kind=nodes&after=25').status_code == 422
        assert client.get(f'/api/v1/workbench/consumers/{"0" * 64}/evidence-graph').status_code == 404
        admin.execute("""insert into control.nexloop_consumer_erasures(tenant_id,world,request_id,consumer_id,source,requested_by,reason,state,watermark)
            values(%s,'real','k5-erasure',%s,'owner','synthetic-owner','synthetic','blocking',0)""", (TENANT, consumer))
        assert client.get(f'/api/v1/workbench/consumers/{consumer}/evidence-graph').status_code == 404
        assert client.get(f'/api/v1/workbench/messages/{w["refusal"]["id"]}/trace').status_code == 404
        assert client.get(f'/api/v1/workbench/conversations/{w["conversation"]["id"]}/metrics').status_code == 404


def test_another_world_is_404_real_world_only(workbench, monkeypatch):
    """The workbench session is world real; a session of another world (as a later sim workbench would have) is refused before SQL."""
    from nexloop_eios.workbench_evidence import EvidenceViews, RealWorldOnly
    from nexloop_eios.workbench_reads import WorkbenchQueries

    class Session:
        world = 'shadow'

    class Reader:
        session = Session()

        def observe(self, verb, **payload):
            raise AssertionError('no SQL for another world')

    views = EvidenceViews(WorkbenchQueries(Reader()))
    for call in (lambda: views.consumer_evidence(consumer_id='a' * 64), lambda: views.message_trace(message_id='m'),
                 lambda: views.conversation_metrics(conversation_id='a' * 64)):
        with pytest.raises(RealWorldOnly):
            call()
    w = workbench
    provision(w)
    monkeypatch.setattr(WorkbenchQueries, 'consumer_evidence', lambda self, **kw: (_ for _ in ()).throw(RealWorldOnly()))
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        got = client.get(f'/api/v1/workbench/consumers/{w["f"]["consumer"]}/evidence-graph')
        assert (got.status_code, got.json()['code']) == (404, 'real_world_only')


def test_operation_codes():
    from nexloop_eios.workbench_evidence import operation_code
    assert operation_code('Commitment.fulfil') == 'commitment.fulfil'
    assert operation_code('service.send') == 'service.send'
    assert operation_code('9-Odd Name!') == 'action_9_odd_name'


def commitment(w, label, claim_id):
    """A registered commitment derived from a Claim (runtime row and its Commitment object), seeded past the registration triggers."""
    admin, consumer = w['admin'], w['f']['consumer']
    commitment_id = hexid('k5-commitment-' + label)
    definition = {'type_name': 'Commitment', 'version': 1, 'properties': [{'property_name': 'status', 'value_type': 'string'},
        {'property_name': 'consumer_id', 'value_type': 'string'}, {'property_name': 'due_at', 'value_type': 'string'}]}
    admin.execute("insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,'Commitment',1,%s) on conflict do nothing", (TENANT, Jsonb(definition)))
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("""insert into runtime.nexloop_commitments(tenant_id,world,commitment_id,dedupe_key,origin,claim_id,message_id,consumer_id,correlation_key,intent_id,properties,registered_at)
            values(%s,'real',%s,%s,'claim',%s,%s,%s,%s,%s,'{}',clock_timestamp())""",
            (TENANT, commitment_id, 'k5-' + label, claim_id, w['refusal']['id'], consumer, 'c' * 64, 'k5-intent-' + label))
        admin.execute("""insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
            values(%s,'real','Commitment',%s,1,%s,'nexloop-action','k5',clock_timestamp(),clock_timestamp())""",
            (TENANT, commitment_id, Jsonb({'status': 'open', 'consumer_id': consumer})))
    return commitment_id


def test_pages_of_25_and_a_commitment_follows_its_claim(workbench):
    w = workbench
    provision(w)
    consumer = w['f']['consumer']
    c, _ = seed(w)
    for n in range(30):
        claim(w, f'page-{n}')
    derived = commitment(w, 'sport', c['sport'])
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        g = groups_of(evidence(client, consumer))
        assert len(g['claim']['items']) == 25 and g['claim']['collapsed'] == 8 and g['claim']['next_cursor'] == '25'
        page = client.get(f'/api/v1/workbench/consumers/{consumer}/evidence-graph?kind=claim&after=25')
        assert page.status_code == 200
        second = groups_of(page.json())
        assert len(second['claim']['items']) == 8 and second['claim']['next_cursor'] is None
        assert_references_listed(page.json())
        items = g['commitment']['items']
        assert [x['commitment_id'] for x in items] == [derived] and items[0]['status'] == 'open'
        # Its claim is visible but on the second page: the reference is null rather than naming an unlisted Claim.
        assert items[0]['claim_id'] in (None, c['sport'])
    restrict(w)
    with w['client']() as client:
        assert workbench_login(client, w['operator']).status_code == 200
        view = evidence(client, consumer)
        g = groups_of(view)
        # The source Claim is hidden (restricted group): the commitment derived from it is counted, never listed.
        assert g['commitment']['items'] == [] and g['commitment']['invisible'] == 1 and derived not in json.dumps(view)


def test_the_sql_refuses_another_world_with_42501(workbench, monkeypatch):
    """The 0406 backstop: a correctly signed observe call of a K5 verb for a world other than real is refused by SQL itself (42501
    'k5: real_world_only'), past the Python check. The reader's own observe body, with only the world argument changed."""
    import hashlib as _hashlib
    import hmac as _hmac
    import psycopg
    from nexloop_eios.assembly import verify_application_role
    from nexloop_eios.context_engine.authority import action_claims
    from nexloop_eios.postgres_artifacts import canonical_payload
    from nexloop_eios.workbench_reads import WORKBENCH_READ, WorkbenchReader
    refused = []

    def observe(self, verb, **payload):
        claims = action_claims(self.pool, self.session, WORKBENCH_READ)
        body = canonical_payload({'verb': verb, **payload})
        claims = {**claims, 'protocol': 'nexloop-workbench-observe-v1', 'key_id': self.signer.key_id, 'parameters_digest': _hashlib.sha256(body.encode()).hexdigest()}
        text = canonical_payload(claims)
        signature = _hmac.new(self.signer.material, ('nexloop-workbench-observe-v1:' + text).encode(), 'sha256').hexdigest()
        try:
            with self.pool.connection() as db, db.transaction():
                verify_application_role(db)
                return db.execute('select authz.nexloop_workbench_observe_read(%s,%s,%s,%s,%s)', (self.session.token_digest, 'shadow', text, signature, body)).fetchone()[0]
        except psycopg.Error as error:
            refused.append((error.sqlstate, str(error).splitlines()[0]))
            raise

    w = workbench
    provision(w)
    seed(w)
    monkeypatch.setattr(WorkbenchReader, 'observe', observe)
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        assert client.get(f'/api/v1/workbench/consumers/{w["f"]["consumer"]}/evidence-graph').status_code == 403
        assert client.get(f'/api/v1/workbench/messages/{w["refusal"]["id"]}/trace').status_code == 403
        assert client.get(f'/api/v1/workbench/conversations/{w["conversation"]["id"]}/metrics').status_code == 403
    assert refused == [('42501', 'k5: real_world_only')] * 3
