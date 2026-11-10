"""nexloop_eios.effect_control_budget: an exhausted EffectControl raised through EffectControl.edit:1 + configure_control:1 only."""
import json
import subprocess
import sys
import uuid

import pytest
from psycopg.conninfo import make_conninfo
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios.effect_contexts import EffectContextUnavailable
from nexloop_eios.effect_intents import EffectIntentUnavailable
from nexloop_eios.effect_control_budget import EffectControlBudgetUnavailable, plan, raise_budget
from multi_authority_fixture import seed_multi_authority
from effect_execution_fixture import execution_plan  # noqa: F401 (fixture)


def private(tmp_path, name, value):
    path = tmp_path / name
    path.write_text(value)
    path.chmod(0o600)
    return path


class Rig:
    def __init__(self, p, admin, pg, tmp_path):
        self.p, self.admin, self.pg, self.tmp_path = p, admin, pg, tmp_path
        self.backend = p['backend']
        self.owner_file = private(tmp_path, 'owner-credential', p['owner_token'])
        self.executor_file = private(tmp_path, 'executor-credential', p['executor_token'])
        self.steps = 1
        self.editor_file = self.owner_file  # until grant_edit: the owner holds no edit authority
        self.editor_token = p['owner_token']

    def grant_edit(self):
        """A separate editor principal with EffectControl.edit:1 and EDIT on this control's budget_units and valid_until."""
        control = self.p['control']
        _, token = seed_multi_authority(self.admin, self.backend._pool, [
            ('eios:action:EffectControl.edit:1', ResourceType.ACTION, Operation.EXECUTE),
            ('eios:object:EffectControl/' + control, ResourceType.OBJECT, Operation.EDIT),
            ('eios:property:EffectControl/' + control + '/budget_units', ResourceType.PROPERTY, Operation.EDIT),
            ('eios:property:EffectControl/' + control + '/valid_until', ResourceType.PROPERTY, Operation.EDIT)], identity_suffix='-budget-editor')
        self.editor_file = private(self.tmp_path, 'editor-credential', token)
        self.editor_token = token

    def ledger(self):
        return self.admin.execute('select control_revision,budget_units,reserved_units from control.nexloop_effect_control_ledger where control_id=%s',
                                  (self.p['control'],)).fetchone()

    def revision(self):
        return self.admin.execute("select nexloop_revision,(properties->>'budget_units')::int from ontology.objects where object_id=%s", (self.p['control'],)).fetchone()

    def reply(self, control_revision):
        """One more Run's reply for a fresh PlanStep: bind its context, then submit one intent (reserves one unit)."""
        self.steps += 1
        # Fresh sessions: a new grant moves the tenant's authority revision.
        planner = self.backend.authenticate(self.p['planner_token'], world='real')
        submitter = self.backend.authenticate(self.p['submitter_token'], world='real')
        second = self.backend.authenticate(self.p['second_token'], world='real')
        step = planner.create_object(action_name='PlanStep.create', action_version=1, intent_id='budget-step-' + str(self.steps), type_name='PlanStep',
            properties={'consumer_id': self.p['consumer'], 'goal_id': self.p['goal'], 'control_id': self.p['control'],
                        'submitter_principals': sorted([submitter._session.authentication.subject_principal_id, second._session.authentication.subject_principal_id]),
                        'action_name': self.p['effect'], 'state': 'ready'})['object_id']
        run = submitter.issue_run_credential(action_resources=['eios:action:' + self.p['effect'] + ':1'])
        planner.bind_effect_context(step_id=step, step_revision=1, goal_revision=1,
            consumer_revision=1, control_revision=control_revision, run_id=run.run_id, run_token=run.token, executor_token=self.p['executor_token'])
        port = self.backend.authenticate_run(run.token, world='real', run_id=run.run_id)
        return port.submit_effect_intent(parameters={'message': 'reply ' + str(self.steps)})

    def recipe(self, from_revision, budget_units, intent='budget-raise-0001', **extra):
        return {'schema_version': '1.0', 'control_id': self.p['control'], 'from_revision': from_revision, 'budget_units': budget_units, 'intent_id': intent, **extra}

    def run(self, recipe, *, executor_file=None, owner_file=None, dry_run=False):
        return raise_budget(self.backend, owner_credential_file=owner_file or self.owner_file, editor_credential_file=self.editor_file,
                            executor_credential_file=executor_file or self.executor_file,
                            recipe=recipe, dry_run=dry_run)


