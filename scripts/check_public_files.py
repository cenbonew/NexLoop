#!/usr/bin/env python3
"""NX-036 §4.3: the public files exist and agree with deploy/compliance/public-release.v1.json.

Default (local CI) mode checks the files that already belong to the repository; `--public` is the public release gate:
every file listed for a public release exists, the README pair links to each other, no document still carries the
security contact placeholder, and the security contact is filled in. Exit 0 ok, 1 problems, 2 input error.
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = 'deploy/compliance/public-release.v1.json'
MIT_FIRST_LINE = 'MIT License'


def load(root):
    config = json.loads((root / CONFIG).read_text())
    if config.get('schema') != 'nexloop-public-release/1' or set(config.get('required_files', {})) != {'ci', 'public'}:
        raise ValueError('public release config shape')
    return config


def expected_phrases(config):
    """Values that the documents must state, so a decision change shows up as a failing check until both agree."""
    targets = config['response_targets_days']
    coc = config['code_of_conduct']
    return {'response_targets_days': [f"{targets['acknowledge_working_days']} 个工作日", f"{targets['assess_working_days']} 个工作日",
                                      f"{targets['fix_critical_days']} 天", f"{targets['fix_high_days']} 天"],
            'supported_releases': ['最新的一个发布' if config['supported_releases'] == 'latest' else str(config['supported_releases'])],
            'code_of_conduct': [f"{coc['name']} {coc['version']}", coc['url']]}


def check(root=ROOT, public=False):
    config = load(root)
    problems = []
    required = config['required_files']['public' if public else 'ci']
    for name in required:
        if not (root / name).is_file():
            problems.append(f'{name}: missing')
    license_file = root / 'LICENSE'
    if license_file.is_file() and license_file.read_text().splitlines()[:1] != [MIT_FIRST_LINE]:
        problems.append('LICENSE: not the MIT license')
    phrases = expected_phrases(config)
    for name, keys in config['values_in'].items():
        path = root / name
        if not path.is_file():
            continue
        text = path.read_text()
        for key in keys:
            for phrase in phrases[key]:
                if phrase not in text:
                    problems.append(f'{name}: does not state {key} ({phrase})')
    if config.get('contribution_signoff') == 'dco' and (root / 'CONTRIBUTING.md').is_file():
        text = (root / 'CONTRIBUTING.md').read_text()
        if 'Signed-off-by' not in text or 'scripts/check_signoff.py' not in text:
            problems.append('CONTRIBUTING.md: does not describe the DCO sign-off')
    if public:
        first, second = config['readme_pair']
        for source, target in ((first, second), (second, first)):
            path = root / source
            if path.is_file() and not re.search(r'\]\((\./)?' + re.escape(target) + r'\)', path.read_text()):
                problems.append(f'{source}: does not link to {target}')
        for name in config['placeholder_free']:
            path = root / name
            if path.is_file() and config['placeholder'] in path.read_text():
                problems.append(f'{name}: security contact placeholder not filled in')
        if not (type(config['security_contact']) is str and config['security_contact'].strip()):
            problems.append('security_contact: not set (no formal response channel; D2)')
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--public', action='store_true')
    args = parser.parse_args(argv)
    try:
        problems = check(public=args.public)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({'ok': False, 'error': type(error).__name__}))
        return 2
    print(json.dumps({'ok': not problems, 'mode': 'public' if args.public else 'ci', 'problems': problems}, ensure_ascii=False))
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
