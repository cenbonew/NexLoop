"""S85 C1 (L6): a message Run's Context v6 carries the customer's incentive catalog too - the consumer open work source (0145, shared
by message and plan Runs) derives every bound, active, valid IncentiveOffering, re-derived at bind - so the Host's incentive
condition (an IncentiveOffering in open work) offers nexloop.incentive.issue to a message Run whenever it can issue one, and the
model takes offering_id, binding_id and the issuable range from that item. Without a catalog there is nothing it could issue."""
import json

from local_message_assembly_fixture import assembled_message, business_plan, configured  # noqa: F401
from test_context_artifacts import context_message, source_declarations  # noqa: F401
from test_context_v6_pg import bound, v6  # noqa: F401
from test_incentive_runs_pg import seed_catalog


def test_a_message_runs_context_lists_the_customers_incentive_offering(v6, admin):  # noqa: F811
    f = v6; tenant = f['original']['tenant']; consumer = f['f']['recipe']['consumer_id']
    offer = seed_catalog(admin, tenant, consumer)
    assert f['v6_relay']().run_once() == 'queued'
    pack, _ = bound(admin)
    (item,) = [i for i in pack['open_work'] if i['subsection'] == 'incentive']
    assert item['ref'] == 'eios:object:IncentiveOffering/' + offer['incentive_offering_id']
    assert item['content']['binding_id'] == offer['binding_id'] and item['content']['offering_revision'] == 1
    assert json.dumps(item['content']).count('limit_amount') == 0  # never the budget
