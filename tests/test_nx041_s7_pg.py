"""NX-041 S7 (design §5, §8, §11 S7): the consumer port, a deterministic twin stub, the complete AT-046 negatives (each with its
positive control) and the three acceptances §8 proposes (stand-ins make no external call, whole deletion, frozen input reproducible).

Real PostgreSQL on a clean catalog. Worlds are created through the owner's governed handler (S2), synthetic Consumers through the
generator (S3); the port is called as its own role nexloop_twin_port with the world's one credential, the stand-in as
nexloop_stand_in_worker (S4). The Host group check of AT-046 runs in apps/agent-host/test/nx041-host-group.test.ts.
"""
import hashlib
import json
import socket
import uuid
from pathlib import Path

import psycopg
import pytest
from jsonschema import Draft202012Validator, FormatChecker
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from authority_fixture import seed_authority
from nexloop_eios import consumer_port as P
from nexloop_eios import stand_in_executor as S
from nexloop_eios.bootstrap import bootstrap
from support.tenants import TENANT_A, TENANT_B

ROOT = Path(__file__).resolve().parents[1]
PORT_CONTRACT = Draft202012Validator(json.loads((ROOT / 'packages/contracts/consumer-port-event.schema.json').read_text()), format_checker=FormatChecker())


def governed(admin, fn, body, tenant=TENANT_A):
    return admin.execute(f"select control.{fn}(%s,'real','owner-principal','owner-subject',%s,%s)",
                         (tenant, 'intent-' + uuid.uuid4().hex[:12], Jsonb(body))).fetchone()[0]


def create_world(admin, kind='simulation', tenant=TENANT_A):
    return governed(admin, 'nexloop_governed_create_sim_world', {'operation': 'create_sim_world', 'request_id': uuid.uuid4().hex, 'kind': kind,
                                                                  'purpose': 'S7 consumer port', 'expires_in_days': 7}, tenant)['world']


def generate(admin, world, seed='frozen-1', consumers=3):
    return governed(admin, 'nexloop_governed_generate_sim_world_data', {
        'operation': 'generate_sim_world_data', 'request_id': 'gen-' + seed, 'world': world, 'kind': 'synthetic', 'seed': seed,
        'consumers': consumers, 'events_per_consumer': {'min': 0, 'max': 3}, 'segments': {'loyal': 1, 'new': 1}})


def consumers(admin, world):
    return [r[0] for r in admin.execute("select object_id from runtime.nexloop_sim_objects where world=%s and type_name='Consumer' order by object_id", (world,))]


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


@pytest.fixture
def s7(admin, pg):
    bootstrap(admin)
    for t in (TENANT_A, TENANT_B):
        admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (t,))
    a, b = create_world(admin), create_world(admin)
    token_a, _ = seed_authority(admin, TENANT_A, 'eios:artifact:' + 'a' * 32, world=a, identity_suffix='-s7-a')
    token_b, _ = seed_authority(admin, TENANT_A, 'eios:artifact:' + 'b' * 32, world=b, identity_suffix='-s7-b')
    token_real, _ = seed_authority(admin, TENANT_A, 'eios:artifact:' + 'c' * 32, world='real', identity_suffix='-s7-real')
    token_test, _ = seed_authority(admin, TENANT_A, 'eios:artifact:' + 'd' * 32, world='test', identity_suffix='-s7-test')
    generate(admin, a)
    role = lambda name: (lambda: psycopg.connect(make_conninfo(pg, user=name), autocommit=True))  # noqa: E731
    return {'admin': admin, 'pg': pg, 'a': a, 'b': b, 'token_a': token_a, 'token_b': token_b, 'token_real': token_real, 'token_test': token_test,
            'twin': role('nexloop_twin_port'), 'stand_in': role('nexloop_stand_in_worker'), 'api': role('nexloop_api'), 'role': role}


def port(f, world=None, token=None):
    return P.ConsumerPort(f['twin'], token or f['token_a'], world or f['a'])


# 1. The consumer port --------------------------------------------------------------------------------------------------------------

