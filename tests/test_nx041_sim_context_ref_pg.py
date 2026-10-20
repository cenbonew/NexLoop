"""NX-041 sim context by reference and the simulation manifest (L6 nx041-host-sim-context-review 4.3).

A simulation Run's input is fixed by SQL (authz.nexloop_sim_context_register, called as nexloop_sim_api with the world's one
credential): the pack S7b's assembler builds in the same transaction, its text and digest bound to the Run, the manifest of its
simulated sources written beside it. The Run registration of a simulation world accepts only that digest, so a pack the caller
assembled itself is refused; real and test registrations are untouched. The bound text is what the Host parses and renders.
Real PostgreSQL on a clean catalog (fixtures from the S7 tests); the Host end is this checkout's built module (Node 24).
"""
import hashlib
import json
import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from nexloop_eios import consumer_port as P
from nexloop_eios.sim_context import register_sim_context
from support.tenants import TENANT_A, TENANT_B
from test_nx041_host_sim_context_pg import command, node_render
from test_nx041_s7_pg import consumers, digest, port, s7  # noqa: F401


def run_credential(admin, world, token):
    """A Run credential of the world issued from its one credential (as authz.nexloop_issue_run_credential leaves it)."""
    source = digest(token)
    run_id = uuid.uuid4()
    admin.execute("""insert into authz.nexloop_run_credentials(token_digest,run_id,source_digest,source_directory_hash,world,audience,allowed_resources,status,issued_at,expires_at)
        values(%s,%s,%s,(select authz.nexloop_root_identity_snapshot(%s,%s)->>'directory_hash'),%s,'nexloop-agent-host',%s,'active',clock_timestamp(),
        clock_timestamp()+interval '200 seconds')""", (hashlib.sha256(uuid.uuid4().bytes).hexdigest(), run_id, source, source, world, world,
                                                        ['eios:action:nexloop.service.request:1']))
    return str(run_id)


def register(f, run, consumer, limit=3, world=None, token=None, role='nexloop_sim_api'):
    with f['role'](role)() as db:
        return register_sim_context(db, digest(token or f['token_a']), world or f['a'], run, consumer, limit)


def bind_run(admin, world, run, input_digest, tenant=TENANT_A):
    """The Run registration row (0037) as the activation entry writes it (other foreign keys off: trusted seed; the input trigger
    fires always)."""
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("""insert into authz.nexloop_runtime_run_bindings(run_id,tenant_id,world,queue,task_id,run_digest,command_digest,input_digest,owner_epoch,
            enrolled_by,created_at) values(%s,%s,%s,'operations',%s,%s,%s,%s,1,'test',now())""",
                      (run, tenant, world, 'task-' + uuid.uuid4().hex, 'f' * 64, 'e' * 64, input_digest))


@pytest.fixture
def ctx(s7):
    f = s7
    c = consumers(f['admin'], f['a'])[0]
    twin = P.TwinStub(seed='ref-ctx')
    events = [port(f).act(twin.act(c, step)) for step in range(1, 5)]
    return {**f, 'c': c, 'events': events}


def test_the_pack_is_assembled_and_fixed_by_reference_and_the_host_reads_it(ctx):
    f = ctx
    run = run_credential(f['admin'], f['a'], f['token_a'])
    text = register(f, run, f['c'])
    pack = json.loads(text)
    row = f['admin'].execute('select pack_text,pack_digest,consumer_id,event_limit,world_time,events_included from runtime.nexloop_sim_context_bindings where run_id=%s',
                             (run,)).fetchone()
    assert row[0] == text and row[1] == hashlib.sha256(text.encode()).hexdigest() and row[2:4] == (f['c'], 3)
    assert row[4] == pack['world_time'] and row[5] == 3 == len(pack['evidence'])
    assert [e['ref'] for e in pack['evidence']] == ['sim:event:' + e['frozen']['event_id'] for e in f['events'][1:]]
    # A repeat returns the fixed text (nothing assembled twice); a later port event does not change it; another Consumer or limit is refused.
    port(f).act(P.TwinStub(seed='ref-ctx').act(f['c'], 5))
    assert register(f, run, f['c']) == text
    for consumer, limit in ((consumers(f['admin'], f['a'])[1], 3), (f['c'], 4)):
        with pytest.raises(psycopg.errors.InvalidParameterValue, match='run_bound'):
            register(f, run, consumer, limit)
    # The Host parses and renders exactly the bound text (its own checks: world, tenant, mode, structure).
    rendered = node_render(text, {**command(f['a'], pack['tenant_id'], consumer=f['c']), 'run_id': run})['ok']
    assert rendered.split('\n')[0].startswith('[模拟环境 SIMULATION]')


