"""NX-054 P2.1 (the derive_one migration; docs/design/relation-backfill-p3.md §2): the P2 derivation's per-object, per-relation body
as ontology.nexloop_relation_derive_one, shared by the commit-time triggers and the P3 backfill. Its results (created, existing,
dangling, none, unknown), the marker it writes and refuses, the triggers on the new trigger function under their 0321 names, the
0321 function gone, and no role holding either function. The P2 behaviour itself is test_relation_types_p2_pg (the regression)."""
import psycopg
import pytest
from psycopg.types.json import Jsonb

from support.tenants import TENANT_A
from test_relation_types_p1_pg import catalog, publish  # noqa: F401 - the P1 fixture (object types, configurator)
from test_relation_types_p2_pg import edges, hexid, put

BACKFILL = {'derived': True, 'backfill': 'run-1'}


def derive(admin, object_id, relation, marker, drop=False):
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT_A,))
        return admin.execute("""select ontology.nexloop_relation_derive_one(o,%s,%s,%s) from ontology.objects o
            where o.tenant_id=%s and o.object_id=%s""", (relation, Jsonb(marker), drop, TENANT_A, object_id)).fetchone()[0]


def test_derive_one_results_and_marker(catalog):  # noqa: F811 - imported fixtures
    c = catalog
    admin = c['admin']
    publish(c)
    a, g, lost, bare = hexid('d1-consumer'), hexid('d1-goal'), hexid('d1-goal-dangling'), hexid('d1-goal-bare')
    put(admin, 'Consumer', a, {}, replica=True)
    put(admin, 'Goal', g, {'consumer_id': a}, replica=True)  # stock: the trigger did not derive it
    put(admin, 'Goal', lost, {'consumer_id': hexid('nobody')}, replica=True)
    put(admin, 'Goal', bare, {'consumer_id': 'pseudonym:' + 'b' * 32}, replica=True)
    assert edges(admin, source=g) == []
    assert derive(admin, g, 'goal_of', BACKFILL) == 'created'
    assert admin.execute("select metadata from ontology.relations where source_object_id=%s", (g,)).fetchone()[0] == BACKFILL
    assert derive(admin, g, 'goal_of', BACKFILL) == 'existing'  # idempotent: the 0016 relation id, nothing rewritten
    assert derive(admin, lost, 'goal_of', BACKFILL) == 'dangling'
    assert derive(admin, bare, 'goal_of', BACKFILL) == 'none'
    assert derive(admin, g, 'conversation_of', BACKFILL) == 'unknown'  # not a relation of Goal
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        derive(admin, g, 'goal_of', {'backfill': 'run-1'})  # without derived:true derivation could never drop it
    # A backfilled relation is maintained by the trigger like a derived one: the property changes, it goes.
    b = hexid('d1-consumer-b')
    put(admin, 'Consumer', b, {}, replica=True)
    admin.execute("update ontology.objects set properties=jsonb_build_object('consumer_id',%s::text) where object_id=%s", (b, g))
    assert edges(admin, source=g) == [(g, b, 'real')]
    assert admin.execute("select metadata from ontology.relations where source_object_id=%s", (g,)).fetchone()[0] == {'derived': True}


def test_the_triggers_run_the_shared_body_and_nothing_is_granted(catalog):  # noqa: F811 - imported fixtures
    admin = catalog['admin']
    triggers = dict(admin.execute("""select t.tgname,p.proname from pg_trigger t join pg_proc p on p.oid=t.tgfoid
        where t.tgrelid='ontology.objects'::regclass and t.tgname like 'nexloop_relation_derive%'""").fetchall())
    assert triggers == {'nexloop_relation_derive': 'nexloop_relation_derive_each', 'nexloop_relation_derive_update': 'nexloop_relation_derive_each'}
    assert admin.execute("select count(*) from pg_proc where proname='nexloop_relation_derive'").fetchone()[0] == 0
    granted = admin.execute("""select p.proname,r.rolname from pg_proc p cross join pg_roles r
        where p.proname in ('nexloop_relation_derive_one','nexloop_relation_derive_each') and r.rolname not in ('nexloop_owner')
        and not r.rolsuper and has_function_privilege(r.oid,p.oid,'execute')""").fetchall()
    assert granted == []
