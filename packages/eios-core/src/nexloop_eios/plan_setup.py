"""NX-032 G-PLAN: establish a consumer's reevaluation plan as the plan_reevaluator service principal (0106 verb establish).

    python -m nexloop_eios.plan_setup --database-url-file F --signing-key-file F --signing-key-id ID --artifact-root D
        --service-credential-file F --settings-file F --recipe-file F

The plan port (PlanPort, eios:action:nexloop.plan.reevaluate:1) is the only writer of plans; this tool calls its establish verb
with the plan reevaluator's own credential and database role (nexloop_domain_worker), exactly as the deployed reevaluator calls
its other verbs. SQL derives the plan's control snapshot (consumer scope, the NX-022 goal version, the Consumer, the model budget)
and refuses a goal version that is not the current one, a Context strategy the tenant has not published and any other shape.
The plan settings (self-trigger window and cap, minimum reassess interval) come from the same deployment file the reevaluator
mounts (deploy/configuration/plan-reevaluation.v2.json on the stage), never from the recipe.

Recipe (schema_version nexloop-plan-setup/1, deploy/examples/nx037/plan-setup.example.json):
  {"schema_version", "plan_id": UUID, "consumer_id", "goal_version_ref": "goal:<NX-022 goal id>@<version>",
   "context_strategy_ref": "context-strategy:<id>@<version>", "strategy": {"content"} | null,
   "recipe": {"role_id", "link_id", "step_id"   (nexloop_eios.role_assembly output),
              "offering_id", "binding_id"        (catalog_setup output), "control_id" (business_setup output)},
   "steps": [{step_key, step_object_id, prerequisites, expected_result, stop_if, reassess_at, budget, intent_ref}, ...]}
The plan id is the stable business-intent id: running the tool again with the same recipe replays (`replay: true`); the same plan
id with a different recipe is refused. A step budget above the Role's execution ceiling makes every launch fail (the Role issuance
refuses it): keep each step budget within the ceiling of the role assembly recipe.
Prints one JSON object of ids and codes; credentials are read from private files and never printed. Exit 0 established or
replayed, 1 refused or unavailable.
"""
import argparse
import json
import logging
import re
import sys
import uuid
from pathlib import Path

from nexloop_eios.private_configuration import read_private_text

SCHEMA = 'nexloop-plan-setup/1'
_ID = re.compile('[a-f0-9]{64}')
_GOAL = re.compile(r'goal:[a-z0-9][a-z0-9._-]{0,127}@[1-9][0-9]{0,8}')
_CONTEXT = re.compile(r'context-strategy:[a-z][a-z0-9_]{0,63}@[1-9][0-9]{0,6}')
_RECIPE = ('role_id', 'link_id', 'step_id', 'offering_id', 'binding_id', 'control_id')


class PlanSetupUnavailable(RuntimeError):
    def __init__(self, detail='plan_setup_unavailable'):
        super().__init__(detail)


def strict_recipe(value):
    """The validated recipe; the step shape is re-validated by SQL (runtime.nexloop_plan_steps_valid)."""
    if type(value) is not dict or set(value) != {'schema_version', 'plan_id', 'consumer_id', 'goal_version_ref', 'context_strategy_ref', 'strategy',
                                                 'recipe', 'steps'} or value['schema_version'] != SCHEMA:
        raise PlanSetupUnavailable('recipe shape')
    if type(value['plan_id']) is not str or str(uuid.UUID(value['plan_id'])) != value['plan_id']:
        raise PlanSetupUnavailable('plan_id')
    if type(value['consumer_id']) is not str or not _ID.fullmatch(value['consumer_id']):
        raise PlanSetupUnavailable('consumer_id')
    if type(value['goal_version_ref']) is not str or not _GOAL.fullmatch(value['goal_version_ref']):
        raise PlanSetupUnavailable('goal_version_ref')
    if type(value['context_strategy_ref']) is not str or not _CONTEXT.fullmatch(value['context_strategy_ref']):
        raise PlanSetupUnavailable('context_strategy_ref')
    strategy = value['strategy']
    if strategy is not None and (type(strategy) is not dict or set(strategy) != {'content'} or type(strategy['content']) is not str
                                 or not 1 <= len(strategy['content']) <= 8192):
        raise PlanSetupUnavailable('strategy')
    recipe = value['recipe']
    if type(recipe) is not dict or set(recipe) != set(_RECIPE) or any(type(recipe[k]) is not str or not _ID.fullmatch(recipe[k]) for k in _RECIPE):
        raise PlanSetupUnavailable('recipe ids')
    if type(value['steps']) is not list or not 1 <= len(value['steps']) <= 32 or any(type(s) is not dict for s in value['steps']):
        raise PlanSetupUnavailable('steps')
    return value


