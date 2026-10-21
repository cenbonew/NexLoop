"""NX-041 S7 (design §5, §8, §11 S7): the consumer port, a deterministic twin stub, the complete AT-046 negatives (each with its
positive control) and the three acceptances §8 proposes (stand-ins make no external call, whole deletion, frozen input reproducible).

Real PostgreSQL on a clean catalog. Worlds are created through the owner's governed handler (S2), synthetic Consumers through the
generator (S3); the port is called as its own role nexloop_twin_port with the world's one credential, the stand-in as
nexloop_stand_in_worker (S4). The Host group check of AT-046 runs in apps/agent-host/test/nx041-host-group.test.ts.
"""
import hashlib
import json
from datetime import UTC, datetime, timedelta
import os
import re
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
    # D10 extended (owner 2026-10-21): each world's twin credential, the only one the twin port entries (NX-042 T3) accept.
    token_twin_a, _ = seed_authority(admin, TENANT_A, 'eios:artifact:' + 'e' * 32, world=a, identity_suffix='-s7-twin-a', sim_use='twin')
    token_twin_b, _ = seed_authority(admin, TENANT_A, 'eios:artifact:' + 'f' * 32, world=b, identity_suffix='-s7-twin-b', sim_use='twin')
    generate(admin, a)
    role = lambda name: (lambda: psycopg.connect(make_conninfo(pg, user=name), autocommit=True))  # noqa: E731
    return {'admin': admin, 'pg': pg, 'a': a, 'b': b, 'token_a': token_a, 'token_b': token_b, 'token_real': token_real, 'token_test': token_test,
            'token_twin_a': token_twin_a, 'token_twin_b': token_twin_b,
            'twin': role('nexloop_twin_port'), 'stand_in': role('nexloop_stand_in_worker'), 'api': role('nexloop_api'), 'role': role}


def grant_raw_act(admin):
    """Test database only (NX-042 T3 decision): the port role no longer executes the raw S7 entry - a twin's events go through
    authz.nexloop_twin_act with provenance. Tests of the raw entry's own rules (validation, server time, ceiling, replay) and tests
    that need the stub's exact content grant it here for their own database."""
    admin.execute('grant execute on function authz.nexloop_sim_consumer_act(text,text,jsonb) to nexloop_twin_port')


@pytest.fixture
def s7_raw(s7):
    grant_raw_act(s7['admin'])
    return s7


def port(f, world=None, token=None):
    return P.ConsumerPort(f['twin'], token or f['token_a'], world or f['a'])


# 1. The consumer port --------------------------------------------------------------------------------------------------------------

def test_the_twin_acts_through_the_port_only_with_server_time_and_simulation_marks(s7_raw):
    f = s7_raw
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


