"""NX-054 P4c (the relation purge union migration): the erasure purge also takes the objects the Consumer's ownership relations reach.
With the first batch published: a Goal linked to the erased Consumer only by a relation (no consumer_id) and a PlanStep reached
through it are deleted by the references phase; a Commitment linked only by commitment_to is pseudonymized by the commitments phase;
objects found by their property still go as before; another Consumer's objects stay. The steps run as the keeper's port would
(runtime.nexloop_erasure_step), from the commitments phase to relay."""
import psycopg
import pytest
from psycopg.types.json import Jsonb

from support.tenants import TENANT_A
from test_relation_backfill_engine_pg import hexid, stock
from test_relation_read_union_pg import relation as linked
from test_relation_types_p1_pg import catalog, publish  # noqa: F401


def relation(admin, name, source_type, source, target_type, target):
    linked(admin, name, source_type, source, target_type, target, tenant=TENANT_A)


def exists(admin, object_id):
    return admin.execute('select properties from ontology.objects where object_id=%s', (object_id,)).fetchone()


def test_the_purge_takes_what_the_relations_reach(catalog):  # noqa: F811
    admin = catalog['admin']
    publish(catalog)
    consumer, other = hexid('purge-consumer'), hexid('purge-other')
    for c in (consumer, other):
        stock(admin, 'Consumer', c, {})
    goal, step, by_property, theirs, commitment = (hexid(x) for x in ('purge-goal', 'purge-step', 'purge-goal-prop', 'purge-goal-other', 'purge-commitment'))
    stock(admin, 'Goal', goal, {})  # no consumer_id: only the relation links it
    relation(admin, 'goal_of', 'Goal', goal, 'Consumer', consumer)
    stock(admin, 'PlanStep', step, {})
    relation(admin, 'plan_step_of', 'PlanStep', step, 'Consumer', consumer)
    stock(admin, 'Goal', by_property, {'consumer_id': consumer})
    stock(admin, 'Goal', theirs, {'consumer_id': other})
    relation(admin, 'goal_of', 'Goal', theirs, 'Consumer', other)
    stock(admin, 'Commitment', commitment, {'status': 'open'})  # no made_to
    relation(admin, 'commitment_to', 'Commitment', commitment, 'Consumer', consumer)
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT_A,))
        admin.execute("""insert into control.nexloop_consumer_erasures(tenant_id,world,request_id,consumer_id,source,requested_by,reason,state,phase,watermark)
            values(%s,'real','purge-union',%s,'owner','synthetic-owner','synthetic erasure','purging','commitments',nextval('control.nexloop_deletion_watermark'))""",
                      (TENANT_A, consumer))
    for _ in range(20):
        with admin.transaction():
            got = admin.execute("select runtime.nexloop_erasure_step(%s,'real','purge-union',100,'synthetic-keeper')", (TENANT_A,)).fetchone()[0]
        if got.get('phase') == 'relay' or not got.get('progress'):
            break
    assert got.get('phase') == 'relay', got
    assert exists(admin, goal) is None and exists(admin, step) is None and exists(admin, by_property) is None
    assert exists(admin, theirs) is not None
    assert exists(admin, commitment)[0]['made_to'] == 'erasure:purge-union'
