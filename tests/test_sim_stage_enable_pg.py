"""Simulation stage enablement tools, on PostgreSQL and on files:
- scripts/deploy/sim_world_credentials.py: one simulation world's credentials per use through the trusted configuration (sim_use set,
  bound to the world alone, a rerun writes nothing), the self-check (own world yes, real and test refused), the twin use refused by this
  release, the dry run writes nothing; no token in the output.
- scripts/deploy/sim_dsn_files.py: the DSN files 0600 from stdin passwords; the dry run reads no password and writes nothing; a password
  is never in the output; the configuration is checked.
"""
import io
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import sim_dsn_files as D  # noqa: E402
import sim_world_credentials as C  # noqa: E402

from test_service_grants_pg import TENANT, deployment  # noqa: E402,F401


def private(path, value):
    path.write_text(value if isinstance(value, str) else json.dumps(value))
    path.chmod(0o600)
    return path


def world_of(admin):
    admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (TENANT,))
    return admin.execute("select control.nexloop_governed_create_sim_world(%s,'real','owner-principal','owner-subject','i-enable',%s)",
                         (TENANT, Jsonb({'operation': 'create_sim_world', 'request_id': uuid.uuid4().hex, 'kind': 'simulation',
                                         'purpose': 'stage enablement', 'expires_in_days': 7}))).fetchone()[0]['world']


def credentials_config(d, tmp_path, world, uses=('api', 'worker', 'agent'), **over):
    over = dict(over)
    uses = over.pop('uses', uses)
    return private(tmp_path / 'sim-credentials.json', {'schema': C.SCHEMA, 'tenant_id': TENANT, 'world': world, 'uses': list(uses),
                   'token_dir': str(tmp_path / 'tokens'), 'configurator_database_url_file': str(d['paths']['dsn']),
                   'signing_key_file': str(d['paths']['signing']), 'signing_key_id': 'nx048-deploy',
                   'credential_expires_at': (datetime.now(UTC) + timedelta(days=30)).isoformat(), 'check_database_url_file': str(private(tmp_path / 'check-dsn', d['pg'])), **over})


def run(main, argv, **kw):
    out = io.StringIO()
    code = main(argv, out=out, **kw)
    return code, json.loads(out.getvalue())


