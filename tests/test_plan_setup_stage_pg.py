"""NX-032 G-PLAN / G-ROLE-READ on a stage-assembled tenant (clean catalog PostgreSQL, tests/assembly_dryrun_fixture.py path).

stage_secrets register -> business / catalog / enterprise setup -> relay enrollment -> role_assembly (and the owner's second
enrollment) -> the owner publishes the Context strategy, the trial budget and an NX-022 goal version on the owner's workbench
session -> nexloop_eios.plan_setup establishes the plan as plan_reevaluator -> the owner requests a reevaluation (the workbench's
request_plan_reevaluation) -> the plan-reevaluator's first round (the stage Compose command, --once) issues the Role Run.
Every write goes through the production tools or governed Actions; admin only probes (one SQL-level test seeds a fault rule). Synthetic data.
"""
import contextlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

import assembly_dryrun_fixture as A
from support.tenants import TENANT_A
from test_assembly_dryrun_pg import (assemble, assemble_role, owner_workbench, publish_strategy, run_workers,  # noqa: F401
                                     set_trial_budget, stage, tree)

TENANT = TENANT_A
GOAL = 'renewal-trial'
SETTINGS = A.ROOT / 'deploy/configuration/plan-reevaluation.v2.json'


@pytest.fixture
def planned(stage, admin, capsys):  # noqa: F811
    """The assembled tenant with its Role, the owner's strategy, budget and goal version."""
    st = assemble(stage)
    role = assemble_role(st)
    role_catalog(st)
    owner_workbench(st, admin)
    publish_strategy(st)
    set_trial_budget(st, admin)
    publish_goal(st)
    capsys.readouterr()
    return st, role


def role_catalog(st):
    """G-ROLE-CATALOG: the Role Context reads the catalog binding of its own Source (service_offerings.read_catalog), so the
    consumer gets a second offering and binding naming role_source: catalog_setup with the catalog maintainer and the role
    source's credential (the plan-reevaluator's source-credential file holds the same token)."""
    recipe = A.private(st.root, 'catalog-role-recipe.json', json.dumps(A.filled('catalog-recipe.example.json', NEW_UUID=str(uuid.uuid4()),
                                                                               CONSUMER_ID_FROM_BUSINESS_SETUP=st.result['business']['consumer_id'],
                                                                               TRIAL_PERIOD_END_UTC_ISO=A.end_of_trial())))
    code, out, err = A.run_tool('nexloop_eios.catalog_setup', [*st.backend_args(), '--maintainer-credential-file', str(st.setup_file('catalog-maintainer-credential')),
                                                               '--source-credential-file', str(st.out / 'app' / 'secrets' / 'plan-reevaluator' / 'source-credential'),
                                                               '--recipe-file', str(recipe)])
    assert code == 0, (out, err)
    st.result['role_catalog'] = json.loads(out)
    assert st.result['role_catalog']['source_ref'] == 'principal:nexloop-service-role-source-principal'
    return st.result['role_catalog']


@contextlib.contextmanager
def owner_actions(st):
    """The owner's governed goal Actions on the owner's own workbench session (the workbench login, as context_strategy_setup)."""
    from nexloop_eios.backend import open_backend
    from nexloop_eios.browser_authorization import authenticate_browser_business
    from nexloop_eios.context_strategy_setup import owner_session
    from nexloop_eios.goal_controls import GoalGovernedActions
    rate_key = bytes.fromhex((st.out / 'app' / 'secrets' / 'api' / 'browser-rate.key').read_text().strip())
    recipe = {'tenant_id': TENANT, 'workbench_application_id': 'stage-workbench'}
    with open_backend(database_url=st.api_dsn.read_text(), artifact_root=st.artifacts, signing_key_file=st.signing_key, signing_key_id='stage-1') as api, \
            owner_session((st.root / 'identity-dsn').read_text(), rate_key, recipe, 'dryrun-owner', (st.root / 'owner-password').read_text(),
                          'https://127.0.0.1:8443') as session:
        yield GoalGovernedActions(api._pool, authenticate_browser_business(api._pool, session, world='real'), api._signer)


