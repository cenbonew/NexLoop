"""Claim → Consumer phase two (design claim-consumer-delivery-windows v2 §2.3, L6 condition C2): between a Schema publication and the
claim-match.json rebinding the matcher answers edit_action_stale_schema (tests/test_schema_structured_property_pg.py); the worker then
defers the claim-match item (0401: no attempt spent, never dead-lettered) instead of completing or failing it, so the Conversation is
matched again once the matcher is rebound. Matchers are stubs here; the deployed feed and its fixture are test_claim_match_settled_pg's."""
from nexloop_eios.claim_match_worker import STALE_DEFER_SECONDS, ClaimMatchWorker
from nexloop_eios.claim_matching import EditActionStale
from test_claim_match_settled_pg import extracted, feed
from test_message_purpose_reads_pg import deployed  # noqa: F401
from test_conversation_messages import conversations  # noqa: F401
from test_browser_business_authorization import browser_business  # noqa: F401
from test_action_definitions import published_action  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401


class StaleMatch:
    def process_conversation(self, conversation_id):
        raise EditActionStale('Consumer edit Action is behind the object Schema')


class StaleApply:
    def process_conversation(self, conversation_id):
        return {'matches': {}, 'applied': {'synthetic-proposal': EditActionStale.code}}


def _available_in(admin, conversation_id):
    return admin.execute("""select extract(epoch from available_at-clock_timestamp()) from runtime.nexloop_work_feed
        where feed='claim-match' and payload->>'conversation_id'=%s""", (conversation_id,)).fetchone()[0]


def test_a_stale_edit_action_defers_the_item_without_spending_an_attempt(deployed, admin):  # noqa: F811
    d = deployed;extracted(d)
    pool, matcher = d['session']('claim_matcher')
    for stub in (StaleMatch(), StaleApply()):
        summary = ClaimMatchWorker(pool, matcher, d['signer'], matcher=stub, max_attempts=1).run_once()
        assert summary['deferred'] == 1 and summary['retry'] == 0 and summary['dead_lettered'] == 0 and summary['conversations'] == 0, summary
        assert feed(admin, d['conversation_id']) == [('pending', 0, 'edit_action_stale_schema')]
        assert 0 < _available_in(admin, d['conversation_id']) <= STALE_DEFER_SECONDS
        admin.execute("update runtime.nexloop_work_feed set available_at=clock_timestamp() where feed='claim-match' and payload->>'conversation_id'=%s",
                      (d['conversation_id'],))
