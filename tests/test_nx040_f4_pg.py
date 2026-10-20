"""NX-040 F4 on PostgreSQL (design §4.6, §6, §7, §11 F4; D21-D27): the gate registry in the frozen bundle, the release permit checked
and used by the Schema publication, the batched migration above 10 000 objects, the Schema watch with its alert (positive control),
the opened ontology_schema dimension, and the whole path end to end: E4 candidate -> F2 freeze -> bundle (nx040-gates-1) -> V1 open ->
F4 gates and reflow reservation (evolution budget) -> conclude -> permit -> F3 publication by a person."""
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jsonschema
import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from authority_fixture import seed_authority
from nexloop_eios.evolution_store import EvaluatorStore
from nexloop_eios.observability import apply_rules, load_rules
from nexloop_eios.schema_gates import GATES, evaluate_schema
from nexloop_eios.schema_publication import build
from test_claim_matching_pg import env  # noqa: F401
from test_evolution_e4_pg import body_of, evo, hexid, opened, propose_raw  # noqa: F401
from test_evolution_e5_pg import SEED
from test_evolution_traces_pg import private
from test_evolution_v1_pg import register
from test_nx040_schema_publication_pg import TENANT, counts, port, proposal, seed_permit
from test_observability_pg import observed  # noqa: F401
from test_review_decisions_pg import review  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_workbench_read_pg import browser_business, conversations, provision, published_action, workbench, workbench_login  # noqa: F401

WITH_ENV = pytest.mark.parametrize('env', [TENANT], indirect=True)
V9 = Path(__file__).resolve().parents[1] / 'deploy/configuration/alert-rules.v9.json'
KEY = 'synthetic-f4-publication-01'


def latest_permit(f, pid):
    return f['admin'].execute("""select p.permit_id::text,p.used_by from runtime.nexloop_evo_release_permits p join ontology.nexloop_schema_proposals s
        on s.candidate_id=p.candidate_id where s.proposal_id=%s order by p.issued_at desc limit 1""", (pid,)).fetchone()


def fresh_proposal(f, name):
    """A proposal whose candidate has no permit yet."""
    pid = proposal(f, 'add_optional_property', 'Consumer', {'property_name': name, 'value_type': 'string'})
    with f['admin'].transaction():
        f['admin'].execute('set local session_replication_role=replica')
        f['admin'].execute('delete from runtime.nexloop_evo_release_permits where candidate_id=(select candidate_id from ontology.nexloop_schema_proposals where proposal_id=%s)', (pid,))
    return pid


@WITH_ENV
def test_publication_checks_and_uses_the_release_permit(review):  # noqa: F811
    f = review
    before = counts(f)
    missing = fresh_proposal(f, 'preferred_store')
    assert port(f).publish(missing, rationale='synthetic', idempotency_key=KEY) == {'published': False, 'codes': ['permit_missing'], 'source': 'permit'}
    for kwargs, code in (({'used': True}, 'permit_used'), ({'expired': True}, 'permit_expired'), ({'mismatch': True}, 'permit_mismatch')):
        pid = fresh_proposal(f, 'shoe_size')
        seed_permit(f, pid, **kwargs)
        assert port(f).publish(pid, rationale='synthetic', idempotency_key=KEY)['codes'] == [code], code
    assert counts(f) == before
    pid = proposal(f, 'add_optional_property', 'Consumer', {'property_name': 'preferred_store', 'value_type': 'string'})
    out = port(f).publish(pid, rationale='synthetic', idempotency_key=KEY)
    permit, used_by = latest_permit(f, pid)
    assert out['published'] is True and out['permit'] == 'permit:' + permit and used_by == 'publication:' + out['publication_id']
    assert f['admin'].execute('select permit_ref from ontology.nexloop_schema_proposals where proposal_id=%s', (pid,)).fetchone()[0] == 'permit:' + permit
    # Used once: the permit cannot be changed or used again.
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='used once'), f['admin'].transaction():
        f['admin'].execute('set local role nexloop_owner')
        f['admin'].execute("select set_config('eios.tenant_id',%s,true)", (TENANT,))
        f['admin'].execute("update runtime.nexloop_evo_release_permits set used_at=now(),used_by=%s where permit_id=%s", ('publication:' + str(uuid.uuid4()), permit))


