#!/usr/bin/env python3
"""Fail closed on publication paths, LAN literals and credential assignments.

Staged content is read from Git's index, never substituted by worktree bytes.
"""
import re
import subprocess
import sys
from pathlib import Path


def paths(command):
    return [name for name in subprocess.check_output(command).decode().split('\0') if name]


def violation(name, raw):
    path = Path(name)
    if (any(part in path.parts for part in ('local-only','private','secrets','runtime-data','backups'))
            or name.startswith('docs/tmp/') or path.name.startswith('NexLoop_完整交接文档_')
            or (path.name.startswith('.env') and path.name != '.env.example')):
        return 'private path'
    if b'\0' in raw:
        return None
    content = raw.decode(errors='replace')
    if re.search(r'(?<![\d.])192\.168\.\d{1,3}\.\d{1,3}(?![\d.])',content):
        return 'private address'
    if re.search(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',content):
        return 'private key'
    if re.search(r'(?m)^MODEL_API_KEY=\S+',content):
        return 'model credential'
    return None


def main():
    staged = paths(['git','diff','--cached','--name-only','--diff-filter=ACMR','-z'])
    candidates = [] if '--staged' in sys.argv else paths(['git','ls-files','-z','--cached','--others','--exclude-standard'])
    errors = []
    for name in candidates:
        path = Path(name)
        if not path.is_file():
            continue
        reason = violation(name,path.read_bytes())
        if reason:
            errors.append((name,'worktree',reason))
    for name in staged:
        # Argument list + colon object syntax: no shell interpolation or option injection.
        raw = subprocess.check_output(['git','show',f':{name}'])
        reason = violation(name,raw)
        if reason:
            errors.append((name,'index',reason))
    for name,source,reason in errors:
        print(f'{name} ({source}): {reason}')
    print(f'Publication scan: {len(set(candidates+staged))} paths, {len(staged)} index blobs, {len(errors)} violations')
    return bool(errors)


if __name__ == '__main__':
    raise SystemExit(main())