def test_the_twin_acts_through_the_port_only_with_server_time_and_simulation_marks(s7):
    f = s7
    c = consumers(f['admin'], f['a'])[0]
    twin = P.TwinStub(seed='exp-1')
    event = port(f).act(twin.act(c, 1))
    PORT_CONTRACT.validate(event)
    frozen = event['frozen']
    assert (frozen['world'], frozen['sequence'], frozen['data_mode'], frozen['output_kind'], frozen['origin'], event['replayed']) == \
        (f['a'], 1, 'simulation', 'simulated_input', 'twin', False)
    # The world time is the server's: the world clock (S5, paused at creation) - never a client value.
    now = f['admin'].execute('select runtime.nexloop_commercial_ts(runtime.nexloop_world_now(%s,%s))', (TENANT_A, f['a'])).fetchone()[0]
    assert frozen['sim_time'] == now
    # A retried step replays; the same step with other content is refused; the next step is the next sequence.
    again = port(f).act(twin.act(c, 1))
    assert again['frozen']['event_id'] == frozen['event_id'] and again['replayed'] is True
    forged = twin.act(c, 1)
    forged['content'] = {'text': '另一段合成文字'} if forged['event_kind'] == 'message' else {'attributes': {'mood': 'other'}}
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='step_reused'):
        port(f).act(forged)
    assert port(f).act(twin.act(c, 2))['frozen']['sequence'] == 2
    # Nothing reached a real table: no conversation, message, Claim or object, in any world.
    for table in ('runtime.nexloop_conversation_messages', 'ontology.nexloop_claims', 'ontology.objects'):
        assert f['admin'].execute(f'select count(*) from {table}').fetchone()[0] == 0, table


def test_the_port_refuses_real_test_unknown_consumers_client_time_and_other_roles(s7):
    f = s7
    c = consumers(f['admin'], f['a'])[0]
    request = P.TwinStub(seed='exp-2').act(c, 1)
    assert port(f).act(request)['frozen']['sequence'] == 1  # positive control
    for world, token in (('real', f['token_real']), ('test', f['token_test']), ('real', f['token_a'])):
        with f['twin']() as db, pytest.raises(psycopg.errors.InsufficientPrivilege, match='real_world_refused'):
            db.execute('select authz.nexloop_sim_consumer_act(%s,%s,%s)', (digest(token), world, Jsonb(request)))
        with pytest.raises(ValueError):
            P.ConsumerPort(f['twin'], token, world)
    # sim A's credential does not reach world B; B has no such Consumer either.
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='service authentication denied'):
        port(f, f['b'], f['token_a']).act(request)
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='consumer_unknown'):
        port(f, f['b'], f['token_b']).act(request)
    # A Consumer that is not a generated synthetic one; a client time, mode, world or event id; a malformed step.
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='consumer_unknown'):
        port(f).act({**request, 'consumer_id': 'e' * 64})
    for extra in ({'sim_time': '2026-01-01T00:00:00Z'}, {'mode': 'real'}, {'world': 'real'}, {'event_id': 'f' * 64}):
        with f['twin']() as db, pytest.raises(psycopg.errors.InvalidParameterValue, match='consumer port: invalid'):
            db.execute('select authz.nexloop_sim_consumer_act(%s,%s,%s)', (digest(f['token_a']), f['a'], Jsonb({**request, **extra})))
    for bad in ({'twin': {**request['twin'], 'step': 0}}, {'event_kind': 'purchase'}, {'content': {'html': '<b>'}},
                {'event_kind': 'message', 'content': {'attributes': {}}}):
        with pytest.raises(psycopg.errors.InvalidParameterValue, match='consumer port: invalid'):
            port(f).act({**request, **bad})
    # Another role holds no port entry.
    with f['api']() as db, pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute('select authz.nexloop_sim_consumer_act(%s,%s,%s)', (digest(f['token_a']), f['a'], Jsonb(request)))