def test_the_world_credentials_are_issued_per_use_bound_to_the_world_and_self_checked(deployment, admin, tmp_path):  # noqa: F811
    d = deployment
    d['apply']()  # the tenant's real service credentials: the self-check's positive control
    world = world_of(admin)
    conf = credentials_config(d, tmp_path, world, compose_secret_dir=str((tmp_path / 'compose').mkdir(mode=0o700) or tmp_path / 'compose'))
    check = lambda: psycopg.connect(d['pg'], autocommit=True, options='-c default_transaction_read_only=on')  # noqa: E731 - the read-only inspection DSN
    code, out = run(C.main, ['--config', str(conf), '--dry-run'], check_connect=check)
    assert code == 0 and out['credentials_to_create'] == ['agent', 'api', 'worker'] and not (tmp_path / 'tokens').exists()
    assert admin.execute('select count(*) from authz.nexloop_service_credentials where worlds=%s', ([world],)).fetchone()[0] == 0
    code, out = run(C.main, ['--config', str(conf)], check_connect=check)
    assert code == 0 and out['changed'] is True and out['credentials_created'] == ['agent', 'api', 'worker'], out
    items = {'row_found', 'worlds_exactly_this_world', 'sim_use', 'status_active', 'grants_minimal', 'own_world', 'refused_in_real', 'refused_in_test'}
    checked = out['check']['credentials']
    assert out['check']['pass'] is True and all(checked[u] == dict.fromkeys(items, True) for u in ('api', 'worker', 'agent')), out
    assert checked['_world'] == {'one_active_per_use': True, 'no_other_credential': True} and checked['_control'] == {'real_credential_accepted_in_real': True}
    rows = admin.execute('select sim_use,worlds,status from authz.nexloop_service_credentials where worlds=%s order by sim_use', ([world],)).fetchall()
    assert rows == [(u, [world], 'active') for u in ('agent', 'api', 'worker')]
    tokens = {p.name: p.read_text() for p in (tmp_path / 'tokens').rglob('*-credential')}
    assert len(tokens) == 3 and all(p.stat().st_mode & 0o777 == 0o600 for p in (tmp_path / 'tokens').rglob('*-credential'))
    assert (tmp_path / 'compose' / 'sim_world_credential').read_text() == tokens['worker-credential']
    text = json.dumps(out)
    assert not any(t in text for t in tokens.values())
    # The api credential holds the queue and effect grants, the worker the queue, the agent the effect (facts of the world's principals).
    granted = dict(admin.execute("""select entity_key[1], count(*) from authz.nexloop_authority_facts where tenant_id=%s and fact_kind='grants'
        and entity_key[1] like 'nexloop-sim-%%' and jsonb_array_length(payload->'grants')>0 group by 1""", (TENANT,)).fetchall())
    assert sorted(granted.values()) == [1, 1, 2] and all(k.endswith(('-api-principal', '-worker-principal', '-agent-principal')) for k in granted)
    # A rerun writes nothing; --check alone passes.
    code, again = run(C.main, ['--config', str(conf)], check_connect=check)
    assert code == 0 and again['changed'] is False
    code, checked = run(C.main, ['--config', str(conf), '--check'], check_connect=check)
    assert code == 0 and checked['ok'] is True
    # Real credentials are untouched: none carries a use.
    assert admin.execute("select count(*) from authz.nexloop_service_credentials where 'real'=any(worlds) and sim_use is not null").fetchone()[0] == 0
    # Without an active real credential the positive control fails, so the check fails (a refusal alone proves nothing).
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("update authz.nexloop_service_credentials set status='revoked' where worlds=array['real']")
    code, checked = run(C.main, ['--config', str(conf), '--check'], check_connect=check)
    assert code == 1 and checked['ok'] is False and checked['check']['credentials']['_control'] == {'real_credential_accepted_in_real': False}
    # A credential whose grants differ from the minimal list fails its item.
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("update authz.nexloop_service_credentials set status='active' where worlds=array['real']")
        admin.execute("""update authz.nexloop_authority_facts set payload=jsonb_set(payload,'{grants}','[]') where tenant_id=%s and fact_kind='grants'
            and entity_key[1] like 'nexloop-sim-%%-agent-principal'""", (TENANT,))
    code, checked = run(C.main, ['--config', str(conf), '--check'], check_connect=check)
    assert code == 1 and checked['check']['credentials']['agent']['grants_minimal'] is False and checked['check']['credentials']['api']['grants_minimal'] is True


def test_the_twin_use_is_issued_since_the_eighth_batch_and_bad_input_is_refused(deployment, admin, tmp_path):  # noqa: F811
    """The eighth batch accepts the twin use (NX-042 T3): its credential is bound to the world alone with sim_use twin and holds no
    grant (the port checks the world credential); the self-check passes for all four uses. Bad input is refused before anything is written."""
    d = deployment
    d['apply']()
    world = world_of(admin)
    assert 'twin' in C.accepted_uses()
    conf = credentials_config(d, tmp_path, world, uses=('api', 'worker', 'agent', 'twin'))
    code, out = run(C.main, ['--config', str(conf)])
    assert code == 0 and out['credentials_created'] == ['agent', 'api', 'twin', 'worker'] and out['check']['pass'] is True, out
    assert out['check']['credentials']['twin']['grants_minimal'] is True and out['check']['credentials']['twin']['refused_in_real'] is True
    assert admin.execute("select worlds,status from authz.nexloop_service_credentials where sim_use='twin'").fetchall() == [([world], 'active')]
    before = admin.execute('select count(*) from authz.nexloop_service_credentials').fetchone()[0]
    assert run(C.main, ['--config', str(credentials_config(d, tmp_path, 'real'))])[0] == 2
    for over in ({'uses': ['api', 'api']}, {'uses': ['api', 'oracle']}, {'credential_expires_at': '2020-01-01T00:00:00+00:00'}, {'extra': 1}):
        assert run(C.main, ['--config', str(credentials_config(d, tmp_path, world, **over))])[0] == 2
    assert admin.execute('select count(*) from authz.nexloop_service_credentials').fetchone()[0] == before

ROLES = sorted(D.SIM_DSN_ROLES.values())