def publish_goal(st):
    """G-GOAL: nexloop_eios.goal_setup, the owner approving the metric and publishing goal version 1 (again: replayed)."""
    now = datetime.now(UTC).replace(microsecond=0)
    body = A.filled('goal-setup.example.json', TENANT_ID=TENANT, WORKBENCH_APPLICATION_ID='stage-workbench', NEW_UUID=str(uuid.uuid4()),
                    METRIC_ID='renewal-rate', METRIC_NAME='Renewal rate', COHORT_RULE='frozen cohort', NX022_GOAL_ID=GOAL,
                    GOAL_OBJECTIVE='试运行内确认续费意向，不为指标硬发消息', NOW_UTC_ISO=(now - timedelta(hours=1)).isoformat(),
                    TRIAL_PERIOD_END_UTC_ISO=A.end_of_trial(), CHANGE_SUMMARY='synthetic trial goal', KR_KEY='renewal', KR_TARGET='0.70')
    recipe = A.private(st.root, 'goal-setup.json', json.dumps(body))
    username = st.root / 'owner-username'
    argv = ['--recipe', str(recipe), *st.backend_args()[:6], '--artifact-root', str(st.artifacts), '--identity-database-url-file', str(st.root / 'identity-dsn'),
            '--username-file', str(username), '--password-file', str(st.root / 'owner-password'),
            '--browser-rate-key-file', str(st.out / 'app' / 'secrets' / 'api' / 'browser-rate.key'), '--browser-origin', 'https://127.0.0.1:8443']
    code, out, err = A.run_tool('nexloop_eios.goal_setup', argv)
    assert code == 0, (out, err)
    first = json.loads(out)
    assert first['goal_version_ref'] == f'goal:{GOAL}@1' and first['goal_replayed'] is False and first['metric_replayed'] is False
    code, out, err = A.run_tool('nexloop_eios.goal_setup', argv)
    again = json.loads(out)
    assert code == 0 and again['goal_replayed'] is True and again['metric_replayed'] is True, (out, err)
    password = (st.root / 'owner-password').read_text()
    assert password not in first.__repr__() + err and 'dryrun-owner' not in out + err  # credentials are never printed
    return first


def plan_recipe(st, role, **changes):
    r = st.result
    body = A.filled('plan-setup.example.json', NEW_UUID=str(uuid.uuid4()), CONSUMER_ID_FROM_BUSINESS_SETUP=r['business']['consumer_id'],
                    GOAL_VERSION_REF_FROM_GOAL_SETUP=f'goal:{GOAL}@1', STRATEGY_REF_FROM_CONTEXT_STRATEGY_SETUP='context-strategy:recent_plus_required@1', PLAN_STRATEGY_TEXT='先确认意向，再谈付款方式',
                    ROLE_ID_FROM_ROLE_ASSEMBLY=role['role_id'], LINK_ID_FROM_ROLE_ASSEMBLY=role['link_id'], STEP_ID_FROM_ROLE_ASSEMBLY=role['step_id'],
                    OFFERING_ID_FROM_CATALOG_SETUP=r['role_catalog']['offering_id'], BINDING_ID_FROM_CATALOG_SETUP=r['role_catalog']['binding_id'],
                    CONTROL_ID_FROM_BUSINESS_SETUP=r['business']['control_id'], PLAN_STEP_KEY='confirm-renewal',
                    PLAN_STEP_PREREQUISITE='客户已收到续费提醒', PLAN_STEP_EXPECTED_RESULT='确认续费意向，不催促',
                    REASSESS_AT_UTC_ISO=(datetime.now(UTC) + timedelta(days=1)).replace(microsecond=0).isoformat().replace('+00:00', 'Z'))
    return body | changes


