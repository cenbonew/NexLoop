#!/usr/bin/env python3
"""NX-036 license compliance (docs/implementation/NX-036-design.md §2; ADR-026: NexLoop is MIT).

Lists every locked Python (uv.lock) and Node (pnpm-lock.yaml) package with its license, checks it against the versioned policy
deploy/compliance/license-policy.v1.json, checks the vendored sources (PROVENANCE hashes, NOTICE, licenses/), the project
LICENSE and the repository's own assets, and generates THIRD_PARTY_NOTICES.md. Licenses are never guessed: a package whose
installed metadata is missing or ambiguous needs a clarification record with the hash of the license file it was read from;
a package that is only installed on another platform needs an off-platform record bound to the exact locked artifact.

    check_licenses.py [--check]            check (default); runs offline after the dependency phase
    check_licenses.py --write-notices      regenerate THIRD_PARTY_NOTICES.md
    check_licenses.py --report <path>      write the inventory as JSON (release gate, SBOM cross-check)
    check_licenses.py --record-off-platform  (network) download the exact locked artifacts that are not installed here and
                                             print their records for review; never edits the policy itself

Exit codes: 0 ok, 1 a policy violation, 2 input error, 3 inventory incomplete (never a pass).
"""
import argparse
import base64
import hashlib
import importlib.metadata as metadata
import io
import json
import re
import subprocess
import sys
import tarfile
import tomllib
import urllib.request
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / 'deploy/compliance/license-policy.v1.json'
NOTICES = ROOT / 'THIRD_PARTY_NOTICES.md'
ASSET_SUFFIXES = {'.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico', '.webp', '.woff', '.woff2', '.ttf', '.otf', '.eot', '.mp3', '.mp4', '.wav'}
_RANK = {'allowed': 2, 'conditional': 1, 'denied': 0}


class Incomplete(Exception):
    """The inventory cannot be completed (exit 3)."""


class InputError(Exception):
    """The policy or a lock file is malformed (exit 2)."""


# ---- policy -------------------------------------------------------------------------------------------------------------------
POLICY_KEYS = {'schema', 'version', 'decision', 'project_license', 'allowed', 'conditional_licenses', 'denied', 'normalize',
               'conditional', 'clarifications', 'off_platform', 'vendored', 'assets', 'own_python', 'own_node'}


def load_policy(path=POLICY):
    value = json.loads(Path(path).read_text())
    if type(value) is not dict or set(value) != POLICY_KEYS or value['schema'] != 'nexloop-license-policy/1' or type(value['version']) is not int:
        raise InputError('license policy shape')
    for key in ('allowed', 'conditional_licenses', 'denied'):
        if type(value[key]) is not list or any(type(x) is not str for x in value[key]):
            raise InputError('license policy ' + key)
    if set(value['allowed']) & (set(value['conditional_licenses']) | set(value['denied'])) or set(value['conditional_licenses']) & set(value['denied']):
        raise InputError('a license is in more than one class')
    return value


# ---- SPDX expressions ---------------------------------------------------------------------------------------------------------
def _tokens(expression):
    return re.findall(r'\(|\)|[A-Za-z0-9.+:-]+', expression)


def parse(expression):
    """A small SPDX expression parser: identifiers, WITH, AND (binds tighter), OR, parentheses."""
    tokens = _tokens(expression)
    if not tokens or ''.join(tokens).replace(' ', '') != re.sub(r'\s+', '', expression):
        raise ValueError('expression')
    position = 0

    def peek():
        return tokens[position] if position < len(tokens) else None

    def take():
        nonlocal position
        if position >= len(tokens):
            raise ValueError('expression')
        position += 1
        return tokens[position - 1]

    def atom():
        if peek() == '(':
            take()
            node = disjunction()
            if take() != ')':
                raise ValueError('expression')
            return node
        name = take()
        if name in ('AND', 'OR', 'WITH', ')') or name is None:
            raise ValueError('expression')
        if peek() == 'WITH':
            take()
            return ('id', name + ' WITH ' + take())
        return ('id', name)

    def conjunction():
        node = atom()
        while peek() == 'AND':
            take()
            node = ('and', node, atom())
        return node

    def disjunction():
        node = conjunction()
        while peek() == 'OR':
            take()
            node = ('or', node, conjunction())
        return node

    tree = disjunction()
    if position != len(tokens):
        raise ValueError('expression')
    return tree


