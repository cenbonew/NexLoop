"""NX-028 slice 1 (D1/D2): workbench roles → Human Action grants, through trusted configuration only.

Public: deploy/authorization/workbench-roles.v<N>.json (role → Action resources). Private (never committed): the member
file {schema_version: nexloop-workbench-members/1, tenant_id, application_id, caller_application_id, members:[{principal_id,
subject_id, role}]}. Members are existing browser Humans the identity operator created; nothing here creates a Human.

Order (0140 enforces it): apply the members first (control.nexloop_configure_workbench, technical configurator), then the
compiled grant facts through the 0050 trusted-configuration manifest (``compile_members`` → ``authority_facts``). To shrink
a role or remove a member, revoke the grants first (empty grant sets), then apply the smaller member list.
"""
import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from eios.authz import facts as F
from eios.authz.applications import ApplicationMode, OperationRestriction, ResourceRestriction
from eios.authz.grants import GrantSubjectKind
from eios.authz.operations import Operation
from eios.authz.policy.canonical import canonicalize_policy_expression
from eios.authz.policy.models import ActionParameterRef, Eq, LiteralValue
from eios.authz.policy.registry import PolicyEffect
from eios.authz.resources import ResourceReference, ResourceType
from eios.identity.models import SubjectKind

from nexloop_eios.authorization import WITNESS
from nexloop_eios.postgres_artifacts import canonical_payload

ROLES_SCHEMA = 'nexloop-workbench-roles/1'
MEMBERS_SCHEMA = 'nexloop-workbench-members/1'
ROLE_NAMES = ('operator', 'owner', 'reviewer')
_ACTION = re.compile(r'eios:action:[A-Za-z][A-Za-z0-9._-]{0,159}:[1-9][0-9]{0,5}')
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,159}')


class WorkbenchRolesRejected(ValueError):
    pass


def validate_roles(value):
    if (type(value) is not dict or value.get('schema_version') != ROLES_SCHEMA or type(value.get('manifest_version')) is not int
            or value['manifest_version'] < 1 or type(value.get('roles')) is not dict or tuple(sorted(value['roles'])) != ROLE_NAMES):
        raise WorkbenchRolesRejected('roles manifest')
    for role, actions in value['roles'].items():
        if (type(actions) is not list or not 1 <= len(actions) <= 64 or len(set(actions)) != len(actions)
                or any(type(a) is not str or not _ACTION.fullmatch(a) for a in actions)):
            raise WorkbenchRolesRejected('role ' + role)
    # D2 / ADR-023 §2.4: owner-only Actions never appear in another role.
    owner_only = {'eios:action:Contact.release:1', 'eios:action:Commitment.cancel:1', 'eios:action:Commitment.mark_communication:1',
                  'eios:action:Goal.publish:1', 'eios:action:Metric.approve:1', 'eios:action:Budget.set:1', 'eios:action:Control.set:1'}
    if owner_only & (set(value['roles']['operator']) | set(value['roles']['reviewer'])):
        raise WorkbenchRolesRejected('owner-only Action in another role')
    return value


def validate_members(value, roles):
    if (type(value) is not dict or set(value) != {'schema_version', 'tenant_id', 'application_id', 'caller_application_id', 'members'}
            or value['schema_version'] != MEMBERS_SCHEMA or type(value['members']) is not list or len(value['members']) > 1000
            or any(type(value[k]) is not str or not _NAME.fullmatch(value[k]) for k in ('tenant_id', 'application_id'))
            or type(value['caller_application_id']) is not str or not value['caller_application_id'].startswith('eios:application:')):
        raise WorkbenchRolesRejected('members file')
    seen = set()
    for m in value['members']:
        if (type(m) is not dict or set(m) != {'principal_id', 'subject_id', 'role'} or m['role'] not in roles['roles']
                or any(type(m[k]) is not str or not _NAME.fullmatch(m[k]) for k in ('principal_id', 'subject_id')) or m['principal_id'] in seen):
            raise WorkbenchRolesRejected('member')
        seen.add(m['principal_id'])
    return value


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _digest(value):
    return F.canonical_authority_digest(value)