@WITH_ENV
def test_above_10000_objects_a_publication_is_refused_or_batched(review):  # noqa: F811
    f = review
    admin = f['admin']
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("""insert into ontology.objects(tenant_id,type_name,object_id,schema_version,properties,created_at,updated_at,world,nexloop_revision)
            select %s,'Consumer',encode(sha256(convert_to('f4-'||g,'UTF8')),'hex'),1,'{}',now(),now(),'real',1 from generate_series(1,10001) g""", (TENANT,))
    total = admin.execute("select count(*) from ontology.objects where tenant_id=%s and type_name='Consumer' and schema_version=1", (TENANT,)).fetchone()[0]
    pid = proposal(f, 'add_optional_property', 'Consumer', {'property_name': 'preferred_store', 'value_type': 'string'})
    refused = port(f).publish(pid, rationale='synthetic', idempotency_key=KEY)
    assert refused['published'] is False and 'migration_over_limit' in refused['codes']
    out = port(f).publish(pid, rationale='synthetic', idempotency_key='synthetic-f4-batched-0001', batch=True)
    assert out['published'] is True and out['objects_migrated'] == 10_000 and out['objects_remaining'] == total - 10_000
    step = port(f).migrate_batch(out['publication_id'])
    assert (step['migrated'], step['remaining'], step['batches']) == (total - 10_000, 0, 2)
    assert port(f).migrate_batch(out['publication_id'])['remaining'] == 0
    assert admin.execute("select count(*) from ontology.objects where tenant_id=%s and type_name='Consumer' and schema_version=1", (TENANT,)).fetchone()[0] == 0


def test_an_ontology_schema_bundle_freezes_nx040_gates_and_the_dimension_is_open(evo):  # noqa: F811
    e = evo
    opened(e)
    assert {r[0] for r in e['admin'].execute('select dimension from control.nexloop_evo_open_dimensions')} == {'semantic_content', 'ontology_schema'}
    cid = str(uuid.uuid4())
    propose_raw(e, body_of(e, candidate={'candidate_id': cid, 'dimension': 'ontology_schema', 'allowed_change_scope': ['eios:object_type:Product']},
                           patch={'change_kind': 'add_optional_property', 'target_refs': ['eios:object_type:Product']}))
    frozen = e['store'].freeze_bundle(cid, seed=SEED)
    assert frozen['bundle']['hard_gates_version'] == 'nx040-gates-1' and frozen['bundle']['hard_gates'] == list(GATES)
    semantic = propose_raw(e, body_of(e, candidate={'candidate_id': str(uuid.uuid4())}))['candidate']['candidate_id']
    other = e['store'].freeze_bundle(semantic, seed=SEED)
    assert other['bundle']['hard_gates_version'] == 'nx039-gates-1'  # semantic_content keeps its list


def jobs(admin, n, failed, at_sql):
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        for i in range(n):
            admin.execute(f"""insert into runtime.jobs(job_id,invocation_id,capability_name,capability_version,capability_type,execution_mode,tenant_id,actor_id,trace_id,
                request_id,normalized_input,plan_snapshot,status,created_at,updated_at,world,queue) values(%s,'inv','nexloop.claim_extraction','1','runtime','async',%s,'a','t','r','{{}}','{{}}',
                %s,{at_sql},{at_sql},'real','claim-extraction')""", ('f4-' + secrets.token_hex(6), TENANT, 'failed' if i < failed else 'succeeded'))


