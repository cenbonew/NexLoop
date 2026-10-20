"""NX-041 S3 (design D8, §2.5, §11 S3): synthetic data and structural generation in a registered simulation world.

Synthetic output comes from parameters and distributions only; structural output takes object types and properties from the
tenant's published definitions and per-type counts of real objects, never a real value, identifier or message. Output is marked
synthetic, lives only in the simulation world (real, test and shadow are refused), never reaches Claims or ontology.objects, is
reproducible from seed and parameters (generator version, parameter digest and definitions digest recorded) and leaves with the
S2 whole deletion while real rows stay untouched.

Real PostgreSQL on a clean catalog. Admin calls the SQL handler the governed entry calls (as test_nx041_sim_worlds_pg) and seeds
definitions and real rows as trusted configuration does; all data is synthetic.
"""
import json
import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from nexloop_eios.bootstrap import bootstrap
from support.tenants import TENANT_A, TENANT_B
from test_nx041_sim_worlds_pg import call, create, delete

ROOT = __import__('pathlib').Path(__file__).resolve().parents[1]
MARK = 'zz-real-content-marker'  # real content that must never reach generated data
GENERATED = ('control.nexloop_sim_generations', 'runtime.nexloop_sim_objects', 'runtime.nexloop_sim_session_events', 'control.nexloop_consumer_personas')


@pytest.fixture
def db(admin):
    bootstrap(admin)
    for t in (TENANT_A, TENANT_B):
        admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (t,))
    return admin


def synthetic(world, seed='s1', request=None, consumers=12, segments=None):
    return {'operation': 'generate_sim_world_data', 'request_id': request or uuid.uuid4().hex, 'world': world, 'kind': 'synthetic', 'seed': seed,
            'consumers': consumers, 'events_per_consumer': {'min': 0, 'max': 6}, 'segments': segments or {'loyal': 3, 'new': 1}}


def structural(world, seed='s1', request=None, consumers=50, max_objects=8):
    return {'operation': 'generate_sim_world_data', 'request_id': request or uuid.uuid4().hex, 'world': world, 'kind': 'structural', 'seed': seed,
            'consumers': consumers, 'events_per_consumer': {'min': 1, 'max': 3}, 'max_objects': max_objects}


def generate(admin, body, tenant=TENANT_A, world='real'):
    return call(admin, 'nexloop_governed_generate_sim_world_data', body, tenant, world)


def seed_real(admin, tenant=TENANT_A):
    """A published type with three properties and real objects carrying recognisable content (Consumers too)."""
    definition = {'type_name': 'ServiceTicket', 'properties': [
        {'property_name': 'title', 'value_type': 'string'}, {'property_name': 'priority', 'value_type': 'integer'},
        {'property_name': 'urgent', 'value_type': 'boolean'}]}
    ids = []
    with admin.transaction():
        admin.execute("insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,'ServiceTicket',1,%s)",
                      (tenant, Jsonb(definition)))
        admin.execute('set local session_replication_role=replica')
        for type_name, n in (('ServiceTicket', 6), ('Consumer', 4)):
            for _ in range(n):
                oid = uuid.uuid4().hex + uuid.uuid4().hex
                ids.append(oid)
                admin.execute('''insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
                    values(%s,'real',%s,%s,1,%s,'test','synthetic',now(),now())''', (tenant, type_name, oid, Jsonb({'title': MARK, 'priority': 7, 'name': MARK})))
    return ids


def rows(admin, world, tenant=TENANT_A):
    return {t: admin.execute(f'select count(*) from {t} where world=%s and tenant_id=%s', (world, tenant)).fetchone()[0] for t in GENERATED}


def content(admin, world):
    return json.dumps([admin.execute(f'select coalesce(jsonb_agg(to_jsonb(x)),\'[]\') from {t} x where world=%s', (world,)).fetchone()[0] for t in GENERATED])


def test_synthetic_generation_is_reproducible_marked_and_recorded(db):
    a, b = create(db)['world'], create(db)['world']
    first = generate(db, synthetic(a))
    assert first['created'] is True and first['generator_version'] == 'nexloop-sim-generator/1' and first['definitions_digest'] is None
    assert first['counts']['Consumer'] == 12 and first['counts']['session_events'] >= 0
    # The same seed and parameters give the same output in another world; another seed does not.
    assert generate(db, synthetic(b))['output_digest'] == first['output_digest']
    assert generate(db, synthetic(b, seed='s2'))['output_digest'] != first['output_digest']
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='seed_used'), db.transaction():
        generate(db, synthetic(a, seed='s1'))
    # Every row is synthetic: personas, origin columns; nothing in ontology.objects, conversations or Claims of the world.
    assert db.execute("select array_agg(distinct persona) from control.nexloop_consumer_personas where world=%s", (a,)).fetchone()[0] == ['synthetic']
    assert db.execute("select bool_and(origin='synthetic') from runtime.nexloop_sim_objects where world=%s", (a,)).fetchone()[0] is True
    for table in ('ontology.objects', 'ontology.nexloop_claims', 'runtime.nexloop_conversation_messages'):
        assert db.execute(f'select count(*) from {table} where world=%s', (a,)).fetchone()[0] == 0, table
    # The frozen input record: version, seed, parameters and their digest.
    rec = db.execute('select generator_version,seed,params,params_digest,created_by from control.nexloop_sim_generations where world=%s and generation_id=%s',
                     (a, first['generation_id'])).fetchone()
    assert rec[0] == 'nexloop-sim-generator/1' and rec[1] == 's1' and rec[2]['segments'] == {'loyal': 3, 'new': 1} and rec[3] == first['params_digest']
    assert rec[4] == 'owner-principal'


