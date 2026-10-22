"""NX-054 P1 (0318, nexloop_eios.relation_catalog; ADR-038 adopted 2026-10-22): the first batch of relation types is published as
trusted configuration only; every ownership chain reaches Consumer without a cycle and the published longest chain is the computed
one (C-1); every ownership-marked type resolves to its Consumer through relations (the structural test: a new marked type without
a working chain fails here); no role but the owner writes relations or relation types (S-1); a relation never crosses worlds.
Admin seeds object types and synthetic objects (configuration and data stand-ins) and probes; nothing derives relations in P1."""
import copy

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from nexloop_eios import relation_catalog as R
from nexloop_eios.bootstrap import bootstrap
from support.tenants import TENANT_A

TABLES = ('ontology.relations', 'ontology.relation_type_versions', 'control.nexloop_relation_type_profiles', 'control.nexloop_relation_catalogs')


def hexid(label):
    import hashlib
    return hashlib.sha256(label.encode()).hexdigest()


@pytest.fixture
def catalog(admin, pg):
    bootstrap(admin)
    admin.execute('alter role nexloop_configurator login')  # the deployment's technical configurator logs in
    admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (TENANT_A,))
    types = sorted({t for s in R.FIRST_BATCH for t in (s.source_type, s.target_type)})
    for t in types:
        admin.execute("""insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,%s,1,%s)
            on conflict do nothing""", (TENANT_A, t, Jsonb({'type_name': t, 'version': 1, 'properties': []})))
    return dict(admin=admin, pg=pg, configurator=lambda: psycopg.connect(make_conninfo(pg, user='nexloop_configurator'), autocommit=True))


def publish(c, value=None):
    with c['configurator']() as db:
        return R.publish(db, TENANT_A, value)


def test_the_first_batch_publishes_once_and_replays(catalog):
    c = catalog
    got = publish(c)
    assert got == {'published': True, 'replayed': False, 'new_relation_types': 26, 'relation_types': 26, 'longest_ownership_chain': 3}
    assert publish(c) == {**got, 'replayed': True, 'new_relation_types': 0}
    admin = c['admin']
    rows = admin.execute("select relation_name,definition->>'cardinality' from ontology.relation_type_versions where tenant_id=%s", (TENANT_A,)).fetchall()
    assert len(rows) == 26 and {card for _, card in rows} == {'many_to_one'}
    marked = dict(admin.execute("select relation_name,consumer_ownership from control.nexloop_relation_type_profiles where consumer_ownership is not null").fetchall())
    assert marked == {s.name: s.ownership for s in R.FIRST_BATCH if s.ownership}
    assert admin.execute("select longest_ownership_chain from control.nexloop_relation_catalogs").fetchall() == [(3,)]


def test_publication_refuses_what_it_must(catalog):
    c = catalog
    good = R.manifest()
    with pytest.raises(psycopg.errors.InsufficientPrivilege), psycopg.connect(make_conninfo(c['pg'], user='nexloop_domain_worker'), autocommit=True) as db:
        R.publish(db, TENANT_A, good)
    wrong_length = {**good, 'longest_ownership_chain': 2}
    cases = {'declared longest ownership chain': wrong_length}
    # Two transitive relations pointing at each other: a cycle.
    cyc = copy.deepcopy(good)
    for r in cyc['relation_types']:
        if r['definition']['relation_name'] == 'conversation_of':
            r['definition']['target_type'], r['consumer_ownership'] = 'Message', 'transitive'
    cases['has a cycle'] = cyc
    # An ownership chain that ends nowhere: message_in alone.
    lone = {**good, 'relation_types': [r for r in good['relation_types'] if r['definition']['relation_name'] != 'conversation_of']}
    cases['does not reach Consumer'] = lone
    # Two ownership relations from one type.
    two = copy.deepcopy(good)
    two['relation_types'].append({'definition': {**R.definition(R.FIRST_BATCH[0]).model_dump(mode='json'), 'relation_name': 'goal_of_again'},
                                  'source_property': 'consumer_id', 'consumer_ownership': 'target'})
    cases['two ownership relations'] = two
    # A typed relation payload and an unknown key are not first-batch shapes.
    props = copy.deepcopy(good)
    props['relation_types'][0]['definition']['properties'] = [{'property_name': 'note', 'value_type': 'string'}]
    cases['relation type invalid'] = props
    for text, value in cases.items():
        with pytest.raises(psycopg.errors.InvalidParameterValue, match=text):
            publish(c, value)
    assert c['admin'].execute('select count(*) from ontology.relation_type_versions').fetchone() == (0,)
    # Published versions are immutable: other content under the same version is refused.
    publish(c)
    changed = copy.deepcopy(good)
    changed['relation_types'][0]['definition']['description'] = 'changed'
    with pytest.raises(psycopg.errors.SerializationFailure, match='published with other content'):
        publish(c, changed)


