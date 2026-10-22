"""NX-054 P1 (ADR-038, adopted 2026-10-22; docs/design/relation-types-design.md §3): the first batch of formal relation types.

Pure and deterministic, like system_catalog: the definitions, the ownership chains computed from the type graph (acyclic, every
ownership chain ends at Consumer, the longest one declared in the manifest) and the manifest the configurator publishes through
control.nexloop_publish_relation_types (0319), which checks all of it again. P1 only defines: nothing derives, backfills or reads
relations yet (P2 derives them at commit from `source_property`).

Every first-batch type is many_to_one (one source object names one target through one property). Not in the first batch
(owner 2026-10-22, as recommended): RelationshipAssessment source_id / target_id (their endpoint types depend on the assessed
relation), the authorization id arrays of RoleExecutionCeiling / RoleAssignmentScope, and RoleDefinition.ceiling_ref and
Commitment.fulfillment_basis (their values are to be verified on stage first).
"""
import hashlib
import json
from dataclasses import dataclass

from eios.ontology.models import RelationCardinality, RelationTypeDefinition

MANIFEST_SCHEMA = 'nexloop-relation-types/1'
CATALOG_VERSION = 1
MAX_CHAIN = 8  # the SQL bound (control.nexloop_relation_catalogs)


@dataclass(frozen=True)
class RelationSpec:
    name: str
    source_type: str
    target_type: str
    source_property: str
    ownership: str | None  # 'target' | 'transitive' | None
    inverse_name: str = ''


# (name, source type, target type, source property, Consumer ownership, inverse name)
FIRST_BATCH = (
    # Ownership: which Consumer an object belongs to (ADR-038 §2.3).
    RelationSpec('goal_of', 'Goal', 'Consumer', 'consumer_id', 'target', 'goals'),
    RelationSpec('plan_step_of', 'PlanStep', 'Consumer', 'consumer_id', 'target', 'plan_steps'),
    RelationSpec('control_of', 'EffectControl', 'Consumer', 'consumer_id', 'target', 'effect_controls'),
    RelationSpec('conversation_of', 'Conversation', 'Consumer', 'consumer_id', 'target', 'conversations'),
    RelationSpec('message_in', 'Message', 'Conversation', 'conversation_id', 'transitive', 'messages'),
    RelationSpec('commitment_to', 'Commitment', 'Consumer', 'made_to', 'target', 'commitments'),
    RelationSpec('commercial_record_of', 'CommercialRecord', 'Consumer', 'consumer_ref', 'target', 'commercial_records'),
    RelationSpec('assignment_of', 'MessageAssignment', 'Consumer', 'consumer_id', 'target', 'message_assignments'),
    RelationSpec('owned_by', 'ConsumerOwnership', 'Consumer', 'consumer_id', 'target', 'ownerships'),
    RelationSpec('role_link_of', 'ConsumerRoleLink', 'Consumer', 'consumer_id', 'target', 'role_links'),
    RelationSpec('service_offering_of', 'ConsumerServiceOffering', 'Consumer', 'consumer_id', 'target', 'service_offerings'),
    RelationSpec('incentive_offering_of', 'ConsumerIncentiveOffering', 'Consumer', 'consumer_id', 'target', 'incentive_offerings'),
    RelationSpec('scope_for_consumer', 'RoleAssignmentScope', 'Consumer', 'consumer_id', 'target', 'role_scopes'),
    RelationSpec('assessment_evidence', 'RelationshipAssessment', 'Message', 'evidence_message_id', 'transitive', 'assessments'),
    # Other links.
    RelationSpec('step_in_goal', 'PlanStep', 'Goal', 'goal_id', None, 'steps'),
    RelationSpec('step_under_control', 'PlanStep', 'EffectControl', 'control_id', None, 'steps'),
    RelationSpec('commitment_for_goal', 'Commitment', 'Goal', 'related_goal_ref', None, 'commitments'),
    RelationSpec('supersedes', 'Commitment', 'Commitment', 'supersedes', None, 'superseded_by'),
    RelationSpec('assignment_for_message', 'MessageAssignment', 'Message', 'message_id', None, 'assignments'),
    RelationSpec('assignment_goal', 'MessageAssignment', 'Goal', 'goal_id', None, 'assignments'),
    RelationSpec('assignment_step', 'MessageAssignment', 'PlanStep', 'step_id', None, 'assignments'),
    RelationSpec('assignment_control', 'MessageAssignment', 'EffectControl', 'control_id', None, 'assignments'),
    RelationSpec('role_link_role', 'ConsumerRoleLink', 'RoleDefinition', 'role_id', None, 'consumer_links'),
    RelationSpec('service_offering_ref', 'ConsumerServiceOffering', 'ServiceOffering', 'offering_id', None, 'consumer_offerings'),
    RelationSpec('incentive_offering_ref', 'ConsumerIncentiveOffering', 'IncentiveOffering', 'offering_id', None, 'consumer_offerings'),
    RelationSpec('scope_of_role', 'RoleAssignmentScope', 'RoleDefinition', 'role_id', None, 'scopes'),
)


