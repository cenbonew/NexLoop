"""NX-054 stage publication runbook tools (docs/implementation/NX-054-stage-publish.md), on the P1/P2 fixture: the read-only
precheck and timings as the read-only metrics login (0336 functions; L6 review R-2), the publication through the configurator
window with its rollback - a new version without source_property stops the derivation, a later one resumes it, an older
--from-version is refused (R-1) - and the timing of a real erasure run to completion by the keeper (R-3)."""
import contextlib
import io
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from nexloop_eios import relation_catalog as R
from support.tenants import TENANT_A
from test_relation_types_p1_pg import catalog  # noqa: F401
from test_relation_types_p2_pg import edges, hexid, put
from commercial_fixture import TENANT as TENANT_ERASURE
from commitment_fixture import commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_erasure_pg import erasing  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
sys.path.insert(0, str(ROOT / 'scripts/acceptance'))
import relation_derive_timing as T  # noqa: E402
import relation_read_gaps as G  # noqa: E402
import relation_reads as RR  # noqa: E402
import relation_types_precheck as Q  # noqa: E402
import relation_types_publish as P  # noqa: E402


def metrics(admin, pg):
    admin.execute('alter role nexloop_metrics login')
    return lambda: RR.checked(psycopg.connect(make_conninfo(pg, user='nexloop_metrics'), autocommit=True))


def publish(c, *args):
    @contextlib.contextmanager
    def window(path, *, refused):
        yield 'configurator'

    def connect(_):
        return psycopg.connect(make_conninfo(c['pg'], user='nexloop_configurator'))
    def registered(path, tenant, types):  # the bootstrap read, here as the test's admin
        return {r[0] for r in c['admin'].execute('select distinct type_name from ontology.object_type_versions where tenant_id=%s and type_name=any(%s)',
                                                 (tenant, sorted(types)))}
    out = io.StringIO()
    code = P.main(['--tenant', TENANT_A, '--bootstrap-dsn-file', '/unused', *args], out=out, window=window, connect=connect, registered=registered)
    return code, json.loads(out.getvalue())


def precheck(c):
    out = io.StringIO()
    code = Q.main(['--dsn-file', '/unused', '--tenant', TENANT_A], out=out, connect=metrics(c['admin'], c['pg']))
    return code, json.loads(out.getvalue())


def test_dry_runs_need_no_connection():
    out = io.StringIO()
    assert P.main(['--dry-run'], out=out) == 0
    doc = json.loads(out.getvalue())
    assert doc['catalog_version'] == 1 and doc['relation_types'] == 26 and doc['deriving'] == 26 and doc['longest_ownership_chain'] == 3
    out = io.StringIO()
    assert P.main(['--dry-run', '--stop-derivation', '--from-version', '1'], out=out) == 0
    assert json.loads(out.getvalue())['catalog_version'] == 2 and json.loads(out.getvalue())['deriving'] == 0
    assert P.main(['--dry-run', '--stop-derivation'], out=io.StringIO()) == 2  # --from-version required


def test_publish_stop_and_resume_the_derivation_from_the_head_only(catalog):  # noqa: F811
    c = catalog
    admin = c['admin']
    code, doc = publish(c)
    assert code == 0 and doc['result']['new_relation_types'] == 26
    consumer, goal = hexid('runbook-consumer'), hexid('runbook-goal')
    put(admin, 'Consumer', consumer, {}, replica=True)
    put(admin, 'Goal', goal, {'consumer_id': consumer})
    assert len(edges(admin, source=goal)) == 1
    # Rollback: v2 without source_property - no new derivation, derived relations go on the next change.
    code, doc = publish(c, '--stop-derivation', '--from-version', '1')
    assert code == 0 and doc['catalog_version'] == 2 and doc['deriving'] == 0
    put(admin, 'Goal', hexid('runbook-goal-2'), {'consumer_id': consumer})
    assert edges(admin, source=hexid('runbook-goal-2')) == []
    admin.execute("update ontology.objects set properties=properties||'{\"state\":\"x\"}' where object_id=%s", (goal,))
    assert edges(admin, source=goal) == []
    # Rerunning the same stop is the same command (accepted, a replay); resuming is a later version.
    assert publish(c, '--stop-derivation', '--from-version', '1')[0] == 0
    code, doc = publish(c, '--resume-derivation', '--from-version', '2')
    assert code == 0 and doc['catalog_version'] == 3 and doc['deriving'] == 26
    put(admin, 'Goal', hexid('runbook-goal-3'), {'consumer_id': consumer})
    assert len(edges(admin, source=hexid('runbook-goal-3'))) == 1
    # R-1: at v3, stopping "from 1" would rebuild v2 and come back as a replay - a rollback that does nothing. Refused.
    code, doc = publish(c, '--stop-derivation', '--from-version', '1')
    assert code == 2 and 'latest published catalog version (3)' in doc['refused']
    assert publish(c)[0] == 2  # the first batch again at v3: refused too
    assert len(edges(admin, source=hexid('runbook-goal-3'))) == 1  # still deriving
    code, doc = precheck(c)
    assert [v['version'] for v in doc['catalog']['versions']] == [1, 2, 3] and set(doc['catalog']['latest_type_versions'].values()) == {3}
    assert doc['catalog']['deriving'] == 26


