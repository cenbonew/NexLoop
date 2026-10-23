"""NX-054 stage publication runbook tools (docs/implementation/NX-054-stage-publish.md), on the P1/P2 fixture: the read-only
precheck (counts only, blocking findings), the publication through the configurator window with its rollback - a new version
without source_property stops the derivation, a later one resumes it - and the baseline timings (relay p95 from process samples,
an erasure's object count and durations)."""
import contextlib
import io
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from support.tenants import TENANT_A
from test_relation_types_p1_pg import catalog  # noqa: F401
from test_relation_types_p2_pg import edges, hexid, put

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/deploy'))
sys.path.insert(0, str(ROOT / 'scripts/acceptance'))
import relation_derive_timing as T  # noqa: E402
import relation_types_precheck as Q  # noqa: E402
import relation_types_publish as P  # noqa: E402


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
    code = Q.main(['--dsn-file', '/unused', '--tenant', TENANT_A], out=out, connect=lambda: psycopg.connect(c['pg'], autocommit=True))
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


def test_publish_stop_and_resume_the_derivation(catalog):  # noqa: F811
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
    # Published versions are immutable: resuming is a later version.
    code, doc = publish(c, '--resume-derivation', '--from-version', '2')
    assert code == 0 and doc['catalog_version'] == 3 and doc['deriving'] == 26
    put(admin, 'Goal', hexid('runbook-goal-3'), {'consumer_id': consumer})
    assert len(edges(admin, source=hexid('runbook-goal-3'))) == 1
    code, doc = precheck(c)
    assert [v['version'] for v in doc['catalog']['versions']] == [1, 2, 3] and set(doc['catalog']['latest_type_versions'].values()) == {3}


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


def test_the_timings_read_samples_and_an_erasure(catalog):  # noqa: F811
    c = catalog
    admin = c['admin']
    now = datetime.now(UTC)
    for p95 in (40, 60, 90):
        admin.execute("""insert into runtime.nexloop_process_samples(kind,service,instance,window_start,window_seconds,sample,recorded_at)
            values('relay_step','goal_create','relay-1',%s,60,%s,%s)""", (now, Jsonb({'calls': 5, 'timeouts': 0, 'errors': 0, 'p50_ms': 10, 'p95_ms': p95, 'max_ms': 100}), now))
    out = io.StringIO()
    assert T.main(['--dsn-file', '/unused', '--tenant', TENANT_A, 'relay', '--since', (now - timedelta(minutes=5)).isoformat(),
                   '--until', (now + timedelta(minutes=5)).isoformat()], out=out, connect=lambda: psycopg.connect(c['pg'], autocommit=True)) == 0
    assert json.loads(out.getvalue())['result']['goal_create'] == {'samples': 3, 'calls': 15, 'p95_ms_median': 60.0, 'p95_ms_max': 90.0}
    consumer = hexid('timing-consumer')
    put(admin, 'Consumer', consumer, {}, replica=True)
    for i in range(3):
        put(admin, 'Goal', hexid(f'timing-goal-{i}'), {'consumer_id': consumer}, replica=True)
    out = io.StringIO()
    assert T.main(['--dsn-file', '/unused', '--tenant', TENANT_A, 'erasure', '--consumer', consumer], out=out,
                  connect=lambda: psycopg.connect(c['pg'], autocommit=True)) == 0
    doc = json.loads(out.getvalue())
    assert doc['objects_now'] == {'Goal': 3} and doc['erasure'] is None