def classify(tree, policy):
    """allowed / conditional / denied for a parsed expression (OR: the best option, AND: the worst part)."""
    kind = tree[0]
    if kind == 'id':
        name = tree[1]
        if name in policy['allowed']:
            return 'allowed'
        if name in policy['conditional_licenses']:
            return 'conditional'
        return 'denied'  # denied or not listed at all: never a guess
    left, right = classify(tree[1], policy), classify(tree[2], policy)
    return (max if kind == 'or' else min)((left, right), key=_RANK.get)


def normalize(raw, policy):
    """Installed metadata → SPDX expression, or None when it is missing or ambiguous (a clarification record is then required)."""
    if raw is None:
        return None
    if type(raw) is list:
        mapped = [policy['normalize'].get(item, item) for item in raw]
        if not mapped or any(item is None or item not in policy['allowed'] + policy['conditional_licenses'] + policy['denied'] for item in mapped):
            return None
        return ' OR '.join(sorted(set(mapped))) if len(set(mapped)) > 1 else mapped[0]
    raw = policy['normalize'].get(raw, raw)
    if raw is None:
        return None
    try:
        parse(raw)
    except (ValueError, TypeError):
        return None
    return raw


# ---- Python (uv.lock) ----------------------------------------------------------------------------------------------------------
def _python_artifact(package):
    wheels = package.get('wheels') or []
    if wheels:
        return wheels[0]['hash']
    return (package.get('sdist') or {}).get('hash')


def python_inventory(root, policy, lookup=None):
    lock = tomllib.loads((root / 'uv.lock').read_text())
    packages = {p['name']: p for p in lock['package']}
    own = set(policy['own_python'])
    runtime, stack = set(), [name for name in own if name != 'nexloop']
    while stack:
        name = stack.pop()
        if name in runtime or name not in packages:
            continue
        runtime.add(name)
        for dep in packages[name].get('dependencies', []):
            stack.append(dep['name'])
            for extra in dep.get('extra', []):
                stack.extend(d['name'] for d in packages[dep['name']].get('optional-dependencies', {}).get(extra, []))
    lookup = lookup or _installed_python
    rows = []
    for name, package in sorted(packages.items()):
        if name in own:
            continue
        version = package['version']
        found = lookup(name, version)
        rows.append(_row(policy, 'python', name, version, 'runtime' if name in runtime else 'dev', found, _python_artifact(package)))
    return rows


def _installed_python(name, version):
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return None
    if dist.version != version:
        raise Incomplete(f'python {name}: installed {dist.version}, locked {version}')
    expression = dist.metadata.get('License-Expression')
    raw = expression if expression else [c.split('::')[-1].strip() for c in (dist.metadata.get_all('Classifier') or []) if c.startswith('License ::')] or None

    def read(relative):
        for file in dist.files or []:
            if str(file) == relative:
                return Path(dist.locate_file(file)).read_bytes()
        return None
    return {'raw': raw, 'read': read}


# ---- Node (pnpm-lock.yaml) -----------------------------------------------------------------------------------------------------
def _key(snapshot):
    return snapshot.split('(')[0]


