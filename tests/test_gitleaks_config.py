"""NX-060: the gitleaks configuration keeps the default rules and only narrow, fully anchored allowlists.

The rules live in scripts/ci/check_gitleaks_config.py, which prepush-scan.sh gate 4 also runs on the trusted configuration
it is about to use; this test holds the repository's configuration to them and shows that each loosening is refused. The
gate itself (blocking, trust, fail-closed, sentinel, ignore files) is exercised by scripts/ci/test-prepush-scan.py.
"""
import copy
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/ci'))
import check_gitleaks_config as checker  # noqa: E402

CONFIG = tomllib.loads((ROOT / '.gitleaks.toml').read_text())
PINNED = json.loads((ROOT / 'versions.lock.json').read_text())['release_tools']['gitleaks']['version']


def test_repository_configuration_conforms():
    assert checker.problems(CONFIG, PINNED) == []
    run = subprocess.run([sys.executable, str(ROOT / 'scripts/ci/check_gitleaks_config.py'), str(ROOT / '.gitleaks.toml'),
                          str(ROOT / 'versions.lock.json')], capture_output=True, text=True)
    assert run.returncode == 0 and run.stdout == ''


def loosen(change):
    config = copy.deepcopy(CONFIG)
    change(config)
    return config


@pytest.mark.parametrize('change', [
    lambda c: c['extend'].update(useDefault=False),
    lambda c: c['extend'].update(disabledRules=['aws-access-token']),
    lambda c: c.update(rules=[{'id': 'aws-access-token', 'regex': 'never'}]),
    lambda c: c.update(allowlist={'regexes': ['.']}),
    lambda c: c.update(minVersion='8.0.0'),
    lambda c: c['allowlists'].append(copy.deepcopy(c['allowlists'][0])),
    lambda c: c['allowlists'][0].update(targetRules=['github-pat']),
    lambda c: c['allowlists'][0].update(condition='OR'),
    lambda c: c['allowlists'][1].update(regexes=['.']),
    lambda c: c['allowlists'][1].update(regexes=['^.*$']),
    lambda c: c['allowlists'][1].update(paths=['^.*$']),
    lambda c: c['allowlists'][1].update(paths=['^tests/.*$']),
    lambda c: c['allowlists'][2].update(regexes=['nexloop-cjk-bigram-v1']),
    lambda c: c['allowlists'][0].update(stopwords=['key']),
    lambda c: c['allowlists'][0].pop('paths'),
], ids=['default-off', 'disabled-rule', 'rule-override', 'global-allowlist', 'min-version', 'fourth-allowlist',
        'other-rule', 'condition-or', 'dot-value', 'catch-all-value', 'catch-all-path', 'any-directory', 'unanchored',
        'stopwords', 'value-only'])
def test_every_loosening_is_refused(change):
    assert checker.problems(loosen(change), PINNED)