def test_a_pack_the_caller_assembled_itself_is_refused_at_the_run_registration(ctx):
    f = ctx
    admin = f['admin']
    run = run_credential(admin, f['a'], f['token_a'])
    text = register(f, run, f['c'])
    forged = json.loads(text)
    forged['evidence'][-1]['content']['text'] = '任意文字：伪造的模拟对话'
    forged_text = json.dumps(forged, ensure_ascii=False)
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='input_not_registered'):
        bind_run(admin, f['a'], run, hashlib.sha256(forged_text.encode()).hexdigest())
    other = run_credential(admin, f['a'], f['token_a'])  # a Run with no pack fixed
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='input_not_registered'):
        bind_run(admin, f['a'], other, hashlib.sha256(text.encode()).hexdigest())
    bind_run(admin, f['a'], run, hashlib.sha256(text.encode()).hexdigest())  # positive control: the fixed pack
    # Real and test registrations are untouched by the rule (positive control of the unchanged real path).
    real = run_credential(admin, 'real', f['token_real'])
    bind_run(admin, 'real', real, 'a' * 64)


def test_only_the_simulation_intake_registers_and_only_its_own_runs(ctx):
    f = ctx
    admin = f['admin']
    run = run_credential(admin, f['a'], f['token_a'])
    for role in ('nexloop_api', 'nexloop_domain_worker', 'nexloop_twin_port', 'nexloop_sim_worker'):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            register(f, run, f['c'], role=role)
    with pytest.raises((psycopg.errors.InsufficientPrivilege, ValueError)):
        register(f, run, f['c'], world='real', token=f['token_real'])
    other_world = run_credential(admin, f['b'], f['token_b'])
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='run_unavailable'):
        register(f, other_world, f['c'])  # a Run of world B with A's credential
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='service authentication denied'):
        register(f, other_world, f['c'], world=f['b'])  # A's credential in world B
    assert register(f, run, f['c'])  # positive control


def test_the_manifest_records_what_the_model_was_given(ctx):
    f = ctx
    run = run_credential(f['admin'], f['a'], f['token_a'])
    pack = json.loads(register(f, run, f['c'], limit=2))
    rows = f['admin'].execute('select ordinal,section,ref,revision,content_hash from runtime.nexloop_sim_context_sources where run_id=%s order by ordinal',
                              (run,)).fetchall()
    expected = [(s['ref'], s['revision'], s['content_hash'], 'consumer_state') for s in pack['consumer_state']] + \
               [(e['ref'], e['revision'], e['content_hash'], 'evidence') for e in pack['evidence']] + \
               [(pack['current_event']['provenance'], str(pack['current_event']['sequence']), None, 'current_event')]
    assert [(r[2], r[3], r[4], r[1]) for r in rows] == expected and [r[0] for r in rows] == list(range(1, len(expected) + 1))
    # Every evidence source of the manifest is a port event of this world with that content (the audit answers 'from which events').
    for ref, revision, content_hash, section in expected:
        if section == 'evidence':
            event_id = ref.removeprefix('sim:event:')
            assert f['admin'].execute('select sequence::text from runtime.nexloop_sim_consumer_events where world=%s and event_id=%s', (f['a'], event_id)).fetchone() == (revision,)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), f['admin'].transaction():  # append-only
        f['admin'].execute("update runtime.nexloop_sim_context_sources set revision='x' where run_id=%s", (run,))