def test_the_twin_observes_what_its_consumer_is_shown(s7):
    f = s7
    c, other = consumers(f['admin'], f['a'])[:2]
    offer = "select runtime.nexloop_sim_consumer_offer(%s,%s,%s,%s,%s)"
    assert f['admin'].execute(offer, (TENANT_A, f['a'], c, 'agent_reply', Jsonb({'text_digest': 'a' * 64}))).fetchone()[0] == 1
    assert f['admin'].execute(offer, (TENANT_A, f['a'], c, 'stand_in_receipt', Jsonb({'status': 'fulfilled'}))).fetchone()[0] == 2
    seen = port(f).observe(c)
    assert [(o['sequence'], o['observation_kind'], o['data_mode']) for o in seen] == [(1, 'agent_reply', 'simulation'), (2, 'stand_in_receipt', 'simulation')]
    assert port(f).observe(c, after=1)[0]['sequence'] == 2 and port(f).observe(other) == []
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='real_world_refused'), f['admin'].transaction():
        f['admin'].execute(offer, (TENANT_A, 'real', c, 'agent_reply', Jsonb({})))


def test_the_port_role_reaches_the_port_only(s7):
    """The twin calls no Action and holds no other entry: the security definer functions nexloop_twin_port can execute are the two
    port entries (and the public erasure helper, which answers false for anyone but nexloop_owner)."""
    reachable = {r[0].split('(')[0] for r in s7['admin'].execute("""select p.oid::regprocedure::text from pg_proc p join pg_namespace n on n.oid=p.pronamespace
        where n.nspname in ('authz','control','runtime','ontology','public') and p.prosecdef and has_function_privilege('nexloop_twin_port',p.oid,'execute')""").fetchall()}
    assert reachable == {'authz.nexloop_sim_consumer_act', 'authz.nexloop_sim_consumer_observe', 'runtime.nexloop_erasure_permits'}, reachable
    for table in ('runtime.nexloop_sim_consumer_events', 'runtime.nexloop_sim_consumer_observations', 'runtime.nexloop_outbound_messages'):
        assert not s7['admin'].execute("select has_table_privilege('nexloop_twin_port',%s,'select,insert,update,delete')", (table,)).fetchone()[0]


# 2. AT-046: the server refuses, even when the model forges the mode -------------------------------------------------------------------

def run_command(world, mode):
    command = json.loads((ROOT / 'packages/contracts/examples/run-command.valid.json').read_text())
    command.update(world_id=world, mode=mode, not_after=(__import__('datetime').datetime.now(__import__('datetime').UTC)
                                                        + __import__('datetime').timedelta(minutes=10)).isoformat().replace('+00:00', 'Z'))
    return command


def test_at046_a_forged_mode_is_refused_by_the_server(s7):
    from nexloop_eios.runtime_activation import _command
    for world, mode in ((s7['a'], 'real'), (s7['a'], 'test'), ('real', 'simulation'), (s7['a'], 'shadow')):
        with pytest.raises(ValueError, match='world unavailable'):
            _command(run_command(world, mode))
    assert len(_command(run_command(s7['a'], 'simulation'))[1]) == 64  # positive control: the mode is the world's kind
    assert len(_command(run_command('real', 'real'))[1]) == 64


def test_at046_a_sim_credential_never_authenticates_real(s7):
    identity = lambda token, world: s7['admin'].execute('select authz.nexloop_service_identity(%s,%s)', (digest(token), world)).fetchone()[0]  # noqa: E731
    assert identity(s7['token_a'], s7['a'])['binding']['tenant_id'] == TENANT_A  # positive control
    for world in ('real', 'test', s7['b']):
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='service authentication denied'), s7['admin'].transaction():
            identity(s7['token_a'], world)


SWITCH = """insert into control.nexloop_real_dispatch_switches(tenant_id,world,scope_kind,scope_ref,enabled,data_mode,revision,reason,changed_by,intent_id)
    values(%s,%s,'tenant','*',false,'real',1,'synthetic','synthetic-owner',%s)"""


