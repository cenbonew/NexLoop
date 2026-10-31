"""S77: the incentive catalog through the published governed Actions (IncentiveOffering.create/edit,
ConsumerIncentiveOffering.create/edit) held by a catalog maintainer, on clean catalog PostgreSQL; 0389 is the server-side rule.

The edit Actions are called directly (not through the tool): a changed amount, consumer, offering or revision, a re-activation
and an extra property are refused by the server; other principals hold no permission. The main path issues an incentive from
a catalog created this way (no replica seed). Synthetic data only."""
from datetime import UTC,datetime,timedelta
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios import incentive_setup as S
from multi_authority_fixture import seed_multi_authority
from system_catalog_fixture import publish_system_catalog
from test_incentives_pg import TENANT,incentives,intent,issue,seed_budget,states,tick  # noqa: F401
from commitment_fixture import commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor,execution_plan  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401

PLAN=pytest.mark.parametrize('execution_plan',['commitment-service'],indirect=True)
ROOT=Path(__file__).resolve().parents[1]
ACTIONS=('IncentiveOffering.create','ConsumerIncentiveOffering.create','IncentiveOffering.edit','ConsumerIncentiveOffering.edit')
TYPES=('IncentiveOffering','ConsumerIncentiveOffering')


def lifecycle_rules(admin,principal,tenant=TENANT):
    """The service-grants v24 rule as register compiles it (0084 facts): EDIT/READ derived for the incentive_lifecycle group
    (active) of every real incentive catalog object, and the owner's decision restricting no group of either type."""
    from nexloop_eios.property_access import PropertyAccessRule,PropertyGroupRestriction
    for t in TYPES:
        rule=PropertyAccessRule(tenant_id=tenant,principal_id=principal,type_name=t,operations=('edit','read'),property_groups=('incentive_lifecycle',),
            include_review_published=False,basis_schema_version=2,active=True,valid_until=datetime.now(UTC)+timedelta(days=1))
        admin.execute('''insert into authz.nexloop_authority_facts values(%s,%s,%s,%s)
            on conflict(tenant_id,fact_kind,entity_key) do update set payload=excluded.payload''',(tenant,'property_access_rule',[principal,t],Jsonb(rule.model_dump(mode='json'))))
        admin.execute('''insert into authz.nexloop_authority_facts values(%s,%s,%s,%s)
            on conflict(tenant_id,fact_kind,entity_key) do update set payload=excluded.payload''',
            (tenant,'property_group_restriction',[t],Jsonb(PropertyGroupRestriction(tenant_id=tenant,type_name=t,restricted_groups=(),
                decision='synthetic owner decision').model_dump(mode='json'))))