def node_inventory(root, policy, lookup=None):
    import yaml
    lock = yaml.safe_load((root / 'pnpm-lock.yaml').read_text())
    packages, snapshots = lock.get('packages') or {}, lock.get('snapshots') or {}

    def walk(starts):
        seen, stack = set(), list(starts)
        while stack:
            key = stack.pop()
            if key in seen:
                continue
            seen.add(key)
            snap = snapshots.get(key) or {}
            for group in ('dependencies', 'optionalDependencies'):
                for dep, ver in (snap.get(group) or {}).items():
                    stack.append(f'{dep}@{ver}')
        return {_key(k) for k in seen}

    starts = {'runtime': [], 'dev': []}
    for importer in (lock.get('importers') or {}).values():
        for group, scope in (('dependencies', 'runtime'), ('optionalDependencies', 'runtime'), ('devDependencies', 'dev')):
            for dep, spec in (importer.get(group) or {}).items():
                version = spec['version'] if type(spec) is dict else spec
                if not str(version).startswith('link:'):
                    starts[scope].append(f'{dep}@{version}')
    runtime = walk(starts['runtime'])
    lookup = lookup or (lambda name, version: _installed_node(root, name, version))
    rows = []
    for key, package in sorted(packages.items()):
        name, _, version = key.rpartition('@')
        integrity = (package.get('resolution') or {}).get('integrity')
        rows.append(_row(policy, 'node', name, version, 'runtime' if key in runtime else 'dev', lookup(name, version), integrity))
    return rows


def _installed_node(root, name, version):
    folder = name.replace('/', '+')
    for path in sorted((root / 'node_modules/.pnpm').glob(f'{folder}@{version}*/node_modules/{name}/package.json')):
        manifest = json.loads(path.read_text())
        if manifest.get('version') != version:
            continue
        raw = manifest.get('license')
        if type(raw) is dict:
            raw = raw.get('type')
        if raw is None and type(manifest.get('licenses')) is list:
            raw = [item.get('type') if type(item) is dict else item for item in manifest['licenses']]
        base = path.parent

        def read(relative, base=base):
            target = base / relative
            return target.read_bytes() if target.is_file() else None
        return {'raw': raw, 'read': read}
    return None


# ---- one package ---------------------------------------------------------------------------------------------------------------
def _clarification(policy, ecosystem, name, version):
    for item in policy['clarifications']:
        if item['ecosystem'] == ecosystem and item['name'] == name and re.fullmatch(item['version_pattern'], version):
            return item
    return None


def _off_platform(policy, ecosystem, name, version):
    for item in policy['off_platform']:
        if item['ecosystem'] == ecosystem and item['name'] == name and item['version'] == version:
            return item
    return None


def _row(policy, ecosystem, name, version, scope, found, artifact):
    row = {'ecosystem': ecosystem, 'name': name, 'version': version, 'scope': scope, 'problems': []}
    if found is None:
        record = _off_platform(policy, ecosystem, name, version)
        if record is None:
            raise Incomplete(f'{ecosystem} {name}@{version}: not installed here and no off-platform record')
        if artifact is None or record['artifact'] != artifact:
            row['problems'].append('off-platform record is for another artifact than the locked one')
        row.update(license=record['license'], source='off_platform_record')
    else:
        expression = normalize(found['raw'], policy)
        clarified = _clarification(policy, ecosystem, name, version)
        if clarified is not None:
            content = found['read'](clarified['license_file'])
            if content is None or hashlib.sha256(content).hexdigest() != clarified['license_file_sha256']:
                row['problems'].append('clarified license file missing or changed')
            expression, source = clarified['license'], 'clarification'
        else:
            source = 'installed_metadata'
        if expression is None:
            row['problems'].append('license metadata missing or ambiguous (add a clarification record)')
            row.update(license=None, source=source)
            return row
        row.update(license=expression, source=source)
    try:
        category = classify(parse(row['license']), policy)
    except (ValueError, TypeError):
        row['problems'].append('license expression invalid')
        return row
    row['category'] = category
    if category == 'denied':
        row['problems'].append('license denied or not listed: ' + row['license'])
    elif category == 'conditional':
        entry = next((c for c in policy['conditional'] if c['ecosystem'] == ecosystem and c['name'] == name), None)
        if entry is None:
            row['problems'].append('weak-copyleft license without a package entry: ' + row['license'])
        elif not re.fullmatch(entry['version_pattern'], version) or scope not in entry['scopes']:
            row['problems'].append('weak-copyleft entry does not cover this version or scope')
        elif date.fromisoformat(entry['review_by']) < date.today():
            row['problems'].append('weak-copyleft entry review date passed')
        else:
            row['obligation'] = entry['use']
    return row


