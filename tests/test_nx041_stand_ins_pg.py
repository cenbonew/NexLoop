"""NX-041 S4 (design §3, D4, D11, D12; AT-046): Action stand-ins, the simulated ledger and the stand-in executor without network egress.

Real PostgreSQL on a clean catalog. A simulation world is created through the governed owner handler (S2) and gets its one credential;
the entries are called as the application roles that hold them (nexloop_api submits, nexloop_action_worker executes). The real effect
path is probed at its tables and entries with a registered sim world and its own credential.
"""
import ast
import hashlib
import inspect
from pathlib import Path
import re
import uuid

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from authority_fixture import seed_authority
from nexloop_eios import stand_in_executor as S
from nexloop_eios.bootstrap import bootstrap
from support.tenants import TENANT_A, TENANT_B

REAL_TABLES = ('runtime.nexloop_effect_intents', 'runtime.nexloop_effect_outbox', 'runtime.nexloop_effect_attempts', 'runtime.nexloop_effect_observations',
               'runtime.nexloop_incentive_issues', 'runtime.nexloop_outreach_sends', 'runtime.nexloop_outbound_messages',
               'control.nexloop_real_dispatch_switches', 'control.nexloop_effect_contexts')


def create_world(admin, kind='simulation', tenant=TENANT_A):
    return admin.execute("select control.nexloop_governed_create_sim_world(%s,'real','owner-principal','owner-subject',%s,%s)",
                         (tenant, 'intent-' + uuid.uuid4().hex[:8], Jsonb({'operation': 'create_sim_world', 'request_id': uuid.uuid4().hex, 'kind': kind,
                                                                         'purpose': 'S4 stand-in test', 'expires_in_days': 7}))).fetchone()[0]['world']


@pytest.fixture
def sim(admin, pg):
    bootstrap(admin)
    for t in (TENANT_A, TENANT_B):
        admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (t,))
    a, b = create_world(admin), create_world(admin, kind='shadow')
    token_a, _ = seed_authority(admin, TENANT_A, 'eios:artifact:' + 'a' * 32, world=a, identity_suffix='-stand-in-a')
    token_b, _ = seed_authority(admin, TENANT_A, 'eios:artifact:' + 'b' * 32, world=b, identity_suffix='-stand-in-b')
    token_real, _ = seed_authority(admin, TENANT_A, 'eios:artifact:' + 'c' * 32, world='real', identity_suffix='-stand-in-real')
    token_test, _ = seed_authority(admin, TENANT_A, 'eios:artifact:' + 'd' * 32, world='test', identity_suffix='-stand-in-test')
    role = lambda name: (lambda: psycopg.connect(make_conninfo(pg, user=name), autocommit=True))  # noqa: E731
    return {'admin': admin, 'pg': pg, 'a': a, 'b': b, 'token_a': token_a, 'token_b': token_b, 'token_real': token_real, 'token_test': token_test,
            'api': role('nexloop_api'), 'worker': role('nexloop_stand_in_worker'), 'action_worker': role('nexloop_action_worker')}


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def intent(action='nexloop.service.request', provider='http', **over):
    # Each intent its own business intent by default (review fixes 2 N4 refuses a second open intent for one business digest).
    body = {'intent_id': str(uuid.uuid4()), 'action_name': action, 'action_version': 1, 'provider_kind': provider, 'business_digest': uuid.uuid4().hex * 2,
            'request_digest': 'c' * 64, 'origin': 'twin'}
    body.update(over)
    return body


def submit(f, world, token, body):
    with f['api']() as db:
        return db.execute('select authz.nexloop_sim_effect_submit(%s,%s,%s)', (digest(token), world, Jsonb(body))).fetchone()[0]


def real_rows(admin):
    return {t: admin.execute(f'select count(*) from {t}').fetchone()[0] for t in REAL_TABLES}


def test_the_catalog_covers_every_external_effect_path_d11(sim):
    rows = sim['admin'].execute('select action_name,provider_kind,stand_in_version from control.nexloop_stand_ins order by 1,2').fetchall()
    assert {(a, p) for a, p, _ in rows} == {('nexloop.incentive.issue', 'incentive_ledger'), ('nexloop.outreach.send', 'outreach_outbox'),
                                            ('nexloop.service.request', 'http'), ('nexloop.service.request', 'local_json')}
    # Every Action the real path can make external (the real dispatch Actions and outreach) has a stand-in.
    effect_actions = {'nexloop.service.request', 'nexloop.incentive.issue', 'nexloop.outreach.send'}
    assert effect_actions <= {a for a, _, _ in rows}
    with pytest.raises(psycopg.errors.InsufficientPrivilege), sim['admin'].transaction():
        sim['admin'].execute("delete from control.nexloop_stand_ins")


