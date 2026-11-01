"""S91 (owner D1-D3, L6 T2/T3): the recall index worker defers an item while the model budget is exhausted (EmbeddingBudgetDenied, its
own except before the generic one) and the takeover expiry worker defers an item whose takeover has not expired; past their bound the
defer answers defer_limit and they fall back to a counted retry. Reverse cases: an unknown error and a denied authority are retried
and counted, never deferred. No database: the feed and the work are stubs recording the calls (the defer verb itself: S90's tests)."""
import re
from datetime import UTC,datetime,timedelta
from pathlib import Path

import pytest

from eios.authz.facts import AuthorizationFactDenied
from nexloop_eios import recall_index_worker as R
from nexloop_eios import takeovers as T
from nexloop_eios.embedding_budget import EmbeddingBudgetDenied

ROOT=Path(__file__).resolve().parents[1]


class Feed:
    def __init__(self,n,defer='deferred'):
        self.items=[{'item_key':'k%d'%i,'fence':i,'attempts':1,'payload':{'takeover_id':'t%d'%i}} for i in range(n)];self.calls=[];self.answer=defer
    def claim(self,**kw):return list(self.items)
    def defer(self,**kw):self.calls.append(('defer',kw));return self.answer
    def retry(self,**kw):self.calls.append(('retry',kw));return 'pending'
    def complete(self,**kw):self.calls.append(('complete',kw));return 'completed'


def recall(feed,errors):
    w=object.__new__(R.RecallInstanceIndexWorker)
    w.feed,w.batch,w.lease_seconds,w.max_attempts,w.retry_base_seconds=feed,10,60,5,30
    w.ensure_profile=lambda:None;w.reflow_definitions=lambda:None
    seen=[]
    def index(payload):
        seen.append(payload)
        error=errors[len(seen)-1]
        if error is not None:raise error
        return 'indexed'
    w._index=index
    return w,seen


def test_an_exhausted_budget_defers_the_item_and_the_rest_of_the_tick():
    feed=Feed(3);w,seen=recall(feed,[EmbeddingBudgetDenied('used up'),None,None])
    summary=w.run_once()
    assert len(seen)==1  # no provider call after the refusal
    assert [c[0] for c in feed.calls]==['defer','defer','defer'] and summary['deferred']==3 and summary['retry']==0
    assert all(c[1]['code']=='embedding_budget' and c[1]['max_defer_seconds']==7*86400 and c[1]['delay_seconds']==R.BUDGET_DEFER_SECONDS for c in feed.calls)


def test_past_seven_days_the_budget_falls_back_to_a_counted_retry():
    feed=Feed(1,defer='defer_limit');w,_=recall(feed,[EmbeddingBudgetDenied('used up')])
    summary=w.run_once()
    assert [c[0] for c in feed.calls]==['defer','retry'] and feed.calls[1][1]['code']=='embedding_budget' and summary['retry']==1


@pytest.mark.parametrize('error,code',[(RuntimeError('unknown'),'index_failed'),(PermissionError('denied'),'index_failed'),
                                       (AuthorizationFactDenied.__new__(AuthorizationFactDenied),'index_failed')])
def test_an_unknown_error_or_a_denied_authority_is_retried_and_counted_never_deferred(error,code):
    feed=Feed(1);w,_=recall(feed,[error])
    summary=w.run_once()
    assert [c[0] for c in feed.calls]==['retry'] and feed.calls[0][1]['code']==code and feed.calls[0][1]['max_attempts']==5
    assert summary['retry']==1 and summary['deferred']==0


class Port:
    def __init__(self,result):self.result=result
    def expire(self,takeover_id):
        if isinstance(self.result,Exception):raise self.result
        return self.result


def takeover(feed,result):
    w=object.__new__(T.TakeoverExpiryWorker);w.feed,w.batch,w.lease_seconds,w.port=feed,20,60,Port(result);return w


def test_a_takeover_not_yet_expired_is_deferred_until_it_expires():
    left=timedelta(hours=30)  # longer than the old retry chain (20 x 1 h)
    feed=Feed(1);summary=takeover(feed,{'status':'active','expires_at':(datetime.now(UTC)+left).isoformat()}).run_once()
    (verb,kw),=feed.calls
    assert verb=='defer' and kw['code']=='takeover_active' and kw['max_defer_seconds']==T.ACTIVE_MAX_DEFER_SECONDS and kw['delay_seconds']==21600
    assert summary['deferred']==1 and summary['waiting']==1 and summary['retry']==0
    soon=Feed(1);takeover(soon,{'status':'active','expires_at':(datetime.now(UTC)+timedelta(seconds=90)).isoformat()}).run_once()
    assert 85<=soon.calls[0][1]['delay_seconds']<=91


def test_past_the_bound_a_takeover_falls_back_to_a_counted_retry():
    feed=Feed(1,defer='defer_limit')
    takeover(feed,{'status':'active','expires_at':(datetime.now(UTC)+timedelta(hours=2)).isoformat()}).run_once()
    assert [c[0] for c in feed.calls]==['defer','retry'] and feed.calls[1][1]['max_attempts']==20


@pytest.mark.parametrize('error',[RuntimeError('unknown'),PermissionError('denied')])
def test_takeover_unknown_error_or_denied_authority_is_retried_and_counted(error):
    feed=Feed(1);summary=takeover(feed,error).run_once()
    assert [c[0] for c in feed.calls]==['retry'] and feed.calls[0][1]['code']=='takeover_unavailable' and feed.calls[0][1]['max_attempts']==10
    assert summary['retry']==1 and summary['deferred']==0


def test_the_takeover_bound_covers_the_longest_takeover_sql_admits():
    sql=(ROOT/'packages/eios-core/src/eios/migrations/0126_nx028_takeover.sql').read_text()
    limit=int(re.search(r"\(definition->>'max_seconds'\)::integer between 60 and (\d+)",sql).group(1))
    assert T.ACTIVE_MAX_DEFER_SECONDS>=limit==604800