def test_the_python_catalog_makes_the_same_checks():
    assert R.ownership_chains()['assessment_evidence'] == 3 and max(R.ownership_chains().values()) == 3
    with pytest.raises(R.RelationCatalogInvalid, match='does not reach Consumer'):
        R.ownership_chains([s for s in R.FIRST_BATCH if s.name != 'conversation_of'])
    with pytest.raises(R.RelationCatalogInvalid, match='cycle'):
        R.ownership_chains([R.RelationSpec('a', 'X', 'Y', 'y', 'transitive'), R.RelationSpec('b', 'Y', 'X', 'x', 'transitive')])
    with pytest.raises(R.RelationCatalogInvalid, match='two ownership'):
        R.ownership_chains([*R.FIRST_BATCH, R.RelationSpec('goal_again', 'Goal', 'Consumer', 'consumer_id', 'target')])


def chain(admin, start_type, label):
    """Synthetic objects along the ownership path of start_type and the relations between them; returns (start id, consumer id)."""
    path = R.ownership_path(start_type)
    ids = [hexid(f'{label}-{start_type}-{i}') for i in range(len(path) + 1)]
    types = [start_type] + [s.target_type for s in path]
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')  # the formal objects' creation guards: synthetic stand-ins
        for t, i in zip(types, ids):
            admin.execute("""insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
                values(%s,'real',%s,%s,1,'{}','synthetic',%s,clock_timestamp(),clock_timestamp()) on conflict do nothing""", (TENANT_A, t, i, label))
    for s, src, dst in zip(path, ids, ids[1:]):  # the world guard runs (no replica role here)
        admin.execute("""insert into ontology.relations(relation_id,tenant_id,world,relation_name,schema_version,source_type,source_object_id,target_type,target_object_id,created_at)
            values(%s,%s,'real',%s,1,%s,%s,%s,%s,clock_timestamp())""", (hexid(f'{s.name}-{src}'), TENANT_A, s.name, s.source_type, src, s.target_type, dst))
    return ids[0], ids[-1], len(path)


def test_every_ownership_type_resolves_to_its_consumer_through_relations(catalog):
    c = catalog
    admin = c['admin']
    publish(c)
    longest = 0
    for s in R.FIRST_BATCH:
        if not s.ownership:
            continue
        start, consumer, hops = chain(admin, s.source_type, 'p1')
        got = admin.execute("select runtime.nexloop_relation_object_consumers(%s,'real',%s,%s)", (TENANT_A, s.source_type, start)).fetchone()[0]
        assert got == [consumer], s.name
        assert hops == R.ownership_chains()[s.name]
        longest = max(longest, hops)
    assert longest == admin.execute('select longest_ownership_chain from control.nexloop_relation_catalogs').fetchone()[0] == 3
    # A link that is not an ownership relation is not followed; an object with no relation belongs to no one; a Consumer to itself.
    goal = hexid('p1-Goal-0')
    assert admin.execute("select runtime.nexloop_relation_object_consumers(%s,'real','Goal',%s)", (TENANT_A, goal)).fetchone()[0] == [hexid('p1-Goal-1')]
    assert admin.execute("select runtime.nexloop_relation_object_consumers(%s,'real','Goal',%s)", (TENANT_A, hexid('nobody'))).fetchone()[0] == []
    assert admin.execute("select runtime.nexloop_relation_object_consumers(%s,'real','Consumer',%s)", (TENANT_A, 'c' * 64)).fetchone()[0] == ['c' * 64]


def test_a_relation_never_crosses_worlds(catalog):
    c = catalog
    admin = c['admin']
    publish(c)
    start, consumer, _ = chain(admin, 'Goal', 'world')
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='outside the relation'):
        admin.execute("""insert into ontology.relations(relation_id,tenant_id,world,relation_name,schema_version,source_type,source_object_id,target_type,target_object_id,created_at)
            values(%s,%s,'test','goal_of',1,'Goal',%s,'Consumer',%s,clock_timestamp())""", ('x' * 64, TENANT_A, start, consumer))


def test_no_role_but_the_owner_writes_relations_or_relation_types(catalog):
    admin = catalog['admin']
    roles = [r for (r,) in admin.execute("select rolname from pg_roles where not rolsuper and rolname not like 'pg\\_%' and rolname<>'nexloop_owner'")]
    assert 'nexloop_api' in roles and 'nexloop_configurator' in roles
    for table in TABLES:
        for role in roles:
            for privilege in ('INSERT', 'UPDATE', 'DELETE'):
                assert admin.execute('select has_table_privilege(%s,%s,%s)', (role, table, privilege)).fetchone() == (False,), (role, table, privilege)
