"""NX-043 X7: the stage acceptance script scripts/acceptance/nx043_stage.py, run locally on PostgreSQL (the positive control).

The owner's HTTPS workbench Actions are replaced by a fake that calls the same governed handlers (control.nexloop_governed_*, as the
governed entry does: the tenant and the owner principal from the server side, world real) and answers like the workbench (200, or 409
with the refusal reason). The probe is the superuser connection in read-only transactions; the simulation worker connects as
nexloop_sim_worker with the shadow world's worker credential. Real inbound traffic is written as the real intake writes it.
"""
import io
import json
import re
import sys
import time
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from authority_fixture import seed_authority
from nexloop_eios.bootstrap import bootstrap
from support.tenants import TENANT_A, TENANT_B
from test_nx042_consumer_twins_pg import publish as publish_twin
from test_nx043_shadow_pg import inbound, sha
from test_nx043_x4_wiring_pg import send

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/acceptance'))
import at046_stage  # noqa: E402
import nx043_stage as X  # noqa: E402


class Owner:
    """The owner workbench, as the governed entry calls the handlers (tenant and principal server side, world real)."""

    def __init__(self, admin):
        self.admin, self.csrf, self.calls = admin, 'csrf', []

    def _call(self, method, path, body=None, extra=None):
        op = path.rsplit('/', 1)[-1]
        self.calls.append(op)
        # The workbench entry's own request validation first (stage X7 S7, release 38: read_experiment_report answered 422 there while
        # this fake, calling the handler directly, accepted the body).
        from nexloop_eios.workbench_actions_http import validate
        try:
            validate(op, body)
        except (ValueError, TypeError, KeyError):
            return 422, {'code': 'invalid_request', 'message': '请求未完成', 'trace_id': uuid.uuid4().hex, 'retryable': False, 'details': {}}
        handler = self.admin.execute('select handler from control.nexloop_governed_capabilities where operation=%s', (op,)).fetchone()[0].split('(')[0]
        try:
            with self.admin.transaction():
                out = self.admin.execute(f"select {handler}(%s,'real','owner-principal','owner-subject',%s,%s)",
                                         (TENANT_A, 'intent-' + uuid.uuid4().hex[:12], Jsonb({'operation': op, 'request_id': extra['Idempotency-Key'], **body}))).fetchone()[0]
            return 200, out
        except psycopg.errors.InvalidParameterValue as refused:
            code = re.search(r': ([a-z_]{1,40})$', refused.diag.message_primary or '')
            # The workbench's actual error body (stage X7 S2, release 37): the code is in details.reason, never at the top level.
            return 409, {'code': 'not_allowed_in_state', 'message': 'not allowed in this state', 'trace_id': uuid.uuid4().hex,
                         'retryable': False, 'details': {'reason': code.group(1) if code else None}}

    def logout(self):
        pass


@pytest.fixture
def stage(admin, pg, tmp_path):
    bootstrap(admin)
    for t in (TENANT_A, TENANT_B):
        admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (t,))
    sim = admin.execute("select control.nexloop_governed_create_sim_world(%s,'real','owner-principal','owner-subject','i-sim',%s)",
                        (TENANT_A, Jsonb({'operation': 'create_sim_world', 'request_id': 'x7-sim', 'kind': 'simulation', 'purpose': 'X7 screening',
                                          'expires_in_days': 7}))).fetchone()[0]['world']
    publish_twin(admin, tenant=TENANT_A)  # published, uncalibrated: screening cannot start (D9)
    owner = Owner(admin)
    ports = at046_stage.Ports(owner=owner, probe=lambda: psycopg.connect(pg, autocommit=True))
    ports.decide = None
    config = {'schema': 'nexloop-s6-stage/1', 'executor': 'L3 local', 'release': 'local', 'tenant_id': TENANT_A, 'results_dir': str(tmp_path / 'results'),
              'inputs': {'ends_in_minutes': 1, 'screening': {'world': sim, 'twin': {'twin_id': 'trial.service_consumer', 'version': 1}},
                         'decisions': {'kind': 'reply', 'limit': 50}, 'guard_timeout_seconds': 5, 'quiet_seconds': 1, 'quiet_timeout_seconds': 10}}
    path = tmp_path / 'nx043.json'
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    return dict(admin=admin, pg=pg, ports=ports, owner=owner, config=str(path), tmp=tmp_path)