def test_a_stand_in_receipt_is_deterministic_simulated_and_repeat_counted(sim):
    f = sim
    executor = S.StandInExecutor(f['worker'], f['token_a'], f['a'])
    bodies = [intent(), intent('nexloop.service.request', 'local_json'), intent('nexloop.incentive.issue', 'incentive_ledger'),
              intent('nexloop.outreach.send', 'outreach_outbox')]
    for body in bodies:
        accepted = submit(f, f['a'], f['token_a'], body)
        assert accepted['state'] == 'accepted' and accepted['simulated'] is True
        assert submit(f, f['a'], f['token_a'], body)['intent_id'] == body['intent_id']  # a retried submission is the same intent
    assert sorted(executor.pending()) == sorted(uuid.UUID(b['intent_id']) for b in bodies)
    receipts = executor.run_once()
    # D12 default: all fulfilled, zero latency; every receipt marked simulated with its stand-in version, a simulated output.
    assert len(receipts) == 4 and all(r.status == 'fulfilled' and r.latency_ms == 0 and r.simulated and r.output_kind == 'simulated_output'
                                      and r.data_mode == 'simulation' and r.stand_in_version == '1.0.0' and r.external_ref.startswith('stand-in:')
                                      and not r.repeated for r in receipts)
    assert executor.pending() == []
    again = executor.execute(bodies[0]['intent_id'])
    first = next(r for r in receipts if r.intent_id == bodies[0]['intent_id'])
    assert (again.status, again.external_ref, again.repeated, again.executions) == (first.status, first.external_ref, True, 2)
    assert executor.query(bodies[0]['intent_id']).external_ref == first.external_ref and executor.query(uuid.uuid4()) is None
    # The ledger itself refuses anything but the repeat count.
    with pytest.raises(psycopg.errors.InsufficientPrivilege), f['admin'].transaction():
        f['admin'].execute("update runtime.nexloop_sim_effect_receipts set status='failed',failure_code='x'")
    # simulated=false, an output kind other than simulated_output, or a data mode other than simulation are refused.
    for simulated, data_mode, output_kind in ((False, 'simulation', 'simulated_output'), (True, 'real', 'simulated_output'), (True, 'simulation', 'fact')):
        with pytest.raises(psycopg.errors.CheckViolation), f['admin'].transaction():
            f['admin'].execute('set local session_replication_role=replica')
            f['admin'].execute("""insert into runtime.nexloop_sim_effect_receipts(tenant_id,world,intent_id,stand_in_id,stand_in_version,simulated,status,external_ref,
                latency_ms,data_mode,output_kind) values(%s,%s,%s,'outreach.send','1.0.0',%s,'fulfilled','stand-in:x',0,%s,%s)""",
                               (TENANT_A, f['a'], uuid.uuid4(), simulated, data_mode, output_kind))
    for world in ('real', 'test'):
        with pytest.raises(psycopg.errors.CheckViolation), f['admin'].transaction():
            f['admin'].execute('set local session_replication_role=replica')
            f['admin'].execute("""insert into runtime.nexloop_sim_effect_intents(tenant_id,world,intent_id,stand_in_id,business_digest,request_digest,origin,sim_clock,
                outcome_salt,submitted_by) values(%s,%s,%s,'outreach.send',%s,%s,'twin',now(),%s,'x')""", (TENANT_A, world, uuid.uuid4(), 'b' * 64, 'c' * 64, 'd' * 64))


def test_behaviour_is_explicit_frozen_configuration_d12(sim):
    f = sim
    admin = f['admin']
    configure = lambda world, cfg: admin.execute("select runtime.nexloop_stand_in_configure(%s,%s,%s,'experiment-inputs')", (TENANT_A, world, Jsonb(cfg))).fetchone()[0]  # noqa: E731
    for bad in ({'fail_rate': 0.7, 'unknown_rate': 0.4}, {'fail_rate': -1}, {'latency_ms': {'min': 5}}, {'colour': 'red'}, {'failure_codes': ['Bad Code']}):
        with pytest.raises(psycopg.errors.InvalidParameterValue, match='config_invalid'), admin.transaction():
            configure(f['a'], bad)
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='real_world_refused'), admin.transaction():
        configure('real', {'fail_rate': 1})
    assert len(configure(f['a'], {'fail_rate': 1, 'failure_codes': ['timeout'], 'latency_ms': {'min': 10, 'max': 20}})) == 64
    with pytest.raises(psycopg.errors.InsufficientPrivilege), admin.transaction():  # frozen: written once
        admin.execute("update control.nexloop_stand_in_configs set config='{}'")
    configure(f['b'], {'unknown_rate': 1})
    ex_a, ex_b = S.StandInExecutor(f['worker'], f['token_a'], f['a']), S.StandInExecutor(f['worker'], f['token_b'], f['b'])
    body = intent()
    submit(f, f['a'], f['token_a'], body)
    failed = ex_a.execute(body['intent_id'])
    assert (failed.status, failed.failure_code) == ('failed', 'timeout') and 10 <= failed.latency_ms <= 20
    submit(f, f['b'], f['token_b'], body)  # the same intent id in another world is another intent
    assert ex_b.execute(body['intent_id']).status == 'unknown'


def test_real_and_test_refuse_the_stand_in_and_worlds_do_not_cross(sim):
    f = sim
    for world, token in (('real', f['token_real']), ('test', f['token_test']), ('real', f['token_a']), ('simulation', f['token_a'])):
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='real_world_refused'):
            submit(f, world, token, intent())
        with f['worker']() as db, pytest.raises(psycopg.errors.InsufficientPrivilege, match='real_world_refused'):
            db.execute('select authz.nexloop_sim_effect_execute(%s,%s,%s)', (digest(token), world, uuid.uuid4()))
    for world in ('real', 'test', 'simulation'):
        with pytest.raises(ValueError):
            S.StandInExecutor(f['worker'], f['token_a'], world)
    # sim A's credential neither submits nor executes in shadow B; B's executor does not see A's intent.
    body = intent()
    submit(f, f['a'], f['token_a'], body)
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='service authentication denied'):
        submit(f, f['b'], f['token_a'], intent())
    ex_b = S.StandInExecutor(f['worker'], f['token_b'], f['b'])
    assert ex_b.pending() == []
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='intent_unavailable'):
        ex_b.execute(body['intent_id'])
    # D11 fail closed: an effect without a stand-in is unavailable; a submission under another role is refused.
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='stand-in: unavailable'):
        submit(f, f['a'], f['token_a'], intent('nexloop.payment.capture', 'http'))
    with f['worker']() as db, pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute('select authz.nexloop_sim_effect_submit(%s,%s,%s)', (digest(f['token_a']), f['a'], Jsonb(intent())))
    # A deleted world's credential is revoked and its stand-in refuses.
    f['admin'].execute("select control.nexloop_governed_delete_sim_world(%s,'real','owner-principal','owner-subject','i-del',%s)",
                       (TENANT_A, Jsonb({'operation': 'delete_sim_world', 'request_id': 'del', 'world': f['a'], 'reason': '结束'})))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        submit(f, f['a'], f['token_a'], intent())
    assert f['admin'].execute('select count(*) from runtime.nexloop_sim_effect_intents where world=%s', (f['a'],)).fetchone()[0] == 0  # S2 deletion


SWITCH = """insert into control.nexloop_real_dispatch_switches(tenant_id,world,scope_kind,scope_ref,enabled,data_mode,revision,reason,changed_by,intent_id)
    values(%s,%s,'tenant','*',false,'real',1,'synthetic','synthetic-owner',%s)"""


