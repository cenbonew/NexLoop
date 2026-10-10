"""NX-029 slice 3 (AT-003 export part): a Consumer export holds only what the requesting member may read.

On the NX-028 workbench fixture: real workbench login (own cookie), owner and operator members with their role grants
(deploy/authorization/workbench-roles.v1.json), ADR-025 member reads. The owner requests an export through the
governed entry and downloads it through GET /api/v1/workbench/data-exports/{id}; it is assembled with the owner's own
member reads: an owner-restricted property group is withheld (counted, never copied in), every read leaves its
read-audit row with purpose 'export', nothing is stored, requests and downloads are audited without content. Operators
(no export grant), other members, services, expired requests and erasing Consumers get nothing. Synthetic data only.
"""
import json

import pytest
from psycopg.types.json import Jsonb
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.browser_authorization import authenticate_browser_business
from nexloop_eios.data_export import DataExporter,ExportUnavailable
from nexloop_eios.goal_controls import GoalGovernedActions
from commercial_fixture import publish_registry_actions
from erasure_support import mark_erasing
from goal_fixture import seed_service
from test_workbench_member_read_pg import BODY,consumer_with_groups,staff_session
from test_workbench_read_pg import (TENANT, browser_business, configure, conversations, identity, provision, published_action, uow,  # noqa: F401
    workbench, workbench_login, write_facts)


@pytest.fixture
def exported(workbench):
    w=workbench;admin=w['admin']
    publish_registry_actions(admin,('nexloop.consumer.export',),tenant=TENANT)
    provision(w)
    consumer=consumer_with_groups(w)
    admin.execute('insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) values(%s,%s,%s,%s) on conflict(tenant_id,fact_kind,entity_key) do update set payload=excluded.payload',
        (TENANT,'property_group_restriction',['Consumer'],Jsonb({'tenant_id':TENANT,'type_name':'Consumer','restricted_groups':['spending_power'],'decision':'owner 2026-10-10 (synthetic)'})))
    r=w['f']['reader']
    def member(m):
        client,inspected=staff_session(w,m)
        return client,authenticate_browser_business(r.pool,inspected,world='real')
    return dict(w=w,consumer=consumer,member=member,pool=r.pool,signer=r.signer)


def request(pool,session,signer,consumer,request_id):
    return GoalGovernedActions(pool,session,signer).export_consumer(action_name='nexloop.consumer.export',action_version=1,request_id=request_id,
        consumer_id=consumer,reason='顾客要求导出本人资料')


def test_export_holds_only_what_the_requesting_member_may_read(exported,admin):
    x=exported;w=x['w'];pool,signer=x['pool'],x['signer']
    client,owner=x['member'](w['owner'])
    assert request(pool,owner,signer,x['consumer'],'export-1')['export_id']=='export-1'
    before=admin.execute('select count(*) from runtime.nexloop_workbench_read_audit').fetchone()[0]
    got=client.get('/api/v1/workbench/data-exports/export-1')
    assert got.status_code==200 and got.headers['content-disposition']=='attachment; filename="consumer-export-export-1.json"' and got.headers['cache-control']=='no-store'
    value=got.json()
    # AT-003: the owner-restricted group is withheld (counted), never copied in; readable content is there.
    assert value['consumer']=={'id':x['consumer'],'properties':{'favorite_sport':'网球'}}
    assert value['withheld']['properties']==1 and 'high' not in got.text and 'income_band' not in got.text
    assert [m['body'] for m in value['messages']]==[BODY] and value['withheld']['messages']==0 and value['withheld']['commercial']==1
    # Each member read is audited with its purpose (ADR-025); the request and the download without content.
    purposes=[r[0] for r in admin.execute('select read_purpose from runtime.nexloop_workbench_read_audit order by audit_id offset %s',(before,)).fetchall()]
    assert purposes and set(purposes)=={'export'}
    audit=admin.execute("select action,payload from runtime.audit_events where request_id='export-1' order by created_at").fetchall()
    assert [a for a,_ in audit]==['nexloop.consumer.export.requested','nexloop.consumer.export.downloaded']
    assert audit[1][1]['counts']['withheld_properties']==1 and BODY not in json.dumps([p for _,p in audit],ensure_ascii=False)
    assert admin.execute("select downloads from control.nexloop_data_exports where export_id='export-1'").fetchone()==(1,)
    # Only the requester: the operator (no export grant) cannot request, nor download the owner's export.
    other_client,operator=x['member'](w['operator'])
    with pytest.raises(Exception):request(pool,operator,signer,x['consumer'],'export-op')
    assert other_client.get('/api/v1/workbench/data-exports/export-1').status_code==404
    with pytest.raises(ExportUnavailable):DataExporter(pool,operator,signer).export('export-1')
    # A service holding the very same grant is refused by the entry (human only) and by the export port (identity first).
    token=seed_service(admin,pool,['nexloop.consumer.export'],suffix='-export-service')
    service=authenticate_service(pool,token,world='real')
    with pytest.raises(Exception,match='human goal authority required'):request(pool,service,signer,x['consumer'],'export-svc')
    with pytest.raises(ExportUnavailable):DataExporter(pool,service,signer).export('export-1')
    # Expired, unknown, or of an erasing Consumer: nothing; no new request either.
    client,owner=x['member'](w['owner'])
    request(pool,owner,signer,x['consumer'],'export-2')
    with admin.transaction():
        admin.execute('set local session_replication_role=replica');admin.execute("update control.nexloop_data_exports set expires_at=clock_timestamp() where export_id='export-2'")
    assert client.get('/api/v1/workbench/data-exports/export-2').status_code==404
    assert client.get('/api/v1/workbench/data-exports/export-unknown').status_code==404
    assert client.get('/api/v1/workbench/data-exports/a b').status_code==422
    assert client.post('/api/v1/workbench/data-exports/export-1').status_code==405
    mark_erasing(admin,TENANT,x['consumer'])
    assert client.get('/api/v1/workbench/data-exports/export-1').status_code==404
    with pytest.raises(Exception):request(pool,owner,signer,x['consumer'],'export-3')
    # Without a workbench session: nothing.
    with w['client']() as anonymous:
        assert anonymous.get('/api/v1/workbench/data-exports/export-1').status_code==401
