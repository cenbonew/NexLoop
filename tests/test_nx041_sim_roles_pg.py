"""NX-041 S6c (design D3; L6 nx041-s6-review 1.2; plan option b): the simulation process group's own database roles.

- nexloop_sim_worker, nexloop_sim_guard and nexloop_sim_api are login roles with no privilege and no membership: none inherits a
  real grant. Each executes exactly the security definer functions of its list (the whitelist below); a new grant fails this test
  until the list is changed on purpose (to be corrected by a track_functions trace once D10 is decided).
- In the functions that check the caller's role by name, a simulation role counts as its real counterpart for a sim:/shadow: world
  only; for real, test or any other world every check refuses. Real roles behave exactly as before.
- The runtime worker and its guard connect as their own simulation roles in the simulation group (the guard in child processes
  with its own DSN); the real group is unchanged.
"""
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from nexloop_eios import runtime_guard_worker, runtime_worker
from nexloop_eios.assembly import APPLICATION_ROLES, RUNTIME_GUARD_ROLES, RUNTIME_WORKER_ROLES
from nexloop_eios.bootstrap import bootstrap

ROOT = Path(__file__).resolve().parents[1]
COMMON = {'authz.nexloop_service_identity_snapshot(text,text)', 'authz.nexloop_load_authority_facts(text,text,jsonb)',
          'authz.nexloop_load_authority_fact_snapshot(text,text,text,text[])', 'authz.nexloop_verify_backend_signer(text,text,text)',
          'authz.nexloop_record_process_sample(text,text,text,timestamp with time zone,integer,jsonb)',
          'runtime.nexloop_erasure_permits(text,text,text,jsonb,jsonb)'}  # granted to public (0129): true only for the owner's own transaction
WHITELIST = {
    'nexloop_sim_worker': COMMON | {'authz.nexloop_queue_command(text,text,text,text,text)', 'authz.nexloop_runtime_activation_command(text,text,text,text,text)',
                                    'authz.nexloop_runtime_activation_hint(text,text,text)', 'authz.nexloop_run_tool_timing(text,text,uuid)'},
    'nexloop_sim_guard': COMMON | {'authz.nexloop_runtime_activation_command(text,text,text,text,text)', 'authz.nexloop_runtime_activation_hint(text,text,text)',
                                   'authz.nexloop_record_run_tool_timing(text,text,uuid,numeric)', 'authz.nexloop_sim_effect_submit(text,text,jsonb)',
                                   'authz.nexloop_sim_effect_query(text,text,uuid)'},
    'nexloop_sim_api': COMMON | {'authz.nexloop_queue_command(text,text,text,text,text)', 'authz.nexloop_runtime_activation_command(text,text,text,text,text)',
                                 'authz.nexloop_issue_run_credential(text,text,text,text)',
                                 'authz.nexloop_sim_context_command(text,text,text,integer)'},  # the Host's simulation context (after S6c)
}
SIM = 'sim:' + str(uuid.uuid4())
SAMPLE = ('host', 'agent-host', 'sim-1', {'active_runs': 0})


@pytest.fixture
def db(admin, pg):
    bootstrap(admin)
    return admin, pg


def connect(pg, role):
    return psycopg.connect(make_conninfo(pg, user=role), autocommit=True)


def test_the_simulation_roles_hold_no_privilege_membership_or_real_grant_and_exactly_their_whitelist(db):
    admin, _ = db
    for role, expected in WHITELIST.items():
        attrs = admin.execute("""select rolcanlogin,rolsuper,rolcreaterole,rolcreatedb,rolreplication,rolbypassrls,rolinherit,
            (select count(*) from pg_auth_members m where m.member=r.oid) from pg_roles r where rolname=%s""", (role,)).fetchone()
        assert attrs == (True, False, False, False, False, False, False, 0), role
        executable = {r[0] for r in admin.execute("""select p.oid::regprocedure::text from pg_proc p join pg_namespace n on n.oid=p.pronamespace
            where n.nspname in ('authz','control','runtime','ontology','extensions','eios') and p.prosecdef and has_function_privilege(%s,p.oid,'execute')""",
                                                  (role,)).fetchall()}
        assert executable == expected, (role, sorted(executable ^ expected))
        # No table, sequence or view of the application schemas is directly readable or writable.
        assert admin.execute("""select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace where n.nspname in ('authz','control','runtime','ontology')
            and c.relkind in ('r','p','v','m','S') and (has_table_privilege(%s,c.oid,'select') or has_table_privilege(%s,c.oid,'insert'))""",
                             (role, role)).fetchone()[0] == 0
    assert {'nexloop_sim_worker', 'nexloop_sim_guard', 'nexloop_sim_api'} <= APPLICATION_ROLES
    # The real roles' grants are untouched by these roles: nothing the real worker executes is new to it here.
    assert admin.execute("select has_function_privilege('nexloop_domain_worker','authz.nexloop_app_role(text)','execute')").fetchone()[0] is False


