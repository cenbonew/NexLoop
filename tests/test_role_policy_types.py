from datetime import UTC,datetime,timedelta
import pytest
from nexloop_eios.role_policies import validate_policy,role_policy_schemas,assert_budget_within

def policy():
    now=datetime.now(UTC)
    return dict(active=True,action_resources=['eios:action:nexloop.service.request:1'],consumer_ids=['c1'],goal_ids=['g1'],step_ids=['s1'],budget=dict(maximum_model_turns=2,maximum_tool_calls=3,active_timeout_seconds=30,maximum_cost='0.10',currency='USD'),effect_units=1,valid_from=now.isoformat(),valid_until=(now+timedelta(minutes=5)).isoformat())

def test_real_eios_json_schemas_and_explicit_policy():
    from eios.ontology.models import PropertyValueType
    schemas=role_policy_schemas()
    assert [s.type_name for s in schemas]==['RoleExecutionCeiling','RoleAssignmentScope']
    assert next(p for p in schemas[0].properties if p.property_name=='action_resources').value_type is PropertyValueType.JSON
    assert validate_policy('RoleExecutionCeiling',policy())['effect_units']==1

@pytest.mark.parametrize('field,value',[('action_resources',['*']),('goal_ids',[]),('consumer_ids',['c1','c1']),('effect_units',True),('valid_from','2026-01-01T00:00:00')])
def test_invalid_policy_fail_closed(field,value):
    p=policy();p[field]=value
    with pytest.raises(ValueError):validate_policy('RoleExecutionCeiling',p)

@pytest.mark.parametrize('field,value',[('maximum_model_turns',3),('maximum_tool_calls',4),('active_timeout_seconds',31),('maximum_cost','0.10000001'),('currency','CNY')])
def test_budget_intersection_rejects_every_excess(field,value):
    b=policy()['budget'];request={**b,field:value}
    with pytest.raises(ValueError):assert_budget_within(request,b)
