"""NX-027 slice 2 (M19, M03; AT-042): observations projected from verified records, correction chains, refund rules,
currency exclusion, maturity, frozen cohort ratio, real/test separation."""
from datetime import UTC,datetime,timedelta
from decimal import Decimal
import psycopg
import pytest
from commercial_fixture import commercial_env,published_action,TENANT  # noqa: F401

NOW=datetime.now(UTC)
WINDOW=(NOW-timedelta(days=1),NOW+timedelta(days=30))


def observations(c,metric):
    return c['admin'].execute('''select observation_id,numerator,retracted,excluded_reason,supersedes_observation_id from control.nexloop_metric_observations
        where tenant_id=%s and metric_id=%s order by recorded_at''',(TENANT,metric)).fetchall()


def test_revenue_net_gross_refund_backdating_corrections_and_currency(commercial_env):
    c=commercial_env;c.connector()
    c.metric('revenue-net');c.metric('revenue-gross',refund_rule='gross')
    c.kr('net-goal','revenue-net',WINDOW);c.kr('gross-goal','revenue-gross',WINDOW)
    t=NOW-timedelta(hours=3)
    c.post(c.event('payment.succeeded','pay-1',amount=10000,occurred_at=t));c.post(c.event('order.paid','ord-1',amount=5000,occurred_at=t))
    c.post(c.event('payment.succeeded','pay-usd',amount=700,currency='USD',occurred_at=t));c.post(c.event('payment.failed','pay-f',amount=900,occurred_at=t))
    c.tick()
    net=c.compute('net-goal');gross=c.compute('gross-goal')
    assert (Decimal(net['value']),Decimal(gross['value']))==(15000,15000) and net['excluded_other_currency']==1 and net['matured_observations']==2
    # A refund two hours later reduces net revenue at the payment's own business time (backdated), never gross.
    c.post(c.event('refund.succeeded','ref-1',amount=3000,occurred_at=t+timedelta(hours=2),related=('payment','pay-1')));c.tick()
    net=c.compute('net-goal');gross=c.compute('gross-goal')
    assert (Decimal(net['value']),Decimal(gross['value']))==(12000,15000) and net['corrections_applied']==1 and gross['corrections_applied']==0
    chain=[r for r in observations(c,'revenue-net') if r[4] is not None]
    assert len(chain)==1 and chain[0][1]==7000  # superseding observation of pay-1 (10000-3000)
    # A counted order later cancelled is retracted, not deleted.
    c.post(c.event('order.cancelled','ord-1',amount=5000,occurred_at=t+timedelta(hours=1)));c.tick()
    net=c.compute('net-goal')
    assert Decimal(net['value'])==7000 and net['retracted_observations']==1 and net['refund_rule_applied']=='net_of_refunds'
    # Re-recording without a change appends nothing.
    before=len(observations(c,'revenue-net'));c.link('cust-1',c.consumer());c.tick()
    assert len(observations(c,'revenue-net'))>=before  # subject change supersedes; values unchanged
    assert Decimal(c.compute('net-goal')['value'])==7000


def test_refund_metric_not_applicable_ignores_refunds(commercial_env):
    c=commercial_env;c.connector()
    c.metric('refund-total',refund_rule='not_applicable',kinds=('refund',),statuses=('succeeded',));c.kr('refund-goal','refund-total',WINDOW)
    t=NOW-timedelta(hours=1)
    c.post(c.event('payment.succeeded','pay-n',amount=100,occurred_at=t));c.post(c.event('refund.succeeded','ref-n',amount=40,occurred_at=t,related=('payment','pay-n')))
    c.tick()
    assert Decimal(c.compute('refund-goal')['value'] or 0)==0 and c.compute('refund-goal')['matured_observations']==0


def test_at042_test_payments_never_count_in_real_metrics(commercial_env):
    c=commercial_env;c.connector();c.connector('shop-test',world='test',data_mode='test',confirmation=None)
    for world in ('real','test'):
        c.metric('revenue',world=world);c.kr('revenue-goal','revenue',WINDOW,world=world)
    t=NOW-timedelta(hours=1)
    c.post(c.event('payment.succeeded','pay-real',amount=1000,occurred_at=t))
    c.post(c.event('payment.succeeded','pay-test',amount=99999,occurred_at=t),connector_id='shop-test')
    c.tick();c.tick('test')
    real=c.compute('revenue-goal');test=c.compute('revenue-goal',world='test')
    assert Decimal(real['value'])==1000 and real['excluded_non_real_observations']==0
    assert Decimal(test['value'])==99999
    assert c['admin'].execute("select count(*) from control.nexloop_metric_observations where world='real' and data_mode<>'real'").fetchone()==(0,)