def test_the_schema_watch_alerts_on_extraction_failures_and_never_rolls_back(observed):  # noqa: F811
    o = observed
    admin = o['admin']
    assert apply_rules(load_rules(V9), TENANT, database_url_file=o['w']['configurator'])['configured'] is True
    firing = lambda: {r[0] for r in admin.execute("select rule_id from control.nexloop_alert_state where tenant_id=%s and firing", (TENANT,)).fetchall()}  # noqa: E731
    assert o['evaluate']()['rules_version'] == 9 and 'schema_release_regressing' not in firing()
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("""insert into runtime.nexloop_schema_watches(tenant_id,world,publication_id,type_name,published_at,watch_until)
            values(%s,'real',%s,'Consumer',now()-interval '2 days',now()-interval '2 days'+interval '7 days')""", (TENANT, str(uuid.uuid4())))
    jobs(admin, 20, 1, "now()-interval '4 days'")
    jobs(admin, 20, 1, "now()-interval '1 day'")
    o['evaluate']()
    assert 'schema_release_regressing' not in firing()  # 5% before, 5% after
    jobs(admin, 20, 4, "now()-interval '6 hours'")  # since: 5/40 = 12.5% vs 5%
    o['evaluate']()
    assert 'schema_release_regressing' in firing()
    w = admin.execute("select runtime.nexloop_metrics_snapshot(%s,'real')->'evolution'->'schema_watch'", (TENANT,)).fetchone()[0]
    assert w['regressing'] == 1 and w['publications'][0]['suggestion'] == 'hide' and w['publications'][0]['extraction_regressing'] is True
    assert admin.execute("select count(*) from ontology.object_type_versions where tenant_id=%s and type_name='Consumer'", (TENANT,)).fetchone()[0] <= 1  # nothing rolled back


def sample(schema):
    kind = schema.get('type')
    if kind == 'object':
        return {k: sample(schema.get('properties', {}).get(k, {})) for k in schema.get('required', [])}
    return {'string': 'x', 'integer': 1, 'number': 1, 'boolean': True, 'array': []}.get(kind, 'x')


