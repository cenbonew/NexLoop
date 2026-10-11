"""AT-043 (0160): governed incentive issue under the tenant incentive budget, on clean catalog PostgreSQL.

The human owner asks through the governed entry; the executor service admits each intent at dispatch (controls,
contact restriction with a notification, catalog, per-Consumer limit, then the budget) and calls a loopback ledger
once. Catalog objects and the budget row are seeded as their governed writers leave them. Synthetic data only."""
from datetime import UTC,datetime,timedelta
import secrets

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios.assembly import open_core
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.incentives import IncentiveIssuer,IncentivePort,LocalIncentiveLedger
from nexloop_eios.plan_reevaluation import PlanUnavailable
from commercial_fixture import TENANT,commercial_setup,publish_registry_actions
from commitment_fixture import commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor,execution_plan  # noqa: F401
from multi_authority_fixture import seed_multi_authority
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401

pytestmark=pytest.mark.parametrize('execution_plan',['commitment-service'],indirect=True)
ACTIONS=('nexloop.incentive.issue',)
ISSUER=[('eios:action:nexloop.incentive.execute:1',ResourceType.ACTION,Operation.EXECUTE)]


@pytest.fixture
def incentives(commitments,admin,pg,tmp_path,identity,uow):
    c=commitments
    with open_core(make_conninfo(pg,user='nexloop_api')) as api,commercial_setup(admin,pg,tmp_path,api_pool=api,signer=c['signer']) as env:
        publish_registry_actions(admin,ACTIONS);c['extra_human_actions']=ACTIONS
        _,token=seed_multi_authority(admin,env['worker'],ISSUER,identity_suffix='-incentive-issuer',tenant=TENANT)
        service=c.service_actions('-incentive-service')
        owner,_=c.owner(identity,uow)
        c.update(env=env,owner_actions=owner,service_actions=c.service_actions_session(service),
            issuer_session=lambda:authenticate_service(env['worker'],token,world='real'),ledger=LocalIncentiveLedger(tmp_path/'incentive-ledger'))
        yield c


def seed_budget(admin,limit):
    now=datetime.now(UTC)
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)",(TENANT,))
        admin.execute("""insert into control.nexloop_budget_limits(tenant_id,world,budget_kind,unit,limit_amount,period_start,period_end,revision)
            values(%s,'real','incentive','CNY',%s,%s,%s,1) on conflict(tenant_id,world,budget_kind) do update set limit_amount=excluded.limit_amount""",
            (TENANT,str(limit),now-timedelta(days=1),now+timedelta(days=30)))