def application_facts(roles, tenant, caller_application_id, *, valid_from):
    """The workbench caller application: restricted to every Action some role carries (execute only)."""
    actions = sorted({a for items in roles['roles'].values() for a in items})
    version_digest = _digest({'schema': ROLES_SCHEMA, 'application': caller_application_id, 'version': str(roles['manifest_version']), 'actions': actions})
    return ('application', [caller_application_id, '1'], F.ApplicationFacts(tenant_id=tenant, repository_witness=WITNESS,
        application_id=caller_application_id, application_revision=1, version='1', version_revision=roles['manifest_version'],
        version_digest=version_digest, record_digest=version_digest, application_status='active', version_status='published', eligible=True,
        mode=ApplicationMode.RESTRICTED, break_glass=False,
        resources=tuple(ResourceRestriction(tenant_id=tenant, resource_type='action', resource_id=a) for a in actions),
        operations=(OperationRestriction(operation=Operation.EXECUTE),), published_at=valid_from, revoked_at=None, expires_at=None))


def compile_member(roles, tenant, member, *, valid_from, world='real'):
    """Deterministic Human authority facts for one member's role (no subject/membership/authentication: the browser
    directory owns those; configuration may only grant an existing Human, 0050)."""
    pid, subject, role = member['principal_id'], member['subject_id'], member['role']
    base = dict(tenant_id=tenant, repository_witness=WITNESS)
    role_id = 'nexloop-workbench-role:' + role
    role_digest = _digest({'role': role_id, 'version': roles['manifest_version']})
    facts = [
        ('actor', [pid], F.ActorFacts(**base, actor_id=subject, actor_principal_id=pid, kind=SubjectKind.HUMAN, status='active', revision=1)),
        ('subject_authority', [pid], F.SubjectAuthorityFacts(**base, subject_kind=GrantSubjectKind.HUMAN, principal_id=pid, groups=(),
            roles=(F.RevisionedAuthority(authority_id=role_id, revision=1, digest=role_digest),), attribute_revisions=(), clearances=(), revision=1, valid_until=None)),
    ]
    clearance = F.SubjectClearanceFact(clearance_id='workbench-clearance', subject_kind=GrantSubjectKind.HUMAN, principal_id=pid,
        kind=F.ControlKind.ORGANIZATION, namespace='nexloop', value='internal', rank=0, dominates=frozenset(), revision=1,
        digest=_digest({'clearance': 'internal'}), valid_until=None)
    condition = Eq(op='eq', node_id='world-boundary', left=ActionParameterRef(kind='action_parameter', parameter_name='world'),
        right=LiteralValue(kind='literal', value=world))
    canonical = canonicalize_policy_expression(condition)
    scopes = frozenset({'action.execute'})
    for rid in sorted(roles['roles'][role]):
        ref = ResourceReference(tenant_id=tenant, resource_id=rid, resource_type=ResourceType.ACTION, security_revision=1)
        op = Operation.EXECUTE
        facts.append(('resource_graph', [rid], F.ResourceGraphFacts(**base, root=F.ResourceNodeFact(resource=ref, parent=None, active=True), ancestors=(),
            dependencies=(), registry_revision=1, registry_digest=_digest(ref), closure_complete=True, next_cursor=None)))
        grant = F.EffectiveGrantFact(grant_id=f'{role}:{rid}', subject_kind=GrantSubjectKind.HUMAN, subject_id=pid, role_id=role_id, role_revision=1,
            role_digest=role_digest, resource=ref, operations=frozenset({op}), inherited=False, valid_from=valid_from, valid_until=None, revision=1,
            digest=_digest({'grant': rid, 'principal': pid, 'operations': ['execute']}))
        facts.append(('grants', [pid, rid], F.GrantFacts(**base, subject_kind=GrantSubjectKind.HUMAN, principal_id=pid, grants=(grant,), valid_until=None,
            complete=True, next_cursor=None, revision=1)))
        facts.append(('scope', [pid, rid, op.value], F.ScopeAuthorityFacts(**base, principal_id=pid, binding_id='workbench-route', catalog_id='nexloop-scopes',
            catalog_version='1', catalog_revision=1, catalog_digest=_digest({'scope': 'action.execute'}), catalog_scopes=scopes,
            required_scopes=frozenset({'action.execute'}), authorities=(F.ScopeAuthority(scope='action.execute', resource_type=ResourceType.ACTION, operation=op, resource_id=rid),),
            risk=F.AuthorizationRisk.SENSITIVE, authorized_scopes=scopes, revision=1, valid_until=None)))
        requirement = F.ResourceControlRequirementFact(requirement_id='internal-resource', resource=ref, operation=op, kind=F.ControlKind.ORGANIZATION,
            namespace='nexloop', value='internal', minimum_rank=0, dominance=F.ControlDominance.EXACT, revision=1, digest=_digest({'requirement': 'internal'}), valid_until=None)
        facts.append(('controls', [pid, rid, op.value], F.ControlFacts(**base, subject_clearances=(clearance,), resource_requirements=(requirement,), complete=True,
            valid_until=None, revision=1)))
        rule = F.PolicyRuleFact(binding_id='world-policy', binding_digest=_digest({'world': world}), resource=ref, operation=op, policy_set_id='workbench-world',
            active_version=1, version_digest=canonical.sha256, canonical_ast_utf8=canonical.utf8, canonical_ast_sha256=canonical.sha256, effect=PolicyEffect.REQUIRE,
            condition=condition, activation_head_revision=1, activation_head_digest=canonical.sha256, registry_revision=1, registry_digest=canonical.sha256,
            activation_witness=canonical.sha256, published_at=valid_from)
        facts.append(('policies', [pid, rid, op.value], F.PolicyFacts(**base, rules=(rule,), complete=True, valid_until=None, revision=1)))
    return facts