def test_at046_the_real_effect_path_refuses_a_sim_world_and_the_stand_in_serves_it(s7):
    f = s7
    admin = f['admin']
    with admin.transaction():  # positive control: a real row passes the CHECK
        admin.execute('set local session_replication_role=replica')
        admin.execute(SWITCH, (TENANT_A, 'real', 'i-real'))
    with pytest.raises(psycopg.errors.CheckViolation, match='nx041_real_effect_world'), admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute(SWITCH, (TENANT_B, f['a'], 'i-sim'))
    # The stand-in, from the same sim credential: accepted and executed, a simulated receipt.
    body = {'intent_id': str(uuid.uuid4()), 'action_name': 'nexloop.outreach.send', 'action_version': 1, 'provider_kind': 'outreach_outbox',
            'business_digest': 'b' * 64, 'request_digest': 'c' * 64, 'origin': 'twin'}
    with f['api']() as db:
        assert db.execute('select authz.nexloop_sim_effect_submit(%s,%s,%s)', (digest(f['token_a']), f['a'], Jsonb(body))).fetchone()[0]['simulated'] is True
    [receipt] = S.StandInExecutor(f['stand_in'], f['token_a'], f['a']).run_once()
    assert receipt.simulated and receipt.output_kind == 'simulated_output' and receipt.status == 'fulfilled'
    # The real outbound entry refuses the twin role (no grant) and the sim credential in its own world.
    with f['twin']() as db, pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute('select authz.nexloop_outbound_message_command(%s,%s,%s,%s,%s)', (digest(f['token_a']), f['a'], '{}', '', '{}'))
    with f['api']() as db, db.transaction(), pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute("select set_config('nexloop.real_dispatch_enabled','true',true)")
        db.execute('select authz.nexloop_outbound_message_command(%s,%s,%s,%s,%s)',
                   (digest(f['token_a']), f['a'], '{"tenant_id":"%s","principal_id":"x"}' % TENANT_A, '', '{"verb":"admit"}'))


def test_at046_simulated_outputs_never_become_facts_or_fulfilled_commitments(s7):
    """L6 S4 review 5.2 (rest), stated as the code has it: a Commitment exists only as registered from a commitment Claim (Claims need
    a real-only conversation) and becomes fulfilled only with ledger evidence (runtime.nexloop_commitment_evidence: effect_fulfilled,
    commercial_event, operator_attestation, delivered_message); the guard refuses a fulfilled status without evidence, and the effect
    evidence source (effect intents) refuses a simulation row."""
    admin = s7['admin']
    src = admin.execute("select prosrc from pg_proc where oid='runtime.nexloop_commitment_target(text,text,text,jsonb,timestamptz)'::regprocedure").fetchone()[0]
    assert "'fulfilled'" in src and 'runtime.nexloop_commitment_evidence' in src
    assert "x.kind in ('effect_fulfilled','commercial_event','operator_attestation')" in src
    guard = admin.execute("select prosrc from pg_proc where oid='ontology.nexloop_commitment_guard()'::regprocedure").fetchone()[0]
    assert 'commitment transition is not justified by its evidence' in guard
    conv = [c for (c,) in admin.execute("""select pg_get_constraintdef(oid) from pg_constraint where conrelid='runtime.nexloop_conversations'::regclass and contype='c'""")]
    assert any("world = 'real'" in c for c in conv)
    assert admin.execute("""select count(*) from pg_constraint where conname='nx041_real_effect_world' and conrelid in
        ('runtime.nexloop_effect_intents'::regclass,'runtime.nexloop_outbound_messages'::regclass)""").fetchone()[0] == 2
    # A Commitment object written straight into a simulation world with a fulfilled status is refused by the guard.
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='commitment creation was not prepared from its evidence'), admin.transaction():
        admin.execute("""insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
            values(%s,%s,'Commitment',%s,1,%s,'twin','synthetic',now(),now())""",
                      (TENANT_A, s7['a'], uuid.uuid4().hex, Jsonb({'status': 'fulfilled', 'made_to': 'f' * 64})))
    assert admin.execute("select count(*) from ontology.objects where type_name='Commitment'").fetchone()[0] == 0


