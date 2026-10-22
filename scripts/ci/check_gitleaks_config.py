#!/usr/bin/env python3
"""NX-060: structure rules for the gitleaks configuration, run by prepush-scan.sh gate 4 on the configuration it is about to
use and by tests/test_gitleaks_config.py on the repository's own.

    check_gitleaks_config.py <config.toml> <versions.lock.json>

The default rules are kept and none is overridden or disabled; at most three allowlists, each for generic-api-key only,
condition AND, with fully anchored, non-catch-all path and value patterns; minVersion is the pinned gitleaks version.
Prints one line naming the first violated rule (never a pattern or a value) and exits 1; exits 0 when every rule holds.
Standard library only.
"""
import json
import re
import sys
import tomllib

ALLOWLIST_KEYS = {'description', 'targetRules', 'condition', 'regexTarget', 'paths', 'regexes'}
MAX_ALLOWLISTS = 3


def _catch_all(pattern):
    # Literal text must remain once anchors, classes, escapes, groups and quantifiers are removed.
    return not re.sub(r'\[[^\]]*\]|\\.|[\^$.*+?(){}|0-9,]', '', pattern)


def problems(config, pinned_version):
    """The violated rules of a parsed configuration, in order (empty when it conforms)."""
    found = []
    if config.get('extend') != {'useDefault': True}:
        found.append('extend must be exactly useDefault = true')
    for key in ('rules', 'allowlist'):
        if key in config:
            found.append(f'no top-level {key} may be defined')
    if config.get('minVersion') != pinned_version:
        found.append('minVersion must be the pinned gitleaks version')
    unknown = set(config) - {'title', 'minVersion', 'extend', 'allowlists'}
    if unknown:
        found.append('unknown top-level keys')
    allowlists = config.get('allowlists') or []
    if not isinstance(allowlists, list) or len(allowlists) > MAX_ALLOWLISTS:
        found.append(f'at most {MAX_ALLOWLISTS} allowlists')
        return found
    for index, entry in enumerate(allowlists, 1):
        where = f'allowlist {index}'
        if not isinstance(entry, dict) or set(entry) != ALLOWLIST_KEYS:
            found.append(f'{where}: keys must be exactly ' + ', '.join(sorted(ALLOWLIST_KEYS)))
            continue
        if entry['targetRules'] != ['generic-api-key'] or entry['condition'] != 'AND':
            found.append(f'{where}: only generic-api-key, condition AND')
        if entry['regexTarget'] not in ('secret', 'match') or not str(entry['description']).strip():
            found.append(f'{where}: regexTarget secret or match, non-empty description')
        paths, regexes = entry['paths'], entry['regexes']
        if not paths or not regexes or not all(isinstance(p, str) for p in paths + regexes):
            found.append(f'{where}: non-empty paths and regexes')
            continue
        for pattern in paths + regexes:
            try:
                re.compile(pattern)
            except re.error:
                found.append(f'{where}: invalid pattern')
                continue
            if not (pattern.startswith('^') and pattern.endswith('$')) or _catch_all(pattern):
                found.append(f'{where}: every pattern anchored at both ends and not a catch-all')
        if any(p.startswith('^.*') or '/.*' in p for p in paths):
            found.append(f'{where}: paths may not match any directory')
    return found


def main(argv):
    if len(argv) != 3:
        print('usage: check_gitleaks_config.py <config.toml> <versions.lock.json>')
        return 2
    try:
        config = tomllib.loads(open(argv[1], encoding='utf-8').read())
        pinned = json.load(open(argv[2], encoding='utf-8'))['release_tools']['gitleaks']['version']
    except (OSError, ValueError, KeyError, tomllib.TOMLDecodeError):
        print('configuration or pin unreadable')
        return 1
    found = problems(config, pinned)
    if found:
        print(found[0])
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
