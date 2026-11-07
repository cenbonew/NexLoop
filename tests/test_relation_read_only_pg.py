"""NX-054 P5 draft (migration relation_read_only; docs/design/relation-read-p5.md): once the tenant's relation catalog is published,
the erasing read guard and object_fields read an object's Consumers through the ownership relations only - the property rule is no
longer a fallback - except a RelationshipAssessment's source_id / target_id (no relation in the first batch). Chains are followed
(Message -> Conversation -> Consumer; an assessment's evidence Message). A tenant without a published catalog keeps the property
rule (test_erasing_indirect_read_pg). The rollback SQL (docs/design/relation-read-p5-rollback.sql) brings the P4a union back."""
from pathlib import Path

from erasure_support import mark_erasing
from test_erasing_indirect_read_pg import TENANT, fields, guard, put
from test_relation_read_union_pg import published, relation  # noqa: F401 - the fixture
from test_workbench_objects_pg import hexid, obj, workbench  # noqa: F401 - the fixture
from test_workbench_read_pg import browser_business, conversations, identity, published_action, uow  # noqa: F401

ROLLBACK = Path(__file__).resolve().parents[1] / 'docs/design/relation-read-p5-rollback.sql'
ERASING = 'consumer is being erased'


def consumers(admin, type_name, object_id):
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT,))
        return admin.execute("select runtime.nexloop_object_consumers_read(%s,'real',%s,%s)", (TENANT, type_name, object_id)).fetchone()[0]


def backfilled(admin, run='p5-backfill'):
    """A backfill run over the current derivation map reaching completed (the coverage trigger records every derivation)."""
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT,))
        admin.execute("""insert into control.nexloop_relation_backfill_runs(tenant_id,backfill_run,derivations_digest,state,started_by)
            values(%s,%s,control.nexloop_relation_backfill_digest(%s),'running','synthetic')""", (TENANT, run, TENANT))
        admin.execute("update control.nexloop_relation_backfill_runs set state='completed' where tenant_id=%s and backfill_run=%s", (TENANT, run))
    return admin.execute('select count(*) from control.nexloop_relation_backfill_coverage where backfill_run=%s', (run,)).fetchone()[0]


def member_may_read(admin, type_name, object_id):
    with admin.transaction():
        return admin.execute("select authz.nexloop_workbench_member_may_read(%s,'real','owner',%s)",
                             (TENANT, f'eios:object:{type_name}/{object_id}')).fetchone()[0]


def test_relations_only_once_the_catalog_is_published(published, admin):
    consumer = published['f']['consumer']
    assert backfilled(admin) > 0
    other = hexid('p5-other-consumer')
    obj(admin, 'Consumer', other, {})
    goal_rel, goal_prop, goal_theirs = hexid('p5-goal-relation'), hexid('p5-goal-property'), hexid('p5-goal-theirs')
    put(admin, 'Goal', goal_rel, {})
    relation(admin, 'goal_of', 'Goal', goal_rel, 'Consumer', consumer)
    put(admin, 'Goal', goal_prop, {'consumer_id': consumer})  # replica: no relation derived - a gap
    put(admin, 'Goal', goal_theirs, {'consumer_id': other})
    relation(admin, 'goal_of', 'Goal', goal_theirs, 'Consumer', other)
    # A chain: a Message in a Conversation of the Consumer; an assessment whose evidence is that Message.
    conversation, message, assessment = hexid('p5-conversation'), hexid('p5-message'), hexid('p5-assessment')
    put(admin, 'Conversation', conversation, {})  # the relation endpoints (ontology objects, as the derivation writes them)
    put(admin, 'Message', message, {})
    relation(admin, 'conversation_of', 'Conversation', conversation, 'Consumer', consumer)
    relation(admin, 'message_in', 'Message', message, 'Conversation', conversation)
    put(admin, 'RelationshipAssessment', assessment, {'source_id': hexid('p5-a'), 'target_id': hexid('p5-b')})
    relation(admin, 'assessment_evidence', 'RelationshipAssessment', assessment, 'Message', message)
    # source_id / target_id: still by property (no relation in the first batch).
    by_source = hexid('p5-assessment-source')
    put(admin, 'RelationshipAssessment', by_source, {'source_id': consumer, 'target_id': other})
    assert consumers(admin, 'Goal', goal_rel) == [consumer] and consumers(admin, 'Goal', goal_prop) == []
    assert consumers(admin, 'Message', message) == [consumer] and consumers(admin, 'RelationshipAssessment', assessment) == sorted([consumer, hexid('p5-a'), hexid('p5-b')])  # evidence by relation, source/target by property
    assert consumers(admin, 'RelationshipAssessment', by_source) == sorted([consumer, other]) and consumers(admin, 'Consumer', other) == [other]
    mark_erasing(admin, TENANT, consumer)
    for type_name, object_id in (('Goal', goal_rel), ('RelationshipAssessment', assessment), ('RelationshipAssessment', by_source)):
        assert guard(admin, type_name, object_id) == ERASING, (type_name, object_id)
        assert fields(admin, type_name, object_id)['readable'] is False, (type_name, object_id)
    assert guard(admin, 'Message', message) == ERASING
    # The property alone no longer refuses (it is the gap the 7-day gate requires to be empty); another Consumer's object reads.
    assert guard(admin, 'Goal', goal_prop) is None and fields(admin, 'Goal', goal_prop)['readable'] is True
    assert guard(admin, 'Goal', goal_theirs) is None and fields(admin, 'Goal', goal_theirs)['readable'] is True