@pytest.mark.xfail(strict=True, raises=SystemExit, reason=(
    'D10 pending (owner): one or three credentials per simulation world is not decided, so no simulation process group (runtime '
    'worker, guard, Host) can run a complete Pi Run yet; the runtime worker still accepts real and test only (AT-046). When D10 is '
    'decided and the worker gains its simulation option, this test passes and strict xfail fails, forcing the end-to-end test.'))
def test_at046_a_simulation_process_group_runs_a_complete_pi_run(s7, tmp_path):
    from nexloop_eios import runtime_worker
    paths = {o: str(tmp_path / o) for o in ('database-url-file', 'signing-key-file', 'service-credential-file', 'artifact-root', 'host-control-key-file',
                                           'host-ca-file', 'guard-key-file', 'guard-certificate-file', 'guard-tls-key-file')}
    argv = [x for o, p in paths.items() for x in ('--' + o, p)] + ['--world', s7['a'], '--queue', 'q', '--host-origin', 'https://127.0.0.1:1', '--guard-port', '1']
    runtime_worker._arguments(argv)
    raise AssertionError('the simulation runtime worker exists: replace this placeholder with the end-to-end Pi Run of the sim group')


# 3. §8 proposed acceptances ---------------------------------------------------------------------------------------------------------

def test_acceptance_stand_ins_make_no_external_call(s7, monkeypatch):
    """§8 'stand-in zero external calls': every stand-in of the catalog runs through the executor with Python-level outbound
    connections recorded; none is attempted (the executor's database connection is libpq, not a Python socket). A real-world
    intent is refused by the stand-in."""
    f = s7
    attempts = []
    real_connect, real_create = socket.socket.connect, socket.create_connection
    monkeypatch.setattr(socket.socket, 'connect', lambda self, address: (attempts.append(address), real_connect(self, address))[1])
    monkeypatch.setattr(socket, 'create_connection', lambda address, *a, **k: (attempts.append(address), real_create(address, *a, **k))[1])
    catalog = f['admin'].execute('select action_name,provider_kind from control.nexloop_stand_ins order by 1,2').fetchall()
    assert len(catalog) == 4
    for n, (action, provider) in enumerate(catalog, 1):
        # one business intent per Action (L6 S4 fix: one open intent per business digest)
        body = {'intent_id': str(uuid.uuid4()), 'action_name': action, 'action_version': 1, 'provider_kind': provider, 'business_digest': f'{n:064x}',
                'request_digest': 'c' * 64, 'origin': 'injected'}
        with f['api']() as db:
            db.execute('select authz.nexloop_sim_effect_submit(%s,%s,%s)', (digest(f['token_a']), f['a'], Jsonb(body)))
    receipts = S.StandInExecutor(f['stand_in'], f['token_a'], f['a']).run_once()
    assert len(receipts) == 4 and all(r.simulated for r in receipts) and attempts == []
    with f['stand_in']() as db, pytest.raises(psycopg.errors.InsufficientPrivilege, match='real_world_refused'):
        db.execute('select authz.nexloop_sim_effect_execute(%s,%s,%s)', (digest(f['token_real']), 'real', uuid.uuid4()))


def world_rows(admin, world):
    return {t: n for (t,) in admin.execute('select t::text from runtime.nexloop_sim_world_tables() t').fetchall()
            for n in [admin.execute(f'select count(*) from {t} where world=%s', (world,)).fetchone()[0]] if n}