# ---- vendored sources, project license, assets -----------------------------------------------------------------------------
def vendored_problems(root, policy):
    problems = []
    notice = (root / 'NOTICE').read_text()
    lock = json.loads((root / 'versions.lock.json').read_text())
    for key, upstream in sorted(lock['upstream_sources'].items()):
        entry = policy['vendored'].get(key)
        if entry is None:
            problems.append(f'vendored {key}: no policy entry')
            continue
        repository = upstream['repository'].rstrip('/').split('github.com/')[-1]
        if repository not in notice or upstream['commit'] not in notice:
            problems.append(f'vendored {key}: NOTICE does not name {repository} at the locked commit')
        file = root / entry['license_file']
        if not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest() != entry['license_file_sha256']:
            problems.append(f'vendored {key}: license text missing or changed')
        if entry.get('provenance'):
            provenance = json.loads((root / entry['provenance']).read_text())
            if provenance.get('commit') != upstream['commit']:
                problems.append(f'vendored {key}: provenance commit differs from versions.lock.json')
            for item in provenance.get('files', []):
                path = root / (item.get('destination') or item.get('path'))
                expected = item.get('nexloop_sha256') or item.get('sha256')
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                    problems.append(f'vendored {key}: {path.relative_to(root)} differs from its provenance record')
    for copy in policy['project_license'].get('notice_copies', []):
        if (root / copy).read_text() != notice:
            problems.append(f'{copy} differs from NOTICE')
    license_file = root / policy['project_license']['file']
    if not license_file.is_file() or hashlib.sha256(license_file.read_bytes()).hexdigest() != policy['project_license']['sha256']:
        problems.append('project LICENSE missing or changed (ADR-026: MIT)')
    return problems


# Directories the locked dependency phase creates in a clean checkout; never part of the source.
INSTALLED_DIRECTORIES = frozenset({'.git', '.venv', 'node_modules', '__pycache__'})


def tracked_files(root):
    """The source files of `root`: `git ls-files` in a work tree; in a `git archive` extraction (the release preflight's clean
    checkout, which has no .git) every file there is a tracked one, apart from what the dependency phase installed."""
    root = Path(root)
    if (root / '.git').exists():
        return subprocess.run(['git', 'ls-files', '-z'], cwd=root, capture_output=True, check=True).stdout.decode().split('\0')
    return sorted(str(path.relative_to(root)) for path in root.rglob('*')
                  if path.is_file() and not INSTALLED_DIRECTORIES.intersection(path.relative_to(root).parts))


def asset_problems(root, policy, tracked=None):
    if tracked is None:
        tracked = tracked_files(root)
    listed = {item['path']: item for item in policy['assets']}
    problems = []
    for name in sorted(n for n in tracked if n):
        path = Path(name)
        if path.suffix.lower() not in ASSET_SUFFIXES or path.parts[0] in ('vendor', 'node_modules') or name.startswith('packages/eios-core/'):
            continue
        entry = listed.get(name)
        if entry is None:
            problems.append(f'asset {name}: not listed (record its owner or third-party license)')
        elif entry['owner'] != 'project':
            try:
                if classify(parse(entry['license']), policy) == 'denied':
                    problems.append(f'asset {name}: license denied')
            except (ValueError, TypeError, KeyError):
                problems.append(f'asset {name}: license invalid')
    for name in listed:
        if name not in tracked:
            problems.append(f'asset {name}: listed but not tracked')
    return problems