@pytest.fixture
def rig(execution_plan, admin, pg, tmp_path):
    r = Rig(execution_plan, admin, pg, tmp_path)
    execution_plan['owner'].configure_effect_control(control_id=execution_plan['control'], control_revision=1, executor_token=execution_plan['executor_token'])
    assert r.reply(1)['state'] == 'accepted'  # the only unit: the control is exhausted
    assert r.ledger() == (1, 1, 1)
    with pytest.raises(EffectIntentUnavailable):
        r.reply(1)
    return r


def test_exhausted_control_raised_then_bind_succeeds_and_replay_adds_nothing(rig):
    rig.grant_edit()
    result = rig.run(rig.recipe(1, 3))
    assert result == {'control_id': rig.p['control'], 'from_revision': 1, 'to_revision': 2, 'object_revision': 2, 'ledger_revision': 2,
                      'budget_units': 3, 'reserved_units': 1, 'remaining': 2, 'phase': 'edit', 'replayed': False}
    assert rig.ledger() == (2, 3, 1) and rig.revision() == (2, 3)
    assert rig.reply(2)['state'] == 'accepted'
    assert rig.ledger() == (2, 3, 2)
    again = rig.run(rig.recipe(1, 3))
    assert again['phase'] == 'done' and again['replayed'] is True and again['budget_units'] == 3 and again['reserved_units'] == 2
    assert rig.ledger() == (2, 3, 2) and rig.revision() == (2, 3)


def test_budget_below_reserved_refused_before_any_write(rig):
    rig.grant_edit()
    rig.run(rig.recipe(1, 3))
    rig.reply(2)
    assert rig.ledger() == (2, 3, 2)
    with pytest.raises(EffectControlBudgetUnavailable) as refused:
        rig.run(rig.recipe(2, 1, intent='budget-lower-0001'))
    assert refused.value.reason == 'below_reserved'
    assert rig.ledger() == (2, 3, 2) and rig.revision() == (2, 3)


def test_owner_without_edit_authority_refused_and_nothing_moves(rig):
    with pytest.raises(EffectControlBudgetUnavailable) as refused:
        rig.run(rig.recipe(1, 3))
    assert refused.value.reason == 'edit_unavailable'
    assert rig.ledger() == (1, 1, 1) and rig.revision() == (1, 1)


def test_a_credential_that_is_not_the_control_owner_cannot_read_or_raise(rig):
    planner = private(rig.tmp_path, 'planner-credential', rig.p['planner_token'])
    with pytest.raises(EffectControlBudgetUnavailable) as refused:
        rig.run(rig.recipe(1, 3), owner_file=planner)
    assert refused.value.reason == 'control_owner_unavailable'
    assert rig.ledger() == (1, 1, 1) and rig.revision() == (1, 1)


def test_configure_failure_leaves_binds_refused_until_the_same_recipe_completes_it(rig):
    rig.grant_edit()
    wrong = private(rig.tmp_path, 'wrong-executor', rig.p['planner_token'])
    with pytest.raises(EffectControlBudgetUnavailable) as refused:
        rig.run(rig.recipe(1, 3), executor_file=wrong)
    assert refused.value.reason == 'configure_unavailable'
    assert rig.revision() == (2, 3) and rig.ledger() == (1, 1, 1)
    for revision in (1, 2):
        with pytest.raises(EffectContextUnavailable):
            rig.reply(revision)  # the gap: object at revision 2, ledger at 1
    result = rig.run(rig.recipe(1, 3))
    assert result['phase'] == 'configure' and result['replayed'] is True and result['remaining'] == 2
    assert rig.ledger() == (2, 3, 1)
    rig.reply(2)
    assert rig.ledger() == (2, 3, 2)


def test_another_intent_on_the_edited_control_is_not_a_replay(rig):
    rig.grant_edit()
    wrong = private(rig.tmp_path, 'wrong-executor', rig.p['planner_token'])
    with pytest.raises(EffectControlBudgetUnavailable):
        rig.run(rig.recipe(1, 3), executor_file=wrong)
    with pytest.raises(EffectControlBudgetUnavailable) as refused:
        rig.run(rig.recipe(1, 3, intent='budget-raise-other'))
    assert refused.value.reason == 'edit_unavailable'
    assert rig.revision() == (2, 3) and rig.ledger() == (1, 1, 1)