def phase(f, name, *extra, ports=None):
    out = io.StringIO()
    code = X.main([name, '--config', f['config'], *extra], ports=ports or f['ports'], out=out)
    doc = json.loads(out.getvalue().strip().splitlines()[-1])
    path = f['tmp'] / f'{name}.json'
    path.write_text(out.getvalue())
    return code, doc, str(path)


def steps(doc):
    return {s['name']: s for s in doc['steps']}


def worker_ports(f, world):
    token, _ = seed_authority(f['admin'], TENANT_A, 'eios:artifact:' + 'a' * 32, world=world, identity_suffix='-x7-worker', sim_use='worker')
    connect = lambda: psycopg.connect(make_conninfo(f['pg'], user='nexloop_sim_worker'), autocommit=True)  # noqa: E731

    def decide(w, key, candidate, kind):
        with connect() as db:
            return db.execute('select authz.nexloop_shadow_decision_record(%s,%s,%s)', (sha(token), w, Jsonb(
                {'event_key': key, 'strategy_id': candidate['strategy_id'], 'strategy_version': candidate['version'], 'decision': kind}))).fetchone()[0]
    p = at046_stage.Ports(owner=f['owner'])  # APP_HOST: the simulation worker only, no probe (L6 X7 review 2.1)
    p.decide = decide
    return p


def test_the_screening_and_shadow_acceptance_end_to_end(stage):
    f = stage
    admin = f['admin']
    code, doc, _ = phase(f, 'prepare')
    assert code == 0 and doc['passed'] is True, [x for x in doc['steps'] if x['passed'] is not True]
    assert set(steps(doc)) == {'shadow_world_created', 'candidate_strategy_published', 'control_strategy_published', 'shadow_experiment_registered',
                               'screening_experiment_registered'}
    state = json.loads((f['tmp'] / 'results' / 'nx043-state.json').read_text())
    assert oct((f['tmp'] / 'results' / 'nx043-state.json').stat().st_mode & 0o777) == '0o600'
    code, doc, _ = phase(f, 'run')
    assert code == 0 and doc['passed'] is True, doc
    assert steps(doc)['screening_start_needs_a_calibrated_twin']['observed'] == {'status': 409, 'reason': 'twin_not_calibrated'}
    # Real inbound traffic while the copy is on (the dispatcher's synthetic webchat Consumers on stage).
    for i in range(3):
        inbound(admin, sha(f'x7-consumer-{i}'), reply=i == 0)
    worker = worker_ports(f, state['shadow_world'])  # the shadow world's worker credential: trusted configuration, before the probe
    code, before, before_path = phase(f, 'probe-before')  # DATA_HOST, after the real side is quiet
    assert code == 0 and before['passed'] is True and before['run_nonce'] == state['run']
    code, keys, keys_path = phase(f, 'shadow-keys')       # DATA_HOST: the keys into a private file
    assert code == 0 and steps(keys)['shadow_events_copied']['observed'] == {'copied': 3}
    assert oct((f['tmp'] / 'results' / 'nx043-shadow-keys.json').stat().st_mode & 0o777) == '0o600'
    code, doc, decide_path = phase(f, 'decide', ports=worker)  # APP_HOST: the simulation worker alone
    assert code == 0 and steps(doc)['shadow_decisions_recorded']['observed'] == {'decisions': 3, 'sent': 0}
    code, doc, _ = phase(f, 'report')
    assert code == 0 and doc['passed'] is True, [x for x in doc['steps'] if x['passed'] is not True]
    assert steps(doc)['experiment_result_reports_nothing_below_the_minimum']['observed']['conclusion'] == 'not_reported'
    assert steps(doc)['shadow_report_aggregates_only']['observed']['identifiers'] is False
    # The guard at the experiment's end (the alert evaluator's tick on stage; its function here once the end has passed).
    ends = state['ends_at']
    while time.time() < time.mktime(time.strptime(ends, '%Y-%m-%dT%H:%M:%SZ')) - time.timezone + 1:
        time.sleep(2)
    admin.execute('select runtime.nexloop_experiment_guard(%s,%s)', (TENANT_A, state['experiments']['shadow']))
    code, doc, after_path = phase(f, 'probe-after', '--from', before_path, '--from', keys_path, '--from', decide_path)
    assert code == 0 and doc['passed'] is True, [s for s in doc['steps'] if s['passed'] is not True]
    got = steps(doc)
    assert got['guard_stops_at_the_end']['observed'] == {'stopped_by_guard': True, 'reason': 'guardrail: ended'}
    assert got['shadow_sends_nothing']['observed']['real_sends'] == 0
    assert got['shadow_decisions_landed']['observed'] == {'decisions': 'some', 'matches_decide': True, 'stand_in_only': True}
    assert got['real_dispatch_switches_unchanged']['passed'] and got['real_side_unchanged']['observed']['changed'] == []
    code, merged, _ = phase(f, 'merge', '--from', before_path, '--from', keys_path, '--from', decide_path, '--from', after_path)
    assert code == 0 and merged['check'] == 'nx043-shadow' and merged['passed'] is True and merged['run_nonce'] == state['run']
    # Every write went through the owner's governed Actions.
    assert set(f['owner'].calls) == {'create_sim_world', 'publish_strategy', 'register_experiment', 'start_experiment', 'set_shadow_copy', 'read_experiment_report'}