def seed_offering(c,admin,*,max_amount=500,per_consumer=1000,currency='CNY',active=True,consumer=None):
    """An IncentiveOffering and its binding to the Consumer (as the governed object create Actions write them)."""
    consumer=consumer or c['consumer'];offering,binding=secrets.token_hex(32),secrets.token_hex(32)
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute('''insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
            values(%s,'real','IncentiveOffering',%s,1,%s,'nexloop-action','synthetic',now(),now())''',(TENANT,offering,Jsonb({'incentive_code':'welcome-credit','kind':'fixed_amount',
            'max_amount_minor':str(max_amount),'per_consumer_period_limit_minor':str(per_consumer),'currency':currency,'eligibility':'bound_consumer',
            'title':'测试优惠','active':active,'valid_until':(datetime.now(UTC)+timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%SZ')})))
        admin.execute('''insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
            values(%s,'real','ConsumerIncentiveOffering',%s,1,%s,'nexloop-action','synthetic',now(),now())''',(TENANT,binding,Jsonb({'consumer_id':consumer,
            'offering_id':offering,'offering_revision':1,'source_principal':'synthetic-owner-principal','active':True})))
    return {'incentive_offering_id':offering,'binding_id':binding,'offering_revision':1}


def issue(c,request_id,offer,amount,*,currency='CNY',notification='',actions=None):
    return (actions or c['owner_actions']).issue_incentive(action_name='nexloop.incentive.issue',action_version=1,request_id=request_id,
        consumer_id=c['consumer'],amount_minor=amount,currency=currency,notification_text=notification,**offer)


def tick(c,ledger=None):
    return IncentiveIssuer(c['env']['worker'],c['issuer_session'](),c['env']['signer'],ledger or c['ledger']).run_once()


def intent(admin,request_id):
    return admin.execute('select intent_id,state,refusal_code,amount_minor from runtime.nexloop_incentive_issues where intent_id like %s',('%'+request_id+'%',)).fetchone()


def states(admin):
    return [(r[0],r[1]) for r in admin.execute('select state,coalesce(refusal_code,\'\') from runtime.nexloop_incentive_issues order by created_at').fetchall()]


def money(admin):
    """Budget consumption and discount cost entries, in recording order."""
    return (admin.execute("select consumption_id,amount::text from control.nexloop_budget_consumption where budget_kind='incentive' order by recorded_at,consumption_id").fetchall(),
            admin.execute("select basis,amount::text from runtime.nexloop_cost_entries where cost_kind='discount' order by recorded_at,entry_id").fetchall())


def test_an_issue_within_budget_is_reserved_issued_once_and_costed(incentives,admin):
    c=incentives;seed_budget(admin,1000);offer=seed_offering(c,admin)
    out=issue(c,'inc-1',offer,300)
    assert out['state']=='accepted' and out['replayed'] is False
    # Accepted is not issued: nothing reserved, nothing sent.
    assert money(admin)==([],[]) and c['ledger'].requests==[]
    assert tick(c)=={'admitted':1,'refused':0,'fulfilled':1,'failed':0,'unknown':0}
    iid=intent(admin,'inc-1')[0]
    assert intent(admin,'inc-1')[1]=='fulfilled' and c['ledger'].requests==[('issue',iid)]
    assert money(admin)==([('incentive:'+iid,'300')],[('budget_reservation','300.00000000')])
    assert admin.execute('select reserved::text,released::text,outcome from runtime.nexloop_budget_settlements where consumption_id=%s',('incentive:'+iid,)).fetchone()==('300','0','fully_used')
    # The same request replays; nothing is issued twice, another tick finds nothing due.
    assert issue(c,'inc-1',offer,300)['replayed'] is True
    assert tick(c)=={'admitted':0,'refused':0,'fulfilled':0,'failed':0,'unknown':0} and len(c['ledger'].requests)==1
    with pytest.raises(Exception):issue(c,'inc-1',offer,301)  # same request, other parameters


def test_an_exhausted_budget_refuses_before_the_provider_and_keeps_what_was_issued(incentives,admin):
    c=incentives;seed_budget(admin,500);offer=seed_offering(c,admin,per_consumer=5000)
    issue(c,'inc-a',offer,300);tick(c)
    issue(c,'inc-b',offer,300)
    assert tick(c)['refused']==1
    assert states(admin)==[('fulfilled',''),('refused','budget_exhausted')]
    assert len(c['ledger'].requests)==1  # zero provider requests for the refused one
    first=intent(admin,'inc-a')[0]
    assert money(admin)==([('incentive:'+first,'300')],[('budget_reservation','300.00000000')])
    # The owner lowers the limit below what is used: new issues are refused, the issued one stays.
    seed_budget(admin,100)
    issue(c,'inc-c',offer,50);tick(c)
    assert states(admin)[-1]==('refused','budget_exhausted') and intent(admin,'inc-a')[1]=='fulfilled'
    assert money(admin)[0]==[('incentive:'+first,'300')]


def test_an_unconfigured_budget_refuses(incentives,admin):
    c=incentives;offer=seed_offering(c,admin)
    issue(c,'inc-u',offer,100);tick(c)
    assert states(admin)==[('refused','budget_unconfigured')] and c['ledger'].requests==[]


def test_an_unknown_result_is_queried_and_never_sent_again(incentives,admin,tmp_path):
    c=incentives;seed_budget(admin,1000);offer=seed_offering(c,admin)
    ledger=LocalIncentiveLedger(tmp_path/'ledger-a',fault='timeout_after_write')
    issue(c,'inc-x',offer,200);tick(c,ledger)
    iid,state,_,_=intent(admin,'inc-x')
    assert state=='unknown' and money(admin)[0]==[('incentive:'+iid,'200')]
    assert admin.execute('select count(*) from runtime.nexloop_budget_settlements').fetchone()==(0,)  # reserved, not settled
    # An observation under a stale fence is refused.
    port=IncentivePort(c['env']['worker'],c['issuer_session'](),c['env']['signer'])
    with pytest.raises(Exception,match='fenced'):port.observe(iid,0,'failed')
    ledger.fault=None
    assert tick(c,ledger)['fulfilled']==1
    assert ledger.requests==[('issue',iid),('query',iid)] and intent(admin,'inc-x')[1]=='fulfilled'
    # The same failure before the record was durable: the query finds nothing, a definite failure, released (D6).
    ledger_b=LocalIncentiveLedger(tmp_path/'ledger-b',fault='timeout_before_write')
    issue(c,'inc-y',offer,150);tick(c,ledger_b)
    jid=intent(admin,'inc-y')[0]
    assert intent(admin,'inc-y')[1]=='unknown'
    ledger_b.fault=None;tick(c,ledger_b)
    assert intent(admin,'inc-y')[1]=='failed' and ledger_b.requests==[('issue',jid),('query',jid)]
    consumption,costs=money(admin)
    assert ('incentive:'+jid,'150') in consumption and ('incentive:'+jid+':settle','-150') in consumption
    assert ('budget_reservation','150.00000000') in costs and ('budget_release','-150.00000000') in costs


def test_a_definite_failure_releases_the_budget_with_a_negative_discount_entry(incentives,admin,tmp_path):
    from decimal import Decimal
    c=incentives;env=c['env'];seed_budget(admin,300);offer=seed_offering(c,admin)
    now=datetime.now(UTC)
    env.metric('discount-cost',source_kind='cost_entry',kinds=(),statuses=(),cost_kinds=('discount',))
    env.kr('g-discount','discount-cost',(now-timedelta(days=1),now+timedelta(days=1)))
    failing=LocalIncentiveLedger(tmp_path/'ledger-f',fault='fail')
    issue(c,'inc-f',offer,300);tick(c,failing)
    fid=intent(admin,'inc-f')[0]
    assert intent(admin,'inc-f')[1]=='failed' and failing.requests==[('issue',fid)]
    assert admin.execute('select reserved::text,released::text,outcome from runtime.nexloop_budget_settlements where consumption_id=%s',('incentive:'+fid,)).fetchone()==('300','300','released')
    assert admin.execute("select sum(amount) from runtime.nexloop_cost_entries where cost_kind='discount'").fetchone()[0]==0
    assert Decimal(env.compute('g-discount')['value'])==0  # the cost metric nets to zero (D6)
    # Released budget is usable again: the full amount fits.
    issue(c,'inc-g',offer,300);tick(c)
    assert intent(admin,'inc-g')[1]=='fulfilled' and Decimal(env.compute('g-discount')['value'])==300


def test_outside_the_catalog_nothing_is_accepted(incentives,admin):
    c=incentives;seed_budget(admin,10000);offer=seed_offering(c,admin,max_amount=500)
    other=seed_offering(c,admin,consumer=secrets.token_hex(32))
    inactive=seed_offering(c,admin,active=False)
    for request_id,args,kw in (('o1',offer,{'amount':501}),('o2',offer,{'amount':100,'currency':'USD'}),('o3',{**offer,'offering_revision':2},{'amount':100}),
                              ('o4',other,{'amount':100}),('o5',inactive,{'amount':100})):
        with pytest.raises(Exception):issue(c,request_id,args,kw['amount'],currency=kw.get('currency','CNY'))
    assert admin.execute('select count(*) from runtime.nexloop_incentive_issues').fetchone()==(0,)
    assert money(admin)==([],[]) and c['ledger'].requests==[]


def test_the_per_consumer_period_limit_is_checked_at_dispatch(incentives,admin):
    c=incentives;seed_budget(admin,10000);offer=seed_offering(c,admin,max_amount=300,per_consumer=500)
    issue(c,'p1',offer,300);issue(c,'p2',offer,300);tick(c)
    assert states(admin)==[('fulfilled',''),('refused','consumer_incentive_limit')] and len(c['ledger'].requests)==1


def test_controls_contact_restriction_and_erasure_refuse_at_dispatch(incentives,admin):
    from erasure_support import mark_erasing
    c=incentives;seed_budget(admin,10000);offer=seed_offering(c,admin)
    # Accepted before a pause: the pause decides at dispatch.
    issue(c,'c1',offer,100)
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)",(TENANT,))
        admin.execute("insert into control.nexloop_control_scopes(tenant_id,world,scope_kind,scope_ref,paused,revision,reason) values(%s,'real','consumer',%s,true,1,'synthetic pause')",(TENANT,c['consumer']))
    tick(c)
    assert states(admin)==[('refused','control_paused')]
    with admin.transaction():  # disposable-database fault: lift the pause (the governed resume is covered by NX-022 tests)
        admin.execute('set local session_replication_role=replica');admin.execute("delete from control.nexloop_control_scopes where scope_ref=%s",(c['consumer'],))
    # Restricted customer (ADR-023 §3 / NX-026 D6): the issue alone passes, one with a notification is refused whole.
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)",(TENANT,))
        admin.execute("""insert into control.nexloop_contact_restrictions(tenant_id,world,consumer_id,active,control_revision,message_id,conversation_id,rule_version,rule_id,certainty,matched_text,restricted_at)
            values(%s,'real',%s,true,1,'synthetic','',0,'synthetic','refuse','',clock_timestamp())""",(TENANT,c['consumer']))
    issue(c,'c2',offer,100,notification='您的优惠已到账');issue(c,'c3',offer,100);tick(c)
    assert states(admin)[1:]==[('refused','attached_notification'),('fulfilled','')]
    # An erasing Consumer gets nothing.
    issue(c,'c4',offer,100);mark_erasing(admin,TENANT,c['consumer']);tick(c)
    assert states(admin)[-1]==('refused','consumer_erasing')
    assert len(c['ledger'].requests)==1


def test_only_the_owner_asks_and_only_the_executor_admits(incentives,admin):
    c=incentives;seed_budget(admin,1000);offer=seed_offering(c,admin)
    with pytest.raises(Exception,match='human'):issue(c,'s1',offer,100,actions=c['service_actions'])
    assert admin.execute('select count(*) from runtime.nexloop_incentive_issues').fetchone()==(0,)
    # A service without the executor grant cannot use the port at all.
    _,token=seed_multi_authority(admin,c['env']['worker'],[('eios:action:nexloop.retention.execute:1',ResourceType.ACTION,Operation.EXECUTE)],
        identity_suffix='-not-issuer',tenant=TENANT)
    with pytest.raises(PlanUnavailable):IncentivePort(c['env']['worker'],authenticate_service(c['env']['worker'],token,world='real'),c['env']['signer']).due()
    with pytest.raises(psycopg.errors.InsufficientPrivilege),admin.transaction():
        admin.execute('set local role nexloop_api');admin.execute('select * from runtime.nexloop_incentive_issues')
