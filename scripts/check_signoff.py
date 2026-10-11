#!/usr/bin/env python3
"""NX-036 D1 (provisional: DCO): every non-merge commit in <base>..<head> carries a `Signed-off-by:` trailer that
matches its author name and email (Developer Certificate of Origin 1.1). Maintainers run it before merging an external
change. Exit 0 ok, 1 unsigned commits, 2 input error.
"""
import json
import subprocess
import sys

FIELD = '\x1f'
RECORD = '\x1e'


def commits(base, head, root=None):
    for ref in (base, head):
        if ref.startswith('-'):
            raise ValueError('ref')
    output = subprocess.run(['git', 'log', '--no-merges', f'--format=%H{FIELD}%an{FIELD}%ae{FIELD}%(trailers:key=Signed-off-by,valueonly,separator=%x1d){RECORD}',
                             f'{base}..{head}', '--'], cwd=root, capture_output=True, text=True, check=True).stdout
    for record in filter(str.strip, output.split(RECORD)):
        sha, name, email, trailers = record.strip('\n').split(FIELD)
        yield sha, name, email, [t.strip() for t in trailers.split('\x1d') if t.strip()]


def check(base, head, root=None):
    problems, count = [], 0
    for sha, name, email, trailers in commits(base, head, root):
        count += 1
        if f'{name} <{email}>' not in trailers:
            problems.append(f'{sha[:12]}: no Signed-off-by matching the author')
    return count, problems


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print('usage: check_signoff.py <base> <head>')
        return 2
    try:
        count, problems = check(*argv)
    except (ValueError, subprocess.CalledProcessError):
        print(json.dumps({'ok': False, 'error': 'invalid range'}))
        return 2
    print(json.dumps({'ok': not problems, 'commits': count, 'problems': problems}))
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
