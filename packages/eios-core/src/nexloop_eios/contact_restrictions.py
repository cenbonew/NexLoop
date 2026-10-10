"""ADR-023: explicit refusal of contact is a hard stop; every inbound consumer message gets a reply.

The protective check, the restriction and the dispatch refusal live in PostgreSQL (migration 0109): an inbound
Message's own transaction runs the versioned deterministic rules and, on a hit, writes the consumer-level contact
restriction through the NX-022 control writer; dispatch then lets only a reply bound to an inbound message through.
This module holds the Python side:

* ``load_refusal_rules`` / ``load_reply_policy``: the versioned deployment files (the migration seeds the same v1);
* ``ContactReadPort``: the query port before NX-028 (``eios:action:nexloop.contact.read:1``);
* ``ReplyGuaranteeWorker``: drains due pending replies; an unsettled message starts one fallback reply Run through
  the configured launcher; anything that cannot produce a reply is escalated to the owner with evidence.
Release is ``GoalGovernedActions.release_contact_restriction`` (a human owner's governed Action).
"""
from decimal import Decimal
import json
from pathlib import Path
import re

from nexloop_eios.authorization import authority_request_scoped
from nexloop_eios.plan_reevaluation import PlanUnavailable,_SignedPort
from nexloop_eios.work_feed import WorkFeed,WorkFeedDenied

READ_ACTION='eios:action:nexloop.contact.read:1'
REPLY_ACTION='eios:action:nexloop.reply.guarantee:1'
FEED='reply-due'
_BUDGET=('maximum_model_turns','maximum_tool_calls','active_timeout_seconds','maximum_cost','currency')


def _json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def load_refusal_rules(path):
    value=_json(path)
    if type(value) is not dict or set(value)!={'schema','version','decision','refuse','uncertain','exclusions'}:raise ValueError('refusal rules shape')
    if value['schema']!='nexloop-contact-refusal-rules/1' or type(value['version']) is not int or value['version']<1:raise ValueError('refusal rules version')
    for kind in ('refuse','uncertain','exclusions'):
        rows=value[kind]
        if type(rows) is not list or not rows or len({r.get('id') for r in rows})!=len(rows):raise ValueError('refusal rules '+kind)
        for row in rows:
            if type(row) is not dict or set(row)!={'id','pattern'} or re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}',str(row['id'])) is None:raise ValueError('rule')
            if type(row['pattern']) is not str or not 1<=len(row['pattern'])<=1000:raise ValueError('rule pattern')
    return value


def load_reply_policy(path):
    value=_json(path)
    if type(value) is not dict or set(value)!={'schema','version','decision','reply_window_seconds','fallback'}:raise ValueError('reply policy shape')
    if value['schema']!='nexloop-reply-guarantee/1' or type(value['version']) is not int or value['version']<1:raise ValueError('reply policy version')
    if type(value['reply_window_seconds']) is not int or not 60<=value['reply_window_seconds']<=604800:raise ValueError('reply window')
    fallback=value['fallback']
    if type(fallback) is not dict or set(fallback)!={'start_after_seconds','context_strategy','runtime_profile','run_budget','run_ttl_seconds','batch','lease_seconds','max_attempts'}:
        raise ValueError('fallback shape')
    budget=fallback['run_budget']
    if type(budget) is not dict or set(budget)!=set(_BUDGET):raise ValueError('fallback budget')
    for key,limit in (('maximum_model_turns',8),('maximum_tool_calls',4),('active_timeout_seconds',300)):
        if type(budget[key]) is not int or not 1<=budget[key]<=limit:raise ValueError('fallback budget '+key)
    if Decimal(budget['maximum_cost'])<=0 or re.fullmatch('[A-Z]{3}',budget['currency']) is None:raise ValueError('fallback cost')
    if fallback['runtime_profile'] not in ('deterministic-test','deepseek-flash'):raise ValueError('fallback runtime profile')
    if re.fullmatch(r'[a-z][a-z0-9_]{0,63}',str(fallback['context_strategy'])) is None:raise ValueError('fallback context strategy')
    for key,low,high in (('start_after_seconds',1,86400),('run_ttl_seconds',30,300),('batch',1,100),('lease_seconds',10,3600),('max_attempts',1,10)):
        if type(fallback[key]) is not int or not low<=fallback[key]<=high:raise ValueError('fallback '+key)
    # ADR-023: the fallback starts and its Run ends inside the reply window; otherwise refuse to start.
    if fallback['start_after_seconds']+fallback_duration(value)>=value['reply_window_seconds']:
        raise ValueError('fallback start plus Run duration must be below the reply window')
    return value


