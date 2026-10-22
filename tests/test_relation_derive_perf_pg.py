"""NX-054 P2 performance against the P1 baseline (docs/implementation/NX-054-P1.md §4), locally: the two bulk paths the derivation
trigger rides on - generating many objects (the NX-041 sim generation's shape: Goals and PlanSteps naming their Consumer and
Goal) and pseudonymizing their reference properties in one statement (the erasure's 0305-0307 shape). Three modes on the same
data: no derivation trigger (P1), trigger without published relation types, trigger with the first batch published. The timings
are printed (run with -s) and recorded in docs/implementation/NX-054-P2.md; the assertion is a generous bound that catches a
derivation gone quadratic, not the 25 % acceptance figure (that is measured on stage)."""
import hashlib
import time

from psycopg.types.json import Jsonb

from support.tenants import TENANT_A
from test_relation_types_p1_pg import catalog, publish  # noqa: F401

N = 1000


def hexid(label):
    return hashlib.sha256(label.encode()).hexdigest()


def run(admin, label):
    consumers = [hexid(f'{label}-c-{i}') for i in range(N // 10)]
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        for cid in consumers:
            admin.execute("""insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
                values(%s,'real','Consumer',%s,1,'{}','synthetic',%s,clock_timestamp(),clock_timestamp())""", (TENANT_A, cid, label))
    started = time.perf_counter()
    with admin.transaction():  # generation: one transaction, N Goals and N PlanSteps (the trigger runs at its commit)
        for i in range(N):
            g, s, c = hexid(f'{label}-g-{i}'), hexid(f'{label}-s-{i}'), consumers[i % len(consumers)]
            admin.execute("""insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
                values(%s,'real','Goal',%s,1,%s,'synthetic',%s,clock_timestamp(),clock_timestamp()),
                      (%s,'real','PlanStep',%s,1,%s,'synthetic',%s,clock_timestamp(),clock_timestamp())""",
                          (TENANT_A, g, Jsonb({'consumer_id': c}), label, TENANT_A, s, Jsonb({'consumer_id': c, 'goal_id': g}), label))
    generated = time.perf_counter() - started
    started = time.perf_counter()
    with admin.transaction():  # the erasure's shape: one statement pseudonymizes every reference
        admin.execute("""update ontology.objects set properties=properties||jsonb_build_object('consumer_id','pseudonym:'||repeat('a',32))
            where tenant_id=%s and type_name in ('Goal','PlanStep') and source_ref=%s""", (TENANT_A, label))
    pseudonymized = time.perf_counter() - started
    return generated, pseudonymized


def test_derivation_cost_against_the_p1_baseline(catalog, capsys):  # noqa: F811 - imported fixtures
    c = catalog
    admin = c['admin']
    admin.execute('alter table ontology.objects disable trigger nexloop_relation_derive')
    base = run(admin, 'p1')
    admin.execute('alter table ontology.objects enable trigger nexloop_relation_derive')
    idle = run(admin, 'idle')
    publish(c)
    derived = run(admin, 'p2')
    assert admin.execute("select count(*) from ontology.relations where relation_name in ('goal_of','plan_step_of','step_in_goal')").fetchone()[0] == N  # step_in_goal stays after pseudonymizing
    with capsys.disabled():
        print(f'\nNX-054 P2 perf (N={N} Goals + {N} PlanSteps): generation p1 {base[0]:.3f}s, trigger idle {idle[0]:.3f}s, derived {derived[0]:.3f}s; '
              f'pseudonymize p1 {base[1]:.3f}s, idle {idle[1]:.3f}s, derived {derived[1]:.3f}s')
    assert derived[0] < base[0] * 4 + 1 and derived[1] < base[1] * 6 + 1