def test_no_identifier_token_or_dsn_in_any_output_and_results_are_private(stage):
    f = stage
    texts = []
    for name in ('prepare', 'run', 'report'):
        code, doc, path = phase(f, name)
        texts.append(Path(path).read_text())
    state = json.loads((f['tmp'] / 'results' / 'nx043-state.json').read_text())
    ids = [state['shadow_world'], state['shadow_world'].split(':', 1)[1], *state['experiments'].values(),
           *(s['strategy_id'] for s in state['strategies'].values()), f['pg']]
    results = [p for p in (f['tmp'] / 'results').glob('nx043-*.json') if p.name != 'nx043-state.json']  # the state file holds the ids, privately
    for text in texts + [p.read_text() for p in results]:
        for x in ids:
            assert x not in text, x
    assert all(oct(p.stat().st_mode & 0o777) == '0o600' for p in (f['tmp'] / 'results').glob('*.json'))


def test_the_dry_run_calls_nothing_and_the_gaps_are_blocked_never_passed(stage):
    f = stage
    before = f['admin'].execute('select count(*) from control.nexloop_experiments').fetchone()[0]
    for name in ('prepare', 'run', 'decide', 'report', 'probe-before', 'probe-after'):
        code, doc, _ = phase(f, name, '--dry-run')
        assert code == 0 and doc['dry_run'] is True and doc['passed'] is None and list(steps(doc)) == ['plan']
    assert f['owner'].calls == [] and f['admin'].execute('select count(*) from control.nexloop_experiments').fetchone()[0] == before
    assert not (f['tmp'] / 'results').exists()
    code, doc, _ = phase(f, 'gaps')
    # Item 10 is the budget drill since 0364 (owner 2026-10-28); online was opened 2026-10-21: nothing is blocked any more.
    assert code == 0 and doc['passed'] is True and doc['blocked'] == [] and 'experiment_budget' not in json.dumps(doc)