def test_the_real_effect_path_never_holds_a_simulation_world_row(sim):
    """Review 1.1 / 1.3: every declared outbound table carries the validated CHECK; a later BEFORE trigger rewriting the world, or
    triggers switched off, cannot get a simulation row past it."""
    f = sim
    admin = f['admin']
    guarded = {r[0] for r in admin.execute('select table_name::text from control.nexloop_real_effect_tables where guarded').fetchall()}
    assert set(REAL_TABLES) <= guarded and {'runtime.nexloop_message_outbox', 'runtime.nexloop_relay_handoffs', 'runtime.nexloop_relay_claims',
                                            'runtime.nexloop_relay_attempts', 'control.nexloop_data_exports'} <= guarded
    checked = {r[0] for r in admin.execute("""select conrelid::regclass::text from pg_constraint where conname='nx041_real_effect_world' and contype='c'
        and convalidated and pg_get_constraintdef(oid) like '%nexloop_world_kind(world)%simulation%shadow%'""").fetchall()}
    assert checked == guarded
    # Positive control: a real row passes (triggers off as trusted configuration); the same row in a sim world does not.
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute(SWITCH, (TENANT_A, 'real', 'i-real'))
    for world in (f['a'], f['b']):
        with pytest.raises(psycopg.errors.CheckViolation, match='nx041_real_effect_world'), admin.transaction():
            admin.execute('set local session_replication_role=replica')  # even with triggers off
            admin.execute(SWITCH, (TENANT_B, world, 'i-sim'))
    # A later BEFORE trigger that rewrites the world (enabled always, so it fires even in replica) is still caught.
    admin.execute(f"""create function public.zz_nx041_rewrite() returns trigger language plpgsql as $$begin new.world:='{f['a']}';return new;end$$""")
    admin.execute('create trigger zz_rewrite_world before insert on control.nexloop_real_dispatch_switches for each row execute function public.zz_nx041_rewrite()')
    admin.execute('alter table control.nexloop_real_dispatch_switches enable always trigger zz_rewrite_world')
    with pytest.raises(psycopg.errors.CheckViolation, match='nx041_real_effect_world'), admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute(SWITCH, (TENANT_B, 'real', 'i-rewritten'))
    admin.execute('drop trigger zz_rewrite_world on control.nexloop_real_dispatch_switches')
    admin.execute('drop function public.zz_nx041_rewrite()')
    # The owner's governed switch never opens a simulation world either, and the dispatch decision closes it at the world layer.
    with pytest.raises(psycopg.Error), admin.transaction():
        admin.execute("""select control.nexloop_governed_set_real_dispatch(%s,%s,'owner','owner','i',%s)""",
                      (TENANT_A, f['a'], Jsonb({'operation': 'set_real_dispatch', 'request_id': 'r', 'scope_kind': 'tenant', 'scope_ref': '*', 'enabled': True,
                                                'data_mode': 'real', 'reason': 'x'})))
    with admin.transaction():
        admin.execute("select set_config('nexloop.real_dispatch_enabled','true',true)")
        assert admin.execute('select control.nexloop_real_dispatch_closed_layer(%s,%s,%s)', (TENANT_A, f['a'], 'eios:action:nexloop.service.request:1')).fetchone()[0] == 'world'


ENTRIES = [('authz.nexloop_local_delivery_authority', 'nexloop_action_worker'), ('authz.nexloop_effect_execution_command', 'nexloop_action_worker'),
           ('authz.nexloop_effect_intent_command', 'nexloop_api'), ('authz.nexloop_incentive_command', 'nexloop_domain_worker'),
           ('authz.nexloop_outreach_command', 'nexloop_domain_worker'), ('authz.nexloop_outbound_message_command', 'nexloop_api'),
           ('authz.nexloop_message_run_issuance_command', 'nexloop_api')]


def test_at046_end_to_end_a_sim_credential_reaches_dispatch_only_through_the_stand_in(sim):
    """From the simulation world's own credential to a dispatched effect: every real entry refuses it (with every real dispatch switch
    open), the stand-in executes it, and no real effect row, credential or channel is touched."""
    f = sim
    admin, pg = f['admin'], f['pg']
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')  # every real switch as if the owner had opened it
        for kind, ref, mode in (('tenant', '*', 'real'), ('action_type', 'eios:action:nexloop.service.request:1', None)):
            admin.execute("""insert into control.nexloop_real_dispatch_switches(tenant_id,world,scope_kind,scope_ref,enabled,data_mode,revision,reason,changed_by,intent_id)
                values(%s,'real',%s,%s,true,%s,1,'synthetic','synthetic-owner','synthetic-intent')""", (TENANT_A, kind, ref, mode))
    before = real_rows(admin)
    for name, role in ENTRIES:
        with psycopg.connect(make_conninfo(pg, user=role), autocommit=True) as db, db.transaction():
            db.execute("select set_config('nexloop.real_dispatch_enabled','true',true)")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                db.execute(f'select {name}(%s,%s,%s,%s,%s)', (digest(f['token_a']), f['a'], '{"tenant_id":"%s","principal_id":"x"}' % TENANT_A, '', '{"verb":"admit"}'))
    # The stand-in path, from the same credential.
    body = intent()
    submit(f, f['a'], f['token_a'], body)
    receipts = S.StandInExecutor(f['worker'], f['token_a'], f['a']).run_once()
    assert [(r.intent_id, r.simulated, r.status) for r in receipts] == [(body['intent_id'], True, 'fulfilled')]
    with admin.transaction():
        admin.execute("select set_config('nexloop.real_dispatch_enabled','true',true)")
        assert admin.execute('select control.nexloop_real_dispatch_closed_layer(%s,%s,%s)', (TENANT_A, f['a'], 'eios:action:nexloop.service.request:1')).fetchone()[0] == 'world'
    assert real_rows(admin) == before  # nothing reached the real effect ledger
    # The credential the executor holds is the world's one credential: sim alone, no real or test principal behind it.
    assert admin.execute('select worlds from authz.nexloop_service_credentials where token_digest=%s', (digest(f['token_a']),)).fetchone()[0] == [f['a']]
    # The real metrics, reconciliation and cost paths never read the simulated ledger: only S4's own functions name its tables.
    readers = {r[0] for r in admin.execute("""select p.oid::regprocedure::text from pg_proc p join pg_namespace n on n.oid=p.pronamespace
        where n.nspname in ('authz','control','runtime','ontology') and p.prosrc ~ '(nexloop_sim_effect_(intents|receipts)|nexloop_stand_in(s|_configs)\\M)'""").fetchall()}
    assert {r.split('(')[0] for r in readers} <= {'authz.nexloop_sim_effect_submit', 'authz.nexloop_sim_effect_execute', 'authz.nexloop_sim_effect_query',
                                                  'authz.nexloop_sim_effect_pending', 'authz.nexloop_sim_effect_tenant', 'runtime.nexloop_stand_in_configure', 'runtime.nexloop_stand_in_configure_copy',
                                                  'runtime.nexloop_sim_ledger_guard'}, readers


