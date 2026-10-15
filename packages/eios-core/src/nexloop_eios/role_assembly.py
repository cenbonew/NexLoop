"""NX-032 G-ROLE: a consumer's Role through published governed Actions (role assembler service principal).

    python -m nexloop_eios.role_assembly --database-url-file F --signing-key-file F --signing-key-id ID --artifact-root D
        --assembler-credential-file F --recipe-file F

Creates, each step committing on its own under a stable intent (role-assembly-<request_id>-<part>; an identical replay returns the
same objects), for one consumer already set up (business_setup) and enrolled with the relay:
  Goal and PlanStep  the standing plan step of the consumer's Role Runs: control_id = the consumer's EffectControl (0160/0180
                     enrollment requires it), submitter = the Role Source principal (0041), action nexloop.service.request;
  RoleExecutionCeiling  what a Run of the Role may do (exact Action resources, budget, effect units) for this consumer/goal/step;
  RoleDefinition     ceiling_ref = the ceiling;
  RoleAssignmentScope and ConsumerRoleLink  the consumer's assignment to the Role (link scope = the assignment scope).
The role Actions need ontology.roles.manage in the caller credential (0057/0081): the role_assembler principal requests it
(stage-principals.v1.json). Nothing here grants authority or writes a schema; the owner then enrolls the consumer again with
role_id, link_id and step_id (nexloop-relay-enroll), and tenant_config writes the relay run profile from the published Role.

Recipe (schema_version nexloop-role-assembly/1, deploy/examples/nx037/role-assembly.example.json):
  {"schema_version", "request_id": UUID, "consumer_id", "control_id", "source_principal_id", "valid_from", "valid_until": UTC ISO,
   "role": {"name", "responsibility"}, "ceiling": {"action_resources": [...], "budget": {...}, "effect_units": int}}
Prints one JSON object of object ids; credentials are read from private files and never printed.
"""
import argparse
import json
import logging
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from eios.identity.models import SubjectKind
from nexloop_eios.private_configuration import read_private_text

SCHEMA = 'nexloop-role-assembly/1'
STEP_ACTION = 'nexloop.service.request'
_ID = re.compile('[a-f0-9]{64}')
_STAMP = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)')
_PRINCIPAL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,319}')


class RoleAssemblyUnavailable(RuntimeError):
    def __init__(self, detail='role_assembly_unavailable'):
        super().__init__(detail)


def strict_recipe(value):
    """The validated recipe (role and ceiling re-validated by role_policies at creation)."""
    from nexloop_eios.role_policies import _ids, validate_budget
    if type(value) is not dict or set(value) != {'schema_version', 'request_id', 'consumer_id', 'control_id', 'source_principal_id', 'valid_from', 'valid_until',
                                                 'role', 'ceiling'} or value['schema_version'] != SCHEMA:
        raise RoleAssemblyUnavailable('recipe shape')
    if type(value['request_id']) is not str or str(uuid.UUID(value['request_id'])) != value['request_id']:
        raise RoleAssemblyUnavailable('request_id')
    if any(type(value[k]) is not str or not _ID.fullmatch(value[k]) for k in ('consumer_id', 'control_id')):
        raise RoleAssemblyUnavailable('consumer_id/control_id')
    if type(value['source_principal_id']) is not str or not _PRINCIPAL.fullmatch(value['source_principal_id']):
        raise RoleAssemblyUnavailable('source_principal_id')
    if any(type(value[k]) is not str or not _STAMP.fullmatch(value[k]) for k in ('valid_from', 'valid_until')):
        raise RoleAssemblyUnavailable('validity')
    start, end = (datetime.fromisoformat(value[k].replace('Z', '+00:00')) for k in ('valid_from', 'valid_until'))
    if not start < end or end <= datetime.now(UTC):
        raise RoleAssemblyUnavailable('validity')
    role, ceiling = value['role'], value['ceiling']
    if type(role) is not dict or set(role) != {'name', 'responsibility'} or any(type(role[k]) is not str or not 1 <= len(role[k]) <= 512 for k in role):
        raise RoleAssemblyUnavailable('role')
    if type(ceiling) is not dict or set(ceiling) != {'action_resources', 'budget', 'effect_units'}:
        raise RoleAssemblyUnavailable('ceiling')
    try:
        _ids(ceiling['action_resources'], actions=True)
        validate_budget(ceiling['budget'])
    except ValueError:
        raise RoleAssemblyUnavailable('ceiling') from None
    if type(ceiling['effect_units']) is not int or not 1 <= ceiling['effect_units'] <= 1000000:
        raise RoleAssemblyUnavailable('ceiling')
    return value


