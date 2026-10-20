"""NX-038 E2 trajectory read (design §11.2, §12.1, §12.5): authz.nexloop_evo_trace_runs as nexloop_evo_reader and the two
TraceSource routes.

Real PostgreSQL on a bootstrapped catalog. Admin only seeds synthetic rows (issued Runs and their message / role links with
foreign-key triggers off inside one transaction: the Runs' contexts and commands are not what this test is about; personas;
erasures) and probes. Every sample validates against the trajectory-sample contract; nothing is written by reading.
"""
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jsonschema
import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from authority_fixture import seed_authority
from erasure_support import mark_erasing
from nexloop_eios.bootstrap import bootstrap
from support.tenants import TENANT_A, TENANT_B
from nexloop_eios.evolution_traces import (DatabaseTraceSource, EvidencePackTraceSource, TraceUnavailable, sample_from_record,
                                           snapshot_digest)

CONTRACT = json.loads((Path(__file__).resolve().parents[1] / 'packages/contracts/trajectory-sample.schema.json').read_text())
TENANT, OTHER = TENANT_A, TENANT_B
READ = 'select authz.nexloop_evo_trace_runs(%s,%s,%s,%s,%s,%s)'


def hexid(label):
    return hashlib.sha256(label.encode()).hexdigest()


def private(path, text):
    path.write_text(text)
    path.chmod(0o600)
    return path


def run(admin, source, at, *, consumer=None, link=None, tenant=TENANT):
    """One issued Run (and, for message / role, the row linking it to its Consumer)."""
    run_id = uuid.uuid4()
    with admin.transaction():
        admin.execute('set local session_replication_role = replica')
        admin.execute("""insert into authz.nexloop_run_credentials(token_digest,run_id,source_digest,source_directory_hash,world,audience,allowed_resources,status,
            issued_at,expires_at) values(%s,%s,%s,'h','real','nexloop-agent-host',array['r'],'active',%s,%s)""",
            (hexid(str(run_id)), run_id, source, at, at + timedelta(seconds=60)))
        if link == 'message':
            conversation, message = 'cv-' + str(run_id), 'm-' + str(run_id)
            admin.execute("insert into runtime.nexloop_conversations(tenant_id,world,conversation_id,consumer_id,principal_id,idempotency_key) values(%s,'real',%s,%s,'p',%s)",
                          (tenant, conversation, consumer, conversation))
            admin.execute("""insert into runtime.nexloop_conversation_messages(tenant_id,world,conversation_id,sequence,message_id,idempotency_key,payload_digest,record)
                values(%s,'real',%s,1,%s,'k','d','{}')""", (tenant, conversation, message))
            admin.execute("""insert into runtime.nexloop_message_routes(tenant_id,world,message_id,run_id,run_digest,queue,event_id,event_envelope,command,command_digest,
                input_digest,bound_by,context_id) values(%s,'real',%s,%s,'d','q',%s,'{}','{}',%s,%s,'b',%s)""",
                (tenant, message, run_id, uuid.uuid4(), 'c' * 64, 'c' * 64, uuid.uuid4()))
        elif link == 'role':
            admin.execute("""insert into authz.nexloop_role_run_bindings(run_id,source_digest,tenant_id,world,consumer_id,link_id,role_id,step_id,link_revision,
                role_revision,step_revision,role_ref,scope,expires_at) values(%s,%s,%s,'real',%s,'l','r','s',1,1,1,'rr','sc',%s)""",
                (run_id, source, tenant, consumer, at + timedelta(seconds=60)))
    return str(run_id)


def persona(admin, consumer, value, tenant=TENANT):
    admin.execute("insert into control.nexloop_consumer_personas(tenant_id,world,consumer_id,persona,revision,updated_by) values(%s,'real',%s,%s,1,'synthetic')",
                  (tenant, consumer, value))


