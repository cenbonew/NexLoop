"""NX-042 T3 (design §5, D1, D4, D6, D7; NX-041 S7, S7b): the consumer twin on the consumer port, end to end.

In a simulation world with synthetic Consumers (S3) a published twin version is bound and runs N steps of observe (the S7b simulation
context pack fixed by reference, and what the Consumer was offered) and act (the server decides the step from the twin's decision layer
and writes it through the S7 port). Checked: the same seed and twin version reproduce byte for byte in another world; every event
carries the twin's provenance and calibration status; the port's 100 000 event ceiling still holds; real and test are refused and no
real row appears. Real PostgreSQL; fixtures from the S7 and T1 tests.
"""
import copy
import inspect
import uuid

import psycopg
import pytest

from nexloop_eios import twin_port as T
from support.tenants import TENANT_A
from test_nx041_s7_pg import consumers, generate, governed, s7  # noqa: F401
from test_nx042_consumer_twins_pg import DEFINITION

TWIN = DEFINITION['twin_id']


def publish(admin, definition=None):
    return governed(admin, 'nexloop_governed_publish_consumer_twin', {'operation': 'publish_consumer_twin', 'request_id': uuid.uuid4().hex,
                                                                     'definition': definition or DEFINITION})


def port(f, world=None, token=None, role='nexloop_twin_port'):
    return T.TwinPort(f['role'](role), token or f['token_a'], world or f['a'])


def real_rows(admin):
    """Rows of the real world in every table with a world column."""
    tables = admin.execute("""select distinct table_schema||'.'||table_name from information_schema.columns c join information_schema.tables t using(table_schema,table_name)
        where c.column_name='world' and t.table_type='BASE TABLE' and table_schema in ('authz','control','runtime','ontology') order by 1""").fetchall()
    return {t: admin.execute(f"select count(*) from {t} where world='real'").fetchone()[0] for (t,) in tables}


def said(act):
    """What a step says, without what belongs to its world (event id, world time, the observation of that world's pack)."""
    event = {k: v for k, v in (act['event'] or {}).items() if k != 'sim_time'}
    provenance = {k: v for k, v in act['provenance'].items() if k != 'observation_digest'}
    return {**{k: act[k] for k in ('consumer_id', 'step', 'behaviors', 'behavior', 'event_kind')}, 'event': event, 'provenance': provenance}


@pytest.fixture
def t3(s7):
    publish(s7['admin'])
    generate(s7['admin'], s7['b'])  # the same S3 seed as world a: the same synthetic Consumers
    return s7


def test_a_bound_twin_runs_observe_and_act_with_provenance_on_every_event(t3):
    f = t3
    admin = f['admin']
    before = real_rows(admin)
    twin = T.ConsumerTwin(TWIN, 1, 'run-1')
    people = consumers(admin, f['a'])
    acts = port(f).run(twin, people, steps=4)
    assert len(acts) == 4 * len(people)
    events = [a for a in acts if a['event_id']]
    assert events, 'the twin acted'
    for a in acts:
        p = a['provenance']
        assert (p['twin_id'], p['twin_version'], p['seed'], p['step'], p['calibration_status']) == (TWIN, 1, 'run-1', a['step'], 'uncalibrated')
        assert (p['data_mode'], p['output_kind'], p['origin']) == ('simulation', 'simulated_input', 'twin')
        for b in a['behaviors'].values():  # D4: intervals and an outcome, never a probability
            assert set(b) == {'low', 'high', 'coverage', 'simulated_outcome'}
    for a in events:
        attrs = a['event']['content']['attributes']
        assert attrs == {'behavior': a['behavior'], 'twin_id': TWIN, 'twin_version': 1, 'twin_seed': 'run-1', 'twin_step': a['step'],
                         'calibration_status': 'uncalibrated', 'origin': 'twin'}
        assert a['event']['port_version'].endswith('/1') and a['behaviors'][a['behavior']]['simulated_outcome'] is True
    # Every port event of this world came from a bound twin step (none without provenance).
    assert admin.execute("""select count(*) from runtime.nexloop_sim_consumer_events e where e.world=%s and not exists(
        select 1 from runtime.nexloop_twin_event_provenance p where p.world=e.world and p.event_id=e.event_id)""", (f['a'],)).fetchone()[0] == 0
    # The observation is the S7b pack, fixed by reference; a later step sees the earlier events.
    binding = port(f).bind(twin)['binding_id']
    first, later = port(f).observe(binding, people[0], 1), port(f).observe(binding, people[0], 4)
    assert first['pack_text'].startswith('{') and '"nexloop.context-pack.sim.v1"' in first['pack_text'] and first['pack_digest'] != later['pack_digest']
    assert port(f).observe(binding, people[0], 1) == first  # fixed
    assert real_rows(admin) == before  # nothing real


def test_the_same_seed_and_twin_version_reproduce_byte_for_byte(t3):
    f = t3
    people = consumers(f['admin'], f['a'])
    assert people == consumers(f['admin'], f['b'])
    twin = T.ConsumerTwin(TWIN, 1, 'repro')
    one = port(f).run(twin, people, steps=3)
    again = port(f).run(twin, people, steps=3)  # a repeated run replays
    assert again == one
    other = port(f, f['b'], f['token_b']).run(twin, people, steps=3)
    assert [said(a) for a in other] == [said(a) for a in one]
    assert [a['event_id'] for a in other if a['event_id']] != [a['event_id'] for a in one if a['event_id']]  # the worlds stay apart
    assert [said(a) for a in port(f).run(T.ConsumerTwin(TWIN, 1, 'repro-2'), people, steps=3)] != [said(a) for a in one]  # positive control


