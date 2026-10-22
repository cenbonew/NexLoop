#!/usr/bin/env python3
"""NX-036 §5: the whole-history audit before the first public release.

    python scripts/release/history_audit.py [<ref>] [--all-refs] [--json FILE] [--markdown FILE]

Walks every commit reachable from <ref> (default HEAD; --all-refs: every local branch and tag), including each parent side of
a merge commit, and checks:
- every blob a commit adds or modifies (deduplicated by blob and path) with the rules of scripts/check_publication.py
  (`violation`: private paths, private addresses, private keys, model credentials; a blob with a NUL byte is binary and only its
  path is checked, exactly as there);
- every such blob and every commit message with the rules of scripts/ci/prepush-scan.sh: the restricted identifiers (one
  extended regular expression per line in local-only/restricted-identifiers.txt of the main checkout, or
  NEXLOOP_RESTRICTED_IDENTIFIERS_FILE; missing or empty fails, as there) and the credential-value expression, which is read from
  prepush-scan.sh itself so that the two never diverge.

The report holds only the category, the rule, the path (or "commit message"), the commits that introduced it, the first and
last of them, and whether the path's blob at <ref> still hits. It never repeats what matched. Keep it in the private evidence
directory; it is not committed.

Categories (NX-036 D9): credential, private_key, restricted_network_identifier must be rotated or revoked; host_alias,
local_path, hardware and other are accepted in history. A restricted identifier line is classified by an optional section
header in the list (`# [host_alias]`, `# [local_path]`, `# [hardware]`, `# [restricted_network_identifier]`) and otherwise by its
shape (an address or key fingerprint: restricted_network_identifier; containing '/': local_path; anything else: host_alias).

Exit: 0 no hits; 1 only accepted categories; 2 a category that must be rotated; 3 input error.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from check_publication import violation  # noqa: E402  the same rules, not a copy

ROTATE = ('credential', 'private_key', 'restricted_network_identifier')
CATEGORIES = ROTATE + ('host_alias', 'local_path', 'hardware', 'other')
PUBLICATION_CATEGORY = {'private address': 'restricted_network_identifier', 'private key': 'private_key', 'model credential': 'credential'}
MESSAGE = 'commit message'


class InputError(Exception):
    pass


def git(*args, cwd=None, binary=False):
    out = subprocess.run(['git', *args], cwd=cwd, capture_output=True, check=False)
    if out.returncode != 0:
        raise InputError('git ' + args[0])
    return out.stdout if binary else out.stdout.decode()


def credential_expression(script=ROOT / 'scripts/ci/prepush-scan.sh'):
    """The added-line credential expression of prepush-scan.sh (`grep -E '^\\+.*(<expression>)'`, or -qE)."""
    match = re.search(r"grep -q?E '\^\\\+\.\*(\(.*\))'", Path(script).read_text())
    if not match:
        raise InputError('credential rule not found in prepush-scan.sh')
    return re.compile(match.group(1).encode())


def identifier_list(cwd):
    path = os.environ.get('NEXLOOP_RESTRICTED_IDENTIFIERS_FILE')
    if not path:
        common = Path(git('rev-parse', '--path-format=absolute', '--git-common-dir', cwd=cwd).strip())
        path = common.parent / 'local-only' / 'restricted-identifiers.txt'
    try:
        lines = Path(path).read_text(encoding='utf-8').splitlines()
    except OSError:
        raise InputError('restricted identifier list missing') from None
    rules, section = [], None
    for line in lines:
        header = re.fullmatch(r'\s*#\s*\[([a-z_]+)\]\s*', line)
        if header:
            if header.group(1) not in CATEGORIES:
                raise InputError('unknown section in restricted identifier list')
            section = header.group(1)
            continue
        if re.fullmatch(r'\s*(#.*)?', line):
            continue
        try:
            expression = re.compile(line.encode())
        except re.error:
            raise InputError(f'restricted identifier line {len(rules) + 1} is not a valid expression') from None
        rules.append((f'identifier:{len(rules) + 1}', section or _shape(line), expression))
    if not rules:
        raise InputError('restricted identifier list empty')
    return rules


def _shape(line):
    if re.search(r'\d{1,3}\\?\.\d{1,3}|SHA256:|[0-9a-fA-F]{2}:[0-9a-fA-F]{2}:', line):
        return 'restricted_network_identifier'
    return 'local_path' if '/' in line else 'host_alias'


def content_hits(path, raw, identifiers, credential):
    """(category, rule) for one blob at one path; check_publication first, then the prepush rules on text blobs."""
    hits = set()
    reason = violation(path, raw)
    if reason:
        hits.add((PUBLICATION_CATEGORY.get(reason, 'other'), 'publication:' + reason.replace(' ', '_')))
    if b'\0' in raw:
        return hits  # binary: check_publication checks only its path
    if credential.search(raw):
        hits.add(('credential', 'credential_value'))
    for rule, category, expression in identifiers:
        if expression.search(raw):
            hits.add((category, rule))
    return hits


def commits(ref, all_refs, cwd):
    args = ['rev-list', '--topo-order', '--reverse'] + (['--branches', '--tags'] if all_refs else [ref])
    return git(*args, cwd=cwd).split()


def changed_blobs(commit, cwd):
    """(path, blob) added or modified by the commit against each parent (the root commit against the empty tree)."""
    raw = git('diff-tree', '-r', '-m', '--root', '--no-renames', '--no-commit-id', '-z', commit, cwd=cwd)
    fields = raw.split('\0')
    i = 0
    while i < len(fields) - 1:
        meta = fields[i]
        if not meta.startswith(':'):
            i += 1
            continue
        _, new_mode, _, new_blob, status = meta[1:].split()
        path = fields[i + 1]
        i += 2
        if status[0] in 'AMT' and new_mode != '160000':
            yield path, new_blob, new_mode


class Blobs:
    def __init__(self, cwd):
        self.process = subprocess.Popen(['git', 'cat-file', '--batch'], cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def read(self, sha):
        self.process.stdin.write(sha.encode() + b'\n')
        self.process.stdin.flush()
        header = self.process.stdout.readline().split()
        if len(header) != 3:
            raise InputError('object missing')
        data = self.process.stdout.read(int(header[2]))
        self.process.stdout.read(1)
        return data

    def close(self):
        self.process.stdin.close()
        self.process.wait()


def audit(ref='HEAD', *, all_refs=False, cwd=None):
    git('rev-parse', '--verify', ref + '^{commit}', cwd=cwd)
    identifiers, credential = identifier_list(cwd), credential_expression()
    findings, seen = {}, {}

    def record(category, rule, path, commit):
        entry = findings.setdefault((category, rule, path), {'category': category, 'rule': rule, 'path': path, 'commits': []})
        if commit not in entry['commits']:
            entry['commits'].append(commit)

    blobs = Blobs(cwd)
    try:
        history = commits(ref, all_refs, cwd)
        for commit in history:
            message = git('cat-file', 'commit', commit, cwd=cwd, binary=True).split(b'\n\n', 1)[-1]
            for category, rule in content_hits('', message, identifiers, credential):
                record(category, rule, MESSAGE, commit)
            for path, blob, mode in changed_blobs(commit, cwd):
                key = (path, blob)
                if key not in seen:
                    seen[key] = content_hits(path, b'' if mode == '120000' else blobs.read(blob), identifiers, credential)
                for category, rule in seen[key]:
                    record(category, rule, path, commit)
    finally:
        blobs.close()
    current = {}
    for line in filter(None, git('ls-tree', '-r', '-z', '--full-tree', ref, cwd=cwd).split('\0')):
        meta, _, path = line.partition('\t')
        current[path] = meta.split()[2]
    out = []
    for (category, rule, path), entry in sorted(findings.items()):
        in_tree = path != MESSAGE and (path, current.get(path)) in seen and (category, rule) in seen[(path, current[path])]
        out.append({**entry, 'first_commit': entry['commits'][0], 'last_commit': entry['commits'][-1], 'in_current_tree': in_tree})
    return {'ref': git('rev-parse', ref, cwd=cwd).strip(), 'all_refs': all_refs, 'commits_scanned': len(history),
            'blobs_scanned': len(seen), 'findings': out,
            'by_category': {c: sum(1 for f in out if f['category'] == c) for c in CATEGORIES},
            'rotate_required': sorted({f['category'] for f in out if f['category'] in ROTATE})}


def markdown(report):
    lines = [f"# History audit of {report['ref'][:12]}", '',
             f"{report['commits_scanned']} commits, {report['blobs_scanned']} blobs. Must rotate or revoke (D9): "
             + (', '.join(report['rotate_required']) or 'none') + '.', '', '| category | findings |', '|---|---|']
    lines += [f'| {c} | {n} |' for c, n in report['by_category'].items()]
    lines += ['', '| category | rule | path | first | last | in current tree |', '|---|---|---|---|---|---|']
    lines += [f"| {f['category']} | {f['rule']} | {f['path']} | {f['first_commit'][:12]} | {f['last_commit'][:12]} | {'yes' if f['in_current_tree'] else 'no'} |"
              for f in report['findings']]
    return '\n'.join(lines) + '\n'


def exit_code(report):
    return 2 if report['rotate_required'] else 1 if report['findings'] else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='NX-036 whole-history audit (report holds no matched content)')
    parser.add_argument('ref', nargs='?', default='HEAD')
    parser.add_argument('--all-refs', action='store_true')
    parser.add_argument('--json')
    parser.add_argument('--markdown')
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 3
    if any(path and os.path.lexists(path) for path in (args.json, args.markdown)):
        print(json.dumps({'ok': False, 'detail': 'report file exists'}))
        return 3
    try:
        report = audit(args.ref, all_refs=args.all_refs)
    except InputError as error:
        print(json.dumps({'ok': False, 'detail': str(error)}))
        return 3
    for path, text in ((args.json, json.dumps(report, indent=1, sort_keys=True) + '\n'), (args.markdown, markdown(report))):
        if path:
            with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8') as handle:
                handle.write(text)
    print(json.dumps({'ok': True, 'commits': report['commits_scanned'], 'findings': len(report['findings']),
                      'by_category': report['by_category'], 'rotate_required': report['rotate_required']}))
    return exit_code(report)


if __name__ == '__main__':
    raise SystemExit(main())
