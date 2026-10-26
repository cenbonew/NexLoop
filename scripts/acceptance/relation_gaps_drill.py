#!/usr/bin/env python3
"""NX-054 P4a alarm drill (docs/implementation/NX-054-stage-publish.md §9.2 steps 1 and 4): make one relation read gap in the
**test** world, and take it away again. Only for this drill.

    <bootstrap DSN on stdin> | python -I -B relation_gaps_drill.py arm   --tenant <TENANT_ID> --dsn-stdin
    <bootstrap DSN on stdin> | python -I -B relation_gaps_drill.py check --tenant <TENANT_ID> --dsn-stdin
    <bootstrap DSN on stdin> | python -I -B relation_gaps_drill.py clear --tenant <TENANT_ID> --dsn-stdin

Who and where: the dispatcher, on DATA_HOST, through the bootstrap window (the stage database's bootstrap role over the host's
socket, the run sheet's §0 boot.dsn; the password piped from the owner's Keychain, never on the command line), with an interpreter
that has psycopg (the host-health venv: /opt/nexloop/host-health/current/venv/bin/python). Nothing else runs in that window.

Why it writes the object store directly, bypassing the governed entries (AGENTS.md: only governed Actions mutate formal business
objects) - for this drill only:
  - a gap is by definition a state the governed paths never leave behind (a write through an Action derives its relation at
    commit), so it cannot be made through them;
  - everything it writes is synthetic and in the test world only: one Consumer and one Goal, both with source_system 'drill-relgaps'
    and source_ref 'drill-relgaps-consumer' / 'drill-relgaps-goal', ids that are the sha256 of those refs (the relation rules need
    64-hex ids); each mode's first statement asserts world = 'test', and the real world is never read or written;
  - clear deletes only by that source_system in the test world (the BEFORE DELETE trigger takes the Goal's relations with it).
Side effect (L6 S4): the work feed triggers on ontology.objects write recall-instance items in the test world too - two upserts on
arm (the Consumer and the Goal) and two deletes on clear; nothing in the real world.
Each mode is one transaction. arm writes the two objects, fires the deferred relation derivation at once (SET CONSTRAINTS ...
IMMEDIATE) and deletes the Goal's goal_of: the gap check then counts one gap (world test, type Goal). check is read only: the drill
Goal without goal_of (1 after arm, 0 after clear). Counts and the fixed refs only are printed; no DSN.
Exit 0 done, 1 the expected state is not there, 2 refused.
"""
import argparse
import hashlib
import json
import re
import sys

class Refused(ValueError):
    pass


WORLD = 'test'
SOURCE = 'drill-relgaps'
CONSUMER_REF, GOAL_REF = 'drill-relgaps-consumer', 'drill-relgaps-goal'
CONSUMER, GOAL = (hashlib.sha256(r.encode()).hexdigest() for r in (CONSUMER_REF, GOAL_REF))


def assert_test_world(db, world=WORLD):
    if db.execute("select %s='test' and runtime.nexloop_world_kind(%s)='test'", (world, world)).fetchone()[0] is not True:
        raise Refused('the drill runs in the test world only')


def gap_count(db, tenant):
    """The drill Goal whose consumer_id names the drill Consumer and has no goal_of to it: the gap the check counts."""
    return db.execute("""select count(*) from ontology.objects g where g.tenant_id=%s and g.world=%s and g.type_name='Goal' and g.source_system=%s
        and g.properties->>'consumer_id'=%s and exists(select 1 from ontology.objects c where c.tenant_id=g.tenant_id and c.world=g.world
          and c.type_name='Consumer' and c.object_id=%s)
        and not exists(select 1 from ontology.relations r where r.tenant_id=g.tenant_id and r.world=g.world and r.relation_name='goal_of'
          and r.source_type='Goal' and r.source_object_id=g.object_id and r.target_object_id=%s)""",
                      (tenant, WORLD, SOURCE, CONSUMER, CONSUMER, CONSUMER)).fetchone()[0]


def arm(db, tenant):
    with db.transaction():
        assert_test_world(db)
        db.execute("select set_config('eios.tenant_id',%s,true)", (tenant,))
        if db.execute('select count(*) from ontology.objects where tenant_id=%s and world=%s and source_system=%s', (tenant, WORLD, SOURCE)).fetchone()[0]:
            raise Refused('drill objects already there: clear first')
        for type_name, object_id, ref, props in (('Consumer', CONSUMER, CONSUMER_REF, {}), ('Goal', GOAL, GOAL_REF, {'consumer_id': CONSUMER})):
            db.execute("""insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
                values(%s,%s,%s,%s,1,%s::jsonb,%s,%s,clock_timestamp(),clock_timestamp())""", (tenant, WORLD, type_name, object_id, json.dumps(props), SOURCE, ref))
        db.execute('set constraints ontology.nexloop_relation_derive immediate')  # derive goal_of now, then take it away
        db.execute("""delete from ontology.relations where tenant_id=%s and world=%s and relation_name='goal_of' and source_type='Goal' and source_object_id=%s""",
                   (tenant, WORLD, GOAL))
    return gap_count(db, tenant)


def clear(db, tenant):
    with db.transaction():
        assert_test_world(db)
        db.execute("select set_config('eios.tenant_id',%s,true)", (tenant,))
        deleted = db.execute('delete from ontology.objects where tenant_id=%s and world=%s and source_system=%s and object_id in (%s,%s) returning object_id',
                             (tenant, WORLD, SOURCE, CONSUMER, GOAL)).fetchall()
    return len(deleted)


def main(argv=None, *, connect=None, stdin=sys.stdin, out=sys.stdout):
    p = argparse.ArgumentParser(description='NX-054 relation read gap drill (test world, synthetic, bootstrap window)')
    p.add_argument('mode', choices=['arm', 'check', 'clear'])
    p.add_argument('--tenant', required=True)
    p.add_argument('--dsn-stdin', action='store_true', required=True)
    a = p.parse_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', a.tenant):
        print(json.dumps({'refused': '--tenant: a tenant id'}), file=out)
        return 2
    if connect is None:
        import psycopg
        dsn = stdin.read(8192).strip()
        connect = lambda: psycopg.connect(dsn, autocommit=True, connect_timeout=10)  # noqa: E731
    try:
        return run(a, connect, out)
    except Refused as error:
        print(json.dumps({'refused': str(error)}), file=out)
        return 2


def run(a, connect, out):
    with connect() as db:
        db.execute("set statement_timeout='30s'")
        db.execute("set lock_timeout='3s'")
        if a.mode == 'arm':
            n = arm(db, a.tenant)
            print(json.dumps({'mode': 'arm', 'world': WORLD, 'drill_gaps': n}), file=out)
            return 0 if n == 1 else 1
        if a.mode == 'check':
            with db.transaction():
                db.execute('set transaction read only')
                assert_test_world(db)
                n = gap_count(db, a.tenant)
            print(json.dumps({'mode': 'check', 'world': WORLD, 'drill_gaps': n}), file=out)
            return 0
        n = clear(db, a.tenant)
        left = gap_count(db, a.tenant)
        print(json.dumps({'mode': 'clear', 'world': WORLD, 'objects_deleted': n, 'drill_gaps': left}), file=out)
        return 0 if left == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