def test_the_online_part_starts_allocates_on_inbound_and_stops(stage):
    """Owner 2026-10-21: online is open in the test stage; real inbound traffic allocates, the owner stops it."""
    f = stage
    assert phase(f, 'prepare')[0] == 0
    code, doc, _ = phase(f, 'online')
    assert code == 0 and doc['passed'] is True, [x for x in doc['steps'] if x['passed'] is not True]
    for i in range(4):
        inbound(f['admin'], sha(f'x7-online-{i}'))
    code, doc, _ = phase(f, 'online-probe')
    assert code == 0 and doc['passed'] is True, doc
    seen = steps(doc)['online_allocated']['observed']
    assert seen['candidate'] + seen['control'] + seen['excluded'] == 4 and seen['candidate'] + seen['control'] >= 1
    code, doc, _ = phase(f, 'online-stop')
    assert code == 0 and doc['passed'] is True, doc
    code, doc, _ = phase(f, 'online-drain')  # nothing was sent: nothing in flight
    assert code == 0 and steps(doc)['online_nothing_in_flight']['observed'] == {'outreach_sends': 0, 'outreach_in_flight': 0, 'effects_in_flight': 0}
    state = json.loads((f['tmp'] / 'results' / 'nx043-state.json').read_text())
    assert f['admin'].execute('select runtime.nexloop_experiment_status(%s,%s)', (TENANT_A, state['experiments']['online'])).fetchone()[0] == 'stopped'


def test_decide_without_the_worker_or_the_key_file_is_blocked(stage):
    f = stage
    phase(f, 'prepare')
    phase(f, 'run')
    code, doc, _ = phase(f, 'decide')  # no simulation worker configured
    assert code == 1 and doc['passed'] is None
    state = json.loads((f['tmp'] / 'results' / 'nx043-state.json').read_text())
    code, doc, _ = phase(f, 'decide', ports=worker_ports(f, state['shadow_world']))  # no key file from shadow-keys
    assert code == 1 and doc['passed'] is None and 'no private key file' in doc['blocked'][0]
    code, doc, _ = phase(f, 'shadow-keys')  # no real inbound yet: nothing copied is a failure of that step
    assert code == 1 and doc['passed'] is False


def test_the_parts_must_be_one_run_each_once_in_order(stage):
    """L6 X7 review 3.1: the --from parts carry one run_nonce, each part once, in the chain's order."""
    f = stage
    phase(f, 'prepare')
    phase(f, 'run')
    for i in range(2):
        inbound(f['admin'], sha(f'x7-chain-{i}'))
    worker = worker_ports(f, json.loads((f['tmp'] / 'results' / 'nx043-state.json').read_text())['shadow_world'])
    _, _, before = phase(f, 'probe-before')
    _, _, keys = phase(f, 'shadow-keys')
    _, _, decide = phase(f, 'decide', ports=worker)
    other = json.loads(Path(keys).read_text())
    other['run_nonce'] = 'zzzzzzzzzz'
    forged = f['tmp'] / 'forged.json'
    forged.write_text(json.dumps(other))
    for args in ((before, str(forged), decide), (before, keys, keys)):  # another run's part; a part twice (argument order means nothing)
        out = io.StringIO()
        assert X.main(['probe-after', '--config', f['config'], *sum((['--from', a] for a in args), [])], ports=f['ports'], out=out) == 2, args
        assert 'refused' in out.getvalue()
    # A later part is never one of the prior parts (L6 X7 fix verify): a probe-after result offered in place of decide.
    code, _, after = phase(f, 'probe-after', '--from', before, '--from', keys, '--from', decide)
    out = io.StringIO()
    assert X.main(['probe-after', '--config', f['config'], '--from', before, '--from', keys, '--from', after], ports=f['ports'], out=out) == 2
    assert 'exactly the parts' in out.getvalue()
    # Out of order in time: the decide part dated before probe-before.
    late = json.loads(Path(before).read_text())
    late['executed_at'] = '2999-01-01T00:00:00+00:00'
    moved = f['tmp'] / 'moved.json'
    moved.write_text(json.dumps(late))
    out = io.StringIO()
    assert X.main(['probe-after', '--config', f['config'], '--from', str(moved), '--from', keys, '--from', decide], ports=f['ports'], out=out) == 2


