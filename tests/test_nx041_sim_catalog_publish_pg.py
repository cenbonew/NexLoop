"""NX-041 D10 follow-up (0278): the owner's governed Action sim_world.publish_catalog / publish_sim_catalog.

Real PostgreSQL on a clean catalog; the handler is called as the governed entry calls it; synthetic tenant and data.
- The real Actions with a stand-in (S4) are copied into one registered, active simulation world: definitions and capabilities only;
  an effect without a stand-in (enterprise delivery) is never copied; no credential, authority fact or effect configuration moves.
- Repeatable (already_present); a different definition already in the world is a conflict; real, test, unregistered, deleted and
  another tenant's worlds are refused, and so is a call outside the owner's real world.
- In the world the effect can only take the stand-in path: a real effect table refuses a simulation row (0262 CHECK).
- The rows leave with the world (S2 whole deletion); the real side is unchanged throughout.
"""
import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from nexloop_eios.bootstrap import bootstrap
from support.tenants import TENANT_A, TENANT_B
from system_catalog_fixture import publish_system_catalog

SERVICE = 'eios:action:nexloop.service.request:1'
DELIVER = 'eios:action:nexloop.enterprise.deliver:1'


def call(admin, fn, body, tenant=TENANT_A, world='real'):
    return admin.execute(f"select control.{fn}(%s,%s,'owner-principal','owner-subject',%s,%s)",
                         (tenant, world, 'intent-' + uuid.uuid4().hex[:12], Jsonb(body))).fetchone()[0]


def publish(admin, world, tenant=TENANT_A, at='real'):
    return call(admin, 'nexloop_governed_publish_sim_catalog', {'operation': 'publish_sim_catalog', 'request_id': uuid.uuid4().hex, 'world': world,
                                                                'reason': '模拟世界的 Action 目录'}, tenant, at)


def new_world(admin, tenant=TENANT_A):
    return call(admin, 'nexloop_governed_create_sim_world', {'operation': 'create_sim_world', 'request_id': uuid.uuid4().hex, 'kind': 'simulation',
                                                             'purpose': '目录发布', 'expires_in_days': 7}, tenant)['world']


def refused(admin, reason, *args, **kw):
    with pytest.raises(psycopg.errors.InvalidParameterValue, match=f'sim world: {reason}'), admin.transaction():
        publish(admin, *args, **kw)
    return True


def real_side(admin):
    return (admin.execute("select md5(coalesce(string_agg(resource_id||definition::text||capability::text,'|' order by resource_id),'')) from control.nexloop_action_definitions where world='real'").fetchone()[0],
            admin.execute('select count(*) from authz.nexloop_service_credentials').fetchone()[0],
            admin.execute('select count(*) from authz.nexloop_authority_facts').fetchone()[0],
            admin.execute("select count(*) from control.nexloop_action_definitions where world='test'").fetchone()[0])


@pytest.fixture
def db(admin):
    bootstrap(admin)
    for t in (TENANT_A, TENANT_B):
        admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (t,))
    publish_system_catalog(admin, TENANT_A, ('Consumer', 'Goal', 'PlanStep', 'EffectControl'), ('nexloop.service.request', 'nexloop.enterprise.deliver'))
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT_A,))
    return admin


def in_world(admin, world):
    return dict(admin.execute('select resource_id,active from control.nexloop_action_definitions where tenant_id=%s and world=%s', (TENANT_A, world)).fetchall())


def test_the_stand_in_actions_are_copied_once_definitions_only_and_real_is_unchanged(db):
    admin = db
    world = new_world(admin)
    before = real_side(admin)
    out = publish(admin, world)
    assert (out['world'], out['actions'], out['copied'], out['already_present']) == (world, [SERVICE], 1, 0)
    assert in_world(admin, world) == {SERVICE: True}  # enterprise delivery has no stand-in: never copied (D11)
    copied, real = (admin.execute('select definition,capability from control.nexloop_action_definitions where world=%s and resource_id=%s', (w, SERVICE)).fetchone()
                    for w in (world, 'real'))
    assert copied == real
    assert real_side(admin) == before  # no real definition, credential or authority fact changed
    assert admin.execute("select count(*) from runtime.audit_events where action='nexloop.sim_world.catalog_published'").fetchone()[0] == 1
    # Repeatable: nothing written again.
    again = publish(admin, world)
    assert (again['copied'], again['already_present']) == (0, 1) and in_world(admin, world) == {SERVICE: True}
    assert admin.execute("select count(*) from runtime.audit_events where action='nexloop.sim_world.catalog_published'").fetchone()[0] == 1


def test_refusals_and_conflicts(db):
    admin = db
    world = new_world(admin)
    for target in ('real', 'test', 'simulation', 'sim:' + str(uuid.uuid4())):
        assert refused(admin, 'not_a_simulation_world' if not target.startswith('sim:') else 'not_active', target)
    assert refused(admin, 'owner_world_only', world, at='test')
    other = new_world(admin, TENANT_B)
    assert refused(admin, 'not_active', other)  # another tenant's world is not this tenant's registered world
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='sim world: invalid'), admin.transaction():
        call(admin, 'nexloop_governed_publish_sim_catalog', {'operation': 'publish_sim_catalog', 'request_id': 'r', 'world': world, 'reason': ' '})
    # A different definition of the same version already in the world (seeded as trusted configuration): a conflict, nothing written.
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("""insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability,active)
            select tenant_id,%s,resource_id,jsonb_set(definition,'{description}','"changed"'),capability,true from control.nexloop_action_definitions
            where tenant_id=%s and world='real' and resource_id=%s""", (world, TENANT_A, SERVICE))
    assert refused(admin, 'catalog_conflict', world)
    # A deleted world is refused.
    gone = new_world(admin)
    call(admin, 'nexloop_governed_delete_sim_world', {'operation': 'delete_sim_world', 'request_id': uuid.uuid4().hex, 'world': gone, 'reason': '结束'})
    assert refused(admin, 'not_active', gone)


