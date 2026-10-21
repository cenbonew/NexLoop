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
        handler = self.admin.execute('select handler from control.nexloop_governed_capabilities where operation=%s', (op,)).fetchone()[0].split('(')[0]
        try:
            with self.admin.transaction():
                out = self.admin.execute(f"select {handler}(%s,'real','owner-principal','owner-subject',%s,%s)",
                                         (TENANT_A, 'intent-' + uuid.uuid4().hex[:12], Jsonb({'operation': op, 'request_id': extra['Idempotency-Key'], **body}))).fetchone()[0]
            return 200, out
        except psycopg.errors.InvalidParameterValue as refused:
            code = re.search(r': ([a-z_]{1,40})$', refused.diag.message_primary or '')
            return 409, {'error': 'not_allowed_in_state', 'reason': code.group(1) if code else None}

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
    assert code == 1 and doc['passed'] is None and len(doc['blocked']) == 3  # allocation wiring, experiment budget, online stage


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
