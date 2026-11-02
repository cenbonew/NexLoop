"""K5 (docs/design/k5-consumer-evidence-views.md v2, L6 PASS; owner decisions Q1-Q5 of 2026-11-02): three read-only workbench views.

SQL (0406, observe verbs consumer_evidence / message_trace / conversation_metrics) returns the raw identifiers of one consumer,
message or conversation in world real, already without the owner-only parts for another role (outreach items, model cost). This
module decides, per node and for the member's current role, what is shown, through the same ADR-025 reads every workbench page
uses (object and property READ with the owner's restricted groups; each shown object read is audited with the view's purpose):

* a Claim is shown only when its matched property (eios:property:<Type>/<name> in the match conclusion) is readable for the member
  and not in an owner-restricted group; a Claim without such a match only when no Consumer property is withheld from the member;
  otherwise it is counted as invisible, and so are the commitments and candidates derived from it;
* an erased Claim is never listed: counted in `erased` for the owner, in `invisible` for every other role;
* a corrected or superseded Claim carries neither predicate nor quote; a quote is returned only to a member holding
  ontology.schema.review or nexloop.knowledge.read with READ of the source message (Q5);
* references (commitment.claim_id, dependent and successor claim ids, edges, trace refs) name only claims listed in the response;
* outreach (Q1), the action and receipt steps of a trace and the model cost (Q3) are the owner's.
"""
import re

from nexloop_eios.workbench_reads import COMMITMENT_READ, SCHEMA_REVIEW, WorkbenchForbidden, _denied

GROUP_LIMIT = 25
REF_LIMIT = 10
KINDS = ('claim', 'commitment', 'action', 'outreach', 'candidate')
STEPS = ('message', 'relay', 'context', 'run', 'action', 'receipt', 'extraction', 'matching', 'ontology_update')
_CODE = re.compile(r'[a-z][a-z0-9_.:-]{0,79}')
_OPERATION = re.compile(r'[a-z][a-z0-9_.]{0,63}')
_PROPERTY = re.compile(r'eios:property:([A-Z][A-Za-z0-9_]{0,63})/([A-Za-z][A-Za-z0-9_]{0,63})')


class RealWorldOnly(Exception):
    """The K5 views serve world real only (404 real_world_only)."""


def _ts(value):
    """A stored time as RFC 3339 UTC (`...Z`), or None."""
    if not value:
        return None
    from datetime import UTC, datetime
    try:
        moment = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(UTC).isoformat().replace('+00:00', 'Z')


def _code(value):
    return value if isinstance(value, str) and _CODE.fullmatch(value) else None


def operation_code(action_name):
    """An Action name as a code: `Commitment.fulfil` -> `commitment.fulfil` (letters, digits, `_` and `.` only)."""
    text = re.sub(r'[^a-z0-9_.]', '_', str(action_name).lower()).strip('_.')[:64]
    if not text or not text[0].isalpha():
        text = ('action_' + text)[:64]
    return text if _OPERATION.fullmatch(text) else 'action'


