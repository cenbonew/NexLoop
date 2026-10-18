"""NX-028 slice 1: the owner workbench's governed human reads (0141, design §3 and §15.2).

Every call runs on the actual Human's own current browser session (re-authenticated in PostgreSQL per request) and
needs that Human's current EXECUTE on the verb's read Action; SQL also requires the session to belong to the tenant's
workbench application and the principal to be a current workbench member whose role carries that Action, and never a
customer principal. Message content (bodies, actors, the promised words, a refusal hit's matched text) and Consumer
properties are read through the ADR-025 `workbench-member-v1` derivation: SQL decides from the live member and role at every
read (owner/operator: the tenant's Messages and Consumers except owner-restricted property groups; reviewer: evidence of
pending review items only) and appends an audit row with the read's purpose. Anything not derived is withheld and marked
(AT-003), never blanked silently.
"""
from datetime import UTC, datetime, timedelta
import hashlib
import hmac

import psycopg
from eios.authz.errors import AuthorizationError

from nexloop_eios.postgres_artifacts import canonical_payload

PROTOCOL = 'nexloop-workbench-read-v1'
WORKBENCH_READ = 'nexloop.workbench.read'
COMMITMENT_READ = 'nexloop.commitment.read'
CONTACT_READ = 'nexloop.contact.read'
VERBS = {'goals': WORKBENCH_READ, 'consumers': WORKBENCH_READ, 'consumer': WORKBENCH_READ, 'conversation': WORKBENCH_READ,
         'plans': WORKBENCH_READ, 'actions': WORKBENCH_READ, 'takeovers': WORKBENCH_READ, 'settings': WORKBENCH_READ, 'me': WORKBENCH_READ,
         'commitments': COMMITMENT_READ, 'commitment': COMMITMENT_READ, 'contact': CONTACT_READ}


class WorkbenchForbidden(PermissionError):
    pass


def _denied(error):
    return isinstance(error, (PermissionError, AuthorizationError, psycopg.errors.InsufficientPrivilege))