def test_the_port_refuses_real_test_unknown_consumers_client_time_and_other_roles(s7_raw):
    f = s7_raw
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
    # NX-042 T3: the twin's own entries (bind, observe, act); the raw act is revoked - a twin's event always has its provenance.
    assert reachable == {'authz.nexloop_sim_consumer_observe', 'runtime.nexloop_erasure_permits',
                         'authz.nexloop_twin_bind', 'authz.nexloop_twin_observe', 'authz.nexloop_twin_act'}, reachable
    assert not s7['admin'].execute("select has_function_privilege('nexloop_twin_port','authz.nexloop_sim_consumer_act(text,text,jsonb)','execute')").fetchone()[0]
    with pytest.raises(psycopg.errors.InsufficientPrivilege), s7['twin']() as db:
        db.execute('select authz.nexloop_sim_consumer_act(%s,%s,%s)', (digest(s7['token_a']), s7['a'], Jsonb(P.TwinStub(seed='x').act(consumers(s7['admin'], s7['a'])[0], 1))))
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
    """S7 review 5 (behaviour, not source text): every kind of Commitment evidence the ledger knows is refused in a simulation world,
    whichever writer (effect intents, commercial records in data_mode test, the owner's attestation, delivered messages) would add it,
    so a Commitment there is never fulfilled; the same rows are accepted in real (positive control). A fulfilled Commitment written
    straight into a simulation world is refused, and its status target stays unfulfilled without evidence."""
    admin = s7['admin']
    # By its name (fix verify 5): the kind CHECK of the evidence ledger (0111, inline column check).
    [definition] = admin.execute("""select pg_get_constraintdef(oid) from pg_constraint where conrelid='runtime.nexloop_commitment_evidence'::regclass
        and conname='nexloop_commitment_evidence_kind_check'""").fetchall()[0]
    kinds = re.findall(r"'([a-z_]+)'::text", definition)
    assert {'effect_fulfilled', 'commercial_event', 'operator_attestation', 'delivered_message'} <= set(kinds)
    commitment = uuid.uuid4().hex * 2
    insert = """insert into runtime.nexloop_commitment_evidence(tenant_id,world,commitment_id,kind,ref,occurred_at,recorded_by)
        values(%s,%s,%s,%s,%s,now(),'writer')"""
    for kind in kinds:
        with admin.transaction(force_rollback=True):
            admin.execute(insert, (TENANT_A, 'real', commitment, kind, 'ref:' + kind))  # positive control (rolled back)
        for world in (s7['a'], s7['b']):
            with pytest.raises(psycopg.errors.CheckViolation, match='nx041_no_simulation_evidence'), admin.transaction():
                admin.execute(insert, (TENANT_A, world, commitment, kind, 'ref:' + kind))
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='commitment creation was not prepared from its evidence'), admin.transaction():
        admin.execute("""insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
            values(%s,%s,'Commitment',%s,1,%s,'twin','synthetic',now(),now())""",
                      (TENANT_A, s7['a'], uuid.uuid4().hex, Jsonb({'status': 'fulfilled', 'made_to': 'f' * 64})))
    target = admin.execute('select runtime.nexloop_commitment_target(%s,%s,%s,%s,now())', (TENANT_A, s7['a'], commitment, Jsonb({
        'status': 'open', 'fulfillment_basis': 'undetermined', 'promised_at': '2026-01-01T00:00:00Z', 'due_at': None}))).fetchone()[0]
    assert target.get('status') != 'fulfilled'


class PiRunBlocked(Exception):
    """A prerequisite of a complete Pi Run in a simulation process group is not there yet."""


