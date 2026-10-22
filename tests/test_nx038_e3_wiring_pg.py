"""NX-038 E3 wiring (dispatcher 2026-10-22, G-S6-1) and the failure basis the owner chose (2026-10-22, option A).

Failure basis: a Run whose semantic read found nothing and whose reply was not completed is failed (attribution._failed), with no
separate miss signal. "Reply not completed" for a message Run = its inbound message got the fallback reply Run (0110); the trace read
says reply_fallback and the sample carries it in its existing outcome field. A miss whose reply was completed is no failure.
Synthetic ids, hashes and refs only.
"""
import hashlib
import json
import uuid

from nexloop_eios.evolution.attribution import attribute, draft_candidates
from nexloop_eios.evolution_traces import sample_from_run
from support.tenants import TENANT_A
from test_evolution_e4_pg import TERM, access, miss_case, sample
from test_evolution_traces_pg import hexid


def _runs(n, *, reply_fallback):
    consumers = [hexid('e3w' + str(i)) for i in range(3)]
    samples, accesses = [], []
    for i in range(n):
        s, a = miss_case(str(uuid.uuid4()), consumers[i % 3])
        # No tool or model failure: only the reply outcome decides.
        s = {**s, 'failures': [], 'outcome': 'reply_fallback' if reply_fallback else None}
        samples.append(s)
        accesses += a
    return samples, accesses


def test_a_miss_with_the_reply_not_completed_is_a_failure_and_drafts_a_retrieval_miss():
    samples, accesses = _runs(6, reply_fallback=True)
    d = attribute(samples, accesses)
    assert d.counts['retrieval_miss'] == 6
    drafts, _ = draft_candidates(d, samples, tenant_id=TENANT_A, parent_revision='sem-7', evaluator_version='l0-1', model_profile='none',
                                 created_at='2026-10-22T00:00:00Z')
    assert len(drafts) == 1 and drafts[0].patch['change_kind'] == 'alias' and drafts[0].patch['term_hashes'] == [TERM]


def test_a_miss_with_the_reply_completed_is_no_failure():
    samples, accesses = _runs(6, reply_fallback=False)
    d = attribute(samples, accesses)
    assert d.counts['retrieval_miss'] == 0 and d.counts['none'] == 6
    drafts, _ = draft_candidates(d, samples, tenant_id=TENANT_A, parent_revision='sem-7', evaluator_version='l0-1', model_profile='none',
                                 created_at='2026-10-22T00:00:00Z')
    assert drafts == []
    # A Run that failed otherwise still counts as before.
    failed = sample(str(uuid.uuid4()), hexid('e3w-x'))
    assert attribute([failed], [access(failed['run_id'])]).counts['retrieval_miss'] == 1


def test_the_trace_read_flag_lands_in_the_sample_outcome():
    run = {'run_id': str(uuid.uuid4()), 'kind': 'message', 'issued_at': '2026-10-22T00:00:00Z', 'consumer_id': hexid('e3w-c'), 'persona': 'real',
           'evidence': {}}
    assert sample_from_run({**run, 'reply_fallback': True})['outcome'] == 'reply_fallback'
    assert sample_from_run({**run, 'reply_fallback': False})['outcome'] is None
    assert sample_from_run(run)['outcome'] is None


# --- the wiring: a real relay assembles and binds a v6 Context (option A) ------------------------------------------------------------

import copy  # noqa: E402

import pytest  # noqa: E402

from nexloop_eios import context_artifacts  # noqa: E402
from local_message_assembly_fixture import assembled_message, business_plan, configured  # noqa: E402,F401
from test_context_artifacts import context_message  # noqa: E402,F401
from test_context_v6_pg import STRATEGY, bound, owner, seed_strategy, v6  # noqa: E402,F401
from test_evolution_traces_pg import traces  # noqa: E402,F401


@pytest.fixture(autouse=True)
def semantic_budget():
    """The process's budgeted semantic-read pool (L6 review 5.1; message-relay --semantic-read-pool-max)."""
    context_artifacts.configure_semantic_reads(2)


def accesses(admin, tenant):
    with admin.transaction():
        owner(admin, tenant)
        return admin.execute("""select tenant_id,world,run_id::text,context_id,kind,operation,term_hashes,result_count
            from runtime.nexloop_semantic_accesses order by recorded_at""").fetchall()


