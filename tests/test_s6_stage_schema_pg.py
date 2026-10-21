"""scripts/acceptance/s6_stage_schema.py, the local positive control on PostgreSQL: the same checks a stage run executes, against a
clean catalog with the E4 / V1 / F4 fixtures (the writer and evaluator login roles with their credentials) and the production
workbench app for the owner (real login, the Schema publication routes). r6 runs the owner's governed handlers directly (the functions
the workbench actions entry calls; that entry has its own tests). Synthetic data only.

Also: the dry run calls nothing; a missing prerequisite is `blocked` (passed null), never a pass; results carry no private value.
"""
import io
import json
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/acceptance'))
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
import s6_stage_schema as S  # noqa: E402

from authority_fixture import seed_authority  # noqa: E402
from nexloop_eios.workbench_roles import compile_members  # noqa: E402
from real_dispatch_switch import Workbench  # noqa: E402
from test_browser_http import ORIGIN  # noqa: E402
from datetime import timedelta  # noqa: E402
from nexloop_eios.evolution.attribution import attribute, draft_candidates  # noqa: E402
from nexloop_eios.evolution_store import CandidateStore  # noqa: E402
from test_evolution_e4_pg import NOW, body_of, hexid, miss_case, opened  # noqa: E402
from test_evolution_traces_pg import run as trace_run  # noqa: E402
from test_evolution_traces_pg import private  # noqa: E402,F811
from test_evolution_v1_pg import register  # noqa: E402
from test_workbench_read_pg import ROLES, members_file, workbench, write_facts, configure  # noqa: E402,F401
from test_workbench_read_pg import TENANT, browser_business, conversations, identity, published_action, uow  # noqa: E402,F401


class AppTransport:
    def __init__(self, client):
        self.client = client

    def request(self, method, path, headers, body=None):
        response = self.client.request(method, path, headers=headers, content=body)
        return response.status_code, list(response.headers.multi_items()), response.content


def config_file(tmp_path, **inputs):
    c = {'schema': 'nexloop-s6-stage/1', 'executor': 'L2 local positive control', 'release': 'local', 'tenant_id': TENANT,
         'results_dir': str(tmp_path / 'results'), 'inputs': inputs}
    path = tmp_path / 's6.json'
    path.write_text(json.dumps(c))
    path.chmod(0o600)
    return path


def writer_evo(admin, pg, tmp_path):
    """E4's evo fixture on the workbench's database, with its own credential suffix (the workbench already holds the default one)."""
    token, _ = seed_authority(admin, TENANT, 'eios:artifact:' + 'e' * 32, identity_suffix='-s6-writer')
    digest = hexid(token)
    admin.execute("""insert into control.nexloop_budget_limits(tenant_id,world,budget_kind,unit,limit_amount,period_start,period_end,revision)
        values(%s,'real','evolution','CNY',100,%s,%s,1) on conflict do nothing""", (TENANT, NOW - timedelta(days=1), NOW + timedelta(days=29)))
    consumers = [hexid('s6-' + str(i)) for i in range(3)]
    samples, accesses = [], []
    for i in range(6):
        rid = trace_run(admin, digest, NOW - timedelta(minutes=10 - i), consumer=consumers[i % 3], link='message')
        smp, acc = miss_case(rid, consumers[i % 3])
        samples.append(smp)
        accesses += acc
    connect = lambda: psycopg.connect(make_conninfo(pg, user='nexloop_evo_writer'), autocommit=True)  # noqa: E731
    credential = private(tmp_path / 's6-writer', token)
    d = attribute(samples, accesses)
    drafts, _ = draft_candidates(d, samples, tenant_id=TENANT, parent_revision='sem-7', evaluator_version='l0-1', model_profile='none',
                                 created_at=NOW.isoformat().replace('+00:00', 'Z'))
    return dict(admin=admin, pg=pg, store=CandidateStore(connect, credential), credential=credential, digest=digest, samples=samples, diagnosis=d, drafts=drafts)