def pi_run_blockers(f):
    """The prerequisites of a complete sim-group Pi Run, each probed in the system as it is (S7 review 4):
    - D10 in its decided form (owner, as recommended): the world takes three credentials, one per use (api, worker, agent), each bound
      to it alone - probed by issuing the worker and agent credentials beside the world's api credential (rolled back);
    - S6c: the simulation process group's own database roles, all three (nexloop_sim_worker, nexloop_sim_guard, nexloop_sim_api);
    - the Host's simulation Context protocol: its module exports the parser of nexloop.context-pack.sim.v1, parseSimContextPack."""
    admin = f['admin']
    blockers = []
    row = admin.execute("select tenant_id,binding,audience,expires_at from authz.nexloop_service_credentials where worlds=%s and status='active'", ([f['a']],)).fetchone()
    try:
        with admin.transaction(force_rollback=True):
            for use in ('worker', 'agent'):
                binding = {**row[1], 'credential_id': 'd10-probe-' + use, 'principal_id': 'd10-probe-' + use, 'subject_principal_id': 'd10-probe-' + use}
                admin.execute('insert into authz.nexloop_service_credentials(token_digest,tenant_id,credential_id,binding,worlds,audience,status,expires_at,sim_use) values(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                              (uuid.uuid4().hex * 2, row[0], 'd10-probe-' + use, Jsonb(binding), [f['a']], row[2], 'active', row[3], use))
    except (psycopg.errors.InsufficientPrivilege, psycopg.errors.UndefinedColumn, psycopg.errors.UniqueViolation) as error:
        blockers.append('D10: three credentials per simulation world (one per use) not in place: ' + str(error).splitlines()[0])
    if admin.execute("select count(*) from pg_roles where rolname in ('nexloop_sim_worker','nexloop_sim_guard','nexloop_sim_api')").fetchone()[0] < 3:
        blockers.append('S6c: simulation process group roles absent')
    exported = re.compile(r'export\s+(?:async\s+)?function\s+parseSimContextPack\b|export\s+(?:const|let)\s+parseSimContextPack\b|export\s*\{[^}]*\bparseSimContextPack\b[^}]*\}')
    if not any(exported.search(p.read_text()) for p in (ROOT / 'apps/agent-host/src').glob('*.ts')):
        blockers.append('Host: simulation Context protocol absent (no exported parseSimContextPack)')
    return blockers


def complete_sim_pi_run(f, tmp_path, blockers=pi_run_blockers):
    """The end-to-end path of the sim group, as far as it exists: the worker accepts the sim world in the simulation group (S6),
    then every prerequisite. Returns only when nothing blocks (then the end-to-end Run belongs here)."""
    from nexloop_eios import runtime_worker
    paths = {o: str(tmp_path / o) for o in ('database-url-file', 'signing-key-file', 'service-credential-file', 'artifact-root', 'host-control-key-file',
                                           'host-ca-file', 'guard-key-file', 'guard-certificate-file', 'guard-tls-key-file',
                                           'guard-database-url-file')}  # S6c: the simulation guard's own database role
    argv = [x for o, p in paths.items() for x in ('--' + o, p)] + ['--world', f['a'], '--world-group', 'simulation', '--queue', 'q',
                                                                  '--host-origin', 'https://127.0.0.1:1', '--guard-port', '8443',
                                                                  '--guard-workers', '2']  # S6c: a simulation guard runs in child processes
    runtime_worker._arguments(argv)  # S6: the simulation group's worker exists (a SystemExit here is a regression, not a blocker)
    found = blockers(f)
    if found:
        raise PiRunBlocked(found)


def test_at046_every_prerequisite_of_a_simulation_pi_run_is_in_place(s7, tmp_path):
    """The former strict xfail, flipped (D10 decided, 0273): pi_run_blockers finds nothing - three credentials per world, one per use;
    S6c's roles; the Host's simulation Context protocol - and the simulation group's worker accepts its world. The complete Pi Run of
    the simulation process group is tests/test_nx041_sim_pi_run_pg.py::test_at046_a_simulation_process_group_runs_a_complete_pi_run."""
    assert pi_run_blockers(s7) == []
    assert complete_sim_pi_run(s7, tmp_path) is None
    assert (ROOT / 'tests/test_nx041_sim_pi_run_pg.py').read_text().count('def test_at046_a_simulation_process_group_runs_a_complete_pi_run(') == 1


def test_the_blocker_probes_still_detect_a_missing_prerequisite(s7, tmp_path):
    """Positive control of the probes: a stubbed blocker is still raised."""
    with pytest.raises(PiRunBlocked):
        complete_sim_pi_run(s7, tmp_path, blockers=lambda f: ['D10: stubbed'])


def test_at046_forged_context_resume_guard_and_activation_paths(s7):
    """S7 review 2.2, server side (the resume path is in apps/agent-host/test/nx041-host-group.test.ts):
    - Context: the real assembly entry refuses a sim world (its body is real-only) and contract v6 refuses a sim binding; a real binding
      validates (positive control);
    - guard: a guard worker of the simulation group refuses real and test, the real group refuses sim; each accepts its own;
    - SQL activation: an unsigned command is refused at its signature gate, alike for every credential and world (see the comment)."""
    from nexloop_eios import runtime_guard_worker as G
    admin = s7['admin']
    # A well-formed, correctly signed context request (a signing key registered for the test): the sim world is refused at the
    # entry's first gate (real only); the same request in real passes the signature and fails only later, at the assemble authority
    # (positive control, fix verify 4) - so the sim refusal is the world's.
    import hmac
    material = os.urandom(32)
    admin.execute("insert into authz.nexloop_authority_signing_keys values('s7-context-probe',%s,true)", (material,))
    payload = '{}'
    claims = json.dumps({'protocol': 'nexloop-context-v6-v1', 'resource_id': 'eios:action:nexloop.context.assemble:1', 'key_id': 's7-context-probe',
                         'parameters_digest': hashlib.sha256(payload.encode()).hexdigest(),
                         'expires_at': (datetime.now(UTC) + timedelta(seconds=20)).isoformat()}, separators=(',', ':'), sort_keys=True)
    signature = hmac.new(material, ('nexloop-context-v6-v1:' + claims).encode(), 'sha256').hexdigest()
    context = 'select authz.nexloop_context_v6_command(%s,%s,%s,%s,%s)'
    with s7['api']() as db, pytest.raises(psycopg.errors.InsufficientPrivilege, match='context v6 unavailable') as sim_refused:
        db.execute(context, (digest(s7['token_a']), s7['a'], claims, signature, payload))
    with s7['api']() as db, pytest.raises(psycopg.Error) as real_refused:
        db.execute(context, (digest(s7['token_real']), 'real', claims, signature, payload))
    assert 'context v6 unavailable' not in str(real_refused.value), real_refused.value  # it got past the world and the signature
    assert 'nexloop_assert_context_assemble' in str(real_refused.value.diag.context)  # the assemble authority, after the signature
    assert 'nexloop_assert_context_assemble' not in str(sim_refused.value.diag.context)  # the sim request never got that far
    # Test hygiene (fix verify F3): the probe key leaves with the test, whatever the fixture's database lifetime.
    admin.execute("delete from authz.nexloop_authority_signing_keys where key_id='s7-context-probe'")
    assert admin.execute("select count(*) from authz.nexloop_authority_signing_keys where key_id='s7-context-probe'").fetchone()[0] == 0
    v6 = Draft202012Validator(json.loads((ROOT / 'packages/contracts/context-pack-v6.schema.json').read_text()), format_checker=FormatChecker())
    example = json.loads((ROOT / 'packages/contracts/examples/context-pack-v6.valid.json').read_text())
    v6.validate(example)
    forged = json.loads(json.dumps(example))
    forged['bindings']['world_id'] = s7['a']
    assert not v6.is_valid(forged)
    base = ['--listen-fd', '3', '--guard-port', '8443'] + [x for o in ('database-url-file', 'signing-key-file', 'service-credential-file', 'artifact-root',
                                                                     'guard-key-file', 'guard-certificate-file', 'guard-tls-key-file') for x in ('--' + o, '/x')]
    assert G._arguments(base + ['--world', s7['a'], '--world-group', 'simulation']).world == s7['a']
    assert G._arguments(base + ['--world', 'real']).world == 'real'
    for argv in (['--world', 'real', '--world-group', 'simulation'], ['--world', 'test', '--world-group', 'simulation'], ['--world', s7['a']]):
        with pytest.raises(SystemExit):
            G._arguments(base + argv)
    # SQL activation: its first gates are the role, the queue and the command signature (0037), before the credential's world is
    # read; the same unsigned request is refused alike for the sim credential and the real one, so nothing world-specific is
    # reached without a signed command. The world binding is held by the service identity (sim credential refused for real/test,
    # test_at046_a_sim_credential_never_authenticates_real) and by the command check (test_at046_a_forged_mode_is_refused_by_the_server).
    worker = s7['role']('nexloop_domain_worker')
    answers = []
    for token, world in ((s7['token_a'], 'real'), (s7['token_real'], 'real'), (s7['token_a'], s7['a'])):
        with worker() as db, pytest.raises(psycopg.errors.InsufficientPrivilege) as refused:
            db.execute('select authz.nexloop_runtime_activation_command(%s,%s,%s,%s,%s)', (digest(token), world, '{}', '', '{"queue":"q","verb":"create"}'))
        answers.append(refused.value.diag.message_primary)
    assert answers == ['activation denied'] * 3


def test_an_existing_port_role_is_brought_to_its_shape(admin):
    """S7 review 1.4 (as S4 N2): a role named nexloop_twin_port that exists before the migration with other attributes, a role
    membership and a privilege leaves it with exactly the declared shape and its port entries (NX-042 T3: the twin entries, no raw act)."""
    admin.execute('drop role if exists nexloop_twin_port')
    admin.execute('create role nexloop_twin_port nologin inherit createdb bypassrls')
    admin.execute('grant pg_read_all_data to nexloop_twin_port')
    bootstrap(admin)
    r = admin.execute("""select rolcanlogin,rolinherit,rolcreatedb,rolcreaterole,rolsuper,rolreplication,rolbypassrls from pg_roles
        where rolname='nexloop_twin_port'""").fetchone()
    assert r == (True, False, False, False, False, False, False), r
    assert admin.execute("select count(*) from pg_auth_members a join pg_roles m on m.oid=a.member where m.rolname='nexloop_twin_port'").fetchone()[0] == 0
    reachable = {x[0].split('(')[0] for x in admin.execute("""select p.oid::regprocedure::text from pg_proc p join pg_namespace n on n.oid=p.pronamespace
        where n.nspname in ('authz','control','runtime','ontology') and p.prosecdef and has_function_privilege('nexloop_twin_port',p.oid,'execute')""").fetchall()}
    # NX-042 T3: the twin's own entries (bind, observe, act); the raw act is revoked - a twin's event always has its provenance.
    assert reachable == {'authz.nexloop_sim_consumer_observe', 'runtime.nexloop_erasure_permits',
                         'authz.nexloop_twin_bind', 'authz.nexloop_twin_observe', 'authz.nexloop_twin_act'}, reachable
    assert not admin.execute("select has_function_privilege('nexloop_twin_port','authz.nexloop_sim_consumer_act(text,text,jsonb)','execute')").fetchone()[0]


def test_the_port_takes_at_most_its_event_limit_per_world(s7_raw):
    """S7 review 1.5: a world takes at most runtime.nexloop_sim_port_event_limit() events (proposed 100 000); a replay is still
    answered; another world is not affected."""
    f = s7_raw
    admin = f['admin']
    limit = admin.execute('select runtime.nexloop_sim_port_event_limit()').fetchone()[0]
    assert limit == 100000
    c = consumers(admin, f['a'])[0]
    twin = P.TwinStub(seed='quota')
    first = port(f).act(twin.act(c, 1))
    with admin.transaction():  # fill the world to one below the limit, as the port would (triggers off: trusted bulk seed)
        admin.execute('set local session_replication_role=replica')
        admin.execute("""insert into runtime.nexloop_sim_consumer_events(tenant_id,world,event_id,consumer_id,sequence,event_kind,content,content_digest,
            twin_version,twin_seed,twin_step,sim_time,recorded_by)
            select %s,%s,encode(sha256(convert_to('bulk'||i,'UTF8')),'hex'),%s,i+1,'visit','{}',repeat('0',64),'twin-stub/1','bulk',i,now(),'seed'
            from generate_series(1,%s) i""", (TENANT_A, f['a'], c, limit - 2))
        # The port counts its events per world (fix verify N1): the seed sets the count as the port would have.
        admin.execute('update runtime.nexloop_sim_port_counters set events=%s where tenant_id=%s and world=%s', (limit - 1, TENANT_A, f['a']))
    assert port(f).act(twin.act(c, 2))['frozen']['sequence'] == limit  # the last one fits (positive control)
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='quota_exceeded'):
        port(f).act(twin.act(c, 3))
    assert port(f).act(twin.act(c, 1))['frozen']['event_id'] == first['frozen']['event_id']  # a replay is answered
    generate(admin, f['b'], seed='other-quota')
    assert port(f, f['b'], f['token_b']).act(twin.act(consumers(admin, f['b'])[0], 1))['frozen']['sequence'] == 1


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


