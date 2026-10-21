"""Stage enrollment of the evolution principals (scripts/deploy/evolution_enroll.py; owner decision 2026-10-21): on a clean
bootstrapped catalog the tool issues the writer, evaluator and test-only evaluation credentials through the trusted configuration
(configurator role), registers the writer and evaluator principals and the D18 evaluation credential in the owner's window, and its
self-check holds the three safety properties: writer and evaluator exclusive (S6-R5), the evaluation credential test-only (D18),
no principal with any grant (AT-046). A dry run writes nothing; a second run changes nothing. With the enrolled credentials alone the
acceptance prerequisites hold: the writer opens a diagnosis, proposes and freezes a candidate, the evaluator opens its evaluation and
an L1 session with the test-only credential. Admin seeds only what the S6 acceptance seeds itself (synthetic Runs, the evolution budget).
"""
import importlib.util
import io
import json
import secrets as random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.evolution.attribution import attribute, draft_candidates
from nexloop_eios.evolution_store import CandidateStore, EvaluatorStore, StoreUnavailable, diagnosis_id
from support.tenants import TENANT_A
from test_evolution_e4_pg import NOW, hexid, miss_case
from test_evolution_traces_pg import run

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('evolution_enroll', ROOT / 'scripts/deploy/evolution_enroll.py')
E = importlib.util.module_from_spec(spec)
spec.loader.exec_module(E)
SEED = '0' * 32
MARKER = 'synthetic-config-marker'


def private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o600)
    return path


@pytest.fixture
def enroll(pg, admin, tmp_path):
    bootstrap(admin)
    admin.execute('alter role nexloop_configurator login')
    cfg = {'schema': E.SCHEMA, 'tenant_id': TENANT_A, 'secret_dir': str(tmp_path / 'secrets'),
           'configurator_database_url_file': str(private(tmp_path / 'configurator-dsn', make_conninfo(pg, user='nexloop_configurator'))),
           'registry_database_url_file': str(private(tmp_path / 'bootstrap-dsn', pg)),
           'signing_key_file': str(private(tmp_path / 'signing.key.hex', random.token_hex(32))), 'signing_key_id': 'stage-evo-1',
           'credential_expires_at': (datetime.now(UTC) + timedelta(days=30)).replace(microsecond=0).isoformat(), 'registered_by': MARKER}
    config = private(tmp_path / 'evolution-enroll.json', json.dumps(cfg))

    def call(*extra):
        out = io.StringIO()
        code = E.main(['--config', str(config), *extra], out=out)
        return code, json.loads(out.getvalue()), out.getvalue()
    return dict(pg=pg, admin=admin, cfg=cfg, config=config, call=call, secrets=Path(cfg['secret_dir']))


def state(admin):
    """Everything the tool may write: credentials, authority facts, both registries, the signing keys."""
    out = {}
    for t in ('authz.nexloop_service_credentials', 'authz.nexloop_authority_facts', 'authz.nexloop_evo_principals', 'authz.nexloop_evo_evaluation_credentials',
              'authz.nexloop_authority_signing_keys'):
        out[t] = admin.execute(f"select md5(coalesce(string_agg(x::text,'|' order by x::text),'')) from {t} x").fetchone()[0]
    return out


def token(f, role):
    return (f['secrets'] / next(p['file'] for p in E.PRINCIPALS if p['role'] == role)).read_text()


