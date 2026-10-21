"""NX-042 T2 (design §2, §5, D1, D4, D6, D7): the decision layer and seeded population sampling in a simulation world.

The simulation intake (nexloop_sim_api, the world's api credential) samples instances of a published twin version (T1) over the world's
synthetic Consumers (S3): every behaviour is the version's interval at its coverage and one simulated outcome drawn inside it (never a
single probability); the same seed and twin version reproduce byte for byte; real and test are refused; every sample is a simulated
output with the twin's calibration status; nothing becomes a Claim or a fact. Real PostgreSQL; fixtures from the S7 tests.
"""
import copy
import hashlib
import json
import uuid
from pathlib import Path

import psycopg
import pytest
from jsonschema import Draft202012Validator, FormatChecker

from support.tenants import TENANT_A
from test_nx041_s7_pg import generate, governed, s7  # noqa: F401
from test_nx042_consumer_twins_pg import DEFINITION

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = Draft202012Validator(json.loads((ROOT / 'packages/contracts/twin-sample.schema.json').read_text()), format_checker=FormatChecker())
TWIN = DEFINITION['twin_id']


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def publish(admin, definition=None):
    return governed(admin, 'nexloop_governed_publish_consumer_twin', {'operation': 'publish_consumer_twin', 'request_id': uuid.uuid4().hex,
                                                                     'definition': definition or DEFINITION})


def sample(f, world=None, token=None, twin=TWIN, version=1, seed='population-1', count=6, role='nexloop_sim_api'):
    with f['role'](role)() as db:
        return db.execute('select authz.nexloop_twin_sample(%s,%s,%s,%s,%s,%s)',
                          (digest(token or f['token_a']), world or f['a'], twin, version, seed, count)).fetchone()[0]


def said(run):
    """What the samples say, without the world or the run id (the reproducibility comparison)."""
    return [{k: v for k, v in s.items() if k not in ('world', 'run_id')} for s in run['samples']]


@pytest.fixture
def t2(s7):
    publish(s7['admin'])
    generate(s7['admin'], s7['b'])  # the same S3 seed as world a: the same synthetic Consumers
    return s7


def test_the_decision_layer_gives_intervals_and_simulated_outcomes_marked_uncalibrated(t2):
    run = sample(t2)
    assert run['instances'] == 6 and run['calibration_status'] == 'uncalibrated' and run['data_mode'] == 'simulation' and len(run['samples']) == 6
    behaviors = DEFINITION['parameters']['behaviors']
    for s in run['samples']:
        SAMPLE.validate(s)
        assert (s['data_mode'], s['output_kind'], s['origin'], s['calibration_status'], s['twin_version']) == ('simulation', 'simulated_output', 'twin', 'uncalibrated', 1)
        assert set(s['behaviors']) == set(behaviors)
        for name, b in s['behaviors'].items():
            # D4: the version's interval at its coverage and an outcome; no probability is output.
            assert (b['low'], b['high'], b['coverage']) == (behaviors[name]['low'], behaviors[name]['high'], behaviors[name]['coverage'])
            assert type(b['simulated_outcome']) is bool and set(b) == {'low', 'high', 'coverage', 'simulated_outcome'}
    assert 'probability' not in json.dumps(run)
    # The Consumers are the world's synthetic ones (cycled when fewer than the instances).
    consumers = {r[0] for r in t2['admin'].execute("select object_id from runtime.nexloop_sim_objects where world=%s and type_name='Consumer'", (t2['a'],))}
    assert {s['consumer_id'] for s in run['samples']} == consumers
    # A single probability is no sample (contract), and the twin it would come from is not publishable (T1).
    forged = copy.deepcopy(run['samples'][0])
    forged['behaviors']['payment_intent'] = {'probability': 0.1}
    assert not SAMPLE.is_valid(forged)


def test_the_same_seed_and_twin_version_reproduce_byte_for_byte(t2):
    one, again = sample(t2), sample(t2)
    assert again == one  # a repeated call returns the same run
    other_world = sample(t2, world=t2['b'], token=t2['token_b'])
    assert other_world['run_id'] != one['run_id'] and said(other_world) == said(one) and other_world['output_digest'] == one['output_digest']
    assert said(sample(t2, seed='population-2')) != said(one)  # positive control: another seed, other outcomes
    changed = copy.deepcopy(DEFINITION)
    changed['parameters']['behaviors']['reply_within_24h'].update(low=0.1, high=0.2)
    assert publish(t2['admin'], changed)['version'] == 2
    assert sample(t2, version=2)['output_digest'] != one['output_digest']  # another twin version, another output