def test_acceptance_whole_deletion_of_a_simulation_world(s7_raw):
    """§8 'simulation world whole deletion': generated data, port events and observations, stand-in intents and receipts all leave
    with the world; real and the other simulation world are untouched."""
    f = s7_raw
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


def run_frozen_input(f, world, token, seed='frozen-x', configured_from=None):
    """One run of a frozen input: generation (seed, parameters), stand-in configuration (with its server outcome_seed; a replay
    copies it from the first run by the server, decision A), twin (version, seed) and an injection plan (two steps per Consumer,
    one stand-in intent per Consumer)."""
    admin = f['admin']
    gen = generate(admin, world, seed=seed)
    if configured_from is None:
        admin.execute("select runtime.nexloop_stand_in_configure(%s,%s,%s,'frozen-input')", (TENANT_A, world, Jsonb({'fail_rate': 0.5, 'latency_ms': {'min': 0, 'max': 50}})))
    else:
        admin.execute("select runtime.nexloop_stand_in_configure_copy(%s,%s,%s,'frozen-input')", (TENANT_A, configured_from, world))
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
            'receipts': [json.dumps({k: v for k, v in vars(r).items() if k != 'world'}, sort_keys=True) for r in receipts],
            'metrics': admin.execute("""select count(*),count(distinct consumer_id),string_agg(event_kind,',' order by consumer_id,sequence)
                from runtime.nexloop_sim_consumer_events where world=%s""", (world,)).fetchone()}