def test_a_real_effect_table_refuses_a_simulation_row_so_only_the_stand_in_path_remains(db):
    """0262 nx041_real_effect_world: with the effect Action published in the world, a simulation effect still cannot reach the real
    executor's tables (negative), while the stand-in catalog serves it (the D10 Pi Run end to end: tests/test_nx041_sim_pi_run_pg.py)."""
    admin = db
    world = new_world(admin)
    publish(admin, world)
    tables = [r[0] for r in admin.execute("select table_name::text from control.nexloop_real_effect_tables where guarded").fetchall()]
    assert 'runtime.nexloop_effect_intents' in tables or any('effect_intents' in t for t in tables)
    with pytest.raises(psycopg.errors.CheckViolation, match='nx041_real_effect_world'), admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("""insert into runtime.nexloop_effect_intents(intent_id,receipt_id,context_id,tenant_id,world,consumer_id,goal_identity,plan_step_identity,slot_identity,
            action_name,action_version,business_digest,provider_payload_digest,frozen_request,action_definition,capability,executor_principal,control_revision,created_at,state,governed_claim_finalized)
            values(gen_random_uuid(),gen_random_uuid(),gen_random_uuid(),%s,%s,%s,'g','s','slot','nexloop.service.request',1,%s,%s,'{}','{}','{}','x',1,clock_timestamp(),'accepted',false)""",
                      (TENANT_A, world, 'c' * 64, 'a' * 64, 'b' * 64))
    assert admin.execute("select count(*) from control.nexloop_stand_ins where action_name='nexloop.service.request'").fetchone()[0] >= 1


def test_the_catalog_leaves_with_the_world(db):
    admin = db
    world = new_world(admin)
    publish(admin, world)
    out = call(admin, 'nexloop_governed_delete_sim_world', {'operation': 'delete_sim_world', 'request_id': uuid.uuid4().hex, 'world': world, 'reason': '结束'})
    assert out['status'] == 'deleted', out
    assert in_world(admin, world) == {}


def test_a_simulation_definition_does_not_advance_the_authority_revision_and_a_real_one_does(db):
    """0278 §5: the governed entry re-asserts the tenant's authority revision at its commit tail; a simulation world's definitions no
    longer advance it (they serve that world's credentials only), every other authority row still does."""
    admin = db
    world = new_world(admin)
    revision = lambda: admin.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s', (TENANT_A,)).fetchone()[0]  # noqa: E731
    before = revision()
    publish(admin, world)
    assert revision() == before
    with admin.transaction():  # a real definition (trusted configuration's write) still advances it
        admin.execute('set local role nexloop_owner')
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT_A,))
        admin.execute("""insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability,active)
            select tenant_id,'test',resource_id,definition,capability,true from control.nexloop_action_definitions where tenant_id=%s and world='real' and resource_id=%s""",
                      (TENANT_A, SERVICE))
    assert revision() == before + 1


WRITE = r'(insert\s+into|update|delete\s+from)\s+control\.nexloop_action_definitions\M'


def test_a_simulation_definition_has_two_write_paths_only(db):
    """L6 sim-catalog-publish review 1.3: a simulation world's definitions do not advance the authority revision, which is safe only while
    they are written by the catalog publication (insert) and leave with the world (S2 whole deletion, after its credentials are revoked).
    Every other function writing the table writes world 'real' only, and nothing updates the table at all (no deactivation path). A new
    writer turns this red: it must then get a per-world revision (review §5) before it may touch a simulation world."""
    admin = db
    writers = dict(admin.execute(r"""select p.oid::regprocedure::text, p.prosrc from pg_proc p join pg_namespace n on n.oid=p.pronamespace
        where n.nspname not in ('pg_catalog','information_schema') and p.prosrc ~* %s""", (WRITE,)).fetchall())
    assert sorted(writers) == ['authz.nexloop_review_decide_v0082(text,text,text,text,text)', 'authz.nexloop_schema_proposal_publish(text,text,text,text,text)',
                               'control.nexloop_configure_manifest(text,text,text,text,bytea,jsonb)',
                               'control.nexloop_governed_publish_sim_catalog(text,text,text,text,text,jsonb)']
    import re
    for name, source in writers.items():
        statements = [m.group(0) for m in re.finditer(WRITE.replace(r'\M', r'\b') + r'[^;]*', source, re.I)]
        assert statements and not any(s.lower().startswith('update') for s in statements), name  # no deactivation of any definition
        if 'publish_sim_catalog' not in name:
            assert all("'real'" in s for s in statements), (name, statements)
    # The S2 deletion is the only dynamic writer of world rows (format over every table with a world column).
    dynamic = [r[0] for r in admin.execute(r"""select p.oid::regprocedure::text from pg_proc p join pg_namespace n on n.oid=p.pronamespace
        where n.nspname not in ('pg_catalog','information_schema') and p.prosrc ~* %s""", (r"execute\s+format\('delete\s+from\s+%s",)).fetchall()]
    assert len(dynamic) == 1 and 'sim_world' in dynamic[0], dynamic
    assert admin.execute("select count(*) from pg_trigger where tgrelid='control.nexloop_action_definitions'::regclass and not tgisinternal").fetchone()[0] >= 1
