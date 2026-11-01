"""NX-028 slice 3: human takeover expiry (D3) on the reply guarantor.

A takeover is active until it is handed back or its expires_at passes (SQL checks the time itself, 0152). The
'takeover-expiry' feed item of each takeover becomes due at expires_at; ``TakeoverExpiryWorker`` then ends it through
``authz.nexloop_takeover_command`` (eios:action:nexloop.takeover.expire:1): the D5 settlement (only the latest unanswered
message returns to normal handling), the hand-back control event (plans reevaluate) and an owner-visible escalation.
Start and hand-back themselves are human Actions (``GoalGovernedActions.take_over_conversation`` / ``hand_back_conversation``).
"""
from datetime import UTC,datetime

from nexloop_eios.authorization import authority_request_scoped
from nexloop_eios.plan_reevaluation import _SignedPort
from nexloop_eios.work_feed import WorkFeed,WorkFeedDenied

EXPIRE_ACTION='eios:action:nexloop.takeover.expire:1'
FEED='takeover-expiry'
# S91 (owner D1/D3): a takeover that has not expired yet is 'later', not a failure - its item is deferred until expires_at (no attempt
# spent). Ending does not depend on this worker (SQL compares expires_at itself); 7 days covers the longest takeover SQL admits
# (0126: max_seconds at most 604800), asserted by test.
ACTIVE_MAX_DEFER_SECONDS=7*86400


class TakeoverPort(_SignedPort):
    PROTOCOL='nexloop-takeover-expire-v1';ACTION=EXPIRE_ACTION;FUNCTION='nexloop_takeover_command'

    def expire(self,takeover_id):return self._signed({'verb':'expire','takeover_id':str(takeover_id)})


class TakeoverExpiryWorker:
    def __init__(self,pool,session,signer,*,batch=20,lease_seconds=60):
        self.feed=WorkFeed(pool,session,signer,feed=FEED);self.port=TakeoverPort(pool,session,signer)
        self.batch,self.lease_seconds=batch,lease_seconds

    @authority_request_scoped
    def run_once(self):
        summary={'expired':0,'ended':0,'waiting':0,'retry':0,'dead_lettered':0,'changed':0,'lease_lost':0,'deferred':0}
        for item in self.feed.claim(limit=self.batch,lease_seconds=self.lease_seconds):
            try:result=self.port.expire(item['payload']['takeover_id'])
            except WorkFeedDenied:raise
            except Exception:
                status=self.feed.retry(item_key=item['item_key'],fence=item['fence'],code='takeover_unavailable',delay_seconds=30,max_attempts=10)
                summary['retry' if status=='pending' else status]+=1;continue
            if result['status']=='active':
                # Not yet (the item was moved forward): wait until it expires.
                left=(datetime.fromisoformat(str(result['expires_at']))-datetime.now(UTC)).total_seconds()
                delay=max(1,min(21600,int(left)+1))
                status=self.feed.defer(item_key=item['item_key'],fence=item['fence'],code='takeover_active',delay_seconds=delay,max_defer_seconds=ACTIVE_MAX_DEFER_SECONDS)
                if status=='defer_limit':
                    status=self.feed.retry(item_key=item['item_key'],fence=item['fence'],code='takeover_active',delay_seconds=min(3600,delay),max_attempts=20)
                summary['waiting' if status in ('pending','deferred') else status]+=1;summary['deferred']+=status=='deferred';continue
            # Ending a takeover removes its own expiry item in the same transaction, so 'lease_lost' here is the normal outcome.
            status=self.feed.complete(item_key=item['item_key'],fence=item['fence'])
            summary[('expired' if result['status']=='expired' else 'ended') if status in ('completed','lease_lost') else status]+=1
        return summary


__all__=['TakeoverExpiryWorker','TakeoverPort']