def test_the_port_ceiling_still_holds_for_the_twin(t3):
    f = t3
    admin = f['admin']
    d = copy.deepcopy(DEFINITION)
    d['twin_id'] = 'trial.always_visits'
    d['parameters']['behaviors'] = {'visit_store': {'low': 0.98, 'high': 1.0, 'coverage': 0.8, 'hypotheses': ['evening_slower']}}
    publish(admin, d)
    c = consumers(admin, f['a'])[0]
    p = port(f)
    binding = p.bind(T.ConsumerTwin('trial.always_visits', 1, 'quota'))['binding_id']
    p.observe(binding, c, 1)
    assert p.act(binding, c, 1)['event_kind'] == 'visit'
    limit = admin.execute('select runtime.nexloop_sim_port_event_limit()').fetchone()[0]
    assert limit == 100000
    with admin.transaction():
        admin.execute('update runtime.nexloop_sim_port_counters set events=%s where world=%s', (limit, f['a']))
    p.observe(binding, c, 2)
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='quota_exceeded'):
        p.act(binding, c, 2)
    assert admin.execute('select count(*) from runtime.nexloop_twin_event_provenance where world=%s and step=2', (f['a'],)).fetchone()[0] == 0
    assert p.act(binding, c, 1)['event_kind'] == 'visit'  # a replay is answered


def test_real_test_other_roles_and_unready_steps_are_refused(t3):
    f = t3
    twin = T.ConsumerTwin(TWIN, 1, 'refuse')
    binding = port(f).bind(twin)['binding_id']  # positive control
    c = consumers(f['admin'], f['a'])[0]
    with pytest.raises(ValueError):
        T.TwinPort(f['role']('nexloop_twin_port'), f['token_real'], 'real')
    for world, token in (('real', f['token_real']), ('test', f['token_test'])):
        with pytest.raises(psycopg.errors.InsufficientPrivilege), f['role']('nexloop_twin_port')() as db:
            db.execute('select authz.nexloop_twin_bind(%s,%s,%s,1,%s)', (T.hashlib.sha256(token.encode()).hexdigest(), world, TWIN, 'x'))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        port(f, world=f['b']).bind(twin)  # A's credential in world B
    for role in ('nexloop_api', 'nexloop_sim_api', 'nexloop_sim_worker', 'nexloop_domain_worker'):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            port(f, role=role).bind(twin)
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='step_not_observed'):
        port(f).act(binding, c, 1)
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='binding_unknown'):
        port(f, f['b'], f['token_b']).observe(binding, c, 1)  # a binding of another world
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='twin_unknown'):
        port(f).bind(T.ConsumerTwin('no.such', 1, 'x'))
    d = copy.deepcopy(DEFINITION)
    d['twin_id'] = 'trial.model_text'
    d['generator_version'] = 'model-text/1'
    # A generator that calls a model, listed as the slice adding it would (trusted configuration; test database only).
    f['admin'].execute("insert into control.nexloop_twin_generators values('model-text/1',true,'simulation')")
    publish(f['admin'], d)
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='model_budget_required'):
        port(f).bind(T.ConsumerTwin('trial.model_text', 1, 'x'))
    governed(f['admin'], 'nexloop_governed_retire_consumer_twin', {'operation': 'retire_consumer_twin', 'request_id': uuid.uuid4().hex,
                                                                    'twin_id': TWIN, 'version': 1, 'reason': '被新版本取代'})
    port(f).observe(binding, c, 1)
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='twin_retired'):
        port(f).act(binding, c, 1)


def test_the_twin_holds_no_credential_and_the_port_no_network():
    assert [x.name for x in T.ConsumerTwin.__dataclass_fields__.values()] == ['twin_id', 'version', 'seed']
    source = inspect.getsource(T)
    for banned in ('import socket', 'import requests', 'import httpx', 'urllib', 'goal_controls', 'business_actions'):
        assert banned not in source, banned


def test_twin_rows_leave_with_the_world(t3):
    f = t3
    admin = f['admin']
    port(f).run(T.ConsumerTwin(TWIN, 1, 'gone'), consumers(admin, f['a']), steps=2)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), admin.transaction():
        admin.execute("update runtime.nexloop_twin_event_provenance set calibration_status='calibrated'")
    result = governed(admin, 'nexloop_governed_delete_sim_world', {'operation': 'delete_sim_world', 'request_id': uuid.uuid4().hex, 'world': f['a'], 'reason': '实验结束'})
    for _ in range(10):
        if result['status'] == 'deleted':
            break
        result = admin.execute('select runtime.nexloop_sim_world_purge(%s,%s,1000)', (TENANT_A, f['a'])).fetchone()[0]
    assert result['status'] == 'deleted', result
    for table in ('runtime.nexloop_twin_bindings', 'runtime.nexloop_twin_observations', 'runtime.nexloop_twin_event_provenance'):
        assert admin.execute(f'select count(*) from {table} where world=%s', (f['a'],)).fetchone()[0] == 0, table

