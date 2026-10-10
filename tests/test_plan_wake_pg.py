"""NX-025 G5 (dispatcher decision 4): a governed change of an object in an active plan's control snapshot wakes
that plan in the writer's transaction; changes of other objects do not. Clean catalog PostgreSQL.

Built on the NX-020 matching fixture: the Consumer change is a real automatic governed write (Consumer.edit through
EIOS) by the claim matcher; the Product write is a governed create of an object no plan names. The plan is
established through the signed plan port. Synthetic data only; admin seeds fixtures and probes.
"""
import uuid

import pytest
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from multi_authority_fixture import seed_multi_authority
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.plan_reevaluation import PlanPort
from test_claim_matching_pg import decision,env,matcher,obj  # noqa: F401
from test_context_v6_pg import STRATEGY,seed_strategy
from test_plan_reevaluation_pg import GOAL,SETTINGS,publish_goal,step
from nexloop_eios.plan_reevaluation import plan_settings

PLAN_TARGETS=[('eios:action:NexLoop.feed.plan-reevaluate:1',ResourceType.ACTION,Operation.EXECUTE),('eios:action:nexloop.plan.reevaluate:1',ResourceType.ACTION,Operation.EXECUTE)]


@pytest.fixture
def waking(env):
    f=env;admin=f['admin']
    seed_strategy(admin,f['tenant'],STRATEGY);publish_goal(admin,f['tenant'],0,'v1')
    _,token=seed_multi_authority(admin,f['worker'],PLAN_TARGETS,identity_suffix='-plan-wake',tenant=f['tenant'])
    def port():return PlanPort(f['worker'],authenticate_service(f['worker'],token,world='real'),f['signer'])
    s={'plan_id':str(uuid.uuid4()),'consumer_id':f['consumer'],'goal_version_ref':f'goal:{GOAL}@1','strategy':None,
        'context_strategy_ref':'context-strategy:recent_plus_required@1',
        'recipe':{'role_id':'a'*64,'link_id':'b'*64,'step_id':'c'*64,'offering_id':'d'*64,'binding_id':'e'*64,'control_id':'f'*64},
        'settings':plan_settings(SETTINGS),'steps':[step()]}
    port().establish(s)
    admin.execute("delete from runtime.nexloop_work_feed where feed='plan-reevaluate'")
    yield dict(f,port=port,spec=s)


def plan_feed(admin):
    return admin.execute("select item_key,payload from runtime.nexloop_work_feed where feed='plan-reevaluate'").fetchall()


def test_governed_consumer_edit_wakes_its_plan_and_precheck_sees_the_change(waking):
    f=waking;admin=f['admin'];c=f['claims']
    assert plan_feed(admin)==[]
    claim=c.add('wake-budget','预算区间','两千元以内',quote='预算在两千元以内')
    m,_=matcher(f,{claim:decision('eios:property:Consumer/budget_level','两千元以内')})
    assert list(m.process_conversation(f['conversation'])['applied'].values())==['applied']
    assert obj(admin,f['consumer'])[1]==2
    (key,payload),=plan_feed(admin)
    assert key=='plan:'+f['spec']['plan_id']
    trigger=payload['triggers'][-1]
    assert trigger['kind']=='object_changed' and trigger['cause']=='external' and trigger['ref']=='Consumer/'+f['consumer'] and trigger['revision']==2
    # The wake is only a trigger: the deterministic precheck re-derives the change (object revision stale) before any Run.
    decision_=f['port']().precheck(f['spec']['plan_id'],payload['triggers'])
    assert decision_['decision']=='reevaluate' and 'object_revision_stale' in decision_['reasons']


def test_governed_edit_of_an_object_outside_the_snapshot_does_not_wake_plans(waking):
    """Another Consumer (no plan names it) edited through the same governed Consumer.edit: no plan wakes."""
    from nexloop_eios.object_edits import GovernedObjectEditor
    from test_claim_matching_pg import put_object
    f=waking;admin=f['admin']
    other=put_object(admin,f['tenant'],'Consumer','consumer-2',{'display_name':'李娜'})
    targets=[('eios:action:Consumer.edit:1',ResourceType.ACTION,Operation.EXECUTE),('eios:object:Consumer/'+other,ResourceType.OBJECT,Operation.READ),
        ('eios:object:Consumer/'+other,ResourceType.OBJECT,Operation.EDIT)]
    targets+=[(f'eios:property:Consumer/{other}/favorite_sport',ResourceType.PROPERTY,op) for op in (Operation.READ,Operation.EDIT)]
    _,token=seed_multi_authority(admin,f['worker'],targets,identity_suffix='-other-editor',tenant=f['tenant'])
    GovernedObjectEditor(f['worker'],authenticate_service(f['worker'],token,world='real'),f['signer']).edit(action_name='Consumer.edit',action_version=1,
        intent_id='nx025-other-consumer-edit',type_name='Consumer',object_id=other,expected_revision=1,properties={'favorite_sport':'游泳'})
    assert obj(admin,other)[1]==2
    assert plan_feed(admin)==[]