def test_no_path_from_a_report_or_the_guard_to_a_strategy_publication(stage):
    """D8 (the winner is never promoted automatically): no report, report Action or guard function writes a strategy version or event
    or calls a publication."""
    admin = stage['admin']
    fns = ('runtime.nexloop_experiment_report_freeze', 'runtime.nexloop_experiment_pooled_freeze', 'runtime.nexloop_shadow_report_freeze',
           'control.nexloop_governed_read_experiment_report', 'runtime.nexloop_experiment_guard', 'runtime.nexloop_experiment_autostop',
           'authz.nexloop_experiment_guard_tick', 'runtime.nexloop_experiment_result')
    for fn in fns:
        bodies = [b for (b,) in admin.execute("select prosrc from pg_proc p join pg_namespace n on n.oid=p.pronamespace where n.nspname||'.'||p.proname=%s", (fn,))]
        assert bodies, fn
        for body in bodies:
            low = body.lower()
            for forbidden in ('nexloop_strategy_versions', 'nexloop_strategy_events', 'publish_strategy', 'semantic_release', 'nexloop_governed_publish'):
                assert forbidden not in low or 'select' in low.split(forbidden)[0][-80:], (fn, forbidden)
            assert not re.search(r'(insert into|update|delete from)\s+control\.nexloop_strategy', low), fn


def test_bad_input_is_refused(stage, tmp_path):
    f = stage
    bad = tmp_path / 'bad.json'
    bad.write_text(json.dumps({'schema': 'other'}))
    bad.chmod(0o600)
    out = io.StringIO()
    assert X.main(['run', '--config', str(bad)], ports=f['ports'], out=out) == 2
    out = io.StringIO()
    assert X.main(['probe-after', '--config', f['config']], ports=f['ports'], out=out) == 2  # --from probe-before required
    assert X.main(['no-such-phase', '--config', f['config']], ports=f['ports'], out=io.StringIO()) == 2


def test_readiness_is_read_only_and_says_ready_or_blocked_with_a_reason(stage):
    """Dispatcher 2026-10-22: before the stage run, one read-only check (the inspector DSN) of what this run needs."""
    f = stage
    admin = f['admin']
    count = lambda: admin.execute('select (select count(*) from control.nexloop_experiments)+(select count(*) from control.nexloop_shadow_copy_events)').fetchone()[0]  # noqa: E731
    before = count()
    code, doc, _ = phase(f, 'readiness')
    assert count() == before and f['owner'].calls == []  # read only, no owner login
    s = steps(doc)
    assert code == 0 and doc['passed'] is True, [x for x in doc['steps'] if x['passed'] is not True]
    assert set(s) == {'sim_world_active', 'screening_twin_uncalibrated', 'candidate_strategy_publishable', 'control_strategy_publishable', 'shadow_copy_off', 'real_inbound_allocation',
                      'real_dispatch_state', 'no_conflicting_experiment'}
    assert all(x['observed']['status'] == 'ready' and x['observed']['reason'] for x in s.values())
    assert s['real_dispatch_state']['observed']['reason'] == 'closed_sends_refused'  # no switch row: the tenant layer is closed
    assert s['real_dispatch_state']['observed']['deployment'] == 'not_observable_from_db'
    # Blocked, each with its reason: the shadow copy left on, a running online experiment, a lever that is no D1 lever, an unknown world.
    assert phase(f, 'prepare')[0] == 0 and phase(f, 'run')[0] == 0 and phase(f, 'online')[0] == 0
    config = json.loads(open(f['config']).read())
    config['inputs'] = {**config['inputs'], 'candidate_levers': {'price': 1}, 'screening': {'world': 'sim:' + '0' * 32, 'twin': config['inputs']['screening']['twin']}}
    Path(f['config']).write_text(json.dumps(config))
    code, doc, _ = phase(f, 'readiness')
    s = steps(doc)
    assert code == 1 and doc['passed'] is False
    assert {k: v['observed']['reason'] for k, v in s.items() if v['observed']['status'] == 'blocked'} == {
        'sim_world_active': 'not_found', 'candidate_strategy_publishable': 'lever_not_allowed', 'shadow_copy_off': 'on_for_another_world',
        'no_conflicting_experiment': 'online_running'}
    text = json.dumps(doc)
    assert config['inputs']['screening']['world'] not in text and TENANT_A not in text