def test_the_rollback_brings_the_property_fallback_back(published, admin):
    consumer = published['f']['consumer']
    backfilled(admin)
    goal = hexid('p5-rollback-goal')
    put(admin, 'Goal', goal, {'consumer_id': consumer})
    mark_erasing(admin, TENANT, consumer)
    assert guard(admin, 'Goal', goal) is None
    with admin.transaction():
        admin.execute(ROLLBACK.read_text())
    assert guard(admin, 'Goal', goal) == ERASING and fields(admin, 'Goal', goal)['readable'] is False
    owner, proc = admin.execute("""select r.rolname,p.proname from pg_proc p join pg_roles r on r.oid=p.proowner
        where p.proname like 'nexloop_object_consumers_read%%'""").fetchone()
    assert (owner, proc) == ('nexloop_owner', 'nexloop_object_consumers_read')
    assert admin.execute("select count(*) from pg_proc where proname like 'nexloop_object_consumers_read%%'").fetchone()[0] == 1


def test_an_unpublished_assessment_evidence_resolves_the_evidence_message_by_property(published, admin):
    """Stage published the catalog with assessment_evidence deferred (NX-054-stage-publish.md §8 step 4): an assessment's evidence
    Message still ties it to its Consumer by the property rule, so P5 does not wait for catalog version 2."""
    consumer = published['f']['consumer']
    message = admin.execute("""select m.message_id from runtime.nexloop_conversation_messages m join runtime.nexloop_conversations c
        on c.tenant_id=m.tenant_id and c.world=m.world and c.conversation_id=m.conversation_id where c.tenant_id=%s and c.consumer_id=%s limit 1""",
                            (TENANT, consumer)).fetchone()
    assert message is not None, 'the fixture has a Message of the Consumer'
    assessment = hexid('p5-deferred-evidence')
    put(admin, 'RelationshipAssessment', assessment, {'source_id': hexid('p5-c'), 'target_id': hexid('p5-d'), 'evidence_message_id': message[0]})
    backfilled(admin)
    assert consumer not in consumers(admin, 'RelationshipAssessment', assessment)  # published and backfilled: the evidence goes through relations only (none here)
    with admin.transaction():  # as on stage: the catalog published without assessment_evidence, so it has no derivation
        admin.execute("delete from control.nexloop_relation_derivations where tenant_id=%s and relation_name='assessment_evidence'", (TENANT,))
    assert consumers(admin, 'RelationshipAssessment', assessment) == sorted([consumer, hexid('p5-c'), hexid('p5-d')])
    mark_erasing(admin, TENANT, consumer)
    assert guard(admin, 'RelationshipAssessment', assessment) == ERASING
    assert fields(admin, 'RelationshipAssessment', assessment)['readable'] is False


def test_a_published_type_keeps_the_property_rule_until_its_backfill_completes(published, admin):
    """C1: relation types published but not yet backfilled (stock objects have no relation) keep the property rule; a completed
    backfill switches them to relations only; a type that its backfill no longer covers (a new version published after it, or the
    run reverted) has the fallback back."""
    consumer = published['f']['consumer']
    goal = hexid('p5-c1-goal')
    put(admin, 'Goal', goal, {'consumer_id': consumer})  # a stock object: property, no relation yet
    mark_erasing(admin, TENANT, consumer)
    assert guard(admin, 'Goal', goal) == ERASING and fields(admin, 'Goal', goal)['readable'] is False  # published, not backfilled
    assert member_may_read(admin, 'Goal', goal) is False
    backfilled(admin, 'p5-c1')
    assert guard(admin, 'Goal', goal) is None  # relations only now: this stock object is a gap the backfill would have closed
    # A newer version of goal_of published after the backfill: not covered, the fallback is back for Goals only.
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT,))
        admin.execute("delete from control.nexloop_relation_backfill_coverage where tenant_id=%s and relation_name='goal_of'", (TENANT,))
    assert guard(admin, 'Goal', goal) == ERASING
    commitment = hexid('p5-c1-commitment')
    put(admin, 'Commitment', commitment, {'made_to': consumer})
    assert guard(admin, 'Commitment', commitment) is None  # commitment_to still covered: relations only
    # The run reverted: none of its coverage remains.
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT,))
        admin.execute("update control.nexloop_relation_backfill_runs set state='reverting' where tenant_id=%s and backfill_run='p5-c1'", (TENANT,))
    assert admin.execute("select count(*) from control.nexloop_relation_backfill_coverage where backfill_run='p5-c1'").fetchone()[0] == 0
    assert guard(admin, 'Commitment', commitment) == ERASING


def test_the_workbench_member_read_follows_the_relations_only_mode(published, admin):
    """C2: the third reader, authz.nexloop_workbench_member_may_read (0387), resolves through the same function: in relation-only
    mode a member still cannot read an object tied to the erasing Consumer by a relation; one tied only by a property is a gap."""
    consumer = published['f']['consumer']
    by_relation, by_property = hexid('p5-c2-relation'), hexid('p5-c2-property')
    put(admin, 'Goal', by_relation, {})
    relation(admin, 'goal_of', 'Goal', by_relation, 'Consumer', consumer)
    put(admin, 'Goal', by_property, {'consumer_id': consumer})
    backfilled(admin)
    assert member_may_read(admin, 'Goal', by_relation) is True
    mark_erasing(admin, TENANT, consumer)
    assert member_may_read(admin, 'Goal', by_relation) is False
    assert member_may_read(admin, 'Goal', by_property) is True
