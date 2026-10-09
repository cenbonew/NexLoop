"""Role policy enforcement after binding (0078); synthetic disposable PG only.

Governed policy EDIT, deactivation and expiry invalidate the bound policy, and a
Role Run can never bind through metadata alone.
"""
import json
import time
from datetime import UTC, datetime, timedelta
import psycopg
import pytest
from psycopg.types.json import Jsonb
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.object_edits import GovernedObjectEditor
from nexloop_eios.role_policies import CEILING_FIELDS, policy_envelope
from nexloop_eios.role_runs import role_binding_envelope
from multi_authority_fixture import seed_multi_authority
from test_role_policy_binding import policy_selection  # noqa: F401
from test_action_definitions import published_action  # noqa: F401


def current(admin, holder, args):
    """Owner-level verifier call: the bound policy must still be current."""
    p = policy_envelope(holder, **args)
    return admin.execute('select authz.nexloop_role_policy_current(%s,%s,%s,%s,%s)',
                         (holder._session.token_digest, 'real', p['text'], p['signature'], p['payload'])).fetchone()[0]


def ceiling_editor(admin, holder, ceiling):
    tenant = 'synthetic-a'
    row = admin.execute("select definition,capability from control.nexloop_action_definitions where tenant_id=%s and resource_id='eios:action:RoleExecutionCeiling.create:1'",
                        (tenant,)).fetchone()
    definition, capability = dict(row[0]), dict(row[1])
    definition.pop('contract_digest', None)
    definition['stable_name'] = 'RoleExecutionCeiling.edit'
    definition['capability_binding'] = {**definition['capability_binding'], 'capability_name': 'ontology.object.edit'}
    capability['capability_name'] = 'ontology.object.edit'
    from eios.ontology.definitions import ActionDefinition
    definition = ActionDefinition.model_validate_json(json.dumps(definition)).model_dump(mode='json')
    admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s)',
                  (tenant, 'real', 'eios:action:RoleExecutionCeiling.edit:1', Jsonb(definition), Jsonb(capability)))
    targets = [('eios:action:RoleExecutionCeiling.edit:1', ResourceType.ACTION, Operation.EXECUTE),
               ('eios:object:RoleExecutionCeiling/' + ceiling, ResourceType.OBJECT, Operation.EDIT)]
    targets.extend(('eios:property:RoleExecutionCeiling/' + ceiling + '/' + f, ResourceType.PROPERTY, Operation.EDIT) for f in CEILING_FIELDS)
    pool, signer = holder._backend._pool, holder._backend._signer
    _, token = seed_multi_authority(admin, pool, targets, identity_suffix='-ceiling-editor', extra_scopes=('ontology.roles.manage',))

    def edit(intent, properties):
        editor = GovernedObjectEditor(pool, authenticate_service(pool, token, world='real'), signer)
        return editor.edit(action_name='RoleExecutionCeiling.edit', action_version=1, intent_id=intent, type_name='RoleExecutionCeiling',
                           object_id=ceiling, expected_revision=1, properties=properties)
    return edit


def refreshed(holder):
    from nexloop_eios.authorization import _identity
    with holder._backend._pool.connection() as db, db.transaction():
        holder._session = _identity(db, holder._session.token_digest, 'real')
    return holder


def test_role_run_never_binds_through_metadata_alone(policy_selection, admin):
    bind, args, holder, run = policy_selection
    r = role_binding_envelope(holder, run_id=args['run_id'], role_id=args['role_id'], link_id=args['link_id'],
                              consumer_id=args['consumer_id'], step_id=args['step_id'])
    with holder._backend._pool.connection() as db, db.transaction():
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='Role execution policy mandatory'):
            db.execute('select authz.nexloop_role_run_bind(%s,%s,%s,%s,%s)', (holder._session.token_digest, 'real', *r))
    assert admin.execute('select count(*) from authz.nexloop_role_run_bindings').fetchone() == (0,)


@pytest.mark.parametrize('change', ['narrow_actions', 'deactivate'])
def test_governed_ceiling_edit_invalidates_bound_policy(policy_selection, admin, change):
    bind, args, holder, run = policy_selection
    bind()
    assert current(admin, holder, args)['ceiling_revision'] == 1
    ceiling = admin.execute("select properties from ontology.objects where type_name='RoleExecutionCeiling' and object_id=%s", (args['ceiling_id'],)).fetchone()[0]
    edit = ceiling_editor(admin, holder, args['ceiling_id'])
    patch = {'action_resources': ['eios:action:Goal.create:1']} if change == 'narrow_actions' else {'active': False}
    assert edit('ceiling-' + change, {**ceiling, **patch})['revision'] == 2
    refreshed(holder)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        current(admin, holder, args)


@pytest.mark.parametrize('policy_selection', ['short-ttl'], indirect=True)
def test_bound_policy_expiry_is_denied(policy_selection, admin):
    bind, args, holder, run = policy_selection
    value = bind()
    deadline = datetime.fromisoformat(value['expires_at'])
    assert deadline <= datetime.now(UTC) + timedelta(seconds=5)
    assert current(admin, holder, args)['run_id'] == args['run_id']
    time.sleep(max(0, (deadline - datetime.now(UTC)).total_seconds()) + 0.2)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        current(admin, holder, args)


def test_policy_wrappers_preserve_prior_public_acl(admin):
    from nexloop_eios.bootstrap import bootstrap
    bootstrap(admin)
    for name in ('nexloop_create_object_action', 'nexloop_edit_object_action'):
        signature = 'authz.' + name + '(text,text,text,text,text)'
        allowed = {role: admin.execute('select has_function_privilege(%s,%s,%s)', (role, signature, 'EXECUTE')).fetchone()[0]
                   for role in ('nexloop_api', 'nexloop_domain_worker', 'nexloop_action_worker', 'nexloop_scheduler', 'nexloop_identity')}
        assert allowed == {'nexloop_api': True, 'nexloop_domain_worker': True, 'nexloop_action_worker': True,
                           'nexloop_scheduler': False, 'nexloop_identity': False}
    assert admin.execute("select has_function_privilege('nexloop_api','authz.nexloop_role_run_bind(text,text,text,text,text)','EXECUTE')").fetchone()[0]
    assert not admin.execute("select has_function_privilege('nexloop_domain_worker','authz.nexloop_role_run_bind(text,text,text,text,text)','EXECUTE')").fetchone()[0]