def test_online_drain_fails_while_a_real_send_is_in_flight(stage):
    f = stage
    config = json.loads(open(f['config']).read())
    config['inputs'] = {**config['inputs'], 'drain_timeout_seconds': 1, 'drain_poll_seconds': 1}
    Path(f['config']).write_text(json.dumps(config))
    assert phase(f, 'prepare')[0] == 0 and phase(f, 'online')[0] == 0
    send(f['admin'], sha('x7-not-a-member'))  # other real outreach of the tenant (L6 review 1.3): not counted
    assert phase(f, 'online-drain')[0] == 0
    member = sha('x7-in-flight')
    inbound(f['admin'], member)  # allocated on first sight
    send(f['admin'], member)  # accepted, never dispatched
    code, doc, _ = phase(f, 'online-drain')
    assert code == 1 and steps(doc)['online_nothing_in_flight']['observed'] == {'outreach_sends': 1, 'outreach_in_flight': 1, 'effects_in_flight': 0}


def test_a_read_only_role_is_made_read_only_by_the_script(pg, tmp_path):
    """L6 review 1.1: even a DSN without default_transaction_read_only connects read only, checked with show."""
    dsn = tmp_path / 'dsn'
    dsn.write_text(pg)
    dsn.chmod(0o600)
    with X.connect_read_only(str(dsn))() as db:
        assert db.execute('show default_transaction_read_only').fetchone()[0] == 'on'
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            db.execute('create temp table x7_write_probe(x int)')


def test_the_dispatch_layers_are_read_from_the_switch_rows_in_a_read_only_session(stage):
    """Stage finding (release 35): readiness read 'deployment' from closed_layer in the inspector session. The tenant and Action
    layers now come from the switch rows (read only, never the function); the deployment layer from the operator's observation."""
    f = stage
    admin = f['admin']
    source = (Path(X.__file__)).read_text()
    assert 'select control.nexloop_real_dispatch_closed_layer' not in source  # never called: it needs a worker's GUC and a row lock

    def switch(kind, ref, enabled, mode='real'):
        with admin.transaction():
            admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT_A,))
            admin.execute("""insert into control.nexloop_real_dispatch_switches(tenant_id,world,scope_kind,scope_ref,enabled,data_mode,revision,reason,changed_by,intent_id)
                values(%s,'real',%s,%s,%s,%s,1,'synthetic','synthetic-owner','synthetic')
                on conflict (tenant_id,world,scope_kind,scope_ref) do update set enabled=excluded.enabled,data_mode=excluded.data_mode""",
                          (TENANT_A, kind, ref, enabled, mode if kind == 'tenant' else None))
    state = lambda deployment=None: X.dispatch_state(f['ports'], TENANT_A, deployment)  # noqa: E731
    assert state()['layers'] == {'outreach.send': 'tenant', 'enterprise.deliver': 'tenant', 'deployment': 'not_observable_from_db'}
    switch('tenant', '*', True, 'test')
    assert state()['layers']['outreach.send'] == 'tenant'  # data_mode test is not real
    switch('tenant', '*', True)
    switch('action_type', X.DISPATCH['outreach.send'], True)
    assert state()['layers'] == {'outreach.send': 'open', 'enterprise.deliver': 'action', 'deployment': 'not_observable_from_db'}
    assert state()['reason'] == 'closed_sends_refused'
    switch('action_type', X.DISPATCH['enterprise.deliver'], True)
    assert state() == {'layers': {'outreach.send': 'open', 'enterprise.deliver': 'open', 'deployment': 'not_observable_from_db'},
                       'reason': 'open_in_database_deployment_not_observed'}
    assert state(True)['reason'] == 'open_sends_execute' and state(True)['layers']['deployment'] == 'open'
    assert state(False)['reason'] == 'closed_sends_refused' and state(False)['layers']['deployment'] == 'closed'
    # The readiness item carries the same, with the operator's observation from the inputs.
    config = json.loads(open(f['config']).read())
    config['inputs'] = {**config['inputs'], 'deployment_real_dispatch': True}
    Path(f['config']).write_text(json.dumps(config))
    observed = steps(phase(f, 'readiness')[1])['real_dispatch_state']['observed']
    assert observed['reason'] == 'open_sends_execute' and observed['outreach.send'] == observed['enterprise.deliver'] == observed['deployment'] == 'open'


