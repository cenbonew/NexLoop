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
from psycopg.conninfo import make_conninfo
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
                   'credential_expires_at': (datetime.now(UTC) + timedelta(days=30)).isoformat(), 'check_database_url_file': str(tmp_path / 'unused'), **over})


def run(main, argv, **kw):
    out = io.StringIO()
    code = main(argv, out=out, **kw)
    return code, json.loads(out.getvalue())


def test_the_world_credentials_are_issued_per_use_bound_to_the_world_and_self_checked(deployment, admin, tmp_path):  # noqa: F811
    d = deployment
    admin.execute('alter role nexloop_sim_api login')
    world = world_of(admin)
    conf = credentials_config(d, tmp_path, world, compose_secret_dir=str((tmp_path / 'compose').mkdir(mode=0o700) or tmp_path / 'compose'))
    check = lambda: psycopg.connect(make_conninfo(d['pg'], user='nexloop_sim_api'), autocommit=True)  # noqa: E731
    code, out = run(C.main, ['--config', str(conf), '--dry-run'], check_connect=check)
    assert code == 0 and out['credentials_to_create'] == ['agent', 'api', 'worker'] and not (tmp_path / 'tokens').exists()
    assert admin.execute('select count(*) from authz.nexloop_service_credentials where worlds=%s', ([world],)).fetchone()[0] == 0
    code, out = run(C.main, ['--config', str(conf)], check_connect=check)
    assert code == 0 and out['changed'] is True and out['credentials_created'] == ['agent', 'api', 'worker'], out
    assert out['check']['pass'] is True and all(v == {'own_world': True, 'refused_in_real': True, 'refused_in_test': True} for v in out['check']['credentials'].values())
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


def test_the_twin_use_is_refused_by_this_release_and_bad_input_too(deployment, admin, tmp_path):  # noqa: F811
    d = deployment
    world = world_of(admin)
    code, out = run(C.main, ['--config', str(credentials_config(d, tmp_path, world, uses=('api', 'twin')))])
    assert code == 2 and 'twin' in out['detail'] and not (tmp_path / 'tokens').exists()
    assert run(C.main, ['--config', str(credentials_config(d, tmp_path, 'real'))])[0] == 2
    for over in ({'uses': ['api', 'api']}, {'credential_expires_at': '2020-01-01T00:00:00+00:00'}, {'extra': 1}):
        assert run(C.main, ['--config', str(credentials_config(d, tmp_path, world, **over))])[0] == 2


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
    assert set(example) == C.KEYS | C.OPTIONAL and example['uses'] == ['api', 'worker', 'agent']