def test_acceptance_whole_deletion_of_a_simulation_world(s7):
    """§8 'simulation world whole deletion': generated data, port events and observations, stand-in intents and receipts all leave
    with the world; real and the other simulation world are untouched."""
    f = s7
    admin = f['admin']
    generate(admin, f['b'], seed='other')
    twin = P.TwinStub(seed='exp-del')
    twin.run(port(f), consumers(admin, f['a']), 2)
    twin.run(port(f, f['b'], f['token_b']), consumers(admin, f['b']), 1)
    admin.execute('select runtime.nexloop_sim_consumer_offer(%s,%s,%s,%s,%s)', (TENANT_A, f['a'], consumers(admin, f['a'])[0], 'outreach', Jsonb({})))
    with f['api']() as db:
        db.execute('select authz.nexloop_sim_effect_submit(%s,%s,%s)', (digest(f['token_a']), f['a'], Jsonb({
            'intent_id': str(uuid.uuid4()), 'action_name': 'nexloop.service.request', 'action_version': 1, 'provider_kind': 'http',
            'business_digest': 'b' * 64, 'request_digest': 'c' * 64, 'origin': 'twin'})))
    S.StandInExecutor(f['stand_in'], f['token_a'], f['a']).run_once()
    before_a, before_b, before_real = world_rows(admin, f['a']), world_rows(admin, f['b']), world_rows(admin, 'real')
    assert {'runtime.nexloop_sim_consumer_events', 'runtime.nexloop_sim_consumer_observations', 'runtime.nexloop_sim_effect_receipts',
            'runtime.nexloop_sim_objects'} <= set(before_a)
    result = governed(admin, 'nexloop_governed_delete_sim_world', {'operation': 'delete_sim_world', 'request_id': uuid.uuid4().hex, 'world': f['a'], 'reason': '实验结束'})
    for _ in range(10):
        if result['status'] == 'deleted':
            break
        result = admin.execute('select runtime.nexloop_sim_world_purge(%s,%s,1000)', (TENANT_A, f['a'])).fetchone()[0]
    assert result['status'] == 'deleted', result
    assert world_rows(admin, f['a']) == {} and world_rows(admin, f['b']) == before_b
    after_real = world_rows(admin, 'real')
    assert {t: after_real.get(t, 0) for t in before_real} == before_real  # real rows kept (the deletion adds only its own records)


def run_frozen_input(f, world, token, seed='frozen-x'):
    """One run of a frozen input: generation (seed, parameters), stand-in configuration, twin (version, seed) and an injection plan
    (two steps per Consumer, one stand-in intent per Consumer)."""
    admin = f['admin']
    gen = generate(admin, world, seed=seed)
    admin.execute("select runtime.nexloop_stand_in_configure(%s,%s,%s,'frozen-input')", (TENANT_A, world, Jsonb({'fail_rate': 0, 'latency_ms': {'min': 0, 'max': 0}})))
    cs = consumers(admin, world)
    events = P.TwinStub(seed='twin-' + seed).run(P.ConsumerPort(f['twin'], token, world), cs, 2)
    for i, c in enumerate(cs):
        with f['api']() as db:
            db.execute('select authz.nexloop_sim_effect_submit(%s,%s,%s)', (digest(token), world, Jsonb({
                'intent_id': str(uuid.UUID(hashlib.sha256(f'{seed}|{i}'.encode()).hexdigest()[:32])), 'action_name': 'nexloop.outreach.send',
                'action_version': 1, 'provider_kind': 'outreach_outbox', 'business_digest': hashlib.sha256(c.encode()).hexdigest(),
                'request_digest': 'c' * 64, 'origin': 'injected'})))
    receipts = sorted(S.StandInExecutor(f['stand_in'], token, world).run_once(), key=lambda r: r.intent_id)
    return {'generation': (gen['output_digest'], gen['params_digest'], gen['counts']),
            'events': [(e['request']['consumer_id'], e['request']['event_kind'], e['frozen']['content_digest'], e['frozen']['sequence']) for e in events],
            'receipts': [(r.intent_id, r.status, r.latency_ms, r.stand_in_version, r.simulated) for r in receipts],
            'metrics': admin.execute("""select count(*),count(distinct consumer_id),string_agg(event_kind,',' order by consumer_id,sequence)
                from runtime.nexloop_sim_consumer_events where world=%s""", (world,)).fetchone()}


