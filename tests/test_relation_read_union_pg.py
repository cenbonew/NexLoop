"""NX-054 P4a (the relation read union migration; docs/design/relation-read-p4.md v2): the erasing read guard and object_fields
resolve an object's Consumers as relations united with the property rule. On the workbench fixture with the first relation batch
published: an object linked to the erasing Consumer only by a relation (no property) is now refused and unreadable; one linked only
by its property still is (never fewer than 0315); another Consumer's objects read as before. The gap check
authz.nexloop_relation_read_gaps: the metrics login only, counts only, gaps / gaps_erasing / relation_only per world and type, and a
backfilled gap goes to 0."""
import hashlib

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from erasure_support import mark_erasing
from nexloop_eios import relation_catalog as R
from test_erasing_indirect_read_pg import TENANT, fields, guard, put
from test_workbench_objects_pg import hexid, obj, workbench  # noqa: F401 - the fixture
from test_workbench_read_pg import (browser_business, conversations, identity, provision, published_action, uow,  # noqa: F401
    workbench_login)


def relation(admin, name, source_type, source, target_type, target, world='real', tenant=TENANT):
    """A relation written as the governed link entry would (not derived): its 0016 id, metadata empty."""
    rid = hashlib.sha256(f'["{tenant}", "{world}", "{name}", "{source_type}", "{source}", "{target_type}", "{target}"]'.encode()).hexdigest()
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (tenant,))
        admin.execute("""insert into ontology.relations(relation_id,tenant_id,world,relation_name,schema_version,source_type,source_object_id,
            target_type,target_object_id,metadata,created_at) values(%s,%s,%s,%s,1,%s,%s,%s,%s,'{}',clock_timestamp())""",
                      (rid, tenant, world, name, source_type, source, target_type, target))


@pytest.fixture
def published(workbench, admin, pg):  # noqa: F811 - imported fixture
    w = workbench
    provision(w)
    for t in sorted({t for spec in R.FIRST_BATCH for t in (spec.source_type, spec.target_type)}):  # the types the batch names
        admin.execute("""insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,%s,1,%s)
            on conflict do nothing""", (TENANT, t, Jsonb({'type_name': t, 'version': 1, 'properties': []})))
    admin.execute('alter role nexloop_configurator login')
    with psycopg.connect(make_conninfo(pg, user='nexloop_configurator'), autocommit=True) as db:
        assert R.publish(db, TENANT)['published'] is True
    admin.execute('alter role nexloop_metrics login')
    return w


def gaps(pg):
    with psycopg.connect(make_conninfo(pg, user='nexloop_metrics')) as db:
        db.execute('set transaction read only')
        return db.execute('select authz.nexloop_relation_read_gaps(%s)', (TENANT,)).fetchone()[0]


def test_a_relation_alone_now_ties_an_object_to_the_erasing_consumer(published, admin):
    consumer = published['f']['consumer']
    other = hexid('union-other-consumer')
    obj(admin, 'Consumer', other, {})
    by_relation, by_property, theirs = hexid('union-commitment-relation'), hexid('union-commitment-property'), hexid('union-commitment-other')
    put(admin, 'Commitment', by_relation, {})  # no made_to: the property rule knows nothing
    relation(admin, 'commitment_to', 'Commitment', by_relation, 'Consumer', consumer)
    put(admin, 'Commitment', by_property, {'made_to': consumer})  # replica: no relation derived
    put(admin, 'Commitment', theirs, {'made_to': other})
    assert guard(admin, 'Commitment', by_relation) is None and fields(admin, 'Commitment', by_relation)['readable'] is True
    mark_erasing(admin, TENANT, consumer)
    for object_id in (by_relation, by_property):
        assert guard(admin, 'Commitment', object_id) == 'consumer is being erased'
        assert fields(admin, 'Commitment', object_id)['readable'] is False
    assert guard(admin, 'Commitment', theirs) is None and fields(admin, 'Commitment', theirs)['readable'] is True


def test_the_gap_check_counts_both_directions_and_the_erasing_part_apart(published, admin, pg):
    consumer, other = published['f']['consumer'], hexid('gap-other-consumer')
    obj(admin, 'Consumer', other, {})
    gap, linked, erasing_gap = hexid('gap-goal'), hexid('gap-linked-goal'), hexid('gap-erasing-goal')
    put(admin, 'Goal', gap, {'consumer_id': other})  # replica: property, no relation -> a gap
    put(admin, 'Goal', linked, {})
    relation(admin, 'goal_of', 'Goal', linked, 'Consumer', other)  # relation, no property -> relation_only
    put(admin, 'Goal', erasing_gap, {'consumer_id': consumer})
    before = gaps(pg)
    goal = next(r for r in before['rows'] if r['world'] == 'real' and r['type_name'] == 'Goal')
    assert goal['gaps'] >= 2 and goal['relation_only'] >= 1 and goal['gaps_erasing'] == 0
    assert set(goal) == {'world', 'type_name', 'objects', 'gaps', 'gaps_erasing', 'relation_only'}  # counts only, no id
    mark_erasing(admin, TENANT, consumer)
    after = next(r for r in gaps(pg)['rows'] if r['world'] == 'real' and r['type_name'] == 'Goal')
    assert after['gaps_erasing'] >= 1 and after['gaps'] == goal['gaps'] - after['gaps_erasing']
    # The gap goes once the relation is there (a backfill, or the property's next change).
    relation(admin, 'goal_of', 'Goal', gap, 'Consumer', other)
    final = next(r for r in gaps(pg)['rows'] if r['world'] == 'real' and r['type_name'] == 'Goal')
    assert final['gaps'] == after['gaps'] - 1


def test_the_gap_check_is_the_metrics_logins_only(published, admin, pg):
    with pytest.raises(psycopg.errors.InsufficientPrivilege), admin.transaction():
        admin.execute('select authz.nexloop_relation_read_gaps(%s)', (TENANT,))  # session_user check, even for a superuser
    for role in ('nexloop_api', 'nexloop_domain_worker'):
        with psycopg.connect(make_conninfo(pg, user=role)) as db, pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute('select authz.nexloop_relation_read_gaps(%s)', (TENANT,))
    volatility = admin.execute("select provolatile from pg_proc where proname='nexloop_relation_read_gaps'").fetchone()[0]
    assert volatility == 's'  # STABLE: it cannot write