# ---- notices ------------------------------------------------------------------------------------------------------------------
def render_notices(rows, policy):
    lines = ['# Third-party notices', '',
             'Generated by `scripts/check_licenses.py --write-notices` from `uv.lock`, `pnpm-lock.yaml` and',
             '`deploy/compliance/license-policy.v1.json`; do not edit by hand. NexLoop itself is MIT (`LICENSE`, ADR-026).',
             'Vendored upstream sources and their license texts are listed in `NOTICE` and `licenses/`.',
             'Dependencies are installed from their registries and are not relicensed; NexLoop does not redistribute them in its',
             'source releases.', '']
    for ecosystem, title in (('python', 'Python packages (uv.lock)'), ('node', 'Node packages (pnpm-lock.yaml)')):
        for scope in ('runtime', 'dev'):
            selected = [r for r in rows if r['ecosystem'] == ecosystem and r['scope'] == scope]
            if not selected:
                continue
            lines += [f'## {title}: {scope}', '', '| Package | Version | License |', '|---|---|---|']
            lines += [f"| {r['name']} | {r['version']} | {r['license']} |" for r in selected]
            lines.append('')
    absent = [r for r in policy['off_platform'] if r.get('license_text_absent')]
    if absent:
        lines += ['## Declared license without license text in the published artifact', '']
        lines += [f"- {r['name']} {r['version']} ({r['license']}): {r['license_text_absent']}" for r in absent]
        lines.append('')
    conditional = [r for r in rows if r.get('obligation')]
    if conditional:
        lines += ['## Weak-copyleft dependencies', '', 'Used unmodified as separate libraries (license policy):', '']
        lines += [f"- {r['name']} {r['version']} ({r['license']}, {r['scope']}): {r['obligation']}" for r in conditional]
        lines.append('')
    return '\n'.join(lines)


# ---- record helper (network; dependency phase only) ------------------------------------------------------------------------
def _fetch(url, attempts=4):
    for attempt in range(attempts):
        try:
            return urllib.request.urlopen(url, timeout=120).read()
        except OSError:
            if attempt == attempts - 1:
                raise


def record_off_platform(root, policy):
    """Download each locked artifact that is not installed here, verify it against the lock, and print a record proposal."""
    import yaml
    out = []
    lock = tomllib.loads((root / 'uv.lock').read_text())
    for package in lock['package']:
        if package['name'] in policy['own_python'] or _installed_python(package['name'], package['version']) is not None:
            continue
        wheel = (package.get('wheels') or [None])[0]
        data = _fetch(wheel['url'])
        if 'sha256:' + hashlib.sha256(data).hexdigest() != wheel['hash']:
            raise InputError('artifact hash mismatch: ' + package['name'])
        archive = zipfile.ZipFile(io.BytesIO(data))
        meta = next(n for n in archive.namelist() if n.endswith('.dist-info/METADATA'))
        text = archive.read(meta).decode()
        found = re.search(r'^License-Expression: (.+)$', text, re.M)
        classifiers = re.findall(r'^Classifier: License :: (?:.+ :: )?(.+)$', text, re.M)
        files = [n for n in archive.namelist() if re.search(r'\.dist-info/(licenses/)?(LICEN[CS]E|COPYING)', n, re.I)]
        out.append({'ecosystem': 'python', 'name': package['name'], 'version': package['version'], 'artifact': wheel['hash'],
                    'license': found.group(1).strip() if found else normalize(classifiers, policy),
                    'license_basis': 'License-Expression' if found else 'classifiers (review by hand)', 'license_file': files[0] if files else None,
                    'license_file_sha256': hashlib.sha256(archive.read(files[0])).hexdigest() if files else None,
                    'recorded': date.today().isoformat()})
    lock = yaml.safe_load((root / 'pnpm-lock.yaml').read_text())
    for key, package in sorted((lock.get('packages') or {}).items()):
        name, _, version = key.rpartition('@')
        if not (package.get('os') or package.get('cpu')) and _installed_node(root, name, version) is not None:
            continue
        integrity = package['resolution']['integrity']
        url = f"https://registry.npmjs.org/{name}/-/{name.split('/')[-1]}-{version}.tgz"
        data = _fetch(url)
        algorithm, _, digest = integrity.partition('-')
        if base64.b64encode(hashlib.new(algorithm, data).digest()).decode() != digest:
            raise InputError('artifact integrity mismatch: ' + key)
        archive = tarfile.open(fileobj=io.BytesIO(data))
        manifest = json.loads(archive.extractfile('package/package.json').read())
        files = sorted(m.name for m in archive.getmembers() if re.fullmatch(r'package/(LICEN[CS]E|COPYING)(\.\w+)?', m.name, re.I))
        out.append({'ecosystem': 'node', 'name': name, 'version': version, 'artifact': integrity, 'license': manifest.get('license'),
                    'license_basis': 'package.json license field',
                    'license_file': files[0][len('package/'):] if files else None,
                    'license_file_sha256': hashlib.sha256(archive.extractfile(files[0]).read()).hexdigest() if files else None,
                    'recorded': date.today().isoformat()})
    return out


