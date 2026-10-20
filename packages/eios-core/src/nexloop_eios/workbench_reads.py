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
OPEN_STATUSES = ('conditional', 'open', 'in_progress', 'breached')
VERBS = {'goals': WORKBENCH_READ, 'consumers': WORKBENCH_READ, 'consumer': WORKBENCH_READ, 'conversation': WORKBENCH_READ,
         'plans': WORKBENCH_READ, 'actions': WORKBENCH_READ, 'takeovers': WORKBENCH_READ, 'settings': WORKBENCH_READ, 'me': WORKBENCH_READ,
         'world_model': WORKBENCH_READ,
         'capabilities': WORKBENCH_READ, 'objects': WORKBENCH_READ, 'object_neighbors': WORKBENCH_READ, 'artifacts': WORKBENCH_READ,
         'commitments': COMMITMENT_READ, 'commitment': COMMITMENT_READ, 'contact': CONTACT_READ}


def _instant(value):
    """An aware datetime from an ISO / PostgreSQL timestamp text (commitment due times are UTC)."""
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00').replace(' ', 'T'))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


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

    def _all_consumers(self):
        """Every Consumer row of the tenant (ids and counts only, no content read), in pages of the SQL maximum."""
        rows, after = [], None
        while True:
            page = self.reader.call('consumers', limit=200, **({'after': after} if after else {}))['consumers']
            rows += page
            if len(page) < 200:
                return rows
            after = page[-1]['consumer_id']

    def consumers(self, *, after=None, limit=50, restricted=None, has_open_commitments=None, sort=None, order='asc', cursor=None):
        """Walkthrough F09: filters on the counts the list already carries (contact restriction, open commitments), sorting, a total
        and an offset cursor; names (one per-property READ each, F04) only for the rows of the page. Without filters or sort the
        original id-cursor page (after) is served unchanged."""
        if restricted is None and has_open_commitments is None and sort is None and cursor is None:
            payload = {'limit': limit, **({'after': after} if after else {})}
            items = self.reader.call('consumers', **payload)['consumers']
            for item in items:
                item['display'] = self.display_state(item['consumer_id'])
            return {'items': items, 'next_cursor': items[-1]['consumer_id'] if len(items) == limit else None}
        rows = self._all_consumers()
        tenant_total = len(rows)
        if restricted is not None:
            rows = [r for r in rows if bool(r['restricted']) == restricted]
        if has_open_commitments is not None:
            rows = [r for r in rows if (r['open_commitments'] > 0) == has_open_commitments]
        key = sort or 'id'
        rows.sort(key=lambda r: (r['consumer_id'],) if key == 'id' else (r[key], r['consumer_id']), reverse=order == 'desc')
        start = int(cursor or 0)
        items = rows[start:start + limit]
        for item in items:
            item['display'] = self.display_state(item['consumer_id'])
        return {'items': items, 'total': len(rows), 'tenant_total': tenant_total,
                'next_cursor': str(start + limit) if start + limit < len(rows) else None}

    def display_state(self, consumer_id, fields=None, values=None):
        """Walkthrough F04: one name state for every page, decided in this order: failed (the read itself failed), restricted (no
        Consumer read for this member: reason forbidden; or display_name owner-restricted: reason withheld, whether or not the
        Consumer has one, so a restriction never tells whether a name exists), missing (readable but not recorded), ok. ADR-025 /
        AT-003: no new grant; the name comes through the per-property READ (one audit row), or from `values` already read."""
        if not consumer_id:
            return {'status': 'missing'}
        try:
            fields = self.reader.consumer_fields(consumer_id) if fields is None else fields
        except WorkbenchForbidden:
            return {'status': 'restricted', 'reason': 'forbidden'}
        except Exception:
            return {'status': 'failed'}
        if fields is None:
            return {'status': 'failed'}
        field = next((f for f in fields if f['name'] == 'display_name'), None)
        if field is not None and not field['readable']:
            return {'status': 'restricted', 'reason': 'withheld'}
        if field is None or field.get('present') is False:
            return {'status': 'missing'}
        if values is None:
            try:
                values = self.reader.read_object('Consumer', consumer_id, ['display_name'], 'consumer')
            except Exception:
                return {'status': 'failed'}
            if values is None:
                return {'status': 'restricted', 'reason': 'forbidden'}
        name = values.get('display_name')
        if not isinstance(name, str) or not name.strip():
            return {'status': 'missing'}
        return {'status': 'ok', 'name': name.strip()[:80]}

    def consumer(self, *, consumer_id):
        value = self.reader.call('consumer', consumer_id=consumer_id)
        if value is None:
            return None
        # AT-003: Consumer properties only through per-property READ; withheld ones are listed, never shown. The name state is
        # decided from the same field list and the values just read (no second read, no second audit row; F04).
        try:
            fields = self.reader.consumer_fields(consumer_id) or []
        except WorkbenchForbidden:
            fields = None
        value['properties'] = self._consumer_properties(consumer_id, fields)
        props = value['properties']
        value['display'] = ({'status': 'restricted', 'reason': 'forbidden'} if fields is None or props['status'] != 'ok'
                            else self.display_state(consumer_id, fields, props['values']))
        # Desktop usability: each conversation's last message time through the conversation read (same WORKBENCH_READ),
        # newest first; a conversation without messages sorts last.
        for item in value['conversations']:
            view = self.reader.call('conversation', conversation_id=item['conversation_id']) or {'messages': []}
            messages = view['messages']
            item['last_message_at'] = messages[-1]['accepted_at'] if messages else None
            item['messages'] = len(messages)
        value['conversations'].sort(key=lambda c: (c['last_message_at'] is not None, c['last_message_at'] or '', c['conversation_id']), reverse=True)
        return value

    def _consumer_properties(self, consumer_id, fields):
        # ADR-025: owner-restricted groups are listed as withheld, never read; SQL re-checks every field it returns.
        if fields is None:
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

    def world_model(self):
        # Enterprise world model W1 (0230): published model metadata (types, relations, events, Actions, Functions, counts); read only.
        return self.reader.call('world_model')

    def capabilities(self):
        # World model W3: the tenant's capability catalog (metadata only; dispatch switches from the metrics source, 0153).
        return self.reader.call('capabilities')

    def artifacts(self, *, before=None, limit=50):
        """World model W4: stored Artifacts as metadata, newest first (SQL lists, hides those of a Consumer being erased and audits
        each listed Artifact in the same transaction; owner and operator only)."""
        value = self.reader.call('artifacts', limit=limit, **({'before': before} if before else {}))
        value.pop('audited', None)
        return value

    def _readable_object(self, type_name, object_id, fields=(), cache=None):
        """World model W2: the ADR-025 member read of one object (and fields), audited with purpose 'world_model' by SQL; False when the
        member's role does not derive it. Nothing is shown from the values read here: they only decide visibility."""
        key = (type_name, object_id, tuple(fields))
        if cache is not None and key in cache:
            return cache[key]
        visible = self.reader.read_object(type_name, object_id, list(fields), 'world_model') is not None
        if cache is not None:
            cache[key] = visible
        return visible

    def objects(self, *, type_name, after=None, limit=50):
        """World model W2: a page of a type's instances, identifiers, versions and times only; each object's READ is checked (ADR-025,
        audited); unreadable objects are counted as invisible, never listed."""
        value = self.reader.call('objects', type_name=type_name, limit=limit, **({'after': after} if after else {}))
        page = value['items']
        visible = [item for item in page if self._readable_object(type_name, item['object_id'])]
        return {'type_name': type_name, 'items': visible, 'invisible': len(page) - len(visible), 'total': value['total'],
                'next_after': page[-1]['object_id'] if len(page) == limit else None}

    def object_neighbors(self, *, type_name, object_id):
        """World model W2: depth 1, at most 25 neighbours a group. The centre must be readable (else 403); a neighbour is shown only
        when its own READ holds and, for a property edge, the READ of the referencing property on whichever object holds it; the rest
        of a group's candidates are counted as invisible, and those beyond the 25 as collapsed (not checked, not shown)."""
        value = self.reader.call('object_neighbors', type_name=type_name, object_id=object_id)
        if value is None:
            return None
        cache = {}
        if not self._readable_object(type_name, object_id, cache=cache):
            raise WorkbenchForbidden('object')
        groups = []
        for group in value['groups']:
            prop = group['via'][len('property:'):] if group['via'].startswith('property:') else None
            items, invisible = [], 0
            for neighbour in group['items'] or []:
                if prop is None:
                    ok = self._readable_object(group['type_name'], neighbour, cache=cache)
                elif group['property_holder'] == 'centre':
                    ok = self._readable_object(type_name, object_id, (prop,), cache) and self._readable_object(group['type_name'], neighbour, cache=cache)
                else:
                    ok = self._readable_object(group['type_name'], neighbour, (prop,), cache)
                if ok:
                    items.append({'type_name': group['type_name'], 'object_id': neighbour})
                else:
                    invisible += 1
            groups.append({'direction': group['direction'], 'via': group['via'], 'type_name': group['type_name'], 'items': items,
                           'invisible': invisible, 'collapsed': group['total'] - len(group['items'] or [])})
        return {'type_name': type_name, 'object_id': object_id, 'depth': 1, 'group_limit': 25, 'groups': groups}

    def _commitment(self, view):
        # The promised words are Message content: shown only with that Message's READ (design §6, AT-003).
        source = view.pop('source_message_id', None)
        if view.get('quote') is not None and not self.reader._readable(source, 'commitment'):
            view['quote'] = None
            view['quote_status'] = 'restricted'
        else:
            view['quote_status'] = 'ok' if view.get('quote') is not None else ('unavailable' if not view.get('source_available') else 'none')
        return view

    def commitments(self, *, consumer_id=None, include_closed=False, status_group=None, sort='registered_at', order='asc', late=None,
                    due_before=None, due_after=None, cursor=None, limit=200):
        """Walkthrough F03/F05/F09. One governed read of the tenant's (or one customer's) commitments, all statuses; then, here, the
        status group (open / ended / all; the legacy `all` flag is group all), the late flag and due window, sorting and an offset
        page. The list, its counts and its exceptions come from that one result, so they always describe the same scope: exceptions
        of the commitments in scope, plus pre-registration ones (subject not a commitment) in the open and all groups. The promised
        words (one Message READ each) and the customer names (one per distinct customer, F04/D3) are read for the page only."""
        group = status_group or ('all' if include_closed else 'open')
        value = self.reader.call('commitments', all=True, **({'consumer_id': consumer_id} if consumer_id else {}))
        rows = value['commitments']
        is_open = lambda v: v['properties'].get('status') in OPEN_STATUSES  # noqa: E731
        counts = {'open': sum(1 for v in rows if is_open(v)), 'ended': sum(1 for v in rows if not is_open(v))}
        if group != 'all':
            rows = [v for v in rows if is_open(v) == (group == 'open')]
        if late is not None:
            rows = [v for v in rows if bool(v['properties'].get('late')) == late]
        due = lambda v: v['properties'].get('due_at') if isinstance(v['properties'].get('due_at'), str) else None  # noqa: E731
        if due_before is not None:
            rows = [v for v in rows if due(v) is not None and _instant(due(v)) < due_before]
        if due_after is not None:
            rows = [v for v in rows if due(v) is not None and _instant(due(v)) >= due_after]
        scope = {'commitment:' + v['commitment_id'] for v in rows}
        exceptions = [e for e in value['exceptions'] if e['subject_ref'] in scope
                      or (group != 'ended' and not e['subject_ref'].startswith('commitment:') and late is None and due_before is None and due_after is None)]
        counts.update(exceptions_in_scope=len(exceptions), exceptions_all=len(value['exceptions']))
        if sort == 'due_at':
            rows.sort(key=lambda v: (due(v) is None, _instant(due(v)) if due(v) else datetime.max.replace(tzinfo=UTC), v['commitment_id']), reverse=order == 'desc')
        elif sort == 'status':
            rows.sort(key=lambda v: (str(v['properties'].get('status')), v['commitment_id']), reverse=order == 'desc')
        else:
            # registered_at (the response's own field; preparation and registration need not happen in the same order), the id as a
            # stable tie-breaker so page boundaries never move; a commitment not yet registered sorts last either way.
            registered = [v for v in rows if isinstance(v.get('registered_at'), str)]
            pending = sorted((v for v in rows if not isinstance(v.get('registered_at'), str)), key=lambda v: v['commitment_id'])
            registered.sort(key=lambda v: (_instant(v['registered_at']), v['commitment_id']), reverse=order == 'desc')
            rows = registered + pending
        start = int(cursor or 0)
        page = rows[start:start + limit]
        names = {}
        for view in page:
            consumer = view.get('consumer_id')
            if consumer not in names:
                names[consumer] = self.display_state(consumer)
        page = [{**self._commitment(v), 'consumer_display': names[v.get('consumer_id')]} for v in page]
        return {'commitments': page, 'exceptions': exceptions, 'counts': counts, 'total': len(rows), 'status_group': group,
                'next_cursor': str(start + limit) if start + limit < len(rows) else None}

    def commitment(self, *, commitment_id):
        value = self.reader.call('commitment', commitment_id=commitment_id)
        if value is None:
            return None
        return {**self._commitment(value), 'consumer_display': self.display_state(value.get('consumer_id'))}

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

    def erasure_precheck(self, consumer_id):
        # 0220: the owner's read-only erasure precheck (persona, erasure state, what is still held; counts only).
        return self.reader.observe('erasure_precheck', consumer_id=consumer_id)

    def operator_personas(self, consumer_id=None):
        # 0227: the owner's persona overview (versions and statuses, current rules, AI disclosure; one Consumer's assignment).
        return self.reader.observe('operator_personas', **({'consumer_id': consumer_id} if consumer_id else {}))

    def contact_preferences(self, consumer_id):
        # 0228: the owner's view of a consumer's communication preferences (from their Claims; no quote) and dismissals.
        return self.reader.observe('contact_preferences', consumer_id=consumer_id)

    def human_actions(self, *, limit=100):
        return self.reader.observe('human_actions', limit=limit)
