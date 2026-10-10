"""NX-031 D6 (implemented in NX-030): the Run's summed tool time in its terminal summary, numbers only.

The actual NX-047 chain runs a real Pi Run whose deterministic profile calls the effect tools through the runtime guard; the
guard adds each tool request's time to that Run after answering; the Runtime Dispatcher writes the sum into the task's terminal
result (runtime.jobs.result.tool_timing). Recording is refused to anyone but the guard roles and to Runs of another tenant.
"""
import uuid

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

import test_outbound_messages_pg as chain_module
from local_message_assembly_fixture import assembled_message, business_plan, configured  # noqa: F401


def test_terminal_run_summary_carries_the_summed_tool_time(assembled_message, admin, tmp_path):
    f = assembled_message
    with chain_module.chain(f, admin, tmp_path) as c:
        assert 'succeeded' in c['runtime_output'] and c['calls']
        tenant, run = c['tenant'], c['run']['run_id']
    result = admin.execute("select result from runtime.jobs where tenant_id=%s and result->>'run_id'=%s and status='succeeded'", (tenant, run)).fetchone()[0]
    timing = result['tool_timing']
    assert set(timing) == {'calls', 'total_ms', 'max_ms'} and timing['calls'] >= len(c['calls']) >= 1
    assert 0 < timing['max_ms'] <= timing['total_ms'] < 30000
    row = admin.execute('select calls from runtime.nexloop_run_tool_timings where tenant_id=%s and run_id=%s', (tenant, run)).fetchone()
    assert row == (timing['calls'],)
    # Only the guard roles may record, and only for a Run of their own tenant.
    with psycopg.connect(make_conninfo(f['original']['pg'], user='nexloop_api')) as db, pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute('select authz.nexloop_record_run_tool_timing(%s,%s,%s,%s)', ('x' * 64, 'real', run, 1))
    with psycopg.connect(make_conninfo(f['original']['pg'], user='nexloop_domain_worker')) as db, pytest.raises(psycopg.Error):
        db.execute('select authz.nexloop_record_run_tool_timing(%s,%s,%s,%s)', ('x' * 64, 'real', str(uuid.uuid4()), 1))
