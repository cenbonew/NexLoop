#!/usr/bin/env python3
"""Fail closed on publication paths, LAN literals and credential assignments.

Staged content is read from Git's index, never substituted by worktree bytes.
NX-036 adds two release modes: `--tree <ref>` scans every blob of a commit's whole tree (not just the diff), and
`--archive <file.tar>` scans every member of a source archive as it would be published (e.g. `git archive` output).
"""
import re
import tarfile
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


def tree_blobs(ref):
    """(path, bytes) for every blob in the commit's tree; submodules and symlinks are reported, not followed."""
    listing = subprocess.check_output(['git','ls-tree','-r','-z','--full-tree',ref]).decode().split('\0')
    for entry in filter(None, listing):
        meta, _, name = entry.partition('\t')
        mode, kind, obj = meta.split()
        if kind == 'commit':
            yield name, None, 'submodule in a public tree'
        elif mode == '120000':
            yield name, None, 'symbolic link in a public tree'
        else:
            yield name, subprocess.check_output(['git','cat-file','blob',obj]), None


def archive_members(path):
    with tarfile.open(path) as archive:
        for member in archive.getmembers():
            if member.isdir() or member.name == 'pax_global_header':
                continue
            if not member.isfile():
                yield member.name, None, 'non-regular archive member'
                continue
            yield member.name, archive.extractfile(member).read(), None


def scan(entries, label):
    errors, count = [], 0
    for name, raw, problem in entries:
        count += 1
        reason = problem or violation(name, raw)
        if reason:
            errors.append((name, reason))
    for name, reason in errors:
        print(f'{name} ({label}): {reason}')
    print(f'Publication scan ({label}): {count} entries, {len(errors)} violations')
    return bool(errors)


def main():
    if '--tree' in sys.argv or '--archive' in sys.argv:
        if len(sys.argv) != 3:
            print('usage: check_publication.py [--staged] | --tree <ref> | --archive <file.tar>')
            return 2
        if sys.argv[1] == '--tree':
            return scan(tree_blobs(sys.argv[2]), 'tree')
        return scan(archive_members(sys.argv[2]), 'archive')
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
