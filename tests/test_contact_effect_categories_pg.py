"""ADR-023 §3 / NX-026 D6 on clean catalog PostgreSQL: a contact restriction stops only effects that reach the customer.

Actual governed Run submissions and the independent effect Worker with a loopback provider (zero requests when
refused). The restriction itself comes from the 0109 inbound check on a refusing consumer message (admin seeds the
stream entry; the trigger restricts in that transaction). The effect category is written only by trusted
configuration (nexloop_configurator) and frozen by the published definition's digest. The positive case of a
notification sent as a reply bound to the inbound message is the NX-025 chain (test_contact_reply_dispatch_pg).
"""
from datetime import UTC,datetime,timedelta

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from support.effect_provider import effect_provider
from commitment_fixture import TENANT,commitments,deadline  # noqa: F401
from effect_execution_fixture import governed_effect_executor,execution_plan  # noqa: F401
from test_commitments_pg import fresh

pytestmark=pytest.mark.parametrize('execution_plan',['commitment-service'],indirect=True)
REFUSAL='以后别再给我发消息了'


def restricted(c):
    message=c.inbound(REFUSAL)
    assert c['admin'].execute('select active from control.nexloop_contact_restrictions where consumer_id=%s',(c['consumer'],)).fetchone()==(True,)
    return message


def refused(c,provider):
    result=c.dispatch(provider)
    assert result['status']=='admission_unavailable' and provider.control('snapshot')['effects']==0,result
    return result


def contact_check(c,intent):
    try:
        c['admin'].execute('select control.nexloop_contact_assert_intent(%s,%s,%s)',(TENANT,'real',intent));return 'pass'
    except psycopg.Error as error:
        c['admin'].rollback();return error.diag.sqlstate+' '+error.diag.message_primary


def test_non_contact_service_delivery_dispatches_to_a_restricted_consumer(commitments,admin,tmp_path):
    c=commitments;c.configure_categories();restricted(c)
    receipt=c.submit({'service':'后台退款处理'})
    assert contact_check(c,receipt['intent_id'])=='pass'
    with effect_provider(tmp_path/'provider.sqlite') as provider:
        c.deliver(provider,receipt['intent_id'])
        assert provider.control('snapshot')['effects']==1
    assert admin.execute('select active from control.nexloop_contact_restrictions where consumer_id=%s',(c['consumer'],)).fetchone()==(True,)


def test_contact_undeclared_and_digest_mismatch_are_refused(commitments,admin,tmp_path):
    c=commitments;restricted(c)
    receipt=c.submit({'service':'后台退款处理'})
    # Undeclared: treated as reaching the customer.
    assert contact_check(c,receipt['intent_id']).startswith('NXC05 contact restricted: outreach is not a reply')
    # A row for another digest of the same Action (definition changed since): still undeclared.
    admin.execute('''insert into control.nexloop_action_effect_categories(tenant_id,world,resource_id,definition_digest,category,notification_parameters,manifest_version)
        values(%s,'real','eios:action:nexloop.service.request:1',%s,'non_contact_service','{}',1)''',(TENANT,'f'*64))
    assert contact_check(c,receipt['intent_id']).startswith('NXC05 contact restricted: outreach is not a reply')
    with effect_provider(tmp_path/'provider.sqlite') as provider:refused(c,provider)


def test_customer_contact_category_is_refused(commitments,admin,tmp_path):
    c=commitments;c.configure_categories(category='customer_contact');restricted(c)
    receipt=c.submit({'service':'短信通知'})
    assert contact_check(c,receipt['intent_id']).startswith('NXC05 contact restricted: outreach is not a reply')
    with effect_provider(tmp_path/'provider.sqlite') as provider:refused(c,provider)


