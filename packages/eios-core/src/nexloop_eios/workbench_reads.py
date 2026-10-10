"""NX-028 slice 1: the owner workbench's governed human reads (0141, design §3 and §15.2).

Every call runs on the actual Human's own current browser session (re-authenticated in PostgreSQL per request) and
needs that Human's current EXECUTE on the verb's read Action; SQL also requires the session to belong to the tenant's
workbench application and the principal to be a current workbench member whose role carries that Action, and never a
customer principal. Message content (bodies, actors, the promised words, a refusal hit's matched text) is shown only
where the same Human also holds that Message's READ (AT-003): otherwise it is withheld and marked, never blanked silently.
"""
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
        self._objects = None

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

    # Message content under the caller's own Message READ (configured grants or the 0077/0086 derivation).
    def message_fields(self, message_id, fields):
        from nexloop_eios.object_reads import AuthorizedObjectReader
        if self._objects is None:
            self._objects = AuthorizedObjectReader(self.pool, self.session, self.signer)
        try:
            value = self._objects.get('Message', message_id, fields=tuple(fields))
        except Exception as error:
            if _denied(error):
                return None
            raise
        return (value or {}).get('properties') if isinstance(value, dict) else None

    def _readable(self, message_id):
        return message_id is not None and self.message_fields(message_id, ('body',)) is not None


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

        return {'goals': _section(goals), 'commitments': _section(commitments), 'actions': _section(actions),
                'contact': _section(contact), 'takeovers': _section(takeovers),
                # Work-feed backlog and commercial/cost summaries (NX-027) are not served by slice 1.
                'backlog': _section(unavailable), 'commercial': _section(unavailable)}

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
        from nexloop_eios.object_reads import AuthorizedObjectReader
        reader = AuthorizedObjectReader(self.reader.pool, self.reader.session, self.reader.signer)
        try:
            value = reader.get('Consumer', consumer_id)
        except Exception as error:
            if _denied(error):
                return {'status': 'forbidden', 'values': None}
            raise
        return {'status': 'ok', 'values': (value or {}).get('properties', {})}

    def conversation(self, *, conversation_id):
        value = self.reader.call('conversation', conversation_id=conversation_id)
        if value is None:
            return None
        for item in value['messages']:
            content = self.reader.message_fields(item['id'], ('actor', 'body'))
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
        if view.get('quote') is not None and not self.reader._readable(source):
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
                readable[message_id] = self.reader._readable(message_id)
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
