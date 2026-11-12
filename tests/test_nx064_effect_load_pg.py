"""NX-064 high-load regression (L7 list item 1; design NX-064-effect-timeout-design.md §4.3, ADR-029c): effect requests at the NX-031
matrix concurrency on the actual multi-consumer message chain (tests/load_chain.py), each made as the Host makes it
(load_chain.submit_like_the_host). Every request either completes within the deployed effect limit, or is handled as ADR-029c says:
the same body is sent once more, there is at most one intent per Run, and the Run ends with an explicit runtime_effect_unavailable -
never a silent failure, never a second intent or a second reservation.

Concurrency: the matrix's gate cells (deploy/load/matrix.v2.json, host_concurrency = the deployment's maximum_active_runs) and twice
that; the guard runs as the deployment's 4 guard worker processes (one backend each). The limit is the deployed one
(deploy/stage/agent-host.runtime-config.example.json, else the Host default) and is never raised here. The Host's own resend code is
covered by apps/agent-host/test/effect-metrics.test.ts and effect-ambiguous-retry.test.ts; this test loads the guard and SQL side.
Runs in HEAVY_TESTS (the low-parallelism stage): it is a load test.

Moving tests/test_context_v6_relationships_pg.py::test_v6_carries_v4_relationships_with_hypotheses_as_evidence_only back out of
HEAVY_TESTS: not now. Conditions (design §4.4), all of them: the Host-side effect histogram (kind host_op, migration 0405) is deployed
on the CI host; 20 full-parallel CI runs with the test in the parallel stage all pass; in each, the effect_submit maximum of the worker
that ran it is below 0.8 x the deployed effect limit; and this regression passes at the gate concurrency in the same period. The limit
and the assertions of that test are not changed to get there."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack

import pytest
from psycopg.conninfo import make_conninfo

from nexloop_eios.backend import open_backend
from nexloop_eios.run_credential_vault import RunCredentialVault
from load_chain import (EFFECT_BODY, LoadChain, activate_runs, deployment_effect_limit_seconds, matrix_gate_concurrency,
                        submit_like_the_host)
from local_message_assembly_fixture import assembled_message, business_plan, configured  # noqa: F401
from test_load_chain_pg import S, _relay, chain_tenant

GUARD_WORKERS = 4  # compose.app.example.yaml: ADR-024, 4 guard processes
GATE = matrix_gate_concurrency()
CONCURRENCY = sorted(set(GATE) | {2 * max(GATE)})


def one_message_each(records):
    """Each manifest consumer with one conversation of one message: one Run per consumer, so intents are counted per consumer."""
    out = []
    for record in records:
        conversation = dict(record['conversations'][0]);conversation['messages'] = conversation['messages'][:1]
        out.append({**record, 'conversations': [conversation]})
    return out


def intents_by_consumer(admin):
    return dict(admin.execute('select consumer_id,count(*) from runtime.nexloop_effect_intents group by 1').fetchall())


def ledgers(admin):
    return admin.execute("""select (select count(*) from runtime.nexloop_effect_intents),(select count(*) from runtime.nexloop_effect_outbox),
        (select coalesce(sum(reserved_units),0) from control.nexloop_effect_contexts),
        (select coalesce(sum(reserved_units),0) from control.nexloop_effect_control_ledger)""").fetchone()


def held_past_the_limit(monkeypatch, limit, first):
    """The guard's first `first` submissions write their intent and then hold the transaction (locks included) past the Host's limit
    before committing: the Host gives up and resends. Only these; every later request runs at the load's own speed."""
    from nexloop_eios.effect_intents import EffectIntentPort
    original, lock, seen = EffectIntentPort._execute_in_transaction, threading.Lock(), [0]

    def held(self, db, *args, **kwargs):
        result = original(self, db, *args, **kwargs)
        with lock:
            seen[0] += 1;slow = seen[0] <= first
        if slow:
            time.sleep(limit + 0.5)
        return result
    monkeypatch.setattr(EffectIntentPort, '_execute_in_transaction', held)