def assemble_role(service, recipe):
    """Create (or replay) the Role objects with the role assembler's service; returns their ids."""
    from nexloop_eios.role_mapping import RoleMappingPort
    from nexloop_eios.role_policies import RolePolicyPort
    recipe = strict_recipe(recipe)
    session = service()._session
    if session.world != 'real' or session.run_context is not None or session.authentication.subject_kind is not SubjectKind.SERVICE:
        raise RoleAssemblyUnavailable('real service session required')
    prefix = 'role-assembly-' + recipe['request_id']
    consumer, until = recipe['consumer_id'], recipe['valid_until']
    # Fixed by the recipe, so an identical replay sends identical payloads under the same intents.
    valid = dict(valid_from=recipe['valid_from'], valid_until=until)

    def create(name, part, properties):
        created = service().create_object(action_name=name + '.create', action_version=1, intent_id=f'{prefix}-{part}', type_name=name, properties=properties)
        if created.get('type_name') != name or created.get('world') != 'real' or not _ID.fullmatch(created.get('object_id', '')):
            raise RoleAssemblyUnavailable(name)
        return created['object_id']
    goal = create('Goal', 'goal', {'consumer_id': consumer, 'state': 'active', 'valid_until': until})
    step = create('PlanStep', 'step', {'consumer_id': consumer, 'goal_id': goal, 'control_id': recipe['control_id'],
                                       'submitter_principals': [recipe['source_principal_id']], 'action_name': STEP_ACTION, 'state': 'ready'})
    ceiling = RolePolicyPort(service()).create(kind='RoleExecutionCeiling', intent_id=prefix + '-ceiling', properties=dict(
        active=True, action_resources=list(recipe['ceiling']['action_resources']), consumer_ids=[consumer], goal_ids=[goal], step_ids=[step],
        budget=dict(recipe['ceiling']['budget']), effect_units=recipe['ceiling']['effect_units'], **valid))['object_id']
    role = RoleMappingPort(service()).create_role(intent_id=prefix + '-role', name=recipe['role']['name'], responsibility=recipe['role']['responsibility'],
                                                  ceiling_ref=ceiling, **valid)['object_id']
    scope = RolePolicyPort(service()).create(kind='RoleAssignmentScope', intent_id=prefix + '-scope', properties=dict(
        active=True, role_id=role, consumer_id=consumer, goal_ids=[goal], step_ids=[step], **valid))['object_id']
    link = RoleMappingPort(service()).assign(intent_id=prefix + '-link', consumer_id=consumer, role_id=role, scope=scope, **valid)['object_id']
    return {'assembled': True, 'consumer_id': consumer, 'control_id': recipe['control_id'], 'goal_id': goal, 'step_id': step, 'ceiling_id': ceiling,
            'role_id': role, 'scope_id': scope, 'link_id': link, 'role_ref': 'role:' + role}


class _Parser(argparse.ArgumentParser):
    def error(self, unused):
        self.exit(2, 'Role assembly configuration unavailable\n')


def main(argv=None):
    parser = _Parser(description=__doc__.split('\n\n')[0])
    for name in ('database-url-file', 'signing-key-file', 'artifact-root', 'assembler-credential-file', 'recipe-file'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--signing-key-id', default='active')
    args = parser.parse_args(argv)
    logger = logging.getLogger('psycopg.pool')
    disabled, logger.disabled = logger.disabled, True
    try:
        from nexloop_eios.assembly import verify_application_role
        from nexloop_eios.backend import open_backend
        if not re.fullmatch('[A-Za-z0-9_-]{1,64}', args.signing_key_id):
            raise RoleAssemblyUnavailable('signing key id')
        recipe = json.loads(read_private_text(args.recipe_file, maximum=32768))
        with open_backend(database_url=read_private_text(args.database_url_file, maximum=16384), artifact_root=args.artifact_root,
                          signing_key_file=args.signing_key_file, signing_key_id=args.signing_key_id) as backend:
            with backend._pool.connection() as db:
                if verify_application_role(db) != 'nexloop_api':
                    raise RoleAssemblyUnavailable('nexloop_api role required')

            def service():
                # Re-read and re-authenticate for every step: a revoked credential stops the assembly at once.
                return backend.authenticate(read_private_text(args.assembler_credential_file, maximum=16384), world='real')
            result = assemble_role(service, recipe)
        print(json.dumps(result, separators=(',', ':'), sort_keys=True))
        return 0
    except RoleAssemblyUnavailable as error:
        print('Role assembly unavailable: ' + str(error), file=sys.stderr)
        return 1
    except Exception:
        print('Role assembly unavailable', file=sys.stderr)
        return 1
    finally:
        logger.disabled = disabled


if __name__ == '__main__':
    raise SystemExit(main())