def test_dry_run_writes_nothing_then_enrollment_passes_its_self_check_and_a_rerun_changes_nothing(enroll):
    f = enroll
    admin = f['admin']
    before = state(admin)
    code, dry, _ = f['call']('--dry-run')
    assert code == 0 and dry['mode'] == 'dry_run' and sorted(dry['tokens_to_create']) == sorted(p['file'] for p in E.PRINCIPALS)
    assert [c['credentials_to_create'] for c in dry['credentials']] == [['evolution_evaluator', 'evolution_writer'], ['evolution_evaluation']]
    assert len(dry['registry']['planned']) == 3 and dry['registry']['written'] == []
    assert state(admin) == before and not f['secrets'].exists()  # no file, no row
    code, first, text = f['call']()
    assert code == 0 and first['ok'] is True and first['changed'] is True, first
    assert first['checks'] == {'writer_evaluator_exclusive': True, 'evaluation_credential_test_only': True, 'no_effect_authority': True}
    assert first['login_roles'] == {'nexloop_evo_writer': True, 'nexloop_evo_evaluator': True}
    assert sorted(first['tokens_created']) == sorted(p['file'] for p in E.PRINCIPALS)
    assert [c['grants_added'] for c in first['credentials']] == [0, 0]
    for p in E.PRINCIPALS:
        path = f['secrets'] / p['file']
        assert path.stat().st_mode & 0o777 == 0o600 and path.read_text() not in text  # never printed
    assert f['cfg']['signing_key_file'] not in text and 'nexloop_configurator' not in text and 'host=' not in text
    enrolled = state(admin)
    # A second run (and a dry run after it) finds everything in place.
    code, again, _ = f['call']()
    assert code == 0 and again['ok'] is True and again['changed'] is False and again['registry']['written'] == [] and again['tokens_created'] == [], again['credentials']
    code, dry2, _ = f['call']('--dry-run')
    assert code == 0 and dry2['tokens_to_create'] == [] and dry2['registry']['planned'] == []
    assert all(c['credentials_to_create'] == [] and c['facts_to_write'] == 0 for c in dry2['credentials'])
    assert state(admin) == enrolled
    assert f['call']('--check')[0] == 0


def test_self_check_writer_and_evaluator_stay_exclusive(enroll):
    """S6-R5 / D39-1: one purpose per principal - the registry refuses the second purpose, the writer's token cannot evaluate,
    the evaluator's token cannot write."""
    f = enroll
    admin = f['admin']
    assert f['call']()[0] == 0
    writer_pid = E.identifiers('evolution_writer')['principal_id']
    with pytest.raises(psycopg.errors.UniqueViolation):
        admin.execute("insert into authz.nexloop_evo_principals(principal_id,tenant_id,purpose,registered_by) values(%s,%s,'evaluator','x')", (writer_pid, TENANT_A))
    writer = CandidateStore(lambda: psycopg.connect(make_conninfo(f['pg'], user='nexloop_evo_writer'), autocommit=True),
                            f['secrets'] / 'evolution-evaluator/service-credential')
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='evaluator principal'):
        writer.read('00000000-0000-0000-0000-000000000000')
    evaluator = EvaluatorStore(lambda: psycopg.connect(make_conninfo(f['pg'], user='nexloop_evo_evaluator'), autocommit=True),
                               f['secrets'] / 'evolution-writer/service-credential')
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='evaluation unavailable'):
        evaluator.open('00000000-0000-0000-0000-000000000000', 'a' * 64)
    # The self-check catches a writer token reused as the evaluator's (positive control of the check).
    private(f['secrets'] / 'evolution-evaluator/service-credential', token(f, 'evolution_writer'))
    code, out, _ = f['call']('--check')
    assert code == 2 and 'different tokens' in out['refused']


def test_self_check_the_evaluation_credential_is_test_only(enroll):
    """D18: the evaluation credential is issued for world test alone, never authenticates in world real, and is registered."""
    f = enroll
    admin = f['admin']
    assert f['call']()[0] == 0
    test_digest = hexid(token(f, 'evolution_evaluation'))
    assert admin.execute('select worlds from authz.nexloop_service_credentials where token_digest=%s', (test_digest,)).fetchone()[0] == ['test']
    assert admin.execute('select count(*) from authz.nexloop_evo_evaluation_credentials where token_digest=%s', (test_digest,)).fetchone()[0] == 1
    with pytest.raises(psycopg.errors.InsufficientPrivilege), admin.transaction():
        admin.execute("select authz.nexloop_service_identity_snapshot(%s,'real')", (test_digest,))
    # The registry itself refuses a real credential (E5 trigger), so the writer's token can never become an evaluation credential.
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='world test only'):
        admin.execute("insert into authz.nexloop_evo_evaluation_credentials(token_digest,tenant_id,registered_by) values(%s,%s,'x')",
                      (hexid(token(f, 'evolution_writer')), TENANT_A))
    # Positive control: a registration the check did not make visible fails it (evaluation credential row missing -> not test-only).
    assert E.self_check(E.load_config(f['config'].read_text()), {'evolution_writer': token(f, 'evolution_writer'),
                                                               'evolution_evaluator': token(f, 'evolution_evaluator'),
                                                               'evolution_evaluation': random.token_urlsafe(48)})['checks']['evaluation_credential_test_only'] is False