def test_acceptance_a_frozen_input_reproduces(s7):
    """§8 'frozen input reproducible': the same frozen input in two worlds gives byte-identical generated data, twin events and event
    metrics, and the same stand-in receipts (status, latency, version). The receipts' external reference is not compared: since the S4
    review (4.2) each intent has a server salt, so it is not reproducible across runs (decision A: the byte-for-byte receipt test below waits for L6's outcome_seed in 0262)."""
    f = s7
    admin = f['admin']
    c, d = create_world(admin), create_world(admin)
    tc, _ = seed_authority(admin, TENANT_A, 'eios:artifact:' + 'e' * 32, world=c, identity_suffix='-s7-c')
    td, _ = seed_authority(admin, TENANT_A, 'eios:artifact:' + 'f' * 32, world=d, identity_suffix='-s7-d')
    one, two = run_frozen_input(f, c, tc), run_frozen_input(f, d, td)
    assert one == two and len(one['events']) == 6 and len(one['receipts']) == 3
    # Positive control: another frozen input (another seed) gives other events.
    e = create_world(admin)
    te, _ = seed_authority(admin, TENANT_A, 'eios:artifact:' + '9' * 32, world=e, identity_suffix='-s7-e')
    assert run_frozen_input(f, e, te, seed='frozen-y')['events'] != one['events']


def test_the_port_module_has_no_network_and_no_action():
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(P))
    imported = {a.name.split('.')[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split('.')[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not imported & {'socket', 'ssl', 'http', 'urllib', 'urllib3', 'requests', 'httpx', 'aiohttp'}
    assert not any(isinstance(n, ast.ImportFrom) and n.module and n.module.startswith('nexloop_eios.') and n.module not in ('nexloop_eios.worlds',)
                   for n in ast.walk(tree))
    same = [P.TwinStub(seed='s').act('a' * 64, step) for step in range(1, 21)]
    assert same == [P.TwinStub(seed='s').act('a' * 64, step) for step in range(1, 21)]
    assert same != [P.TwinStub(seed='t').act('a' * 64, step) for step in range(1, 21)]
    assert {e['event_kind'] for e in same} <= set(P.EVENT_KINDS) and all(PORT_CONTRACT.is_valid({'schema': 'nexloop-consumer-port-event/1', 'request': e}) for e in same)


def test_acceptance_a_frozen_input_reproduces_the_stand_in_receipts_byte_for_byte(s7):
    """§8 'frozen input reproducible', decision A: with the frozen stand-in configuration copied by the server (outcome_seed included,
    never seen or given by the caller), the same intents give byte-identical receipts, external reference included."""
    f = s7
    admin = f['admin']
    c, d = create_world(admin), create_world(admin)
    tc, _ = seed_authority(admin, TENANT_A, 'eios:artifact:' + '7' * 32, world=c, identity_suffix='-s7-ra')
    td, _ = seed_authority(admin, TENANT_A, 'eios:artifact:' + '8' * 32, world=d, identity_suffix='-s7-rb')
    admin.execute("select runtime.nexloop_stand_in_configure(%s,%s,%s,'frozen-input')", (TENANT_A, c, Jsonb({'fail_rate': 0.5})))
    # Decision A at integration: L6's copy entry (0262) copies the frozen configuration with its server-generated outcome_seed.
    admin.execute("select runtime.nexloop_stand_in_configure_copy(%s,%s,%s,'frozen-input')", (TENANT_A, c, d))
    receipts = {}
    for world, token in ((c, tc), (d, td)):
        for i in range(8):
            with f['api']() as db:
                db.execute('select authz.nexloop_sim_effect_submit(%s,%s,%s)', (digest(token), world, Jsonb({
                    'intent_id': str(uuid.UUID(int=i + 1)), 'action_name': 'nexloop.service.request', 'action_version': 1, 'provider_kind': 'http',
                    # one business intent each (L6 S4 fix: one open intent per business digest)
                    'business_digest': f'{i + 1:064x}', 'request_digest': 'c' * 64, 'origin': 'injected'})))
        receipts[world] = sorted((r.intent_id, r.status, r.external_ref, r.latency_ms) for r in S.StandInExecutor(f['stand_in'], token, world).run_once())
    assert receipts[c] == receipts[d] and {s for _, s, _, _ in receipts[c]} == {'failed', 'fulfilled'}
