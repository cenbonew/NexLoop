"""NX-028 slice 1 (D1/D2): workbench roles → Human Action grants, through trusted configuration only.

Public: deploy/authorization/workbench-roles.v<N>.json (role → Action resources). Private (never committed): the member
file {schema_version: nexloop-workbench-members/1, tenant_id, application_id, caller_application_id, members:[{principal_id,
subject_id, role}]}. Members are existing browser Humans the identity operator created; nothing here creates a Human.

Order (0140 enforces it): apply the members first (control.nexloop_configure_workbench, technical configurator), then the
compiled grant facts through the 0050 trusted-configuration manifest (``compile_members`` → ``authority_facts``). To shrink
a role or remove a member, revoke the grants first (empty grant sets), then apply the smaller member list.

Removing a member from the command line (G-WB-REVOKE): `--apply --revoke-member PRINCIPAL_ID` (repeatable) with a members file that no
longer lists it runs exactly that order (apply_revocations): the empty grant sets first, then the smaller member list.
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
                  'eios:action:Goal.publish:1', 'eios:action:Metric.approve:1', 'eios:action:Budget.set:1', 'eios:action:Control.set:1',
                  'eios:action:nexloop.alert.silence:1',  # NX-030: alert silencing is the owner's
                  'eios:action:nexloop.restore.open_scope:1',  # NX-035: opening a restored database is the owner's
                  'eios:action:nexloop.incentive.issue:1',  # AT-043: it spends the tenant incentive budget
                  'eios:action:nexloop.real_dispatch.set:1',  # 0153: opening real dispatch is the owner's
                  'eios:action:nexloop.contact_key.revoke:1',  # NX-053: re-allowing contact with an erased identity is the owner's
                  'eios:action:nexloop.effect.confirm_not_delivered:1',  # 0210: closing an unknown effect as not delivered is the owner's
                  'eios:action:nexloop.consumer.set_persona:1',  # 0220: who counts as a test persona is the owner's (ADR-034)
                  'eios:action:nexloop.consumer.erase_synthetic:1',  # 0220: erasing a synthetic Consumer is the owner's
                  'eios:action:nexloop.operator_persona.publish:1',  # 0227: how the operator speaks is the owner's
                  'eios:action:nexloop.operator_persona.assign:1',
                  'eios:action:nexloop.operator_persona.rules_publish:1',
                  'eios:action:nexloop.ai_disclosure.set:1',  # 0227: AI disclosure is the tenant owner's choice
                  'eios:action:nexloop.contact_preference.dismiss:1',  # 0228: declining a consumer's own preference is the owner's
                  'eios:action:nexloop.outreach_controls.set:1',  # 0232: when and how often the tenant reaches out is the owner's
                  'eios:action:nexloop.outreach_purposes.set:1',  # outreach slice 3: which purposes the tenant may use is the owner's
                  'eios:action:nexloop.outreach.send:1',  # outreach slice 4: a proactive message reaches a customer
                  'eios:action:nexloop.marketing_consent.record:1',  # outreach slice 7: whether a customer receives marketing is the owner's record
                  'eios:action:nexloop.context.strategy.publish:1',  # how Context is assembled is the owner's
                  'eios:action:nexloop.knowledge.read:1'}  # NX-028 D2 revision (2026-10-18): the owner reads the knowledge workbench
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
    role = member['role']
    return human_facts(tenant, member['principal_id'], member['subject_id'], 'nexloop-workbench-role:' + role, roles['manifest_version'],
                       [(rid, ResourceType.ACTION) for rid in sorted(roles['roles'][role])], grant_prefix=role, binding_id='workbench-route',
                       clearance_id='workbench-clearance', policy_set_id='workbench-world', valid_from=valid_from, world=world)


def human_facts(tenant, pid, subject, role_id, role_version, resources, *, grant_prefix, binding_id, clearance_id, policy_set_id, valid_from,
                world='real', catalog_scopes=frozenset({'action.execute'})):
    """The authority facts that let one existing browser Human execute `resources` [(resource id, ResourceType.ACTION or
    FUNCTION)] in `world`: actor, subject authority (one role), and per resource the graph node, grant, scope, controls and world
    policy. Shared by the workbench roles and the webchat consumer role (webchat_consumers)."""
    base = dict(tenant_id=tenant, repository_witness=WITNESS)
    role_digest = _digest({'role': role_id, 'version': role_version})
    facts = [
        ('actor', [pid], F.ActorFacts(**base, actor_id=subject, actor_principal_id=pid, kind=SubjectKind.HUMAN, status='active', revision=1)),
        ('subject_authority', [pid], F.SubjectAuthorityFacts(**base, subject_kind=GrantSubjectKind.HUMAN, principal_id=pid, groups=(),
            roles=(F.RevisionedAuthority(authority_id=role_id, revision=1, digest=role_digest),), attribute_revisions=(), clearances=(), revision=1, valid_until=None)),
    ]
    clearance = F.SubjectClearanceFact(clearance_id=clearance_id, subject_kind=GrantSubjectKind.HUMAN, principal_id=pid,
        kind=F.ControlKind.ORGANIZATION, namespace='nexloop', value='internal', rank=0, dominates=frozenset(), revision=1,
        digest=_digest({'clearance': 'internal'}), valid_until=None)
    condition = Eq(op='eq', node_id='world-boundary', left=ActionParameterRef(kind='action_parameter', parameter_name='world'),
        right=LiteralValue(kind='literal', value=world))
    canonical = canonicalize_policy_expression(condition)
    scopes = frozenset(catalog_scopes)
    for rid, kind in resources:
        ref = ResourceReference(tenant_id=tenant, resource_id=rid, resource_type=kind, security_revision=1)
        op = Operation.EXECUTE
        scope = f'{kind.value}.{op.value}'
        facts.append(('resource_graph', [rid], F.ResourceGraphFacts(**base, root=F.ResourceNodeFact(resource=ref, parent=None, active=True), ancestors=(),
            dependencies=(), registry_revision=1, registry_digest=_digest(ref), closure_complete=True, next_cursor=None)))
        grant = F.EffectiveGrantFact(grant_id=f'{grant_prefix}:{rid}', subject_kind=GrantSubjectKind.HUMAN, subject_id=pid, role_id=role_id, role_revision=1,
            role_digest=role_digest, resource=ref, operations=frozenset({op}), inherited=False, valid_from=valid_from, valid_until=None, revision=1,
            digest=_digest({'grant': rid, 'principal': pid, 'operations': ['execute']}))
        facts.append(('grants', [pid, rid], F.GrantFacts(**base, subject_kind=GrantSubjectKind.HUMAN, principal_id=pid, grants=(grant,), valid_until=None,
            complete=True, next_cursor=None, revision=1)))
        facts.append(('scope', [pid, rid, op.value], F.ScopeAuthorityFacts(**base, principal_id=pid, binding_id=binding_id, catalog_id='nexloop-scopes',
            catalog_version='1', catalog_revision=1, catalog_digest=_digest({'scope': scope}), catalog_scopes=scopes,
            required_scopes=frozenset({scope}), authorities=(F.ScopeAuthority(scope=scope, resource_type=kind, operation=op, resource_id=rid),),
            risk=F.AuthorizationRisk.SENSITIVE, authorized_scopes=scopes, revision=1, valid_until=None)))
        requirement = F.ResourceControlRequirementFact(requirement_id='internal-resource', resource=ref, operation=op, kind=F.ControlKind.ORGANIZATION,
            namespace='nexloop', value='internal', minimum_rank=0, dominance=F.ControlDominance.EXACT, revision=1, digest=_digest({'requirement': 'internal'}), valid_until=None)
        facts.append(('controls', [pid, rid, op.value], F.ControlFacts(**base, subject_clearances=(clearance,), resource_requirements=(requirement,), complete=True,
            valid_until=None, revision=1)))
        rule = F.PolicyRuleFact(binding_id='world-policy', binding_digest=_digest({'world': world}), resource=ref, operation=op, policy_set_id=policy_set_id,
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


def publish_human_authority(tenant, *, authority_facts, business_application, humans, label, database_url_file, signing_key_file, signing_key_id,
                            refused=WorkbenchRolesRejected):
    """One 0050 manifest (technical configurator) with Human authority facts and the browser application's 0045 business row
    (caller application, version, requested scopes). Every Human must be an existing browser Human (0050 refuses the grant otherwise);
    the tenant must exist. Shared by the workbench roles and the webchat consumer role."""
    import uuid
    from nexloop_eios import service_grants as G
    from nexloop_eios.trusted_configuration import apply_manifest_with_secrets
    current = G.inventory(database_url_file, tenant)
    if not current['tenant_present']:
        raise refused('tenant not configured yet')
    known = set(current.get('human_identifiers') or [])
    missing = sorted(h for h in humans if h not in known)
    if missing:
        raise refused('not an existing browser Human: ' + ', '.join(missing))
    body = {'schema_version': '1.0', 'tenant_id': tenant, 'expected_revision': current['authority_revision'], 'tenant_status': current['tenant_status'],
            'object_types': [], 'actions': [], 'functions': [], 'service_credentials': [], 'browser_applications': [], 'browser_rate_policies': [],
            'identity_allowances': [], 'authority_facts': authority_facts, 'browser_business_applications': [business_application]}
    body['manifest_id'] = str(uuid.uuid5(G.NAMESPACE, tenant + ':' + label + ':' + str(current['authority_revision']) + ':' + canonical_payload(body)))
    result = apply_manifest_with_secrets(body, database_url_file=database_url_file, signing_key_file=signing_key_file, signing_key_id=signing_key_id,
                                         service_secrets={})
    return result['authority_revision']


def apply(roles, members, *, database_url_file, signing_key_file, signing_key_id):
    """G-WORKBENCH-AUTH: the whole workbench configuration in one run, in the order the SQL requires: the workbench caller application
    fact and its 0045 business row (0050; control.nexloop_configure_workbench refuses an application that is not a published browser
    business application), then the members (configure_workbench), then the members' grant facts (0050, 0140: grants follow the
    member list). Same input, same state."""
    validate_roles(roles)
    validate_members(members, roles)
    facts = compile_members(roles, members)
    application = [f for f in facts if f['kind'] == 'application']
    common = dict(business_application={'application_id': members['application_id'], 'caller_application_id': members['caller_application_id'],
                                        'application_version': '1', 'requested_scopes': ['action.execute']},
                  database_url_file=database_url_file, signing_key_file=signing_key_file, signing_key_id=signing_key_id)
    publish_human_authority(members['tenant_id'], authority_facts=application, humans=[], label='workbench-application', **common)
    configured = apply_members(roles, members, database_url_file=database_url_file)
    revision = publish_human_authority(members['tenant_id'], authority_facts=facts, humans=[m['principal_id'] for m in members['members']],
                                       label='workbench-roles', **common)
    return {'configured': True, 'workbench': configured, 'authority_revision': revision, 'members': len(members['members']),
            'roles_manifest_version': roles['manifest_version']}


def apply_revocations(roles, members, revoked, *, database_url_file, signing_key_file, signing_key_id):
    """G-WB-REVOKE: remove members in the order this module requires. First one 0050 manifest with empty grant sets for every
    workbench Action of each revoked principal (revoke_member; the principal is still a member, so 0140 accepts the facts), then
    `apply` with the remaining member list (members, the remaining members' grant facts). The member file must no longer list a
    revoked principal. Same input, same state: a rerun writes the same empty sets and the same member list."""
    validate_roles(roles)
    validate_members(members, roles)
    if not revoked or len(set(revoked)) != len(revoked) or any(type(r) is not str or not _NAME.fullmatch(r) for r in revoked):
        raise WorkbenchRolesRejected('revoke-member: distinct principal ids')
    still = sorted(set(revoked) & {m['principal_id'] for m in members['members']})
    if still:
        raise WorkbenchRolesRejected('the members file still lists a revoked member: ' + ', '.join(still))
    tenant = members['tenant_id']
    facts = [{'kind': kind, 'key': key, 'payload': fact.model_dump(mode='json')} for r in sorted(revoked) for kind, key, fact in revoke_member(roles, tenant, r)]
    business = {'application_id': members['application_id'], 'caller_application_id': members['caller_application_id'], 'application_version': '1',
                'requested_scopes': ['action.execute']}
    revision = publish_human_authority(tenant, authority_facts=facts, business_application=business, humans=sorted(revoked), label='workbench-revoke',
                                       database_url_file=database_url_file, signing_key_file=signing_key_file, signing_key_id=signing_key_id)
    applied = apply(roles, members, database_url_file=database_url_file, signing_key_file=signing_key_file, signing_key_id=signing_key_id)
    return {**applied, 'revoked': sorted(revoked), 'revoked_grant_sets': len(facts), 'revocation_revision': revision}


def main(argv=None, *, window=None):
    p = argparse.ArgumentParser(description='Workbench roles → Human Action grants (NX-028 D1/D2), trusted configuration only')
    p.add_argument('--roles', type=Path, required=True)
    p.add_argument('--members', type=Path)
    p.add_argument('--database-url-file', type=Path)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--compile-facts', action='store_true', help='print the authority_facts for the 0050 manifest')
    mode.add_argument('--apply-members', action='store_true')
    mode.add_argument('--apply', action='store_true', help='members, grant facts and the business application row (G-WORKBENCH-AUTH)')
    p.add_argument('--configurator-dsn-file', type=Path)
    p.add_argument('--bootstrap-dsn-file', type=Path)
    p.add_argument('--signing-key-file', type=Path)
    p.add_argument('--signing-key-id')
    p.add_argument('--revoke-member', action='append', metavar='PRINCIPAL_ID',
                   help='with --apply: empty grant sets for this member first, then the members file (which must not list it); repeatable')
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
        if a.revoke_member and not a.apply:
            raise WorkbenchRolesRejected('--revoke-member needs --apply')
        if a.compile_facts:
            print(json.dumps(compile_members(roles, members), ensure_ascii=False, sort_keys=True))
            return 0
        if a.apply:
            if not (a.signing_key_file and a.signing_key_id) or (a.configurator_dsn_file is None) == (a.bootstrap_dsn_file is None):
                raise WorkbenchRolesRejected('apply needs one of --configurator-dsn-file / --bootstrap-dsn-file, --signing-key-file and --signing-key-id')
            def run(dsn):
                if a.revoke_member:
                    return apply_revocations(roles, members, a.revoke_member, database_url_file=dsn, signing_key_file=a.signing_key_file,
                                             signing_key_id=a.signing_key_id)
                return apply(roles, members, database_url_file=dsn, signing_key_file=a.signing_key_file, signing_key_id=a.signing_key_id)
            if a.bootstrap_dsn_file is not None:
                if window is None:
                    raise WorkbenchRolesRejected('--bootstrap-dsn-file: use scripts/deploy/workbench_roles.py')
                with window(a.bootstrap_dsn_file) as dsn:
                    print(json.dumps(run(dsn), sort_keys=True))
            else:
                print(json.dumps(run(a.configurator_dsn_file), sort_keys=True))
            return 0
        if a.database_url_file is None:
            raise WorkbenchRolesRejected('database url file required')
        print(json.dumps(apply_members(roles, members, database_url_file=a.database_url_file)))
        return 0
    except Exception as error:
        print(json.dumps({'valid': False, 'reason': str(error) if isinstance(error, WorkbenchRolesRejected) else type(error).__name__}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