def test_the_precheck_reports_counts_and_what_blocks(catalog):  # noqa: F811
    c = catalog
    admin = c['admin']
    consumer = hexid('pre-consumer')
    put(admin, 'Consumer', consumer, {}, replica=True)
    put(admin, 'Goal', hexid('pre-1'), {'consumer_id': consumer}, replica=True)
    put(admin, 'Goal', hexid('pre-2'), {'consumer_id': consumer}, replica=True)
    put(admin, 'Goal', hexid('pre-3'), {'consumer_id': 'pseudonym:' + 'a' * 32}, replica=True)
    put(admin, 'Goal', hexid('pre-4'), {'consumer_id': hexid('nobody')}, replica=True)
    code, doc = precheck(c)
    assert code == 0, doc['blocking']
    goal = next(f for f in doc['fields'] if f['relation'] == 'goal_of')
    assert {k: goal[k] for k in ('objects', 'with_value', 'hex64', 'pseudonyms', 'target_same_world', 'dangling', 'sharing_a_target')} == {
        'objects': 4, 'with_value': 4, 'hex64': 3, 'pseudonyms': 1, 'target_same_world': 2, 'dangling': 1, 'sharing_a_target': 1}
    assert consumer not in json.dumps(doc)  # counts only
    put(admin, 'Goal', hexid('pre-5'), {'consumer_id': 'not-an-id'}, replica=True)
    put(admin, 'RelationshipAssessment', hexid('ra'), {'relation_name': 'goal_of', 'relation_version': 0}, replica=True)
    code, doc = precheck(c)
    assert code == 1 and doc['assessments']['first_batch_name_collisions'] == ['goal_of'] and doc['assessments']['version_zero'] == 1
    assert any('goal_of: 1 value(s) neither 64 hex' in b for b in doc['blocking'])


def test_the_reads_run_as_the_read_only_metrics_login_only(catalog):  # noqa: F811
    """R-2: the server's read-only identity - the metrics login executes the 0336 functions and nothing else; another login is
    refused by the tools; every query runs under a statement timeout."""
    c = catalog
    admin = c['admin']
    db = metrics(admin, c['pg'])()
    assert db.execute('show statement_timeout').fetchone() == ('1min',)
    for table in ('ontology.objects', 'ontology.relations', 'control.nexloop_relation_catalogs'):
        assert admin.execute("select has_table_privilege('nexloop_metrics',%s,'select') or has_table_privilege('nexloop_metrics',%s,'insert')",
                             (table, table)).fetchone() == (False,)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute('select count(*) from ontology.objects')
    db.close()
    with pytest.raises(RR.Refused, match='nexloop_metrics'):
        RR.checked(psycopg.connect(c['pg'], autocommit=True))
    out = io.StringIO()
    assert Q.main(['--dsn-file', '/unused', '--tenant', TENANT_A], out=out, connect=lambda: RR.checked(psycopg.connect(c['pg'], autocommit=True))) == 2


