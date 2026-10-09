"""NX-020 background matching: claim-match feed → ClaimMatcher → CandidateGluer.

Recording Claims (the end of an extraction run) marks their Conversation in the same
transaction (0092). This worker (nexloop_domain_worker service, claim_matcher principal)
matches every Claim of the Conversation, applies the resulting proposals through the
governed edit/create Actions (proposal state machine, revision conflict → reassess), then
glues newly staged candidate definitions (NX-045). Matching is idempotent per
(Claim, matcher version), so a replayed or re-marked Conversation never duplicates
proposals or candidates.
"""
from eios.authz import facts as F
from eios.authz.errors import AuthorizationUnavailable
from nexloop_eios.authorization import authority_request_scoped
from nexloop_eios.claim_matching import MatchProviderUnavailable,MatchRejected
from nexloop_eios.work_feed import WorkFeed,WorkFeedDenied,retry_delay

FEED='claim-match'


class ClaimMatchWorker:
    def __init__(self,pool,session,signer,*,matcher,gluer=None,batch=10,lease_seconds=300,max_attempts=5,retry_base_seconds=30):
        self.feed=WorkFeed(pool,session,signer,feed=FEED)
        self.matcher,self.gluer=matcher,gluer
        self.batch,self.lease_seconds,self.max_attempts,self.retry_base_seconds=batch,lease_seconds,max_attempts,retry_base_seconds

    @authority_request_scoped
    def run_once(self):
        summary={'conversations':0,'matched':0,'applied':0,'glued':0,'changed':0,'retry':0,'dead_lettered':0,'lease_lost':0}
        for item in self.feed.claim(limit=self.batch,lease_seconds=self.lease_seconds):
            try:
                result=self.matcher.process_conversation(item['payload']['conversation_id'])
            except WorkFeedDenied:raise
            except Exception as error:
                code=('provider_unavailable' if isinstance(error,MatchProviderUnavailable) else 'output_rejected' if isinstance(error,MatchRejected)
                    else 'denied' if isinstance(error,(PermissionError,F.AuthorizationFactDenied,AuthorizationUnavailable)) else 'match_failed')
                status=self.feed.retry(item_key=item['item_key'],fence=item['fence'],code=code,
                    delay_seconds=retry_delay(item['attempts'],self.retry_base_seconds),max_attempts=self.max_attempts)
                summary['retry' if status=='pending' else status]+=1;continue
            summary['matched']+=len(result['matches']);summary['applied']+=sum(1 for s in result['applied'].values() if s=='applied')
            status=self.feed.complete(item_key=item['item_key'],fence=item['fence'])
            summary['conversations' if status=='completed' else status]+=1
        if self.gluer is not None:summary['glued']=len(self.gluer.process_staged())
        return summary
