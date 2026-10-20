"""NX-039 V4 semantic release and rollback (design §7, §8; D39-7/8/9/10/12/13/14), actual PostgreSQL.

Publish and rollback run through the governed handlers exactly as the governed entry calls them (owner principal, intent), on the V2
fixture's evaluated candidate; the owner-only gate runs through the workbench Action path; the definition-read merge runs through the
real SemanticAdapter on the recall fixture. Admin seeds (a second candidate and its report, a Claim match) and probes only.
"""
import hashlib
import secrets
import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from commercial_fixture import publish_registry_actions
from commitment_fixture import commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.context_engine.semantic import SemanticAdapter
from nexloop_eios.postgres_artifacts import AuthoritySigner
from support.tenants import TENANT_A
from test_browser_http import ORIGIN
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_evolution_e4_pg import OTHER_REF, REF, evo, table_digest  # noqa: F401
from test_evolution_e5_pg import proposed  # noqa: F401
from test_evolution_v1_pg import evaluation  # noqa: F401
from test_evolution_v2_pg import opened_eval, report_for  # noqa: F401
from test_recall_pg import A, recall_db, recall_for  # noqa: F401
from test_restore_fence_pg import restoring  # noqa: F401
from test_workbench_actions_http import act
from test_workbench_read_pg import workbench_login

PLAN = pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)
ALIAS = 'synthetic-term'  # E4's TERM is the sha256 of this text: the missed term the alias candidate targets


def call(admin, fn, body, tenant=TENANT_A):
    return admin.execute(f"select control.{fn}(%s,'real','owner-principal','owner-subject',%s,%s)",
                         (tenant, 'intent-' + uuid.uuid4().hex[:12], Jsonb(body))).fetchone()[0]


def publish(admin, candidate, items, reason='发布'):
    return call(admin, 'nexloop_governed_publish_semantic_release',
                {'operation': 'publish_semantic_release', 'request_id': 'r', 'candidate_id': candidate, 'items': items, 'reason': reason})


def rollback(admin, target, reason='回退'):
    return call(admin, 'nexloop_governed_rollback_semantic_release',
                {'operation': 'rollback_semantic_release', 'request_id': 'r', 'target_version': target, 'reason': reason})


def refused(admin, code, fn, *args):
    with pytest.raises(psycopg.Error, match='semantic release: ' + code), admin.transaction():
        fn(admin, *args)


def seed(admin, *statements):
    with admin.transaction():
        admin.execute('set local session_replication_role = replica')
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT_A,))
        for sql, params in statements:
            admin.execute(sql, params)


@pytest.fixture
def accepted(opened_eval):  # noqa: F811
    f = opened_eval
    out = f['evaluator'].conclude(report_for(f))
    assert out['status'] == 'accepted_for_release'
    f['report_digest'] = out['report_digest']
    return f


def clone(f, *, status='accepted_for_release', conclusion=None, change_class=None, parent=None, change_kind=None):
    """A second candidate of the same diagnosis (seed data): optionally its own patch (another change kind) and report."""
    new, patch = str(uuid.uuid4()), str(uuid.uuid4())
    statements = []
    if change_kind:
        statements.append(("""insert into runtime.nexloop_evo_patches select tenant_id,world,%s::uuid,%s::uuid,jsonb_set(body,'{change_kind}',to_jsonb(%s::text)),
            body_digest,created_at from runtime.nexloop_evo_patches where candidate_id=%s""", (patch, new, change_kind, f['candidate'])))
    statements.append(("""insert into runtime.nexloop_evo_candidates select tenant_id,world,%s::uuid,diagnosis_id,category,dimension,coalesce(%s,parent_revision),
        candidate_revision,primary_hypothesis,coalesce(%s::uuid,patch_id),input_snapshot_digest,design_case_ids,validation_case_ids,case_groups,regroups,
        evaluator_version,model_profile,maximum_rounds,allowed_change_scope,%s,null,cost_limit,unit,created_at,updated_at
        from runtime.nexloop_evo_candidates where candidate_id=%s""", (new, parent, patch if change_kind else None, status, f['candidate'])))
    if conclusion:
        bundle = secrets.token_hex(32)
        statements.append(("insert into runtime.nexloop_evo_bundles select tenant_id,world,%s::uuid,bundle,%s,frozen_at from runtime.nexloop_evo_bundles where candidate_id=%s",
                           (new, bundle, f['candidate'])))
        statements.append(("""insert into runtime.nexloop_evo_reports(tenant_id,world,report_id,candidate_id,bundle_digest,level,conclusion,report,report_digest)
            values(%s,'real',gen_random_uuid(),%s,%s,'L0',%s,%s,%s)""", (TENANT_A, new, bundle, conclusion, Jsonb({'change_class': change_class}), secrets.token_hex(32))))
    seed(f['admin'], *statements)
    return new


