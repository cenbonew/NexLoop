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
         'plans': WORKBENCH_READ, 'actions': WORKBENCH_READ, 'takeovers': WORKBENCH_READ, 'settings': WORKBENCH_READ,
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
        return {'items': items, 'next_cursor': items[-1]['consumer_id'] if len(items) == limit else None}

    def consumer(self, *, consumer_id):
        value = self.reader.call('consumer', consumer_id=consumer_id)
        if value is None:
            return None
        # AT-003: Consumer properties only through per-property READ; withheld ones are listed, never shown.
        value['properties'] = self._consumer_properties(consumer_id)
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

    def plans(self, *, consumer_id=None):
        return self.reader.call('plans', **({'consumer_id': consumer_id} if consumer_id else {}))

    def actions(self, *, state=None, consumer_id=None, limit=50):
        return self.reader.call('actions', limit=limit, **({'state': state} if state else {}), **({'consumer_id': consumer_id} if consumer_id else {}))

    def takeovers(self):
        return self.reader.call('takeovers')

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
    # reads (ontology.schema.review EXECUTE; a decided candidate is no longer returned); the evidence Message bodies are read
    # through the ADR-025 reviewer derivation (pending_review evidence only, audited with purpose review_evidence).
    def _review(self):
        from nexloop_eios.candidate_merge import ReviewQueueReader
        return ReviewQueueReader(self.reader.pool, self.reader.session, self.reader.signer)

    def review_queue(self, *, limit=50):
        try:
            items = self._review().pending(limit=limit)
        except PermissionError:
            raise WorkbenchForbidden('review') from None
        return {'items': [{'candidate_id': x['candidate_id'], 'kind': x['kind'], 'display_name': (x.get('candidate') or {}).get('proposed', {}).get('display_name'),
                           'created_at': x['created_at'], 'evidence_count': x['dependent_claim_count']} for x in items]}

    def review_evidence(self, *, candidate_id):
        try:
            detail = self._review().candidate(candidate_id)
        except PermissionError:
            raise WorkbenchForbidden('review') from None
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

    def human_actions(self, *, limit=100):
        return self.reader.observe('human_actions', limit=limit)