def plan_body(recipe, settings):
    """The establish payload: the recipe plus the plan settings of the reevaluator's deployment file."""
    from nexloop_eios.plan_reevaluation import plan_settings
    recipe = strict_recipe(recipe)
    return {'plan_id': recipe['plan_id'], 'consumer_id': recipe['consumer_id'], 'goal_version_ref': recipe['goal_version_ref'],
            'strategy': recipe['strategy'], 'context_strategy_ref': recipe['context_strategy_ref'], 'recipe': dict(recipe['recipe']),
            'settings': plan_settings(settings), 'steps': list(recipe['steps'])}


def establish_plan(service, recipe, settings):
    """Establish (or replay) the plan through the plan port with the reevaluator's current service session."""
    from eios.identity.models import SubjectKind
    from nexloop_eios.plan_reevaluation import PlanPort, PlanUnavailable
    body = plan_body(recipe, settings)
    current = service()
    session = current._session
    if session.world != 'real' or session.run_context is not None or session.authentication.subject_kind is not SubjectKind.SERVICE:
        raise PlanSetupUnavailable('real service session required')
    try:
        result = PlanPort(current._backend._pool, session, current._backend._signer).establish(body)
    except PlanUnavailable:
        raise PlanSetupUnavailable('plan port refused') from None
    except Exception as error:
        # SQL names the refusal in its message (shape, goal version superseded, context strategy unavailable, plan id reused).
        sqlstate = getattr(error, 'sqlstate', None)
        if sqlstate in ('22023', '40001', 'NXC03'):
            raise PlanSetupUnavailable({'22023': 'plan refused', '40001': 'plan id reused with a different recipe',
                                        'NXC03': 'goal version superseded'}[sqlstate]) from None
        raise
    return {'established': True, 'plan_id': str(result['plan_id']), 'version': result['version'], 'replay': bool(result['replay']),
            'consumer_id': body['consumer_id'], 'goal_version_ref': body['goal_version_ref'], 'role_id': body['recipe']['role_id'],
            'link_id': body['recipe']['link_id'], 'step_id': body['recipe']['step_id']}


class _Parser(argparse.ArgumentParser):
    def error(self, unused):
        self.exit(2, 'Plan setup configuration unavailable\n')


def main(argv=None):
    parser = _Parser(description=__doc__.split('\n\n')[0])
    for name in ('database-url-file', 'signing-key-file', 'artifact-root', 'service-credential-file', 'settings-file', 'recipe-file'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--signing-key-id', default='active')
    args = parser.parse_args(argv)
    logger = logging.getLogger('psycopg.pool')
    disabled, logger.disabled = logger.disabled, True
    try:
        from nexloop_eios.assembly import verify_application_role
        from nexloop_eios.backend import open_backend
        from nexloop_eios.plan_reevaluation import load_settings
        if not re.fullmatch('[A-Za-z0-9_-]{1,64}', args.signing_key_id):
            raise PlanSetupUnavailable('signing key id')
        try:
            settings = load_settings(args.settings_file)
        except (OSError, ValueError):
            raise PlanSetupUnavailable('settings file') from None
        recipe = json.loads(read_private_text(args.recipe_file, maximum=65536))
        strict_recipe(recipe)
        with open_backend(database_url=read_private_text(args.database_url_file, maximum=16384), artifact_root=args.artifact_root,
                          signing_key_file=args.signing_key_file, signing_key_id=args.signing_key_id, pool_max_size=1) as backend:
            with backend._pool.connection() as db:
                if verify_application_role(db) != 'nexloop_domain_worker':
                    raise PlanSetupUnavailable('nexloop_domain_worker role required')

            def service():
                # Re-read and re-authenticate: a revoked plan reevaluator credential stops the setup at once.
                return backend.authenticate(read_private_text(args.service_credential_file, maximum=16384), world='real')
            result = establish_plan(service, recipe, settings)
        print(json.dumps(result, separators=(',', ':'), sort_keys=True))
        return 0
    except PlanSetupUnavailable as error:
        print('Plan setup unavailable: ' + str(error), file=sys.stderr)
        return 1
    except Exception:
        print('Plan setup unavailable', file=sys.stderr)
        return 1
    finally:
        logger.disabled = disabled


if __name__ == '__main__':
    raise SystemExit(main())