def test_the_reason_is_read_from_the_error_bodys_details():
    """Stage X7 S2 (release 37): a refusal's code is in details.reason; a top-level reason still counts; anything else is None."""
    body = {'code': 'not_allowed_in_state', 'message': 'm', 'trace_id': 't', 'retryable': False, 'details': {'reason': 'twin_not_calibrated'}}
    assert X._reason(body) == 'twin_not_calibrated'
    assert X._reason({'reason': 'ended'}) == 'ended' and X._reason({'details': None}) is None and X._reason('text') is None


def test_the_base_waits_for_live_real_claims_and_blocks_naming_them(monkeypatch):
    """Third round S8 (release 38): the base was taken while the Runs of the S3 traffic still held Goal/PlanStep claims. The quiet
    check now also waits for no live (active, lease not run out) world-real Action claim; on timeout the base is not taken and the
    unfinished Actions are named (counts only)."""
    monkeypatch.setattr(X.time, 'sleep', lambda s: None)
    monkeypatch.setattr(X, 'real_digest', lambda ports, tenant: {'t': 'same'})
    live = {'claims': [('Goal.create', 2), ('PlanStep.create', 1)]}

    class Ports:
        def read(self, sql, params=()):
            if 'runtime.jobs' in sql:
                return [(0,)]
            if 'nexloop_action_claims' in sql:
                assert "claim->>'state'='active'" in sql and 'lease_expires_at' in sql
                return live['claims']
            raise AssertionError(sql)
    digests, quiet, unfinished = X._quiet_real(Ports(), TENANT_A, {'quiet_seconds': 1, 'quiet_timeout_seconds': -1})
    assert quiet is False and unfinished == {'unfinished_jobs': 0, 'unfinished_claims': {'Goal.create': 2, 'PlanStep.create': 1}}
    live['claims'] = []
    assert X._quiet_real(Ports(), TENANT_A, {'quiet_seconds': 1, 'quiet_timeout_seconds': -1})[1:] == (True, {})


class DrillProbe:
    """The read-only probe, except that the drill world's Run Action counts as published (AT-046 prepare's publish_sim_catalog, which
    this local fixture does not run)."""

    def __init__(self, pg):
        self.pg = pg

    def __call__(self):
        conn = psycopg.connect(self.pg, autocommit=True)
        execute = conn.execute

        def patched(sql, params=None, **kw):
            if 'control.nexloop_action_definitions' in sql:
                return execute('select 1')
            return execute(sql, params, **kw)
        conn.execute = patched
        return conn


def dispatch(admin, world, job, run):
    """What the simulation runtime worker does at its control stage: reserve the Run's declared maximum cost (1.0 CNY) in its world -
    the SQL triggers decide (0297 attribution, 0364 refusal) - then finish the task: refused NXB01 -> failed with code budget_exhausted
    (nexloop_eios.goal_controls.REASONS), else succeeded."""
    try:
        with admin.transaction():
            admin.execute("""insert into control.nexloop_budget_consumption(tenant_id,world,budget_kind,consumption_id,amount,unit,source_ref,principal_id)
                values(%s,%s,'model',%s,1.0,'CNY',%s,'sim-worker')""", (TENANT_A, world, 'run:' + run, 'run:' + run))
        status, result = 'succeeded', {}
    except psycopg.Error as error:
        from nexloop_eios.goal_controls import REASONS
        status, result = 'failed', {'code': REASONS[error.sqlstate]}
    admin.execute("update runtime.jobs set status=%s,result=%s where job_id=%s", (status, Jsonb(result), job))


