"""NX-054 P2 (0319; ADR-038 decision 5 option a): relations derived from the objects' reference properties at commit, and deleted
with either end. Derivation follows the property (put, change, clear, pseudonym), sees targets written later in the same
transaction (N-2), counts dangling references, refuses a cardinality conflict at commit; deleting an object - 0129's financial
retention, a whole simulation world - is never stopped by a relation (N-1); the production write paths leave relation_dangling at 0
and every reference property with its relation. Admin seeds Consumers (configuration stand-ins) with triggers suspended and writes
the referencing objects with triggers on, so derivation runs as it does for a governed write."""
import copy
import hashlib

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from nexloop_eios import relation_catalog as R
from support.tenants import TENANT_A
from test_relation_types_p1_pg import catalog, publish  # noqa: F401 - the P1 fixture (object types, configurator)
from message_runtime_fixture import message_plan  # noqa: F401 - Actions-only object writes
from commercial_fixture import TENANT as TENANT_RETENTION
from commitment_fixture import commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_retention_pg import kept  # noqa: F401 - the NX-029 retention fixture (commercial configuration, keeper)


def hexid(label):
    return hashlib.sha256(label.encode()).hexdigest()


def put(admin, type_name, object_id, props, *, world='real', replica=False, tenant=TENANT_A):
    sql = """insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
        values(%s,%s,%s,%s,1,%s,'synthetic','p2',clock_timestamp(),clock_timestamp())"""
    if replica:
        with admin.transaction():
            admin.execute('set local session_replication_role=replica')
            admin.execute(sql, (tenant, world, type_name, object_id, Jsonb(props)))
    else:
        admin.execute(sql, (tenant, world, type_name, object_id, Jsonb(props)))


def edges(admin, name='goal_of', source=None):
    return admin.execute('select source_object_id,target_object_id,world from ontology.relations where relation_name=%s::text and (%s::text is null or source_object_id=%s::text) order by 1,2',
                         (name, source, source)).fetchall()


def dangling(admin):
    return admin.execute('select runtime.nexloop_relation_dangling(%s)', (TENANT_A,)).fetchone()[0]


def test_derivation_follows_the_property(catalog):  # noqa: F811 - imported fixtures
    c = catalog
    admin = c['admin']
    publish(c)
    a, b, g = hexid('consumer-a'), hexid('consumer-b'), hexid('goal-1')
    put(admin, 'Consumer', a, {}, replica=True)
    put(admin, 'Consumer', b, {}, replica=True)
    put(admin, 'Goal', g, {'consumer_id': a})
    assert edges(admin, source=g) == [(g, a, 'real')]
    admin.execute("update ontology.objects set properties=jsonb_set(properties,'{consumer_id}',to_jsonb(%s::text)) where object_id=%s", (b, g))
    assert edges(admin, source=g) == [(g, b, 'real')]  # changed: the old one dropped, the new one put
    admin.execute("update ontology.objects set properties=properties-'consumer_id' where object_id=%s", (g,))
    assert edges(admin, source=g) == []  # cleared
    admin.execute("update ontology.objects set properties=jsonb_build_object('consumer_id',%s::text) where object_id=%s", (b, g))
    admin.execute("update ontology.objects set properties=jsonb_build_object('consumer_id','pseudonym:'||repeat('a',32)) where object_id=%s", (g,))
    assert edges(admin, source=g) == [] and dangling(admin) == {}  # the erasure's pseudonym: dropped, not counted (I-5)
    # Dangling: a value naming no object is counted, no relation.
    put(admin, 'Goal', hexid('goal-2'), {'consumer_id': hexid('nobody')})
    assert edges(admin, source=hexid('goal-2')) == [] and dangling(admin) == {'real:goal_of': 1}
    # Only the object's own relation names: a PlanStep with goal and consumer gets both, its control (absent) is dangling.
    s = hexid('step-1')
    put(admin, 'PlanStep', s, {'consumer_id': a, 'goal_id': g, 'control_id': hexid('no-control')})
    assert edges(admin, 'plan_step_of', s) == [(s, a, 'real')] and edges(admin, 'step_in_goal', s) == [(s, g, 'real')]
    assert dangling(admin)['real:step_under_control'] == 1