def test_the_whole_deletion_leaves_no_binding_and_no_manifest(ctx):
    f = ctx
    admin = f['admin']
    run = run_credential(admin, f['a'], f['token_a'])
    register(f, run, f['c'])
    other = run_credential(admin, f['b'], f['token_b'])
    from test_nx041_s7_pg import generate
    generate(admin, f['b'], seed='kept')
    port(f, f['b'], f['token_b']).act(P.TwinStub(seed='kept').act(consumers(admin, f['b'])[0], 1))
    register(f, other, consumers(admin, f['b'])[0], world=f['b'], token=f['token_b'])
    result = admin.execute("select control.nexloop_governed_delete_sim_world(%s,'real','owner','owner','i-del',%s)",
                           (TENANT_A, Jsonb({'operation': 'delete_sim_world', 'request_id': uuid.uuid4().hex, 'world': f['a'], 'reason': '实验结束'}))).fetchone()[0]
    for _ in range(10):
        if result['status'] == 'deleted':
            break
        result = admin.execute('select runtime.nexloop_sim_world_purge(%s,%s,1000)', (TENANT_A, f['a'])).fetchone()[0]
    assert result['status'] == 'deleted' and result['blocked'] == {}, result
    for table in ('runtime.nexloop_sim_context_bindings', 'runtime.nexloop_sim_context_sources'):
        assert admin.execute(f'select count(*) from {table} where world=%s', (f['a'],)).fetchone()[0] == 0, table
        assert admin.execute(f'select count(*) from {table} where world=%s', (f['b'],)).fetchone()[0] > 0, table  # the other world kept


# Follow-ups (L6 sim-context-ref review, L2, L4) ------------------------------------------------------------------------------------

ROOT = __import__('pathlib').Path(__file__).resolve().parents[1]
RENDER_GOLDEN = ROOT / 'tests/support/golden/nx041-sim-render.json'


def test_register_requires_the_simulation_intake_itself_even_if_granted_elsewhere(ctx):
    """L2: besides app_role, the entry requires session_user nexloop_sim_api - a grant given to another role by mistake does not open it."""
    f = ctx
    admin = f['admin']
    run = run_credential(admin, f['a'], f['token_a'])
    admin.execute('grant execute on function authz.nexloop_sim_context_register(text,text,uuid,text,integer) to nexloop_api')
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='sim context: unavailable'):
            register(f, run, f['c'], role='nexloop_api')
    finally:
        admin.execute('revoke execute on function authz.nexloop_sim_context_register(text,text,uuid,text,integer) from nexloop_api')
    assert register(f, run, f['c'])  # positive control


def test_the_input_trigger_watches_the_tenant_too(ctx):
    """L6: moving a simulation Run's registration to another tenant is checked like a new one (refused: no pack fixed there)."""
    f = ctx
    admin = f['admin']
    run = run_credential(admin, f['a'], f['token_a'])
    text = register(f, run, f['c'])
    bind_run(admin, f['a'], run, hashlib.sha256(text.encode()).hexdigest())
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='input_not_registered'), admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("update authz.nexloop_runtime_run_bindings set tenant_id=%s where run_id=%s", (TENANT_B, run))
    trigger = admin.execute("""select pg_get_triggerdef(oid) from pg_trigger where tgname='nx041_sim_run_input_fixed'""").fetchone()[0]
    assert 'tenant_id' in trigger and admin.execute("select tgenabled from pg_trigger where tgname='nx041_sim_run_input_fixed'").fetchone()[0] == 'A'


def host_render_version():
    import shutil
    import subprocess
    node = shutil.which('node')
    script = "import {SIM_RENDER_VERSION} from %s;process.stdout.write(SIM_RENDER_VERSION);" % json.dumps((ROOT / 'apps/agent-host/dist/sim-context.js').as_uri())
    return subprocess.run([node, '--input-type=module', '-e', script], capture_output=True, text=True, timeout=30, check=True).stdout