@pytest.fixture
def traces(admin, pg, tmp_path):
    bootstrap(admin)
    token, _ = seed_authority(admin, TENANT, 'eios:artifact:'+'e'*32)
    other_token, _ = seed_authority(admin, OTHER, 'eios:artifact:'+'e'*32, identity_suffix='-other')
    digest, other = hexid(token), hexid(other_token)
    now = datetime.now(UTC).replace(microsecond=0)
    c = {k: hexid('evo-consumer-' + k) for k in ('real', 'agent', 'synthetic', 'erased')}
    persona(admin, c['agent'], 'simulated_agent')
    persona(admin, c['synthetic'], 'synthetic')
    runs = {'real': run(admin, digest, now - timedelta(minutes=5), consumer=c['real'], link='message'),
            'agent': run(admin, digest, now - timedelta(minutes=4), consumer=c['agent'], link='role'),
            'other': run(admin, digest, now - timedelta(minutes=3)),
            'synthetic': run(admin, digest, now - timedelta(minutes=2), consumer=c['synthetic'], link='message'),
            'erased': run(admin, digest, now - timedelta(minutes=1), consumer=c['erased'], link='role'),
            'tenant_b': run(admin, other, now - timedelta(minutes=1), consumer=hexid('b'), link='role', tenant=OTHER),
            'old': run(admin, digest, now - timedelta(days=40))}
    mark_erasing(admin, TENANT, c['erased'])
    return {'admin': admin, 'pg': pg, 'token': token, 'digest': digest, 'consumers': c, 'runs': runs, 'now': now,
            'credential': private(tmp_path / 'evo-credential', token + '\n'),
            'evo': lambda: psycopg.connect(make_conninfo(pg, user='nexloop_evo_reader'), autocommit=True)}


def counts(admin):
    return admin.execute("""select (select count(*) from authz.nexloop_run_credentials),(select count(*) from runtime.audit_events),
        (select count(*) from control.nexloop_consumer_personas),(select count(*) from control.nexloop_consumer_erasures)""").fetchone()


def test_the_reader_reads_its_tenants_runs_labelled_and_counts_what_it_excludes(traces):
    t = traces
    before = counts(t['admin'])
    source = DatabaseTraceSource(t['evo'], t['credential'])
    read = source.read(t['now'] - timedelta(days=1), t['now'] + timedelta(minutes=1))
    for s in read.samples:
        jsonschema.validate(s, CONTRACT)
    got = [(s['run_id'], s['run_kind'], s['consumer_id'], s['persona']) for s in read.samples]
    assert got == [(t['runs']['real'], 'message', t['consumers']['real'], 'real'), (t['runs']['agent'], 'role', t['consumers']['agent'], 'simulated_agent'),
                   (t['runs']['other'], 'other', None, None)]
    assert read.excluded == {'erasure': 1, 'persona': 1}
    assert all(s['source'] == 'database' and s['model'] == {'calls': 0, 'failed': 0, 'unknown': 0} for s in read.samples)
    assert read.digest == snapshot_digest(list(reversed(read.samples)))  # order-independent, stable
    text = json.dumps(read.samples)
    assert t['runs']['tenant_b'] not in text and t['runs']['old'] not in text and t['runs']['erased'] not in text
    # Synthetic Consumers are read only when asked for, still labelled.
    every = source.read(t['now'] - timedelta(days=1), t['now'] + timedelta(minutes=1), personas=('real', 'simulated_agent', 'synthetic'))
    assert [s['persona'] for s in every.samples] == ['real', 'simulated_agent', None, 'synthetic'] and every.excluded == {'erasure': 1, 'persona': 0}
    # Pages of one Run walk the window with the cursor; the maximum stops the read and counts the rest.
    paged = DatabaseTraceSource(t['evo'], t['credential'], page=1)
    assert [s['run_id'] for s in paged.read(t['now'] - timedelta(days=1), t['now'] + timedelta(minutes=1)).samples] == [s['run_id'] for s in read.samples]
    capped = paged.read(t['now'] - timedelta(days=1), t['now'] + timedelta(minutes=1), maximum=2)
    assert len(capped.samples) == 2 and capped.excluded['over_limit'] == 1
    assert counts(t['admin']) == before