def test_the_budget_drill_stops_at_the_cap_and_refuses_the_next_run_at_dispatch(stage, monkeypatch):
    """Item 10 (owner 2026-10-28, option A): budget-start, budget-before, budget-runs, budget-probe with the SQL of 0364 doing the work;
    only the intake and the worker's dispatch are stand-ins (the stage runs the AT-046 run chain)."""
    f = stage
    admin = f['admin']
    monkeypatch.setattr(X.time, 'sleep', lambda s: None)
    world = json.loads(Path(f['config']).read_text())['inputs']['screening']['world']
    admin.execute("select control.nexloop_governed_generate_sim_world_data(%s,'real','owner-principal','owner-subject','i-gen',%s)",
                  (TENANT_A, Jsonb({'operation': 'generate_sim_world_data', 'request_id': 'gen-drill', 'world': world, 'kind': 'synthetic', 'seed': 's',
                                    'consumers': 2, 'events_per_consumer': {'min': 1, 'max': 1}, 'segments': {'trial': 1}})))
    config = json.loads(Path(f['config']).read_text())
    config['inputs']['budget_drill'] = {'world': world, 'twin': {'twin_id': 'trial.service_consumer', 'version': 1}, 'gap_seconds': 1, 'run_timeout_seconds': 5}
    Path(f['config']).write_text(json.dumps(config))
    assert phase(f, 'prepare')[0] == 0
    code, doc, _ = phase(f, 'budget-start')
    assert code == 0 and doc['passed'] is True, [x for x in doc['steps'] if x['passed'] is not True]
    assert steps(doc)['budget_drill_registered']['observed']['notice'] == X.DRILL_NOTICE
    probe = at046_stage.Ports(owner=f['owner'], probe=DrillProbe(f['pg']))
    code, before, before_path = phase(f, 'budget-before', ports=probe)
    assert code == 0 and before['passed'] is True, before
    jobs = []

    def intake(w, consumer, effect):
        job, run = str(uuid.uuid4()), str(uuid.uuid4())
        with admin.transaction():
            admin.execute('set local session_replication_role=replica')
            admin.execute("""insert into runtime.jobs(job_id,invocation_id,capability_name,capability_version,capability_type,execution_mode,tenant_id,actor_id,trace_id,
                request_id,normalized_input,plan_snapshot,status,created_at,updated_at,world,queue,attempts,max_attempts)
                values(%s,%s,'nexloop.runtime.run','1','atomic','async',%s,'synthetic','synthetic',%s,'{}','{}','pending',now(),now(),%s,'operations',0,3)""",
                          (job, str(uuid.uuid4()), TENANT_A, str(uuid.uuid4()), w))
        jobs.append((job, run))
        dispatch(admin, w, job, run)  # the worker picks it up before the next intake (gap_seconds)
        return job, run
    code, runs, runs_path = phase(f, 'budget-runs', '--from', before_path, ports=at046_stage.Ports(intake=intake))
    assert code == 0 and runs['passed'] is True, runs
    code, doc, _ = phase(f, 'budget-probe', '--from', before_path, '--from', runs_path, ports=probe)
    got = steps(doc)
    assert code == 0 and doc['passed'] is True, [x for x in doc['steps'] if x['passed'] is not True]
    assert got['new_spend_refused_at_dispatch']['observed'] == {'task_status': 'failed', 'code': 'budget_exhausted', 'reserved': 0}
    assert got['first_run_kept']['observed']['ledger_entries'] == 1 and got['budget_exhausted_stops_the_drill']['observed']['reason'] == 'guardrail: budget_exhausted'
    # Positive control for the probe: had the second Run been let through, the probe fails.
    admin.execute("update runtime.jobs set status='succeeded',result='{}' where job_id=%s", (jobs[1][0],))
    code, doc, _ = phase(f, 'budget-probe', '--from', before_path, '--from', runs_path, ports=probe)
    assert code == 1 and steps(doc)['new_spend_refused_at_dispatch']['passed'] is False


def test_a_plain_screening_start_is_still_refused_beside_the_drill(stage):
    """Item 4 keeps its stage check: run's screening (no budget_drill) is refused twin_not_calibrated with the same uncalibrated twin."""
    f = stage
    assert phase(f, 'prepare')[0] == 0
    code, doc, _ = phase(f, 'run')
    assert steps(doc)['screening_start_needs_a_calibrated_twin']['observed'] == {'status': 409, 'reason': 'twin_not_calibrated'}
