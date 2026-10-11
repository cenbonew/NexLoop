#!/usr/bin/env python3
"""NX-036 §4.1 / AT-054 (static half): no hosted workflow may run unreviewed code with secrets or write access.

Every file under .github/workflows (and any action.yml under .github) is checked: no `pull_request_target` trigger,
no reference to `secrets.` (including `secrets: inherit`), and `permissions` declared at the top level as read-only
(`read-all`, `{}` or a mapping whose values are only `read` or `none`); job-level permissions follow the same rule.
Today the repository has no workflow, which passes. The network half of AT-054 is NX-032's isolation probe evidence.
Exit 0 ok, 1 problems, 2 input error.
"""
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _triggers(document):
    # YAML 1.1 reads a bare `on:` key as boolean True.
    value = document.get('on', document.get(True))
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {str(item) for item in value}
    if isinstance(value, dict):
        return {str(key) for key in value}
    return set()


def _read_only(permissions):
    if permissions in ('read-all', {}):
        return True
    return isinstance(permissions, dict) and all(value in ('read', 'none') for value in permissions.values())


def problems_in(name, text):
    problems = []
    if re.search(r'secrets\s*[.\[:]|secrets\s*:\s*inherit', text):
        problems.append(f'{name}: references secrets')
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        return problems + [f'{name}: not valid YAML']
    if not isinstance(document, dict):
        return problems + [f'{name}: not a workflow mapping']
    if name.endswith(('action.yml', 'action.yaml')) and 'jobs' not in document:
        return problems
    if 'pull_request_target' in _triggers(document) or 'pull_request_target' in text:
        problems.append(f'{name}: uses pull_request_target')
    if 'permissions' not in document or not _read_only(document['permissions']):
        problems.append(f'{name}: top-level permissions missing or not read-only')
    for job, body in (document.get('jobs') or {}).items():
        if isinstance(body, dict) and 'permissions' in body and not _read_only(body['permissions']):
            problems.append(f'{name}: job {job} permissions not read-only')
    return problems


def check(root=ROOT):
    github = root / '.github'
    files = sorted(p for p in github.rglob('*') if p.is_file() and p.suffix in ('.yml', '.yaml')) if github.is_dir() else []
    problems = []
    for path in files:
        relative = path.relative_to(root).as_posix()
        if relative.startswith('.github/workflows/') or path.name in ('action.yml', 'action.yaml'):
            problems += problems_in(relative, path.read_text())
    return len(files), problems


def main():
    try:
        count, problems = check()
    except (OSError, UnicodeDecodeError) as error:
        print(json.dumps({'ok': False, 'error': type(error).__name__}))
        return 2
    print(json.dumps({'ok': not problems, 'workflow_files': count, 'problems': problems}))
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