class EvidenceViews:
    def __init__(self, queries):
        self.q = queries
        self.reader = queries.reader
        self.cache = {}
        self._held = {}

    # -- shared ------------------------------------------------------------------------------------------------------------------------
    def _observe(self, verb, **payload):
        if self.reader.session.world != 'real':
            raise RealWorldOnly()
        return self.reader.observe(verb, **payload)

    def _holds(self, action):
        """Whether the member holds EXECUTE of a read Action now (no audit row; a refusal is not an error)."""
        if action not in self._held:
            from nexloop_eios.context_engine.authority import action_claims
            try:
                action_claims(self.reader.pool, self.reader.session, action)
                self._held[action] = True
            except Exception as error:
                if not _denied(error):
                    raise
                self._held[action] = False
        return self._held[action]

    def _quote_reader(self):
        from nexloop_eios.candidate_merge import KNOWLEDGE_READ_ACTION
        return self._holds(SCHEMA_REVIEW) or self._holds(KNOWLEDGE_READ_ACTION)

    def _fields(self, type_name, object_id):
        return self.q._fields_of(type_name, object_id, self.cache)

    def _property_visible(self, type_name, object_id, name):
        info = self._fields(type_name, object_id)
        if not info or not info['readable']:
            return False
        field = next((f for f in info.get('fields') or [] if f['name'] == name), None)
        return field is not None and field['readable'] and not field.get('restricted') and not field.get('erasing')

    def _consumer_withholds(self, consumer_id):
        info = self._fields('Consumer', consumer_id)
        return not info or not info['readable'] or any(not f['readable'] or f.get('restricted') or f.get('erasing') for f in info.get('fields') or [])

    def claim_visible(self, claim, consumer_id):
        """L6 condition 2: by the matched property only; without one, hidden whenever any Consumer property is withheld."""
        if claim['erased']:
            return False
        match = claim.get('match') or {}
        found = _PROPERTY.fullmatch(match.get('property_ref') or '')
        if found:
            type_name, name = found.groups()
            if type_name == 'Consumer':
                return self._property_visible('Consumer', consumer_id, name)
            # Another type: the instance is the proposal's and its property READ is not decided here; hidden (design §2.6a:
            # rather hide than guess).
            return False
        return not self._consumer_withholds(consumer_id)

    def _claim_items(self, claims, consumer_id, purpose):
        """(shown items in order, invisible count, erased count, ids shown) for one list of raw claims."""
        successors = {}
        for c in claims:
            if c.get('corrects_claim_id'):
                successors.setdefault(c['corrects_claim_id'], []).append(c['claim_id'])
        quotes = self._quote_reader()
        items, invisible, erased = [], 0, 0
        for c in claims:
            if c['erased']:
                erased += 1
                continue
            if not self.claim_visible(c, consumer_id):
                invisible += 1
                continue
            state = 'corrected' if c['claim_id'] in successors else 'superseded' if c['resolution_state'] == 'superseded' else 'active'
            quote, status = None, state
            if state == 'active':
                if not c.get('quote'):
                    status = 'none'
                elif not quotes:
                    status = 'reviewer_only'
                elif self.reader._readable(c.get('source_message_id'), purpose):
                    quote, status = c['quote'][:8192], 'ok'
                else:
                    status = 'restricted'
            items.append({'claim_id': c['claim_id'], 'state': state, 'predicate': c['predicate'] if state == 'active' else None,
                          'epistemic_kind': c['epistemic_kind'], 'resolution_state': c['resolution_state'],
                          'confidence': float(c['confidence']) if c.get('confidence') is not None else None,
                          'source': {'conversation_id': c['conversation_id'], 'sequence': c.get('sequence'), 'speaker': c['speaker'],
                                     'quote': quote, 'quote_status': status},
                          '_successors': successors.get(c['claim_id'], [])})
        return items, invisible, erased

    # -- A: evidence graph --------------------------------------------------------------------------------------------------------------
    def consumer_evidence(self, *, consumer_id, kind=None, after=None):
        raw = self._observe('consumer_evidence', consumer_id=consumer_id)
        if raw is None:
            return None
        owner = raw['role'] == 'owner'
        self._held['role'] = raw['role']
        # The consumer itself must be readable for this member (one audited read with the view's purpose).
        if self.reader.read_object('Consumer', consumer_id, (), 'consumer_evidence') is None:
            raise WorkbenchForbidden('consumer')
        claims, claim_invisible, claim_erased = self._claim_items(raw['claims'], consumer_id, 'consumer_evidence')
        visible_claims = {c['claim_id'] for c in claims}
        all_claims = {c['claim_id'] for c in raw['claims']}

        commitments, commitment_invisible = [], 0
        may_commitments = self._holds(COMMITMENT_READ)
        for r in raw['commitments']:
            if not may_commitments or (r.get('claim_id') in all_claims and r['claim_id'] not in visible_claims) or (r.get('claim_id') and r['claim_id'] not in all_claims):
                commitment_invisible += 1
                continue
            info = self._fields('Commitment', r['commitment_id'])
            wanted = [f['name'] for f in (info or {}).get('fields') or [] if f['name'] in ('status', 'due_at') and f['readable'] and f['present']]
            values = self.reader.read_object('Commitment', r['commitment_id'], wanted, 'consumer_evidence') if info and info['readable'] and 'status' in wanted else None
            if values is None or values.get('status') not in ('conditional', 'open', 'in_progress', 'fulfilled', 'breached', 'cancelled', 'superseded'):
                commitment_invisible += 1
                continue
            due = _ts(values.get('due_at')) if isinstance(values.get('due_at'), str) else None
            commitments.append({'commitment_id': r['commitment_id'], 'revision': int(info.get('revision') or 1), 'status': values['status'],
                                'due_at': due, 'origin': r['origin'], 'claim_id': r.get('claim_id'), 'replaces_commitment_id': r.get('replaces_commitment_id'),
                                '_intent': r.get('intent_id')})

        actions, action_invisible = [], 0
        if self._actions_readable():
            actions = [{'intent_id': str(a['intent_id']), 'operation': operation_code(a['action_name']), 'state': a['state'], 'created_at': _ts(a['created_at'])}
                       for a in raw['actions']]
        else:
            action_invisible = len(raw['actions'])

        outreach = [{'intent_id': o['intent_id'], 'purpose': o['purpose'], 'state': o['state'], 'created_at': _ts(o['created_at'])} for o in raw['outreach']] if owner else []

        candidates, candidate_invisible = [], 0
        for d in raw['candidates']:
            deps = d['dependent_claim_ids']
            if any(x not in visible_claims for x in deps):
                candidate_invisible += 1
                continue
            if d['candidate_kind'] == 'proposal' and d.get('property_name') and not (
                    d.get('type_name') == 'Consumer' and self._property_visible('Consumer', consumer_id, d['property_name'])):
                candidate_invisible += 1
                continue
            candidates.append({'candidate_kind': d['candidate_kind'], 'candidate_id': d['candidate_id'], 'status': _code(d['status']) or 'unknown',
                               'match_decision': _code(d.get('match_decision')), 'match_reason_code': _code(d.get('match_reason_code')),
                               'dependent_claim_ids': deps})

        lists = {'claim': (claims, claim_invisible + (0 if owner else claim_erased), len(raw['claims'])),
                 'commitment': (commitments, commitment_invisible, len(raw['commitments'])),
                 'action': (actions, action_invisible, len(raw['actions'])),
                 'outreach': (outreach, 0 if owner else raw['outreach_total'], raw['outreach_total']),
                 'candidate': (candidates, candidate_invisible, len(raw['candidates']))}
        groups, shown = [], {}
        for name in KINDS:
            items, invisible, total = lists[name]
            start = int(after) if kind == name and after else 0
            page = items[start:start + GROUP_LIMIT]
            groups.append({'kind': name, 'total': total, 'invisible': invisible, 'erased': (claim_erased if name == 'claim' else 0) if owner else None,
                           'collapsed': max(0, len(items) - start - len(page)),
                           'restricted_reason': 'owner_only' if name == 'outreach' and not owner else None,
                           'next_cursor': str(start + GROUP_LIMIT) if start + GROUP_LIMIT < len(items) else None, 'items': page})
            shown[name] = page
        listed = {c['claim_id'] for c in shown['claim']}
        edges = []
        for c in shown['claim']:
            c['successor_claim_ids'] = [s for s in c.pop('_successors') if s in listed][:REF_LIMIT]
            edges += [{'source': 'claim:' + c['claim_id'], 'target': 'claim:' + s, 'rel': 'corrected_by'} for s in c['successor_claim_ids']]
        listed_commitments = {}
        for r in shown['commitment']:
            intent = r.pop('_intent')
            if r['claim_id'] not in listed:
                r['claim_id'] = None
            else:
                edges.append({'source': 'commitment:' + r['commitment_id'], 'target': 'claim:' + r['claim_id'], 'rel': 'derived_from'})
            listed_commitments[intent] = r['commitment_id']
        for a in shown['action']:
            if a['intent_id'] in listed_commitments:
                edges.append({'source': 'action:' + a['intent_id'], 'target': 'commitment:' + listed_commitments[a['intent_id']], 'rel': 'fulfils'})
        for d in shown['candidate']:
            d['dependent_claim_ids'] = [x for x in d['dependent_claim_ids'] if x in listed][:GROUP_LIMIT]
            edges += [{'source': 'candidate:' + d['candidate_id'], 'target': 'claim:' + x, 'rel': 'depends_on'} for x in d['dependent_claim_ids']]
        from datetime import UTC, datetime
        return {'consumer_id': consumer_id, 'world': 'real', 'generated_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
                'group_limit': GROUP_LIMIT, 'groups': groups, 'edges': edges[:500]}

    def _actions_readable(self):
        # Actions and receipts are the owner's (the /effect-determination role, design §2.4).
        if 'actions' not in self._held:
            self._held['actions'] = self._role() == 'owner'
        return self._held['actions']

    def _role(self):
        if 'role' not in self._held:
            self._held['role'] = (self.reader.call('me') or {}).get('role')
        return self._held['role']

    # -- B: message trace ---------------------------------------------------------------------------------------------------------------
    def message_trace(self, *, message_id):
        raw = self._observe('message_trace', message_id=message_id)
        if raw is None:
            return None
        self._held['role'] = raw['role']
        self._held.pop('actions', None)
        if self.reader.call('conversation', conversation_id=raw['conversation_id']) is None:
            raise WorkbenchForbidden('conversation')
        if self.reader.read_object('Message', message_id, (), 'message_trace') is None:
            raise WorkbenchForbidden('message')
        claims, _, _ = self._claim_items(raw['claims'], raw['consumer_id'], 'message_trace')
        shown = {c['claim_id'] for c in claims}
        visible = [c for c in raw['claims'] if c['claim_id'] in shown]
        owner = raw['role'] == 'owner'

        def step(name, status, at=None, finished=None, refs=(), code=None):
            refs = list(dict.fromkeys(refs))
            return {'step': name, 'status': status, 'at': _ts(at), 'finished_at': _ts(finished), 'refs': refs[:REF_LIMIT],
                    'more': max(0, len(refs) - REF_LIMIT), 'code': _code(code)}

        inbound = raw['direction'] == 'inbound'
        relay, run = raw['relay'], raw['run']
        if not inbound:
            relay_step = step('relay', 'skipped')
        elif relay.get('handoff'):
            relay_step = step('relay', 'failed', relay['handoff']['at'], relay['handoff']['at'], code=relay['handoff']['reason'])
        elif run:
            relay_step = step('relay', 'done', run['issued_at'], run['issued_at'])
        elif relay.get('leased') or relay.get('attempts'):
            relay_step = step('relay', 'running', (relay.get('attempts') or {}).get('at'))
        else:
            relay_step = step('relay', 'none')
        contexts = raw['contexts']
        context_step = (step('context', 'done', contexts[0]['at'], contexts[-1]['at'], ['context:' + str(c['context_id']) for c in contexts]) if contexts
                        else step('context', 'skipped' if not inbound else 'none'))
        models = raw['models']
        if not run:
            run_step = step('run', 'skipped' if not inbound else 'none')
        else:
            states = {m['status'] for m in models}
            status = 'failed' if 'failed' in states else 'running' if (None in states or 'unknown' in states) else 'done'
            finished = models[-1]['finished_at'] if models and status != 'running' else None
            run_step = step('run', status, run['issued_at'], finished, ['run:' + str(run['run_id'])],
                            'model_unknown' if 'unknown' in states else 'model_failed' if 'failed' in states else None)
        intents = raw['intents']
        if not owner:
            action_step, receipt_step = step('action', 'restricted'), step('receipt', 'restricted')
        elif not intents:
            action_step, receipt_step = step('action', 'skipped'), step('receipt', 'skipped')
        else:
            states = {i['state'] for i in intents}
            status = 'failed' if 'failed' in states else 'running' if states & {'accepted', 'dispatching', 'unknown'} else 'done'
            action_step = step('action', status, intents[0]['at'], None, ['intent:' + str(i['intent_id']) for i in intents])
            ended = [i for i in intents if i['state'] in ('fulfilled', 'confirmed', 'failed')]
            receipt_step = (step('receipt', 'failed' if any(i['state'] == 'failed' for i in ended) else 'done', None, None,
                                 ['receipt:' + str(i['receipt_id']) for i in ended]) if ended
                            else step('receipt', 'running', code='unknown' if 'unknown' in states else None))
        extraction = raw.get('extraction') or {}
        extraction_step = (step('extraction', 'done', extraction.get('at'), extraction.get('at'), ['claim:' + c['claim_id'] for c in visible])
                           if extraction.get('runs') else step('extraction', 'none'))
        matched = [c for c in visible if c.get('match')]
        matching_step = (step('matching', 'done', matched[0]['match']['recorded_at'], matched[-1]['match']['recorded_at'],
                              ['match:' + c['claim_id'] for c in matched], matched[0]['match']['outcome']) if matched else step('matching', 'none'))
        proposals = [c['match']['proposal_id'] for c in matched if c['match'].get('proposal_id')]
        update_step = step('ontology_update', 'done', refs=['proposal:' + p for p in proposals]) if proposals else step('ontology_update', 'none')
        steps = [step('message', 'done', raw['accepted_at'], raw['accepted_at'], ['message:' + message_id]), relay_step, context_step, run_step,
                 action_step, receipt_step, extraction_step, matching_step, update_step]
        return {'message_id': message_id, 'conversation_id': raw['conversation_id'], 'world': 'real', 'steps': steps}

    # -- C: conversation metrics --------------------------------------------------------------------------------------------------------
    def conversation_metrics(self, *, conversation_id):
        raw = self._observe('conversation_metrics', conversation_id=conversation_id)
        if raw is None:
            return None
        if self.reader.call('conversation', conversation_id=conversation_id) is None:
            raise WorkbenchForbidden('conversation')
        if self.reader.read_object('Consumer', raw['consumer_id'], (), 'conversation_metrics') is None:
            raise WorkbenchForbidden('consumer')
        claims, _, _ = self._claim_items(raw['claims'], raw['consumer_id'], 'conversation_metrics')
        shown = {c['claim_id'] for c in claims}
        all_claims = {c['claim_id'] for c in raw['claims']}
        commitments = 0
        if self._holds(COMMITMENT_READ):
            for r in raw['commitments']:
                if r.get('claim_id') and (r['claim_id'] not in all_claims or r['claim_id'] not in shown):
                    continue
                info = self._fields('Commitment', r['commitment_id'])
                if info and info['readable']:
                    commitments += 1
        from datetime import UTC, datetime
        owner = raw['role'] == 'owner'
        return {'conversation_id': conversation_id, 'world': 'real', 'as_of': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
                'response_ms': raw['response_ms'], 'model_cost': raw['model_cost'] if owner else None, 'claims': len(claims),
                'commitments': commitments, 'gate_blocks': {'total': raw['gate_blocks']['total'],
                    'by_code': [{'code': _code(g['code']) or 'other', 'count': g['count']} for g in raw['gate_blocks']['by_code']]},
                'withheld': [] if owner else ['model_cost']}
