#!/usr/bin/env python3
"""NX-034, local part only (docs/implementation/NX-034-design.md §2, §5; NX-032 D4 minimal manifest first).

Builds, signs, verifies and scans a release on the machine it runs on. It never pushes, tags, publishes images or connects to any
server (those steps are owner-confirmed B-class operations and need NX-032/033).

    release.py manifest  --sha <sha> --images <images.json> --ci-result <result.json> --out <release_dir> [--sequence N]
    release.py sign      --release-dir <dir> --key <private key file>
    release.py verify    --release-dir <dir> --allowed-signers <file> --deploy-sha <sha> [--deploy-image name=sha256:...]...
    release.py sbom      --release-dir <dir>
    release.py rollback-check --current <manifest.json> --target <manifest.json> [--database-head NNNN] [--runtime-format N]
    release.py public-preflight --ref <tag or sha> --out <dir> [--previous-public <ref>] [--release-dir <dir>]

NX-036 (docs/implementation/NX-036-design.md §3, §6): `sbom` also applies deploy/compliance/vulnerability-policy.v1.json (stage
category: listed, not blocking; an expired waiver fails); `public-preflight` runs the nine-step public release gate on a
clean checkout of the given ref. It never creates a tag or a GitHub Release (B-class operations stay with the owner).

Every subcommand prints one JSON object. Exit codes: 0 ok, 1 refused (a check failed), 2 input or environment error,
3 a required tool is not installed (never treated as a pass).
"""
import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from datetime import UTC, date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / 'packages/eios-core/src/eios/migrations'
PI_SQLITE_MIGRATIONS = ROOT / 'vendor/pi/packages/durable/src/storage/sqlite/migrations.ts'
LOCK_FILES = ('versions.lock.json', 'uv.lock', 'pnpm-lock.yaml')
NAMESPACE = 'nexloop-release'
SIGNER_IDENTITY = 'nexloop-release'
EXPAND_ONLY = '-- nexloop:expand-only'
MANIFEST_VERSION = 1
_SHA = re.compile(r'[0-9a-f]{40}')
_DIGEST = re.compile(r'sha256:[0-9a-f]{64}')
_NAME = re.compile(r'[a-z0-9][a-z0-9._/-]{0,127}')
SEVERITIES = ('Critical', 'High', 'Medium', 'Low', 'Negligible', 'Unknown')
_FINDING = re.compile(r'(CVE-[0-9]{4}-[0-9]{4,}|GHSA(-[23456789cfghjmpqrvwx]{4}){3}|[A-Z]+-[0-9A-Za-z.:-]{1,64})')


class Refused(Exception):
    """A release check failed (exit 1)."""


class InputError(Exception):
    """Bad input or environment (exit 2)."""