class RelationCatalogInvalid(ValueError):
    pass


def definition(spec):
    return RelationTypeDefinition(relation_name=spec.name, source_type=spec.source_type, target_type=spec.target_type,
                                  cardinality=RelationCardinality.MANY_TO_ONE, inverse_name=spec.inverse_name,
                                  description=f'{spec.source_type}.{spec.source_property} -> {spec.target_type}')


def ownership_chains(specs=FIRST_BATCH):
    """{ownership relation name: hops to Consumer}. Refuses two ownership relations from one type, a chain that does not reach
    Consumer, and a cycle (the same checks 0319 makes at publication)."""
    owners = {}
    for s in specs:
        if s.ownership is None:
            continue
        if s.ownership not in ('target', 'transitive') or (s.ownership == 'target') != (s.target_type == 'Consumer'):
            raise RelationCatalogInvalid(f'{s.name}: ownership {s.ownership} with target {s.target_type}')
        if s.source_type in owners:
            raise RelationCatalogInvalid(f'two ownership relations from {s.source_type}')
        owners[s.source_type] = s
    chains = {}
    for s in owners.values():
        hops, cur, seen = 1, s, {s.source_type}
        while cur.ownership == 'transitive':
            cur = owners.get(cur.target_type)
            if cur is None:
                raise RelationCatalogInvalid(f'ownership chain of {s.name} does not reach Consumer')
            if cur.source_type in seen or hops >= MAX_CHAIN:
                raise RelationCatalogInvalid(f'ownership chain of {s.name} has a cycle')
            seen.add(cur.source_type)
            hops += 1
        chains[s.name] = hops
    return chains


def ownership_path(type_name, specs=FIRST_BATCH):
    """The ownership relations from type_name to Consumer, in order ([] for Consumer itself)."""
    owners = {s.source_type: s for s in specs if s.ownership}
    path, cur = [], type_name
    while cur != 'Consumer':
        s = owners.get(cur)
        if s is None:
            raise RelationCatalogInvalid(f'{type_name} has no ownership chain')
        path.append(s)
        cur = s.target_type
    return path


def manifest(specs=FIRST_BATCH, version=CATALOG_VERSION):
    names = [s.name for s in specs]
    if len(set(names)) != len(names):
        raise RelationCatalogInvalid('relation type declared twice')
    chains = ownership_chains(specs)
    return {'schema': MANIFEST_SCHEMA, 'version': version, 'longest_ownership_chain': max(chains.values()),
            'relation_types': [{'definition': definition(s).model_dump(mode='json'), 'source_property': s.source_property,
                                'consumer_ownership': s.ownership} for s in specs]}


def manifest_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def publish(db, tenant, value=None):
    """As nexloop_configurator (0319 refuses any other session)."""
    value = value or manifest()
    return db.execute('select control.nexloop_publish_relation_types(%s,%s::jsonb)', (tenant, json.dumps(value))).fetchone()[0]


def main(argv=None):
    """Publish the first batch for one tenant as the technical configurator (stage: the configurator login window)."""
    import argparse

    from nexloop_eios.trusted_configuration import configurator_connection
    p = argparse.ArgumentParser(description='NX-054 P1: publish the first batch of relation types (trusted configuration)')
    p.add_argument('--tenant', required=True)
    p.add_argument('--database-url-file', required=True)
    p.add_argument('--dry-run', action='store_true', help='print the manifest summary; connect to nothing')
    a = p.parse_args(argv)
    value = manifest()
    if a.dry_run:
        print(json.dumps({'relation_types': len(value['relation_types']), 'longest_ownership_chain': value['longest_ownership_chain'],
                          'manifest_digest': manifest_digest(value)}, sort_keys=True))
        return 0
    db = configurator_connection(a.database_url_file)
    with db, db.transaction():
        db.execute("set local statement_timeout='10000ms'; set local lock_timeout='3000ms'")
        print(json.dumps(publish(db, a.tenant, value), sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