def off_platform_problems(root, policy, node_lookup=None):
    """Each record carries license text evidence: the artifact's own license file hash, the license file of the same
    project's installed main package (verified here), or an explicit reviewed statement that the artifact ships none."""
    problems, lookup = [], node_lookup or (lambda name, version: _installed_node(root, name, version))
    for record in policy['off_platform']:
        label = f"off-platform {record.get('name')}@{record.get('version')}"
        if not all(type(record.get(k)) is str and record[k] for k in ('ecosystem', 'name', 'version', 'artifact', 'license', 'license_basis', 'recorded')):
            problems.append(label + ': record incomplete')
            continue
        evidence = [k for k in ('license_file_sha256', 'license_text_from', 'license_text_absent') if record.get(k)]
        if len(evidence) != 1:
            problems.append(label + ': exactly one kind of license text evidence is required')
        elif evidence == ['license_text_from']:
            source = record['license_text_from']
            found = lookup(source['name'], source['version']) if source.get('ecosystem') == 'node' else None
            content = found['read'](source['file']) if found else None
            if content is None or hashlib.sha256(content).hexdigest() != source.get('sha256'):
                problems.append(label + ': referenced license text missing or changed')
        elif evidence == ['license_file_sha256'] and not record.get('license_file'):
            problems.append(label + ': license file name missing')
    return problems


# ---- entry -------------------------------------------------------------------------------------------------------------------
def inventory(root=ROOT, policy=None, *, python_lookup=None, node_lookup=None):
    policy = policy or load_policy(root / 'deploy/compliance/license-policy.v1.json')
    return python_inventory(root, policy, python_lookup) + node_inventory(root, policy, node_lookup)


def check(root=ROOT, policy=None, *, python_lookup=None, node_lookup=None, tracked=None):
    policy = policy or load_policy(root / 'deploy/compliance/license-policy.v1.json')
    rows = inventory(root, policy, python_lookup=python_lookup, node_lookup=node_lookup)
    problems = [f"{r['ecosystem']} {r['name']}@{r['version']}: {p}" for r in rows for p in r['problems']]
    problems += off_platform_problems(root, policy, node_lookup)
    problems += vendored_problems(root, policy) + asset_problems(root, policy, tracked)
    notices = render_notices(rows, policy)
    target = root / 'THIRD_PARTY_NOTICES.md'
    if not target.is_file() or target.read_text() != notices:
        problems.append('THIRD_PARTY_NOTICES.md is missing or stale (run --write-notices)')
    return rows, problems, notices


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--write-notices', action='store_true')
    mode.add_argument('--record-off-platform', action='store_true')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.record_off_platform:
            print(json.dumps(record_off_platform(ROOT, load_policy()), indent=2, ensure_ascii=False))
            return 0
        rows, problems, notices = check()
        if args.write_notices:
            (ROOT / 'THIRD_PARTY_NOTICES.md').write_text(notices)
            problems = [p for p in problems if not p.startswith('THIRD_PARTY_NOTICES.md')]
        if args.report:
            args.report.write_text(json.dumps([{k: r.get(k) for k in ('ecosystem', 'name', 'version', 'scope', 'license', 'category', 'source')}
                                               for r in rows], indent=2, sort_keys=True) + '\n')
    except Incomplete as error:
        print(json.dumps({'ok': False, 'incomplete': str(error)}, ensure_ascii=False))
        return 3
    except (InputError, OSError, ValueError, KeyError) as error:
        print(json.dumps({'ok': False, 'error': type(error).__name__, 'detail': str(error)}, ensure_ascii=False))
        return 2
    counts = {}
    for row in rows:
        counts[row.get('category', 'unknown')] = counts.get(row.get('category', 'unknown'), 0) + 1
    print(json.dumps({'ok': not problems, 'packages': len(rows), 'categories': counts, 'problems': problems}, ensure_ascii=False))
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
