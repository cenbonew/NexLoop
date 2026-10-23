"""NX-054 stage publication runbook tools (docs/implementation/NX-054-stage-publish.md), on the P1/P2 fixture: the read-only
precheck and timings as the read-only metrics login (0325 functions; L6 review R-2), the publication through the configurator
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
    out = io.StringIO()
    code = P.main(['--tenant', TENANT_A, '--bootstrap-dsn-file', '/unused', *args], out=out, window=window, connect=connect)
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
    """R-2: the server's read-only identity - the metrics login executes the 0325 functions and nothing else; another login is
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