def test_relay_p95_from_process_samples(catalog):  # noqa: F811
    c = catalog
    admin = c['admin']
    now = datetime.now(UTC)
    for p95 in (40, 60, 90):
        admin.execute("""insert into runtime.nexloop_process_samples(kind,service,instance,window_start,window_seconds,sample,recorded_at)
            values('relay_step','goal_create','relay-1',%s,60,%s,%s)""", (now, Jsonb({'calls': 5, 'timeouts': 0, 'errors': 0, 'p50_ms': 10, 'p95_ms': p95, 'max_ms': 100}), now))
    out = io.StringIO()
    assert T.main(['--dsn-file', '/unused', 'relay', '--since', (now - timedelta(minutes=5)).isoformat(), '--until', (now + timedelta(minutes=5)).isoformat()],
                  out=out, connect=metrics(admin, c['pg'])) == 0
    assert json.loads(out.getvalue())['result']['goal_create'] == {'samples': 3, 'calls': 15, 'p95_ms_median': 60.0, 'p95_ms_max': 90.0}


@pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)
def test_the_timing_of_a_real_erasure_run_by_the_keeper(erasing, admin, pg):  # noqa: F811
    """R-3: a real NX-029 erasure (owner action, keeper to completion): objects counted before, then state, request-to-completion,
    the synchronous purge span (every item class but run_file) and the span per item class, by the exact request_ref."""
    from test_erasure_pg import act, keeper, request
    c = erasing
    consumer = c['consumer']
    c.inbound('请删除我的资料')
    c.outbound('好的，已为您登记')
    run = metrics(admin, pg)

    def timing():
        out = io.StringIO()
        assert T.main(['--dsn-file', '/unused', '--tenant', TENANT_ERASURE, 'erasure', '--consumer', consumer],
                      out=out, connect=run) == 0
        return json.loads(out.getvalue())
    before = timing()
    assert before['erasure'] is None and sum(before['objects_now'].values()) >= 2  # its Conversation and Messages at least
    act(c['owner_actions'], 'erase_consumer', 'erase-timing', consumer_id=consumer, reason='计时')
    for _ in range(10):
        keeper(c, host=lambda run_id: 'purged').run_once()
        if request(admin, consumer)[1] == 'completed':
            break
    after = timing()['erasure']
    assert after['state'] == 'completed' and after['request_to_completion_seconds'] is not None
    assert after['sync_tombstones'] >= 1 and after['sync_purge_seconds'] is not None and after['sync_purge_seconds'] >= 0
    assert after['by_item_class'] and all(v['tombstones'] >= 1 for v in after['by_item_class'].values())
    assert consumer not in json.dumps(after)


def test_defer_dry_runs_and_refusals():
    out = io.StringIO()
    assert P.main(['--dry-run', '--defer', 'assessment_evidence'], out=out) == 0
    doc = json.loads(out.getvalue())
    assert doc['deferred'] == ['assessment_evidence'] and doc['relation_types'] == 25 and doc['longest_ownership_chain'] == 2
    out = io.StringIO()
    assert P.main(['--dry-run'], out=out) == 0 and json.loads(out.getvalue())['deferred'] == []
    for name in ('message_in', 'no_such_type'):  # message_in would leave assessment_evidence without a chain
        out = io.StringIO()
        assert P.main(['--dry-run', '--defer', name], out=out) == 2 and 'refused' in json.loads(out.getvalue())
    out = io.StringIO()
    assert P.main(['--dry-run', '--stop-derivation', '--from-version', '1', '--defer', 'assessment_evidence'], out=out) == 0
    assert json.loads(out.getvalue())['relation_types'] == 25


