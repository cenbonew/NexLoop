"""Dispatch-time Role policy re-check (0082); synthetic PG, owned loopback provider only.

A bound Role Run's next model/start/tool/submit/admit call carries the current signed
Source policy recipe; governed policy EDIT, deactivation, READ revocation or expiry
denies it. effect_units bounds distinct submissions per Run under concurrency.
"""
import pytest
from psycopg.conninfo import make_conninfo
from eios.authz.errors import AuthorizationUnavailable
from runtime_effect_fixture import runtime_effect_plan  # noqa: F401
from role_run_fixture import role_runtime_plan  # noqa: F401
from nexloop_eios.backend import open_backend
from nexloop_eios.effect_dispatch import EffectDispatcher
from nexloop_eios.effect_provider import HttpEffectProvider, EffectProviderConfiguration
from support.effect_provider import effect_provider


def worker(plan):
    # Same genuine Worker; policy publication may change the directory hash.
    plan['worker'] = plan['backend_worker'].authenticate(plan['worker_token'], world='real')
    return plan['worker']


def authorize(plan, operation):
    return worker(plan).authorize_runtime_activation(activation_ref=plan['activations'][0], command=plan['commands'][0], operation=operation)


def submit(plan, message):
    return worker(plan).runtime_effect_tool(activation_ref=plan['activations'][0], command=plan['commands'][0], tool_operation='submit',
                                            parameters={'message': message})


def dispatch(plan, tmp_path):
    with open_backend(database_url=make_conninfo(plan['pg'], user='nexloop_action_worker'), artifact_root=tmp_path / 'executor',
                      signing_key_file=plan['signing_key'], signing_key_id=plan['signing_key_id']) as backend:
        executor = backend.authenticate(plan['executor_token'], world='real')
        with effect_provider(tmp_path / 'provider.sqlite') as provider:
            result = EffectDispatcher(executor, HttpEffectProvider(EffectProviderConfiguration(provider.origin, test_loopback_http=True, timeout=1))).run_once()
            return result, provider.control('snapshot')['requests']


def revoke_ceiling_read(plan, admin):
    principal = plan['sources'][0]._session.authentication.subject_principal_id
    ceiling = plan['role_policies'][principal]['ceiling_id']
    resource = 'eios:property:RoleExecutionCeiling/' + ceiling + '/action_resources'
    assert admin.execute("select 1 from authz.nexloop_authority_facts where tenant_id=%s and fact_kind='grants' and entity_key=%s", (plan['tenant'], [principal, resource])).fetchone()
    admin.execute("update authz.nexloop_authority_facts set payload=jsonb_set(payload,'{grants}','[]'::jsonb) where tenant_id=%s and fact_kind='grants' and entity_key=%s",
                  (plan['tenant'], [principal, resource]))


def expire_binding(plan, admin):
    # Disposable fault injection: the bound policy deadline is reached (object-level
    # valid_until expiry of the policy itself is covered in test_role_policy_enforcement).
    admin.execute("update authz.nexloop_role_policy_bindings set expires_at=clock_timestamp()-interval '1 second' where run_id=%s", (plan['commands'][0]['run_id'],))


CHANGES = {
    'ceiling_deactivated': lambda plan, admin: plan['edit_policy'](0, 'RoleExecutionCeiling', {'active': False}),
    'ceiling_actions_narrowed': lambda plan, admin: plan['edit_policy'](0, 'RoleExecutionCeiling', {'action_resources': ['eios:action:Goal.create:1']}),
    'scope_deactivated': lambda plan, admin: plan['edit_policy'](0, 'RoleAssignmentScope', {'active': False}),
    'ceiling_read_revoked': revoke_ceiling_read,
    'binding_expired': expire_binding,
}


@pytest.mark.parametrize('change', sorted(CHANGES))
def test_policy_change_denies_next_model_start_submit(role_runtime_plan, admin, tmp_path, change):
    plan = role_runtime_plan
    assert authorize(plan, 'model')['authorized']
    CHANGES[change](plan, admin)
    for operation in ('model', 'start', 'tool'):
        with pytest.raises(AuthorizationUnavailable):
            authorize(plan, operation)
    with pytest.raises(Exception):
        submit(plan, 'after Role policy change')
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents where tenant_id=%s', (plan['tenant'],)).fetchone() == (0,)
    result, requests = dispatch(plan, tmp_path)
    assert requests == []


