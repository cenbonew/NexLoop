"""NX-021 §3.6 instance index reflow: recall-instance feed → RecallIndexer.

Every governed create/edit (and any erasure) of an ontology object marks the instance in
the same transaction (0092). This worker (nexloop_domain_worker service) re-derives the
index rows from the stored object, or removes them when the object is gone. Only the
configured instance types are indexed (``types``: type name → instance spec or None for
the definition defaults); other types are acknowledged without indexing. Recall itself
still filters by tenant, world and the caller's current EIOS READ, so revocation applies
at query time without re-indexing.
"""
import psycopg

from nexloop_eios.authorization import authority_request_scoped
from nexloop_eios.recall import RecallIndexer
from nexloop_eios.work_feed import WorkFeed,WorkFeedDenied,retry_delay

FEED='recall-instance'


class RecallInstanceIndexWorker:
    def __init__(self,pool,session,signer,*,types,provider=None,expected_dimension=None,batch=20,lease_seconds=60,
                 max_attempts=5,retry_base_seconds=30):
        if not isinstance(types,dict) or not types:raise ValueError('instance types to index required')
        self.feed=WorkFeed(pool,session,signer,feed=FEED)
        self.indexer=RecallIndexer(pool,session,provider=provider,expected_dimension=expected_dimension)
        self.world=session.world;self.types=dict(types)
        self.batch,self.lease_seconds,self.max_attempts,self.retry_base_seconds=batch,lease_seconds,max_attempts,retry_base_seconds

    def source_key(self,type_name,object_id):return f'object:{self.world}/{type_name}/{object_id}'

    def _index(self,payload):
        type_name,object_id=payload['type_name'],payload['object_id']
        if type_name not in self.types:return 'skipped'
        source=self.source_key(type_name,object_id)
        if payload['op']=='delete':
            self.indexer.remove_source(source);return 'removed'
        try:
            self.indexer.index_instance(type_name,object_id,spec=self.types[type_name]);return 'indexed'
        except psycopg.errors.InsufficientPrivilege as error:
            # The object (or its type definition) no longer exists in this world: nothing may stay recallable.
            if 'recall object unavailable' in str(error) or 'recall definition unavailable' in str(error):
                self.indexer.remove_source(source);return 'removed'
            raise

    @authority_request_scoped
    def run_once(self):
        summary={'indexed':0,'removed':0,'skipped':0,'changed':0,'retry':0,'dead_lettered':0,'lease_lost':0}
        for item in self.feed.claim(limit=self.batch,lease_seconds=self.lease_seconds):
            try:
                outcome=self._index(item['payload'])
            except WorkFeedDenied:raise
            except psycopg.errors.SerializationFailure:
                # The object moved on during indexing; its newer change re-marks the item.
                status=self.feed.retry(item_key=item['item_key'],fence=item['fence'],code='revision_stale',delay_seconds=0,max_attempts=self.max_attempts)
                summary['retry' if status=='pending' else status]+=1;continue
            except Exception as error:
                code='embedding_unavailable' if type(error).__name__.startswith('Embedding') else 'index_failed'
                status=self.feed.retry(item_key=item['item_key'],fence=item['fence'],code=code,
                    delay_seconds=retry_delay(item['attempts'],self.retry_base_seconds),max_attempts=self.max_attempts)
                summary['retry' if status=='pending' else status]+=1;continue
            status=self.feed.complete(item_key=item['item_key'],fence=item['fence'])
            summary[outcome if status=='completed' else status]+=1
        return summary