def expiry(days=30):
    return (datetime.now(UTC)+timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')


def recipe(consumers,**offering):
    return {'schema_version':S.SETUP_SCHEMA,'request_id':str(uuid.uuid4()),'consumers':consumers,
            'offering':{'incentive_code':'s77-test-credit','max_amount_minor':'500','per_consumer_period_limit_minor':'1000','currency':'CNY',
                        'title':'测试优惠','valid_until':expiry(),**offering}}


@pytest.fixture
def incentive_catalog(incentives,admin,tmp_path,identity,uow):  # noqa: F811
    """The four catalog Actions as the system catalog publishes them, and a maintainer holding exactly their EXECUTE."""
    c=incentives;backend=c['plan']['backend']
    present={t for (t,) in admin.execute("select type_name from ontology.object_type_versions where tenant_id=%s and version=2 and type_name in ('IncentiveOffering','ConsumerIncentiveOffering')",(TENANT,))}
    publish_system_catalog(admin,TENANT,[t for t in TYPES if t not in present],ACTIONS)
    _,token=seed_multi_authority(admin,backend._pool,[('eios:action:'+name+':1',ResourceType.ACTION,Operation.EXECUTE) for name in ACTIONS]
                                 +[('eios:object_type:'+t,ResourceType.OBJECT_TYPE,Operation.READ) for t in TYPES],identity_suffix='-incentive-maintainer',tenant=TENANT)
    principal=backend.authenticate(token,world='real')._session.authentication.subject_principal_id
    lifecycle_rules(admin,principal)
    c['owner_actions'],_=c.owner(identity,uow)  # a fresh owner session after the authority facts changed
    path=tmp_path/'incentive-maintainer';path.write_text(token);path.chmod(0o600)
    c.update(maintainer_file=path,maintainer_token=token,maintainer=lambda:backend.authenticate(token,world='real'))
    return c


def setup(c,value):
    return S.setup_incentives(c['plan']['backend'],maintainer_credential_file=c['maintainer_file'],recipe=S.setup_recipe(json.dumps(value)))


def offer(result,binding=0):
    return {'incentive_offering_id':result['offering_id'],'binding_id':result['bindings'][binding]['binding_id'],'offering_revision':1}


def obj(admin,object_id):
    return admin.execute('select nexloop_revision,properties from ontology.objects where object_id=%s',(object_id,)).fetchone()


@PLAN
def test_a_governed_catalog_issues_once_and_replays(incentive_catalog,admin):
    c=incentive_catalog;value=recipe([c['consumer']])
    first=setup(c,value)
    assert first['configured'] is True and first['dispatch_authorized'] is False and first['bindings'][0]['consumer_id']==c['consumer']
    assert setup(c,value)==first  # same intents: the same objects
    revision,binding=obj(admin,first['bindings'][0]['binding_id'])
    assert revision==1 and binding['source_principal']==c['maintainer']()._session.authentication.subject_principal_id
    seed_budget(admin,1000)
    out=issue(c,'s77-1',offer(first),300)
    assert out['state']=='accepted'
    assert tick(c)['fulfilled']==1 and intent(admin,'s77-1')[1]=='fulfilled'
    # A changed recipe under the same request is refused by the governed intent, not overwritten.
    changed={**value,'offering':{**value['offering'],'title':'另一个标题'}}
    with pytest.raises(S.IncentiveSetupRefused) as refused:setup(c,changed)
    assert (refused.value.code,refused.value.step)==('refused','offering') and obj(admin,first['offering_id'])[0]==1


@PLAN
def test_the_edit_actions_called_directly_allow_only_a_withdrawal(incentive_catalog,admin):
    c=incentive_catalog;result=setup(c,recipe([c['consumer']]));m=c['maintainer']()
    offering,binding=result['offering_id'],result['bindings'][0]['binding_id']
    def edit(type_name,object_id,patch,revision=1,intent=None):
        return m.edit_object(action_name=type_name+'.edit',action_version=1,intent_id=intent or 'edit-'+uuid.uuid4().hex[:12],type_name=type_name,
                             object_id=object_id,expected_revision=revision,properties=patch)
    refused=[('IncentiveOffering',offering,{'max_amount_minor':'900'}),('IncentiveOffering',offering,{'valid_until':expiry(10)}),
             ('IncentiveOffering',offering,{'active':False,'title':'改名'}),('IncentiveOffering',offering,{'active':False,'extra':'1'}),
             ('ConsumerIncentiveOffering',binding,{'consumer_id':'e'*64}),('ConsumerIncentiveOffering',binding,{'offering_id':'d'*64}),
             ('ConsumerIncentiveOffering',binding,{'offering_revision':2}),('ConsumerIncentiveOffering',binding,{'active':False,'source_principal':'x'})]
    for type_name,object_id,patch in refused:
        with pytest.raises(Exception):edit(type_name,object_id,patch)
    assert obj(admin,offering)[0]==1 and obj(admin,binding)[0]==1
    # The binding withdrawn (true -> false): the admission refuses it; it cannot be switched on again.
    assert edit('ConsumerIncentiveOffering',binding,{'active':False})['revision']==2
    with pytest.raises(Exception):edit('ConsumerIncentiveOffering',binding,{'active':True},revision=2)
    assert obj(admin,binding)==(2,{**obj(admin,binding)[1],'active':False})
    seed_budget(admin,1000)
    with pytest.raises(psycopg.Error,match='incentive outside catalog: binding'):issue(c,'s77-w1',offer(result),100)
    # The offering withdrawn: every binding of it is outside the catalog; a second withdrawal is refused (already false).
    assert edit('IncentiveOffering',offering,{'active':False})['revision']==2
    with pytest.raises(Exception):edit('IncentiveOffering',offering,{'active':False},revision=2)
    with pytest.raises(Exception):edit('IncentiveOffering',offering,{'active':True},revision=2)
    assert obj(admin,offering)[0]==2


@PLAN
def test_other_principals_cannot_create_or_edit(incentive_catalog,admin):
    c=incentive_catalog;result=setup(c,recipe([c['consumer']]));plan=c['plan']
    other=plan['backend'].authenticate(plan['submitter_token'],world='real')
    with pytest.raises(Exception):
        other.edit_object(action_name='IncentiveOffering.edit',action_version=1,intent_id='other-edit',type_name='IncentiveOffering',
                          object_id=result['offering_id'],expected_revision=1,properties={'active':False})
    with pytest.raises(Exception):
        other.create_object(action_name='IncentiveOffering.create',action_version=1,intent_id='other-create',type_name='IncentiveOffering',
                            properties=S.offering_properties(recipe([c['consumer']])['offering']))
    assert obj(admin,result['offering_id'])[0]==1
    assert admin.execute("select count(*) from ontology.objects where type_name='IncentiveOffering'").fetchone()==(1,)


@PLAN
def test_the_server_refuses_a_binding_to_no_current_consumer_and_any_other_shape(incentive_catalog,admin):
    from erasure_support import mark_erasing
    c=incentive_catalog
    # An unknown Consumer: refused by 0389 at create (the maintainer cannot read Consumers; the guard decides), reported by step.
    with pytest.raises(S.IncentiveSetupRefused) as refused:setup(c,recipe([c['consumer'],'f'*64]))
    assert (refused.value.step,refused.value.index)==('binding',1)
    mark_erasing(admin,TENANT,c['consumer'])
    with pytest.raises(S.IncentiveSetupRefused) as refused:setup(c,recipe([c['consumer']]))
    assert (refused.value.step,refused.value.index)==('binding',0)
    assert admin.execute("select count(*) from ontology.objects where type_name='ConsumerIncentiveOffering'").fetchone()==(1,)  # only the first run's
    # Real world only; the exact offering shape (written directly, the guard still runs).
    good=S.offering_properties(recipe([c['consumer']])['offering'])
    for world,props in (('test',good),('real',{**good,'kind':'percent'}),('real',{**good,'per_consumer_period_limit_minor':'100'}),
                        ('real',{**good,'active':False}),('real',{**good,'valid_until':'2020-01-01T00:00:00Z'}),('real',{**good,'extra':'1'})):
        with pytest.raises(psycopg.Error),admin.transaction():
            admin.execute('''insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
                values(%s,%s,'IncentiveOffering',%s,1,%s,'nexloop-action','synthetic',now(),now())''',(TENANT,world,uuid.uuid4().hex*2,Jsonb(props)))


@PLAN
def test_an_erased_consumer_loses_the_binding_and_gets_nothing(incentive_catalog,admin):
    c=incentive_catalog;consumer=c['consumer'];result=setup(c,recipe([consumer]));binding=result['bindings'][0]['binding_id']
    request='erase-s77'
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)",(TENANT,))
        admin.execute("""insert into control.nexloop_consumer_erasures(tenant_id,world,request_id,consumer_id,source,requested_by,reason,state,phase,watermark)
            values(%s,'real',%s,%s,'owner','synthetic-owner','synthetic erasure','purging','messages',nextval('control.nexloop_deletion_watermark'))""",(TENANT,request,consumer))
    for _ in range(40):
        with admin.transaction():
            admin.execute('set local role nexloop_owner')
            out=admin.execute("select runtime.nexloop_erasure_step(%s,'real',%s,200,'synthetic-keeper')",(TENANT,request)).fetchone()[0]
        if out.get('state')=='completed':break
    assert out['state']=='completed',out
    assert obj(admin,binding) is None and obj(admin,result['offering_id']) is not None  # the offering names no one
    seed_budget(admin,1000)
    with pytest.raises(Exception):issue(c,'s77-e1',offer(result),100)