def plan_setup(st, recipe, name=None):
    """nexloop_eios.plan_setup with the plan reevaluator's own DSN and credential (the generated stage tree)."""
    secrets = st.out / 'app' / 'secrets' / 'plan-reevaluator'
    dsn = st.root / 'plan-reevaluator-dsn'
    if not dsn.exists():
        A.private(st.root, dsn.name, A.make_conninfo(st.pg, user='nexloop_domain_worker'))
    path = st.root / (name or 'plan-' + recipe['plan_id'] + '.json')
    if not path.exists():
        A.private(st.root, path.name, json.dumps(recipe))
    assert json.loads(path.read_text()) == recipe
    return A.run_tool('nexloop_eios.plan_setup', ['--database-url-file', str(dsn), '--signing-key-file', str(st.signing_key), '--signing-key-id', 'stage-1',
                                                  '--artifact-root', str(st.artifacts), '--service-credential-file', str(secrets / 'service-credential'),
                                                  '--settings-file', str(SETTINGS), '--recipe-file', str(path)])


def test_plan_setup_establishes_as_the_plan_reevaluator_and_replays(planned, admin):
    st, role = planned
    recipe = plan_recipe(st, role)
    code, out, err = plan_setup(st, recipe)
    assert code == 0, (out, err)
    first = json.loads(out)
    assert first['established'] and first['replay'] is False and first['plan_id'] == recipe['plan_id'] and first['version'] == 1
    code, out, err = plan_setup(st, recipe)  # the same plan id and recipe: replayed, nothing written twice
    assert code == 0 and json.loads(out)['replay'] is True, (out, err)
    row = admin.execute("select consumer_id, goal_version_ref, recipe - 'goal_chain', settings, created_by, control_snapshot->'goals' from runtime.nexloop_plans "
                        "where tenant_id=%s and plan_id=%s", (TENANT, recipe['plan_id'])).fetchall()
    settings = json.loads(SETTINGS.read_text())
    assert row == [(recipe['consumer_id'], f'goal:{GOAL}@1', recipe['recipe'], {k: settings[k] for k in ('self_window_seconds', 'self_trigger_cap', 'min_reassess_seconds')},
                    'service', [{'goal_id': GOAL, 'version': 1}])]
    # The plan waits for its reassess_at (a day ahead) in the work feed: no resident loop.
    assert admin.execute("select status, available_at > clock_timestamp() + interval '12 hours' from runtime.nexloop_work_feed where tenant_id=%s and feed='plan-reevaluate'",
                         (TENANT,)).fetchall() == [('pending', True)]
    # Refusals: the same plan id with another recipe; a goal version that is not current; another principal's credential.
    code, _, err = plan_setup(st, {**recipe, 'recipe': {**recipe['recipe'], 'link_id': role['scope_id']}}, 'reused-recipe.json')
    assert code == 1 and 'plan id reused' in err, err
    code, _, err = plan_setup(st, plan_recipe(st, role, goal_version_ref=f'goal:{GOAL}@2'), 'future-goal.json')
    assert code == 1 and 'goal version superseded' in err, err
    code, _, err = plan_setup(st, plan_recipe(st, role, context_strategy_ref='context-strategy:recent_only@1'), 'unpublished-strategy.json')
    assert code == 1 and 'plan refused' in err, err  # a Context strategy the tenant has not published
    assert admin.execute('select count(*) from runtime.nexloop_plans where tenant_id=%s', (TENANT,)).fetchone() == (1,)