def test_only_an_accepted_candidate_of_an_allowed_class_is_published_and_the_class_comes_from_the_report(accepted):
    f = accepted
    admin = f['admin']
    before = table_digest(admin)
    items = [{'kind': 'alias', 'ref': REF['ref'], 'text': ALIAS}]
    refused(admin, 'not_accepted', publish, clone(f, status='proposed'), items)  # not evaluated
    refused(admin, 'not_accepted', publish, clone(f, conclusion='reject', change_class='minor'), items)  # rejected
    refused(admin, 'breaking_change', publish, clone(f, conclusion='accept', change_class='major'), items)  # closed
    refused(admin, 'breaking_change', publish, clone(f, conclusion='accept', change_class='refused'), items)
    refused(admin, 'invalid', publish, f['candidate'], [{**items[0], 'change_class': 'patch'}])  # D39-7: no class in the request
    refused(admin, 'item_not_in_patch', publish, f['candidate'], [{'kind': 'semantic_note', 'ref': REF['ref'], 'text': '说明'}])
    refused(admin, 'item_not_in_patch', publish, f['candidate'], [{'kind': 'alias', 'ref': 'eios:object_type:Other', 'text': ALIAS}])
    refused(admin, 'alias_not_in_patch', publish, f['candidate'], [{'kind': 'alias', 'ref': REF['ref'], 'text': '别的说法'}])
    refused(admin, 'parent_moved', publish, clone(f, conclusion='accept', change_class='minor', parent='release:9.9.9'), items)
    out = publish(admin, f['candidate'], items)
    assert (out['version'], out['change_class'], out['items'], out['reason']) == ('0.1.0', 'minor', 1, 'publish')  # the report's class
    row = admin.execute("""select version,reason,candidate_id::text,report_digest,change_class,published_by,request_intent,owner_reason
        from ontology.nexloop_semantic_releases""").fetchone()
    assert row[:6] == ('0.1.0', 'publish', f['candidate'], f['report_digest'], 'minor', 'owner-principal') and row[6].startswith('intent-') and row[7] == '发布'
    assert admin.execute('select version from ontology.nexloop_semantic_release_pointers').fetchone() == ('0.1.0',)
    event = admin.execute("select event_kind,detail->>'version',recorded_by from runtime.nexloop_evo_candidate_events order by event_id desc limit 1").fetchone()
    assert event == ('published', '0.1.0', 'human:owner-principal')
    refused(admin, 'already_published', publish, f['candidate'], items)
    # The same text on another definition is a meaning change (G8 at publication).
    refused(admin, 'alias_conflict', publish, clone(f, conclusion='accept', change_class='patch', parent='release:0.1.0'),
            [{'kind': 'alias', 'ref': OTHER_REF['ref'], 'text': ALIAS}])
    # AT-023: no definition, object, Claim, budget or authority changed; the NX-045 alias table is untouched (D39-13).
    assert table_digest(admin) == before
    assert admin.execute('select count(*) from ontology.nexloop_definition_aliases').fetchone() == (0,)
    for statement in ("update ontology.nexloop_semantic_releases set owner_reason='x'", 'delete from ontology.nexloop_semantic_release_items'):
        with pytest.raises(psycopg.Error, match='immutable'), admin.transaction():
            admin.execute('set local role nexloop_owner')
            admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT_A,))
            admin.execute(statement)


def test_semantic_note_comes_only_from_an_item_and_is_at_most_200_characters(accepted):
    f = accepted
    admin = f['admin']
    note = clone(f, conclusion='accept', change_class='patch', change_kind='description')
    refused(admin, 'invalid_text', publish, note, [{'kind': 'semantic_note', 'ref': REF['ref'], 'text': '说' * 201}])
    refused(admin, 'invalid_text', publish, note, [{'kind': 'semantic_note', 'ref': REF['ref'], 'text': '一行\n两行'}])
    refused(admin, 'item_not_in_patch', publish, note, [{'kind': 'alias', 'ref': REF['ref'], 'text': ALIAS}])
    assert publish(admin, note, [{'kind': 'semantic_note', 'ref': REF['ref'], 'text': '  ' + '说' * 200 + '  '}])['version'] == '0.0.1'
    view = admin.execute("select runtime.nexloop_semantic_release_view(%s,'real',%s)", (TENANT_A, REF['ref'])).fetchone()[0]
    assert view == {'aliases': [], 'semantic_note': '说' * 200, 'version': '0.0.1'}
    with pytest.raises(psycopg.Error), admin.transaction():  # the item table itself refuses a longer note
        admin.execute('set local session_replication_role = replica')
        admin.execute("update ontology.nexloop_semantic_release_items set body=repeat('长',201)")