@pytest.mark.parametrize('concurrency', CONCURRENCY)
@pytest.mark.parametrize('guard', ['load', 'held_past_the_limit'])
def test_effect_requests_at_matrix_concurrency_complete_or_end_as_adr029c_says(assembled_message, admin, tmp_path, concurrency, guard, record_property,
                                                                              monkeypatch):
    f = assembled_message
    limit = deployment_effect_limit_seconds()
    if guard == 'held_past_the_limit':
        held_past_the_limit(monkeypatch, limit, concurrency)
    *records, _ = [json.loads(x) for x in S.lines(64, 2 * concurrency)]
    records = one_message_each(records)
    o = f['original']
    with chain_tenant(f, tmp_path) as (api, uow):
        chain = LoadChain(f, admin, api, uow)
        loaded = chain.load(records)
        assert loaded['consumers'] == loaded['enrolled'] == len(records) == 2 * concurrency
        people = {p['consumer'] for _, p in loaded['people']}
        vault_dir = tmp_path / 'vault';vault_dir.mkdir(mode=0o700)
        with RunCredentialVault(vault_dir) as vault:
            relay = _relay(api, chain, vault)
            assert [relay.run_once() for _ in records] == ['queued'] * len(records)
        dsn = make_conninfo(o['pg'], user='nexloop_domain_worker')
        with ExitStack() as stack:
            backends = [stack.enter_context(open_backend(database_url=dsn, artifact_root=tmp_path / f'guard-{n}', signing_key_file=o['paths']['backend_signing'],
                                                         signing_key_id='explicit-configuration')) for n in range(GUARD_WORKERS)]
            workers = [b.authenticate(f['tokens']['assembly-runtime-worker'], world='real') for b in backends]
            runs = activate_runs(workers[0], len(records))
            before = ledgers(admin)
            served = iter(range(10 ** 6));lock = threading.Lock()

            def guard_call(run):
                def call(body):
                    with lock:
                        n = next(served)
                    return workers[n % GUARD_WORKERS].runtime_effect_tool(activation_ref=run['activation']['activation_ref'], command=run['command'],
                                                                          tool_operation='submit', parameters=body)
                return call

            # The Host: `concurrency` active Runs at a time; each request thread may be abandoned at the limit and keep running.
            with ThreadPoolExecutor(max_workers=4 * concurrency) as requests, ThreadPoolExecutor(max_workers=concurrency) as hosts:
                started = time.monotonic()
                results = list(hosts.map(lambda run: submit_like_the_host(guard_call(run), limit, requests), runs))
                wall = time.monotonic() - started
                for result in results:  # the guard finishes what the Host gave up on, before anything is counted
                    for future in result['abandoned']:
                        try:
                            future.result(timeout=120)
                        except Exception:  # noqa: BLE001 - an abandoned request may end either way; only its effect counts
                            pass
        after = ledgers(admin)
        per_consumer = intents_by_consumer(admin)
    outcomes = [r['outcome'] for r in results]
    first = sorted(r['timings'][0] for r in results)
    record_property('nx064_load', json.dumps({'guard': guard, 'concurrency': concurrency, 'runs': len(runs), 'limit_s': limit, 'wall_s': round(wall, 2),
        'outcomes': {k: outcomes.count(k) for k in set(outcomes)}, 'first_p50_s': round(first[len(first) // 2], 3), 'first_max_s': round(first[-1], 3)}))
    record_property('nx064_attempts', json.dumps([r['attempt_traces'] for r in results]))
    record_property('nx064_ledgers', json.dumps({'before': before, 'after': after,
        'intent_counts_per_consumer': sorted(per_consumer.values())}, default=str))
    # Never silent: every Run ends explicitly - accepted (first time or after the one resend) or runtime_effect_unavailable.
    assert len(results) == len(runs) and set(outcomes) <= {'ok', 'ok_after_resend', 'runtime_effect_unavailable'}, outcomes
    for r in results:
        assert 1 <= r['attempts'] <= 2 and len(r['bodies']) == r['attempts'] and all(b == EFFECT_BODY for b in r['bodies'])  # same body, once more at most
        if r['outcome'] != 'runtime_effect_unavailable':
            assert r['timings'][-1] <= limit + 0.05 and r['receipt']['state'] == 'accepted'  # completed within the deployed limit
    # At most one intent per Run (one Run per consumer); one for every Run that was answered; no reservation twice.
    assert set(per_consumer) <= people and all(n == 1 for n in per_consumer.values()), per_consumer
    answered = sum(o != 'runtime_effect_unavailable' for o in outcomes)
    assert answered <= after[0] - before[0] == len(per_consumer) <= len(runs)
    assert after[1] - before[1] == after[0] - before[0]  # one outbox item per intent
    assert after[2] - before[2] <= after[0] - before[0] and after[3] - before[3] <= after[0] - before[0]
    if guard == 'held_past_the_limit':
        # The ADR-029c path was taken: the held first requests were abandoned and resent with the same body; the guard still committed
        # them, so every Run has its one intent whatever the Host was told.
        resent = [r for r in results if r['attempts'] == 2]
        assert len(resent) >= concurrency and all(r['abandoned'] for r in resent), outcomes
        assert len(per_consumer) == len(runs)
