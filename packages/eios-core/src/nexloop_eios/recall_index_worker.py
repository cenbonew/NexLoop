"""NX-021 §3.6 instance index reflow: recall-instance feed → RecallIndexer.

Every governed create/edit (and any erasure) of an ontology object marks the instance in
the same transaction (0092). This worker (nexloop_domain_worker service) re-derives the
index rows from the stored object, or removes them when the object is gone. Only the
configured instance types are indexed (``types``: type name → instance spec or None for
the definition defaults); other types are acknowledged without indexing. Recall itself
still filters by tenant, world and the caller's current EIOS READ, so revocation applies
at query time without re-indexing.
"""
import logging

import psycopg

from nexloop_eios.authorization import authority_request_scoped
from nexloop_eios.recall import RecallIndexer
from nexloop_eios.work_feed import WorkFeed,WorkFeedDenied,retry_delay

FEED='recall-instance'
# (credential digest, world, profile) already activated by this process; the worker object is rebuilt every tick.
_activated=set()
# S55: types found still stale right after their rebuild, over the process lifetime.
_stuck=[0]
_log=logging.getLogger(__name__)


class RecallInstanceIndexWorker:
    def __init__(self,pool,session,signer,*,types,provider=None,expected_dimension=None,batch=20,lease_seconds=60,
                 max_attempts=5,retry_base_seconds=30):
        if not isinstance(types,dict) or not types:raise ValueError('instance types to index required')
        self.feed=WorkFeed(pool,session,signer,feed=FEED)
        self.indexer=RecallIndexer(pool,session,provider=provider,expected_dimension=expected_dimension)
        self.world=session.world;self.types=dict(types);self.session=session
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

    def ensure_profile(self):
        """The production path that activates the tenant's recall embedding profile (PR #7 review): the recall-index service
        (nexloop_domain_worker, its own service credential; the tenant comes from that credential server side) calls the governed
        control.nexloop_recall_activate_profile with its configured embedding. Idempotent; it never switches silently: when another
        profile is active the call is refused (explicit replacement required) and the tick fails visibly. The merge (glue)
        configuration can be published only on this active profile (real_tenant_init merge_configuration step)."""
        if self.indexer.provider is None:return None
        key=(self.session.token_digest,self.world,self.indexer.provider.profile_id)
        if key not in _activated:
            self.indexer.activate_profile()
            # 0219 (PR #8 review): at every start, what still lacks a vector of the profile is queued again (objects, a dead-
            # lettered backfill item included) and the published definitions without vectors are re-indexed here. A failure
            # leaves the process unmarked, so the next tick tries again; nothing is recorded as done for good.
            result=self.indexer.backfill_profile(self.types)
            for type_name in result['definition_types']:
                self.indexer.index_object_type(type_name)
            _activated.add(key)
        return key[2]

    def reflow_definitions(self):
        """I26 (0375): every published type whose latest version is not in the definition index is reindexed on the tick (a schema
        publication or a tenant configuration publish does not touch the index). A version that moves on meanwhile is left for the
        next tick; any other failure propagates, so the tick fails visibly."""
        done=0;rebuilt=[]
        for type_name in self.indexer.stale_definitions():
            try:self.indexer.index_object_type(type_name);done+=1;rebuilt.append(type_name)
            except psycopg.errors.SerializationFailure:continue
        # S55: a type still stale right after its rebuild would be rebuilt on every tick; say so and count it.
        stuck=sorted(set(rebuilt)&set(self.indexer.stale_definitions())) if rebuilt else []
        if stuck:
            _log.warning('recall definitions still stale after reindex: %d',len(stuck))
            _stuck[0]+=len(stuck)
        return done

    @authority_request_scoped
    def run_once(self):
        self.ensure_profile()
        self.reflow_definitions()
        from nexloop_eios.embedding_budget import EmbeddingBudgetDenied
        summary={'indexed':0,'removed':0,'skipped':0,'changed':0,'retry':0,'dead_lettered':0,'lease_lost':0}
        denied=False
        for item in self.feed.claim(limit=self.batch,lease_seconds=self.lease_seconds):
            if denied:
                # The model budget closed during this tick: the rest goes back unprocessed (no provider call).
                status=self.feed.retry(item_key=item['item_key'],fence=item['fence'],code='embedding_budget',delay_seconds=300,max_attempts=20)
                summary['retry' if status=='pending' else status]+=1;continue
            try:
                outcome=self._index(item['payload'])
            except WorkFeedDenied:raise
            except psycopg.errors.SerializationFailure:
                # The object moved on during indexing; its newer change re-marks the item.
                status=self.feed.retry(item_key=item['item_key'],fence=item['fence'],code='revision_stale',delay_seconds=0,max_attempts=self.max_attempts)
                summary['retry' if status=='pending' else status]+=1;continue
            except EmbeddingBudgetDenied:
                denied=True
                status=self.feed.retry(item_key=item['item_key'],fence=item['fence'],code='embedding_budget',delay_seconds=300,max_attempts=20)
                summary['retry' if status=='pending' else status]+=1;continue
            except Exception as error:
                code='embedding_unavailable' if type(error).__name__.startswith('Embedding') else 'index_failed'
                status=self.feed.retry(item_key=item['item_key'],fence=item['fence'],code=code,
                    delay_seconds=retry_delay(item['attempts'],self.retry_base_seconds),max_attempts=self.max_attempts)
                summary['retry' if status=='pending' else status]+=1;continue
            status=self.feed.complete(item_key=item['item_key'],fence=item['fence'])
            summary[outcome if status=='completed' else status]+=1
        return summary
