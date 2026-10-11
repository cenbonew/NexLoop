"""AT-043 wiring: the owner issues a catalog incentive from the workbench (real workbench login, CSRF, Idempotency-Key, the
production app); an operator holding the workbench but not the owner-only grant is refused (workbench-roles v8).
Synthetic data; admin seeds configuration fixtures (catalog, budget) and probes."""
import pytest
from commercial_fixture import publish_registry_actions
from commitment_fixture import commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor,execution_plan  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_workbench_ui_pg import Page,ui  # noqa: F401
from test_incentives_pg import seed_budget,seed_offering

pytestmark=pytest.mark.parametrize('execution_plan',['commitment-service'],indirect=True)


def test_the_owner_issues_from_the_workbench_and_an_operator_is_refused(ui,admin):
    c=ui;publish_registry_actions(admin,('nexloop.incentive.issue',))
    seed_budget(admin,1000);offer=seed_offering(c,admin)
    body={'consumer_id':c['consumer'],**offer,'amount_minor':'300','currency':'CNY','notification_text':''}
    with c['app']() as client:
        operator=Page(client,c['staff']['operator'])
        assert 'eios:action:nexloop.incentive.issue:1' not in operator.get('me').json()['actions']
        assert operator.act('issue_incentive',body).status_code==403
    assert admin.execute('select count(*) from runtime.nexloop_incentive_issues').fetchone()==(0,)
    with c['app']() as client:
        owner=Page(client,c['staff']['owner'])
        assert 'eios:action:nexloop.incentive.issue:1' in owner.get('me').json()['actions']
        response=owner.act('issue_incentive',body,key='wb-incentive-0001-synthetic')
        assert response.status_code==200,response.text
        assert owner.act('issue_incentive',body,key='wb-incentive-0001-synthetic').status_code==200  # replay of the same submission
        assert owner.act('issue_incentive',{**body,'amount_minor':'1.5'}).status_code==422
    rows=admin.execute('select initiator,origin,state,amount_minor,principal_id from runtime.nexloop_incentive_issues').fetchall()
    assert rows==[('human','human','accepted',300,c['staff']['owner']['principal_id'])]
