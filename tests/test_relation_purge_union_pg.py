"""NX-054 P4c (the relation purge union migration): the erasure purge also takes the objects the Consumer's ownership relations reach,
as frozen when the purge starts (L6 C-1: the purge itself cuts chains - the messages phase removes a Message's relations, the
references phase deletes a Conversation, and its message_in, before the RelationshipAssessment behind it). With the first batch
published and relations only (no properties): a Goal (goal_of) and a Commitment (commitment_to) at depth 1, a Conversation at depth 1
with a Message (message_in, depth 2) and a RelationshipAssessment on that Message (assessment_evidence, depth 3). Run from the
messages phase with batch 1 (the chain cut by hand after the first step) and from the references phase with batch 1, the
references phase deletes the Goal, the Conversation and the assessment, the commitments phase pseudonymizes the Commitment;
objects found by their property go as before; another Consumer's objects stay; the frozen rows go with the consumer phase."""
import pytest

from support.tenants import TENANT_A
from test_relation_backfill_engine_pg import hexid, stock
from test_relation_read_union_pg import relation as linked
from test_relation_types_p1_pg import catalog, publish  # noqa: F401


def relation(admin, name, source_type, source, target_type, target):
    linked(admin, name, source_type, source, target_type, target, tenant=TENANT_A)


def exists(admin, object_id):
    return admin.execute('select properties from ontology.objects where object_id=%s', (object_id,)).fetchone()


def seed(admin):
    consumer, other = hexid('purge-consumer'), hexid('purge-other')
    for c in (consumer, other):
        stock(admin, 'Consumer', c, {})
    ids = {k: hexid('purge-' + k) for k in ('goal', 'conversation', 'message', 'assessment', 'by_property', 'theirs', 'commitment')}
    stock(admin, 'Goal', ids['goal'], {})
    relation(admin, 'goal_of', 'Goal', ids['goal'], 'Consumer', consumer)
    stock(admin, 'Conversation', ids['conversation'], {})
    relation(admin, 'conversation_of', 'Conversation', ids['conversation'], 'Consumer', consumer)
    stock(admin, 'Message', ids['message'], {})
    relation(admin, 'message_in', 'Message', ids['message'], 'Conversation', ids['conversation'])
    stock(admin, 'RelationshipAssessment', ids['assessment'], {})
    relation(admin, 'assessment_evidence', 'RelationshipAssessment', ids['assessment'], 'Message', ids['message'])
    stock(admin, 'Goal', ids['by_property'], {'consumer_id': consumer})
    stock(admin, 'Goal', ids['theirs'], {'consumer_id': other})
    relation(admin, 'goal_of', 'Goal', ids['theirs'], 'Consumer', other)
    stock(admin, 'Commitment', ids['commitment'], {'status': 'open'})
    relation(admin, 'commitment_to', 'Commitment', ids['commitment'], 'Consumer', consumer)
    return consumer, ids


def erase(admin, consumer, phase):
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT_A,))
        admin.execute("""insert into control.nexloop_consumer_erasures(tenant_id,world,request_id,consumer_id,source,requested_by,reason,state,phase,watermark)
            values(%s,'real','purge-union',%s,'owner','synthetic-owner','synthetic erasure','purging',%s,nextval('control.nexloop_deletion_watermark'))""",
                      (TENANT_A, consumer, phase))


def step(admin):
    with admin.transaction():
        return admin.execute("select runtime.nexloop_erasure_step(%s,'real','purge-union',1,'synthetic-keeper')", (TENANT_A,)).fetchone()[0]


def frozen(admin):
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT_A,))
        return {r for r in admin.execute("select type_name,object_id from control.nexloop_erasure_relation_targets where request_id='purge-union'").fetchall()}


def run_to(admin, phase, limit=200):
    for _ in range(limit):
        got = step(admin)
        if got.get('phase') == phase or not got.get('progress'):
            return got
    raise AssertionError('no progress to ' + phase)


def assert_purged(admin, ids, commitments_ran):
    for k in ('goal', 'conversation', 'assessment', 'by_property'):
        assert exists(admin, ids[k]) is None, k
    assert exists(admin, ids['theirs']) is not None
    # The commitments phase comes before references: from references on it does not run in this request.
    assert (exists(admin, ids['commitment'])[0].get('made_to') == 'erasure:purge-union') is commitments_ran


@pytest.mark.parametrize('start', ['messages', 'references'])
def test_the_purge_takes_the_frozen_relation_targets_batch_by_batch(catalog, start):  # noqa: F811
    admin = catalog['admin']
    publish(catalog)
    consumer, ids = seed(admin)
    erase(admin, consumer, start)
    got = step(admin)
    targets = frozen(admin)
    assert {('Goal', ids['goal']), ('Conversation', ids['conversation']), ('Message', ids['message']),
            ('RelationshipAssessment', ids['assessment']), ('Commitment', ids['commitment'])} <= targets
    assert ('Goal', ids['theirs']) not in targets and all(t != 'Consumer' for t, _ in targets)
    if start == 'messages':
        # The chain cut as the messages phase cuts it (the relations of an erased Message): the frozen targets still hold.
        admin.execute("delete from ontology.relations where relation_name in ('message_in','assessment_evidence')")
    got = run_to(admin, 'relay')
    assert got.get('phase') == 'relay', got
    assert_purged(admin, ids, commitments_ran=start == 'messages')
