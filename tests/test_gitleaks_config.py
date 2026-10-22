"""NX-060: the gitleaks configuration keeps the default rules and only narrow, fully anchored allowlists.

prepush-scan.sh gate 4 uses the configuration of the scanned commit, so loosening it would loosen its own gate; these
limits make any loosening a visible test change. The gate itself (blocking, fail-closed, sentinel) is exercised by
scripts/ci/test-prepush-scan.py.
"""
import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = tomllib.loads((ROOT / '.gitleaks.toml').read_text())


def test_default_rules_are_kept_and_none_is_disabled_or_overridden():
    assert CONFIG['extend'] == {'useDefault': True}
    assert 'rules' not in CONFIG and 'allowlist' not in CONFIG


def test_min_version_is_the_pinned_version():
    pinned = json.loads((ROOT / 'versions.lock.json').read_text())['release_tools']['gitleaks']['version']
    assert CONFIG['minVersion'] == pinned


def test_every_allowlist_is_one_rule_anchored_paths_and_anchored_values():
    allowlists = CONFIG['allowlists']
    assert 1 <= len(allowlists) <= 3
    for entry in allowlists:
        assert set(entry) == {'description', 'targetRules', 'condition', 'regexTarget', 'paths', 'regexes'}, entry
        assert entry['targetRules'] == ['generic-api-key'] and entry['condition'] == 'AND'
        assert entry['regexTarget'] in ('secret', 'match') and entry['description'].strip()
        for pattern in entry['paths'] + entry['regexes']:
            assert pattern.startswith('^') and pattern.endswith('$'), pattern
            re.compile(pattern)
            # No catch-all: every pattern needs literal text beyond anchors, classes and quantifiers.
            assert re.sub(r'\[[^\]]*\]|\\.|[\^$.*+?(){}|0-9,]', '', pattern), pattern
        for pattern in entry['paths']:
            assert not pattern.startswith('^.*') and '/.*' not in pattern, pattern
