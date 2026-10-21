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
    return T.TwinPort(f['role'](role), token or f['token_twin_a'], world or f['a'])


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
        text = {'text_template', 'text_generator', 'text_marker'} if 'text' in a['event']['content'] else set()  # T6: the text layer's attributes
        assert {k: v for k, v in attrs.items() if k not in text} == {'behavior': a['behavior'], 'twin_id': TWIN, 'twin_version': 1, 'twin_seed': 'run-1',
                                                                    'twin_step': a['step'], 'calibration_status': 'uncalibrated', 'origin': 'twin'}
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
    other = port(f, f['b'], f['token_twin_b']).run(twin, people, steps=3)
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
        port(f, f['b'], f['token_twin_b']).observe(binding, c, 1)  # a binding of another world
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



def test_steps_are_contiguous_and_alternate_and_the_client_never_gets_the_digest(t3):
    """L6 T3 fix verify 1.2: a twin cannot pick which steps to run - observe n needs act n-1, act n needs observe n; skipped or earlier
    new steps are refused, a repeated step replays. bind and act never hand the port client the twin digest (outcomes are unguessable
    before a step); the seed is the client's own and stays a provenance mark of the event."""
    f = t3
    p = port(f)
    bound = p.bind(T.ConsumerTwin(TWIN, 1, 'order'))
    assert set(bound) == {'binding_id', 'world', 'twin_id', 'twin_version', 'calibration_status'}
    b, c = bound['binding_id'], consumers(f['admin'], f['a'])[0]
    for step in (2, 3):  # not the first step
        with pytest.raises(psycopg.errors.InvalidParameterValue, match='step_out_of_order'):
            p.observe(b, c, step)
    first = p.observe(b, c, 1)
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='step_out_of_order'):
        p.observe(b, c, 2)  # act 1 first
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='step_not_observed'):
        p.act(b, c, 2)
    acted = p.act(b, c, 1)
    assert 'twin_digest' not in acted['provenance'] and acted['provenance']['seed'] == 'order'
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='step_out_of_order'):
        p.observe(b, c, 3)  # skipping step 2
    assert p.observe(b, c, 1) == first and p.act(b, c, 1) == acted  # repeats replay
    p.observe(b, c, 2)
    assert p.act(b, c, 2)['step'] == 2
    # Another Consumer has its own sequence.
    other = consumers(f['admin'], f['a'])[1]
    assert p.observe(b, other, 1)['step'] == 1


def test_the_assembler_admits_the_port_role_for_simulation_worlds_only(t3):
    """L6 T3 fix verify 4.2: the role check admits nexloop_twin_port only for a sim:/shadow: world (besides sim_effect_tenant)."""
    src = t3['admin'].execute("select prosrc from pg_proc where oid='authz.nexloop_sim_context_command(text,text,text,integer)'::regprocedure").fetchone()[0]
    assert "not (session_user='nexloop_twin_port' and coalesce(runtime.nexloop_world_kind(p_world),'') in ('simulation','shadow'))" in src
    assert not t3['admin'].execute("select has_function_privilege('nexloop_twin_port','authz.nexloop_sim_context_command(text,text,text,integer)','execute')").fetchone()[0]


def test_only_the_worlds_twin_credential_reaches_the_port(t3):
    """D10 extended (owner 2026-10-21): the twin port entries take the world's twin credential only; its api, worker and agent
    credentials are refused (credential_use), as is another world's twin credential."""
    from authority_fixture import seed_authority
    f = t3
    admin = f['admin']
    assert port(f).bind(T.ConsumerTwin(TWIN, 1, 'use'))['twin_id'] == TWIN  # positive control: the twin credential
    tokens = {'api': f['token_a']}
    for use, c in (('worker', '7'), ('agent', '8')):
        tokens[use], _ = seed_authority(admin, TENANT_A, 'eios:artifact:' + c * 32, world=f['a'], identity_suffix=f'-t3-{use}', sim_use=use)
    for use, token in tokens.items():
        for call in (lambda p: p.bind(T.ConsumerTwin(TWIN, 1, 'use')), lambda p: p.observe('a' * 64, 'b' * 64, 1), lambda p: p.act('a' * 64, 'b' * 64, 1)):
            with pytest.raises(psycopg.errors.InsufficientPrivilege, match='credential_use'):
                call(port(f, token=token))
        assert use
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        port(f, world=f['a'], token=f['token_twin_b']).bind(T.ConsumerTwin(TWIN, 1, 'use'))  # B's twin credential in world A
    # Revoked with its world's other credentials: one active twin credential per world.
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='one active credential per use'):
        admin.execute("""insert into authz.nexloop_service_credentials(token_digest,tenant_id,credential_id,binding,worlds,audience,status,expires_at,sim_use)
            select repeat('9',64),tenant_id,credential_id||'-2',binding,worlds,audience,'active',expires_at,'twin' from authz.nexloop_service_credentials
            where worlds=%s and sim_use='twin' and status='active'""", ([f['a']],))