@pytest.mark.parametrize('change', ['ceiling_deactivated', 'binding_expired'])
def test_policy_change_after_submit_denies_admit_without_post(role_runtime_plan, admin, tmp_path, change):
    plan = role_runtime_plan
    receipt = submit(plan, 'submitted before Role policy change')
    assert receipt['receipt']['intent_id']
    CHANGES[change](plan, admin)
    result, requests = dispatch(plan, tmp_path)
    assert requests == []
    state = admin.execute('select state from runtime.nexloop_effect_intents where intent_id=%s', (receipt['receipt']['intent_id'],)).fetchone()[0]
    assert state not in ('dispatching', 'fulfilled', 'observed_fulfilled')


def test_unchanged_policy_dispatches_normally(role_runtime_plan, admin, tmp_path):
    plan = role_runtime_plan
    receipt = submit(plan, 'current Role policy')
    result, requests = dispatch(plan, tmp_path)
    assert result['provider_state'] == 'accepted' and len(requests) == 1
    assert requests[0][1] == receipt['receipt']['intent_id']


# effect_units (owner-confirmed): ceiling on distinct external-effect submissions per Run,
# serialized by the Run-scoped lock. The shared EffectControl is widened so that only the
# Role ceiling can deny.
import threading
import runtime_effect_fixture
import role_run_fixture


@pytest.fixture
def wide_control(monkeypatch):
    monkeypatch.setattr(runtime_effect_fixture, 'EFFECT_BUDGET_UNITS', 3)
    monkeypatch.setattr(role_run_fixture, 'ROLE_EFFECT_UNITS', 1)


def test_effect_units_concurrent_distinct_submissions_stay_within_ceiling(wide_control, role_runtime_plan, admin):
    plan = role_runtime_plan; run_id = plan['commands'][0]['run_id']
    assert admin.execute("select effect_units from authz.nexloop_role_policy_bindings where run_id=%s", (run_id,)).fetchone() == (1,)
    barrier = threading.Barrier(8); outcomes = []
    from test_backend_lifecycle_capacity import retrying
    def attempt(index):
        services = retrying(lambda: plan['backend_worker'].authenticate(plan['worker_token'], world='real'))
        barrier.wait()
        try:
            # BackendBusy (explicit overload on a slow host) is retried; effect outcomes are not.
            retrying(lambda: services.runtime_effect_tool(activation_ref=plan['activations'][0], command=plan['commands'][0], tool_operation='submit',
                                                          parameters={'message': 'distinct concurrent payload ' + str(index)}))
            outcomes.append('ok')
        except Exception as error:
            outcomes.append(type(error).__name__)
    threads = [threading.Thread(target=attempt, args=(index,)) for index in range(8)]
    for thread in threads:thread.start()
    for thread in threads:thread.join(180); assert not thread.is_alive()
    assert outcomes.count('ok') == 1, outcomes
    # Overload is retried (BackendBusy); the only other outcome is the business-level payload conflict.
    assert set(outcomes) <= {'ok', 'EffectIntentConflict'}, outcomes
    assert admin.execute('select count(distinct intent_id) from runtime.nexloop_effect_submissions where run_id=%s', (run_id,)).fetchone() == (1,)
    assert admin.execute("select reserved_units from control.nexloop_effect_control_ledger").fetchone()[0] <= 1


def test_effect_units_ceiling_denies_once_reached(wide_control, role_runtime_plan, admin, tmp_path):
    plan = role_runtime_plan; run_id = plan['commands'][0]['run_id']
    first = submit(plan, 'first external effect')['receipt']['intent_id']
    # Disposable fault injection: the Run already holds a second distinct external-effect
    # submission (a cloned intent in another slot), i.e. effect_units=1 is exhausted.
    second = admin.execute("""insert into runtime.nexloop_effect_intents
        select gen_random_uuid(),gen_random_uuid(),context_id,tenant_id,world,consumer_id,goal_identity,plan_step_identity,slot_identity||':synthetic-second',
               action_name,action_version,business_digest,provider_payload_digest,frozen_request,action_definition,capability,executor_principal,control_revision,created_at,state
        from runtime.nexloop_effect_intents where intent_id=%s returning intent_id""", (first,)).fetchone()[0]
    admin.execute("""insert into runtime.nexloop_effect_submissions
        select %s,run_id,principal_id,source_digest,created_at from runtime.nexloop_effect_submissions where intent_id=%s and run_id=%s""", (second, first, run_id))
    with pytest.raises(Exception):
        submit(plan, 'first external effect')
    import psycopg
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='Role effect unit ceiling exceeded'):
        # The same verdict straight from the SQL tail (tenant context as set by claims_current).
        admin.execute("select set_config('eios.tenant_id',%s,false)", (plan['tenant'],))
        admin.execute("select authz.nexloop_role_policy_claims_tail(%s,%s,%s,%s,true)", (plan['sources'][0]._session.token_digest, 'real', '{}',
                      __import__('json').dumps({'run_id': run_id, 'expires_at': '2999-01-01T00:00:00+00:00'})))