def test_a_retried_request_replays_and_a_reused_request_is_refused(db):
    a = create(db)['world']; body = synthetic(a)
    first = generate(db, body)
    again = generate(db, body)
    assert (again['created'], again['generation_id'], again['output_digest']) == (False, first['generation_id'], first['output_digest'])
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='request_reused'), db.transaction():
        generate(db, {**body, 'consumers': 3})


def test_only_a_registered_active_simulation_world_of_the_tenant_receives_data(db):
    a = create(db)['world']; shadow = create(db, kind='shadow')['world']
    for world, code in (('real', 'not_a_simulation_world'), ('test', 'not_a_simulation_world'), (shadow, 'not_a_simulation_world'),
                        ('sim:' + str(uuid.uuid4()), 'not_registered')):
        with pytest.raises(psycopg.errors.InvalidParameterValue, match=code), db.transaction():
            generate(db, synthetic(world))
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='not_registered'), db.transaction():
        generate(db, synthetic(a), tenant=TENANT_B)  # another tenant's world
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='owner_world_only'), db.transaction():
        generate(db, synthetic(a), world='test')
    for bad in ({'consumers': 0}, {'consumers': 2001}, {'seed': 'a b'}, {'events_per_consumer': {'min': 3, 'max': 1}},
                {'events_per_consumer': {'min': 0, 'max': 51}}, {'segments': {}}, {'segments': {'x': 0}}, {'max_objects': 3}, {'extra': 1}):
        with pytest.raises(psycopg.errors.InvalidParameterValue, match='sim generation: invalid'), db.transaction():
            generate(db, {**synthetic(a), **bad})
    # Tables refuse any other world even for a direct write.
    for world in ('real', 'test', shadow):
        with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
            db.execute("""insert into control.nexloop_sim_generations(tenant_id,world,generation_id,request_id,kind,generator_version,seed,params,params_digest,
                counts,output_digest,created_by) values(%s,%s,%s,'r','synthetic','v','s','{}',%s,'{}',%s,'x')""", (TENANT_A, world, 'a' * 64, 'a' * 64, 'a' * 64))
    # A deleting world receives nothing new.
    delete(db, a)
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='not_registered|world_not_active'), db.transaction():
        generate(db, synthetic(a))


def test_structural_generation_copies_structure_and_counts_never_real_content(db):
    real_ids = seed_real(db)
    real_before = db.execute("select md5(string_agg(md5(x::text),',' order by object_id)) from ontology.objects x where world='real'").fetchone()[0]
    a, b = create(db)['world'], create(db)['world']
    out = generate(db, structural(a))
    # Scaled to at most max_objects: 10 real objects, max 8 -> 0.8; 6 tickets -> 5, 4 Consumers -> 3.
    assert out['counts']['ServiceTicket'] == 5 and out['counts']['Consumer'] == 3 and len(out['definitions_digest']) == 64
    props = [r[0] for r in db.execute("select properties from runtime.nexloop_sim_objects where world=%s and type_name='ServiceTicket'", (a,))]
    assert all(set(p) == {'title', 'priority', 'urgent'} and p['title'].startswith('syn-') and type(p['urgent']) is bool for p in props)
    text = content(db, a)
    assert MARK not in text and not any(i in text for i in real_ids)
    # Reproducible while the published definitions and the real counts stay the same.
    assert generate(db, structural(b))['output_digest'] == out['output_digest']
    assert db.execute("select md5(string_agg(md5(x::text),',' order by object_id)) from ontology.objects x where world='real'").fetchone()[0] == real_before


def test_whole_deletion_leaves_no_generated_row_and_real_untouched(db):
    seed_real(db)
    a, kept = create(db)['world'], create(db)['world']
    generate(db, synthetic(a)); generate(db, structural(a, seed='s9')); generate(db, synthetic(kept))
    real_before = {t: db.execute(f"select count(*) from {t} where world='real'").fetchone()[0] for t in GENERATED + ('ontology.objects',)}
    kept_before = rows(db, kept)
    assert all(n > 0 for t, n in rows(db, a).items() if t != 'control.nexloop_sim_generations') and rows(db, a)['control.nexloop_sim_generations'] == 2
    result = delete(db, a)
    for _ in range(20):
        if result['status'] == 'deleted':
            break
        result = db.execute('select runtime.nexloop_sim_world_purge(%s,%s,1000)', (TENANT_A, a)).fetchone()[0]
    assert result['status'] == 'deleted' and result['remaining'] == 0 and result['blocked'] == {}
    assert rows(db, a) == {t: 0 for t in GENERATED}
    assert {t: db.execute(f"select count(*) from {t} where world='real'").fetchone()[0] for t in GENERATED + ('ontology.objects',)} == real_before
    assert rows(db, kept) == kept_before


def test_the_action_is_owner_only_and_python_mirrors_sql(db):
    assert db.execute("select subject_rule from control.nexloop_governed_capabilities where capability='sim_world.generate'").fetchone() == ('human',)
    roles = json.loads((ROOT / 'deploy/authorization/workbench-roles.v1.json').read_text())['roles']
    assert sorted(r for r, grants in roles.items() if 'eios:action:nexloop.sim_world.generate:1' in grants) == ['owner']
    from nexloop_eios.business_actions import PROFILES
    from nexloop_eios.workbench_actions_http import OPERATIONS
    assert PROFILES['sim_world.generate'] == 'human_owner'
    ops = OPERATIONS['generate_sim_world_data']
    for name, bad in (('world', 'real'), ('world', 'shadow:' + str(uuid.uuid4())), ('kind', 'snapshot'), ('consumers', 0),
                      ('events_per_consumer', {'min': 0}), ('max_objects', 10001)):
        with pytest.raises(ValueError):
            ops[name](bad)
