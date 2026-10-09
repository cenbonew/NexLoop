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