@pytest.fixture
def stage(workbench, pg, tmp_path):
    w = workbench
    admin = w['admin']
    e = writer_evo(admin, pg, tmp_path)
    configure(w, (w['owner'], 'owner'), (w['operator'], 'operator'))
    write_facts(admin, compile_members(ROLES, members_file((w['owner'], 'owner'), (w['operator'], 'operator'))))
    opened(e)
    template = tmp_path / 'candidate-template.json'
    template.write_text(json.dumps(body_of(e)))
    template.chmod(0o600)
    token, _ = seed_authority(admin, TENANT, 'eios:artifact:' + 'f' * 32, identity_suffix='-s6-evaluator')
    register(admin, hexid(token), 'evaluator')
    # D23 needs a valid and an invalid case per Action: the fixture's Consumer Action takes any input ({}); a real input schema first (an
    # Action that accepts everything fails G12 for ever - F4's known limitation, also on stage).
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("""update control.nexloop_action_definitions set definition=jsonb_set(definition,'{input_schema}',%s)
            where tenant_id=%s and resource_id like 'eios:action:Consumer.%%'""",
                      (Jsonb({'type': 'object', 'required': ['consumer_id'], 'additionalProperties': False, 'properties': {'consumer_id': {'type': 'string'}}}), TENANT))
    clients = []

    def ports(member='owner'):
        client = w['client']().__enter__()
        clients.append(client)
        owner = Workbench(AppTransport(client), ORIGIN)
        owner.login(w[member]['username'], w[member]['password'])
        return S.Ports(writer_connect=lambda: psycopg.connect(make_conninfo(e['pg'], user='nexloop_evo_writer'), autocommit=True), writer_digest=e['digest'],
                       writer_credential=e['credential'],
                       evaluator_connect=lambda: psycopg.connect(make_conninfo(e['pg'], user='nexloop_evo_evaluator'), autocommit=True),
                       evaluator_credential=private(tmp_path / 's6-ev', token),
                       verify_connect=lambda: psycopg.connect(make_conninfo(e['pg'], user='nexloop_api'), autocommit=True), owner=owner)
    inputs = {'candidate_template': str(template), 'schema_type': 'Consumer', 'new_property': 's6_preferred_store', 'new_type': 'S6Voucher'}
    yield {'admin': admin, 'ports': ports, 'inputs': inputs, 'w': w, 'e': e}
    for client in clients:
        client.__exit__(None, None, None)


def run(tmp_path, check, ports, inputs, *extra):
    out = io.StringIO()
    code = S.main([check, '--config', str(config_file(tmp_path, **inputs)), *extra], ports=ports, out=out)
    return code, json.loads(out.getvalue())


def steps(doc):
    return {s['name']: s for s in doc['steps']}


def schema_counts(admin):
    return (admin.execute('select count(*) from ontology.object_type_versions where tenant_id=%s', (TENANT,)).fetchone()[0],
            admin.execute('select count(*) from ontology.nexloop_schema_publications').fetchone()[0])


def test_at024_refuses_at_the_write_the_freeze_and_the_publication_gate(stage, tmp_path):
    s = stage
    before = schema_counts(s['admin'])
    code, doc = run(tmp_path, 'at024', s['ports'](), s['inputs'])
    assert code == 0 and doc['passed'] is True and doc['acceptance'] == ['AT-024', 'S6-R9'], json.dumps(doc, ensure_ascii=False)
    st = steps(doc)
    assert st['candidate_write_refuses_an_action_in_scope']['observed']['message'].endswith('change scope not allowed')
    assert st['proposal_freeze_refuses_a_carried_action']['passed'] and st['proposal_freeze_refuses_a_type_outside_scope']['passed']
    assert st['new_type_publishes_no_action']['observed']['successor_actions'] == 0
    assert st['publication_gate_refuses_without_accepted_gates']['observed']['codes'] == ['permit_missing']
    assert schema_counts(s['admin']) == before  # nothing published
    assert len(list((tmp_path / 'results').iterdir())) == 1


def test_r8_goes_through_g9_g13_and_the_catalog_stays(stage, tmp_path):
    s = stage
    code, doc = run(tmp_path, 'r8', s['ports'](), s['inputs'])
    assert code == 0 and doc['passed'] is True, json.dumps(doc, ensure_ascii=False)
    st = steps(doc)
    assert st['g9_g13_accept']['observed']['conclusion'] == 'accept' and st['g9_g13_accept']['observed']['failed_gates'] == []
    assert st['catalog_and_verify_unchanged']['observed']['before'] == st['catalog_and_verify_unchanged']['observed']['after']
    # --publish: the owner's B-class step; still no catalog change.
    code, doc = run(tmp_path, 'r8', s['ports'](), {**s['inputs'], 'new_property': 's6_shoe_size'}, '--publish')
    assert code == 0 and steps(doc)['owner_publication_changes_no_catalog']['observed']['published'] is True, json.dumps(doc, ensure_ascii=False)