def test_outcomes_follow_the_interval(t2):
    """The outcome is drawn against a probability inside the interval: a high interval yields mostly true, a low one mostly false."""
    d = copy.deepcopy(DEFINITION)
    d['twin_id'] = 'trial.extremes'
    d['parameters']['behaviors'] = {'likely': {'low': 0.9, 'high': 1.0, 'coverage': 0.8, 'hypotheses': ['evening_slower']},
                                    'unlikely': {'low': 0.0, 'high': 0.05, 'coverage': 0.8, 'hypotheses': ['evening_slower']}}
    publish(t2['admin'], d)
    run = sample(t2, twin='trial.extremes', count=400)
    likely = sum(s['behaviors']['likely']['simulated_outcome'] for s in run['samples'])
    unlikely = sum(s['behaviors']['unlikely']['simulated_outcome'] for s in run['samples'])
    assert likely >= 340 and unlikely <= 40, (likely, unlikely)


def test_real_test_other_roles_and_other_worlds_are_refused(t2):
    f = t2
    assert sample(f)['instances'] == 6  # positive control
    for world, token in (('real', f['token_real']), ('test', f['token_test']), ('real', f['token_a'])):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            sample(f, world=world, token=token)
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='service authentication denied'):
        sample(f, world=f['b'])  # A's credential in world B
    for role in ('nexloop_api', 'nexloop_domain_worker', 'nexloop_sim_worker', 'nexloop_twin_port'):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            sample(f, role=role)
    for bad in ({'count': 0}, {'count': 1001}, {'seed': 'a b'}, {'version': 0}):
        with pytest.raises(psycopg.errors.InvalidParameterValue, match='twin sample: invalid'):
            sample(f, **bad)
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='twin_unknown'):
        sample(f, twin='no.such')


def test_a_retired_twin_and_a_model_generator_do_not_run(t2):
    f = t2
    governed(f['admin'], 'nexloop_governed_retire_consumer_twin', {'operation': 'retire_consumer_twin', 'request_id': uuid.uuid4().hex,
                                                                    'twin_id': TWIN, 'version': 1, 'reason': '被新版本取代'})
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='twin_retired'):
        sample(f)
    d = copy.deepcopy(DEFINITION)
    d['twin_id'] = 'trial.model_text'
    d['generator_version'] = 'model-text/1'
    publish(f['admin'], d)
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='model_budget_required'):  # D6: no model in T2
        sample(f, twin='trial.model_text')


def test_samples_are_never_facts_and_leave_with_the_world(t2):
    f = t2
    admin = f['admin']
    sample(f)
    for table in ('ontology.nexloop_claims', 'runtime.nexloop_conversation_messages', 'ontology.objects'):
        assert admin.execute(f'select count(*) from {table} where world=%s', (f['a'],)).fetchone()[0] == 0, table
    with pytest.raises(psycopg.errors.InsufficientPrivilege), admin.transaction():  # append-only
        admin.execute("update runtime.nexloop_twin_samples set behaviors='{}'")
    result = governed(admin, 'nexloop_governed_delete_sim_world', {'operation': 'delete_sim_world', 'request_id': uuid.uuid4().hex, 'world': f['a'],
                                                                   'reason': '实验结束'})
    for _ in range(10):
        if result['status'] == 'deleted':
            break
        result = admin.execute('select runtime.nexloop_sim_world_purge(%s,%s,1000)', (TENANT_A, f['a'])).fetchone()[0]
    assert result['status'] == 'deleted' and result['blocked'] == {}, result
    for table in ('runtime.nexloop_twin_sample_runs', 'runtime.nexloop_twin_samples'):
        assert admin.execute(f'select count(*) from {table} where world=%s', (f['a'],)).fetchone()[0] == 0, table
    assert admin.execute('select count(*) from control.nexloop_consumer_twins').fetchone()[0] == 1  # the twin stays (D5)