def test_a_simulation_role_counts_as_its_real_counterpart_for_a_simulation_world_only(db):
    _, pg = db
    with connect(pg, 'nexloop_sim_worker') as c:
        # Real or test world: the role check itself refuses (before any credential lookup).
        for world in ('real', 'test', 'simulation'):
            with pytest.raises(psycopg.errors.InsufficientPrivilege, match='tool timing unavailable'):
                c.execute('select authz.nexloop_run_tool_timing(%s,%s,%s)', ('0' * 64, world, uuid.uuid4()))
        # A simulation world: the role check passes and the credential check refuses the bogus digest.
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='authentication denied'):
            c.execute('select authz.nexloop_run_tool_timing(%s,%s,%s)', ('0' * 64, SIM, uuid.uuid4()))
        c.execute('select authz.nexloop_record_process_sample(%s,%s,%s,now(),60,%s)', (*SAMPLE[:3], psycopg.types.json.Jsonb(SAMPLE[3])))
        for name in ('authz.nexloop_record_run_tool_timing(%s,%s,%s,1)', 'authz.nexloop_sim_effect_submit(%s,%s,%s)'):  # not on its list
            with pytest.raises(psycopg.errors.InsufficientPrivilege, match='permission denied'):
                c.execute('select ' + name, ('0' * 64, SIM, uuid.uuid4() if 'timing' in name else psycopg.types.json.Jsonb({})))
    with connect(pg, 'nexloop_sim_guard') as c:
        for world in ('real', 'test'):
            with pytest.raises(psycopg.errors.InsufficientPrivilege, match='tool timing unavailable'):
                c.execute('select authz.nexloop_record_run_tool_timing(%s,%s,%s,1)', ('0' * 64, world, uuid.uuid4()))
            with pytest.raises(psycopg.errors.InsufficientPrivilege, match='stand-in: unavailable'):
                c.execute('select authz.nexloop_sim_effect_query(%s,%s,%s)', ('0' * 64, world, uuid.uuid4()))
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='authentication denied'):
            c.execute('select authz.nexloop_record_run_tool_timing(%s,%s,%s,1)', ('0' * 64, SIM, uuid.uuid4()))
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='permission denied'):
            c.execute("select authz.nexloop_queue_command('x','x','x','x','x')")  # the guard never touches the queue
    for role in ('nexloop_sim_worker', 'nexloop_sim_guard', 'nexloop_sim_api'):
        with connect(pg, role) as c:
            for fn in ("authz.nexloop_real_dispatch_summary()", "authz.nexloop_service_readiness()", "authz.nexloop_restore_readiness()"):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    c.execute('select ' + fn)


def test_the_real_roles_are_unchanged(db):
    _, pg = db
    with connect(pg, 'nexloop_domain_worker') as c:
        # The role check passes in every world as before; the credential check refuses the bogus digest.
        for world in ('real', 'test', SIM):
            with pytest.raises(psycopg.errors.InsufficientPrivilege, match='authentication denied'):
                c.execute('select authz.nexloop_run_tool_timing(%s,%s,%s)', ('0' * 64, world, uuid.uuid4()))
        c.execute('select authz.nexloop_record_process_sample(%s,%s,%s,now(),60,%s)', (*SAMPLE[:3], psycopg.types.json.Jsonb(SAMPLE[3])))
    with connect(pg, 'nexloop_api') as c, pytest.raises(psycopg.errors.InsufficientPrivilege, match='tool timing unavailable|permission denied'):
        c.execute('select authz.nexloop_run_tool_timing(%s,%s,%s)', ('0' * 64, 'real', uuid.uuid4()))


WORKER = ['--database-url-file', '/nonexistent/dsn', '--signing-key-file', '/nonexistent/k', '--service-credential-file', '/nonexistent/c',
          '--artifact-root', '/nonexistent/a', '--host-control-key-file', '/nonexistent/h', '--host-ca-file', '/nonexistent/ca',
          '--guard-key-file', '/nonexistent/g', '--guard-certificate-file', '/nonexistent/gc', '--guard-tls-key-file', '/nonexistent/gt',
          '--queue', 'operations', '--host-origin', 'https://127.0.0.1:20000', '--guard-port', '20001']


def test_the_simulation_runtime_worker_needs_its_guard_dsn_and_guard_children_the_real_one_never_takes_them():
    sim = ['--world', SIM, '--world-group', 'simulation']
    args = runtime_worker._arguments(WORKER + sim + ['--guard-database-url-file', '/nonexistent/guard', '--guard-workers', '2'])
    assert (str(args.guard_database_url_file), args.guard_workers) == ('/nonexistent/guard', 2)
    assert runtime_worker._arguments(WORKER + ['--world', 'real']).guard_database_url_file is None
    for bad in (sim, sim + ['--guard-database-url-file', '/x'], sim + ['--guard-workers', '2'],
                ['--world', 'real', '--guard-database-url-file', '/x', '--guard-workers', '2']):
        with pytest.raises(SystemExit) as refused:
            runtime_worker._arguments(WORKER + bad)
        assert refused.value.code == 2, bad
    assert RUNTIME_WORKER_ROLES == {'real': {'nexloop_domain_worker', 'nexloop_scheduler'}, 'simulation': {'nexloop_sim_worker'}}
    assert RUNTIME_GUARD_ROLES == {'real': {'nexloop_domain_worker', 'nexloop_scheduler'}, 'simulation': {'nexloop_sim_guard'}}
    assert runtime_guard_worker.GuardFiles.__dataclass_fields__['world_group'].default == 'real'


