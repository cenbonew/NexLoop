"""Token budget, mandatory pinning, ordered trimming and explicit insufficiency (AT-028).

Mandatory sections and pinned items (current negations, contact limits, unconfirmed
execution) are never trimmed or truncated. If they alone exceed the budget the result
is ``insufficient_context`` rather than a silently shortened Context (docs/05 §2).
"""
from dataclasses import dataclass,field
import hashlib
import math

from nexloop_eios.postgres_artifacts import canonical_payload

MANDATORY_SECTIONS=('bindings','role','goal','current_event','constraints')
PIN_TAGS=frozenset({'negation','contact_limit','unconfirmed'})
SECTIONS=MANDATORY_SECTIONS+('consumer_state','open_work','evidence','semantics','experience')


@dataclass(frozen=True)
class Item:
    section:str
    subsection:str
    ref:str
    revision:str
    content:object=field(compare=False)
    evidence_kind:str
    access_decision_ref:str
    relevance:float=0.0
    at:str=''
    duplicate_key:str|None=None
    tags:frozenset=frozenset()

    def __post_init__(self):
        if self.section not in SECTIONS:raise ValueError('context section')
        if not 0<=self.relevance<=1:raise ValueError('relevance')
        object.__setattr__(self,'tags',frozenset(self.tags))

    @property
    def pinned(self):return self.section in MANDATORY_SECTIONS or bool(self.tags&PIN_TAGS)

    @property
    def content_hash(self):return hashlib.sha256(canonical_payload(self.content).encode()).hexdigest()


class ConservativeEstimator:
    """Deliberately high token estimate: ceil(UTF-8 bytes / 2) + per-item framing.

    CJK text (3 bytes/char, usually ~1 token/char) and ASCII (~4 bytes/token) are both
    over-counted. A model-specific tokenizer may replace it; characters are never tokens.
    """
    estimator_id='conservative-bytes-half-v1'
    item_overhead=8
    def estimate(self,item):
        return math.ceil(len(canonical_payload(item.content).encode())/2)+self.item_overhead


@dataclass
class BudgetOutcome:
    kept:list
    omitted:list
    insufficient:list
    report:dict


def _order_worst_first(items,key):return sorted(items,key=key)


def apply_budget(items,strategy,*,estimator=None):
    estimator=estimator or ConservativeEstimator()
    items=list(items)
    if len({(i.section,i.ref) for i in items})!=len(items):raise ValueError('duplicate context item')
    cost={id(i):estimator.estimate(i) for i in items}
    budget=strategy['input_token_budget'];available=budget-strategy['framing_reserve']
    kept=list(items);omitted=[];insufficient=[]
    def drop(item,reason):
        kept.remove(item);omitted.append({'section':item.section,'subsection':item.subsection,'ref':item.ref,'reason':reason})
    def used():return sum(cost[id(i)] for i in kept)
    # 1. Section quotas for non-pinned items (lowest relevance, then oldest, first out).
    for section in ('consumer_state','open_work','evidence','semantics','experience'):
        limit=strategy['sections'][section]['max_items']
        loose=[i for i in kept if i.section==section and not i.pinned]
        for item in _order_worst_first(loose,lambda i:(i.relevance,i.at,i.ref))[:max(0,len(loose)-limit)]:drop(item,'section_quota')
    pinned=[i for i in kept if i.pinned]
    if sum(cost[id(i)] for i in pinned)>available:
        for item in [i for i in kept if not i.pinned]:drop(item,'mandatory_exceeds_budget')
        insufficient.append({'code':'mandatory_exceeds_budget','section':None,'refs':[i.ref for i in pinned]})
        return BudgetOutcome(kept,omitted,insufficient,_report(items,kept,omitted,cost,strategy,estimator))
    recent=strategy['sections']['evidence']['recent_turns']
    def candidates(step):
        loose=[i for i in kept if not i.pinned]
        if step=='duplicate_evidence':
            groups={}
            for i in loose:
                if i.section=='evidence' and i.duplicate_key:groups.setdefault(i.duplicate_key,[]).append(i)
            out=[]
            for group in groups.values():out+=sorted(group,key=lambda i:(i.at,i.ref))[:-1]
            return sorted(out,key=lambda i:(i.at,i.ref))
        if step=='experience':return _order_worst_first([i for i in loose if i.section=='experience'],lambda i:(i.relevance,i.at))
        if step=='hypotheses':return _order_worst_first([i for i in loose if i.subsection=='hypotheses'],lambda i:(i.relevance,i.at))
        if step=='semantics_low_relevance':return _order_worst_first([i for i in loose if i.section=='semantics'],lambda i:(i.relevance,i.ref))
        if step=='older_conversation':
            turns=sorted([i for i in kept if i.subsection=='conversation'],key=lambda i:(i.at,i.ref))
            protected={id(i) for i in turns[-recent:]} if recent else set()
            return [i for i in turns if not i.pinned and id(i) not in protected]
        if step=='older_claim_evidence':return sorted([i for i in loose if i.subsection=='claim_evidence'],key=lambda i:(i.at,i.ref))
        if step=='low_score_recall':return _order_worst_first([i for i in loose if i.subsection=='recall'],lambda i:(i.relevance,i.ref))
        raise ValueError('trim step')
    for step in strategy['trim_order']:
        for item in candidates(step):
            if used()<=available:break
            drop(item,step)
    if used()>available:
        # Core business state would have to go: say so explicitly instead of pretending.
        core=_order_worst_first([i for i in kept if not i.pinned],lambda i:(i.section=='open_work',i.relevance,i.at))
        trimmed=[]
        for item in core:
            if used()<=available:break
            drop(item,'core_exceeds_budget');trimmed.append(item.ref)
        insufficient.append({'code':'core_trimmed','section':None,'refs':trimmed})
    return BudgetOutcome(kept,omitted,insufficient,_report(items,kept,omitted,cost,strategy,estimator))


def _report(items,kept,omitted,cost,strategy,estimator):
    sections={}
    for name in SECTIONS:
        k=[i for i in kept if i.section==name]
        sections[name]={'included':len(k),'omitted':sum(1 for o in omitted if o['section']==name),'tokens':sum(cost[id(i)] for i in k)}
    return {'estimator':estimator.estimator_id,'input_token_budget':strategy['input_token_budget'],'output_reserve':strategy['output_reserve'],
        'framing_reserve':strategy['framing_reserve'],'available':strategy['input_token_budget']-strategy['framing_reserve'],
        'used':sum(cost[id(i)] for i in kept),'sections':sections,'omitted':omitted}
