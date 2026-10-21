"""NX-042 T7 (design §4, §5, §6): the complete AT-046 acceptance with the consumer twin, and the three acceptances §6 proposes.

1. End to end: a simulation world with synthetic Consumers (S3) -> a published twin version bound on the consumer port (T3) -> steps of
   observe/act -> the simulation process group runs one actual Pi Run from the world's sim context, which carries the twin's events
   (NX-041 D10's chain, test_nx041_sim_pi_run_pg.run_sim_pi_run). No real credential or channel; the real data does not change.
2. §6 proposed acceptances: (a) twin output writes nothing to real - the digest of every real row is unchanged; (b) calibration
   independence - on T5's calibration run (integration: no longer a strict xfail); (c) no disguised probability - an uncalibrated twin's
   outputs say so and are never selectable (the report part pending T5, NX-043's refusal pending NX-043).
3. §5: twin events and calibration conclusions are never facts or Claims.
Real PostgreSQL; Host and real Pi under Node 24, serially.
"""
import copy
import json
import uuid

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from nexloop_eios import twin_port as T
from authority_fixture import seed_authority
from support.tenants import TENANT_A
from test_nx041_s7_pg import consumers, digest, governed, s7  # noqa: F401
from test_nx041_sim_pi_run_pg import real_counts, run_sim_pi_run, sim_group  # noqa: F401
from test_nx042_consumer_twins_pg import DEFINITION
from test_nx042_twin_calibration_read_pg import db  # noqa: F401  (T5 calibration fixture)

TWIN = DEFINITION['twin_id']
SCHEMAS = ('authz', 'control', 'runtime', 'ontology', 'public')


def publish(admin, tenant=TENANT_A, definition=None):
    return admin.execute("select control.nexloop_governed_publish_consumer_twin(%s,'real','owner-principal','owner-subject',%s,%s)",
                         (tenant, 'intent-' + uuid.uuid4().hex[:12], Jsonb({'operation': 'publish_consumer_twin', 'request_id': uuid.uuid4().hex,
                                                                           'definition': definition or DEFINITION}))).fetchone()[0]


def real_digest(admin):
    """Per table, md5 of its real rows ordered by row text: a table with a world column by that column; a durable-core table without
    one but with a job_id (runtime.invocations, runtime.job_events) by its job's world (runtime.jobs carries it); any other table whole."""
    tables = admin.execute("""select n.nspname||'.'||c.relname,
          exists(select 1 from pg_attribute a where a.attrelid=c.oid and a.attname='world' and not a.attisdropped),
          exists(select 1 from pg_attribute a where a.attrelid=c.oid and a.attname='job_id' and not a.attisdropped)
        from pg_class c join pg_namespace n on n.oid=c.relnamespace where c.relkind in ('r','p') and n.nspname = any(%s) order by 1""", (list(SCHEMAS),)).fetchall()
    out = {}
    for table, has_world, has_job in tables:
        if has_world:
            where = " where t.world='real'"
        elif has_job:
            where = " where not exists(select 1 from runtime.jobs j where j.tenant_id=t.tenant_id and j.job_id=t.job_id and j.world<>'real')"
        else:
            where = ''
        out[table] = admin.execute(f'select md5(coalesce(string_agg(t::text,chr(10) order by t::text),\'\')) from {table} t{where}').fetchone()[0]
    return out


def changed(before, after):
    return sorted(t for t in after if before.get(t) != after[t])


@pytest.fixture
def t7(s7):
    publish(s7['admin'])
    return s7