def test_acceptance_a_frozen_input_reproduces(s7_raw):
    """§8 'frozen input reproducible': the same frozen input in two worlds gives byte-identical generated data, twin events and event
    metrics, and the same stand-in receipts: every field but the world, external reference included (S7 review 3), with the frozen
    stand-in configuration copied by the server (decision A, outcome_seed)."""
    f = s7_raw
    admin = f['admin']
    c, d = create_world(admin), create_world(admin)
    tc, _ = seed_authority(admin, TENANT_A, 'eios:artifact:' + 'e' * 32, world=c, identity_suffix='-s7-c')
    td, _ = seed_authority(admin, TENANT_A, 'eios:artifact:' + 'f' * 32, world=d, identity_suffix='-s7-d')
    one = run_frozen_input(f, c, tc)
    two = run_frozen_input(f, d, td, configured_from=c)
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


def test_acceptance_a_frozen_input_reproduces_the_stand_in_receipts_byte_for_byte(s7_raw):
    """§8 'frozen input reproducible', decision A: with the frozen stand-in configuration copied by the server (outcome_seed included,
    never seen or given by the caller), the same intents give byte-identical receipts, external reference included."""
    f = s7_raw
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
        # The whole receipt but its world (S7 review 3): status, failure code, external reference, latency, version, marks, counts.
        receipts[world] = sorted((json.dumps({k: v for k, v in vars(r).items() if k != 'world'}, sort_keys=True)
                                  for r in S.StandInExecutor(f['stand_in'], token, world).run_once()))
    assert receipts[c] == receipts[d] and {json.loads(r)['status'] for r in receipts[c]} == {'failed', 'fulfilled'}
    assert all(json.loads(r)['external_ref'].startswith('stand-in:') for r in receipts[c])