def fallback_duration(policy):
    """Longest a fallback reply Run may live: its credential TTL or its active budget, whichever is longer."""
    fallback=policy['fallback']
    return max(fallback['run_ttl_seconds'],fallback['run_budget']['active_timeout_seconds'])


class ContactReadPort(_SignedPort):
    """Active contact restrictions with their evidence, and reply escalations (query port before NX-028)."""
    PROTOCOL='nexloop-contact-read-v1';ACTION=READ_ACTION;FUNCTION='nexloop_contact_read'

    def restrictions(self,consumer_id=None):
        payload={'verb':'restrictions'}
        if consumer_id is not None:payload['consumer_id']=str(consumer_id)
        return self._signed(payload)['restrictions']

    def escalations(self):return self._signed({'verb':'escalations'})['escalations']


class ReplyPort(_SignedPort):
    PROTOCOL='nexloop-reply-guarantee-v1';ACTION=REPLY_ACTION;FUNCTION='nexloop_reply_command'

    def state(self,message_id):return self._signed({'verb':'state','message_id':str(message_id)})
    def escalate(self,message_id,reason,detail):return self._signed({'verb':'escalate','message_id':str(message_id),'reason':reason,'detail':dict(detail)})


class ReplyGuaranteeWorker:
    """One tick: each due pending reply is settled, answered by its one fallback reply Run, or escalated (never dropped).

    ``launcher`` (``reply_fallback.FallbackReplyLauncher``) starts the fallback reply Run of a message; the item is then
    checked again once the fallback Run's longest duration has passed. A message still unsettled at that point (model
    unavailable or timed out, reply refused, channel failure), a message whose fallback cannot start, or any message
    when no launcher is configured, is escalated to the owner with evidence. At most one fallback Run per message
    (SQL, 0110).
    """

    def __init__(self,pool,session,signer,*,policy,launcher=None):
        self.feed=WorkFeed(pool,session,signer,feed=FEED)
        self.port=ReplyPort(pool,session,signer)
        self.policy,self.launcher=policy,launcher

    @authority_request_scoped
    def run_once(self):
        summary={'settled':0,'fallback_started':0,'escalated':0,'retry':0,'dead_lettered':0,'changed':0,'lease_lost':0}
        fallback=self.policy['fallback'];recheck=fallback_duration(self.policy)+30
        for item in self.feed.claim(limit=fallback['batch'],lease_seconds=fallback['lease_seconds']):
            message_id=item['payload']['message_id']
            try:
                state=self.port.state(message_id)
                if state['settled'] or state['escalated']:outcome='settled'
                elif state.get('fallback_run_id'):
                    # The one fallback Run had its full duration and the message is still unanswered.
                    self.port.escalate(message_id,'fallback_unanswered',{'fallback_run_id':str(state['fallback_run_id']),'restricted':state['restricted']});outcome='escalated'
                elif self.launcher is None:
                    self.port.escalate(message_id,'fallback_unavailable',{'restricted':state['restricted'],'in_flight':state['in_flight']});outcome='escalated'
                else:
                    try:started=self.launcher.start(message_id=message_id,state=state)
                    except Exception as error:
                        self.port.escalate(message_id,'fallback_failed',{'error':type(error).__name__,'restricted':state['restricted']});outcome='escalated'
                    else:
                        if started!='started':
                            self.port.escalate(message_id,'fallback_not_started',{'restricted':state['restricted']});outcome='escalated'
                        else:
                            status=self.feed.retry(item_key=item['item_key'],fence=item['fence'],code='fallback_started',delay_seconds=recheck,
                                max_attempts=fallback['max_attempts']+3)
                            summary['fallback_started' if status=='pending' else status]+=1;continue
            except WorkFeedDenied:raise
            except Exception:
                status=self.feed.retry(item_key=item['item_key'],fence=item['fence'],code='reply_unavailable',delay_seconds=30,max_attempts=fallback['max_attempts']+3)
                summary['retry' if status=='pending' else status]+=1;continue
            status=self.feed.complete(item_key=item['item_key'],fence=item['fence'])
            summary[outcome if status=='completed' else status]+=1
        return summary


__all__=['ContactReadPort','ReplyGuaranteeWorker','ReplyPort','PlanUnavailable','load_refusal_rules','load_reply_policy']
