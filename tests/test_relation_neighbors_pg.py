"""NX-054 P4b (the relation neighbors migration): the world model's neighbourhood reads relations for the links the relation catalog
covers. On the workbench fixture with the first batch published: Goals naming the centre Consumer (goal_of derived at their
insert) appear as the relation group ('incoming', 'goal_of', 'Goal') and no longer as the guessed property group
('incoming', 'property:consumer_id', 'Goal'); a link no relation type covers (Consumer.referrer) stays a property group; the
contract still validates and another verb of the workbench read answers as before."""
import jsonschema
import pytest

from test_relation_read_union_pg import published  # noqa: F401 - workbench + first batch published
from test_workbench_objects_pg import contract, hexid, obj, workbench  # noqa: F401
from test_workbench_read_pg import (browser_business, conversations, identity, provision, published_action, uow,  # noqa: F401
    workbench_login)


def test_covered_links_are_relation_groups_and_the_rest_stay(published, admin):  # noqa: F811
    w = published
    centre = w['f']['consumer']
    goals = [hexid(f'p4b-goal-{n}') for n in range(3)]
    for g in goals:
        obj(admin, 'Goal', g, {'consumer_id': centre})  # an ordinary insert: the P2 trigger derives goal_of
    referrer = hexid('p4b-referrer')
    obj(admin, 'Consumer', referrer, {'referrer': centre})  # no relation type for referrer
    assert admin.execute("select count(*) from ontology.relations where relation_name='goal_of' and target_object_id=%s", (centre,)).fetchone()[0] == 3
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        got = client.get(f'/api/v1/workbench/objects/Consumer/{centre}/neighbors')
        assert got.status_code == 200, got.text
        n = got.json()
        jsonschema.validate(n, contract('workbench-object-neighbors'))
        groups = {(g['direction'], g['via'], g['type_name']): g for g in n['groups']}
        assert ('incoming', 'property:consumer_id', 'Goal') not in groups  # covered by goal_of: no guessed group
        relation = groups[('incoming', 'goal_of', 'Goal')]
        assert len(relation['items']) + relation['invisible'] + relation['collapsed'] == 3
        assert ('incoming', 'property:referrer', 'Consumer') in groups  # not covered: as before
        # The outgoing side: the Goal's own neighbourhood shows goal_of to the centre, not property:consumer_id.
        r = client.get(f'/api/v1/workbench/objects/Goal/{goals[0]}/neighbors')
        assert r.status_code == 200, r.text
        own = {(g['direction'], g['via'], g['type_name']) for g in r.json()['groups']}
        assert ('outgoing', 'property:consumer_id', 'Consumer') not in own and ('outgoing', 'goal_of', 'Consumer') in own
        # Another verb of the wrapped read: unchanged.
        assert client.get('/api/v1/workbench/objects/Consumer').status_code == 200
