"""NX-040 F3: the reviewer's Schema publication port (design §4, §5, §6; D22 publication by hand only, divergence 1 decided
2026-10-20: a rollback is the next Schema version, never an SQL migration).

Runs for an actual Human browser session holding eios:action:ontology.schema.review:1 (as the review decision does). For a frozen
proposal (F2) the backend reads the publication basis (the proposal, the latest version of its type, the active Actions bound to
it, the owner-restricted property names), judges the change with the F1 compatibility library and builds the next version and the
successor Actions; the SQL function (authz.nexloop_schema_proposal_publish) re-derives the expected version from the proposal,
re-checks the successors and the permission proof, and publishes atomically - or answers the codes and writes nothing. The next
version is built on the stored JSON of the latest version (plain additions), so it is exactly what SQL re-derives.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid

from eios.ontology.definitions import ActionDefinition
from eios.ontology.models import ObjectTypeDefinition
from eios.ontology.semantics import SchemaCompatibilityError, assert_object_compatible, schema_contract_digest
from nexloop_eios.candidate_merge import ReviewQueueReader
from nexloop_eios.evolution.schema_compat import judge
from nexloop_eios.review_actions import _human

PROTOCOL = 'nexloop-schema-publication-v1'
NAMESPACE = uuid.UUID('6d1f0c40-4e1c-4a40-9f04-0f1d2b3c4d40')
_KEY = re.compile(r'[A-Za-z0-9._~-]{16,160}')


def _h(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def _f1_actions(rows):
    out = []
    for row in rows:
        d = row['definition']
        out.append({'stable_name': d['stable_name'], 'version': d['version'], 'object_types': [{'stable_name': r['stable_name'], 'version': r['version']}
                    for r in d['object_types']], 'input_schema_hash': _h(d.get('input_schema')), 'capability_schema_hash': _h(row['capability']),
                    'risk_level': (d.get('governance') or {}).get('risk_level'), 'effect_categories': sorted((row['capability'] or {}).get('effect_categories') or [])})
    return out


def successors(basis, new_schema: dict) -> list[dict]:
    """The active Actions bound to the old version, each re-bound to the next one (version + 1, Schema reference and digest only)."""
    if not basis['actions']:
        return []
    new = ObjectTypeDefinition.model_validate(new_schema)
    digest = schema_contract_digest(new)
    out = []
    for row in basis['actions']:
        body = json.loads(json.dumps(row['definition']))
        body.pop('contract_digest', None)
        body['version'] = body['version'] + 1
        rebind = lambda refs: [dict(r, version=new.version, schema_digest=digest) if r['stable_name'] == new.type_name else r for r in refs]  # noqa: E731
        body['object_types'] = rebind(body['object_types'])
        body['governance']['change_scope']['object_types'] = rebind(body['governance']['change_scope']['object_types'])
        out.append({'definition': ActionDefinition.model_validate_json(json.dumps(body)).model_dump(mode='json'), 'capability': row['capability']})
    return out


def build(basis: dict, kind: str, property_name: str | None = None, *, batch: bool = False) -> tuple[dict, dict]:
    """(publication {schema, actions}, compatibility report) for publish or hide. batch (D25): more than 10 000 objects move in batches."""
    op, change, name, old = basis['operation'], basis['change'], basis['type_name'], basis['schema']
    parent = {'types': {name: old} if old else {}, 'object_counts': {name: basis.get('object_count') or 0},
              'actions': _f1_actions(basis['actions']), 'restricted_names': basis.get('restricted_names') or []}
    if kind == 'hide':
        new = {**old, 'version': old['version'] + 1,
               'properties': [dict(p, visibility='hidden') if p['property_name'] == property_name else p for p in old['properties']]}
        try:
            assert_object_compatible(ObjectTypeDefinition.model_validate(old), ObjectTypeDefinition.model_validate(new))
            report = {'op': 'hide_property', 'allowed': True, 'change_class': 'minor', 'codes': []}
        except (SchemaCompatibilityError, ValueError):
            report = {'op': 'hide_property', 'allowed': False, 'change_class': 'major', 'codes': ['breaking_change']}
        return {'schema': new, 'actions': successors(basis, new)}, report
    if op == 'add_optional_property':
        np = {'property_name': change['property_name'], 'value_type': change['value_type'], 'required': False, 'nullable': True, 'default_value': None,
              'description': '', 'display_name': '', 'render_hint': 'plain', 'visibility': 'normal', 'type_descriptor': None}
        new = {**old, 'version': old['version'] + 1, 'properties': list(old['properties']) + [np]}
        f1 = {'op': op, 'type_name': name, 'property': {k: np[k] for k in ('property_name', 'value_type', 'required', 'nullable')}}
    elif op == 'add_vocabulary_value':
        props = []
        for p in old['properties']:
            if p['property_name'] == change['property_name'] and (p.get('type_descriptor') or {}).get('enum'):
                p = {**p, 'type_descriptor': {**p['type_descriptor'], 'enum': list(p['type_descriptor']['enum']) + [change['value']]}}
            props.append(p)
        new = {**old, 'version': old['version'] + 1, 'properties': props}
        f1 = {'op': op, 'type_name': name, 'property_name': change['property_name'], 'value': change['value']}
    else:  # add_object_type: version 1, edited only through governed Actions, no key, no Action (published separately)
        new = {'type_name': name, 'version': 1, 'only_edit_via_actions': True, 'primary_key': list(change.get('primary_key') or []),
               'properties': [{'property_name': p['property_name'], 'value_type': p['value_type'], 'required': False, 'nullable': True}
                              for p in change.get('properties') or []]}
        f1 = {'op': op, 'type_name': name, 'definition': new}
    # F1 first (the change against the Parent); successors are built only for an allowed change (a refused one may not even be a
    # valid next version, e.g. a duplicate property).
    probe = judge({**f1, 'successor_actions': [{'stable_name': a['stable_name'], 'replaces': a['version'], **a, 'version': a['version'] + 1,
                                                 'object_types': [dict(t, version=t['version'] + 1) if t['stable_name'] == name else t for t in a['object_types']]}
                                                for a in parent['actions']]}, parent)
    actions = successors(basis, new) if op != 'add_object_type' and probe['allowed'] else []
    if probe['allowed']:
        f1['successor_actions'] = [{'stable_name': a['stable_name'], 'replaces': a['version'] - 1, **a} for a in _f1_actions(actions)]
        verdict = judge(f1, parent)
    else:
        verdict = probe
    report = {k: verdict[k] for k in ('op', 'allowed', 'change_class', 'reason', 'codes')}
    if batch and report['codes'] == ['migration_over_limit']:  # D25: a batched publication is allowed above the limit
        report = {**report, 'allowed': True, 'reason': None, 'codes': []}
        if op != 'add_object_type':
            actions = successors(basis, new)
    publication = {'schema': new, 'actions': actions, **({'batch': True} if batch else {})}
    return publication, report


class SchemaPublicationPort(ReviewQueueReader):
    """The reviewer's governed Schema publication (by hand only, D22)."""

    def __init__(self, pool, session, signer):
        super().__init__(pool, _human(session), signer)

    def basis(self, proposal_id: str) -> dict | None:
        return self._call('nexloop_read_schema_publication_basis', {'proposal_id': proposal_id})

    def dependents(self, proposal_id: str) -> dict | None:
        """A1: the published Definitions that mention the proposal's type (0322), for nexloop_eios.schema_impact."""
        return self._call('nexloop_read_schema_dependents', {'proposal_id': proposal_id})

    def _publish(self, proposal_id, kind, rationale, idempotency_key, property_name=None, batch=False):
        if type(rationale) is not str or not 1 <= len(rationale.strip()) <= 2000:
            raise ValueError('rationale')
        if type(idempotency_key) is not str or not _KEY.fullmatch(idempotency_key):
            raise ValueError('idempotency key')
        basis = self.basis(proposal_id)
        if basis is None:
            raise LookupError('schema proposal unknown')
        publication, report = build(basis, kind, property_name, batch=batch)
        auth = self.session.authentication
        payload = {'publication_id': str(uuid.uuid5(NAMESPACE, f'{auth.tenant_id}:{auth.subject_principal_id}:{idempotency_key}')),
                   'proposal_id': proposal_id, 'kind': kind, 'rationale': rationale.strip(), 'publication': publication, 'compatibility_report': report,
                   **({'property_name': property_name} if property_name else {})}
        return self._call('nexloop_schema_proposal_publish', payload, PROTOCOL)

    def publish(self, proposal_id: str, *, rationale: str, idempotency_key: str, batch: bool = False) -> dict:
        """Uses the candidate's release permit once (NX-040 F4). batch: above 10 000 objects, the first 10 000 move now (D25)."""
        return self._publish(proposal_id, 'publish', rationale, idempotency_key, batch=batch)

    def migrate_batch(self, publication_id: str) -> dict:
        """The next 10 000 objects of a batched publication (D25)."""
        return self._call('nexloop_schema_migrate_batch', {'publication_id': publication_id}, PROTOCOL)

    def hide(self, proposal_id: str, *, rationale: str, idempotency_key: str) -> dict:
        """The rollback of a published optional property: the next Schema version with it hidden (no SQL migration)."""
        basis = self.basis(proposal_id)
        if basis is None:
            raise LookupError('schema proposal unknown')
        return self._publish(proposal_id, 'hide', rationale, idempotency_key, basis['change']['property_name'])