def test_recipes_and_ceilings(tmp_path):
    good=recipe(['a'*64])
    assert S.setup_recipe(json.dumps(good))['consumers']==['a'*64]
    def code(value,**kw):
        with pytest.raises(S.IncentiveSetupRefused) as refused:S.setup_recipe(json.dumps(value),**kw)
        return refused.value.code
    o=good['offering']
    for bad in ({**good,'extra':1},{**good,'consumers':[]},{**good,'consumers':['a'*64,'a'*64]},{**good,'consumers':['A'*64]},
                {**good,'offering':{**o,'kind':'fixed_amount'}},{**good,'offering':{**o,'max_amount_minor':'0'}},
                {**good,'offering':{**o,'max_amount_minor':500}},{**good,'offering':{**o,'per_consumer_period_limit_minor':'100'}},
                {**good,'offering':{**o,'valid_until':'2026-12-01T00:00:00+08:00'}},{**good,'offering':{**o,'valid_until':'2020-01-01T00:00:00Z'}},
                {**good,'schema_version':'nexloop-incentive-setup/2'},{**good,'request_id':'x'}):
        assert code(bad)=='recipe_invalid',bad
    assert code({**good,'offering':{**o,'max_amount_minor':'10001','per_consumer_period_limit_minor':'20000'}})=='amount_above_ceiling'
    assert S.setup_recipe(json.dumps({**good,'offering':{**o,'max_amount_minor':'10001','per_consumer_period_limit_minor':'20000'}}),max_amount_minor=20000)
    assert code({**good,'offering':{**o,'valid_until':expiry(91)}})=='validity_above_ceiling'
    assert S.setup_recipe(json.dumps({**good,'offering':{**o,'valid_until':expiry(91)}}),max_validity_days=120)
    w={'schema_version':S.WITHDRAW_SCHEMA,'request_id':str(uuid.uuid4()),'target':'binding','object_id':'b'*64,'expected_revision':1}
    assert S.withdraw_recipe(json.dumps(w))['target']=='binding'
    for bad in ({**w,'target':'consumer'},{**w,'expected_revision':0},{**w,'active':True}):
        with pytest.raises(S.IncentiveSetupRefused):S.withdraw_recipe(json.dumps(bad))
    # The example recipes in deploy/examples are valid (amounts in minor units).
    example=json.loads((ROOT/'deploy/examples/incentive-setup.example.json').read_text())
    example['offering']['valid_until']=expiry();assert S.setup_recipe(json.dumps(example))
    assert S.withdraw_recipe((ROOT/'deploy/examples/incentive-withdraw.example.json').read_text())