class WorkbenchReader:
    def __init__(self, pool, session, signer):
        self.pool, self.session, self.signer = pool, session, signer

    def call(self, verb, **payload):
        from nexloop_eios.assembly import verify_application_role
        from nexloop_eios.context_engine.authority import action_claims
        if verb not in VERBS:
            raise ValueError('workbench verb')
        try:
            claims = action_claims(self.pool, self.session, VERBS[verb])
        except Exception as error:
            if _denied(error):
                raise WorkbenchForbidden(verb) from None
            raise
        body = canonical_payload({'verb': verb, **payload})
        claims = {**claims, 'protocol': PROTOCOL, 'key_id': self.signer.key_id,
                  'parameters_digest': hashlib.sha256(body.encode()).hexdigest()}
        text = canonical_payload(claims)
        signature = hmac.new(self.signer.material, (PROTOCOL + ':' + text).encode(), 'sha256').hexdigest()
        try:
            with self.pool.connection() as db, db.transaction():
                verify_application_role(db)
                return db.execute('select authz.nexloop_workbench_read(%s,%s,%s,%s,%s)',
                    (self.session.token_digest, self.session.world, text, signature, body)).fetchone()[0]
        except psycopg.errors.InsufficientPrivilege:
            raise WorkbenchForbidden(verb) from None

    # ADR-025: role-derived content reads, each audited with its purpose by SQL.
    def _derived(self, kind, name, purpose):
        auth = self.session.authentication
        target = f'eios:{kind}:{name}'
        return {'tenant_id': auth.tenant_id, 'principal_id': auth.subject_principal_id, 'credential_id': auth.credential_id,
                'directory_hash': self.session.directory_hash, 'world': self.session.world, 'resource_id': target, 'target_resource': target,
                'operation': 'read', 'expires_at': (datetime.now(UTC) + timedelta(seconds=25)).isoformat(), 'facts': [],
                'derivation': 'workbench-member-v1', 'read_purpose': purpose}

    def read_object(self, type_name, object_id, fields, purpose):
        """Properties dict, or None when the member's role does not derive this read (withheld, AT-003)."""
        from nexloop_eios.assembly import verify_application_role
        fields = tuple(sorted(set(fields)))
        claims = self._derived('object', f'{type_name}/{object_id}', purpose)
        claims.update(protocol='nexloop-object-read-v1', key_id=self.signer.key_id, type_name=type_name, object_id=object_id, fields=fields,
                      property_authorities=[self._derived('property', f'{type_name}/{object_id}/{f}', purpose) for f in fields])
        text = canonical_payload(claims)
        signature = hmac.new(self.signer.material, ('nexloop-object-read-v1:' + text).encode(), 'sha256').hexdigest()
        try:
            with self.pool.connection() as db, db.transaction():
                verify_application_role(db)
                value = db.execute('select authz.nexloop_read_object(%s,%s,%s,%s)', (self.session.token_digest, self.session.world, text, signature)).fetchone()[0]
        except psycopg.errors.InsufficientPrivilege:
            return None
        return value.get('properties') if isinstance(value, dict) else None

    def message_fields(self, message_id, fields, purpose='conversation'):
        return self.read_object('Message', message_id, fields, purpose)

    def _readable(self, message_id, purpose):
        return message_id is not None and self.message_fields(message_id, ('body',), purpose) is not None

    def consumer_fields(self, consumer_id):
        from nexloop_eios.assembly import verify_application_role
        try:
            with self.pool.connection() as db, db.transaction():
                verify_application_role(db)
                return db.execute('select authz.nexloop_workbench_consumer_fields(%s,%s,%s)', (self.session.token_digest, self.session.world, consumer_id)).fetchone()[0]
        except psycopg.errors.InsufficientPrivilege:
            raise WorkbenchForbidden('consumer fields') from None

    def observe(self, verb, **payload):
        """NX-030 (D7): metrics, alerts and the human-action audit through their own function (0151); 0117 unchanged."""
        from nexloop_eios.assembly import verify_application_role
        from nexloop_eios.context_engine.authority import action_claims
        try:
            claims = action_claims(self.pool, self.session, WORKBENCH_READ)
        except Exception as error:
            if _denied(error):
                raise WorkbenchForbidden(verb) from None
            raise
        body = canonical_payload({'verb': verb, **payload})
        claims = {**claims, 'protocol': 'nexloop-workbench-observe-v1', 'key_id': self.signer.key_id, 'parameters_digest': hashlib.sha256(body.encode()).hexdigest()}
        text = canonical_payload(claims)
        signature = hmac.new(self.signer.material, ('nexloop-workbench-observe-v1:' + text).encode(), 'sha256').hexdigest()
        try:
            with self.pool.connection() as db, db.transaction():
                verify_application_role(db)
                return db.execute('select authz.nexloop_workbench_observe_read(%s,%s,%s,%s,%s)', (self.session.token_digest, self.session.world, text, signature, body)).fetchone()[0]
        except psycopg.errors.InsufficientPrivilege:
            raise WorkbenchForbidden(verb) from None

    def audit(self, *, limit=100, before=None):
        """ADR-025 §2.3: the read audit, owner only (SQL refuses everyone else)."""
        from nexloop_eios.assembly import verify_application_role
        from nexloop_eios.context_engine.authority import action_claims
        try:
            claims = action_claims(self.pool, self.session, WORKBENCH_READ)
        except Exception as error:
            if _denied(error):
                raise WorkbenchForbidden('audit') from None
            raise
        body = canonical_payload({'limit': limit, **({'before': str(before)} if before else {})})
        claims = {**claims, 'protocol': 'nexloop-workbench-audit-v1', 'key_id': self.signer.key_id, 'parameters_digest': hashlib.sha256(body.encode()).hexdigest()}
        text = canonical_payload(claims)
        signature = hmac.new(self.signer.material, ('nexloop-workbench-audit-v1:' + text).encode(), 'sha256').hexdigest()
        try:
            with self.pool.connection() as db, db.transaction():
                verify_application_role(db)
                return db.execute('select authz.nexloop_workbench_audit_read(%s,%s,%s,%s,%s)', (self.session.token_digest, self.session.world, text, signature, body)).fetchone()[0]
        except psycopg.errors.InsufficientPrivilege:
            raise WorkbenchForbidden('audit') from None


def _section(load):
    """One overview block: ok with data, forbidden (no grant), or unavailable; never a silent zero."""
    try:
        return {'status': 'ok', 'data': load()}
    except WorkbenchForbidden:
        return {'status': 'forbidden', 'data': None}
    except Exception:
        return {'status': 'unavailable', 'data': None}