def test_r10_three_breaking_changes_each_return_their_code(stage, tmp_path):
    s = stage
    code, doc = run(tmp_path, 'r10', s['ports'](), {**s['inputs'], 'schema_type': 'Message', 'new_property': 's6_channel_hint'})
    assert code == 0 and doc['passed'] is True, json.dumps(doc, ensure_ascii=False)
    codes = {name: st['observed']['codes'] for name, st in steps(doc).items()}
    assert 'breaking_remove_property' in codes['breaking_removed_property_refused']
    assert 'breaking_change_value_type' in codes['breaking_changed_value_type_refused']
    assert 'breaking_required_property' in codes['breaking_required_property_refused']


def test_a_missing_prerequisite_is_blocked_never_a_pass_and_the_dry_run_calls_nothing(tmp_path):
    class Nothing(S.Ports):
        def __getattribute__(self, name):
            if name in ('owner_get', 'owner_post', 'propose', 'freeze_proposal', 'evaluator', 'verify', 'freeze_bundle'):
                raise AssertionError('called ' + name)
            return super().__getattribute__(name)
    code, doc = run(tmp_path, 'at024', Nothing(), {'schema_type': 'T', 'new_property': 'p', 'new_type': 'N', 'candidate_template': '/x'}, '--dry-run')
    assert code == 0 and doc['dry_run'] is True and doc['passed'] is None and not (tmp_path / 'results').exists()
    code, doc = run(tmp_path, 'at024', S.Ports(), {'schema_type': 'T', 'new_property': 'p', 'new_type': 'N'})
    assert code == 1 and doc['passed'] is None and doc['blocked'] and 'candidate_template' in doc['blocked'][0]
    code, doc = run(tmp_path, 'r6', S.Ports(), {})
    assert code == 1 and doc['passed'] is None and 'inputs.semantic' in doc['blocked'][0]


def test_results_carry_no_private_value(stage, tmp_path):
    s = stage
    code, doc = run(tmp_path, 'r10', s['ports'](), {**s['inputs'], 'schema_type': 'Message', 'new_property': 's6_channel_hint'})
    text = json.dumps(doc) + ''.join(p.read_text() for p in (tmp_path / 'results').iterdir())
    assert code == 0 and str(tmp_path) not in text and TENANT not in text and 'postgresql://' not in text
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in (tmp_path / 'results').iterdir())


# r6: the owner's governed handlers (what the workbench actions entry calls) behind the same Ports interface ----------------------
from test_semantic_release_pg import ALIAS, accepted, call, clone, seed  # noqa: E402,F401
from test_evolution_e4_pg import REF  # noqa: E402
from test_evolution_v2_pg import opened_eval  # noqa: E402,F401
from test_evolution_v1_pg import evaluation  # noqa: E402,F401
from test_evolution_e5_pg import proposed  # noqa: E402,F401
from test_evolution_e4_pg import evo  # noqa: E402,F401
from support.tenants import TENANT_A  # noqa: E402


class HandlerOwner(S.Ports):
    """Owner reads and Actions answered by the SQL handlers the governed entry calls (status, body) - the HTTP entry has its own tests."""

    def __init__(self, admin):
        super().__init__()
        self.admin = admin

    def owner_get(self, path):
        if path == '/api/v1/workbench/semantic-review':
            return 200, self.admin.execute("select runtime.nexloop_evo_review_summary(%s,'real')", (TENANT_A,)).fetchone()[0]
        assert path == '/api/v1/workbench/semantic-releases'
        row = self.admin.execute('select version from ontology.nexloop_semantic_release_pointers where tenant_id=%s', (TENANT_A,)).fetchone()
        return 200, {'current': row[0] if row else None}

    def owner_post(self, path, body, key):
        name = path.rsplit('/', 1)[1]
        fn = {'publish_semantic_release': 'nexloop_governed_publish_semantic_release', 'rollback_semantic_release': 'nexloop_governed_rollback_semantic_release'}[name]
        try:
            return 200, call(self.admin, fn, {'operation': name, 'request_id': key, **body})
        except psycopg.Error as error:
            return 409, {'code': 'not_allowed_in_state', 'details': {'reason': str(error)}}