def test_the_executor_has_no_provider_no_channel_and_no_network():
    tree = ast.parse(inspect.getsource(S))
    imported = {a.name.split('.')[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split('.')[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not imported & {'socket', 'ssl', 'http', 'urllib', 'urllib3', 'requests', 'httpx', 'aiohttp'}
    assert not any(isinstance(n, ast.ImportFrom) and n.module in ('nexloop_eios.effect_provider', 'nexloop_eios.effect_dispatch', 'nexloop_eios.incentives')
                   for n in ast.walk(tree))
    for argv in (['--world', 'real'], ['--world', 'test'], ['--world', 'simulation'], ['--world', 'sim:' + str(uuid.uuid4()), '--provider-config-file', '/x']):
        with pytest.raises(SystemExit) as stopped:
            S._arguments(['--database-url-file', '/nonexistent', '--service-credential-file', '/nonexistent'] + argv)
        assert stopped.value.code == 2
    assert S._arguments(['--database-url-file', '/x', '--service-credential-file', '/y', '--world', 'shadow:' + str(uuid.uuid4())]).once is False
    with pytest.raises(ValueError):
        S.StandInReceipt.from_row({'simulated': False, 'output_kind': 'simulated_output', 'data_mode': 'simulation'})


# Functions that write external effect paths, recognised by their name (review fixes 2): a table with a world column that one of them
# inserts into, updates or deletes from is on an outbound path and must be declared.
WRITER_NAMES = r'(^|_)(dispatch|relay|execut|deliver|effect|outreach|outbound|incentive|export|send|lease|webhook|notif|mail)'
# The name net on the tables themselves (the original list plus webhook, notification, mail, push, callback and channel).
TABLE_NAMES = r'(outbox|relay|deliver|export|send|dispatch|effect_(intents|attempts|observations|contexts)|outbound|incentive_issues|webhook|notif|mail|push|callback|channel)'


def world_tables(admin):
    return {r[0] for r in admin.execute("""select c.oid::regclass::text from pg_class c join pg_namespace n on n.oid=c.relnamespace
        join pg_attribute a on a.attrelid=c.oid and a.attname='world' and not a.attisdropped
        where c.relkind in ('r','p') and not c.relispartition and n.nspname in ('authz','control','ontology','runtime')""").fetchall()}


def written_by_effect_functions(admin, tables):
    """{table: writer functions} for the tables a dispatch/relay/executor-like function writes (schema-qualified targets in its body)."""
    out = {}
    for name, src in admin.execute("""select p.oid::regprocedure::text,p.prosrc from pg_proc p join pg_namespace n on n.oid=p.pronamespace
            where n.nspname in ('authz','control','ontology','runtime') and p.proname ~ %s""", (WRITER_NAMES,)).fetchall():
        for m in re.finditer(r'(?i)\b(insert\s+into|update|delete\s+from)\s+([a-z_]+\.[a-z_0-9]+)', src or ''):
            if m.group(2).lower() in tables:
                out.setdefault(m.group(2).lower(), set()).add(name)
    return out


def test_every_outbound_table_is_declared(sim):
    """Review 1.3 guard (by structure, review fixes 2): every table with a world column that a dispatch, relay, executor, delivery,
    effect, outreach, outbound, incentive, export, send, lease, webhook, notification or mail function writes must be declared in
    control.nexloop_real_effect_tables (guarded, or not with a reason); so must every table whose own name says it sends. A new one
    that is not declared fails here."""
    admin = sim['admin']
    tables = world_tables(admin)
    declared = dict(admin.execute('select table_name::text,reason from control.nexloop_real_effect_tables').fetchall())
    named = {r[0] for r in admin.execute("""select c.oid::regclass::text from pg_class c join pg_namespace n on n.oid=c.relnamespace
        join pg_attribute a on a.attrelid=c.oid and a.attname='world' and not a.attisdropped
        where c.relkind in ('r','p') and not c.relispartition and n.nspname in ('authz','control','ontology','runtime') and c.relname ~ %s""",
                                            (TABLE_NAMES,)).fetchall()}
    assert named - set(declared) == set(), sorted(named - set(declared))
    written = written_by_effect_functions(admin, tables)
    undeclared = {t: sorted(f)[:3] for t, f in written.items() if t not in declared}
    assert undeclared == {}, undeclared
    # Positive control: the structural scan does find the outbound tables (it is not vacuous).
    assert {'runtime.nexloop_effect_outbox', 'runtime.nexloop_outbound_messages', 'runtime.nexloop_outreach_sends'} <= set(written)
    assert all(len(r.strip()) > 10 for r in declared.values())


# The durable job queue outbox (0035) is not guarded: simulation Runs use the job queue (S6). Its rows reach outside only through a
# worker that takes a governed effect intent; this guard fixes that: the outbox is written by the queue entry alone, and no function
# that writes it also writes a declared outbound table or reaches a provider.
OUTBOX_WRITERS = ('authz.nexloop_queue_command',)  # and its renamed predecessors (rename and wrap: <name>_before_*)


def test_the_job_outbox_reaches_outside_only_through_an_effect_intent(sim):
    admin = sim['admin']
    writers = {}
    for name, src in admin.execute(r"""select p.oid::regprocedure::text,p.prosrc from pg_proc p join pg_namespace n on n.oid=p.pronamespace
            where n.nspname not in ('pg_catalog','information_schema') and p.prosrc ~* '(insert\s+into|update)\s+runtime\.nexloop_outbox\M'""").fetchall():
        writers[name.split('(')[0]] = src
    assert writers and all(w in OUTBOX_WRITERS or w.startswith(tuple(x + '_before_' for x in OUTBOX_WRITERS)) for w in writers), sorted(writers)
    guarded = {r[0] for r in admin.execute('select table_name::text from control.nexloop_real_effect_tables where guarded').fetchall()}
    for name, src in writers.items():
        targets = {m.group(2).lower() for m in re.finditer(r'(?i)\b(insert\s+into|update|delete\s+from)\s+([a-z_]+\.[a-z_0-9]+)', src)}
        assert not targets & guarded, (name, sorted(targets & guarded))
    # A job's kind names an internal hand-off; the outbox has no column for a destination, an address or a request body.
    columns = {r[0] for r in admin.execute("""select attname from pg_attribute where attrelid='runtime.nexloop_outbox'::regclass and attnum>0 and not attisdropped""").fetchall()}
    assert columns == {'tenant_id', 'world', 'queue', 'outbox_id', 'job_id', 'kind', 'status', 'fence', 'lease_until', 'lease_credential', 'created_at'}, columns
    # Python: the modules that take work from the durable queue and also hold a network client are declared, each with what it calls.
    import nexloop_eios
    root = Path(nexloop_eios.__file__).parent
    network = re.compile(r'^\s*(import|from)\s+(http\.client|urllib|requests|httpx|socket|ssl|smtplib|aiohttp)\b', re.M)
    queue = re.compile(r'durable_queue|PostgresDurableQueue|nexloop_queue_command')
    both = {p.relative_to(root).as_posix() for p in root.rglob('*.py') if queue.search(p.read_text()) and network.search(p.read_text())}
    assert both == QUEUE_WORKERS_WITH_NETWORK.keys(), sorted(both ^ QUEUE_WORKERS_WITH_NETWORK.keys())  # a new one, or a stale entry, fails


# What each queue worker with a network client reaches (none is an external provider reached from a job without an effect intent).
QUEUE_WORKERS_WITH_NETWORK = {
    'runtime_dispatch.py': 'the Agent Host internal API (its configured host origin, loopback in both process groups, TLS); no provider - '
                           'effects of a Run go through governed effect intents',
}


def test_the_client_cannot_choose_the_outcome(sim):
    """Review 4.1 / 4.2: the simulated clock is the world's and the salt the server's. A client clock is refused; the client cannot
    compute the outcome from what it chooses (intent id, version, world, clock); a repeat gives the same receipt."""
    f = sim
    admin = f['admin']
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='stand-in: invalid'):
        submit(f, f['a'], f['token_a'], {**intent(), 'sim_clock': '2026-10-21T08:00:00+00:00'})
    admin.execute("select runtime.nexloop_stand_in_configure(%s,%s,%s,'experiment-inputs')", (TENANT_A, f['a'], Jsonb({'fail_rate': 0.5})))
    executor = S.StandInExecutor(f['worker'], f['token_a'], f['a'])
    bodies = [intent() for _ in range(24)]
    for body in bodies:
        submit(f, f['a'], f['token_a'], body)
    rows = {str(i): (c, salt) for i, c, salt in admin.execute(
        'select intent_id,sim_clock,outcome_salt from runtime.nexloop_sim_effect_intents where world=%s', (f['a'],)).fetchall()}
    world_now = admin.execute('select runtime.nexloop_world_now(%s,%s)', (TENANT_A, f['a'])).fetchone()[0]
    assert all(c <= world_now for c, _ in rows.values()) and len({salt for _, salt in rows.values()}) == len(bodies)
    receipts = {r.intent_id: r for r in executor.run_once()}
    # What a client could have predicted without the salt (the S4 v1 hash) does not decide the outcome.
    predicted = {}
    for i, (c, _) in rows.items():
        stamp = admin.execute("select to_char(%s::timestamptz at time zone 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS.US')", (c,)).fetchone()[0]
        h = hashlib.sha256(f"1.0.0|{f['a']}|{i}|{stamp}".encode()).hexdigest()
        predicted[i] = 'failed' if int(h[:8], 16) / 4294967296 < 0.5 else 'fulfilled'
    assert any(predicted[i] != receipts[i].status for i in rows)
    assert {r.status for r in receipts.values()} == {'failed', 'fulfilled'}
    again = executor.execute(bodies[0]['intent_id'])
    assert (again.status, again.external_ref, again.repeated) == (receipts[bodies[0]['intent_id']].status, receipts[bodies[0]['intent_id']].external_ref, True)


APP_ROLES = ('nexloop_api', 'nexloop_domain_worker', 'nexloop_action_worker', 'nexloop_scheduler', 'nexloop_runtime', 'nexloop_identity',
             'nexloop_metrics', 'nexloop_stand_in_worker')
LEDGER = ('runtime.nexloop_sim_effect_intents', 'runtime.nexloop_sim_effect_receipts', 'control.nexloop_stand_in_configs', 'control.nexloop_stand_ins')


# Functions that enumerate tables through the catalog, and what they read (review fixes 2, 2.2 boundary).
CATALOG_ENUMERATORS = {
    'authz.nexloop_read_memo_table_ok': 'checks that one named memo table exists; reads no row',
    'runtime.nexloop_sim_world_has_tenant': 'whether a table has a tenant column; reads no row',
    'runtime.nexloop_sim_world_tables': 'the world tables for the S2 whole deletion (the ledger of the world being deleted)',
    'runtime.nexloop_sim_world_targets': 'S2 fixes: the deletion targets (as nexloop_sim_world_tables)',
    'runtime.nexloop_sim_world_snapshot': 'S2: counts and digests of the world being deleted',
    'runtime.nexloop_sim_world_purge': 'S2: deletes the world being deleted',
}


def test_only_the_stand_in_functions_and_the_whole_deletion_read_the_simulated_ledger(sim):
    """Review 2.2: no application role holds a privilege on the ledger; no view, policy or rule names it (other than its own tenant
    policies); functions naming it are S4's own; functions enumerating world tables dynamically are S2's snapshot and purge only."""
    admin = sim['admin']
    # Every nexloop_% role and PUBLIC (review fixes 2: enumerated, not a fixed list); only the owner (and superusers, which bypass
    # privileges) are left out. APP_ROLES stays as the minimum that must exist.
    roles = {r[0] for r in admin.execute("select rolname from pg_roles where rolname like 'nexloop%%' and rolname<>'nexloop_owner' and not rolsuper").fetchall()}
    assert set(APP_ROLES) <= roles
    for table in LEDGER:
        for role in sorted(roles):
            assert not admin.execute("select has_table_privilege(%s,%s,'select,insert,update,delete,truncate,references,trigger')", (role, table)).fetchone()[0], (role, table)
        assert admin.execute("""select count(*) from pg_class c,aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) x
            where c.oid=%s::regclass and x.grantee=0""", (table,)).fetchone()[0] == 0, ('PUBLIC', table)
    assert admin.execute("select count(*) from pg_views where definition ~ '(nexloop_sim_effect_|nexloop_stand_in)'").fetchone()[0] == 0
    assert admin.execute("select count(*) from pg_rules where definition ~ '(nexloop_sim_effect_|nexloop_stand_in)'").fetchone()[0] == 0
    policies = admin.execute("""select schemaname||'.'||tablename,policyname from pg_policies
        where coalesce(qual,'')||coalesce(with_check,'') ~ '(nexloop_sim_effect_|nexloop_stand_in)'""").fetchall()
    assert policies == []
    own = {r[0] for r in admin.execute("""select p.oid::regprocedure::text from pg_proc p join pg_namespace n on n.oid=p.pronamespace
        where n.nspname not in ('pg_catalog','information_schema') and p.prosrc ~ '(nexloop_sim_effect_(intents|receipts)|nexloop_stand_in(s|_configs)\\M)'""").fetchall()}
    assert {r.split('(')[0] for r in own} <= {'authz.nexloop_sim_effect_submit', 'authz.nexloop_sim_effect_execute', 'authz.nexloop_sim_effect_query',
                                              'authz.nexloop_sim_effect_pending', 'authz.nexloop_sim_effect_tenant', 'runtime.nexloop_stand_in_configure', 'runtime.nexloop_stand_in_configure_copy',
                                              'runtime.nexloop_sim_ledger_guard'}, own
    # Positive control: the pattern does find the readers (the S7 port only authenticates through nexloop_sim_effect_tenant and is
    # not a reader).
    assert {'authz.nexloop_sim_effect_submit', 'authz.nexloop_sim_effect_execute', 'runtime.nexloop_stand_in_configure'} <= {r.split('(')[0] for r in own}
    enumerators = {r[0].split('(')[0] for r in admin.execute("""select p.oid::regprocedure::text from pg_proc p join pg_namespace n on n.oid=p.pronamespace
        where n.nspname not in ('pg_catalog','information_schema') and p.prosrc ~ '(nexloop_sim_world_tables|nexloop_sim_world_targets)'""").fetchall()}
    assert enumerators <= {'runtime.nexloop_sim_world_snapshot', 'runtime.nexloop_sim_world_purge', 'runtime.nexloop_sim_world_targets'}, enumerators
    # Boundary (review fixes 2): a function that enumerates tables itself (pg_class, pg_attribute, pg_tables, information_schema) can
    # reach the ledger without naming it; every such function is registered in CATALOG_ENUMERATORS with what it reads.
    found = {r[0].split('(')[0] for r in admin.execute("""select p.oid::regprocedure::text from pg_proc p join pg_namespace n on n.oid=p.pronamespace
        where n.nspname not in ('pg_catalog','information_schema') and p.prosrc ~ '(pg_class|pg_attribute|pg_tables|information_schema[.](tables|columns))'""").fetchall()}
    assert found <= CATALOG_ENUMERATORS.keys(), sorted(found - CATALOG_ENUMERATORS.keys())


def test_the_stand_in_role_executes_the_stand_in_entries_only(sim):
    """Review 3.1: nexloop_stand_in_worker executes pending, execute and query, and no other security definer function (no real
    dispatch summary, no readiness, no real entry); the real executor role no longer reaches the stand-in."""
    admin = sim['admin']
    reachable = {r[0].split('(')[0] for r in admin.execute("""select p.oid::regprocedure::text from pg_proc p join pg_namespace n on n.oid=p.pronamespace
        where n.nspname in ('authz','control','runtime','ontology','public') and p.prosecdef
        and has_function_privilege('nexloop_stand_in_worker',p.oid,'execute')""").fetchall()}
    # runtime.nexloop_erasure_permits is executable by public (0129): the append-only triggers of every role ask it; it answers false
    # for anyone but nexloop_owner inside a transaction that opened the scope.
    assert reachable == {'authz.nexloop_sim_effect_pending', 'authz.nexloop_sim_effect_execute', 'authz.nexloop_sim_effect_query',
                         'runtime.nexloop_erasure_permits'}, reachable
    with sim['action_worker']() as db, pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute('select authz.nexloop_sim_effect_pending(%s,%s,10)', (digest(sim['token_a']), sim['a']))
    with sim['worker']() as db, pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute('select authz.nexloop_real_dispatch_summary()')


def test_simulated_outputs_cannot_become_facts(sim):
    """Review 5.2 (evaluation fixed as a guard): a Claim needs a conversation, and conversations are real only, so no Claim exists
    in a simulation world; no Claim kind is a fact (verified_fact is absent by design); a Commitment is fulfilled only from effect
    intents, which never hold a simulation row."""
    admin = sim['admin']
    conv = admin.execute("""select pg_get_constraintdef(oid) from pg_constraint where conrelid='runtime.nexloop_conversations'::regclass and contype='c'
        and pg_get_constraintdef(oid) like '%world%'""").fetchall()
    assert any("world = 'real'" in c for (c,) in conv), conv
    fk = admin.execute("""select count(*) from pg_constraint where conrelid='ontology.nexloop_claims'::regclass and contype='f'
        and confrelid='runtime.nexloop_conversations'::regclass""").fetchone()[0]
    assert fk == 1 and admin.execute("""select a.attnotnull from pg_attribute a where a.attrelid='ontology.nexloop_claims'::regclass and a.attname='conversation_id'""").fetchone()[0]
    kinds = admin.execute("""select pg_get_constraintdef(oid) from pg_constraint where conrelid='ontology.nexloop_claims'::regclass and contype='c'
        and pg_get_constraintdef(oid) like '%epistemic_kind = ANY%'""").fetchone()[0]
    assert 'fact' not in kinds.replace('verified_fact', '')  and 'verified_fact' not in kinds
    assert admin.execute("select guarded from control.nexloop_real_effect_tables where table_name='runtime.nexloop_effect_intents'::regclass").fetchone()[0]


def test_one_open_intent_per_business_intent(sim):
    """Review fixes 2 (N4): a submitter cannot resubmit one business intent under new intent ids and keep only the fulfilled
    receipt. A second intent id for the same business digest is refused while the first is open (no receipt, or unknown); a
    retry of the same intent id is still accepted; after a terminal receipt (fulfilled or failed) a new intent is possible and is
    recorded beside the first, so every attempt stays in the ledger."""
    f = sim
    admin = f['admin']
    first = intent()
    submit(f, f['a'], f['token_a'], first)
    assert submit(f, f['a'], f['token_a'], first)['intent_id'] == first['intent_id']  # the same intent again: a retry
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='stand-in: business_intent_open'):
        submit(f, f['a'], f['token_a'], intent(business_digest=first['business_digest']))
    # Positive control: another business intent, and the same business digest in another world, are accepted.
    submit(f, f['a'], f['token_a'], intent())
    submit(f, f['b'], f['token_b'], intent(business_digest=first['business_digest']))
    # unknown is not terminal: still refused.
    admin.execute("select runtime.nexloop_stand_in_configure(%s,%s,%s,'experiment-inputs')", (TENANT_A, f['a'], Jsonb({'unknown_rate': 1})))
    receipts = {r.intent_id: r for r in S.StandInExecutor(f['worker'], f['token_a'], f['a']).run_once()}
    assert receipts[first['intent_id']].status == 'unknown'
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='stand-in: business_intent_open'):
        submit(f, f['a'], f['token_a'], intent(business_digest=first['business_digest']))
    # A terminal receipt closes it: the next attempt is a new intent beside the first one.
    other = intent()
    submit(f, f['b'], f['token_b'], other)
    assert S.StandInExecutor(f['worker'], f['token_b'], f['b']).execute(other['intent_id']).status == 'fulfilled'
    submit(f, f['b'], f['token_b'], intent(business_digest=other['business_digest']))
    assert admin.execute('select count(*) from runtime.nexloop_sim_effect_intents where world=%s and business_digest=%s',
                         (f['b'], other['business_digest'])).fetchone()[0] == 2