def test_self_check_no_principal_holds_any_grant_so_no_effect_authority(enroll):
    """AT-046: none of the three holds a grant, scope or policy; a grant added by hand is caught by the self-check."""
    f = enroll
    admin = f['admin']
    assert f['call']()[0] == 0
    pids = [E.identifiers(p['role'])['principal_id'] for p in E.PRINCIPALS]
    assert admin.execute("select count(*) from authz.nexloop_authority_facts where fact_kind in ('grants','scope','policies','controls') and entity_key[1] = any(%s)",
                         (pids,)).fetchone()[0] == 0
    # Positive control: one effect grant fact for the writer turns the check red.
    admin.execute("""insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) values(%s,'grants',%s,%s)""",
                  (TENANT_A, [pids[0], 'eios:action:nexloop.service.request:1'], json.dumps({'tenant_id': TENANT_A, 'principal_id': pids[0], 'grants': [{}]})))
    code, out, _ = f['call']('--check')
    assert code == 1 and out['checks']['no_effect_authority'] is False and out['checks']['writer_evaluator_exclusive'] is True


def test_the_enrolled_credentials_meet_the_acceptance_prerequisites(enroll):
    """With the enrolled credentials only: diagnosis, candidate, frozen bundle (L0+L1), evaluation opened, L1 session opened."""
    f = enroll
    admin, pg = f['admin'], f['pg']
    assert f['call']()[0] == 0
    writer_token = token(f, 'evolution_writer')
    admin.execute("""insert into control.nexloop_budget_limits(tenant_id,world,budget_kind,unit,limit_amount,period_start,period_end,revision)
        values(%s,'real','evolution','CNY',100,%s,%s,1)""", (TENANT_A, NOW - timedelta(days=1), NOW + timedelta(days=29)))
    consumers = [hexid('enroll-' + str(i)) for i in range(3)]
    samples, accesses = [], []
    for i in range(6):
        rid = run(admin, hexid(writer_token), NOW - timedelta(minutes=10 - i), consumer=consumers[i % 3], link='message', tenant=TENANT_A)
        s, a = miss_case(rid, consumers[i % 3])
        samples.append(s)
        accesses += a
    store = CandidateStore(lambda: psycopg.connect(make_conninfo(pg, user='nexloop_evo_writer'), autocommit=True), f['secrets'] / 'evolution-writer/service-credential')
    d = attribute(samples, accesses)
    drafts, _ = draft_candidates(d, samples, tenant_id=TENANT_A, parent_revision='sem-7', evaluator_version='l0-1', model_profile='none',
                                 created_at=NOW.isoformat().replace('+00:00', 'Z'))
    store.open_diagnosis(d, samples, parent_revision='sem-7', evaluator_version='l0-1', model_profile='none')
    candidate = store.propose(diagnosis_id(d.snapshot_digest), drafts[0])['candidate']['candidate_id']
    bundle_digest = store.freeze_bundle(candidate, levels=('L0', 'L1'), seed=SEED)['bundle_digest']
    evaluator = EvaluatorStore(lambda: psycopg.connect(make_conninfo(pg, user='nexloop_evo_evaluator'), autocommit=True),
                               f['secrets'] / 'evolution-evaluator/service-credential')
    assert evaluator.open(candidate, bundle_digest)['opened'] is True
    session = evaluator.l1_open(candidate, hexid(token(f, 'evolution_evaluation')))
    assert session['opened'] is True and evaluator.l1_close(session['session_id'])
    # The writer's token never opens an evaluation through the evaluator role (unregistered as evaluator).
    with pytest.raises((psycopg.errors.InsufficientPrivilege, StoreUnavailable)):
        EvaluatorStore(evaluator.connect, f['secrets'] / 'evolution-writer/service-credential').open(candidate, bundle_digest)


def test_inputs_are_refused_without_touching_the_database(enroll, tmp_path):
    f = enroll
    before = state(f['admin'])
    bad = json.loads(f['config'].read_text())
    for change in ({'schema': 'x'}, {'credential_expires_at': '2000-01-01T00:00:00+00:00'}, {'tenant_id': 'bad tenant'}, {'extra': 'x'}):
        path = private(tmp_path / 'bad.json', json.dumps({**bad, **change}))
        out = io.StringIO()
        assert E.main(['--config', str(path)], out=out) == 2 and json.loads(out.getvalue())['ok'] is False
    f['config'].chmod(0o644)
    assert f['call']()[0] == 2
    assert state(f['admin']) == before and not f['secrets'].exists()
