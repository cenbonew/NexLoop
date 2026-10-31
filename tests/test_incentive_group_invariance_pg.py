"""S77 / ADR-042, L6 condition 2: the incentive_lifecycle group (types v2) changes no existing read or write result. For the same
object at v1 and at v2, every workbench role reads exactly the same (0123 member derivation and the 0387 read_types chain), and
the 0084 type derivation gives nothing to anyone but the catalog maintainer, and to it no property but active, on a v2 object.
A disposable database; rows written as migrations and governed Actions would leave them."""
from datetime import UTC,datetime,timedelta

from psycopg.types.json import Jsonb

from support.tenants import TENANT_A as TENANT
from test_workbench_read_pg import browser_business, conversations, identity, provision, published_action, uow, workbench  # noqa: F401

MAY="select authz.nexloop_workbench_member_may_read(%s,'real',%s,%s)"
# The derivation reads the tenant's facts under row security (its callers set eios.tenant_id first, as 0084's basis does).
DERIVE="select set_config('eios.tenant_id',%s,true) is not null and authz.nexloop_property_access_derivation(%s,%s,'real',%s,%s) is not null"
MAINTAINER='nexloop-service-catalog-maintainer-principal'
OTHERS=('nexloop-service-claim-matcher-principal','nexloop-service-commitment-keeper-principal','nexloop-service-relay-source-principal','synthetic-owner-principal')
ROLES_UNDER_TEST=('owner','operator','reviewer',None,'customer')