def semantic(admin, tenant, enabled):
    newer = copy.deepcopy(STRATEGY)
    newer.update(version=2, semantic={**STRATEGY['semantic'], 'enabled': enabled})
    seed_strategy(admin, tenant, newer)


def test_an_enabled_strategy_records_the_runs_semantic_read(v6, admin):
    f = v6
    tenant = f['original']['tenant']
    semantic(admin, tenant, True)
    assert f['v6_relay']().run_once() == 'queued'
    pack, row = bound(admin)
    rows = accesses(admin, tenant)
    assert len(rows) == 1
    t, world, run_id, context_id, kind, operation, terms, _ = rows[0]
    assert (t, world, run_id, kind, operation) == (tenant, 'real', row[2], 'access', 'browse')
    assert context_id is None  # option A: read before the bind
    assert terms == [hashlib.sha256(f['message']['body'].strip().encode()).hexdigest()]  # the hash, never the text
    assert f['message']['body'] not in json.dumps(rows)
    assert pack['semantics'] == []  # option A: nothing in the pack


def test_a_disabled_strategy_never_reads(v6, admin, monkeypatch):
    f = v6
    called = []
    monkeypatch.setattr('nexloop_eios.context_engine.semantic.SemanticAdapter.browse', lambda *a, **k: called.append(1))
    assert f['v6_relay']().run_once() == 'queued'  # the built-in strategy: semantic.enabled false
    assert called == [] and accesses(admin, f['original']['tenant']) == []


@pytest.mark.parametrize('mode', ['error', 'timeout'])
def test_a_failed_or_slow_read_never_stops_the_context(v6, admin, monkeypatch, mode):
    import time
    f = v6
    tenant = f['original']['tenant']
    semantic(admin, tenant, True)

    def broken(*a, **k):
        if mode == 'timeout':
            time.sleep(context_artifacts.SEMANTIC_READ_TIMEOUT_SECONDS * 4)
        raise RuntimeError('synthetic')
    monkeypatch.setattr('nexloop_eios.context_engine.semantic.SemanticAdapter.browse', broken)
    started = time.monotonic()
    assert f['v6_relay']().run_once() == 'queued'
    assert bound(admin)[0]['schema_version'] == 'nexloop.context-pack.v6' and accesses(admin, tenant) == []
    if mode == 'timeout':
        assert time.monotonic() - started < 30


def test_an_actual_pi_run_keeps_its_semantic_read_bound_to_it(v6, admin, tmp_path):
    """The real path end to end (Host parses v6, Pi runs it): the access recorded at assembly belongs to the Run the runtime then
    activated and ran, in the tenant and world of the service identity."""
    from test_context_v6_pg import test_actual_pi_run_records_every_model_call_of_a_v6_run as actual_pi_run
    tenant = v6['original']['tenant']
    semantic(admin, tenant, True)
    actual_pi_run(v6, admin, tmp_path)
    _, row = bound(admin)
    rows = accesses(admin, tenant)
    assert [(r[0], r[1], r[2], r[4]) for r in rows] == [(tenant, 'real', row[2], 'access')]
    assert admin.execute('select count(*) from authz.nexloop_runtime_run_bindings where run_id=%s and tenant_id=%s and world=%s',
                         (row[2], tenant, 'real')).fetchone() == (1,)


def test_the_trace_read_says_reply_fallback_for_a_message_whose_fallback_ran(traces):
    """0313: a message Run whose inbound message got the fallback reply Run (0110) reads as reply_fallback; others do not."""
    from datetime import timedelta
    from nexloop_eios.evolution_traces import DatabaseTraceSource
    from test_evolution_traces_pg import run as issued_run
    t = traces
    admin = t['admin']
    message = 'm-' + t['runs']['real']
    fallback = issued_run(admin, t['digest'], t['now'] - timedelta(seconds=30))
    with admin.transaction():
        admin.execute('set local session_replication_role = replica')
        admin.execute("""insert into authz.nexloop_message_fallback_issuances(tenant_id,world,message_id,run_id,request_id,issuance_nonce,assignment_id,
            assignment_revision,assignment_digest,issuer_principal,run_digest,issued_at,expires_at)
            values(%s,'real',%s,%s,'r',%s,'a',1,%s,'p',%s,%s,%s)""",
                      (TENANT_A, message, fallback, 'f' * 64, 'f' * 64, hexid(fallback), t['now'] - timedelta(seconds=30), t['now']))
    read = DatabaseTraceSource(t['evo'], t['credential']).read(t['now'] - timedelta(days=1), t['now'] + timedelta(minutes=1))
    outcome = {s['run_id']: s['outcome'] for s in read.samples}
    assert outcome[t['runs']['real']] == 'reply_fallback' and outcome[t['runs']['agent']] is None and outcome[fallback] is None