def test_the_simulation_compose_project_uses_only_simulation_roles():
    import yaml
    project = yaml.safe_load((ROOT / 'deploy/stage/compose.simulation.example.yaml').read_text())
    worker = project['services']['sim-runtime-worker']
    command = worker['command']
    assert command[command.index('--database-url-file') + 1] == '/run/secrets/sim_worker_dsn'
    assert command[command.index('--guard-database-url-file') + 1] == '/run/secrets/sim_guard_dsn'
    assert int(command[command.index('--guard-workers') + 1]) >= 2
    dsns = {name for service in project['services'].values() for name in service.get('secrets', []) if name.endswith('_dsn')}
    assert dsns == {'sim_worker_dsn', 'sim_guard_dsn', 'sim_stand_in_worker_dsn', 'sim_files_dsn'}


ROLE_CHECKED = ('authz.nexloop_queue_command_before_refusals_v0150', 'authz.nexloop_runtime_activation_command_v0037', 'authz.nexloop_runtime_activation_hint',
                'authz.nexloop_run_tool_timing', 'authz.nexloop_record_run_tool_timing', 'authz.nexloop_record_process_sample',
                'authz.nexloop_sim_effect_submit', 'authz.nexloop_sim_effect_query')


def test_the_role_checked_functions_differ_from_their_latest_earlier_definition_only_in_the_role_check():
    """Integration guard (as S5's): each of the eight bodies, authz.nexloop_app_role(...) read back as session_user, equals the latest
    earlier definition at the integration head (a function created by a rename: the renamed function's last body). A branch that
    changes one of them earlier (as L6's 0262 review fixes did for the stand-in submit) makes this fail until the body is re-copied;
    later migrations that redefine one keep the role check."""
    import re
    from pathlib import Path
    migrations = sorted((Path(__file__).resolve().parents[1] / 'packages/eios-core/src/eios/migrations').glob('0*.sql'))
    own = next(m for m in migrations if m.name.endswith('_nx041_sim_roles.sql'))
    defines = lambda name: re.compile(r'create\s+(?:or\s+replace\s+)?function\s+' + re.escape(name) + r'\s*\(', re.I)  # noqa: E731

    def body(source, start):
        m = re.compile(r'\bas\s+(\$[A-Za-z_]*\$)', re.I).search(source, start)
        return source[m.end():source.index(m.group(1), m.end())]

    def latest(name, before, position=None):
        """The last body of name before migration `before` (and before `position` inside it, if given), following a rename back to the
        function it was renamed from."""
        short = name.split('.')[1]
        for m in reversed([m for m in migrations if m.name < before or (position is not None and m.name == before)]):
            text = m.read_text()
            limit = position if m.name == before else len(text)
            events = [(e.start(), 'define', e) for e in defines(name).finditer(text) if e.start() < limit]
            events += [(e.start(), 'rename', e) for e in re.finditer(r'alter\s+function\s+(authz\.[a-z0-9_]+)\s*\([^)]*\)\s*rename\s+to\s+' + short + r'\b', text, re.I)
                       if e.start() < limit]
            if events:
                _, kind, e = max(events, key=lambda x: x[0])
                return body(text, e.end()) if kind == 'define' else latest(e.group(1).lower(), m.name, e.start())
        raise AssertionError(f'{name}: no earlier definition')

    text = own.read_text()
    # L6's 0262 review fixes stay in the stand-in submit as S6c redefines it: N4 (one open intent per business digest) and the frozen
    # outcome seed (decision A).
    submit = body(text, defines('authz.nexloop_sim_effect_submit').search(text).end())
    assert "'stand-in: business_intent_open'" in submit and 'outcome_seed' in submit and 'v_seed' in submit
    for name in ROLE_CHECKED:
        converted = re.sub(r'authz\.nexloop_app_role\((?:p_world|null)\)', 'session_user', body(text, defines(name).search(text).end()))
        assert 'nexloop_app_role' in body(text, defines(name).search(text).end()), name
        assert converted == latest(name, own.name), f'{name}: differs from its latest earlier definition beyond the role check'
        for m in (m for m in migrations if m.name > own.name):
            for again in defines(name).finditer(m.read_text()):
                assert 'nexloop_app_role' in body(m.read_text(), again.end()), (name, m.name)
    # The Host's simulation context (after S6c): S7b's entry with the same single change, from S7b's latest body.
    host = next(m for m in migrations if m.name.endswith('_nx041_host_sim_context.sql'))
    name = 'authz.nexloop_sim_context_command'
    redefined = body(host.read_text(), defines(name).search(host.read_text()).end())
    assert 'nexloop_app_role' in redefined
    assert re.sub(r'authz\.nexloop_app_role\(p_world\)', 'session_user', redefined) == latest(name, host.name), name