def seed(admin,consumer):
    """IncentiveOffering / ConsumerIncentiveOffering at v1 and v2, one offering and one binding per version, the maintainer's two
    rules and the owner's decision restricting no group (as register writes them from service-grants v24)."""
    from nexloop_eios.incentives import offering_schemas,fixed_amount_example
    from nexloop_eios.property_access import PropertyAccessRule,PropertyGroupRestriction
    ids={}
    until=(datetime.now(UTC)+timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%SZ')
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        for version in (1,2):
            for schema in offering_schemas(version):
                admin.execute('insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,%s,%s,%s) on conflict do nothing',
                              (TENANT,schema.type_name,version,Jsonb(schema.model_dump(mode='json'))))
            offering=f'{version}'*64;binding=f'{version+2}'*64;ids[version]=(offering,binding)
            props={'IncentiveOffering':fixed_amount_example(code='s77',max_amount_minor=500,per_consumer_period_limit_minor=1000,currency='CNY',valid_until=until),
                   'ConsumerIncentiveOffering':{'consumer_id':consumer,'offering_id':offering,'offering_revision':1,'source_principal':MAINTAINER,'active':True}}
            for type_name,object_id in (('IncentiveOffering',offering),('ConsumerIncentiveOffering',binding)):
                admin.execute('''insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
                    values(%s,'real',%s,%s,%s,%s,'nexloop-action','s77-invariance',now(),now())''',(TENANT,type_name,object_id,version,Jsonb(props[type_name])))
        for t in ('IncentiveOffering','ConsumerIncentiveOffering'):
            rule=PropertyAccessRule(tenant_id=TENANT,principal_id=MAINTAINER,type_name=t,operations=('edit','read'),property_groups=('incentive_lifecycle',),
                include_review_published=False,basis_schema_version=2,active=True,valid_until=datetime.now(UTC)+timedelta(days=1))
            admin.execute('insert into authz.nexloop_authority_facts values(%s,%s,%s,%s) on conflict(tenant_id,fact_kind,entity_key) do update set payload=excluded.payload',
                          (TENANT,'property_access_rule',[MAINTAINER,t],Jsonb(rule.model_dump(mode='json'))))
            admin.execute('insert into authz.nexloop_authority_facts values(%s,%s,%s,%s) on conflict(tenant_id,fact_kind,entity_key) do update set payload=excluded.payload',
                          (TENANT,'property_group_restriction',[t],Jsonb(PropertyGroupRestriction(tenant_id=TENANT,type_name=t,restricted_groups=(),
                              decision='synthetic owner decision').model_dump(mode='json'))))
    return ids


def targets(type_name,object_id,fields):
    return ['eios:object:'+type_name+'/'+object_id]+['eios:property:'+type_name+'/'+object_id+'/'+f for f in fields]


def test_the_group_changes_no_existing_result_and_adds_only_the_maintainer_edit(workbench):
    from nexloop_eios.incentives import BINDING_FIELDS,OFFERING_FIELDS
    w=workbench;admin=w['admin']
    provision(w)  # roles v52 with read_types: the owner's list names both incentive types (0387)
    consumer=admin.execute("select object_id from ontology.objects where tenant_id=%s and world='real' and type_name='Consumer' limit 1",(TENANT,)).fetchone()[0]
    ids=seed(admin,consumer)
    for type_name,index,fields in (('IncentiveOffering',0,OFFERING_FIELDS),('ConsumerIncentiveOffering',1,BINDING_FIELDS)):
        v1,v2=targets(type_name,ids[1][index],fields),targets(type_name,ids[2][index],fields)
        for role in ROLES_UNDER_TEST:  # workbench reads (0123 + 0387 read_types + owner restriction last): identical at v1 and v2
            before=[admin.execute(MAY,(TENANT,role,t)).fetchone()[0] for t in v1]
            after=[admin.execute(MAY,(TENANT,role,t)).fetchone()[0] for t in v2]
            assert before==after,(type_name,role,before,after)
        # The owner reads every declared property either way (listed type, no restricted group); the others read nothing of it.
        assert all(admin.execute(MAY,(TENANT,'owner',t)).fetchone()[0] for t in v2)
        assert not any(admin.execute(MAY,(TENANT,r,t)).fetchone()[0] for r in ('operator','reviewer',None,'customer') for t in v2)
        for operation in ('read','edit'):
            # Nobody else derives anything, at either version.
            assert not any(admin.execute(DERIVE,(TENANT,TENANT,p,t,operation)).fetchone()[0] for p in OTHERS for t in v1+v2)
            # The maintainer: at v1 the object itself (0084 derives an object target of a ruled type) but no property (no group), so
            # no edit can pass at v1; at v2 the object and the property active only.
            assert [t for t in v1 if admin.execute(DERIVE,(TENANT,TENANT,MAINTAINER,t,operation)).fetchone()[0]]==[v1[0]]
            derived=[t for t in v2 if admin.execute(DERIVE,(TENANT,TENANT,MAINTAINER,t,operation)).fetchone()[0]]
            assert derived==[v2[0],v2[0].replace('eios:object:','eios:property:')+'/active'],(type_name,operation,derived)
    # The one way the group could change a workbench read is an explicit owner decision restricting it: then active (v2 only) is
    # hidden from the owner, nothing else changes - and the maintainer's derivation stops too (0084 never derives a restricted group).
    admin.execute("update authz.nexloop_authority_facts set payload=jsonb_set(payload,'{restricted_groups}','[\"incentive_lifecycle\"]') "
                  "where tenant_id=%s and fact_kind='property_group_restriction' and entity_key=array['IncentiveOffering']",(TENANT,))
    v1,v2=targets('IncentiveOffering',ids[1][0],OFFERING_FIELDS),targets('IncentiveOffering',ids[2][0],OFFERING_FIELDS)
    hidden=[t for t,a,b in zip(v2,[admin.execute(MAY,(TENANT,'owner',t)).fetchone()[0] for t in v1],[admin.execute(MAY,(TENANT,'owner',t)).fetchone()[0] for t in v2]) if a!=b]
    assert hidden==[v2[0].replace('eios:object:','eios:property:')+'/active'],hidden
    assert not admin.execute(DERIVE,(TENANT,TENANT,MAINTAINER,v2[0].replace('eios:object:','eios:property:')+'/active','edit')).fetchone()[0]