def test_the_render_version_is_recorded_and_tied_to_the_host_rendering(ctx):
    """L6: every binding records the Host render version; SQL's version is the Host's SIM_RENDER_VERSION, and a golden render of the
    contract example under that version ties output and version: a rendering change without a version bump fails here."""
    f = ctx
    run = run_credential(f['admin'], f['a'], f['token_a'])
    register(f, run, f['c'])
    sql_version = f['admin'].execute('select runtime.nexloop_sim_render_version()').fetchone()[0]
    assert f['admin'].execute('select render_version from runtime.nexloop_sim_context_bindings where run_id=%s', (run,)).fetchone()[0] == sql_version
    assert host_render_version() == sql_version
    example = (ROOT / 'packages/contracts/examples/context-pack-sim.valid.json').read_text()
    pack = json.loads(example)
    rendered = node_render(json.dumps(pack, ensure_ascii=False), {**command(pack['world'], pack['tenant_id'], consumer=pack['consumer_id'])})['ok']
    actual = {'render_version': sql_version, 'sha256': hashlib.sha256(rendered.encode()).hexdigest()}
    if __import__('os').environ.get('NEXLOOP_WRITE_GOLDEN') == '1':
        RENDER_GOLDEN.write_text(json.dumps(actual, indent=1) + '\n')
    golden = json.loads(RENDER_GOLDEN.read_text())
    assert actual == golden, 'the Host rendering changed: bump SIM_RENDER_VERSION and runtime.nexloop_sim_render_version(), then rewrite the golden'


def test_no_application_role_and_not_public_holds_a_privilege_on_the_manifest(ctx):
    admin = ctx['admin']
    roles = {r[0] for r in admin.execute("select rolname from pg_roles where rolname like 'nexloop%%' and rolname<>'nexloop_owner' and not rolsuper").fetchall()}
    assert {'nexloop_api', 'nexloop_sim_api', 'nexloop_twin_port', 'nexloop_stand_in_worker'} <= roles
    for table in ('runtime.nexloop_sim_context_bindings', 'runtime.nexloop_sim_context_sources'):
        for role in sorted(roles):
            assert not admin.execute("select has_table_privilege(%s,%s,'select,insert,update,delete,truncate,references,trigger')", (role, table)).fetchone()[0], (role, table)
        assert admin.execute("""select count(*) from pg_class c,aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) x
            where c.oid=%s::regclass and x.grantee=0""", (table,)).fetchone()[0] == 0, ('PUBLIC', table)
    # The manifest's foreign key to its binding (the whole deletion removes both, in dependency order).
    assert admin.execute("""select count(*) from pg_constraint where conrelid='runtime.nexloop_sim_context_sources'::regclass and contype='f'
        and confrelid='runtime.nexloop_sim_context_bindings'::regclass""").fetchone()[0] == 1


def test_the_contract_example_is_self_consistent(ctx):
    """L4: the example's counts agree (included = evidence items <= limit, omitted = total - included, the omission reported) and
    every content_hash is the sha256 of its content as PostgreSQL jsonb text."""
    admin = ctx['admin']
    pack = json.loads((ROOT / 'packages/contracts/examples/context-pack-sim.valid.json').read_text())
    b = pack['budget_report']
    assert b['events_included'] == len(pack['evidence']) <= b['event_limit'] and b['omitted'] == b['events_total'] - b['events_included']
    assert (b['omitted'] > 0) == ({'code': 'older_events_omitted', 'section': 'evidence'} in pack['insufficient'])
    for item in pack['consumer_state'] + pack['evidence']:
        assert admin.execute("select encode(sha256(convert_to(%s::jsonb::text,'UTF8')),'hex')", (json.dumps(item['content'], ensure_ascii=False),)).fetchone()[0] == item['content_hash']