class WorkbenchQueries:
    """Read verbs with AT-003 content filtering; one instance per authenticated request."""

    def __init__(self, reader):
        self.reader = reader

    def overview(self):
        r = self.reader

        def goals():
            g = r.call('goals', limit=20)
            return {'goals': [{'goal_id': x['goal_id'], 'goal_kind': x['goal_kind'], 'objective': x['objective'], 'current_version': x['current_version'],
                               'key_results': len(x['key_results'])} for x in g['goals']],
                    'control_revision': g['control']['revision'], 'paused_scopes': [s for s in g['control']['scopes'] if s['paused']]}

        def commitments():
            c = r.call('commitments')
            reasons = {}
            for e in c['exceptions']:
                reasons[e['reason']] = reasons.get(e['reason'], 0) + 1
            statuses = [x['properties'].get('status') for x in c['commitments']]
            return {'open': len(statuses), 'breached': statuses.count('breached'),
                    'exceptions': [{'reason': k, 'count': v} for k, v in sorted(reasons.items())]}

        def actions():
            return r.call('actions', state='unknown', limit=1)['unknown']

        def contact():
            c = r.call('contact')
            return {'restricted': sum(1 for x in c['restrictions'] if x['active']), 'escalations': len(c['escalations'])}

        def takeovers():
            t = r.call('takeovers')
            if t.get('status') != 'ok':
                raise LookupError('takeovers unavailable')
            return t

        def unavailable():
            raise LookupError('not in this slice')

        def backlog():
            # NX-030 M06/M07: queues, work feeds and the extraction feed from the SQL metrics snapshot.
            m = r.observe('metrics')
            return {'queues': m['queues'], 'feeds': m['feeds'], 'extraction': m['extraction']}

        return {'goals': _section(goals), 'commitments': _section(commitments), 'actions': _section(actions),
                'contact': _section(contact), 'takeovers': _section(takeovers),
                'backlog': _section(backlog),
                # Commercial/cost summaries (NX-027) are not served here yet.
                'commercial': _section(unavailable)}

    def goals(self, *, limit=50):
        return self.reader.call('goals', limit=limit)

    def consumers(self, *, after=None, limit=50):
        payload = {'limit': limit, **({'after': after} if after else {})}
        items = self.reader.call('consumers', **payload)['consumers']
        for item in items:
            item['display'] = self._display_name(item['consumer_id'])
        return {'items': items, 'next_cursor': items[-1]['consumer_id'] if len(items) == limit else None}

    def _display_name(self, consumer_id):
        # Desktop usability (2026-10-17): a readable name in the list through the same per-property READ as the detail view
        # (ADR-025 / AT-003). No new grant: an owner-restricted display_name is 'withheld' (never read), a member without the
        # property read gets 'forbidden', a Consumer without the property 'absent'; the page then shows a non-identifying alias.
        try:
            fields = self.reader.consumer_fields(consumer_id) or []
        except WorkbenchForbidden:
            return {'status': 'forbidden'}
        field = next((f for f in fields if f['name'] == 'display_name'), None)
        if field is None or not field['present']:
            return {'status': 'absent'}
        if not field['readable']:
            return {'status': 'withheld'}
        values = self.reader.read_object('Consumer', consumer_id, ['display_name'], 'consumer')
        name = (values or {}).get('display_name')
        if not isinstance(name, str) or not name.strip():
            return {'status': 'forbidden' if values is None else 'absent'}
        return {'status': 'ok', 'name': name.strip()[:80]}

    def consumer(self, *, consumer_id):
        value = self.reader.call('consumer', consumer_id=consumer_id)
        if value is None:
            return None
        # AT-003: Consumer properties only through per-property READ; withheld ones are listed, never shown.
        value['properties'] = self._consumer_properties(consumer_id)
        # The name comes from the properties just read (no second read, no second audit row).
        props = value['properties']
        name = (props['values'] or {}).get('display_name') if props['status'] == 'ok' else None
        value['display'] = ({'status': 'ok', 'name': name.strip()[:80]} if isinstance(name, str) and name.strip()
                            else {'status': 'forbidden'} if props['status'] != 'ok'
                            else {'status': 'withheld'} if 'display_name' in props['withheld'] else {'status': 'absent'})
        # Desktop usability: each conversation's last message time through the conversation read (same WORKBENCH_READ),
        # newest first; a conversation without messages sorts last.
        for item in value['conversations']:
            view = self.reader.call('conversation', conversation_id=item['conversation_id']) or {'messages': []}
            messages = view['messages']
            item['last_message_at'] = messages[-1]['accepted_at'] if messages else None
            item['messages'] = len(messages)
        value['conversations'].sort(key=lambda c: (c['last_message_at'] is not None, c['last_message_at'] or '', c['conversation_id']), reverse=True)
        return value

    def _consumer_properties(self, consumer_id):
        # ADR-025: owner-restricted groups are listed as withheld, never read; SQL re-checks every field it returns.
        try:
            fields = self.reader.consumer_fields(consumer_id) or []
        except WorkbenchForbidden:
            return {'status': 'forbidden', 'values': None, 'withheld': []}
        readable = [f['name'] for f in fields if f['readable'] and f['present']]
        values = self.reader.read_object('Consumer', consumer_id, readable, 'consumer')
        if values is None:
            return {'status': 'forbidden', 'values': None, 'withheld': []}
        return {'status': 'ok', 'values': values, 'withheld': sorted(f['name'] for f in fields if not f['readable'])}

    def conversation(self, *, conversation_id):
        value = self.reader.call('conversation', conversation_id=conversation_id)
        if value is None:
            return None
        for item in value['messages']:
            content = self.reader.message_fields(item['id'], ('actor', 'body'), 'conversation')
            item['content'] = {'status': 'ok', 'actor': content.get('actor'), 'body': content.get('body')} if content is not None else {'status': 'restricted'}
        return value

    def consumer_history(self, *, consumer_id, before=None, limit=10):
        """The customer's recent exchanges across all of their conversations, newest first, `limit` per page (owner feedback 2026-10-18:
        a member who takes over sees what the customer said before). Composed of the existing governed reads only: the consumer read
        (its conversations), the conversation read (order, direction, time) and the per-message READ for bodies, made for the page's
        messages only (each audited with the purpose 'conversation'); a body the member may not read is 'restricted'. Cursor:
        '<accepted_at>|<message id>' of the last item; the next page holds the strictly older messages."""
        value = self.reader.call('consumer', consumer_id=consumer_id)
        if value is None:
            return None
        items = []
        for conversation in value['conversations']:
            view = self.reader.call('conversation', conversation_id=conversation['conversation_id']) or {'messages': []}
            for m in view['messages']:
                items.append({'id': m['id'], 'conversation_id': conversation['conversation_id'], 'sequence': m['sequence'], 'accepted_at': m['accepted_at'],
                              'direction': m['direction'], 'sender_kind': m['sender_kind']})
        items.sort(key=lambda x: (x['accepted_at'] or '', x['id']), reverse=True)
        if before is not None:
            at, _, message_id = before.rpartition('|')
            items = [x for x in items if ((x['accepted_at'] or ''), x['id']) < (at, message_id)]
        page = items[:limit]
        for item in page:
            content = self.reader.message_fields(item['id'], ('actor', 'body'), 'conversation')
            item['content'] = {'status': 'ok', 'actor': content.get('actor'), 'body': content.get('body')} if content is not None else {'status': 'restricted'}
        last = page[-1] if page else None
        return {'consumer_id': consumer_id, 'conversations': len(value['conversations']), 'items': page,
                'next_cursor': f"{last['accepted_at']}|{last['id']}" if last and len(items) > limit else None}

    def plans(self, *, consumer_id=None):
        return self.reader.call('plans', **({'consumer_id': consumer_id} if consumer_id else {}))

    def actions(self, *, state=None, consumer_id=None, limit=50):
        return self.reader.call('actions', limit=limit, **({'state': state} if state else {}), **({'consumer_id': consumer_id} if consumer_id else {}))

    def takeovers(self):
        return self.reader.call('takeovers')

    def me(self):
        # NX-028 page integration (0160): role and role Actions, for showing only the controls the role carries.
        return self.reader.call('me')

    def settings(self):
        return self.reader.call('settings')

    def _commitment(self, view):
        # The promised words are Message content: shown only with that Message's READ (design §6, AT-003).
        source = view.pop('source_message_id', None)
        if view.get('quote') is not None and not self.reader._readable(source, 'commitment'):
            view['quote'] = None
            view['quote_status'] = 'restricted'
        else:
            view['quote_status'] = 'ok' if view.get('quote') is not None else ('unavailable' if not view.get('source_available') else 'none')
        return view

    def commitments(self, *, consumer_id=None, include_closed=False):
        value = self.reader.call('commitments', **({'consumer_id': consumer_id} if consumer_id else {}), **({'all': True} if include_closed else {}))
        value['commitments'] = [self._commitment(v) for v in value['commitments']]
        return value

    def commitment(self, *, commitment_id):
        value = self.reader.call('commitment', commitment_id=commitment_id)
        return None if value is None else self._commitment(value)

    def contact(self, *, consumer_id=None, include_released=False):
        value = self.reader.call('contact', **({'consumer_id': consumer_id} if consumer_id else {}), **({'all': True} if include_released else {}))
        readable = {}

        def visible(message_id):
            if message_id not in readable:
                readable[message_id] = self.reader._readable(message_id, 'contact')
            return readable[message_id]

        # A refusal hit's matched text is the customer's own words (design §5).
        for restriction in value['restrictions']:
            for item in [restriction, *restriction['hits']]:
                if visible(item['message_id']):
                    item['matched_text_status'] = 'ok'
                else:
                    item['matched_text'] = None
                    item['matched_text_status'] = 'restricted'
        return value

    def audit(self, *, limit=100, before=None):
        return self.reader.audit(limit=limit, before=before)

    # NX-028 known limitation closed: the reviewer's evidence page. The queue and the candidate come from the NX-046 review
    # reads (ontology.schema.review EXECUTE, or the owner's read-only nexloop.knowledge.read since the D2 revision of
    # 2026-10-18; a decided candidate is no longer returned); the evidence Message bodies are read through the ADR-025
    # derivation (the reviewer: pending_review evidence only; the owner: as any Message), audited with purpose review_evidence.
    def _review_read(self, read):
        """read(reader) with the member's own grant: the reviewer's ontology.schema.review, else the owner's read-only
        nexloop.knowledge.read (0213, NX-028 D2 revision); neither is a 403. The SQL read accepts exactly these two."""
        from nexloop_eios.candidate_merge import KNOWLEDGE_READ_ACTION, REVIEW_ACTION, ReviewQueueReader
        for action in (REVIEW_ACTION, KNOWLEDGE_READ_ACTION):
            try:
                return read(ReviewQueueReader(self.reader.pool, self.reader.session, self.reader.signer, action=action))
            except PermissionError:
                continue
        raise WorkbenchForbidden('review')

    def review_queue(self, *, limit=50):
        items = self._review_read(lambda reader: reader.pending(limit=limit))
        return {'items': [{'candidate_id': x['candidate_id'], 'kind': x['kind'], 'display_name': (x.get('candidate') or {}).get('proposed', {}).get('display_name'),
                           'created_at': x['created_at'], 'evidence_count': x['dependent_claim_count']} for x in items]}

    def review_evidence(self, *, candidate_id):
        detail = self._review_read(lambda reader: reader.candidate(candidate_id))
        if detail is None:
            # Decided, superseded or unknown: the review is over and its evidence is no longer readable for the reviewer.
            return {'candidate_id': candidate_id, 'status': 'ended', 'evidence': []}
        evidence = []
        for item in detail.get('evidence', []):
            message = item.get('source_message_id')
            content = self.reader.message_fields(message, ('body',), 'review_evidence') if message else None
            evidence.append({'claim_id': item['claim_id'], 'predicate': item.get('predicate'), 'source_message_id': message,
                             'content': {'status': 'ok', 'body': content.get('body')} if content is not None else {'status': 'restricted'}})
        return {'candidate_id': candidate_id, 'status': 'pending_review', 'kind': detail['kind'],
                'display_name': (detail.get('candidate') or {}).get('proposed', {}).get('display_name'), 'evidence': evidence}

    # NX-030: metrics snapshot, alerts (world real only, D6) and the owner's human-action audit.
    def metrics(self):
        return self.reader.observe('metrics')

    def alerts(self, *, limit=100):
        return self.reader.observe('alerts', limit=limit)

    def effect_determination(self, intent_id):
        # 0210: the owner's read-only precheck before confirming an unknown effect did not happen (codes and times only).
        return self.reader.observe('effect_determination', intent_id=intent_id)

    def human_actions(self, *, limit=100):
        return self.reader.observe('human_actions', limit=limit)