def dsn_config(tmp_path, **over):
    secret = tmp_path / 'sim-secrets'
    secret.mkdir(mode=0o700, exist_ok=True)
    return private(tmp_path / 'dsn.json', {'schema': D.SCHEMA, 'secret_dir': str(secret), 'data_host_name': 'data.stage.invalid',
                                            'data_host_addr': '192.0.2.20', 'pg_port': 55432, 'db_name': 'nexloop_stage', **over})


class NoStdin:
    def read(self, *a):
        raise AssertionError('the dry run reads no password')


def test_the_dsn_files_are_written_0600_from_stdin_and_never_print_a_password(tmp_path):
    conf = dsn_config(tmp_path)
    code, out = run(D.main, ['--config', str(conf), '--dry-run'], stdin=NoStdin())
    assert code == 0 and [f['role'] for f in out['files']] == [D.SIM_DSN_ROLES[f['file']] for f in out['files']] and not any((tmp_path / 'sim-secrets').iterdir())
    passwords = {r: f"zz'pw\\{i}-" + 'x' * 20 for i, r in enumerate(ROLES)}
    lines = ''.join(f'{r} {p}\n' for r, p in passwords.items())
    code, out = run(D.main, ['--config', str(conf)], stdin=io.StringIO(lines))
    assert code == 0 and out['written'] == len(ROLES)
    assert not any(p in json.dumps(out) for p in passwords.values())
    for name, role in D.SIM_DSN_ROLES.items():
        f = tmp_path / 'sim-secrets' / name
        text = f.read_text()
        assert f.stat().st_mode & 0o777 == 0o600 and f'user={role} ' in text and 'sslmode=verify-full sslrootcert=/run/secrets/sim_pg_ca' in text
        parsed = psycopg.conninfo.conninfo_to_dict(text.strip())
        assert parsed['password'] == passwords[role] and parsed['dbname'] == 'nexloop_stage'
    # A rerun replaces the files atomically; the check hook receives the files and names, never prints a DSN.
    seen = {}
    code, out = run(D.main, ['--config', str(conf), '--check'], stdin=io.StringIO(lines),
                    check=lambda dsns, required: seen.update(n=sorted(dsns), r=required) or {'pass': True})
    assert code == 0 and out['check'] == {'pass': True} and seen['n'] == sorted(D.SIM_DSN_ROLES) and 'password' not in json.dumps(out)
    # Review 3.2: a check that raises reports its type only, never a message that may hold a value.
    def broken(dsns, required):
        raise psycopg.OperationalError('connection failed: password=' + passwords[ROLES[0]])
    code, out = run(D.main, ['--config', str(conf), '--check'], stdin=io.StringIO(lines), check=broken)
    assert code == 1 and out == {'ok': False, 'error': 'OperationalError'}


@pytest.mark.parametrize('lines,detail', [('', 'no password line'), ('nexloop_api ' + 'x' * 20 + '\n', 'each simulation role once'),
                                          ('nexloop_sim_worker short\n', '16-512')])
def test_password_lines_are_checked(tmp_path, lines, detail):
    code, out = run(D.main, ['--config', str(dsn_config(tmp_path))], stdin=io.StringIO(lines))
    assert code == 2 and detail in out['detail'] and not any((tmp_path / 'sim-secrets').iterdir())


def test_dsn_configuration_and_example(tmp_path):
    for over in ({'secret_dir': 'relative'}, {'pg_port': 0}, {'db_name': 'Bad;db'}, {'extra': 1}):
        assert run(D.main, ['--config', str(dsn_config(tmp_path, **over)), '--dry-run'])[0] == 2
    assert D.load_config((ROOT / 'deploy/examples/sim-dsn-files.example.json').read_text())['db_name'] == 'nexloop_stage'
    example = json.loads((ROOT / 'deploy/examples/sim-world-credentials.example.json').read_text())
    assert set(example) == C.KEYS and example['uses'] == ['api', 'worker', 'agent']  # DATA_HOST issuing: no compose secret there
    assert 'host=/var/run/postgresql' in example['configurator_database_url_file'] and 'host=/var/run/postgresql' in example['check_database_url_file']
    install = C.load_install((ROOT / 'deploy/examples/sim-world-credentials-install.example.json').read_text())
    assert install['token_dir'] == '/sim-tokens' and install['compose_secret_dir'] == '/sim-secrets'


