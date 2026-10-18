"""AT-046 (ROADMAP §10), v0.1 regression negatives for world 'test', the world local and synthetic stacks really run in (the
runtime worker accepts it): unlike the forged simulation/shadow callers of test_at046_simulation_isolation_pg, a test-world
caller here holds a credential that is genuinely valid in its own world. It still cannot use a real effect credential, and
with its own credential it cannot dispatch a real Action at any effect entry, even with every real dispatch layer open; the
effect processes refuse --world test before reading any credential. Clean catalog PostgreSQL; no provider, channel or ledger
is reached."""
import hashlib
import json
import secrets
import uuid

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from nexloop_eios.authorization import authenticate_service
from test_at046_simulation_isolation_pg import ENTRIES, digest, tokens
from test_service_grants_pg import TENANT, deployment, private  # noqa: F401

WORLD = 'test'
RESOURCE = 'eios:action:nexloop.service.request:1'
EFFECT_TABLES = ('runtime.nexloop_effect_intents', 'runtime.nexloop_effect_attempts', 'runtime.nexloop_outbound_messages',
                 'runtime.nexloop_incentive_issues')


def open_every_real_layer(admin):
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')  # test harness: every switch as if the owner had opened it
        for kind, ref, mode in (('tenant', '*', 'real'), ('action_type', RESOURCE, None)):
            admin.execute("""insert into control.nexloop_real_dispatch_switches(tenant_id,world,scope_kind,scope_ref,enabled,data_mode,revision,reason,changed_by,intent_id)
                values(%s,'real',%s,%s,true,%s,1,'synthetic','synthetic-owner','synthetic-intent')""", (TENANT, kind, ref, mode))


def issue_test_world_credential(admin, role):
    """A credential of the same principal as the role's real one, issued for world 'test' only (allowed by 0165)."""
    row = admin.execute("""select c.tenant_id,c.credential_id,c.binding,c.audience,c.expires_at from authz.nexloop_service_credentials c
        where c.worlds=array['real'] and c.binding->>'database_role'=%s limit 1""", (role,)).fetchone()
    if row is None:
        row = admin.execute("select tenant_id,credential_id,binding,audience,expires_at from authz.nexloop_service_credentials where worlds=array['real'] limit 1").fetchone()
    token, credential = 'test-world-' + secrets.token_hex(24), 'test-world-' + uuid.uuid4().hex
    with admin.transaction():
        admin.execute('insert into authz.nexloop_service_credentials(token_digest,tenant_id,credential_id,binding,worlds,audience,status,expires_at) values(%s,%s,%s,%s,%s,%s,%s,%s)',
                      (digest(token), row[0], credential, Jsonb({**row[2], 'credential_id': credential}), [WORLD], row[3], 'active', row[4]))
    return token


def test_a_real_credential_never_authenticates_in_the_test_world(deployment):  # noqa: F811
    d = deployment
    d['apply']()
    checked = 0
    for principal in d['manifest']['principals']:
        pool = d['pools'].get(principal['database_role'])
        if pool is None:
            continue
        token = d['tokens'][principal['credential_reference']]
        assert authenticate_service(pool, token, world='real') is not None
        with pytest.raises(Exception, match='service authentication denied|denied'):
            authenticate_service(pool, token, world=WORLD)
        checked += 1
    assert checked > 0


def test_a_test_world_credential_cannot_dispatch_a_real_action_at_any_effect_entry(deployment, admin, pg):  # noqa: F811
    d = deployment
    d['apply']()
    open_every_real_layer(admin)
    authenticated = 0
    for name, role, _ in ENTRIES:
        own = issue_test_world_credential(admin, role)
        real = (tokens(d, role) or [None])[0]
        if d['pools'].get(role) is not None:  # the refusal below is not an invalid credential: it authenticates in its own world
            assert authenticate_service(d['pools'][role], own, world=WORLD) is not None
            with pytest.raises(Exception, match='denied'):
                authenticate_service(d['pools'][role], own, world='real')
            authenticated += 1
        with psycopg.connect(make_conninfo(pg, user=role), autocommit=True) as db:
            # its own, genuinely valid test-world credential, in world test and when it claims world real
            for token, world in ((own, WORLD), (own, 'real')) + (((real, WORLD),) if real else ()):
                with db.transaction():
                    db.execute("select set_config('nexloop.real_dispatch_enabled','true',true)")  # deployment layer open
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        db.execute(f'select {name}(%s,%s,%s,%s,%s)', (digest(token), world, json.dumps({'tenant_id': TENANT, 'principal_id': 'x'}),
                                                                     '', json.dumps({'verb': 'admit'})))
    assert authenticated > 0
    with admin.transaction():
        admin.execute("select set_config('nexloop.real_dispatch_enabled','true',true)")
        assert admin.execute('select control.nexloop_real_dispatch_closed_layer(%s,%s,%s)', (TENANT, 'real', RESOURCE)).fetchone()[0] is None
        assert admin.execute('select control.nexloop_real_dispatch_closed_layer(%s,%s,%s)', (TENANT, WORLD, RESOURCE)).fetchone()[0] == 'world'
    for table in EFFECT_TABLES:
        assert admin.execute(f'select count(*) from {table}').fetchone()[0] == 0, table


def test_switch_rows_forced_open_in_the_test_world_still_leave_it_closed(deployment, admin):  # noqa: F811
    """0153: the governed owner Action refuses to enable dispatch outside world real; even rows forced open for world test by
    the harness do not count, because the dispatch decision closes every non-real world at the world layer first."""
    d = deployment
    d['apply']()
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        for kind, ref, mode in (('tenant', '*', 'real'), ('action_type', RESOURCE, None)):
            admin.execute("""insert into control.nexloop_real_dispatch_switches(tenant_id,world,scope_kind,scope_ref,enabled,data_mode,revision,reason,changed_by,intent_id)
                values(%s,%s,%s,%s,true,%s,1,'synthetic','synthetic-owner','synthetic-intent')""", (TENANT, WORLD, kind, ref, mode))
    with admin.transaction():
        admin.execute("select set_config('nexloop.real_dispatch_enabled','true',true)")
        assert admin.execute('select control.nexloop_real_dispatch_closed_layer(%s,%s,%s)', (TENANT, WORLD, RESOURCE)).fetchone()[0] == 'world'
    source = admin.execute("select prosrc from pg_proc where proname='nexloop_governed_set_real_dispatch'").fetchone()[0]
    assert "p_world<>'real' and (body->>'enabled')::boolean then raise exception 'real dispatch exists only in world real'" in source


def test_effect_processes_refuse_the_test_world_before_reading_any_credential(tmp_path, capsys):
    """The processes that produce external effects accept only world real (the runtime worker and guard, which run Runs and
    never hold an effect credential, may run in test)."""
    from nexloop_eios import background_services as B
    from nexloop_eios import effect_worker, outbound_messages
    missing = str(tmp_path / 'never-created')
    common = ['--database-url-file', missing, '--signing-key-file', missing, '--service-credential-file', missing, '--world', WORLD]
    for entry in (effect_worker.main, outbound_messages.main):
        with pytest.raises(SystemExit) as exit_:
            entry(common + ['--artifact-root', missing])
        assert exit_.value.code == 2
    for service in ('incentive-issuer', 'commercial-recorder', 'reply-guarantor'):
        with pytest.raises(SystemExit) as exit_:
            B.main_for(service, common + ['--artifact-root', missing, '--once'])
        assert exit_.value.code == 2
    assert not (tmp_path / 'never-created').exists()
    capsys.readouterr()
