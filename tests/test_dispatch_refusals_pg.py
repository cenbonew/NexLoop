"""NX-030 D8: dispatch refusals are recorded by the transaction that persists the refusal outcome, on both paths.

The NX-022 dispatch scenarios run unchanged (same outcomes, zero provider requests, no Host start); afterwards the append-only
runtime.nexloop_dispatch_refusals holds exactly one row per refused task (Run path, written in the queue `finish`) and one row
per refused intent and code (effect path, written right after the rolled-back admission), with the stable NXC/NXB codes.
"""
import psycopg
import pytest

from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_runtime_atomic_accept import accepted_input  # noqa: F401
from test_runtime_authority import authority  # noqa: F401
from test_runtime_activation import synthetic_credentials  # noqa: F401
from test_nx022_dispatch_e2e import (
    test_effect_paused_after_enqueue_is_denied_with_zero_effect_then_resume_needs_reevaluation as effect_paused_scenario,
    test_runtime_model_budget_is_reserved_per_run_and_exhaustion_denies as runtime_budget_scenario,
    test_runtime_paused_after_enqueue_is_failed_before_host_start as runtime_paused_scenario)


def refusals(admin):
    return admin.execute('select path,code,task_id is not null,intent_id is not null from runtime.nexloop_dispatch_refusals order by refusal_id').fetchall()


def test_effect_refusals_are_recorded_once_per_intent_and_code(governed_effect_executor, admin, tmp_path):
    effect_paused_scenario(governed_effect_executor, admin, tmp_path)
    # Paused (NXC01), then the queued snapshot predates the resume (NXC02); the successful re-submission records nothing.
    assert refusals(admin) == [('effect', 'NXC01', False, True), ('effect', 'NXC02', False, True)]
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        admin.execute('delete from runtime.nexloop_dispatch_refusals')


def test_run_refusal_is_recorded_in_the_finish_of_the_refused_task(accepted_input, admin, tmp_path):
    runtime_paused_scenario(accepted_input, admin, tmp_path)
    assert refusals(admin) == [('run', 'NXC01', True, False)]
    task = admin.execute("select job_id from runtime.jobs where result->>'code'='control_paused'").fetchone()[0]
    assert admin.execute('select task_id from runtime.nexloop_dispatch_refusals').fetchone()[0] == task


def test_budget_exhaustion_is_recorded_and_a_successful_run_records_nothing(accepted_input, admin, tmp_path):
    runtime_budget_scenario(accepted_input, admin, tmp_path, '0.50', 'budget_exhausted')
    assert refusals(admin) == [('run', 'NXB01', True, False)]


def test_successful_run_records_no_refusal(accepted_input, admin, tmp_path):
    runtime_budget_scenario(accepted_input, admin, tmp_path, '5.00', 'succeeded')
    assert refusals(admin) == []