def test_attached_notification_is_refused_even_right_after_the_customer_wrote(commitments,admin,tmp_path):
    c=commitments;c.configure_categories();restricted(c)
    c.inbound('退款什么时候到账？')  # the customer just wrote: a pending reply exists, inside the reply window
    assert admin.execute("select count(*) from runtime.nexloop_work_feed where feed='reply-due'").fetchone()[0]==2
    for notification in ({'message':'您的退款已处理'},{'notice':'退款已到账'}):
        receipt=c.submit({'service':'后台退款处理',**notification})
        assert contact_check(c,receipt['intent_id'])=='NXC05 contact restricted: attached_notification (send it as a reply bound to an inbound message)'
    with effect_provider(tmp_path/'provider.sqlite') as provider:
        refused(c,provider);refused(c,provider)
    # Without the notification the same service delivery goes through.
    receipt=c.submit({'service':'后台退款处理（无通知）'})
    assert contact_check(c,receipt['intent_id'])=='pass'


def test_unrestricted_consumer_and_category_configuration_boundary(commitments,admin,tmp_path):
    c=commitments;c.configure_categories(category='customer_contact')
    receipt=c.submit({'service':'短信通知'})
    assert contact_check(c,receipt['intent_id'])=='pass'  # no restriction: categories change nothing
    # Only the technical configurator writes categories; the declaration of a digest is immutable.
    with psycopg.connect(make_conninfo(c['pg'],user='nexloop_domain_worker')) as db:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):db.execute("select control.nexloop_configure_effect_categories(%s,'real','{}'::jsonb)",(TENANT,))
        db.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):db.execute('select * from control.nexloop_action_effect_categories')
    from nexloop_eios.trusted_configuration import ConfigurationRejected
    with pytest.raises(Exception,match='immutable|configuration'):c.configure_categories(category='non_contact_service')
    with pytest.raises(Exception):admin.execute("update control.nexloop_action_effect_categories set category='non_contact_service'")


def test_restricted_consumer_commitment_contact_fulfilment_is_blocked_and_service_fulfilment_works(commitments,admin,tmp_path):
    c=commitments;c.configure_categories()
    blocked,_=fresh(c);served,_=fresh(c,reply='我们会为您办理退款。',quote='我们会为您办理退款',predicate='退款')
    restricted(c)
    notify=c.submit({'service':'进展通知','message':'您的问题处理中','commitment_ref':'commitment:'+blocked})
    refund=c.submit({'service':'后台退款','commitment_ref':'commitment:'+served})
    import time
    with effect_provider(tmp_path/'provider.sqlite') as provider:
        # The notification intent was queued first: refused (NXC05), zero requests; the refund goes next.
        assert c.dispatch(provider)['status']=='admission_unavailable' and provider.control('snapshot')['effects']==0
        accepted=c.dispatch(provider);assert accepted['claimed'] is True and accepted['status']!='admission_unavailable',accepted
        provider.control('fulfill',intent_id=refund['intent_id']);time.sleep(3.1)
        # Both leases expired: the Worker may take the refused notification again before observing the refund.
        results=[c.dispatch(provider) for _ in range(2)]
        assert 'fulfilled' in [r['status'] for r in results] and all(r['status'] in ('fulfilled','admission_unavailable') for r in results),results
        assert provider.control('snapshot')['effects']==1
    c.tick()
    assert c.commitment(served)['properties']['status']=='fulfilled'
    assert c.commitment(blocked)['properties']['status']=='in_progress'
    assert [e[1] for e in c.exceptions(blocked)]==['blocked_by_contact_restriction']
    assert admin.execute('select state from runtime.nexloop_effect_intents where intent_id=%s',(notify['intent_id'],)).fetchone()[0]=='accepted'


def test_commitment_made_while_restricted_is_flagged_for_the_owner(commitments,admin):
    """ADR-023 §2.7: a reply to a restricted customer must not promise further contact; if it did, the owner sees it."""
    c=commitments;restricted(c)
    commitment,_=fresh(c,due_in=timedelta(days=3))
    assert [e[1] for e in c.exceptions(commitment)]==['made_under_contact_restriction']
    assert c.commitment(commitment)['properties']['status']=='open'  # registered all the same: the words were delivered