def test_a_target_written_later_in_the_transaction_is_seen_at_commit(catalog):  # noqa: F811 - imported fixtures
    """N-2: the trigger runs at commit, so the Goal written before its Consumer in one transaction still gets its relation."""
    c = catalog
    admin = c['admin']
    publish(c)
    consumer, goal = hexid('late-consumer'), hexid('early-goal')
    with admin.transaction():
        put(admin, 'Goal', goal, {'consumer_id': consumer})
        admin.execute('set local session_replication_role=replica')  # the Consumer's own creation guards (a configuration stand-in)
        put(admin, 'Consumer', consumer, {})
        admin.execute('set local session_replication_role=origin')
    assert edges(admin, source=goal) == [(goal, consumer, 'real')] and dangling(admin) == {}
    # The final properties count: a goal created and repointed in one transaction has the final relation only.
    other = hexid('other-consumer')
    put(admin, 'Consumer', other, {}, replica=True)
    with admin.transaction():
        put(admin, 'Goal', hexid('goal-x'), {'consumer_id': consumer})
        admin.execute("update ontology.objects set properties=jsonb_build_object('consumer_id',%s::text) where object_id=%s", (other, hexid('goal-x')))
    assert edges(admin, source=hexid('goal-x')) == [(hexid('goal-x'), other, 'real')]


def test_a_cardinality_conflict_refuses_the_commit(catalog):  # noqa: F811 - imported fixtures
    """Not in the first batch (all many_to_one): a one_to_one type published for the test refuses a second source at commit."""
    c = catalog
    admin = c['admin']
    value = R.manifest()
    extra = copy.deepcopy(value['relation_types'][0])
    extra['definition'].update(relation_name='goal_unique', cardinality='one_to_one', description='test only')
    extra['consumer_ownership'] = None
    value['relation_types'].append(extra)
    publish(c, value)
    consumer = hexid('unique-consumer')
    put(admin, 'Consumer', consumer, {}, replica=True)
    put(admin, 'Goal', hexid('unique-1'), {'consumer_id': consumer})
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='relation cardinality conflict'):
        put(admin, 'Goal', hexid('unique-2'), {'consumer_id': consumer})
    assert admin.execute('select count(*) from ontology.objects where object_id=%s', (hexid('unique-2'),)).fetchone() == (0,)  # the commit was refused


def test_deleting_either_end_takes_its_relations(catalog):  # noqa: F811 - imported fixtures
    c = catalog
    admin = c['admin']
    publish(c)
    consumer, goal = hexid('del-consumer'), hexid('del-goal')
    put(admin, 'Consumer', consumer, {}, replica=True)
    put(admin, 'Goal', goal, {'consumer_id': consumer})
    assert len(edges(admin, source=goal)) == 1
    with admin.transaction():
        admin.execute('set local session_replication_role=origin')
        admin.execute("delete from ontology.objects where type_name='Consumer' and object_id=%s", (consumer,))  # the target end
    assert edges(admin, source=goal) == []
    put(admin, 'Consumer', consumer, {}, replica=True)
    admin.execute("update ontology.objects set updated_at=clock_timestamp(),properties=properties where object_id=%s", (goal,))
    assert edges(admin, source=goal) == []  # P-1: an update that leaves the properties equal queues nothing
    admin.execute("update ontology.objects set properties=properties||'{\"state\":\"active\"}' where object_id=%s", (goal,))
    assert len(edges(admin, source=goal)) == 1
    admin.execute("delete from ontology.objects where type_name='Goal' and object_id=%s", (goal,))  # the source end
    assert admin.execute('select count(*) from ontology.relations').fetchone() == (0,)