@WITH_ENV
def test_end_to_end_candidate_freeze_gates_permit_publication(review, evo, tmp_path):  # noqa: F811
    f, e = review, evo
    admin = f['admin']
    opened(e)
    # E4: an ontology_schema candidate; F2: its proposal.
    cid = str(uuid.uuid4())
    propose_raw(e, body_of(e, candidate={'candidate_id': cid, 'dimension': 'ontology_schema',
                                        'allowed_change_scope': ['eios:object_type:Consumer', 'eios:property:Consumer.preferred_store']},
                           patch={'change_kind': 'add_optional_property', 'target_refs': ['eios:object_type:Consumer']}))
    body = {'proposal_id': str(uuid.uuid4()), 'candidate_id': cid, 'operation': 'add_optional_property', 'type_name': 'Consumer',
            'change': {'property_name': 'preferred_store', 'value_type': 'string'}, 'claim_ids': [], 'candidate_definition_ids': [],
            'compatibility_report_digest': 'a' * 64, 'permission_proof_digest': 'b' * 64}
    with psycopg.connect(make_conninfo(e['pg'], user='nexloop_evo_writer'), autocommit=True) as db:
        assert db.execute('select authz.nexloop_schema_proposal_freeze(%s,%s)', (e['digest'], json.dumps(body))).fetchone()[0]['frozen'] is True
    pid = body['proposal_id']
    # The bundle (nx040-gates-1) and the evaluator's opening (V1; the dimension is open, D21).
    frozen = e['store'].freeze_bundle(cid, seed=SEED)
    token, _ = seed_authority(admin, TENANT, 'eios:artifact:' + 'e' * 32, identity_suffix='-f4-evaluator')
    register(admin, hexid(token), 'evaluator')
    ev = EvaluatorStore(lambda: psycopg.connect(make_conninfo(e['pg'], user='nexloop_evo_evaluator'), autocommit=True), private(tmp_path / 'f4-ev', token))
    assert ev.open(cid, frozen['bundle_digest'])['opened'] is True
    # D23 needs a valid and an invalid case per Action: the fixture's Action takes any input ({}), so give it a real input schema first
    # (an Action that accepts everything would fail G12 for ever).
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("""update control.nexloop_action_definitions set definition=jsonb_set(definition,'{input_schema}',%s)
            where tenant_id=%s and resource_id='eios:action:Consumer.edit:1'""",
                      (Jsonb({'type': 'object', 'required': ['consumer_id'], 'additionalProperties': False, 'properties': {'consumer_id': {'type': 'string'}}}), TENANT))
    # F4 gates: F1 against the Parent, the tool regression set (D23), the Context baseline and the reflow reserved as evolution (D26).
    p = port(f)
    basis = p.basis(pid)
    publication, report = build(basis, 'publish')
    assert report['allowed'] is True
    schemas = {}
    for row, succ in zip(basis['actions'], publication['actions']):
        schemas[row['definition']['stable_name']] = {'parent': row['definition']['input_schema'], 'candidate': succ['definition']['input_schema']}
    cases = []
    for name, s in schemas.items():
        cases += [{'id': name + '-valid', 'action': name, 'params': sample(s['parent']), 'expect': 'valid'},
                  {'id': name + '-invalid', 'action': name, 'params': [], 'expect': 'invalid'}]
        jsonschema.Draft202012Validator(s['parent']).validate(sample(s['parent']))
    reserved = ev.reflow_reserve(cid, 'reflow:' + cid, '0.5', 'CNY')
    assert reserved['reserved'] is True
    assert admin.execute("select budget_kind,source_ref from control.nexloop_budget_consumption where consumption_id=%s", ('reflow:' + cid,)).fetchone() == \
        ('evolution', 'evolution-candidate:' + cid)
    f1_change = {'op': 'add_optional_property', 'type_name': 'Consumer', 'property': {'property_name': 'preferred_store', 'value_type': 'string', 'nullable': True},
                 'successor_actions': [{'stable_name': a['definition']['stable_name'], 'replaces': a['definition']['version'] - 1, 'version': a['definition']['version'],
                                        'object_types': [{'stable_name': t['stable_name'], 'version': t['version']} for t in a['definition']['object_types']],
                                        'input_schema_hash': 'same', 'capability_schema_hash': 'same', 'risk_level': None, 'effect_categories': []} for a in publication['actions']]}
    parent = {'types': {'Consumer': basis['schema']}, 'object_counts': {'Consumer': basis['object_count']},
              'actions': [{'stable_name': r['definition']['stable_name'], 'version': r['definition']['version'],
                           'object_types': [{'stable_name': t['stable_name'], 'version': t['version']} for t in r['definition']['object_types']],
                           'input_schema_hash': 'same', 'capability_schema_hash': 'same', 'risk_level': None, 'effect_categories': []} for r in basis['actions']]}
    evaluation = evaluate_schema(frozen['bundle'], frozen['bundle_digest'], f1_change, parent, created_at='2026-10-20T14:00:00Z',
                                 inputs={'tool_cases': cases, 'action_schemas': schemas, 'context_volume': {'parent': 1000, 'candidate': 1010},
                                         'reflow_reserved': reserved['reserved'], 'reflow_cost': '0.5'})
    assert (evaluation['conclusion'], evaluation['change_class']) == ('accept', 'minor'), evaluation['failed_gates']
    assert ev.conclude(evaluation)['status'] == 'accepted_for_release'
    permit = ev.permit_issue(cid)
    assert permit['issued'] is True and ev.permit_issue(cid)['replayed'] is True
    # F3 by a person, using the permit once.
    out = port(f).publish(pid, rationale='端到端：E4 → F2 → 门槛 → 许可 → F3', idempotency_key=KEY)
    assert out['published'] is True and out['permit'] == 'permit:' + permit['permit_id']
    assert latest_permit(f, pid)[1] == 'publication:' + out['publication_id']
    assert admin.execute('select status,permit_ref from ontology.nexloop_schema_proposals where proposal_id=%s', (pid,)).fetchone() == ('published', 'permit:' + permit['permit_id'])
    assert admin.execute('select count(*) from runtime.nexloop_schema_watches where publication_id=%s', (out['publication_id'],)).fetchone()[0] == 1
