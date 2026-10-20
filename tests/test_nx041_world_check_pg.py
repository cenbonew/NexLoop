"""NX-041 S1 (design D1, D9, §2.1): one world check. A world is real, test, sim:<uuid> or shadow:<uuid>; 'simulation' is only the cost and
Claim data mode. Negatives for invalid literals, a sim: suffix that is not a uuid, reserved names and 'simulation' used as a world; the
real and test paths keep their exact results (data modes, recall entry, metrics export)."""
import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from authority_fixture import seed_authority
from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.runtime_activation import _command  # noqa: F401 - imported to fail loudly if the activation validator moves
from nexloop_eios.worlds import data_mode, valid_world, world_kind
from support.tenants import SIM_WORLD, TENANT_A

U = '0b8f1c1e-1d2a-4c3b-9d4e-5f6a7b8c9d0e'
VALID = ['real', 'test', 'sim:' + U, 'shadow:' + U]
INVALID = ['simulation', 'shadow', 'sim', 'sim:', 'sim:not-a-uuid', 'sim:' + U.upper(), 'shadow:' + U + 'x', 'Real', 'REAL', 'staging', 'prod',
           'test ', ' real', 'sim:' + U + ':extra', 'shadow-' + U, '', 'sim-1']


def test_the_python_check_mirrors_d1():
    assert all(valid_world(w) for w in VALID) and not any(valid_world(w) for w in INVALID + [None, 1])
    assert [world_kind(w) for w in VALID] == ['real', 'test', 'simulation', 'shadow'] and world_kind('simulation') is None
    assert [data_mode(w) for w in VALID] == ['real', 'test', 'simulation', 'simulation']


def test_the_sql_check_mirrors_d1_and_real_test_modes_are_unchanged(admin):
    bootstrap(admin)
    q = lambda f, w: admin.execute(f'select {f}(%s)', (w,)).fetchone()[0]  # noqa: E731
    assert all(q('runtime.nexloop_world_valid', w) for w in VALID) and not any(q('runtime.nexloop_world_valid', w) for w in INVALID)
    assert q('runtime.nexloop_world_valid', None) is False
    assert [q('runtime.nexloop_world_kind', w) for w in VALID] == ['real', 'test', 'simulation', 'shadow']
    # Costs (0120) and Claims (0155): real and test exactly as before; sim:* and shadow:* map to the data mode simulation.
    assert [q('runtime.nexloop_cost_data_mode', w) for w in VALID] == ['real', 'test', 'simulation', 'simulation']
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='world invalid'), admin.transaction():
        q('runtime.nexloop_assert_world', 'simulation')


def test_simulation_and_other_invalid_worlds_are_refused_where_worlds_are_stored_or_entered(admin):
    bootstrap(admin)
    admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (TENANT_A,))
    # Service credentials (0165 + S1): every world valid; 'simulation' as a world, a bad sim: suffix or a reserved name are refused.
    for world in ('simulation', 'sim:not-a-uuid', 'staging'):
        with pytest.raises(psycopg.errors.CheckViolation), admin.transaction():
            seed_authority(admin, TENANT_A, 'eios:artifact:' + 'e' * 32, world=world, identity_suffix='-bad-' + uuid.uuid4().hex[:6])
    seed_authority(admin, TENANT_A, 'eios:artifact:' + 'e' * 32, world=SIM_WORLD, identity_suffix='-good-sim')  # a valid experiment world
    with pytest.raises(psycopg.errors.CheckViolation), admin.transaction():
        admin.execute("update authz.nexloop_service_credentials set worlds=%s where worlds=%s", (['test', 'shadow'], [SIM_WORLD]))
    # The semantic access record (0234): its world check is the one check now.
    for world in ('simulation', 'sim:1', 'prod'):
        with pytest.raises(psycopg.errors.CheckViolation), admin.transaction():
            admin.execute('set local session_replication_role=replica')
            admin.execute("""insert into runtime.nexloop_semantic_accesses(tenant_id,world,access_id,kind,operation,query_kind,term_hashes,coverage,ambiguity)
                values(%s,%s,%s,'access','browse','any',%s,0,false)""", (TENANT_A, world, uuid.uuid4(), ['a' * 64]))
    # The recall entry (0067): a well-formed but invalid world is refused before any credential lookup.
    for world in ('simulation', 'shadow', 'sim:abc'):
        with pytest.raises(psycopg.errors.InvalidParameterValue, match='recall world invalid'), admin.transaction():
            admin.execute('select ontology.nexloop_recall_tenant(%s,%s)', ('0' * 64, world))


def test_the_metrics_export_lists_real_and_test_only(admin):
    bootstrap(admin)
    src = admin.execute("select pg_get_functiondef('authz.nexloop_metrics_export()'::regprocedure)").fetchone()[0]
    assert "('real'),('test')" in src and "'simulation'" not in src


def test_a_run_command_world_must_be_valid_and_of_its_mode():
    from nexloop_eios import runtime_activation as R
    base = {'schema_version': '1.0', 'run_id': str(uuid.uuid4()), 'tenant_id': TENANT_A, 'world_id': 'real', 'mode': 'real', 'request_id': 'r' * 20,
            'trigger_event_id': str(uuid.uuid4()), 'role_ref': 'role:x', 'consumer_ref': 'consumer:x', 'goal_version_ref': 'goal:x', 'context_manifest_ref': 'artifact:x',
            'runtime_profile': 'p', 'credential_ref': 'run:x', 'budget': {}, 'not_after': '2026-10-20T00:00:00Z', 'runtime_owner_epoch': 1}
    check = getattr(R, '_command', None) or getattr(R, 'validate_run_command')

    def world_ok(world, mode):
        try:
            check({**base, 'world_id': world, 'mode': mode})
        except ValueError as error:
            return str(error) != 'world unavailable'
        return True
    assert world_ok('real', 'real') and world_ok('test', 'test') and world_ok(SIM_WORLD, 'simulation') and world_ok('shadow:' + U, 'shadow')
    for world, mode in (('simulation', 'simulation'), ('sim:1', 'simulation'), ('test', 'real'), ('real', 'test'), (SIM_WORLD, 'test'), ('staging', 'test')):
        assert not world_ok(world, mode), (world, mode)
