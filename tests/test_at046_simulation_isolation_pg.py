"""AT-046 (ROADMAP §10), v0.1 regression negatives on clean catalog PostgreSQL: a simulation (or shadow) world cannot borrow a
real effect credential, and a caller that forges the world is refused at every entry that can produce an external effect,
even with all three real dispatch layers open. Credentials come from the repository service-grants manifest applied through
trusted configuration (as deployed). No provider, channel or ledger is reached in any case: every refusal happens in the
database or in argument parsing before a credential file is opened."""
import hashlib
import json
import uuid

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from nexloop_eios.authorization import authenticate_service
from test_service_grants_pg import TENANT, deployment, private  # noqa: F401

FORGED = ('simulation', 'shadow')
# Entry, connecting role, and the refusal message when the world check is the first one the entry makes.
ENTRIES = [
    ('authz.nexloop_local_delivery_authority', 'nexloop_action_worker', 'local delivery denied'),
    ('authz.nexloop_message_run_issuance_command', 'nexloop_api', 'message relay unavailable'),
    ('authz.nexloop_outbound_message_command', 'nexloop_api', 'outbound message unavailable'),
    ('authz.nexloop_effect_execution_command', 'nexloop_action_worker', None),
    ('authz.nexloop_effect_intent_command', 'nexloop_api', None),
    ('authz.nexloop_incentive_command', 'nexloop_domain_worker', None),
]


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def tokens(d, role):
    return [d['tokens'][p['credential_reference']] for p in d['manifest']['principals'] if p['database_role'] == role]


@pytest.mark.parametrize('world', FORGED)
def test_a_real_credential_never_authenticates_in_a_simulation_world(deployment, world):  # noqa: F811
    d = deployment
    d['apply']()
    for principal in d['manifest']['principals']:
        pool = d['pools'].get(principal['database_role'])
        if pool is None:
            continue
        token = d['tokens'][principal['credential_reference']]
        assert authenticate_service(pool, token, world='real') is not None
        with pytest.raises(Exception, match='service authentication denied|denied'):
            authenticate_service(pool, token, world=world)


def test_a_credential_holding_real_holds_only_real(deployment, admin):  # noqa: F811
    """0320: the same token cannot serve real and another world, so no simulation process can carry a real credential."""
    d = deployment
    d['apply']()
    row = admin.execute("select tenant_id,credential_id,binding,audience,expires_at from authz.nexloop_service_credentials where worlds=array['real'] limit 1").fetchone()
    for worlds in (['real', 'simulation'], ['simulation', 'real'], ['real', 'shadow'], ['real', 'test']):
        mixed = 'mixed-' + uuid.uuid4().hex
        with pytest.raises(psycopg.errors.CheckViolation, match='real_alone'), admin.transaction():
            admin.execute('insert into authz.nexloop_service_credentials(token_digest,tenant_id,credential_id,binding,worlds,audience,status,expires_at) values(%s,%s,%s,%s,%s,%s,%s,%s)',
                          (uuid.uuid4().hex * 2, row[0], mixed, Jsonb({**row[2], 'credential_id': mixed}), worlds, row[3], 'active', row[4]))
        with pytest.raises(psycopg.errors.CheckViolation, match='real_alone'), admin.transaction():
            admin.execute("update authz.nexloop_service_credentials set worlds=%s where credential_id=%s", (worlds, row[1]))
    assert admin.execute("select count(*) from authz.nexloop_service_credentials where 'real'=any(worlds) and cardinality(worlds)>1").fetchone()[0] == 0