def test_dry_run_reads_only(rig):
    rig.grant_edit()
    before = rig.admin.execute('select count(*) from control.nexloop_action_claims').fetchone() if rig.admin.execute(
        "select to_regclass('control.nexloop_action_claims')").fetchone()[0] else None
    planned = rig.run(rig.recipe(1, 30), dry_run=True)
    assert planned['dry_run'] is True and planned['phase'] == 'edit' and planned['planned_budget_units'] == 30 and planned['planned_remaining'] == 29
    assert planned['reserved_units'] == 1 and planned['remaining'] == 0
    assert rig.ledger() == (1, 1, 1) and rig.revision() == (1, 1)
    if before is not None:
        assert rig.admin.execute('select count(*) from control.nexloop_action_claims').fetchone() == before


def test_cli_prints_one_line_without_secrets(rig):
    rig.grant_edit()
    t = rig.tmp_path
    argv = ['--database-url-file', str(private(t, 'dsn', make_conninfo(rig.pg, user='nexloop_api'))), '--signing-key-file', str(rig.p['key']),
            '--signing-key-id', 'synthetic-plan', '--artifact-root', str(t / 'cli-artifacts'), '--owner-credential-file', str(rig.owner_file),
            '--editor-credential-file', str(rig.editor_file),
            '--executor-credential-file', str(rig.executor_file), '--recipe-file', str(private(t, 'recipe', json.dumps(rig.recipe(1, 4))))]
    run = lambda extra=(): subprocess.run([sys.executable, '-m', 'nexloop_eios.effect_control_budget', *argv, *extra], capture_output=True, text=True, timeout=60)
    dry = run(['--dry-run'])
    assert dry.returncode == 0 and dry.stderr == '' and json.loads(dry.stdout)['dry_run'] is True
    assert rig.ledger() == (1, 1, 1)
    done = run()
    assert done.returncode == 0 and done.stderr == '' and json.loads(done.stdout)['remaining'] == 3
    for out in (dry, done):
        assert all(secret not in out.stdout + out.stderr for secret in (rig.p['owner_token'], rig.p['executor_token'], rig.editor_token))
    private(t, 'recipe', json.dumps(rig.recipe(2, 1, intent='budget-lower-cli')))
    refused = run()
    assert refused.returncode == 1 and refused.stdout == '' and refused.stderr == 'Effect control budget unavailable: below_current_cap\n'


STATE = {'control_id': 'a' * 64, 'consumer_id': 'b' * 64, 'object_revision': 4, 'budget_units': 10, 'valid_until': '2099-01-01T00:00:00+00:00',
         'ledger': {'control_revision': 4, 'budget_units': 10, 'reserved_units': 7, 'valid_until': '2099-01-01T00:00:00+00:00'}}


@pytest.mark.parametrize('change,recipe,reason', [
    ({}, {'from_revision': 4, 'budget_units': 6}, 'below_reserved'),
    ({}, {'from_revision': 4, 'budget_units': 8}, 'below_current_cap'),
    ({}, {'from_revision': 4, 'budget_units': 10}, 'unchanged'),
    ({}, {'from_revision': 3, 'budget_units': 20}, 'revision_moved'),
    ({'ledger': {**STATE['ledger'], 'control_revision': 3}}, {'from_revision': 4, 'budget_units': 20}, 'ledger_behind'),
    ({'ledger': None}, {'from_revision': 4, 'budget_units': 20}, 'control_not_configured'),
    ({'object_revision': 5, 'budget_units': 21}, {'from_revision': 4, 'budget_units': 20}, 'revision_moved'),
])
def test_plan_refusals(change, recipe, reason):
    with pytest.raises(EffectControlBudgetUnavailable) as refused:
        plan({**STATE, **change}, {'schema_version': '1.0', 'control_id': 'a' * 64, 'intent_id': 'intent-' + uuid.uuid4().hex, **recipe})
    assert refused.value.reason == reason


@pytest.mark.parametrize('change,phase', [({}, 'edit'), ({'object_revision': 5, 'budget_units': 20}, 'configure'),
                                          ({'object_revision': 5, 'budget_units': 20, 'ledger': {**STATE['ledger'], 'control_revision': 5, 'budget_units': 20}}, 'done')])
def test_plan_phases(change, phase):
    assert plan({**STATE, **change}, {'schema_version': '1.0', 'control_id': 'a' * 64, 'intent_id': 'intent-0001', 'from_revision': 4, 'budget_units': 20}) == phase