def revoke_member(roles, tenant, principal_id, actions=None):
    """Empty grant sets for a member's workbench Actions (revocation is a write, never a delete)."""
    actions = sorted(actions if actions is not None else {a for items in roles['roles'].values() for a in items})
    return [('grants', [principal_id, rid], F.GrantFacts(tenant_id=tenant, repository_witness=WITNESS, subject_kind=GrantSubjectKind.HUMAN,
        principal_id=principal_id, grants=(), valid_until=None, complete=True, next_cursor=None, revision=1)) for rid in actions]


def compile_members(roles, members, *, valid_from=None):
    """Authority facts for the trusted-configuration manifest (the 0050 ``authority_facts`` list)."""
    validate_roles(roles)
    validate_members(members, roles)
    valid_from = valid_from or datetime(2026, 10, 10, tzinfo=UTC)
    tenant = members['tenant_id']
    out = {}
    for kind, key, fact in [application_facts(roles, tenant, members['caller_application_id'], valid_from=valid_from),
                            *[f for m in members['members'] for f in compile_member(roles, tenant, m, valid_from=valid_from)]]:
        out[(kind, tuple(key))] = {'kind': kind, 'key': key, 'payload': fact.model_dump(mode='json')}
    return [out[k] for k in sorted(out)]


def configuration_payloads(roles, members):
    """The two arguments of control.nexloop_configure_workbench (subject ids stay out of the stored member map)."""
    validate_roles(roles)
    validate_members(members, roles)
    return roles, {'schema_version': MEMBERS_SCHEMA, 'tenant_id': members['tenant_id'], 'application_id': members['application_id'],
                   'members': [{'principal_id': m['principal_id'], 'role': m['role']} for m in members['members']]}


def apply_members(roles, members, *, database_url_file):
    """Technical configurator only (0140 refuses any other session)."""
    from nexloop_eios.trusted_configuration import configurator_connection
    roles_payload, members_payload = configuration_payloads(roles, members)
    db = configurator_connection(database_url_file)
    with db, db.transaction():
        db.execute("set local statement_timeout='10000ms'; set local lock_timeout='3000ms'")
        return db.execute('select control.nexloop_configure_workbench(%s,%s::jsonb,%s::jsonb)',
            (members['tenant_id'], canonical_payload(roles_payload), canonical_payload(members_payload))).fetchone()[0]


def main(argv=None):
    p = argparse.ArgumentParser(description='Workbench roles → Human Action grants (NX-028 D1/D2), trusted configuration only')
    p.add_argument('--roles', type=Path, required=True)
    p.add_argument('--members', type=Path)
    p.add_argument('--database-url-file', type=Path)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--compile-facts', action='store_true', help='print the authority_facts for the 0050 manifest')
    mode.add_argument('--apply-members', action='store_true')
    a = p.parse_args(argv)
    try:
        roles = validate_roles(load(a.roles))
        if a.check:
            if a.members is not None:
                validate_members(load(a.members), roles)
            print(json.dumps({'valid': True, 'manifest_version': roles['manifest_version']}))
            return 0
        if a.members is None:
            raise WorkbenchRolesRejected('members file required')
        members = load(a.members)
        if a.compile_facts:
            print(json.dumps(compile_members(roles, members), ensure_ascii=False, sort_keys=True))
            return 0
        if a.database_url_file is None:
            raise WorkbenchRolesRejected('database url file required')
        print(json.dumps(apply_members(roles, members, database_url_file=a.database_url_file)))
        return 0
    except Exception as error:
        print(json.dumps({'valid': False, 'reason': str(error) if isinstance(error, WorkbenchRolesRejected) else 'rejected'}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