def test_a_slow_database_never_holds_a_context_connection(v6, admin, monkeypatch):
    """L6 review 2.1: reads stuck in a slow database run on their own pool, at most two at once; the rest are skipped and counted,
    the assembly never waits longer than the bound and the Context pool keeps its connections."""
    import threading
    import time
    from nexloop_eios.context_artifacts import ContextV6ArtifactProducer, _semantic_pool
    f = v6
    stuck = threading.Event()
    running, peak = [0], [0]
    guard = threading.Lock()

    def slow(*a, **k):
        with guard:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        stuck.wait(10)
        with guard:
            running[0] -= 1
    monkeypatch.setattr('nexloop_eios.context_engine.semantic.SemanticAdapter.browse', slow)
    producer = ContextV6ArtifactProducer(f['source'], 'recent_plus_required')
    enabled = {'semantic': {'enabled': True, 'top_k': 5, 'ambiguity_margin': 0.05}}
    try:
        for n in range(5):
            started = time.monotonic()
            producer._semantic_read(enabled, {'body': 'synthetic statement'}, str(uuid.uuid4()))
            assert time.monotonic() - started < context_artifacts.SEMANTIC_READ_TIMEOUT_SECONDS + 0.5
            with producer.pool.connection(timeout=1) as db:  # the Context pool still gives a connection at once
                assert db.execute('select 1').fetchone() == (1,)
        assert producer.semantic['timeout'] == 2 and producer.semantic['skipped'] == 3 and peak[0] <= context_artifacts.SEMANTIC_READ_CONCURRENCY
    finally:
        stuck.set()
    deadline = time.monotonic() + 10
    while running[0] and time.monotonic() < deadline:
        time.sleep(0.05)
    # The slots come back once the stuck reads end; the database side ends a statement after 400 ms.
    producer._semantic_run = None
    with _semantic_pool(producer.pool).connection() as db:
        assert db.execute('show statement_timeout').fetchone()[0] == context_artifacts.SEMANTIC_STATEMENT_TIMEOUT


def test_without_a_budgeted_pool_nothing_is_read(v6, admin, monkeypatch):
    """L6 review 5.1: a process whose budget declares no semantic-read pool (the default 0) reads nothing, counted skipped."""
    from nexloop_eios.context_artifacts import ContextV6ArtifactProducer, FallbackContextV6Producer
    called = []
    monkeypatch.setattr('nexloop_eios.context_engine.semantic.SemanticAdapter.browse', lambda *a, **k: called.append(1))
    monkeypatch.setattr(context_artifacts, '_semantic_slots', None)
    producer = ContextV6ArtifactProducer(v6['source'], 'recent_plus_required')
    enabled = {'semantic': {'enabled': True, 'top_k': 5, 'ambiguity_margin': 0.05}}
    producer._semantic_read(enabled, {'body': 'synthetic statement'}, str(uuid.uuid4()))
    assert called == [] and producer.semantic['skipped'] == 1
    # The fallback reply Run never reads: its message Run already did, and the reply-guarantor needs no semantic connections.
    fallback = FallbackContextV6Producer(v6['source'], 'recent_plus_required')
    monkeypatch.setattr(context_artifacts, '_semantic_slots', __import__('threading').BoundedSemaphore(2))
    fallback._semantic_read(enabled, {'body': 'synthetic statement'}, str(uuid.uuid4()))
    assert called == [] and fallback.semantic == {'read': 0, 'timeout': 0, 'failed': 0, 'skipped': 0}


def test_the_relay_takes_its_semantic_pool_from_the_command_line():
    from nexloop_eios import message_relay_cli
    base = [a for n in ('database-url-file', 'signing-key-file', 'route-credential-file', 'source-credential-file', 'planner-credential-file',
                        'executor-credential-file', 'artifact-root', 'vault-root') for a in ('--' + n, 'x')] + ['--recipe-file', 'x']
    assert message_relay_cli.arguments(base).semantic_read_pool_max == 0
    assert message_relay_cli.arguments(base + ['--semantic-read-pool-max', '1']).semantic_read_pool_max == 1
    with pytest.raises(SystemExit):
        message_relay_cli.arguments(base + ['--semantic-read-pool-max', '5'])