def test_acceptance_twin_output_writes_nothing_to_real(t7):
    """§6 (a): running a twin in a simulation world (bind, observe, act, sampling) leaves every real row as it was."""
    f = t7
    admin = f['admin']
    before = real_digest(admin)
    acts = T.TwinPort(f['twin'], f['token_twin_a'], f['a']).run(T.ConsumerTwin(TWIN, 1, 'zero-real'), consumers(admin, f['a']), steps=3)
    assert any(a['event_id'] for a in acts)
    T_SAMPLE = 'select authz.nexloop_twin_sample(%s,%s,%s,1,%s,%s)'
    with f['role']('nexloop_sim_api')() as db:
        assert db.execute(T_SAMPLE, (digest(f['token_a']), f['a'], TWIN, 'zero-real', 20)).fetchone()[0]['instances'] == 20
    assert changed(before, real_digest(admin)) == []
    # Positive control: the digest sees a real change (rolled back).
    with admin.transaction(force_rollback=True):
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT_A,))
        admin.execute("insert into control.nexloop_consumer_personas(tenant_id,world,consumer_id,persona,revision,updated_by) values(%s,'real',%s,'synthetic',1,'probe')",
                      (TENANT_A, 'f' * 64))
        assert changed(before, real_digest(admin)) == ['control.nexloop_consumer_personas']


def test_acceptance_calibration_windows_are_independent_and_grouped_by_consumer(db):
    """§6 (b), on T5's calibration run (the governed calibrate_consumer_twin over T4's aggregates; integration: this was a strict xfail
    until T5 was in): the reference window [split - span, split) and the calibration window [split, split + span) do not overlap, a
    Consumer of the reference window never counts in the calibration window (grouped by Consumer, not by message), reference-window
    data does not reach the verdict, and a window the version's parameters could have seen is refused."""
    from datetime import timedelta
    from test_nx042_twin_calibration_pg import INSIDE, calibrate, dayparts, events, publish, refused, report
    from test_nx042_twin_calibration_read_pg import SPLIT, cid, seed
    admin, _ = db
    version = publish(admin)  # published before the first calibration window
    dayparts(admin, INSIDE)  # the calibration windows alone: 18 of 24 cells covered (0.75, inside 70%-90%)
    # Reference-window data that would change the verdict if it were read: 60 Consumers who all reply and all refuse...
    seed(admin, 'ref-only', 60, SPLIT - timedelta(days=10), replied=60, refused=60)
    # ...and 30 who were contacted in the reference window and again inside the first calibration window (daypart 00-06).
    both = seed(admin, 'ref-and-cal', 30, SPLIT - timedelta(days=5), replied=30, refused=30)
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        for i, c in enumerate(both):
            admin.execute("""insert into runtime.nexloop_conversation_messages(tenant_id,world,conversation_id,sequence,message_id,idempotency_key,payload_digest,record)
                values(%s,'real',%s,3,%s,'k-3',%s,%s)""", (TENANT_A, cid(f'conv-ref-and-cal-{i}'), cid(f'later-{i}'), 'd' * 64,
                Jsonb({'direction': 'outbound', 'sender_kind': 'agent', 'accepted_at': (SPLIT + timedelta(days=6)).isoformat()})))
    out = calibrate(admin, version)
    assert out['conclusion'] == 'passed' and float(out['coverage']) == 0.75  # exactly the calibration windows' own verdict
    body, _, _ = report(admin, out['report_ref'])
    first = body['windows'][0]
    assert first['reference'][1] == first['calibration'][0] == first['split_at']  # adjacent, never overlapping
    assert first['reference'][0] < first['reference'][1] < first['calibration'][1]
    assert first['excluded']['in_reference_window'] == 30 and first['reference_consumers_excluded'] == 30  # by Consumer
    assert {g['group']: g['consumers'] for g in first['groups']}['all'] == 120  # the four dayparts' 30 each, none of the 30 excluded
    assert body['grouping'] == 'consumer' and events(admin, version) == [('passed', out['report_ref'])]
    # A version published inside a calibration window could have seen it: refused.
    late = publish(admin, published_at=SPLIT + timedelta(days=1))
    assert refused(admin, 'window_before_publication', late)


