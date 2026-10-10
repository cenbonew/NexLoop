"""Section partition: what may enter the Context, where, and labelled how.

Formal zone (constraints / consumer_state / open_work) carries only governed EIOS
objects, published policy and ledger execution state. Claims are candidate knowledge:
hypotheses are excluded unless a non-real research strategy opts in (and then only as
labelled hypotheses); Claims still under review appear only as their verbatim source
text (ADR-019 decisions 2/5, AT-064). Outbound replies not yet accepted by the channel
are execution state, never conversation (ADR-020 §2). The Consumer's active plans are open work as
read-only policy items (NX-025 G4); they stay runtime records, never ontology.
"""
from nexloop_eios.context_engine.budget import Item

FORMAL_SECTIONS={'constraints':('formal_object','policy'),'consumer_state':('formal_object','policy'),'open_work':('formal_object','execution_state','policy')}
EVIDENCE_SUBSECTIONS={'conversation','claim_evidence','relationships','recall','hypotheses'}
UNCONFIRMED_OUTBOUND=('persisted','dispatching','unknown')
CHANNEL_ACCEPTED=('provider_accepted','delivered')
REVIEW_STATES=('unresolved','needs_resolution','awaiting_definition','resolved')


class PartitionViolation(ValueError):
    pass


def assert_partition(items):
    """Same rules the SQL check constraints of runtime.nexloop_context_sources enforce."""
    for item in items:
        allowed=FORMAL_SECTIONS.get(item.section)
        if allowed is not None:
            if item.evidence_kind not in allowed or item.ref.startswith(('claim:','nexloop:claim:')):
                raise PartitionViolation(f'{item.ref} cannot enter formal section {item.section}')
        if item.evidence_kind=='hypothesis' and (item.section,item.subsection)!=('evidence','hypotheses'):
            raise PartitionViolation('hypotheses only in evidence.hypotheses')
        if item.section=='evidence' and item.subsection not in EVIDENCE_SUBSECTIONS:raise PartitionViolation('evidence subsection')
    return items


def claim_items(view,strategy,*,decision):
    """claim.schema.json items (NX-019 read view) → evidence items; never formal ones."""
    out=[]
    for claim in view['statements']+view['hypotheses']:
        kind=claim['epistemic_kind'];state=claim['resolution_state']
        if kind=='hypothesis':
            if not strategy['include_hypotheses']:continue
            out.append(Item('evidence','hypotheses','claim:'+claim['claim_id'],claim['extractor_version'],
                {'hypothesis':claim['predicate'],'value':claim['value'],'derived_from':claim['derived_from']},'hypothesis',decision,
                relevance=min(claim['confidence'],0.6),at=claim['recorded_at']))
            continue
        if state not in REVIEW_STATES or claim['source'] is None:continue  # rejected_definition / superseded never enter
        source=claim['source'];tags=set()
        if claim['polarity']=='negated':tags.add('negation')
        if kind=='constraint':tags.add('contact_limit')
        # Original text only: no structured value, so nothing here can act as a formal property.
        out.append(Item('evidence','claim_evidence','claim:'+claim['claim_id'],claim['extractor_version'],
            {'quote':source['quote'],'message_ref':'eios:object:Message/'+source['message_id'],'span':[source['span_start'],source['span_end']],
             'speaker':claim['speaker'],'epistemic_kind':kind,'resolution_state':state,'modality':claim['modality'],'condition':claim['condition']},
            'user_statement' if claim['speaker']=='consumer' else 'conversation',decision,relevance=claim['confidence'],
            at=claim['recorded_at'],duplicate_key=claim['correlation_key'],tags=frozenset(tags)))
    return assert_partition(out)


def open_work_items(open_work,*,decision):
    out=[]
    for intent in open_work['intents']:
        out.append(Item('open_work','intents','nexloop:intent:'+intent['intent_id'],intent['state'],
            {'action':intent['action'],'state':intent['state'],'since':intent['created_at']},'execution_state',decision,
            relevance=1.0,at=intent['created_at'],tags=frozenset({'unconfirmed'})))
    for row in open_work['outbound']:
        state=row['delivery_state']
        if state in CHANNEL_ACCEPTED:continue  # visible conversation, read as a Message
        out.append(Item('open_work','outbound','nexloop:outbound:'+row['intent_id'],state,
            {'delivery_state':state,'since':row['delivery_changed_at'],'note':'not confirmed as delivered to the consumer'},
            'execution_state',decision,relevance=1.0 if state in UNCONFIRMED_OUTBOUND else 0.5,at=row['delivery_changed_at'],
            tags=frozenset({'unconfirmed'}) if state in UNCONFIRMED_OUTBOUND else frozenset()))
    for plan in open_work.get('plans',()):
        # Current active version only, re-derived by SQL at bind; pinned (a reevaluation Run reads its plan).
        out.append(Item('open_work','plan','nexloop:plan:'+plan['plan_id']+'@'+str(plan['version']),str(plan['version']),plan['content'],'policy',decision,
            relevance=1.0,at=plan['created_at']))
    return assert_partition(out)


def conversation_items(messages,*,decision,negation_refs=frozenset()):
    """Persisted stream records (inbound and channel-accepted outbound), as untrusted data."""
    out=[]
    for record in messages:
        ref='eios:object:Message/'+record['id']
        tags={'negation'} if ref in negation_refs else set()
        out.append(Item('evidence','conversation',ref,str(record['sequence']),
            {'speaker':'agent' if record.get('direction')=='outbound' else 'consumer','body':record['body'],'sequence':record['sequence'],
             'accepted_at':record['accepted_at']},'conversation',decision,relevance=0.5,at=record['accepted_at'],tags=frozenset(tags)))
    return assert_partition(out)


def formal_object_item(section,projection,*,decision,relevance=1.0):
    """AuthorizedObjectReader.get(...) result → formal item (projected properties only)."""
    if section not in ('constraints','consumer_state'):raise PartitionViolation('formal object section')
    return assert_partition([Item(section,projection['type_name'],'eios:object:'+projection['type_name']+'/'+projection['object_id'],
        str(projection['revision']),{'type':projection['type_name'],'properties':projection['properties']},'formal_object',decision,relevance=relevance)])[0]