def test_rollback_is_a_new_higher_version_and_reports_claims_without_changing_them(accepted):
    f = accepted
    admin = f['admin']
    note = clone(f, conclusion='accept', change_class='patch', change_kind='description')
    assert publish(admin, note, [{'kind': 'semantic_note', 'ref': REF['ref'], 'text': '只指实物商品'}])['version'] == '0.0.1'
    seed(admin, ("update runtime.nexloop_evo_candidates set parent_revision='release:0.0.1' where candidate_id=%s", (f['candidate'],)))
    assert publish(admin, f['candidate'], [{'kind': 'alias', 'ref': REF['ref'], 'text': ALIAS}])['version'] == '0.1.0'
    # A Claim matched through REF while 0.1.0 is live (seeded with foreign keys off: the Claim itself is not what this tests).
    claim = hashlib.sha256(b'claim-under-release').hexdigest()
    seed(admin, ("""insert into ontology.nexloop_claim_matches(tenant_id,world,claim_id,matcher_version,outcome,decision,recall,proposal_id)
        values(%s,'real',%s,'m1','full_match','{}',%s,%s)""", (TENANT_A, claim, Jsonb([{'ref': REF['ref'], 'score': 1.0}]), str(uuid.uuid4()))))
    claims = lambda: admin.execute("select md5(string_agg(x::text,'|' order by x::text)) from ontology.nexloop_claim_matches x").fetchone()  # noqa: E731
    before = claims()
    refused(admin, 'already_current', rollback, '0.1.0')
    refused(admin, 'unknown_version', rollback, '7.0.0')
    out = rollback(admin, '0.0.1')
    assert (out['version'], out['reason'], out['rolled_back_to']) == ('0.1.1', 'rollback', '0.0.1')  # the number never moves back
    report = out['report']
    assert (report['removed_aliases'], report['claim_count'], report['claim_ids'], report['claims_changed']) == (1, 1, [claim], 0)
    assert claims() == before  # D39-9: Claims are reported, never changed
    view = admin.execute("select runtime.nexloop_semantic_release_view(%s,'real',%s)", (TENANT_A, REF['ref'])).fetchone()[0]
    assert view == {'aliases': [], 'semantic_note': '只指实物商品', 'version': '0.1.1'}
    stored = admin.execute("select reason,rolled_back_to,rollback_report->>'claim_count',published_by from ontology.nexloop_semantic_releases where version='0.1.1'").fetchone()
    assert stored == ('rollback', '0.0.1', '1', 'owner-principal')
    # A later rollback to 0.1.0 brings the alias back as a new version 0.1.2.
    assert rollback(admin, '0.1.0')['version'] == '0.1.2'
    assert admin.execute("select runtime.nexloop_semantic_release_view(%s,'real',%s)->'aliases'", (TENANT_A, REF['ref'])).fetchone()[0] == [ALIAS]


def test_retention_keeps_the_current_and_the_previous_version(accepted):
    f = accepted
    admin = f['admin']
    note = clone(f, conclusion='accept', change_class='patch', change_kind='description')
    publish(admin, note, [{'kind': 'semantic_note', 'ref': REF['ref'], 'text': '一'}])
    seed(admin, ("update runtime.nexloop_evo_candidates set parent_revision='release:0.0.1' where candidate_id=%s", (f['candidate'],)))
    publish(admin, f['candidate'], [{'kind': 'alias', 'ref': REF['ref'], 'text': ALIAS}])
    rollback(admin, '0.0.1')  # 0.1.1; versions 0.0.1, 0.1.0, 0.1.1
    seed(admin, ("update ontology.nexloop_semantic_releases set recorded_at=now()-interval '400 days'", ()),
         ("update runtime.nexloop_evo_reports set recorded_at=now()-interval '400 days'", ()))
    out = admin.execute("select runtime.nexloop_retention_sweep(%s,'real','semantic_release',200,'sweep:release','service:synthetic')", (TENANT_A,)).fetchone()[0]
    assert out['counts']['semantic_releases'] == 1  # 0.0.1 leaves; 0.1.1 (current) and 0.1.0 (the one before) stay
    assert [r[0] for r in admin.execute("select version from ontology.nexloop_semantic_releases order by major,minor,patch")] == ['0.1.0', '0.1.1']
    # Reports a kept release names stay; the clone's report (named by the deleted 0.0.1) is expired and leaves.
    assert admin.execute("select count(*) from runtime.nexloop_evo_reports where report_digest=%s", (f['report_digest'],)).fetchone() == (1,)
    assert out['counts']['evaluation_reports'] >= 1


