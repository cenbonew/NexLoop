"""G-WEBCHAT-AUTH: nexloop_eios.webchat_consumers compiles the webchat consumer role and recipe into 0050 authority facts and the 0045
browser business application row (deterministic, consumer resources only, simulated_agent persona). The end-to-end path on a
clean catalog database is tests/test_assembly_dryrun_pg.py::test_the_webchat_consumer_opens_a_conversation_and_writes_after_the_consumer_role_is_applied."""
import copy
import json
from pathlib import Path

import pytest

from nexloop_eios import webchat_consumers as W

ROOT = Path(__file__).resolve().parents[1]
ROLE = json.loads((ROOT / 'deploy/authorization/webchat-consumer-role.v1.json').read_text())
TENANT = '00000000-0000-4000-8000-00000000000a'
RECIPE = {'schema_version': W.CONSUMERS_SCHEMA, 'tenant_id': TENANT, 'browser_application_id': 'stage-webchat',
          'caller_application_id': 'eios:application:nexloop-webchat',
          'consumers': [{'principal_id': 'consumer-principal-1', 'subject_id': 'consumer-subject-1', 'persona': 'simulated_agent'}]}


def test_the_committed_role_and_the_example_recipe_are_valid():
    W.validate_role(ROLE)
    assert set(ROLE['resources']) == set(W.CONSUMER_RESOURCES)
    example = json.loads((ROOT / 'deploy/examples/nx037/webchat-consumers.example.json').read_text())
    assert set(example) == set(RECIPE) and example['consumers'][0]['persona'] == 'simulated_agent'


def test_compilation_is_deterministic_and_covers_every_role_resource():
    first, second = W.compile_recipe(ROLE, RECIPE), W.compile_recipe(ROLE, copy.deepcopy(RECIPE))
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first['browser_business_applications'] == [{'application_id': 'stage-webchat', 'caller_application_id': 'eios:application:nexloop-webchat',
                                                        'application_version': '1', 'requested_scopes': ['action.execute', 'function.execute']}]
    facts = {(f['kind'], tuple(f['key'])): f['payload'] for f in first['authority_facts']}
    app = facts[('application', ('eios:application:nexloop-webchat', '1'))]
    assert sorted(r['resource_id'] for r in app['resources']) == ROLE['resources'] and app['mode'] == 'restricted'
    for rid in ROLE['resources']:
        grant = facts[('grants', ('consumer-principal-1', rid))]
        assert grant['subject_kind'] == 'human' and len(grant['grants']) == 1
        scope = facts[('scope', ('consumer-principal-1', rid, 'execute'))]
        assert scope['required_scopes'] == [rid.split(':')[1] + '.execute']
    assert facts[('actor', ('consumer-principal-1',))]['kind'] == 'human'


def test_a_revoked_consumer_gets_empty_grant_sets_only():
    recipe = copy.deepcopy(RECIPE)
    recipe['consumers'][0]['revoke'] = True
    facts = W.compile_recipe(ROLE, recipe)['authority_facts']
    kinds = {(f['kind'], f['key'][0]) for f in facts}
    assert ('actor', 'consumer-principal-1') not in kinds
    assert all(f['payload']['grants'] == [] for f in facts if f['kind'] == 'grants') and sum(f['kind'] == 'grants' for f in facts) == len(ROLE['resources'])


@pytest.mark.parametrize('change', [
    lambda r: r.update(resources=sorted(r['resources'] + ['eios:action:Budget.set:1'])),  # an owner Action is never a consumer's
    lambda r: r.update(resources=list(reversed(r['resources']))),
    lambda r: r.update(schema_version='other'),
])
def test_the_role_validator_refuses(change):
    role = copy.deepcopy(ROLE)
    change(role)
    with pytest.raises(W.WebchatConsumersRejected):
        W.validate_role(role)


@pytest.mark.parametrize('change', [
    lambda r: r['consumers'][0].update(persona='human'),  # v0.1: simulated_agent only
    lambda r: r['consumers'].append(dict(r['consumers'][0])),
    lambda r: r.update(caller_application_id='nexloop-webchat'),
    lambda r: r.update(tenant_id='not-a-uuid'),
    lambda r: r['consumers'][0].update(extra=1),
])
def test_the_recipe_validator_refuses(change):
    recipe = copy.deepcopy(RECIPE)
    change(recipe)
    with pytest.raises(W.WebchatConsumersRejected):
        W.validate_recipe(recipe)


def test_apply_without_a_connection_source_is_refused(tmp_path, capsys):
    role = tmp_path / 'role.json'
    role.write_text(json.dumps(ROLE))
    recipe = tmp_path / 'recipe.json'
    recipe.write_text(json.dumps(RECIPE))
    assert W.main(['--role', str(role), '--recipe', str(recipe), '--apply']) == 2
    assert W.main(['--role', str(role), '--recipe', str(recipe), '--compile-facts']) == 0
    assert W.main(['--role', str(role), '--recipe', str(recipe), '--apply', '--bootstrap-dsn-file', str(role), '--signing-key-file', str(role),
                   '--signing-key-id', 'k']) == 2  # the window is the scripts/deploy wrapper's