def test_an_unregistered_endpoint_type_is_deferred_not_published(catalog, tmp_path):  # noqa: F811
    """Stage 2026-10-27: RelationshipAssessment never registered for the tenant - the whole batch is refused (0319), the batch
    without assessment_evidence publishes, and its derivation follows."""
    c = catalog
    admin = c['admin']
    admin.execute("delete from ontology.object_type_versions where tenant_id=%s and type_name='RelationshipAssessment'", (TENANT_A,))
    code, doc = publish(c)
    assert code == 2 and doc['message'] == 'relation type invalid: assessment_evidence'
    assert admin.execute('select count(*) from ontology.relation_type_versions where tenant_id=%s', (TENANT_A,)).fetchone()[0] == 0
    code, doc = publish(c, '--defer', 'assessment_evidence')
    assert code == 0 and doc['deferred'] == ['assessment_evidence'] and doc['result']['new_relation_types'] == 25
    names = {r[0] for r in admin.execute('select relation_name from ontology.relation_type_versions where tenant_id=%s', (TENANT_A,))}
    assert names == {s.name for s in R.FIRST_BATCH} - {'assessment_evidence'}
    owners = admin.execute("""select count(distinct m.source_type) from control.nexloop_relation_derivations m join control.nexloop_relation_type_profiles f
        on f.tenant_id=m.tenant_id and f.relation_name=m.relation_name and f.version=m.version
        where m.tenant_id=%s and f.consumer_ownership is not null""", (TENANT_A,)).fetchone()[0]
    assert owners == 13  # the gap check's ownership_types (0360): 14 source types less RelationshipAssessment
    consumer, goal = hexid('defer-consumer'), hexid('defer-goal')
    put(admin, 'Consumer', consumer, {}, replica=True)
    put(admin, 'Goal', goal, {'consumer_id': consumer})
    assert len(edges(admin, source=goal)) == 1
    code, doc = publish(c, '--defer', 'assessment_evidence')  # the same command rerun replays
    assert code == 0 and doc['result']['replayed'] is True
    # L6 I1: a type whose endpoints are both registered is never deferred.
    code, doc = publish(c, '--defer', 'goal_of')
    assert code == 2 and 'goal_of' in doc['refused'] and 'registered' in doc['refused']
    # Point 4: the precheck counts the deferred type's source objects (0 here: nothing blocks).
    out = io.StringIO()
    assert Q.main(['--dsn-file', '/unused', '--tenant', TENANT_A, '--defer', 'assessment_evidence'], out=out, connect=metrics(admin, c['pg'])) == 0
    assert json.loads(out.getvalue())['deferred'] == {'assessment_evidence': {'source_type': 'RelationshipAssessment', 'source_objects': 0}}
    # L6 F1: the gap check after the deferred publication expects 13 and covers 13 - exit 0, not catalog_unpublished.
    gout, gerr = io.StringIO(), io.StringIO()
    code = G.main(['--dsn-file', '/unused', '--tenant', TENANT_A, '--evidence-dir', str(tmp_path)], connect=lambda _: metrics(admin, c['pg'])(),
                  out=gout, err=gerr)
    got = json.loads(gout.getvalue())
    assert code == 0, gerr.getvalue()
    assert got['ownership_types'] == got['expected_ownership_types'] == 13 and got['unpublished_ownership'] == ['assessment_evidence']
    # Once the type is registered, --resume-derivation --from-version 1 (no --defer) publishes the whole batch as version 2.
    admin.execute("""insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,'RelationshipAssessment',1,%s)""",
                  (TENANT_A, Jsonb({'type_name': 'RelationshipAssessment', 'version': 1, 'properties': []})))
    code, doc = publish(c, '--resume-derivation', '--from-version', '1')
    assert code == 0 and doc['catalog_version'] == 2 and doc['relation_types'] == 26 and doc['deriving'] == 26 and doc['deferred'] == []
    gout = io.StringIO()
    assert G.main(['--dsn-file', '/unused', '--tenant', TENANT_A, '--evidence-dir', str(tmp_path)], connect=lambda _: metrics(admin, c['pg'])(),
                  out=gout, err=io.StringIO()) == 0
    assert json.loads(gout.getvalue())['ownership_types'] == 14


def test_the_precheck_blocks_a_deferred_type_with_objects(catalog):  # noqa: F811
    c = catalog
    admin = c['admin']
    put(admin, 'RelationshipAssessment', hexid('deferred-assessment'), {'evidence_message_id': hexid('some-message')}, replica=True)
    out = io.StringIO()
    assert Q.main(['--dsn-file', '/unused', '--tenant', TENANT_A, '--defer', 'assessment_evidence'], out=out, connect=metrics(admin, c['pg'])) == 1
    doc = json.loads(out.getvalue())
    assert doc['deferred']['assessment_evidence']['source_objects'] == 1 and any('assessment_evidence: deferred' in b for b in doc['blocking'])
    assert Q.main(['--dsn-file', '/unused', '--tenant', TENANT_A, '--defer', 'nope'], out=io.StringIO(), connect=metrics(admin, c['pg'])) == 2
