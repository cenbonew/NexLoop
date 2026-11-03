"""Claim-extraction failures by code (migration extraction_failed_codes, temporary number; L6 follow-up to S-DUP 2026-11-03): the worker
finishes a job the recorder refused for citing a context Message as failed / context_cited (not denied, never retried); the data health
extraction block counts failed jobs of 24 hours by code, context_cited_24h, finished_24h, inputs_24h, no_data and idle; alert rules v30
warn on context_cited and on no data (input arrived, nothing measured: not a pass); a tenant with no input is idle, not no data. Real PostgreSQL with the NX-030 observability fixture; admin seeds the
synthetic jobs in replica mode."""
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from psycopg.types.json import Jsonb

from nexloop_eios import claim_extraction_jobs
from nexloop_eios.claim_extraction_jobs import ClaimExtractionWorker
from nexloop_eios.claim_store import ClaimExtractionContextCited, ClaimExtractionDenied
from nexloop_eios.observability import load_rules
from support.tenants import TENANT_B
from test_data_health_pg import health, replica, stale_feed
from test_observability_pg import TENANT, observed  # noqa: F401
from test_workbench_read_pg import browser_business, conversations, identity, published_action, uow, workbench  # noqa: F401

RULES = Path(__file__).resolve().parents[1] / 'deploy/configuration'
NEW = ('extraction_context_cited', 'extraction_no_data')


def job(admin, status, *, code=None, at=None, tenant=TENANT):
    at = at or datetime.now(UTC)
    replica(admin, """insert into runtime.jobs(job_id,invocation_id,capability_name,capability_version,capability_type,execution_mode,tenant_id,actor_id,trace_id,
        request_id,normalized_input,plan_snapshot,status,result,created_at,updated_at,world,queue,attempts,max_attempts)
        values(%s,%s,'nexloop.claim.extract','1','atomic','async',%s,'synthetic','synthetic',%s,'{}','{}',%s,%s,%s,%s,'real','claim-extraction',1,3)""",
        (str(uuid.uuid4()), str(uuid.uuid4()), tenant, str(uuid.uuid4()), status, Jsonb({'code': code} if code else {}), at, at))


def v30():
    return load_rules(RULES / 'alert-rules.v30.json')


def fired(snapshot):
    """As the SQL evaluator (0131): only a JSON number is compared."""
    out = set()
    for r in [x for x in v30()['rules'] if x['rule_id'] in NEW]:
        value = snapshot
        for part in r['metric'].split('.'):
            value = value.get(part) if type(value) is dict else None
        if type(value) in (int, float) and value > r['threshold']:
            out.add(r['rule_id'])
    return out


def test_v30_adds_exactly_the_two_rules_on_v29():
    v29, new = json.loads((RULES / 'alert-rules.v29.json').read_text()), v30()
    assert new['version'] == 30 and len(new['rules']) == len(v29['rules']) + 2 and new['rules'][:-2] == v29['rules']
    assert new['decision'].startswith(v29['decision'] + ' v30 ')
    assert [(r['rule_id'], r['metric'], r['condition'], r['threshold'], r['severity']) for r in new['rules'][-2:]] == [
        ('extraction_context_cited', 'data_health.extraction.context_cited_24h', 'gt', 0, 'warning'),
        ('extraction_no_data', 'data_health.extraction.no_data', 'gt', 0, 'warning')]


def test_no_input_at_all_is_idle_not_no_data(observed):  # noqa: F811
    x = health(observed['admin'], TENANT_B)['extraction']  # the fixture's own tenant has a Message waiting; the other has no traffic
    assert (x['failed_24h'], x['context_cited_24h'], x['finished_24h'], x['inputs_24h'], x['no_data'], x['idle']) == ({}, 0, 0, 0, 0, 1)
    assert fired({'data_health': {'extraction': x}}) == set()  # a quiet tenant raises nothing


def test_input_without_any_finished_job_is_no_data_not_a_pass(observed):  # noqa: F811
    admin = observed['admin']
    before = health(admin)['extraction']['inputs_24h']  # the fixture's Message, if any
    stale_feed(admin, secrets.token_hex(16))  # a Message waiting for extraction (feed row of two hours ago)
    job(admin, 'pending')  # and a job no worker took
    x = health(admin)['extraction']
    assert (x['finished_24h'], x['inputs_24h'], x['no_data'], x['idle']) == (0, before + 2, 1, 0)
    assert fired({'data_health': {'extraction': x}}) == {'extraction_no_data'}
    job(admin, 'succeeded', code='recorded')  # one job ends: measured again
    x = health(admin)['extraction']
    assert (x['finished_24h'], x['no_data'], x['idle']) == (1, 0, 0) and fired({'data_health': {'extraction': x}}) == set()


def test_failed_jobs_count_by_code_and_context_cited_warns(observed):  # noqa: F811
    admin = observed['admin']
    job(admin, 'succeeded', code='recorded')
    job(admin, 'failed', code='context_cited')
    job(admin, 'failed', code='denied')
    job(admin, 'retry_wait', code='provider_timeout')  # not finished
    job(admin, 'failed', code='context_cited', at=datetime.now(UTC) - timedelta(days=2))  # outside the 24 hours
    job(admin, 'failed', code='context_cited', tenant=TENANT_B)  # another tenant
    x = health(admin)['extraction']
    assert (x['failed_24h'], x['context_cited_24h'], x['finished_24h'], x['no_data']) == ({'context_cited': 1, 'denied': 1}, 1, 3, 0)
    assert 'context_cited' in x['reasons'] and x['status'] == 'attention'
    assert fired({'data_health': {'extraction': x}}) == {'extraction_context_cited'}
    theirs = health(admin, TENANT_B)['extraction']
    assert (theirs['failed_24h'], theirs['context_cited_24h']) == ({'context_cited': 1}, 1)


class _Queue:
    def __init__(self):self.finished = None
    def claim(self, *, lease_seconds):return {'task_id': 't', 'fence': 1, 'attempts': 1, 'payload': {'conversation_id': 'c', 'message_ids': ['m']}}
    def finish(self, **kw):self.finished = kw;return {'status': kw['status']}


class _Extractor:
    def __init__(self, error):self.error = error
    def extract(self, **kw):raise self.error


def test_the_worker_finishes_context_cited_failed_and_apart_from_denied():
    for error, code in ((ClaimExtractionContextCited(), 'context_cited'), (ClaimExtractionDenied(), 'denied')):
        worker = object.__new__(ClaimExtractionWorker)
        worker.queue, worker.extractor, worker.lease_seconds, worker.retry_base_seconds = _Queue(), _Extractor(error), 60, 30
        assert worker.run_once() == 'failed'
        assert (worker.queue.finished['status'], worker.queue.finished['result'], worker.queue.finished['retry_seconds']) == ('failed', {'code': code}, 0)
    assert claim_extraction_jobs.ClaimExtractionContextCited is ClaimExtractionContextCited
