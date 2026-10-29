"""G-CONTEXT-STRATEGY: nexloop_eios.context_strategy_setup's recipe and strategy selection (the owner's publication itself runs
end to end in tests/test_assembly_dryrun_pg.py::test_the_owner_publishes_the_relay_strategy_and_the_relay_issues_the_run)."""
import copy
import json
from pathlib import Path

import pytest

from nexloop_eios import context_strategy_setup as C

ROOT = Path(__file__).resolve().parents[1]
STRATEGIES = ROOT / 'deploy/configuration/context-strategies.v1.json'
RECIPE = {'schema_version': C.RECIPE_SCHEMA, 'tenant_id': '00000000-0000-4000-8000-00000000000a', 'workbench_application_id': 'stage-workbench',
          'strategy_id': 'recent_plus_required', 'version': 1, 'request_id': 'context-strategy-recent-plus-required-1'}


def test_the_example_recipe_selects_the_relay_strategy():
    example = json.loads((ROOT / 'deploy/examples/nx037/context-strategy-setup.example.json').read_text())
    assert set(example) == set(RECIPE) and example['strategy_id'] == 'recent_plus_required'
    definition = C.strategy_definition(STRATEGIES, C.validate_recipe(RECIPE))
    assert definition['strategy_id'] == 'recent_plus_required' and definition['version'] == 1
    # The relay's compose command names the same strategy (deployment hard constraint 3).
    import re
    import yaml
    services = yaml.safe_load((ROOT / 'deploy/stage/compose.app.example.yaml').read_text())['services']
    env = dict(line.split('=', 1) for line in (ROOT / 'deploy/stage/compose.app.env.example').read_text().splitlines()
               if line.startswith('RELAY_CONTEXT_STRATEGY='))
    for name in ('message-relay', 'message-relay-2'):
        args = [str(a) for a in services[name]['command']]
        raw = args[args.index('--context-strategy') + 1]
        assert re.sub(r'\$\{RELAY_CONTEXT_STRATEGY:-([^}]*)\}', r'\1', raw) == 'recent_plus_required'
        assert re.sub(r'\$\{RELAY_CONTEXT_STRATEGY:-([^}]*)\}', env['RELAY_CONTEXT_STRATEGY'], raw) == 'recent_plus_required'


@pytest.mark.parametrize('change', [lambda r: r.update(version=0), lambda r: r.update(tenant_id='x'), lambda r: r.update(extra=1),
                                    lambda r: r.update(request_id='')])
def test_the_recipe_validator_refuses(change):
    recipe = copy.deepcopy(RECIPE)
    change(recipe)
    with pytest.raises(C.ContextStrategySetupRejected):
        C.validate_recipe(recipe)


def test_an_undeclared_strategy_version_is_refused():
    with pytest.raises(C.ContextStrategySetupRejected, match='not declared'):
        C.strategy_definition(STRATEGIES, {**RECIPE, 'version': 99})


def test_the_cli_refuses_without_printing_credentials(tmp_path, capsys):
    recipe = tmp_path / 'recipe.json'
    recipe.write_text(json.dumps({**RECIPE, 'version': 99}))
    secret = tmp_path / 'password'
    secret.write_text('synthetic-owner-password-xyz')
    argv = ['--recipe', str(recipe), '--strategies-file', str(STRATEGIES), '--signing-key-id', 'k', '--browser-origin', 'https://127.0.0.1:8443']
    for name in ('--database-url-file', '--identity-database-url-file', '--signing-key-file', '--artifact-root', '--username-file', '--browser-rate-key-file'):
        argv += [name, str(tmp_path / 'missing')]
    assert C.main([*argv, '--password-file', str(secret)]) == 2
    out = capsys.readouterr()
    assert 'synthetic-owner-password-xyz' not in out.out + out.err and 'not declared' in out.err