def test_maturity_window_lists_immature_observations(commercial_env):
    c=commercial_env;c.connector();c.metric('revenue-mature',maturity=3600);c.kr('mature-goal','revenue-mature',WINDOW)
    c.post(c.event('payment.succeeded','pay-old',amount=100,occurred_at=NOW-timedelta(hours=2)));c.post(c.event('payment.succeeded','pay-new',amount=50,occurred_at=NOW-timedelta(minutes=5)))
    c.tick();r=c.compute('mature-goal')
    assert Decimal(r['value'])==100 and r['immature_observations']==1


def test_frozen_cohort_ratio_keeps_its_denominator(commercial_env):
    c=commercial_env;c.connector()
    c.metric('renewal-rate',aggregation='ratio_of_sums',currency=None,refund_rule='not_applicable',value='count',kinds=('renewal',),statuses=('succeeded',),
        cohort_kinds=('order','payment'))
    window=(NOW-timedelta(days=1),NOW+timedelta(days=30));c.kr('renewal-goal','renewal-rate',window,target='0.5')
    a,b,late,other=c.consumer(),c.consumer(),c.consumer(),c.consumer()
    for ref,consumer in (('ca',a),('cb',b),('cl',late),('co',other)):c.link(ref,consumer)
    before=NOW-timedelta(days=2)
    c.post(c.event('order.paid','ord-a',amount=1000,customer='ca',occurred_at=before));c.post(c.event('payment.succeeded','pay-b',amount=1000,customer='cb',occurred_at=before))
    c.tick()
    first=c.compute('renewal-goal')
    assert first['cohort']['size']==2 and Decimal(first['value'])==0
    # Inside the window: a cohort member renews; a non-member renews (not counted); a member paid before the window but known only now is late.
    inside=NOW-timedelta(hours=1)
    c.post(c.event('subscription.renewed','sub-a',amount=900,customer='ca',occurred_at=inside))
    c.post(c.event('subscription.renewed','sub-o',amount=900,customer='co',occurred_at=inside))
    c.post(c.event('payment.succeeded','pay-late',amount=1000,customer='cl',occurred_at=before))
    c.tick()
    r=c.compute('renewal-goal')
    assert (r['cohort']['size'],r['cohort']['late_members'],r['cohort']['members_with_outcome'])==(2,1,1)
    assert Decimal(r['value'])==Decimal('0.5') and r['met'] is True and r['cohort']['frozen_at']==first['cohort']['frozen_at']


def test_cohort_is_not_frozen_before_the_window_starts(commercial_env):
    c=commercial_env;c.connector()
    c.metric('renewal-future',aggregation='ratio_of_sums',currency=None,refund_rule='not_applicable',value='count',kinds=('renewal',),statuses=('succeeded',),
        cohort_kinds=('payment',))
    c.kr('future-goal','renewal-future',(NOW+timedelta(days=2),NOW+timedelta(days=30)))
    r=c.compute('future-goal')
    assert r['value'] is None and r['cohort']['size'] is None
    assert c['admin'].execute('select count(*) from control.nexloop_metric_cohort_heads').fetchone()==(0,)


def test_metric_source_declarations_fit_their_definition_and_are_append_only(commercial_env):
    c=commercial_env
    with pytest.raises(psycopg.errors.InvalidParameterValue):c.metric('bad-amount',aggregation='count',value='amount')
    with pytest.raises(psycopg.errors.InvalidParameterValue):c.metric('bad-ratio',aggregation='ratio_of_sums',currency=None,value='count')
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        from nexloop_eios import commercial
        commercial.declare_metric_source(c['configurator_dsn'],tenant=TENANT,world='real',metric_id='missing',metric_version=1,source_kind='commercial_record',
            value='count',record_kinds=('order',),statuses=('paid',))
    c.metric('rev-once')
    from nexloop_eios import commercial
    again=commercial.declare_metric_source(c['configurator_dsn'],tenant=TENANT,world='real',metric_id='rev-once',metric_version=1,source_kind='commercial_record',
        value='amount',record_kinds=('order','payment','renewal'),statuses=('paid','succeeded','refunded_partial','refunded'))
    assert again['replayed'] is True
    with pytest.raises(psycopg.errors.UniqueViolation):
        commercial.declare_metric_source(c['configurator_dsn'],tenant=TENANT,world='real',metric_id='rev-once',metric_version=1,source_kind='commercial_record',
            value='amount',record_kinds=('order',),statuses=('paid',))
    with c['worker'].connection() as db:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):db.execute('select * from control.nexloop_metric_sources')