@PLAN
def test_only_the_owner_publishes_or_rolls_back(restoring, admin):
    c = restoring
    publish_registry_actions(admin, ('nexloop.semantic_release.publish', 'nexloop.semantic_release.rollback'))
    body = {'candidate_id': str(uuid.uuid4()), 'items': [{'kind': 'alias', 'ref': REF['ref'], 'text': ALIAS}], 'reason': '发布'}
    with c['app']() as client:
        csrf = workbench_login(client, c['operator_member']).json()['csrf_token']
        assert act(client, csrf, 'publish_semantic_release', body).status_code == 403
        assert act(client, csrf, 'rollback_semantic_release', {'target_version': '0.0.1', 'reason': 'x'}).status_code == 403
        assert client.get('/api/v1/workbench/semantic-releases', headers={'Origin': ORIGIN}).status_code == 403
    with c['app']() as client:
        csrf = workbench_login(client, c['owner_member']).json()['csrf_token']
        view = client.get('/api/v1/workbench/semantic-releases', headers={'Origin': ORIGIN})
        assert view.status_code == 200 and view.json() == {'current': None, 'releases': [], 'publishable': []}, view.text
        refused_publish = act(client, csrf, 'publish_semantic_release', body)
        assert refused_publish.status_code == 409 and refused_publish.json()['details'] == {'reason': 'unknown_candidate'}, refused_publish.text
        refused_back = act(client, csrf, 'rollback_semantic_release', {'target_version': '0.0.1', 'reason': '回退'})
        assert refused_back.status_code == 409 and refused_back.json()['details'] == {'reason': 'no_release'}
        assert act(client, csrf, 'publish_semantic_release', {**body, 'change_class': 'patch'}).status_code == 422  # D39-7
    # Every attempt went through the governed entry and is in the human-action audit.
    audited = admin.execute("select count(*) from runtime.nexloop_action_claims where action_name like 'nexloop.semantic_release.%%'").fetchone()[0]
    assert audited >= 2


def test_definition_reads_merge_the_current_release(recall_db):  # noqa: F811
    f = recall_db
    admin = f['admin']
    signer = AuthoritySigner('semantic-release-key', secrets.token_bytes(32))
    admin.execute('insert into authz.nexloop_authority_signing_keys values(%s,%s,true)', (signer.key_id, signer.material))
    session = authenticate_service(f['worker'], f['reader_token'], world='real')
    adapter = SemanticAdapter(f['worker'], session, signer, recall_for(f, session))
    plain = adapter.browse('关注防水功能')
    consumer = next(i for i in plain['items'] if i['ref'] == 'eios:object_type:Consumer')
    assert 'semantic_note' not in consumer
    items = [{'kind': 'alias', 'ref': 'eios:object_type:Consumer', 'body': '会员', 'source_candidate_id': str(uuid.uuid4()), 'source_consumer_ids': []},
             {'kind': 'semantic_note', 'ref': 'eios:object_type:Consumer', 'body': '指已登记的客户', 'source_candidate_id': str(uuid.uuid4()), 'source_consumer_ids': []}]
    admin.execute("""select runtime.nexloop_semantic_release_write(%s,'real',array[0,1,0],'publish',%s,%s,'minor',null,null,'synthetic','owner','intent',%s)""",
                  (A, str(uuid.uuid4()), 'a' * 64, Jsonb(items)))
    merged = next(i for i in adapter.browse('关注防水功能')['items'] if i['ref'] == 'eios:object_type:Consumer')
    assert '会员' in merged['aliases'] and set(consumer['aliases']) <= set(merged['aliases']) and merged['semantic_note'] == '指已登记的客户'
    # The recall index carries the release alias (lexical), so the alias text itself finds the definition.
    assert admin.execute("select count(*) from ontology.nexloop_recall_entries where tenant_id=%s and source_key like 'release-alias:%%'", (A,)).fetchone() == (1,)