def test_r6_publish_then_rollback_new_version_claims_unchanged_watch_active(accepted, tmp_path):
    f = accepted
    admin = f['admin']
    note = clone(f, conclusion='accept', change_class='patch', change_kind='description')
    assert call(admin, 'nexloop_governed_publish_semantic_release', {'operation': 'publish_semantic_release', 'request_id': 'r0', 'candidate_id': note,
                                                                     'items': [{'kind': 'semantic_note', 'ref': REF['ref'], 'text': '只指实物商品'}], 'reason': '先有一个版本'})['version'] == '0.0.1'
    seed(admin, ("update runtime.nexloop_evo_candidates set parent_revision='release:0.0.1' where candidate_id=%s", (f['candidate'],)))
    claims = lambda: admin.execute("select md5(coalesce(string_agg(x::text,'|' order by x::text),'')) from ontology.nexloop_claims x").fetchone()  # noqa: E731
    before = claims()
    owner = HandlerOwner(admin)
    inputs = {'semantic': {'candidate_id': f['candidate'], 'items': [{'kind': 'alias', 'ref': REF['ref'], 'text': ALIAS}], 'reason': 'S6-R6 合成'}}
    out = io.StringIO()
    code = S.main(['r6', '--config', str(config_file(tmp_path, **inputs))], ports=owner, out=out)
    doc = json.loads(out.getvalue())
    assert code == 0 and doc['passed'] is True, json.dumps(doc, ensure_ascii=False)
    st = steps(doc)
    assert st['publish_a_semantic_release']['observed']['version'] == '0.1.0'
    assert (st['rollback_is_a_new_higher_version_and_changes_no_claim']['observed']['version'], st['rollback_is_a_new_higher_version_and_changes_no_claim']['observed']['claims_changed']) == ('0.1.1', 0)
    assert st['the_watch_is_active']['observed']['active'] >= 1
    assert claims() == before



def test_the_example_configuration_loads(tmp_path):
    from s6_common import load_config
    copy = tmp_path / 'example.json'
    copy.write_text((ROOT / 'deploy/examples/s6-stage.example.json').read_text())
    copy.chmod(0o600)
    c = load_config(copy)
    assert set(c['roles']) == {'writer', 'evaluator', 'verify'} and c['owner']['origin'].startswith('https://<')


def test_r6_through_the_workbench_http_entry(stage, tmp_path):
    """The same r6 check through the production workbench entry (owner login, /api/v1/workbench/actions/*, semantic-releases,
    semantic-review): candidates seeded accepted as trusted configuration (clone of the writer's own candidate)."""
    from commercial_fixture import publish_registry_actions
    s = stage
    admin = s['admin']
    publish_registry_actions(admin, ('nexloop.semantic_release.publish', 'nexloop.semantic_release.rollback'))
    e = s['e']
    proposed_body = body_of(e)
    with psycopg.connect(make_conninfo(e['pg'], user='nexloop_evo_writer'), autocommit=True) as db:
        cid = db.execute('select authz.nexloop_evo_candidate_propose(%s,%s)', (e['digest'], json.dumps(proposed_body))).fetchone()[0]['candidate']['candidate_id']
    e['store'].freeze_bundle(cid, seed='0' * 32)
    f = {'admin': admin, 'candidate': cid}
    first = clone(f, conclusion='accept', change_class='minor')
    later = clone(f, conclusion='accept', change_class='patch', change_kind='description', parent='release:0.1.0')
    ports = s['ports']()
    status, out = ports.owner_post('/api/v1/workbench/actions/publish_semantic_release',
                                   {'candidate_id': first, 'items': [{'kind': 'alias', 'ref': REF['ref'], 'text': ALIAS}], 'reason': '先有一个版本'}, 's6-r6-http-first-0001')
    assert status == 200 and out['version'] == '0.1.0', out
    claims = lambda: admin.execute("select md5(coalesce(string_agg(x::text,'|' order by x::text),'')) from ontology.nexloop_claims x").fetchone()  # noqa: E731
    before = claims()
    inputs = {'semantic': {'candidate_id': later, 'items': [{'kind': 'semantic_note', 'ref': REF['ref'], 'text': '只指实物商品'}], 'reason': 'S6-R6 合成'}}
    code, doc = run(tmp_path, 'r6', ports, inputs)
    assert code == 0 and doc['passed'] is True, json.dumps(doc, ensure_ascii=False)
    st = steps(doc)
    assert st['publish_a_semantic_release']['observed'] == {'status': 200, 'version': '0.1.1', 'previous': '0.1.0'}
    back = st['rollback_is_a_new_higher_version_and_changes_no_claim']['observed']
    assert (back['version'], back['rolled_back_to'], back['claims_changed']) == ('0.1.2', '0.1.0', 0)
    assert st['the_watch_is_active']['observed']['active'] >= 1
    assert claims() == before
    # The operator cannot: 403 through the same entry.
    status, _ = s['ports']('operator').owner_post('/api/v1/workbench/actions/rollback_semantic_release', {'target_version': '0.1.1', 'reason': 'x'}, 's6-r6-http-operator-01')
    assert status == 403
