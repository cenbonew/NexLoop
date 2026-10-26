"""NX-054 P4a drill script (scripts/acceptance/relation_gaps_drill.py) on real PostgreSQL with the first batch published: arm makes
exactly one gap in the test world (the metrics login's gap check counts it, world test, type Goal), check reads it, clear removes
only the drill's synthetic objects and the gap with them; a second arm is refused; the real world is untouched."""
import io
import json
import sys
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/acceptance'))
import relation_gaps_drill as D  # noqa: E402

from support.tenants import TENANT_A
from test_relation_types_p1_pg import catalog, publish  # noqa: F401


def run(pg, mode):
    out = io.StringIO()
    code = D.main([mode, '--tenant', TENANT_A, '--dsn-stdin'], connect=lambda: psycopg.connect(pg, autocommit=True), out=out)
    return code, json.loads(out.getvalue())


def gaps(admin, pg):
    admin.execute('alter role nexloop_metrics login')
    with psycopg.connect(make_conninfo(pg, user='nexloop_metrics')) as db:
        doc = db.execute('select authz.nexloop_relation_read_gaps(%s)', (TENANT_A,)).fetchone()[0]
    return {(r['world'], r['type_name']): r['gaps'] for r in doc['rows']}


def test_arm_check_clear(catalog):  # noqa: F811
    admin, pg = catalog['admin'], catalog['pg']
    publish(catalog)
    real_before = admin.execute("select count(*) from ontology.objects where world='real'").fetchone()[0]
    assert run(pg, 'arm') == (0, {'mode': 'arm', 'world': 'test', 'drill_gaps': 1})
    assert gaps(admin, pg).get(('test', 'Goal')) == 1  # the daily check sees it
    assert run(pg, 'check') == (0, {'mode': 'check', 'world': 'test', 'drill_gaps': 1})
    assert run(pg, 'arm') == (2, {'refused': 'drill objects already there: clear first'})  # armed once only
    assert run(pg, 'clear') == (0, {'mode': 'clear', 'world': 'test', 'objects_deleted': 2, 'drill_gaps': 0})
    assert gaps(admin, pg).get(('test', 'Goal'), 0) == 0
    assert admin.execute("select count(*) from ontology.objects where source_system='drill-relgaps'").fetchone()[0] == 0
    assert admin.execute("select count(*) from ontology.objects where world='real'").fetchone()[0] == real_before


def test_a_malformed_tenant_is_refused():
    out = io.StringIO()
    assert D.main(['check', '--tenant', "x' or '1", '--dsn-stdin'], connect=lambda: None, out=out) == 2 and 'tenant' in out.getvalue()


def test_the_check_says_what_it_covers(catalog):  # noqa: F811
    """Stage drill 2026-10-26: before the relation catalog is published the check covers no type (ownership_types 0, rows []) -
    the case the stage drill hit; after publication it covers the ownership source types and sees the drill's gap in the test world."""
    admin, pg = catalog['admin'], catalog['pg']

    def check():
        admin.execute('alter role nexloop_metrics login')
        with psycopg.connect(make_conninfo(pg, user='nexloop_metrics')) as db:
            return db.execute('select authz.nexloop_relation_read_gaps(%s)', (TENANT_A,)).fetchone()[0]
    assert run(pg, 'arm')[1]['drill_gaps'] == 1
    before = check()
    assert before['ownership_types'] == 0 and before['rows'] == [] and before['gaps'] == 0  # unpublished: nothing covered
    run(pg, 'clear')
    publish(catalog)
    assert run(pg, 'arm')[1]['drill_gaps'] == 1
    after = check()
    assert after['ownership_types'] >= 12 and after['gaps'] == 1
    assert {(r['world'], r['type_name']) for r in after['rows'] if r['gaps']} == {('test', 'Goal')}
    run(pg, 'clear')


def test_a_derivation_without_ownership_is_not_coverage(catalog):  # noqa: F811
    """L6 S2: a published relation type that derives (source_property) but marks no Consumer ownership is not counted: the check
    covers only the ownership source types."""
    admin, pg = catalog['admin'], catalog['pg']
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT_A,))
        admin.execute("""insert into ontology.relation_type_versions(tenant_id,relation_name,version,definition) values(%s,'step_in_goal',1,
            '{"relation_name":"step_in_goal","source_type":"PlanStep","target_type":"Goal","cardinality":"many_to_one","version":1,
              "description":"test only","inverse_name":"","properties":[]}')""", (TENANT_A,))
        admin.execute("""insert into control.nexloop_relation_type_profiles(tenant_id,relation_name,version,source_property,consumer_ownership)
            values(%s,'step_in_goal',1,'goal_id',null)""", (TENANT_A,))
        assert admin.execute("select count(*) from control.nexloop_relation_derivations where tenant_id=%s", (TENANT_A,)).fetchone()[0] == 1
    admin.execute('alter role nexloop_metrics login')
    with psycopg.connect(make_conninfo(pg, user='nexloop_metrics')) as db:
        doc = db.execute('select authz.nexloop_relation_read_gaps(%s)', (TENANT_A,)).fetchone()[0]
    assert doc['ownership_types'] == 0 and doc['rows'] == []