def test_a_whole_simulation_world_is_deleted_with_its_relations(catalog):  # noqa: F811 - imported fixtures
    """N-1: the sim world purge (0260) is not stopped by relations (here derived in the world, endpoints in that world)."""
    from test_nx041_sim_worlds_pg import create
    c = catalog
    admin = c['admin']
    publish(c)
    world = create(admin)['world']
    consumer, goal = hexid('sim-consumer'), hexid('sim-goal')
    put(admin, 'Consumer', consumer, {}, world=world, replica=True)
    put(admin, 'Goal', goal, {'consumer_id': consumer}, world=world)
    assert edges(admin, source=goal) == [(goal, consumer, world)]
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT_A,))
    admin.execute("update control.nexloop_sim_worlds set status='deleting' where world=%s", (world,))
    assert admin.execute('select runtime.nexloop_sim_world_snapshot(%s,%s,100000)', (TENANT_A, world)).fetchone()[0] is True
    for _ in range(20):
        r = admin.execute('select runtime.nexloop_sim_world_purge(%s,%s,50)', (TENANT_A, world)).fetchone()[0]
        if r['status'] == 'deleted':
            break
    assert r['status'] == 'deleted'
    assert admin.execute('select count(*) from ontology.objects where world=%s', (world,)).fetchone() == (0,)
    assert admin.execute('select count(*) from ontology.relations where world=%s', (world,)).fetchone() == (0,)


@pytest.fixture
def relations_published(monkeypatch):
    """Publishes the relation types the fixture's object types allow, right after the fixture publishes its system catalog -
    so every object the fixture then creates through the governed Actions is derived."""
    import message_runtime_fixture as F
    original = F.publish_system_catalog

    def with_relations(admin, tenant, types, *args, **kwargs):
        out = original(admin, tenant, types, *args, **kwargs)
        present = set(types)
        specs = closed([s for s in R.FIRST_BATCH if s.source_type in present and s.target_type in present])
        admin.execute('alter role nexloop_configurator login')
        with psycopg.connect(make_conninfo(admin.info.dsn, user='nexloop_configurator'), autocommit=True) as db:
            R.publish(db, tenant, R.manifest(specs))
        relations_published.tenant = tenant
        return out
    monkeypatch.setattr(F, 'publish_system_catalog', with_relations)
    return relations_published


def closed(specs):
    """Drop transitive ownership types whose chain leaves the published types (publication refuses a chain not reaching Consumer)."""
    while True:
        owners = {s.source_type for s in specs if s.ownership}
        keep = [s for s in specs if s.ownership != 'transitive' or s.target_type in owners]
        if len(keep) == len(specs):
            return keep
        specs = keep


def test_the_production_write_paths_leave_no_dangling_reference(relations_published, message_plan, admin):  # noqa: F811 - imported fixtures
    """Consumer, Goal, PlanStep, EffectControl, ConsumerOwnership, Conversation and Message written through their governed
    Actions (message_runtime_fixture): every reference property of a published relation type has its relation; nothing dangles."""
    tenant = relations_published.tenant
    admin.execute("select set_config('eios.tenant_id',%s,false)", (tenant,))
    profiles = admin.execute("""select f.relation_name,t.definition->>'source_type',f.source_property,t.definition->>'target_type'
        from control.nexloop_relation_type_profiles f join ontology.relation_type_versions t using(tenant_id,relation_name,version) where f.tenant_id=%s""", (tenant,)).fetchall()
    assert profiles, 'no relation type published'
    expected = set()
    for name, source, prop, target in profiles:
        for object_id, value, world in admin.execute("""select o.object_id,o.properties->>%s,o.world from ontology.objects o where o.tenant_id=%s and o.type_name=%s
                and exists(select 1 from ontology.objects t where t.tenant_id=o.tenant_id and t.world=o.world and t.type_name=%s and t.object_id=o.properties->>%s)""",
                                                     (prop, tenant, source, target, prop)).fetchall():
            expected.add((name, object_id, value, world))
    got = set(admin.execute('select relation_name,source_object_id,target_object_id,world from ontology.relations where tenant_id=%s', (tenant,)).fetchall())
    assert expected and got == expected
    assert {name for name, *_ in got} >= {'conversation_of', 'owned_by'}
    assert admin.execute('select runtime.nexloop_relation_dangling(%s)', (tenant,)).fetchone()[0] == {}


@pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)
def test_the_financial_retention_deletes_a_record_that_has_a_relation(kept, admin):  # noqa: F811 - imported fixtures
    """N-1: 0129's financial class deletes an expired CommercialRecord (events, then the object) without knowing relations; the
    record's relation to its Consumer goes with it instead of stopping the keeper on the end-point foreign key."""
    from test_retention_pg import OLD, backdate, keeper
    c, env = kept
    env.post(env.event('payment.succeeded', 'pay-rel'))
    env.tick()
    record = env.record('payment', 'pay-rel')
    admin.execute("""insert into ontology.relations(relation_id,tenant_id,world,relation_name,schema_version,source_type,source_object_id,target_type,target_object_id,created_at)
        values(%s,%s,'real','commercial_record_of',1,'CommercialRecord',%s,'Consumer',%s,clock_timestamp())""",
                  (hexid('rel-' + record['object_id']), TENANT_RETENTION, record['object_id'], c['consumer']))
    key = env.record_key('payment', 'pay-rel')
    backdate(admin, 'update runtime.nexloop_commercial_events set occurred_at=%s,received_at=%s where record_key=%s', (OLD, OLD, key))
    result = keeper(c, env).run_once()
    assert result['incomplete'] == 0
    assert env.record('payment', 'pay-rel') is None
    assert admin.execute('select count(*) from ontology.relations where source_object_id=%s', (record['object_id'],)).fetchone() == (0,)


def test_a_newer_version_without_source_property_stops_the_derivation(catalog):  # noqa: F811
    """I-1: the latest version decides; v2 of goal_of without source_property drops the derived relations on the next change
    and derives no new ones - it never falls back to v1."""
    c = catalog
    admin = c['admin']
    publish(c)
    consumer, goal = hexid('v-consumer'), hexid('v-goal')
    put(admin, 'Consumer', consumer, {}, replica=True)
    put(admin, 'Goal', goal, {'consumer_id': consumer})
    assert len(edges(admin, source=goal)) == 1
    v2 = R.manifest()
    v2['version'] = 2
    for r in v2['relation_types']:
        if r['definition']['relation_name'] == 'goal_of':
            r['definition']['version'], r['source_property'] = 2, None
    publish(c, v2)
    assert admin.execute("select version,source_property from control.nexloop_relation_derivations where relation_name='goal_of'").fetchone() == (2, None)
    admin.execute("update ontology.objects set properties=properties||'{\"state\":\"x\"}' where object_id=%s", (goal,))
    assert edges(admin, source=goal) == []
    put(admin, 'Goal', hexid('v-goal-2'), {'consumer_id': consumer})
    assert edges(admin, source=hexid('v-goal-2')) == []


def test_derivation_leaves_governed_links_and_is_scoped(catalog):  # noqa: F811
    """I-2: only relations marked derived are dropped by derivation. Plus: an object inserted and deleted in one transaction leaves
    nothing (S-7); a replica write derives nothing; a target in another world is not a target."""
    c = catalog
    admin = c['admin']
    publish(c)
    a, b, goal = hexid('l-a'), hexid('l-b'), hexid('l-goal')
    put(admin, 'Consumer', a, {}, replica=True)
    put(admin, 'Consumer', b, {}, replica=True)
    put(admin, 'Goal', goal, {'consumer_id': a})
    assert admin.execute("select metadata from ontology.relations where source_object_id=%s", (goal,)).fetchone() == ({'derived': True},)
    # A relation of the same name to b written through the link entry (stand-in: not marked derived) survives property changes.
    admin.execute("""insert into ontology.relations(relation_id,tenant_id,world,relation_name,schema_version,source_type,source_object_id,target_type,target_object_id,metadata,created_at)
        values(%s,%s,'real','goal_of',1,'Goal',%s,'Consumer',%s,'{}',clock_timestamp())""", (hexid('linked'), TENANT_A, goal, b))
    admin.execute("update ontology.objects set properties=properties-'consumer_id' where object_id=%s", (goal,))
    assert edges(admin, source=goal) == [(goal, b, 'real')]
    with admin.transaction():
        put(admin, 'Goal', hexid('s7'), {'consumer_id': a})
        admin.execute("delete from ontology.objects where object_id=%s", (hexid('s7'),))
    assert edges(admin, source=hexid('s7')) == []
    put(admin, 'Goal', hexid('replica-goal'), {'consumer_id': a}, replica=True)
    assert edges(admin, source=hexid('replica-goal')) == []
    from test_nx041_sim_worlds_pg import create
    world = create(admin)['world']
    put(admin, 'Goal', hexid('other-world-goal'), {'consumer_id': a}, world=world)  # a is a real-world Consumer
    assert edges(admin, source=hexid('other-world-goal')) == [] and dangling(admin) == {f'{world}:goal_of': 1}