def test_only_the_reader_role_with_an_active_service_credential_reads(traces):
    t = traces
    window = (t['now'] - timedelta(days=1), t['now'])
    for role in ('nexloop_api', 'nexloop_metrics'):
        with psycopg.connect(make_conninfo(t['pg'], user=role), autocommit=True) as db:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                db.execute(READ, (t['digest'], *window, None, 10, ['real'])).fetchone()
    with pytest.raises(TraceUnavailable):
        DatabaseTraceSource(lambda: psycopg.connect(make_conninfo(t['pg'], user='nexloop_api'), autocommit=True), t['credential']).read(*window)
    with t['evo']() as db:
        # No other table or function: the role reads nothing directly.
        for sql in ('select count(*) from authz.nexloop_run_credentials', "select runtime.nexloop_nx037_run_evidence('x','real',gen_random_uuid())"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                db.execute(sql).fetchone()
        with pytest.raises(psycopg.Error):
            db.execute(READ, (hexid('unknown-token'), *window, None, 10, ['real'])).fetchone()
        for bad in ((t['now'], t['now'], None, 10, ['real']), (t['now'] - timedelta(days=32), t['now'], None, 10, ['real']), (*window, None, 501, ['real']),
                    (*window, None, 10, ['robot']), (*window, None, 10, []), (*window, 'yesterday', 10, ['real'])):
            with pytest.raises(psycopg.errors.InvalidParameterValue):
                db.execute(READ, (t['digest'], *bad)).fetchone()
    t['admin'].execute("update authz.nexloop_service_credentials set status='revoked' where token_digest=%s", (t['digest'],))
    with t['evo']() as db, pytest.raises(psycopg.Error):
        db.execute(READ, (t['digest'], *window, None, 10, ['real'])).fetchone()


def test_the_evidence_pack_route_drops_test_stack_and_non_real_records_and_filters_personas(tmp_path):
    base = {'seq': 1, 'kind': 'ordinary_reply', 'run_id': str(uuid.uuid4()), 'started_at': '2026-10-20T04:00:00Z', 'ended_at': '2026-10-20T04:00:05Z',
            'outcome': 'replied', 'error_code': None, 'time_ms': 5000, 'model': {'calls': 1, 'failed': 0, 'unknown': 0, 'ms': 10}, 'tool_calls': 2, 'cost': {},
            'intents': {'fulfilled': 1}, 'intent_ids': [str(uuid.uuid4())], 'refusals': {}, 'failures': [], 'recovered_tool_errors': [], 'receipts': ['r-1'],
            'reconciliation': None, 'facts': {}, 'test_stack': False, 'persona': 'real', 'driver': 'real'}
    records = [dict(base, interaction_id=str(uuid.uuid4())),
               dict(base, interaction_id=str(uuid.uuid4()), persona='simulated_agent'),
               dict(base, interaction_id=str(uuid.uuid4()), persona='synthetic'),
               dict(base, interaction_id=str(uuid.uuid4()), test_stack=True),
               dict(base, interaction_id=str(uuid.uuid4()), driver='scripted')]
    (tmp_path / 'interactions').mkdir()
    (tmp_path / 'interactions' / 'records.json').write_text(json.dumps(records))
    read = EvidencePackTraceSource(tmp_path).read()
    assert [s['interaction_id'] for s in read.samples] == [r['interaction_id'] for r in records[:2]]
    assert read.excluded == {'test_stack': 1, 'driver': 1, 'persona': 1}
    for s in read.samples:
        jsonschema.validate(s, CONTRACT)
        assert s['source'] == 'evidence_pack' and 'facts' not in s and 'cost' not in s
    assert read.digest == EvidencePackTraceSource(tmp_path).read().digest
    assert sample_from_record(records[0]) == read.samples[0]
    with pytest.raises(TraceUnavailable):
        EvidencePackTraceSource(tmp_path).read(personas=('robot',))
    with pytest.raises(TraceUnavailable):
        EvidencePackTraceSource(tmp_path / 'missing').read()