def test_concurrent_submissions_of_one_business_intent_keep_one(sim, pg):
    """N4 under concurrency: two connections submit different intent ids for one business digest; the second waits for the first
    (advisory lock per business digest) and is then refused."""
    f = sim
    body = intent()
    with psycopg.connect(make_conninfo(pg, user='nexloop_api')) as one, psycopg.connect(make_conninfo(pg, user='nexloop_api')) as two:
        one.execute('select authz.nexloop_sim_effect_submit(%s,%s,%s)', (digest(f['token_a']), f['a'], Jsonb(body)))
        two.execute('set statement_timeout=500')
        with pytest.raises(psycopg.errors.QueryCanceled):
            two.execute('select authz.nexloop_sim_effect_submit(%s,%s,%s)', (digest(f['token_a']), f['a'], Jsonb(intent(business_digest=body['business_digest']))))
        two.rollback()
        one.commit()
        with pytest.raises(psycopg.errors.InvalidParameterValue, match='business_intent_open'):
            two.execute('select authz.nexloop_sim_effect_submit(%s,%s,%s)', (digest(f['token_a']), f['a'], Jsonb(intent(business_digest=body['business_digest']))))


def test_an_existing_stand_in_role_is_brought_to_its_shape(admin):
    """Review fixes 2 (N2): a role named nexloop_stand_in_worker that exists before the migration with other attributes, a role
    membership and a table privilege leaves it with exactly the declared shape and the three stand-in entries."""
    admin.execute('drop role if exists nexloop_stand_in_worker')
    admin.execute('create role nexloop_stand_in_worker nologin inherit createdb bypassrls')
    admin.execute('grant pg_read_all_data to nexloop_stand_in_worker')
    bootstrap(admin)
    r = admin.execute("""select rolcanlogin,rolinherit,rolcreatedb,rolcreaterole,rolsuper,rolreplication,rolbypassrls from pg_roles
        where rolname='nexloop_stand_in_worker'""").fetchone()
    assert r == (True, False, False, False, False, False, False), r
    assert admin.execute("""select count(*) from pg_auth_members a join pg_roles m on m.oid=a.member where m.rolname='nexloop_stand_in_worker'""").fetchone()[0] == 0
    reachable = {x[0].split('(')[0] for x in admin.execute("""select p.oid::regprocedure::text from pg_proc p join pg_namespace n on n.oid=p.pronamespace
        where n.nspname in ('authz','control','runtime','ontology') and p.prosecdef and has_function_privilege('nexloop_stand_in_worker',p.oid,'execute')""").fetchall()}
    assert reachable == {'authz.nexloop_sim_effect_pending', 'authz.nexloop_sim_effect_execute', 'authz.nexloop_sim_effect_query',
                         'runtime.nexloop_erasure_permits'}, reachable
    assert not admin.execute("select has_table_privilege('nexloop_stand_in_worker','runtime.nexloop_sim_effect_intents','select')").fetchone()[0]