@pytest.mark.parametrize('world', FORGED)
def test_every_effect_entry_refuses_a_forged_world_with_every_switch_open(deployment, admin, pg, world):  # noqa: F811
    d = deployment
    d['apply']()
    resource = 'eios:action:nexloop.service.request:1'
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')  # test harness: every switch as if the owner had opened it
        for kind, ref, mode in (('tenant', '*', 'real'), ('action_type', resource, None)):
            admin.execute("""insert into control.nexloop_real_dispatch_switches(tenant_id,world,scope_kind,scope_ref,enabled,data_mode,revision,reason,changed_by,intent_id)
                values(%s,'real',%s,%s,true,%s,1,'synthetic','synthetic-owner','synthetic-intent')""", (TENANT, kind, ref, mode))
    for name, role, message in ENTRIES:
        with psycopg.connect(make_conninfo(pg, user=role), autocommit=True) as db:
            for token in tokens(d, role) or ['unused-' + uuid.uuid4().hex]:
                with db.transaction():
                    db.execute("select set_config('nexloop.real_dispatch_enabled','true',true)")  # deployment layer open
                    with pytest.raises(psycopg.errors.InsufficientPrivilege) as refused:
                        db.execute(f'select {name}(%s,%s,%s,%s,%s)', (digest(token), world, json.dumps({'tenant_id': TENANT, 'principal_id': 'x'}), '', json.dumps({'verb': 'admit'})))
                if message:
                    assert message in str(refused.value), name
    # The decision at dispatch: all layers open in world real, the forged world is closed at the world layer.
    with admin.transaction():
        admin.execute("select set_config('nexloop.real_dispatch_enabled','true',true)")
        assert admin.execute('select control.nexloop_real_dispatch_closed_layer(%s,%s,%s)', (TENANT, 'real', resource)).fetchone()[0] is None
        assert admin.execute('select control.nexloop_real_dispatch_closed_layer(%s,%s,%s)', (TENANT, world, resource)).fetchone()[0] == 'world'
    # Nothing was attempted: no outbox, attempt or delivery row exists.
    for table in ('runtime.nexloop_effect_intents', 'runtime.nexloop_effect_attempts', 'runtime.nexloop_outbound_messages', 'runtime.nexloop_incentive_issues'):
        assert admin.execute(f'select count(*) from {table}').fetchone()[0] == 0, table


def test_the_effect_execution_port_refuses_a_non_real_session_before_any_connection():
    from nexloop_eios.effect_execution import EffectExecutionPort, EffectExecutionUnavailable

    class Pool:
        def connection(self):
            raise AssertionError('no connection may be opened')

    for world in FORGED + ('test',):
        session = type('Session', (), {'run_context': None, 'world': world})()
        with pytest.raises(EffectExecutionUnavailable):
            EffectExecutionPort(Pool(), session, None)


@pytest.mark.parametrize('world', FORGED)
def test_effect_processes_refuse_a_simulation_world_before_reading_any_credential(tmp_path, capsys, world):
    """Argument parsing rejects the world first; the credential paths given do not exist, so any read would fail differently."""
    from nexloop_eios import background_services as B
    from nexloop_eios import effect_worker, outbound_messages, runtime_guard_worker, runtime_worker
    missing = str(tmp_path / 'never-created')
    common = ['--database-url-file', missing, '--signing-key-file', missing, '--service-credential-file', missing, '--world', world]
    for entry in (effect_worker.main, outbound_messages.main):
        with pytest.raises(SystemExit) as exit_:
            entry(common + ['--artifact-root', missing])
        assert exit_.value.code == 2
    for service in ('incentive-issuer', 'commercial-recorder', 'reply-guarantor'):
        with pytest.raises(SystemExit) as exit_:
            B.main_for(service, common + ['--artifact-root', missing, '--once'])
        assert exit_.value.code == 2
    worker = ['--database-url-file', missing, '--signing-key-file', missing, '--service-credential-file', missing, '--artifact-root', missing,
              '--host-control-key-file', missing, '--host-ca-file', missing, '--guard-key-file', missing, '--guard-certificate-file', missing,
              '--guard-tls-key-file', missing, '--world', world, '--queue', 'operations', '--host-origin', 'https://127.0.0.1:1', '--guard-port', '2000']
    with pytest.raises(SystemExit) as exit_:
        runtime_worker.parse(worker) if hasattr(runtime_worker, 'parse') else runtime_worker.main(worker)
    assert exit_.value.code == 2
    with pytest.raises(SystemExit) as exit_:
        runtime_guard_worker.main(['--listen-fd', '3', '--guard-port', '2000', '--world', world, *sum((['--' + o, missing] for o in (
            'database-url-file', 'signing-key-file', 'service-credential-file', 'artifact-root', 'guard-key-file', 'guard-certificate-file', 'guard-tls-key-file')), [])])
    assert exit_.value.code == 2
    assert not (tmp_path / 'never-created').exists()
    capsys.readouterr()