def test_a_credential_that_is_not_the_plan_reevaluator_is_refused(planned, admin):
    st, role = planned
    secrets = st.out / 'app' / 'secrets' / 'plan-reevaluator'
    recipe = plan_recipe(st, role)
    path = A.private(st.root, 'other.json', json.dumps(recipe))
    dsn = A.private(st.root, 'domain-dsn', A.make_conninfo(st.pg, user='nexloop_domain_worker'))
    # The Role Source's credential (same tree, no nexloop.plan.reevaluate grant).
    code, _, err = A.run_tool('nexloop_eios.plan_setup', ['--database-url-file', str(dsn), '--signing-key-file', str(st.signing_key), '--signing-key-id', 'stage-1',
                                                          '--artifact-root', str(st.artifacts), '--service-credential-file', str(secrets / 'source-credential'),
                                                          '--settings-file', str(SETTINGS), '--recipe-file', str(path)])
    assert code == 1 and 'Plan setup unavailable' in err, err
    # The API role instead of the domain worker role: refused before any write.
    code, _, err = A.run_tool('nexloop_eios.plan_setup', ['--database-url-file', str(st.api_dsn), '--signing-key-file', str(st.signing_key), '--signing-key-id', 'stage-1',
                                                          '--artifact-root', str(st.artifacts), '--service-credential-file', str(secrets / 'service-credential'),
                                                          '--settings-file', str(SETTINGS), '--recipe-file', str(path)])
    assert code == 1 and 'nexloop_domain_worker role required' in err, err
    assert admin.execute('select count(*) from runtime.nexloop_plans where tenant_id=%s', (TENANT,)).fetchone() == (0,)


def test_the_plan_reevaluator_first_round_issues_the_role_run(planned, admin, capsys):
    """G-PLAN + G-ROLE-READ: after plan_setup and the owner's reevaluation request, the plan-reevaluator's stage command (--once)
    claims the plan, prechecks it and issues one governed Role Run (Role policy binding, Context v6 pack, queued activation)."""
    st, role = planned
    recipe = plan_recipe(st, role)
    code, out, err = plan_setup(st, recipe)
    assert code == 0, (out, err)
    with owner_actions(st) as owner:
        owner.request_plan_reevaluation(action_name='nexloop.plan.request_reevaluation', action_version=1, request_id='g-plan-reevaluate-' + recipe['plan_id'],
                                        plan_id=recipe['plan_id'], reason='试运行：首轮复评')
    results = run_workers(st, admin, capsys, ('plan-reevaluator',))
    code, detail = results['plan-reevaluator']
    with capsys.disabled():
        print(f'\nPLAN reevaluator: {code} {detail[:400]}')
        print('FEED', admin.execute("select status, last_code, attempts from runtime.nexloop_work_feed where tenant_id=%s and feed='plan-reevaluate'", (TENANT,)).fetchall())
    assert code == 0 and '"launched":1' in detail, (detail, admin.execute(
        "select status, last_code from runtime.nexloop_work_feed where tenant_id=%s and feed='plan-reevaluate'", (TENANT,)).fetchall())
    (run_id,), = admin.execute('select run_id::text from runtime.nexloop_plan_runs where tenant_id=%s and plan_id=%s', (TENANT, recipe['plan_id'])).fetchall()
    assert admin.execute('select allowed_resources from authz.nexloop_run_credentials where run_id=%s', (run_id,)).fetchone()[0] == [
        'eios:action:nexloop.service.request:1']
    assert admin.execute('select count(*) from authz.nexloop_role_policy_bindings where run_id=%s', (run_id,)).fetchone() == (1,)
    assert admin.execute('select count(*) from runtime.nexloop_context_packs where run_id=%s', (run_id,)).fetchone() == (1,)
    (task_id,) = admin.execute('select task_id from authz.nexloop_runtime_run_bindings where run_id=%s', (run_id,)).fetchone()
    (payload, queue) = admin.execute('select normalized_input, queue from runtime.jobs where job_id::text=%s', (task_id,)).fetchone()
    command = payload['run_command']
    assert queue == 'operations' and command['consumer_ref'] == 'consumer:' + recipe['consumer_id'] and command['runtime_profile'] == 'deepseek-flash'
    assert command['budget'] == {'maximum_model_turns': 4, 'maximum_tool_calls': 8, 'active_timeout_seconds': 120, 'maximum_cost': '2.00', 'currency': 'CNY'}