def receipts_without_world(executor, ids):
    out = {}
    for i in ids:
        r = executor.query(i)
        out[i] = {k: v for k, v in vars(r).items() if k != 'world'}
    return out


def test_a_replay_with_the_copied_frozen_configuration_gives_the_same_receipts(sim):
    """S7 decision A: the frozen configuration carries a server outcome seed; copying it to a new simulation world and submitting the
    same frozen intents there gives byte-identical receipts (the world aside); a fresh configuration (another seed) gives other
    receipts; a caller can neither pass a seed nor read it; one open intent per business digest still holds in the replay world."""
    f = sim
    admin = f['admin']
    config = Jsonb({'fail_rate': 0.4, 'unknown_rate': 0.2, 'latency_ms': {'min': 10, 'max': 900}, 'failure_codes': ['timeout', 'rejected']})
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='outcome_seed_server_only'), admin.transaction():
        admin.execute("select runtime.nexloop_stand_in_configure(%s,%s,%s,'experiment-inputs')", (TENANT_A, f['a'], Jsonb({'outcome_seed': 'e' * 64})))
    digest_a = admin.execute("select runtime.nexloop_stand_in_configure(%s,%s,%s,'experiment-inputs')", (TENANT_A, f['a'], config)).fetchone()[0]
    bodies = [intent() for _ in range(24)]
    ids = [b['intent_id'] for b in bodies]

    def run(world, token):
        for body in bodies:
            submit(f, world, token, body)
        executor = S.StandInExecutor(f['worker'], token, world)
        executor.run_once(limit=100)
        return receipts_without_world(executor, ids)
    first = run(f['a'], f['token_a'])
    assert {r['status'] for r in first.values()} >= {'fulfilled', 'failed'}
    # The replay world: the same frozen configuration and seed, copied by reference to the source world.
    replay = create_world(admin)
    token_r, _ = seed_authority(admin, TENANT_A, 'eios:artifact:' + 'e' * 32, world=replay, identity_suffix='-stand-in-replay')
    assert admin.execute("select runtime.nexloop_stand_in_configure_copy(%s,%s,%s,'experiment-replay')", (TENANT_A, f['a'], replay)).fetchone()[0] == digest_a
    assert run(replay, token_r) == first  # byte for byte, the world aside
    # N4 still holds in the replay world: a new intent id for an open business intent is refused.
    open_ids = [i for i, r in first.items() if r['status'] == 'unknown']
    assert open_ids
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='business_intent_open'):
        submit(f, replay, token_r, intent(business_digest=next(b for b in bodies if b['intent_id'] == open_ids[0])['business_digest']))
    # Another seed (a fresh configuration with the same values): other receipts.
    fresh = create_world(admin)
    token_f, _ = seed_authority(admin, TENANT_A, 'eios:artifact:' + 'f' * 32, world=fresh, identity_suffix='-stand-in-fresh')
    admin.execute("select runtime.nexloop_stand_in_configure(%s,%s,%s,'experiment-inputs')", (TENANT_A, fresh, config))
    other = run(fresh, token_f)
    assert other != first and any(other[i]['external_ref'] != first[i]['external_ref'] for i in ids)
    # A copy never overwrites a frozen configuration, never targets a real world, never copies from another tenant's world.
    with pytest.raises(psycopg.errors.UniqueViolation), admin.transaction():
        admin.execute("select runtime.nexloop_stand_in_configure_copy(%s,%s,%s,'x')", (TENANT_A, f['a'], fresh))
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='real_world_refused'), admin.transaction():
        admin.execute("select runtime.nexloop_stand_in_configure_copy(%s,%s,'real','x')", (TENANT_A, f['a']))
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='world_unavailable'), admin.transaction():
        admin.execute("select runtime.nexloop_stand_in_configure_copy(%s,%s,%s,'x')", (TENANT_B, f['a'], create_world(admin, tenant=TENANT_B)))


