"""NX-031: build the load template on the actual message chain (run only through scripts/load/build_template.sh; outside
`testpaths`, so CI never collects it).

Disposable PostgreSQL from the test fixtures; the assembled message configuration (trusted configuration, business setup,
relay route / planner / executor / runtime-worker identities, message catalog) as the base; the fixed-seed manifest loaded only
through governed entries: per consumer identity, Consumer, EffectControl, catalog, ownership and enrollment with the
multi-consumer relay (tests/load_chain.py), messages through each customer's own port; commercial events through a test
connector; workbench Actions and members; the service-grants manifest; commitments (staff promise → extraction → keeper); the
outbound recorder's publication. Before the commitments, the history (v2 manifest: the latest one or two inbound messages per
consumer, dispatcher 2026-10-11 option A) is replied to through the actual message chain (scripts/load/stack.py: multi-consumer
relay, runtime worker with dispatch loops, deterministic Agent Host, effect worker, outbound recorder) until no inbound message
is unrouted, no Run task is open and no reply is due. Then an online pg_basebackup. Writes into $NEXLOOP_LOAD_OUT:
  manifest.jsonl, template.json (seed, digest, counts, timings, commit, catalog revision; no secrets),
  private/ (0600: customer and staff accounts, service tokens, the chain's tokens and run profile, signing keys),
  pgdata/ (the template cluster).
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

from psycopg.conninfo import make_conninfo
from nexloop_eios.backend import open_backend
from nexloop_eios.browser_identity import open_browser_identity
from nexloop_eios.browser_sessions import PostgresBrowserSessionUnitOfWork
import authority_fixture
from datetime import timedelta
# The cells reuse the template for days: the synthetic service credentials the fixtures seed must outlive a whole lane run.
authority_fixture.CREDENTIAL_VALIDITY = timedelta(days=30)
from conftest import admin, pg  # noqa: F401,E402
from local_message_assembly_fixture import assembled_message, business_plan, configured  # noqa: F401
from commercial_fixture import commercial_setup
from load_chain import LoadChain, load_plans, publish_workbench_actions, workbench_members
from load_template import apply_service_grants, load_commercial, load_commitments, snapshot
from test_outbound_messages_pg import publish_recorder

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/load'))
import synthetic  # noqa: E402
import collect  # noqa: E402
import stack as load_stack  # noqa: E402

HISTORY_TIMEOUT = int(os.environ.get('NEXLOOP_LOAD_HISTORY_TIMEOUT', '7200'))


def private(path, content):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb' if isinstance(content, bytes) else 'w') as handle:
        handle.write(content)
    return path


def history_open(db, tenant):
    """Inbound messages not yet routed, open message Run tasks and replies still due (reply-due items are deleted by the bound reply)."""
    return {'unrouted': db.execute("select count(*) from runtime.nexloop_message_outbox where tenant_id=%s and status='pending'", (tenant,)).fetchone()[0],
            'run_tasks': db.execute("select count(*) from runtime.jobs where queue='operations' and status in ('pending','retry_wait','running')").fetchone()[0],
            'replies_due': db.execute("select count(*) from runtime.nexloop_work_feed where tenant_id=%s and feed='reply-due' and status='pending'", (tenant,)).fetchone()[0]}


def reply_history(out, pg, tenant, lane, *, dispatch_loops=4):
    """History option A: the actual message chain replies to every loaded inbound message before the snapshot."""
    collect.TENANT = tenant
    chain = load_stack.Stack(SimpleNamespace(dsn=pg), out, lane, {})
    started, last, changed = time.monotonic(), None, time.monotonic()
    try:
        chain.start_message_chain(dispatch_loops)
        while True:
            with psycopg.connect(pg, autocommit=True) as db:
                left = history_open(db, tenant)
            if not any(left.values()):
                break
            if left != last:
                print(f'history: {left} after {time.monotonic() - started:.0f} s', flush=True)
                last, changed = left, time.monotonic()
            # Every Run task has ended and nothing is unrouted, but some replies stay due (their Run failed): the fallback reply Run
            # cannot run on the deterministic message Host, so they are recorded as unreplied history, never answered here.
            if not left['unrouted'] and not left['run_tasks'] and time.monotonic() - changed > 60:
                break
            if time.monotonic() - started > HISTORY_TIMEOUT or not all(chain.alive().values()):
                raise RuntimeError(f'history replies did not settle: {left}')
            time.sleep(5)
    finally:
        chain.stop()
    with psycopg.connect(pg, autocommit=True) as db:
        outcome = {'seconds': round(time.monotonic() - started, 1), 'jobs': collect.jobs_summary(db), 'run_durations': collect.run_durations(db),
                   'unreplied': history_open(db, tenant)['replies_due'],
                   'replies': db.execute("""select count(*) from runtime.nexloop_conversation_messages where tenant_id=%s
                       and record->>'direction'='outbound' and record->>'trigger_message_id' is not null""", (tenant,)).fetchone()[0]}
    return outcome


# The chain's relay identity also holds nexloop.reply.fallback, as message_relay does in the service-grants manifest (v9): the
# reply guarantor's fallback replies are issued with it (without it every fallback failed with fallback_failed escalations).
@pytest.mark.reply_fallback
def test_build_load_template(assembled_message, admin, tmp_path):
    f = assembled_message
    o = f['original']
    tenant, pg = o['tenant'], o['pg']
    out = Path(os.environ['NEXLOOP_LOAD_OUT'])
    seed, consumers = int(os.environ['NEXLOOP_LOAD_SEED']), int(os.environ['NEXLOOP_LOAD_CONSUMERS'])
    lines = synthetic.lines(seed, consumers)
    (out / 'manifest.jsonl').write_text('\n'.join(lines) + '\n')
    *records, trailer = [json.loads(x) for x in lines]
    (out / 'private').mkdir(mode=0o700)
    started = time.monotonic()
    with open_backend(database_url=make_conninfo(pg, user='nexloop_api'), artifact_root=tmp_path / 'load-api', signing_key_file=o['paths']['backend_signing'],
                      signing_key_id='explicit-configuration') as api, open_browser_identity(make_conninfo(pg, user='nexloop_identity')) as pool:
        uow = PostgresBrowserSessionUnitOfWork(pool, tenant_id=tenant, application_id=o['application'])
        chain = LoadChain(f, admin, api, uow)
        loaded = chain.load(records, progress=lambda n, s: print(f'loaded {n}/{consumers} consumers in {s:.0f} s', flush=True))
        (tmp_path / 'commercial').mkdir()
        with commercial_setup(admin, pg, tmp_path / 'commercial', api_pool=api._pool, signer=api._signer, tenant=tenant) as env:
            commercial = load_commercial(env, records)
        publish_workbench_actions(admin, tenant)
        staff_identities = {}
        staff = workbench_members(admin, pg, tenant, tmp_path, identities=staff_identities)
        # The recorder's Action publication applies the assembled trusted configuration: before the service-grants manifest.
        recorder_token = publish_recorder(f, admin)
        granted = apply_service_grants(admin, pg, out / 'private', tenant=tenant)
        # The chain's private files are needed by the history step (scripts/load/stack.py reads them from the template).
        private(out / 'private/backend-signing-key', Path(o['paths']['backend_signing']).read_bytes())
        private(out / 'private/chain.json', json.dumps({'tenant': tenant, 'browser_application': o['application'], 'signing_key_id': 'explicit-configuration',
            'run_profile': chain.run_profile, 'tokens': {'route': chain.route_token, 'planner': chain.planner_token, 'executor': chain.executor_token,
            'runtime_worker': chain.worker_token, 'source': chain.source_token, 'recorder': recorder_token}}))
        history = reply_history(out, pg, tenant, tmp_path / 'history-lane')
        print(f'history replied: {history["replies"]} replies, {history["unreplied"]} unreplied, in {history["seconds"]} s; jobs {history["jobs"]}', flush=True)
        promised = load_commitments(None, admin, pg, records, out / 'private', tmp_path / 'commitments', staff['operator-1'], tenant=tenant,
                                    signer=api._signer, identity_dsn=make_conninfo(pg, user='nexloop_identity'), browser_app=o['application'])
        # Active plans, data only (dispatcher 2026-10-11 option B): established after the history replies, never reevaluated in a cell.
        plan_token = json.loads((out / 'private/service-tokens.json').read_text())['plan_reevaluator']
        with open_backend(database_url=make_conninfo(pg, user='nexloop_domain_worker'), artifact_root=tmp_path / 'plan-worker',
                          signing_key_file=o['paths']['backend_signing'], signing_key_id='explicit-configuration') as worker:
            plans = load_plans(admin, api, worker, pool, tenant, staff_identities['owner'], plan_token, loaded['people'])
    load_seconds = round(time.monotonic() - started, 1)
    # The login rate policy the cells run under: the trusted configuration's, never relaxed for the load (each cell asserts it).
    login_policy = admin.execute("select maximum_attempts,window_seconds,active from control.nexloop_browser_rate_policies "
                                 "where tenant_id=%s and action='local_login' and operator_principal_id='nexloop_identity'", (tenant,)).fetchone()
    backup_seconds = snapshot(pg, out)
    people = loaded['people']
    private(out / 'private/credentials.json', json.dumps({
        'customers': [{'username': p['identity'][3].username, 'password': p['identity'][-1], 'principal_id': p['identity'][2].principal_id,
                       'consumer_id': p['consumer']} for _, p in people],
        'workbench': staff}))
    commit = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
    catalog = admin.execute('select max(version) from control.schema_migrations').fetchone()[0]
    (out / 'template.json').write_text(json.dumps({'schema': 'nexloop.load-template.v2', 'seed': seed, 'manifest_digest': synthetic.digest(lines),
        'manifest_totals': trailer['totals'], 'tenant': tenant, 'world': 'real',
        'loaded': {'consumers': loaded['consumers'], 'enrolled': loaded['enrolled'], 'messages': loaded['messages'], 'commercial_events': commercial['statuses'],
                   'history_replies': history['replies'], 'history_unreplied': history['unreplied'], 'history_jobs': history['jobs'], 'history_run_durations': history['run_durations'],
                   'active_plans': plans['established'], 'commitments_registered': promised['registered'], 'staff_promises': promised['staff_replies'], 'extraction_tasks': promised['extraction_tasks']},
        'seconds': {**loaded['seconds'], **{'commercial_' + k: v for k, v in commercial['seconds'].items()},
                    **{'commitments_' + k: v for k, v in promised['seconds'].items()}, 'history_replies': history['seconds'], **{'plans_' + k: v for k, v in plans['seconds'].items()}, 'total_load': load_seconds, 'pg_basebackup': backup_seconds},
        'login_rate_policy': {'maximum_attempts': login_policy[0], 'window_seconds': login_policy[1], 'active': login_policy[2]} if login_policy else None,
        'workbench_members': {'owner': 1, 'operator': 2}, 'service_grants': granted, 'commit': commit, 'catalog_revision': catalog,
        'not_loaded': [], 'plans': 'data only: reassess_at null, the cells run no plan reevaluator (no plan Run)'}, ensure_ascii=False, indent=2) + '\n')