def service(st, credential):
    """A stage service session on the API role (the Role launch's backend), re-authenticated from its credential file."""
    from nexloop_eios.backend import open_backend
    backend = open_backend(database_url=st.api_dsn.read_text(), artifact_root=st.artifacts, signing_key_file=st.signing_key, signing_key_id='stage-1')
    return backend, backend.__enter__().authenticate(credential.read_text(), world='real')


@contextlib.contextmanager
def reader(st, credential):
    backend = None
    try:
        backend, current = service(st, credential)
        yield current
    finally:
        if backend is not None:
            backend.__exit__(None, None, None)


SOURCE = ('app', 'secrets', 'plan-reevaluator', 'source-credential')


def readable(st, credential, type_name, object_id, fields=()):
    with reader(st, credential) as current:
        try:
            current.read_object(type_name=type_name, object_id=object_id, fields=fields)
            return True
        except Exception:  # noqa: BLE001 - the refusal (configured denial or derivation unavailable) is the result
            return False


def plan_objects(st, role, admin):
    """The objects a Role launch of this plan reads, with the fields it reads (role_source)."""
    r = st.result
    step = admin.execute("select properties->>'goal_id' from ontology.objects where object_id=%s", (role['step_id'],)).fetchone()[0]
    return [('Consumer', r['business']['consumer_id'], ()), ('Goal', step, ()), ('PlanStep', role['step_id'], ('consumer_id', 'state', 'submitter_principals')),
            ('EffectControl', r['business']['control_id'], ('allow_effect', 'budget_units', 'executor_principal', 'valid_until')),
            ('ConsumerServiceOffering', r['role_catalog']['binding_id'], ('active', 'consumer_id', 'offering_id', 'offering_revision', 'source_principal')),
            ('ServiceOffering', r['role_catalog']['offering_id'], ('active', 'title')),
            ('RoleDefinition', role['role_id'], ('active', 'ceiling_ref', 'name', 'responsibility', 'valid_from', 'valid_until')),
            ('ConsumerRoleLink', role['link_id'], ('active', 'consumer_id', 'role_id', 'scope', 'valid_from', 'valid_until')),
            ('RoleExecutionCeiling', role['ceiling_id'], ('action_resources', 'active', 'budget', 'effect_units')),
            ('RoleAssignmentScope', role['scope_id'], ('active', 'consumer_id', 'goal_ids', 'role_id', 'step_ids'))]


def plan_reevaluator_port(st):
    from nexloop_eios.backend import open_backend
    from nexloop_eios.plan_reevaluation import PlanPort
    backend = open_backend(database_url=(st.root / 'plan-reevaluator-dsn').read_text(), artifact_root=st.artifacts, signing_key_file=st.signing_key,
                           signing_key_id='stage-1')
    current = backend.__enter__().authenticate((st.out / 'app' / 'secrets' / 'plan-reevaluator' / 'service-credential').read_text(), world='real')
    return backend, PlanPort(current._backend._pool, current._session, current._backend._signer)