def test_after_the_world_is_deleted_its_old_credentials_are_refused(deployment, admin, tmp_path):  # noqa: F811
    """L6 sim-catalog-publish review: the whole deletion revokes the world's credentials before its definitions go, so a token issued for
    the world is refused afterwards: the identity snapshot with 42501 in the deleted world, real and test; the stand-in tenant entry and
    the Action authority entry are refused as well (42501 or 22023). These are the entries covered here, not every entry. The revocation
    advanced the authority revision."""
    d = deployment
    d['apply']()
    world = world_of(admin)
    conf = credentials_config(d, tmp_path, world)
    check = lambda: psycopg.connect(d['pg'], autocommit=True, options='-c default_transaction_read_only=on')  # noqa: E731
    code, out = run(C.main, ['--config', str(conf)], check_connect=check)
    assert code == 0 and out['check']['pass'] is True, out
    import hashlib
    digests = [hashlib.sha256(p.read_text().encode()).hexdigest() for p in (tmp_path / 'tokens').rglob('*-credential')]
    revision = lambda: admin.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s', (TENANT,)).fetchone()[0]  # noqa: E731
    before = revision()
    gone = admin.execute("select control.nexloop_governed_delete_sim_world(%s,'real','owner-principal','owner-subject','i-delete',%s)",
                         (TENANT, Jsonb({'operation': 'delete_sim_world', 'request_id': uuid.uuid4().hex, 'world': world, 'reason': '结束'}))).fetchone()[0]
    assert gone['status'] == 'deleted', gone
    assert revision() > before
    with check() as db:
        for digest in digests:
            for w in (world, 'real', 'test'):
                with pytest.raises(psycopg.Error) as refused, db.transaction():
                    db.execute('select authz.nexloop_service_identity_snapshot(%s,%s)', (digest, w))
                assert refused.value.sqlstate == '42501', (w, refused.value.sqlstate)
            # Review A2: the other entries tried here refuse too (42501, or 22023 where the world's state is checked first).
            for sql, args in (('select authz.nexloop_sim_effect_tenant(%s,%s)', (digest, world)),
                              ('select authz.nexloop_assert_action_authority(%s,%s,%s)', (digest, world, Jsonb({})))):
                with pytest.raises(psycopg.Error) as refused, admin.transaction():  # a read-write session (the entries lock rows)
                    admin.execute(sql, args)
                assert refused.value.sqlstate in ('42501', '22023'), (sql, refused.value.sqlstate)
    code, checked = run(C.main, ['--config', str(conf), '--check'], check_connect=check)
    assert code == 1 and checked['ok'] is False


