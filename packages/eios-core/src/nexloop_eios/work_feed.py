"""Leased change feeds written in the business transaction (0092), drained by service workers.

``recall-instance``: every ontology.objects insert/update/delete marks the instance for
(re)indexing or removal (NX-021 §3.6). ``claim-match``: every recorded Claim marks its
Conversation for matching (NX-020). A worker claims items under a lease fenced by the
item's change sequence, then completes or retries them; a change that arrives while an
item is being processed keeps the item pending, so nothing is dropped. The feed's own
technical Action (``eios:action:NexLoop.feed.<feed>:1``) authorizes the service; the
work itself (indexing, matching, governed writes) is authorized separately.
"""
from datetime import UTC,datetime,timedelta
import hashlib
import hmac

import psycopg

from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.authz.service import AuthorizationDecisionService
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.authorization import resolve_authority
from nexloop_eios.postgres_artifacts import canonical_payload

FEEDS=('recall-instance','claim-match','plan-reevaluate','reply-due')
PROTOCOL='nexloop-work-feed-v1'


class WorkFeedDenied(PermissionError):
    def __init__(self):super().__init__('work feed denied')


def retry_delay(attempts,base_seconds):
    return min(3600,base_seconds*2**max(0,attempts-1))


class WorkFeed:
    def __init__(self,pool,session,signer,*,feed):
        if feed not in FEEDS:raise ValueError('unknown work feed')
        if session.run_context is not None:raise WorkFeedDenied()
        self.pool,self.session,self.signer,self.feed=pool,session,signer,feed
        self.resource=f'eios:action:NexLoop.feed.{feed}:1'

    def _call(self,verb,**parameters):
        entries=[]
        query=self.session.query(resource_id=self.resource,resource_type=ResourceType.ACTION,operation=Operation.EXECUTE)
        try:decision=AuthorizationDecisionService().decide_resolved(resolve_authority(self.pool,self.session,query,entries))
        except Exception:raise WorkFeedDenied() from None
        if not decision.allowed or not decision.authoritative or decision.obligations:raise WorkFeedDenied()
        body=canonical_payload({'feed':self.feed,'verb':verb,**parameters});auth=self.session.authentication
        claims={'protocol':PROTOCOL,'key_id':self.signer.key_id,'tenant_id':auth.tenant_id,'principal_id':auth.subject_principal_id,
            'credential_id':auth.credential_id,'directory_hash':self.session.directory_hash,'world':self.session.world,'resource_id':self.resource,
            'action_resource':self.resource,'operation':'execute','expires_at':min(decision.expires_at,datetime.now(UTC)+timedelta(seconds=25)).isoformat(),
            'parameters_digest':hashlib.sha256(body.encode()).hexdigest(),'facts':sorted(entries,key=lambda row:(row['kind'],row['key']))}
        text=canonical_payload(claims)
        signature=hmac.new(self.signer.material,(PROTOCOL+':'+text).encode(),'sha256').hexdigest()
        try:
            with self.pool.connection() as db,db.transaction():
                verify_application_role(db)
                return db.execute('select authz.nexloop_work_feed(%s,%s,%s,%s,%s)',
                    (self.session.token_digest,self.session.world,text,signature,body)).fetchone()[0]
        except psycopg.errors.InsufficientPrivilege:raise WorkFeedDenied() from None

    def claim(self,*,limit=20,lease_seconds=60):return self._call('claim',limit=limit,lease_seconds=lease_seconds)
    def complete(self,*,item_key,fence):return self._call('complete',item_key=item_key,fence=fence)['status']
    def retry(self,*,item_key,fence,code,delay_seconds,max_attempts):
        return self._call('retry',item_key=item_key,fence=fence,code=code,delay_seconds=delay_seconds,max_attempts=max_attempts)['status']
    def backlog(self):return self._call('backlog')