def test_acceptance_no_disguised_probability(t7):
    """§6 (c), what exists: an uncalibrated twin's every output (samples, port events, provenance) says 'uncalibrated', gives intervals
    and never one probability, and the version is never selectable (NX-043 selects through selectable only). The report's own
    'uncalibrated' mark is T5's, NX-043's refusal NX-043's."""
    f = t7
    admin = f['admin']
    with f['role']('nexloop_sim_api')() as db:
        run = db.execute('select authz.nexloop_twin_sample(%s,%s,%s,1,%s,%s)', (digest(f['token_a']), f['a'], TWIN, 'no-disguise', 10)).fetchone()[0]
    acts = T.TwinPort(f['twin'], f['token_twin_a'], f['a']).run(T.ConsumerTwin(TWIN, 1, 'no-disguise'), consumers(admin, f['a']), steps=2)
    marks = [run['calibration_status']] + [s['calibration_status'] for s in run['samples']] + [a['provenance']['calibration_status'] for a in acts]
    marks += [a['event']['content']['attributes']['calibration_status'] for a in acts if a['event_id']]
    assert set(marks) == {'uncalibrated'}
    for behaviors in [s['behaviors'] for s in run['samples']] + [a['behaviors'] for a in acts]:
        for b in behaviors.values():
            assert set(b) == {'low', 'high', 'coverage', 'simulated_outcome'} and b['high'] - b['low'] >= 0.01
    assert 'probability' not in json.dumps([run, acts])
    assert admin.execute('select runtime.nexloop_consumer_twin_selectable(%s,%s,1)', (TENANT_A, TWIN)).fetchone()[0] is False
    # A single probability never becomes a twin (T1): refused at publication.
    point = copy.deepcopy(DEFINITION)
    point['twin_id'] = 'trial.point'
    point['parameters']['behaviors']['payment_intent'].update(low=0.3, high=0.3)
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='consumer twin: invalid'):
        publish(admin, definition=point)


def test_twin_events_and_calibration_conclusions_are_never_facts_or_claims(t7):
    """§5: a twin's events are simulated outputs in their simulation world, marked as such in the context the model reads; a
    calibration conclusion is a control record of the version. Neither becomes a fact, a Claim, a Commitment's evidence, nor turns a
    hypothesis into a fact."""
    f = t7
    admin = f['admin']
    claims = lambda: admin.execute('select count(*) from ontology.nexloop_claims').fetchone()[0]  # noqa: E731
    objects = lambda: admin.execute('select world,type_name,count(*) from ontology.objects group by 1,2 order by 1,2').fetchall()  # noqa: E731
    evidence = lambda: admin.execute('select count(*) from runtime.nexloop_commitment_evidence').fetchone()[0]  # noqa: E731
    before = (claims(), objects(), evidence())
    people = consumers(admin, f['a'])
    port = T.TwinPort(f['twin'], f['token_twin_a'], f['a'])
    acts = port.run(T.ConsumerTwin(TWIN, 1, 'not-fact'), people, steps=3)
    events = {a['event_id'] for a in acts if a['event_id']}
    assert events
    # The context the model reads marks every twin event as simulated input, from a simulated source - never a fact.
    binding = port.bind(T.ConsumerTwin(TWIN, 1, 'not-fact'))['binding_id']
    pack = json.loads(port.observe(binding, people[0], 4, 64)['pack_text'])
    twin_evidence = [e for e in pack['evidence'] if e['ref'].removeprefix('sim:event:') in events]
    assert twin_evidence and {(e['evidence_kind'], e['source']) for e in twin_evidence} == {('simulated_input', 'simulated')}
    assert pack['data_mode'] == 'simulation' and pack['world'] == f['a']
    # A calibration conclusion (what T5 writes after checking its report) is a control record of the version.
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')  # a calibration event as T5 writes it; T5's report check is T5's test
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT_A,))
        admin.execute("""insert into control.nexloop_consumer_twin_calibrations(tenant_id,twin_id,version,report_ref,verdict,recorded_by,request_id)
            values(%s,%s,1,%s,'passed','t5-test','cal-fact')""", (TENANT_A, TWIN, 'twin-calibration:' + 'c' * 64))
    assert (claims(), objects(), evidence()) == before  # no Claim, no object, no Commitment evidence in any world
    # A passed calibration leaves the hypotheses hypotheses.
    definition = admin.execute('select runtime.nexloop_consumer_twin_definition(%s,%s,1)', (TENANT_A, TWIN)).fetchone()[0]
    assert {h['kind'] for h in definition['hypotheses']} == {'hypothesis'}
    # Where facts and statements live, a simulation world is refused outright.
    with pytest.raises(psycopg.errors.CheckViolation, match='nx041_no_simulation_evidence'), admin.transaction():
        admin.execute("""insert into runtime.nexloop_commitment_evidence(tenant_id,world,commitment_id,kind,ref,occurred_at,recorded_by)
            values(%s,%s,%s,'commercial_event',%s,now(),'twin')""", (TENANT_A, f['a'], 'c' * 64, 'sim:event:' + next(iter(events))))
    # A Claim hangs on a conversation (foreign key) and a conversation is real only (CHECK): no Claim can exist in a simulation world.
    fk = admin.execute("""select confrelid::regclass::text from pg_constraint where conrelid='ontology.nexloop_claims'::regclass and contype='f'
        and confrelid='runtime.nexloop_conversations'::regclass""").fetchall()
    assert fk == [('runtime.nexloop_conversations',)]
    with pytest.raises(psycopg.errors.CheckViolation), admin.transaction():
        admin.execute("""insert into runtime.nexloop_conversations(tenant_id,world,conversation_id,consumer_id,principal_id,idempotency_key)
            values(%s,%s,'conv-twin',%s,'twin','twin-1')""", (TENANT_A, f['a'], people[0]))


