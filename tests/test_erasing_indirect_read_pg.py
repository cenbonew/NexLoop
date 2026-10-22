"""Erasing indirect read (0315; L6 world-model-visibility review 1-1): an object that belongs to a Consumer only through a property -
a Commitment (made_to), a CommercialRecord (consumer_ref), a RelationshipAssessment (source_id / target_id), any other object naming
consumer_id - is refused by the read guard and answered unreadable by object_fields while that Consumer is being erased, through
the one resolution runtime.nexloop_object_consumers; browsing them leaves no refused read; another Consumer's objects and the state
after the erasure (references unlinked) read as before. Admin only seeds and probes; the workbench reads go through the login."""
import psycopg
import pytest
from psycopg.types.json import Jsonb

from erasure_support import mark_erasing
from test_workbench_objects_pg import TENANT, hexid, obj, refused_reads, workbench  # noqa: F401 - the fixture
from test_workbench_read_pg import (browser_business, conversations, identity, provision, published_action, uow,  # noqa: F401
    workbench_login)

INDIRECT = (('Commitment', 'made_to'), ('CommercialRecord', 'consumer_ref'), ('RelationshipAssessment', 'source_id'),
            ('RelationshipAssessment', 'target_id'), ('MessageAssignment', 'consumer_id'), ('ConsumerRoleLink', 'consumer_id'))


def guard(admin, type_name, object_id):
    """The read guard every audited read runs (assert_read_authority -> assert_not_erasing): None when it passes, else the refusal."""
    try:
        with admin.transaction():
            admin.execute("select runtime.nexloop_assert_not_erasing('real',%s)",
                          (Jsonb({'tenant_id': TENANT, 'resource_id': f'eios:object:{type_name}/{object_id}'}),))
        return None
    except psycopg.errors.InsufficientPrivilege as error:
        return error.diag.message_primary


def fields(admin, type_name, object_id):
    with admin.transaction():
        return admin.execute("select authz.nexloop_workbench_object_fields(%s,'real','owner',%s,%s)", (TENANT, type_name, object_id)).fetchone()[0]


def put(admin, type_name, object_id, props):
    """A synthetic stand-in of a formal object (the governed creation guards are bypassed for the seed only)."""
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        obj(admin, type_name, object_id, props)


def seed(admin, consumer, other):
    mine, theirs = {}, {}
    for type_name, prop in INDIRECT:
        mine[(type_name, prop)] = hexid(f'indirect-{type_name}-{prop}')
        theirs[(type_name, prop)] = hexid(f'indirect-other-{type_name}-{prop}')
        put(admin, type_name, mine[(type_name, prop)], {prop: consumer})
        put(admin, type_name, theirs[(type_name, prop)], {prop: other})
    return mine, theirs


def test_objects_of_an_erasing_consumer_through_a_property_are_refused_and_hidden(workbench, monkeypatch):  # noqa: F811
    w = workbench
    provision(w)
    admin, consumer = w['admin'], w['f']['consumer']
    other = hexid('indirect-other-consumer')
    obj(admin, 'Consumer', other, {})
    mine, theirs = seed(admin, consumer, other)
    # Before the erasure: every object passes the guard; the Commitment is readable to the owner.
    assert all(guard(admin, t, i) is None for (t, _), i in {**mine, **theirs}.items())
    assert fields(admin, 'Commitment', mine[('Commitment', 'made_to')])['readable'] is True
    mark_erasing(admin, TENANT, consumer)
    for (type_name, prop), object_id in mine.items():
        assert guard(admin, type_name, object_id) == 'consumer is being erased', (type_name, prop)
        assert fields(admin, type_name, object_id)['readable'] is False, (type_name, prop)
        assert guard(admin, type_name, theirs[(type_name, prop)]) is None, (type_name, prop)  # another Consumer's: unchanged
    assert fields(admin, 'Commitment', theirs[('Commitment', 'made_to')])['readable'] is True
    # Property claims of the object too.
    with pytest.raises(psycopg.errors.InsufficientPrivilege), admin.transaction():
        admin.execute("select runtime.nexloop_assert_not_erasing('real',%s)",
                      (Jsonb({'tenant_id': TENANT, 'resource_id': f"eios:property:Commitment/{mine[('Commitment', 'made_to')]}/made_to"}),))
    # Browsing: the erasing Consumer's Commitment is hidden, the other's listed, and no read is refused.
    refused = refused_reads(monkeypatch)
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        page = client.get('/api/v1/workbench/objects/Commitment').json()
        listed = {x['object_id'] for x in page['items']}
        assert theirs[('Commitment', 'made_to')] in listed and mine[('Commitment', 'made_to')] not in listed and page['invisible'] >= 1
        assert client.get(f"/api/v1/workbench/objects/Commitment/{mine[('Commitment', 'made_to')]}/values").status_code in (403, 404)
        n = client.get(f'/api/v1/workbench/objects/Consumer/{other}/neighbors').json()
        assert any(x['object_id'] == theirs[('Commitment', 'made_to')] for g in n['groups'] for x in g['items'])
    assert refused == []
    # After the erasure: the references are unlinked (the purge pseudonymizes or deletes them) and the erasure completed - the objects
    # that remain read as before; the Consumer's own id is still refused.
    with admin.transaction():
        admin.execute("set local session_replication_role=replica")
        admin.execute("update control.nexloop_consumer_erasures set state='completed' where tenant_id=%s and consumer_id=%s", (TENANT, consumer))
        for (type_name, prop), object_id in mine.items():
            admin.execute("update ontology.objects set properties=%s where tenant_id=%s and type_name=%s and object_id=%s",
                          (Jsonb({prop: 'pseudonym:' + 'a' * 32}), TENANT, type_name, object_id))
    for (type_name, _), object_id in mine.items():
        assert guard(admin, type_name, object_id) is None
    assert fields(admin, 'Commitment', mine[('Commitment', 'made_to')])['readable'] is True
    assert guard(admin, 'Consumer', consumer) == 'consumer is being erased'


def test_an_evidence_message_links_a_relationship_assessment(workbench):  # noqa: F811
    """A RelationshipAssessment whose evidence Message is the erasing Consumer's (the purge's own link, 0227) is refused too."""
    w = workbench
    provision(w)
    admin, consumer = w['admin'], w['f']['consumer']
    message = admin.execute("""select m.message_id from runtime.nexloop_conversation_messages m join runtime.nexloop_conversations c
        on c.tenant_id=m.tenant_id and c.world=m.world and c.conversation_id=m.conversation_id where c.tenant_id=%s and c.consumer_id=%s limit 1""",
                            (TENANT, consumer)).fetchone()
    if message is None:
        pytest.skip('the fixture has no Message of the Consumer')
    assessment = hexid('indirect-evidence')
    put(admin, 'RelationshipAssessment', assessment, {'source_id': hexid('a'), 'target_id': hexid('b'), 'evidence_message_id': message[0]})
    assert guard(admin, 'RelationshipAssessment', assessment) is None
    mark_erasing(admin, TENANT, consumer)
    assert guard(admin, 'RelationshipAssessment', assessment) == 'consumer is being erased'