def test_the_port_counter_is_backfilled_from_existing_events(s7_raw):
    """Fix verify F2: events that exist before the counter (a split rollout) are counted by the backfill the migration runs; the
    count is exact and the ceiling then holds."""
    f = s7_raw
    admin = f['admin']
    c = consumers(admin, f['a'])[0]
    with admin.transaction():  # events written before any counter existed (triggers off: trusted seed, as a prior rollout)
        admin.execute('set local session_replication_role=replica')
        admin.execute("""insert into runtime.nexloop_sim_consumer_events(tenant_id,world,event_id,consumer_id,sequence,event_kind,content,content_digest,
            twin_version,twin_seed,twin_step,sim_time,recorded_by)
            select %s,%s,encode(sha256(convert_to('early'||i,'UTF8')),'hex'),%s,i,'visit','{}',repeat('0',64),'twin-stub/1','early',i,now(),'seed'
            from generate_series(1,7) i""", (TENANT_A, f['a'], c))
        admin.execute('delete from runtime.nexloop_sim_port_counters where world=%s', (f['a'],))
    assert admin.execute('select runtime.nexloop_sim_port_counters_backfill()').fetchone()[0] >= 1
    count = lambda: admin.execute('select events from runtime.nexloop_sim_port_counters where world=%s', (f['a'],)).fetchone()[0]  # noqa: E731
    assert count() == 7
    assert admin.execute('select runtime.nexloop_sim_port_counters_backfill()').fetchone()[0] >= 1 and count() == 7  # idempotent
    port(f).act(P.TwinStub(seed='after-backfill').act(c, 1))
    assert count() == 8