def test_at046_the_twin_drives_a_complete_simulation_pi_run(sim_group, admin, tmp_path):
    """AT-046 with the twin (design §4): S3 Consumers -> twin bound and stepped through the port -> the simulation group's Pi Run from
    the world's sim context, which carries the twin's events. Node 24, serial."""
    f = sim_group
    publish(admin, tenant=f['tenant'])
    # D10 extended (owner 2026-10-21): the port authenticates with the world's fourth credential, its twin use.
    twin_token, _ = seed_authority(admin, f['tenant'], 'eios:artifact:' + 'e' * 32, world=f['world'], identity_suffix='-sim-twin', sim_use='twin')  # no Action grant
    # The snapshot after the setup (credentials are trusted configuration), before the twin and the Run.
    before_counts, before = real_counts(admin), real_digest(admin)
    port = T.TwinPort(lambda: psycopg.connect(make_conninfo(f['pg'], user='nexloop_twin_port'), autocommit=True), twin_token, f['world'])
    acts = port.run(T.ConsumerTwin(TWIN, 1, 'at046'), [f['consumer']], steps=6)
    events = [a['event_id'] for a in acts if a['event_id']]
    assert events, 'the twin acted'
    r = run_sim_pi_run(f, tmp_path)
    assert r['done']['runtime_outcome'] == 'succeeded', r['done']
    pack = json.loads(r['pack_text'])
    refs = {e['ref'] for e in pack['evidence']}
    assert {'sim:event:' + e for e in events} & refs, 'the Run read the twin events'
    assert all(a['provenance']['calibration_status'] == 'uncalibrated' for a in acts)
    # Nothing real: no real credential used, the real tables and every real row unchanged.
    used = admin.execute('select array_agg(distinct w) from authz.nexloop_service_credentials, unnest(worlds) w where tenant_id=%s', (f['tenant'],)).fetchone()[0]
    assert used == [f['world']]
    assert real_counts(admin) == before_counts
    assert changed(before, real_digest(admin)) == []
