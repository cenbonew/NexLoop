"""NX-039 V6 workbench (design §11 V6; D39-7, D39-8, D39-10, D39-11, D39-14): the review read (latest report per candidate with each
frozen gate's result, L0/L1 scores, the non-inferiority rule, the change class, digests and counts only), the governed stop switch
(the release scope's third Action, owner only, reason required and audited), and the 7-day release watch in the same read.

Real PostgreSQL on the V2/V4 fixtures (an evaluated candidate accepted for release, test_semantic_release_pg) and the owner/operator
workbench fixture. Admin only probes and calls the SQL handlers the governed entry calls.
"""
import json

import psycopg
import pytest

from commercial_fixture import publish_registry_actions
from commitment_fixture import commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from support.tenants import TENANT_A, TENANT_B
from test_browser_http import ORIGIN
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_evolution_e4_pg import REF, evo  # noqa: F401
from test_evolution_e5_pg import proposed  # noqa: F401
from test_evolution_v1_pg import evaluation  # noqa: F401
from test_evolution_v2_pg import opened_eval  # noqa: F401
from test_restore_fence_pg import restoring  # noqa: F401
from test_semantic_release_pg import ALIAS, accepted, call, publish  # noqa: F401
from test_workbench_actions_http import act
from test_workbench_read_pg import workbench_login

PLAN = pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)
GATE_KEYS = {'gate', 'result'}


def summary(admin, tenant=TENANT_A):
    return admin.execute("select runtime.nexloop_evo_review_summary(%s,'real')", (tenant,)).fetchone()[0]


def stop(admin, dimension, stopped, reason='负责人停止'):
    return call(admin, 'nexloop_governed_set_evolution_stop', {'operation': 'set_evolution_stop', 'request_id': 'r', 'dimension': dimension,
                                                              'stopped': stopped, 'reason': reason})


def test_the_review_read_shows_gates_scores_class_and_digests_but_no_person(accepted):
    f = accepted
    admin = f['admin']
    s = summary(admin)
    (c,) = [x for x in s['candidates'] if x['candidate_id'] == f['candidate']]
    assert c['status'] == 'accepted_for_release' and c['report_digest'] == f['report_digest'] and c['conclusion'] == 'accept'
    assert c['change_class'] in ('patch', 'minor') and c['dimension'] == 'semantic_content' and c['hard_gates_version'] == 'nx039-gates-1'
    assert c['gates'] and all(set(g) == GATE_KEYS and g['result'] in ('pass', 'fail') for g in c['gates'])  # no evidence ids
    assert {g['gate'] for g in c['gates']} >= {'references_valid', 'zero_overreach', 'semantic_conflict', 'cost_performance'}
    assert set(c['scores']) == {'design', 'validation', 'ambiguity_rate', 'regressions', 'missing_cases'}
    assert set(c['scores']['design']) == {'cases', 'parent', 'candidate'} and c['non_inferiority']['margin'] == 0.01
    # D39-11: a count and a digest of the case set, never a Run or Consumer id.
    cases = admin.execute('select design_case_ids||validation_case_ids from runtime.nexloop_evo_candidates where candidate_id=%s', (f['candidate'],)).fetchone()[0]
    assert c['cases'] == len(cases) and len(c['cases_digest']) == 64
    text = json.dumps(s)
    assert not any(str(r) in text for r in cases)
    consumers = [r[0] for r in admin.execute('select consumer_id from runtime.nexloop_evo_patch_sources').fetchall()]
    assert consumers and not any(x in text for x in consumers) and ALIAS not in text
    assert c['published_version'] is None
    # Stops: every open dimension, not stopped by default; the release watch is part of the same read.
    assert s['stops'] == [{'dimension': 'semantic_content', 'open': True, 'stopped': False, 'revision': 0, 'changed_at': None}]
    assert s['watch'] == {'active': 0, 'regressing': 0, 'releases': []}
    # After publication the candidate names its version and the watch starts (7 days, never an automatic rollback).
    out = publish(admin, f['candidate'], [{'kind': 'alias', 'ref': REF['ref'], 'text': ALIAS}])
    s = summary(admin)
    (c,) = [x for x in s['candidates'] if x['candidate_id'] == f['candidate']]
    assert c['published_version'] == out['version'] and s['watch']['active'] == 1
    (w,) = s['watch']['releases']
    assert w['regressing'] is False and w['suggestion'] is None and w['judged'] is False
    # Another tenant reads nothing of it.
    assert summary(admin, TENANT_B)['candidates'] == []


