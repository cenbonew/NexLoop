"""NX-031 slice 3 (0150): a claim-match item whose Conversation has nothing left to match completes instead of dead-lettering.

Found under load: extraction marks claim-match and commitment-register together; when the commitment keeper resolves the
commitment Claims first, the matcher's purpose READ on the Conversation (0102) has ended, every attempt was denied and the item
dead-lettered after five. The deployed matcher (manifest authority only) now asks, for the item it leases, whether any Claim is
still to be matched (a boolean), and completes the item when none is. Admin only builds data and resolves Claims (the keeper's
effect, as test_message_purpose_reads_pg does) and probes."""
import psycopg
import pytest
from nexloop_eios.claim_extraction_jobs import ClaimExtractionWorker
from nexloop_eios.claim_match_worker import ClaimMatchWorker
from nexloop_eios.work_feed import WorkFeed
from test_message_purpose_reads_pg import FixedProvider, deployed, enqueue  # noqa: F401
from test_conversation_messages import conversations  # noqa: F401
from test_browser_business_authorization import browser_business  # noqa: F401
from test_action_definitions import published_action  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401


class RecordingMatcher:
    def __init__(self):self.calls=[]
    def process_conversation(self,conversation_id):
        self.calls.append(conversation_id);return {'matches':[],'applied':{}}


def extracted(d):
    enqueue(d)
    pool,worker=d['session']('claim_extraction_worker')
    for _ in range(2):ClaimExtractionWorker(pool,worker,d['signer'],FixedProvider(),timezone='Asia/Shanghai').run_once()


def feed(admin,conversation_id):
    return admin.execute("select status,attempts,last_code from runtime.nexloop_work_feed where feed='claim-match' and payload->>'conversation_id'=%s",
                         (conversation_id,)).fetchall()


def test_resolved_conversation_completes_without_matching_or_dead_letter(deployed,admin):
    d=deployed;extracted(d)
    assert feed(admin,d['conversation_id'])==[('pending',0,None)]
    # The keeper's effect: every Claim of the Conversation leaves the to-be-matched states.
    admin.execute("update ontology.nexloop_claims set resolution_state='resolved' where conversation_id=%s",(d['conversation_id'],))
    pool,matcher=d['session']('claim_matcher');recording=RecordingMatcher()
    summary=ClaimMatchWorker(pool,matcher,d['signer'],matcher=recording,max_attempts=1).run_once()
    assert summary['settled']==1 and summary['dead_lettered']==0 and summary['retry']==0,summary
    assert recording.calls==[] and feed(admin,d['conversation_id'])==[]


def test_pending_claim_is_still_matched(deployed,admin):
    d=deployed;extracted(d)
    pool,matcher=d['session']('claim_matcher');recording=RecordingMatcher()
    summary=ClaimMatchWorker(pool,matcher,d['signer'],matcher=recording).run_once()
    assert recording.calls==[d['conversation_id']] and summary['settled']==0 and summary['conversations']==1,summary
    assert feed(admin,d['conversation_id'])==[]


def test_settled_answer_only_for_the_leased_item_and_the_purpose_principal(deployed,admin):
    d=deployed;extracted(d)
    pool,matcher=d['session']('claim_matcher')
    item,=WorkFeed(pool,matcher,d['signer'],feed='claim-match').claim(limit=1,lease_seconds=60)
    def ask(session,key,fence,pool=pool):
        with pool.connection() as db:
            return db.execute('select authz.nexloop_claim_match_settled(%s,%s,%s,%s)',(session.token_digest,session.world,key,fence)).fetchone()[0]
    assert ask(matcher,item['item_key'],item['fence']) is False  # Claims are still unresolved
    for key,fence in ((item['item_key'],item['fence']+1),('claim-match:unknown',item['fence'])):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):ask(matcher,key,fence)
    # Another service of the same role without the claim_matching purpose rule gets no answer.
    worker_pool,worker=d['session']('claim_extraction_worker')
    with pytest.raises(psycopg.errors.InsufficientPrivilege):ask(worker,item['item_key'],item['fence'],pool=worker_pool)
    # Revoking the purpose rule ends the answer at once.
    admin.execute("update authz.nexloop_authority_facts set payload=jsonb_set(payload,'{active}','false') where fact_kind='message_purpose_rule' and entity_key[2]='claim_matching'")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):ask(matcher,item['item_key'],item['fence'])
    # The API role cannot call it at all.
    with pytest.raises(psycopg.errors.InsufficientPrivilege),d['fixture']['reader'].pool.connection() as db:
        db.execute('select authz.nexloop_claim_match_settled(%s,%s,%s,%s)',(matcher.token_digest,'real',item['item_key'],item['fence']))