def test_issuing_reaches_the_database_over_the_socket_only_and_the_tokens_move_by_pipe(deployment, admin, tmp_path):  # noqa: F811
    """L6 review B2.1: the configurator and the bootstrap inspection role have local pg_hba rules only, so issuing and the self-check
    run on DATA_HOST over the socket directory; a TCP DSN in either file is refused before any connection or write. --export writes
    '<use> <token>' lines to a pipe only (never a terminal); --install on APP_HOST reads them on stdin and writes the token files and
    the compose sim_world_credential without any database; a different token for an existing file is refused."""
    d = deployment
    d['apply']()
    world = world_of(admin)
    assert d['pg'].split('host=')[1].startswith('/')  # the test cluster listens on a socket only, as DATA_HOST's local rules
    tcp = private(tmp_path / 'tcp-dsn', 'host=127.0.0.1 port=5432 dbname=nexloop_stage user=nexloop_configurator')
    for key in ('configurator_database_url_file', 'check_database_url_file'):
        conf = credentials_config(d, tmp_path, world, **{key: str(tcp)})
        code, out = run(C.main, ['--config', str(conf)])
        assert code == 2 and 'socket' in out['detail'], (key, out)
    assert admin.execute('select count(*) from authz.nexloop_service_credentials where worlds=%s', ([world],)).fetchone()[0] == 0
    conf = credentials_config(d, tmp_path, world)
    code, out = run(C.main, ['--config', str(conf)])
    assert code == 0 and out['check']['pass'] is True, out
    # Export: a FIFO only (review 2.2) - a captured stream, a terminal or a file is refused; the tokens go into the pipe.
    for sink in (io.StringIO(), open(tmp_path / 'export-file', 'w')):
        with sink:
            assert C.main(['--config', str(conf), '--export'], out=sink) == 2
    assert 'agent ' not in (tmp_path / 'export-file').read_text()
    import os
    r, w = os.pipe()
    with os.fdopen(w, 'w') as writer:
        assert C.main(['--config', str(conf), '--export'], out=writer) == 0
    with os.fdopen(r) as reader:
        lines = reader.read()
    assert sorted(line.split(' ')[0] for line in lines.splitlines()) == ['agent', 'api', 'worker']
    # Install on the other host: no database, files 0600, the worker token as the compose secret; a rerun with the same tokens is fine.
    (tmp_path / 'app-secrets').mkdir(mode=0o700)
    inst = private(tmp_path / 'install.json', {'schema': C.INSTALL_SCHEMA, 'world': world, 'uses': ['api', 'worker', 'agent'],
                                               'token_dir': str(tmp_path / 'app-tokens'), 'compose_secret_dir': str(tmp_path / 'app-secrets')})
    code, out = run(C.main, ['--install', str(inst)], stdin=io.StringIO(lines))
    assert code == 0 and out == {'ok': True, 'installed': ['agent', 'api', 'worker'], 'compose_sim_world_credential': True}
    worker = dict(line.split(' ') for line in lines.splitlines())['worker']
    assert (tmp_path / 'app-secrets' / 'sim_world_credential').read_text() == worker and worker not in json.dumps(out)
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in (tmp_path / 'app-tokens').rglob('*-credential'))
    code, out = run(C.main, ['--install', str(inst)], stdin=io.StringIO(lines))
    assert code == 0 and out['installed'] == []
    other = lines.replace(worker, 'Z' * 64)
    code, out = run(C.main, ['--install', str(inst)], stdin=io.StringIO(other))
    assert code == 2 and 'another token' in out['detail'] and 'Z' * 64 not in json.dumps(out)
    code, out = run(C.main, ['--install', str(inst)], stdin=io.StringIO('worker ' + worker + '\n'))
    assert code == 2 and 'missing' in out['detail']
    # Review 2.1: once installed, DATA_HOST forgets its copy; a later export has nothing to send.
    code, out = run(C.main, ['--config', str(conf), '--forget'])
    assert code == 0 and out == {'ok': True, 'forgotten': ['agent', 'api', 'worker']} and not list((tmp_path / 'tokens').rglob('*-credential'))
    r, w = os.pipe()
    with os.fdopen(w, 'w') as writer:
        assert C.main(['--config', str(conf), '--export'], out=writer) == 2
    os.close(r)


def test_the_documented_steps_name_their_host_and_connection():
    """Review B2.1: the step table names, for each step, the host and the connection; issuing is on DATA_HOST over the socket and
    nothing in the table sends the configurator or the bootstrap role over hostssl."""
    doc = (ROOT / 'docs/implementation/sim-stage-enable.md').read_text()
    rows = [line for line in doc.splitlines() if line.startswith('| ') and line.split('|')[1].strip().isdigit()]
    assert len(rows) >= 11 and all(('APP_HOST' in r or 'DATA_HOST' in r) and ('socket' in r or 'hostssl' in r or '不连数据库' in r) for r in rows), rows
    issue = next(r for r in rows if 'sim_world_credentials.py' in r and '签发' in r)
    assert 'DATA_HOST' in issue and 'socket' in issue and 'compose.sim-issue' in issue
    compose = (ROOT / 'deploy/stage/compose.sim-issue.example.yaml').read_text()
    assert '/var/run/postgresql' in compose and 'network_mode: none' in compose
    assert "'999" not in compose and '${NEXLOOP_PG_UID}:${NEXLOOP_PG_GID}' in compose  # review 3.1: the socket owner's uid is a variable
    for part in ('probe-before', 'probe-after'):  # review 1.1: the AT-046 probe parts on DATA_HOST, the intake on APP_HOST
        assert 'DATA_HOST' in next(r for r in rows if part in r)
    assert 'APP_HOST' in next(r for r in rows if ' intake ' in r) and '工作台 HTTPS' not in next(r for r in rows if ' intake ' in r)