@PLAN
def test_the_cli_sets_up_withdraws_and_prints_no_credential(incentive_catalog,admin,pg,tmp_path):
    c=incentive_catalog;plan=c['plan']
    def private(name,text):
        path=tmp_path/name;path.write_text(text);path.chmod(0o600);return path
    dsn=private('incentive-dsn',make_conninfo(pg,user='nexloop_api'))
    def run(command,value,*extra):
        args=[sys.executable,'-m','nexloop_eios.incentive_setup',command,'--database-url-file',str(dsn),'--signing-key-file',str(plan['key']),
              '--signing-key-id','synthetic-plan','--maintainer-credential-file',str(c['maintainer_file']),
              '--recipe-file',str(private(f'{command}-{uuid.uuid4().hex[:8]}',json.dumps(value))),'--artifact-root',str(tmp_path/'incentive-cli'),*extra]
        result=subprocess.run(args,env={'PATH':os.environ['PATH'],'PYTHONPATH':str(ROOT/'packages/eios-core/src')},capture_output=True,text=True,timeout=30)
        assert c['maintainer_token'] not in result.stdout+result.stderr
        return result.returncode,json.loads(result.stdout)
    value=recipe([c['consumer']])
    assert run('setup',value,'--dry-run')==(0,{'command':'setup','consumers':1,'dry_run':True})
    assert admin.execute("select count(*) from ontology.objects where type_name='IncentiveOffering'").fetchone()==(0,)
    code,out=run('setup',value);assert code==0 and out['configured'] is True,out
    assert run('setup',value)==(0,out)
    code,bad=run('setup',{**value,'offering':{**value['offering'],'max_amount_minor':'20000','per_consumer_period_limit_minor':'20000'}})
    assert (code,bad)==(1,{'code':'amount_above_ceiling','configured':False})
    w={'schema_version':S.WITHDRAW_SCHEMA,'request_id':str(uuid.uuid4()),'target':'offering','object_id':out['offering_id'],'expected_revision':1}
    code,done=run('withdraw',w);assert code==0 and done=={'object_id':out['offering_id'],'revision':2,'target':'offering','withdrawn':True}
    assert run('withdraw',w)[0]==0  # replay of the same intent
    assert obj(admin,out['offering_id'])==(2,{**obj(admin,out['offering_id'])[1],'active':False})


@PLAN
def test_the_guard_itself_refuses_every_update_but_a_withdrawal(incentive_catalog,admin):
    """0389 is the last line behind the authorization layer: an UPDATE that reaches the table (here directly, past the derived
    EDIT that covers only active) is refused unless it is exactly active true -> false."""
    c=incentive_catalog;result=setup(c,recipe([c['consumer']]))
    offering,binding=result['offering_id'],result['bindings'][0]['binding_id']
    def update(object_id,expr):
        with pytest.raises(psycopg.Error,match='incentive catalog'),admin.transaction():
            admin.execute(f'update ontology.objects set properties={expr} where object_id=%s',(object_id,))
    update(offering,"properties||'{\"max_amount_minor\":\"900\"}'")
    update(offering,"properties||'{\"active\":false,\"valid_until\":\"2027-01-01T00:00:00Z\"}'")
    update(binding,"properties||jsonb_build_object('consumer_id',repeat('e',64))")
    update(binding,"properties||'{\"offering_revision\":2}'")
    update(binding,"properties-'source_principal'||'{\"active\":false}'")
    with admin.transaction():
        admin.execute("update ontology.objects set properties=properties||'{\"active\":false}' where object_id=%s",(binding,))
    update(binding,"properties||'{\"active\":true}'")
    with pytest.raises(psycopg.Error,match='identity is immutable'),admin.transaction():
        admin.execute("update ontology.objects set world='test' where object_id=%s",(offering,))