def test_the_stop_switch_is_versioned_audited_and_refuses_publication(accepted):
    f = accepted
    admin = f['admin']
    assert stop(admin, 'semantic_content', True) == {'dimension': 'semantic_content', 'stopped': True, 'revision': 1, 'changed': True}
    assert stop(admin, 'semantic_content', True)['changed'] is False  # the same state: no new revision
    assert summary(admin)['stops'][0]['stopped'] is True
    with pytest.raises(psycopg.Error, match='semantic release: dimension_stopped'), admin.transaction():
        publish(admin, f['candidate'], [{'kind': 'alias', 'ref': REF['ref'], 'text': ALIAS}])
    assert stop(admin, 'semantic_content', False, '负责人恢复')['revision'] == 2
    audit = admin.execute("select action,payload from runtime.audit_events where action like 'nexloop.semantic_release.%%' order by created_at").fetchall()
    assert [a[0] for a in audit] == ['nexloop.semantic_release.stopped', 'nexloop.semantic_release.resumed']
    assert audit[0][1]['reason'] == '负责人停止' and audit[1][1]['revision'] == 2
    for body in ({'dimension': 'semantic_content', 'stopped': True, 'reason': '  '}, {'dimension': 'everything', 'stopped': True, 'reason': 'x'},
                 {'dimension': 'semantic_content', 'stopped': 'yes', 'reason': 'x'}):
        with pytest.raises(psycopg.Error, match='evolution stop: invalid'), admin.transaction():
            call(admin, 'nexloop_governed_set_evolution_stop', {'operation': 'set_evolution_stop', 'request_id': 'r', **body})


@PLAN
def test_only_the_owner_reads_the_review_and_stops(restoring, admin):
    c = restoring
    publish_registry_actions(admin, ('nexloop.semantic_release.stop',))
    with c['app']() as client:
        csrf = workbench_login(client, c['operator_member']).json()['csrf_token']
        assert client.get('/api/v1/workbench/semantic-review', headers={'Origin': ORIGIN}).status_code == 403
        assert act(client, csrf, 'set_evolution_stop', {'dimension': 'semantic_content', 'stopped': True, 'reason': '停止'}).status_code == 403
    with c['app']() as client:
        csrf = workbench_login(client, c['owner_member']).json()['csrf_token']
        view = client.get('/api/v1/workbench/semantic-review', headers={'Origin': ORIGIN})
        assert view.status_code == 200, view.text
        assert view.json()['candidates'] == [] and view.json()['stops'][0]['stopped'] is False
        assert client.get('/api/v1/workbench/semantic-review?x=1', headers={'Origin': ORIGIN}).status_code == 422
        done = act(client, csrf, 'set_evolution_stop', {'dimension': 'semantic_content', 'stopped': True, 'reason': '观察期内先停'})
        assert done.status_code == 200 and done.json()['stopped'] is True, done.text
        assert act(client, csrf, 'set_evolution_stop', {'dimension': 'semantic_content', 'stopped': True, 'reason': ''}).status_code == 422  # reason required
        assert act(client, csrf, 'set_evolution_stop', {'dimension': 'semantic_content', 'stopped': True}).status_code == 422
        assert client.get('/api/v1/workbench/semantic-review', headers={'Origin': ORIGIN}).json()['stops'][0]['stopped'] is True
    audited = admin.execute("select count(*) from runtime.nexloop_action_claims where action_name='nexloop.semantic_release.stop'").fetchone()[0]
    assert audited >= 1