def test_no_world_role_reads_the_outcome_seed(sim):
    """S7 decision A: the models, twins and simulation credentials of a world reach the database as the application roles; none of
    them can read the seed: no privilege on the configuration table or its column, no executable function returns or names it but
    the server's own configuration entries (held by no role), and no answer of the stand-in entries carries it."""
    f = sim
    admin = f['admin']
    admin.execute("select runtime.nexloop_stand_in_configure(%s,%s,%s,'experiment-inputs')", (TENANT_A, f['a'], Jsonb({'fail_rate': 0.5})))
    seed = admin.execute('select outcome_seed from control.nexloop_stand_in_configs where world=%s', (f['a'],)).fetchone()[0]
    roles = {r[0] for r in admin.execute("select rolname from pg_roles where rolname like 'nexloop%%' and rolname<>'nexloop_owner' and not rolsuper").fetchall()}
    for role in sorted(roles):
        assert not admin.execute("select has_column_privilege(%s,'control.nexloop_stand_in_configs','outcome_seed','select')", (role,)).fetchone()[0], role
        for fn in ('runtime.nexloop_stand_in_configure(text,text,jsonb,text)', 'runtime.nexloop_stand_in_configure_copy(text,text,text,text)'):
            assert not admin.execute('select has_function_privilege(%s,%s,%s)', (role, fn, 'execute')).fetchone()[0], (role, fn)
    naming = {r[0].split('(')[0] for r in admin.execute("""select p.oid::regprocedure::text from pg_proc p join pg_namespace n on n.oid=p.pronamespace
        where n.nspname not in ('pg_catalog','information_schema') and p.prosrc ~ 'outcome_seed\\M'""").fetchall()}
    assert naming == {'runtime.nexloop_stand_in_configure', 'runtime.nexloop_stand_in_configure_copy', 'authz.nexloop_sim_effect_submit'}, naming
    body = intent()
    answers = [str(submit(f, f['a'], f['token_a'], body))]
    executor = S.StandInExecutor(f['worker'], f['token_a'], f['a'])
    answers += [repr(r) for r in executor.run_once()] + [repr(executor.query(body['intent_id']))]
    assert not any(seed in a for a in answers)
    with f['api']() as db, pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute('select outcome_seed from control.nexloop_stand_in_configs')
