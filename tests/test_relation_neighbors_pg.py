"""NX-054 P4b (the relation neighbors migration): the world model's neighbourhood reads relations for the links the relation catalog
covers. On the workbench fixture with the first batch published: Goals naming the centre Consumer (goal_of derived at their
insert) appear as the relation group ('incoming', 'goal_of', 'Goal') and no longer as the guessed property group
('incoming', 'property:consumer_id', 'Goal'); a link no relation type covers (Consumer.referrer) stays a property group; the
contract still validates and another verb of the workbench read answers as before. L6 review: a Goal without its relation (written in
replica mode) keeps the property group (the relation group holds fewer), and a MessageAssignment (a type no member derives) in the
assignment_of relation group is counted invisible (each neighbour's READ is checked)."""
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
        # A MessageAssignment naming the centre: its relation group checks the neighbour's READ. Since roles v52 (I35, owner option A)
        # the owner reads MessageAssignment through read_types and sees it; the operator's list is empty and it stays invisible.
        assignment = hexid('p4b-assignment')
        obj(admin, 'MessageAssignment', assignment, {'consumer_id': centre, 'state': 'active'})
        g = {(x['direction'], x['via'], x['type_name']): x for x in client.get(f'/api/v1/workbench/objects/Consumer/{centre}/neighbors').json()['groups']}
        assert [i['object_id'] for i in g[('incoming', 'assignment_of', 'MessageAssignment')]['items']] == [assignment]
        assert g[('incoming', 'assignment_of', 'MessageAssignment')]['invisible'] == 0
    with w['client']() as other:
        assert workbench_login(other, w['operator']).status_code == 200
        g = {(x['direction'], x['via'], x['type_name']): x for x in other.get(f'/api/v1/workbench/objects/Consumer/{centre}/neighbors').json()['groups']}
        assert g[('incoming', 'assignment_of', 'MessageAssignment')]['items'] == [] and g[('incoming', 'assignment_of', 'MessageAssignment')]['invisible'] == 1
        assert assignment not in str(g)
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        # A Goal without its relation (replica: nothing derived): the relation group holds 3, the property group 4 - it stays.
        with admin.transaction():
            admin.execute('set local session_replication_role=replica')
            obj(admin, 'Goal', hexid('p4b-goal-unrelated'), {'consumer_id': centre})
        g = {(x['direction'], x['via'], x['type_name']): x for x in client.get(f'/api/v1/workbench/objects/Consumer/{centre}/neighbors').json()['groups']}
        prop = g[('incoming', 'property:consumer_id', 'Goal')]
        assert len(prop['items']) + prop['invisible'] + prop['collapsed'] == 4 and ('incoming', 'goal_of', 'Goal') in g
        assert client.get('/api/v1/workbench/objects/Consumer').status_code == 200