class NotInstalled(Exception):
    """A required external tool is missing (exit 3); never a pass."""


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def git(*args, root=ROOT, binary=False):
    try:
        out = subprocess.run(['git', '-C', str(root), *args], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise InputError('git ' + args[0] + ' failed') from error
    return out.stdout if binary else out.stdout.decode().strip()


# ---- inputs read from the checkout -----------------------------------------------------------------------------------------
def catalog_summary(migrations=MIGRATIONS):
    """Migration head, digest of the published (version, checksum) list, and the lowest head an older application may run
    against: every migration above it declares itself expand-only (§5)."""
    catalog = json.loads((migrations / 'catalog.json').read_text())
    rows = [(m['version'], m['sha256']) for m in catalog['migrations']]
    if not rows:
        raise InputError('empty migration catalog')
    head = rows[-1][0]
    since = head
    for version, _ in reversed(rows):
        if version == head:
            continue
        upper = [m for m in catalog['migrations'] if m['version'] > version]
        if all(expand_only(migrations / m['name']) for m in upper):
            since = version
        else:
            break
    return {'head': head, 'count': len(rows), 'digest': hashlib.sha256(canonical(rows).encode()).hexdigest(), 'expand_only_since': since}


def expand_only(path):
    try:
        with open(path) as f:
            for line in f:
                if line.strip() == EXPAND_ONLY:
                    return True
                if line.strip() and not line.startswith('--'):
                    return False
    except OSError:
        return False
    return False


def runtime_store_format(path=PI_SQLITE_MIGRATIONS):
    """The Pi Durable SQLite schema version the Agent Host writes (highest SQLITE_MIGRATIONS version)."""
    try:
        text = path.read_text()
    except OSError as error:
        raise InputError('Pi Durable migrations unavailable') from error
    block = re.search(r'SQLITE_MIGRATIONS[^=]*=\s*\[(.*?)\];', text, re.S)
    versions = [int(v) for v in re.findall(r'version:\s*(\d+)', block.group(1))] if block else []
    if not versions:
        raise InputError('Pi Durable schema version not found')
    return max(versions)


def contracts_digest(root=ROOT):
    files = sorted(p for p in (root / 'packages/contracts').rglob('*') if p.is_file())
    return hashlib.sha256(canonical([[str(p.relative_to(root)), sha256_file(p)] for p in files]).encode()).hexdigest()


def archive_digests(archive):
    """Manifest digests an OCI image archive declares in its index.json (empty when the archive has no OCI index)."""
    try:
        with tarfile.open(archive) as tar:
            member = tar.getmember('index.json')
            index = json.load(tar.extractfile(member))
    except (KeyError, tarfile.TarError, OSError, ValueError):
        return set()
    return {m.get('digest') for m in index.get('manifests', []) if isinstance(m, dict)}


# ---- manifest ---------------------------------------------------------------------------------------------------------------
def build_manifest(*, sha, images, ci_result, out, sequence=1, root=ROOT, migrations=MIGRATIONS, pi=PI_SQLITE_MIGRATIONS, now=None):
    if not _SHA.fullmatch(sha or ''):
        raise InputError('sha must be a full 40-hex commit')
    if git('rev-parse', '--verify', sha + '^{commit}', root=root) != sha:
        raise InputError('commit not in this repository')
    ci = json.loads(Path(ci_result).read_text())
    # The release is of a tested commit only (AT-055): the CI result names this exact commit and passed.
    if ci.get('sha') != sha or ci.get('status') != 'passed':
        raise Refused('CI result is not a pass of this commit')
    spec = json.loads(Path(images).read_text())
    if type(spec) is not dict or not spec:
        raise InputError('images: a non-empty object {name: {archive, digest}}')
    out = Path(out)
    if out.exists():
        raise InputError('release directory exists (releases are immutable)')
    (out / 'images').mkdir(parents=True)
    lock = json.loads((root / 'versions.lock.json').read_text())
    entries = []
    for name, item in sorted(spec.items()):
        if not _NAME.fullmatch(name) or type(item) is not dict or not _DIGEST.fullmatch(item.get('digest', '')):
            raise InputError(f'image {name}: name and sha256 digest required')
        source = Path(item['archive'])
        declared = archive_digests(source)
        if declared and item['digest'] not in declared:
            raise Refused(f'image {name}: archive does not contain the declared digest')
        target = out / 'images' / (name.replace('/', '_') + '.tar')
        shutil.copyfile(source, target)
        entries.append({'name': name, 'digest': item['digest'], 'archive': str(target.relative_to(out)), 'archive_sha256': sha256_file(target),
                        'base_digest': (lock.get('images', {}).get(item.get('base', '')) or {}).get('digest'), 'sbom_sha256': None})
    archive = git('archive', '--format=tar', sha, root=root, binary=True)
    now = now or datetime.now(UTC)
    manifest = {
        'manifest_version': MANIFEST_VERSION,
        'release_id': f"{now.strftime('%Y%m%d')}-{sha[:12]}-{int(sequence)}",
        'commit': sha, 'parents': git('rev-list', '--parents', '-n', '1', sha, root=root).split()[1:], 'tree': git('rev-parse', sha + '^{tree}', root=root),
        'ci_result_digest': sha256_file(ci_result), 'ci_policy_version': ci.get('policy_version'), 'deadline_gate': ci.get('deadline_gate'),
        'tests': ci.get('counts'),
        'images': entries, 'scan': None,
        'catalog': catalog_summary(migrations), 'runtime_store_format': runtime_store_format(pi), 'contracts_digest': contracts_digest(root),
        'locks': {name: sha256_file(root / name) for name in LOCK_FILES if (root / name).exists()},
        'source_archive_sha256': hashlib.sha256(archive).hexdigest(),
        'built_at': now.isoformat().replace('+00:00', 'Z'), 'builder_version': MANIFEST_VERSION,
    }
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    return manifest


# ---- signature (D1: ssh-keygen -Y) -----------------------------------------------------------------------------------------
def _ssh_keygen():
    tool = shutil.which('ssh-keygen')
    if tool is None:
        raise NotInstalled('ssh-keygen not installed')
    return tool


def sign(release_dir, key):
    release_dir = Path(release_dir)
    manifest = release_dir / 'manifest.json'
    if (release_dir / 'manifest.json.sig').exists():
        raise InputError('already signed')
    try:
        subprocess.run([_ssh_keygen(), '-Y', 'sign', '-n', NAMESPACE, '-f', str(key), str(manifest)], capture_output=True, check=True)
    except subprocess.CalledProcessError as error:
        raise InputError('signing failed') from error
    return {'signed': True, 'signature_sha256': sha256_file(release_dir / 'manifest.json.sig')}


def verify_signature(release_dir, allowed_signers):
    release_dir = Path(release_dir)
    signature = release_dir / 'manifest.json.sig'
    if not signature.exists():
        raise Refused('manifest is not signed')
    with open(release_dir / 'manifest.json', 'rb') as data:
        result = subprocess.run([_ssh_keygen(), '-Y', 'verify', '-f', str(allowed_signers), '-I', SIGNER_IDENTITY, '-n', NAMESPACE, '-s', str(signature)],
                                stdin=data, capture_output=True)
    if result.returncode != 0:
        raise Refused('manifest signature invalid')


def verify(release_dir, *, allowed_signers, deploy_sha, deploy_images):
    """AT-055: what is about to be deployed must be exactly the signed, tested manifest."""
    release_dir = Path(release_dir)
    verify_signature(release_dir, allowed_signers)
    manifest = json.loads((release_dir / 'manifest.json').read_text())
    failures = []
    if deploy_sha != manifest['commit']:
        failures.append('deploy_sha_differs_from_tested_commit')
    expected = {i['name']: i for i in manifest['images']}
    for image in manifest['images']:
        path = release_dir / image['archive']
        if not path.exists() or sha256_file(path) != image['archive_sha256']:
            failures.append('archive_changed:' + image['name'])
    for name, digest in deploy_images.items():
        if name not in expected:
            failures.append('image_not_in_manifest:' + name)
        elif digest != expected[name]['digest']:
            failures.append('image_digest_differs:' + name)
    missing = sorted(set(expected) - set(deploy_images))
    if missing:
        failures.append('image_not_deployed_from_manifest:' + ','.join(missing))
    if failures:
        raise Refused(';'.join(failures))
    return {'verified': True, 'release_id': manifest['release_id'], 'commit': manifest['commit']}


# ---- SBOM and vulnerability scan (D2: syft + grype, pinned versions, never a silent pass) -----------------------------------
def pinned_tools(root=ROOT):
    return json.loads((root / 'versions.lock.json').read_text())['release_tools']


def _tool(name, pinned):
    path = shutil.which(name)
    if path is None:
        raise NotInstalled(f'{name} not installed (pinned {pinned[name]["version"]})')
    try:
        out = subprocess.run([path, 'version'], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired) as error:
        raise NotInstalled(f'{name} not runnable') from error
    found = re.search(r'Version:\s*v?([0-9][0-9.]*)', out)
    if not found or found.group(1) != pinned[name]['version']:
        raise Refused(f'{name} version {found.group(1) if found else "unknown"} is not the pinned {pinned[name]["version"]}')
    return path


def sbom(release_dir, root=ROOT):
    release_dir = Path(release_dir)
    pinned = pinned_tools(root)
    syft, grype = _tool('syft', pinned), _tool('grype', pinned)
    manifest_path = release_dir / 'manifest.json'
    if (release_dir / 'manifest.json.sig').exists():
        raise InputError('manifest already signed: scan before signing')
    manifest = json.loads(manifest_path.read_text())
    (release_dir / 'sbom').mkdir(exist_ok=True)
    scan = {'tools': {'syft': pinned['syft']['version'], 'grype': pinned['grype']['version']}, 'images': {}}
    all_findings = []
    for image in manifest['images']:
        target = release_dir / 'sbom' / (image['name'].replace('/', '_') + '.spdx.json')
        archive = release_dir / image['archive']
        if subprocess.run([syft, 'scan', 'oci-archive:' + str(archive), '-o', 'spdx-json=' + str(target), '-q'], capture_output=True).returncode != 0 or not target.exists():
            raise Refused('sbom generation failed: ' + image['name'])
        result = subprocess.run([grype, 'sbom:' + str(target), '-o', 'json', '-q'], capture_output=True, text=True)
        if result.returncode != 0:
            raise Refused('vulnerability scan failed: ' + image['name'])
        report = json.loads(result.stdout)
        found = findings(report, 'image:' + image['name'])
        (release_dir / 'sbom' / (image['name'].replace('/', '_') + '.findings.json')).write_text(json.dumps(found, indent=2, sort_keys=True) + '\n')
        all_findings.extend(found)
        counts = {s: 0 for s in SEVERITIES}
        for match in report.get('matches', []):
            severity = (match.get('vulnerability') or {}).get('severity', 'Unknown')
            counts[severity if severity in counts else 'Unknown'] += 1
        fixable = sum(1 for m in report.get('matches', []) if (m.get('vulnerability') or {}).get('severity') == 'Critical'
                      and ((m.get('vulnerability') or {}).get('fix') or {}).get('state') == 'fixed')
        image['sbom_sha256'] = sha256_file(target)
        scan['images'][image['name']] = {'counts': counts, 'critical_with_fix': fixable}
        db = (report.get('descriptor') or {}).get('db') or {}
        scan['db'] = db.get('built') or (db.get('status') or {}).get('built')
    verdict = evaluate_vulnerabilities(all_findings, load_vulnerability_policy(root), 'stage', scan.get('db'))
    scan['policy'] = verdict['summary']
    manifest['scan'] = scan  # counts and identifiers only; never vulnerability text (D2: reported, not blocking)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    if verdict['problems']:
        raise Refused('; '.join(verdict['problems']))
    return {'scanned': True, 'scan': scan}


# ---- NX-036: vulnerability policy (D5, D6, D12; recommended values pending the owner's decision) ------------------------
def load_vulnerability_policy(root=ROOT):
    policy = json.loads((root / 'deploy/compliance/vulnerability-policy.v1.json').read_text())
    if (policy.get('schema') != 'nexloop-vulnerability-policy/1' or set(policy.get('thresholds', {})) != {'stage', 'public'}
            or type(policy.get('waiver_max_days')) is not int or type(policy.get('waivers')) is not list):
        raise InputError('vulnerability policy shape')
    return policy


def findings(report, scope):
    """grype JSON → identifiers only: finding id, package, version, severity, fix state, scope. Never descriptions."""
    rows = []
    for match in report.get('matches', []):
        vulnerability = match.get('vulnerability') or {}
        artifact = match.get('artifact') or {}
        identifier = str(vulnerability.get('id') or 'UNKNOWN')
        rows.append({'id': identifier if _FINDING.fullmatch(identifier) else 'UNKNOWN', 'package': str(artifact.get('name') or ''),
                     'version': str(artifact.get('version') or ''), 'severity': vulnerability.get('severity') if vulnerability.get('severity') in SEVERITIES else 'Unknown',
                     'fix': (vulnerability.get('fix') or {}).get('state') or 'unknown', 'scope': scope})
    return sorted(rows, key=lambda r: (r['scope'], r['id'], r['package'], r['version']))


def _waiver_problem(waiver, policy, today):
    keys = {'id', 'package', 'version_pattern', 'scope', 'reason', 'approved_by', 'created', 'expires'}
    if type(waiver) is not dict or set(waiver) != keys or not all(type(waiver[k]) is str and waiver[k].strip() for k in keys):
        return 'malformed'
    try:
        created, expires = date.fromisoformat(waiver['created']), date.fromisoformat(waiver['expires'])
        re.compile(waiver['version_pattern'])
    except (ValueError, re.error):
        return 'malformed'
    if created > today or expires < created or (expires - created).days > policy['waiver_max_days']:
        return 'outside the allowed period'
    if expires < today:
        return 'expired'
    return None


def evaluate_vulnerabilities(rows, policy, category, db_built, today=None):
    """Apply the thresholds of one category ('stage' or 'public'). Returns problems (each a refusal) and a summary of ids."""
    today = today or datetime.now(UTC).date()
    rules = policy['thresholds'][category]
    problems, waived, blocking, listed, used = [], [], [], [], set()
    valid = []
    for index, waiver in enumerate(policy['waivers']):
        problem = _waiver_problem(waiver, policy, today)
        if problem:
            problems.append(f"waiver {waiver.get('id', index) if type(waiver) is dict else index} {problem}")
        else:
            valid.append(waiver)

    def matches(rule, row):
        return row['severity'] == rule['severity'] and (rule['fix'] == 'any' or row['fix'] == rule['fix'])
    for row in rows:
        waiver = next((w for w in valid if w['id'] == row['id'] and w['package'] == row['package'] and re.fullmatch(w['version_pattern'], row['version'])
                       and row['scope'].startswith(w['scope'])), None)
        if waiver is not None:
            used.add(id(waiver))
            waived.append(row['id'])
            continue
        if any(matches(rule, row) for rule in rules['block']):
            blocking.append(row['id'])
        elif any(matches(rule, row) for rule in rules['list']):
            listed.append(row['id'])
    if blocking:
        problems.append(f'{len(blocking)} finding(s) above the {category} threshold without a waiver')
    max_age = policy['database_max_age_days'].get(category)
    if max_age is not None:
        try:
            built = datetime.fromisoformat(str(db_built).replace('Z', '+00:00')).date()
        except ValueError:
            built = None
        if built is None or (today - built).days > max_age:
            problems.append('vulnerability database missing or older than allowed')
    summary = {'category': category, 'blocking': sorted(set(blocking)), 'listed': sorted(set(listed)), 'waived': sorted(set(waived)),
               'cleanup': sorted(w['id'] for w in valid if id(w) not in used), 'database': db_built}
    return {'problems': problems, 'summary': summary}


# ---- NX-036: source SBOM and its cross-check with the license inventory ------------------------------------------------
def _purls(spdx):
    found = set()
    for package in spdx.get('packages', []):
        for ref in package.get('externalRefs', []):
            locator = ref.get('referenceLocator', '')
            hit = re.fullmatch(r'pkg:(pypi|npm)/(.+)@([^?#]+)(?:[?#].*)?', locator)
            if ref.get('referenceType') == 'purl' and hit:
                name = hit.group(2).replace('%40', '@').replace('%2F', '/').replace('%2f', '/')
                found.add(({'pypi': 'python', 'npm': 'node'}[hit.group(1)], name.lower() if hit.group(1) == 'pypi' else name, hit.group(3)))
    return found


def cross_check(spdx, inventory, own):
    """Every locked third-party package in the license inventory appears in the source SBOM and vice versa."""
    listed = {(r['ecosystem'], r['name'].lower() if r['ecosystem'] == 'python' else r['name'], r['version']) for r in inventory}
    sbom_set = {item for item in _purls(spdx) if item[1] not in own}
    problems = [f'in the license inventory but not in the SBOM: {e} {n}@{v}' for e, n, v in sorted(listed - sbom_set)]
    problems += [f'in the SBOM but not in the license inventory: {e} {n}@{v}' for e, n, v in sorted(sbom_set - listed)]
    return problems


def source_sbom(checkout, out, root=ROOT):
    """syft over the source tree of a clean checkout (tracked files only), grype over that SBOM."""
    pinned = pinned_tools(root)
    syft, grype = _tool('syft', pinned), _tool('grype', pinned)
    out = Path(out)
    target = out / 'source.spdx.json'
    if subprocess.run([syft, 'scan', 'dir:' + str(checkout), '-o', 'spdx-json=' + str(target), '-q'], capture_output=True).returncode != 0 or not target.exists():
        raise Refused('source sbom generation failed')
    result = subprocess.run([grype, 'sbom:' + str(target), '-o', 'json', '-q'], capture_output=True, text=True)
    if result.returncode != 0:
        raise Refused('source vulnerability scan failed')
    report = json.loads(result.stdout)
    db = (report.get('descriptor') or {}).get('db') or {}
    return json.loads(target.read_text()), findings(report, 'source'), db.get('built') or (db.get('status') or {}).get('built')


# ---- NX-036: public release gate (nine steps; any missing condition is a failure) --------------------------------------
PREFLIGHT_STEPS = ('clean_checkout', 'licenses', 'public_files', 'ci_isolation', 'publication_scan', 'history_scan', 'source_sbom',
                   'vulnerabilities', 'summary')


def _run(command, cwd):
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    except OSError:
        return 127, ''  # a command that is not installed is a failed step, never a pass
    return result.returncode, result.stdout


def public_preflight(ref, out, *, previous_public=None, release_dir=None, root=ROOT, runner=None, sbom_maker=None, today=None):
    """Run every step even after a failure (the report shows all of them); ok only when every step passed.

    The clean checkout is the ref's `git archive` (tracked files only) extracted into <out>/checkout; the released ref's
    own scripts and policies are used. Git-history steps run in the repository with the checkout's scripts."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    run = runner or _run
    steps, state = {}, {}

    def record(name, ok, detail=None):
        steps[name] = {'ok': bool(ok), **({'detail': detail} if detail is not None else {})}

    sha = git('rev-parse', '--verify', ref + '^{commit}', root=root)
    archive, checkout = out / 'source.tar', out / 'checkout'
    if checkout.exists() or archive.exists():
        raise InputError('output directory already holds a preflight')
    code, _ = run(['git', 'archive', '--format=tar', '-o', str(archive), sha], root)
    if code == 0 and archive.is_file():
        checkout.mkdir()
        with tarfile.open(archive) as source:
            source.extractall(checkout, filter='data')
        dependency = [run(['uv', 'sync', '--frozen'], checkout)[0], run(['pnpm', 'install', '--frozen-lockfile'], checkout)[0]]
        record('clean_checkout', dependency == [0, 0], {'commit': sha, 'dependency_exits': dependency})
    else:
        record('clean_checkout', False, {'commit': sha, 'archive_exit': code})
    ready = steps['clean_checkout']['ok']
    python = ['uv', 'run', '--frozen', 'python']
    inventory_path = out / 'licenses.json'
    for name, command in (('licenses', python + ['scripts/check_licenses.py', '--check', '--report', str(inventory_path)]),
                          ('public_files', python + ['scripts/check_public_files.py', '--public']),
                          ('ci_isolation', python + ['scripts/check_ci_isolation.py'])):
        code = run(command, checkout)[0] if ready else None
        record(name, code == 0, {'exit': code})
    codes = [run(['python3', str(checkout / 'scripts/check_publication.py'), '--tree', sha], root)[0] if ready else None,
             run(['python3', str(checkout / 'scripts/check_publication.py'), '--archive', str(archive)], root)[0] if ready else None]
    record('publication_scan', codes == [0, 0], {'exits': {'tree': codes[0], 'archive': codes[1]}})
    if previous_public is None:
        # prepush-scan.sh needs a base commit; the first public release instead needs the full-history audit with the
        # owner's confirmation (design §5, D9), which is not something this gate can stand in for.
        record('history_scan', False, {'refused': 'no previous public release: the full-history audit and owner confirmation are required'})
    else:
        code = run(['bash', str(checkout / 'scripts/ci/prepush-scan.sh'), sha, previous_public], root)[0] if ready else None
        record('history_scan', code == 0, {'exit': code})
    try:
        spdx, rows, db = (sbom_maker or (lambda: source_sbom(checkout, out, root)))()
        inventory = json.loads(inventory_path.read_text()) if inventory_path.is_file() else None
        problems = ['license inventory missing'] if inventory is None else cross_check(spdx, inventory, _own_names(checkout))
        record('source_sbom', not problems, {'problems': problems[:50], 'packages': len(_purls(spdx))})
        state.update(findings=rows, db=db, inventory=inventory or [])
    except NotInstalled as error:
        record('source_sbom', False, {'not_installed': str(error)})
    except (Refused, OSError, ValueError, InputError) as error:
        record('source_sbom', False, {'refused': str(error)})
    if 'findings' in state:
        rows, missing = list(state['findings']), []
        if release_dir is not None:
            image_findings = sorted((Path(release_dir) / 'sbom').glob('*.findings.json'))
            missing = [] if image_findings else ['release directory has no image findings (run release.py sbom)']
            for path in image_findings:
                rows += json.loads(path.read_text())
        policy_root = checkout if (checkout / 'deploy/compliance/vulnerability-policy.v1.json').is_file() else root
        verdict = evaluate_vulnerabilities(rows, load_vulnerability_policy(policy_root), 'public', state['db'], today)
        record('vulnerabilities', not (verdict['problems'] or missing), {'problems': missing + verdict['problems'], **verdict['summary']})
    else:
        record('vulnerabilities', False, {'refused': 'no source scan'})
    ok = all(step['ok'] for step in steps.values())
    detail = steps['vulnerabilities']['detail']
    record('summary', ok, {'for_owner_confirmation': {
        'blocking': detail.get('blocking'), 'listed': detail.get('listed'), 'waived': detail.get('waived'), 'waiver_cleanup': detail.get('cleanup'),
        'conditional_licenses': sorted(f"{r['name']} {r['version']} ({r['license']}, {r['scope']})" for r in state.get('inventory', [])
                                       if r.get('category') == 'conditional')}})
    report = {'ref': ref, 'commit': sha, 'ok': ok, 'steps': {name: steps[name] for name in PREFLIGHT_STEPS}}
    text = json.dumps(report, indent=2, sort_keys=True) + '\n'
    (out / 'preflight.json').write_text(text)
    report['report_sha256'] = hashlib.sha256(text.encode()).hexdigest()
    return report


def _own_names(root):
    """The workspace's own packages (Python workspace members, Node workspace importers) are not third-party."""
    own = {'nexloop', 'nexloop-eios-core'}
    try:
        import yaml
        lock = yaml.safe_load((Path(root) / 'pnpm-lock.yaml').read_text())
        for importer in lock.get('importers', {}):
            manifest = Path(root) / importer / 'package.json'
            if manifest.is_file():
                own.add(json.loads(manifest.read_text()).get('name', ''))
    except (OSError, ValueError, ImportError):
        pass
    return own


# ---- rollback protection (AT-057) -------------------------------------------------------------------------------------------
def rollback_check(current, target, *, database_head=None, runtime_format=None, migrations=MIGRATIONS):
    """Refuse a blind rollback: the target must run on the current schema (equal head, or only expand-only migrations above its
    head) and must not lower the runtime store format. Refusals say how to move forward instead."""
    cur, tgt = json.loads(Path(current).read_text()), json.loads(Path(target).read_text())
    db_head = database_head or cur['catalog']['head']
    fmt = runtime_format if runtime_format is not None else cur['runtime_store_format']
    reasons, hints = [], []
    h = tgt['catalog']['head']
    if h > db_head:
        reasons.append('target_is_newer_schema')
        hints.append('this is an upgrade with migrations: use the normal release flow, not rollback')
    elif h < db_head:
        catalog = json.loads((migrations / 'catalog.json').read_text())
        above = [m for m in catalog['migrations'] if h < m['version'] <= db_head]
        unmarked = [m['version'] for m in above if not expand_only(migrations / m['name'])]
        if not above:
            # The database is ahead of what this checkout knows: nothing proves the target can run on it.
            reasons.append('migrations_between_unknown')
            hints.append('run the check from the checkout of the current release')
        if unmarked:
            reasons.append('schema_not_backward_compatible:' + ','.join(unmarked))
            hints.append('fix forward: release a corrected build on the current schema; never run down migrations')
    if tgt['runtime_store_format'] < fmt:
        reasons.append(f'runtime_store_format_downgrade:{fmt}->{tgt["runtime_store_format"]}')
        hints.append('keep the newer runtime files and mark affected Runs blocked; fix forward on the current Host')
    if reasons:
        raise Refused(json.dumps({'reasons': reasons, 'hints': hints}, ensure_ascii=False))
    return {'rollback_allowed': True, 'target_release': tgt['release_id'], 'database_head': db_head, 'target_head': h}


# ---- CLI --------------------------------------------------------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('manifest')
    p.add_argument('--sha', required=True)
    p.add_argument('--images', required=True, type=Path)
    p.add_argument('--ci-result', required=True, type=Path)
    p.add_argument('--out', required=True, type=Path)
    p.add_argument('--sequence', type=int, default=1)
    p = sub.add_parser('sign')
    p.add_argument('--release-dir', required=True, type=Path)
    p.add_argument('--key', required=True, type=Path)
    p = sub.add_parser('verify')
    p.add_argument('--release-dir', required=True, type=Path)
    p.add_argument('--allowed-signers', required=True, type=Path)
    p.add_argument('--deploy-sha', required=True)
    p.add_argument('--deploy-image', action='append', default=[], help='name=sha256:<digest> of each image about to be deployed')
    p = sub.add_parser('sbom')
    p.add_argument('--release-dir', required=True, type=Path)
    p = sub.add_parser('public-preflight')
    p.add_argument('--ref', required=True)
    p.add_argument('--out', required=True, type=Path)
    p.add_argument('--previous-public')
    p.add_argument('--release-dir', type=Path)
    p = sub.add_parser('rollback-check')
    p.add_argument('--current', required=True, type=Path)
    p.add_argument('--target', required=True, type=Path)
    p.add_argument('--database-head')
    p.add_argument('--runtime-format', type=int)
    args = parser.parse_args(argv)
    try:
        if args.command == 'manifest':
            m = build_manifest(sha=args.sha, images=args.images, ci_result=args.ci_result, out=args.out, sequence=args.sequence)
            result = {'release_id': m['release_id'], 'commit': m['commit'], 'images': len(m['images'])}
        elif args.command == 'sign':
            result = sign(args.release_dir, args.key)
        elif args.command == 'verify':
            images = {}
            for item in args.deploy_image:
                name, _, digest = item.partition('=')
                if not _DIGEST.fullmatch(digest):
                    raise InputError('deploy image must be name=sha256:<64 hex>')
                images[name] = digest
            result = verify(args.release_dir, allowed_signers=args.allowed_signers, deploy_sha=args.deploy_sha, deploy_images=images)
        elif args.command == 'sbom':
            result = sbom(args.release_dir)
        elif args.command == 'public-preflight':
            report = public_preflight(args.ref, args.out, previous_public=args.previous_public, release_dir=args.release_dir)
            if not report['ok']:
                print(json.dumps({'ok': False, 'refused': 'public preflight failed', 'report_sha256': report['report_sha256'],
                                  'steps': {k: v['ok'] for k, v in report['steps'].items()}}))
                return 1
            result = {'commit': report['commit'], 'report_sha256': report['report_sha256'], 'steps': {k: v['ok'] for k, v in report['steps'].items()}}
        else:
            result = rollback_check(args.current, args.target, database_head=args.database_head, runtime_format=args.runtime_format)
    except Refused as error:
        print(json.dumps({'ok': False, 'refused': str(error)}, ensure_ascii=False))
        return 1
    except NotInstalled as error:
        print(json.dumps({'ok': False, 'not_installed': str(error)}, ensure_ascii=False))
        return 3
    except (InputError, OSError, ValueError, KeyError) as error:
        print(json.dumps({'ok': False, 'error': type(error).__name__, 'detail': str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps({'ok': True, **result}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