def test_role_source_reads_only_the_objects_of_an_active_plan(planned, admin):
    """0211: role_source derives READ on exactly the plan's objects while the plan is current and active; nothing before the plan,
    nothing outside the plan or outside the fixed fields, nothing for another Source, nothing once the plan is invalidated."""
    st, role = planned
    source = st.out.joinpath(*SOURCE)
    objects = plan_objects(st, role, admin)
    # Before any plan: the G-ROLE-READ refusal (no configured grant, nothing to derive from).
    assert [readable(st, source, t, o, f) for t, o, f in objects] == [False] * len(objects)
    recipe = plan_recipe(st, role)
    assert plan_setup(st, recipe)[0] == 0
    assert [readable(st, source, t, o, f) for t, o, f in objects] == [True] * len(objects)
    # Outside the plan: the relay's own catalog binding (names relay_source) and the enterprise slot; a field not in the fixed list.
    r = st.result
    assert not readable(st, source, 'ConsumerServiceOffering', r['catalog']['binding_id'], ('active',))
    assert not readable(st, source, 'ServiceOffering', r['enterprise']['enterprise_offering_id'], ('active',))
    assert not readable(st, source, 'EffectControl', r['enterprise']['enterprise_control_id'], ('allow_effect',))
    assert not readable(st, source, 'PlanStep', role['step_id'], ('action_name',))
    # Another Source (the relay Source holds no role_source_read_rule and is not the step's submitter).
    relay = st.out / 'app' / 'secrets' / 'message-relay' / 'source-credential'
    assert not readable(st, relay, 'RoleDefinition', role['role_id'], ('active',))
    assert not readable(st, relay, 'ConsumerRoleLink', role['link_id'], ('active',))
    # The plan invalidated through its own port (plan reevaluator, verb mark): the derivation ends at the next read.
    backend, port = plan_reevaluator_port(st)
    try:
        port.mark(plan_id=recipe['plan_id'], version=1, status='invalidated', reason='synthetic: goal withdrawn')
    finally:
        backend.__exit__(None, None, None)
    assert [readable(st, source, t, o, f) for t, o, f in objects] == [False] * len(objects)
    # No per-object fact was written and role_source's credential binding kept its scopes (no rotation).
    principal = 'nexloop-service-role-source-principal'
    assert admin.execute("select count(*) from authz.nexloop_authority_facts where tenant_id=%s and fact_kind='grants' and entity_key[1]=%s and entity_key[2] like 'eios:object:%%'",
                         (TENANT, principal)).fetchone() == (0,)
    scopes = admin.execute("select binding->'requested_scopes' from authz.nexloop_service_credentials where tenant_id=%s and binding->>'subject_principal_id'=%s",
                           (TENANT, principal)).fetchone()[0]
    assert not {'object.read', 'property.read'} & set(scopes), scopes


def test_the_derivation_is_scoped_to_the_tenant_the_rule_and_the_step_submitter(planned, admin):
    """SQL level (admin probes and one seeded fault): another tenant derives nothing for the same object; a principal with a rule
    that the plan's step does not name as a submitter derives nothing; the rule's own principal does."""
    from support.tenants import TENANT_B
    st, role = planned
    recipe = plan_recipe(st, role)
    assert plan_setup(st, recipe)[0] == 0
    target = 'eios:object:RoleDefinition/' + role['role_id']
    derive = "select authz.nexloop_role_source_derivation(%s,%s,'real',%s,'read')"
    source, relay = 'nexloop-service-role-source-principal', 'nexloop-service-relay-source-principal'
    basis = admin.execute(derive, (TENANT, source, target)).fetchone()[0]
    assert basis['derivation'] == 'role-source-v1' and basis['plan_id'] == recipe['plan_id'] and basis['plan_version'] == 1
    assert admin.execute(derive, (TENANT_B, source, target)).fetchone() == (None,)
    assert admin.execute(derive, (TENANT, relay, target)).fetchone() == (None,)  # no rule
    # Seeded fault (admin): a rule for the relay Source; the step's submitter_principals still names only role_source.
    admin.execute("insert into authz.nexloop_authority_facts values(%s,'role_source_read_rule',array[%s],%s)",
                  (TENANT, relay, json.dumps({'tenant_id': TENANT, 'principal_id': relay, 'active': True, 'valid_until': '2027-10-09T00:00:00+00:00'})))
    assert admin.execute(derive, (TENANT, relay, target)).fetchone() == (None,)
    assert admin.execute(derive, (TENANT, source, 'eios:object:RoleDefinition/' + 'e' * 64)).fetchone() == (None,)  # not a plan object
    assert admin.execute(derive, (TENANT, source, 'eios:property:RoleDefinition/' + role['role_id'] + '/name')).fetchone()[0] is not None
    assert admin.execute(derive, (TENANT, source, 'eios:property:Consumer/' + st.result['business']['consumer_id'] + '/state')).fetchone() == (None,)
