#!/usr/bin/env python3
"""NX-033 local part: local Git ref rules, the CI queue and controller, the trusted judgement, merge, GitHub sync state and the
runner isolation checks (docs/implementation/NX-033-design.md §2–§7; D1–D5 settled).

Standard library only. Every host fact (repository, queue and result directories, protected paths, data hosts and ports, push
identities) comes from arguments or private files; nothing here names a real host, address, user or path. Creating users,
repositories, firewall rules or switching remotes are owner-confirmed B-class steps and are not done by this tool.

    nexloop_ci.py pre-receive   --repo R --identities F            (stdin: <old> <new> <ref> lines; identity from NEXLOOP_PUSH_IDENTITY)
    nexloop_ci.py post-receive  --repo R --queue Q                 (stdin as above)
    nexloop_ci.py enqueue       --repo R --queue Q --ref REF --sha SHA --identity ID [--kind check|merge] --identities F
    nexloop_ci.py run-once      --repo R --queue Q --policy D --identities F [--runner F --facts F] [--stages-version 1|2|3|4]
    nexloop_ci.py ack-policy    --queue Q --sha SHA --identity ID --identities F
    nexloop_ci.py sync          --repo R --queue Q --remote NAME [--timeout S]
    nexloop_ci.py status        --queue Q
    nexloop_ci.py result        --queue Q --sha SHA [--wait SECONDS]
    nexloop_ci.py check-runner  --runner F --facts F
    nexloop_ci.py isolation-probe --targets F

Each command prints one JSON object. Exit codes: 0 ok / passed, 1 refused or failed, 2 configuration or environment error.
"""
import argparse
import fcntl
import glob
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

ZERO = '0' * 40
_SHA = re.compile(r'[0-9a-f]{40}')
_IDENTITY = re.compile(r'[a-z0-9][a-z0-9._-]{0,63}')
_TOPIC = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,127}')
ROLES = ('developer', 'integrator', 'maintainer', 'controller', 'release')
ENQUEUE_PREFIXES = ('refs/heads/incoming/', 'refs/heads/integration/')
# Paths whose change makes a candidate a policy change (§2.2, §4.3).
POLICY_PATHS = ('scripts/ci/', '.ci-policy/', 'deploy/', '.githooks/')
LOCK_FILES = ('versions.lock.json', 'uv.lock', 'pnpm-lock.yaml')
CONTROLLER_VERSION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class ConfigError(Exception):
    """Exit 2: configuration or environment."""


def now():
    return datetime.now(UTC).isoformat().replace('+00:00', 'Z')


def git(repo, *args, check=True, env=None):
    run = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, env=env)
    if check and run.returncode != 0:
        raise ConfigError('git ' + args[0] + ' failed')
    return run.stdout.strip() if check else run


def load_identities(path):
    """Private file: {"identities": {"<identity>": "<role>"}}; roles as ROLES."""
    value = json.loads(Path(path).read_text())
    ids = value.get('identities') if type(value) is dict else None
    if type(ids) is not dict or not ids or any(not _IDENTITY.fullmatch(k) or v not in ROLES for k, v in ids.items()):
        raise ConfigError('identities invalid')
    return ids


def write_atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / ('.tmp-' + path.name + '-' + str(os.getpid()))
    with open(tmp, 'w') as stream:
        stream.write(json.dumps(value, sort_keys=True, indent=1))
        stream.flush()
        os.fsync(stream.fileno())
    os.rename(tmp, path)


# §2.1 ref rules (pre-receive) ---------------------------------------------------------------------------------------------
# Release tags (owner 2026-10-20, v0.1.0): vMAJOR.MINOR.PATCH, optionally -rc.N (whether rc tags are used is the owner's decision).
_VERSION_TAG = re.compile(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-rc\.(0|[1-9][0-9]*))?')


def version_tag_problem(repo, obj, name, signers):
    """None when `obj` is a release tag the owner signed, else the reason: the name is a version, the object is an annotated tag
    carrying that same name, it points at a commit already in main, and its SSH signature verifies against the owner's allowed
    signers file. Works on a pushed object still in the receive quarantine (the hook's environment carries it)."""
    if not _VERSION_TAG.fullmatch(name or ''):
        return 'tag_name_invalid'
    if git(repo, 'cat-file', '-t', obj, check=False).stdout.strip() != 'tag':
        return 'tag_not_annotated'
    header = git(repo, 'cat-file', 'tag', obj, check=False).stdout.split('\n\n', 1)[0].splitlines()
    if f'tag {name}' not in header:
        return 'tag_name_mismatch'
    target = git(repo, 'rev-parse', '--verify', '-q', obj + '^{commit}', check=False).stdout.strip()
    if not target or git(repo, 'merge-base', '--is-ancestor', target, 'refs/heads/main', check=False).returncode != 0:
        return 'tag_not_on_main'
    if not signers or not Path(signers).is_file():
        return 'tag_signers_missing'
    verified = git(repo, '-c', 'gpg.format=ssh', '-c', f'gpg.ssh.allowedSignersFile={signers}', 'verify-tag', obj, check=False)
    return None if verified.returncode == 0 else 'tag_signature_invalid'


def check_update(repo, identities, identity, old, new, ref, tag_signers=None):
    """None when the update is allowed, else the reason. Deleting a ref is refused except an own incoming branch and review/*.
    tag_signers: the owner's allowed signers file; a maintainer creates a release tag only when it verifies (version_tag_problem)."""
    role = identities.get(identity)
    if role is None:
        return 'unknown_identity'
    deleting = new == ZERO
    if ref.startswith('refs/heads/incoming/'):
        owner, _, topic = ref[len('refs/heads/incoming/'):].partition('/')
        if owner != identity or not _TOPIC.fullmatch(topic or ''):
            return 'not_own_incoming'
        return None  # non-fast-forward and delete allowed on one's own topic
    if ref.startswith('refs/heads/integration/'):
        if role not in ('integrator', 'maintainer') or not _TOPIC.fullmatch(ref[len('refs/heads/integration/'):]):
            return 'integration_requires_integrator'
        return 'delete_refused' if deleting else None
    if ref == 'refs/heads/main':
        if role != 'controller':
            return 'main_is_controller_only'
        if deleting:
            return 'delete_refused'
        if old != ZERO and git(repo, 'merge-base', '--is-ancestor', old, new, check=False).returncode != 0:
            return 'main_fast_forward_only'
        return None
    if ref.startswith('refs/tags/v'):
        if role not in ('release', 'maintainer'):
            return 'tag_requires_maintainer_or_release'
        if old != ZERO or deleting:
            return 'tag_immutable'  # never moved, never deleted
        if role == 'maintainer':
            # Owner 2026-10-20 (v0.1.0): the owner signs the tag on their own machine and pushes it here as the maintainer.
            return version_tag_problem(repo, new, ref[len('refs/tags/'):], tag_signers)
        return None
    if ref.startswith('refs/heads/review/'):
        return None if role == 'maintainer' else 'review_is_maintainer_only'
    return 'ref_namespace_refused'


def pre_receive(repo, identities, identity, lines, tag_signers=None):
    refused = []
    for line in lines:
        old, new, ref = line.split()
        reason = check_update(repo, identities, identity, old, new, ref, tag_signers)
        if reason:
            refused.append({'ref': ref, 'reason': reason})
    return refused


# §2.2 queue ----------------------------------------------------------------------------------------------------------------
def queue_dirs(queue):
    queue = Path(queue)
    for name in ('pending', 'running', 'done', 'results', 'merges', 'acks', 'sync'):
        (queue / name).mkdir(parents=True, exist_ok=True)
    return queue


def _next_seq(queue):
    seq_file = queue / 'seq'
    with open(queue / 'seq.lock', 'a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        value = int(seq_file.read_text()) + 1 if seq_file.exists() else 1
        seq_file.write_text(str(value))
    return value


def enqueue(repo, queue, ref, sha, identity, kind='check', identities=None):
    """One immutable request: the SHA as resolved now; a later move of the ref changes nothing queued. Only incoming/* and
    integration/* enter the queue; review/*, main, tags and anything else never do (public PRs never run, AT-054)."""
    queue = queue_dirs(queue)
    if not ref.startswith(ENQUEUE_PREFIXES):
        return {'enqueued': False, 'reason': 'ref_not_queueable'}
    if not _SHA.fullmatch(sha or '') or git(repo, 'cat-file', '-t', sha, check=False).stdout.strip() != 'commit':
        return {'enqueued': False, 'reason': 'sha_unknown'}
    if kind not in ('check', 'merge'):
        raise ConfigError('kind invalid')
    if kind == 'merge' and (identities or {}).get(identity) not in ('integrator', 'maintainer'):
        return {'enqueued': False, 'reason': 'merge_requires_integrator'}
    seq = _next_seq(queue)
    request = {'seq': seq, 'ref': ref, 'sha': sha, 'identity': identity, 'kind': kind, 'requested_at': now()}
    write_atomic(queue / 'pending' / f'{seq:08d}-{sha}.json', request)
    return {'enqueued': True, **request}


def post_receive(repo, queue, identity, lines):
    out = []
    for line in lines:
        old, new, ref = line.split()
        if new != ZERO and ref.startswith(ENQUEUE_PREFIXES):
            out.append(enqueue(repo, queue, ref, new, identity))
    return out


# §4.3 trusted judgement -------------------------------------------------------------------------------------------------
def load_policy(policy_dir, stages_version=1):
    """stages.v1.json (the whole suite as one stage) unless the trusted installation selects another version (v2: one stage
    per scripts/ci/stages script)."""
    policy_dir = Path(policy_dir)
    if type(stages_version) is not int or stages_version not in (1, 2, 3, 4):
        raise ConfigError('stages version')
    stages_file = policy_dir / f'stages.v{stages_version}.json'
    stages = json.loads(stages_file.read_text())
    if stages.get('version') != stages_version:
        raise ConfigError('policy invalid')
    # Stages v3 and v4 (D-j) go with required-suites v2 (python-heavy, count groups); v1 and v2 with required-suites v1.
    suites_file = policy_dir / ('required-suites.v2.json' if stages_version >= 3 else 'required-suites.v1.json')
    suites = json.loads(suites_file.read_text())
    if stages.get('schema') != 'nexloop-ci-stages/1' or suites.get('schema') != 'nexloop-ci-required-suites/1':
        raise ConfigError('policy invalid')
    return {'stages': stages, 'suites': suites, 'version': suites['version'],
            'stages_version': stages_version,
            'digest': hashlib.sha256(stages_file.read_bytes() + suites_file.read_bytes()).hexdigest()}


def junit_counts(path):
    root = ET.parse(path).getroot()
    cases = list(root.iter('testcase'))
    count = lambda tag: sum(1 for c in cases if c.find(tag) is not None)
    failed, errors, skipped = count('failure'), count('error'), count('skipped')
    return {'tests': len(cases), 'passed': len(cases) - failed - errors - skipped, 'failed': failed, 'errors': errors, 'skipped': skipped}


FAILURES_SCHEMA = 'nexloop-ci-failures/1'
FAILURE_MESSAGE_LIMIT = 300
FAILURES_PER_SUITE = 200
_REDACT = (
    (re.compile(r'([A-Za-z][A-Za-z0-9+.-]*://)[^\s/@]*@'), r'\1<redacted>@'),  # credentials in a URI
    (re.compile(r'(?i)\b(password|passwd|secret|token|api[_-]?key|dsn|authorization)(\s*[=:]\s*)\S+'), r'\1\2<redacted>'),
    (re.compile(r'[A-Za-z0-9+/=_-]{40,}'), '<redacted>'),  # long opaque runs (keys, tokens, digests)
)


def _failure_message(case):
    """The failure's message attribute (or the first line of its text): one line, credentials and long opaque runs redacted,
    at most FAILURE_MESSAGE_LIMIT characters. Never the raw log."""
    text = case.get('message') or ((case.text or '').strip().splitlines() or [''])[0]
    text = ' '.join(''.join(ch if ch.isprintable() else ' ' for ch in text).split())
    for pattern, replacement in _REDACT:
        text = pattern.sub(replacement, text)
    return text[:FAILURE_MESSAGE_LIMIT]


def junit_failures(path):
    """Failed and errored cases of one JUnit file: [{classname, name, kind, message}] (kind 'failure' or 'error')."""
    found = []
    for case in ET.parse(path).getroot().iter('testcase'):
        for kind in ('failure', 'error'):
            node = case.find(kind)
            if node is not None:
                found.append({'classname': (case.get('classname') or '')[:FAILURE_MESSAGE_LIMIT], 'name': (case.get('name') or '')[:FAILURE_MESSAGE_LIMIT],
                              'kind': kind, 'message': _failure_message(node)})
                break
    return found


def judge(policy, stages, junit, baseline):
    """The trusted policy decides, not the candidate: every required suite present, nothing failed, errored or skipped, and no
    suite with fewer tests than the last main result beyond its registered allowance."""
    reasons = []
    for stage in stages:
        if stage['status'] != 'passed':
            reasons.append('stage_failed:' + stage['name'])
    groups = policy['suites'].get('count_groups') or []
    grouped = {name for group in groups for name in group['suites']}
    for group in groups:
        # A registered move between suites (required-suites v2): the group's sum must not drop; a missing baseline suite counts 0.
        if all((baseline or {}).get(name, {}).get('tests') is None for name in group['suites']):
            continue
        before = sum((baseline or {}).get(name, {}).get('tests') or 0 for name in group['suites'])
        now_sum = sum((junit.get(name) or {}).get('tests') or 0 for name in group['suites'])
        if now_sum < before - group.get('allowed_drop', 0):
            reasons.append('test_count_dropped:' + '+'.join(group['suites']))
    for name, rule in policy['suites']['suites'].items():
        got = junit.get(name)
        if got is None:
            reasons.append('suite_missing:' + name)
            continue
        if got['failed'] or got['errors'] or got['skipped']:
            reasons.append('suite_not_clean:' + name)
        if got['tests'] < rule.get('min_tests', 1):
            reasons.append('suite_too_small:' + name)
        before = (baseline or {}).get(name, {}).get('tests')
        if name not in grouped and before is not None and got['tests'] < before - rule.get('allowed_drop', 0):
            reasons.append('test_count_dropped:' + name)
    return reasons


# Runner -------------------------------------------------------------------------------------------------------------------
SECRET_ENV = re.compile(r'(SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE|CREDENTIAL|DSN|DATABASE_URL|API_KEY|_KEY$|AUTH)', re.I)
_SOCKETS = ('docker.sock', 'podman.sock')


def _within(path, roots):
    p = os.path.realpath(path)
    return any(p == os.path.realpath(r) or p.startswith(os.path.realpath(r).rstrip('/') + '/') for r in roots if r)


def check_runner(runner, facts, *, euid=None, groups=None):
    """§3 startup check: rootless, no secret, no LAN or data ports, no network by default. Returns the violations (codes only)."""
    v = []
    euid = os.geteuid() if euid is None else euid
    groups = [g for g in (groups if groups is not None else _group_names())]
    protected = [facts.get(k) for k in ('SECRET_ROOT', 'RUNTIME_ROOT', 'ARTIFACT_ROOT', 'BACKUP_ROOT', 'BACKUP_KEY_ROOT', 'RELEASE_ARCHIVE_ROOT')]
    if euid == 0:
        v.append('runs_as_root')
    if 'docker' in groups or 'root' in groups:
        v.append('privileged_group')
    if runner.get('runtime') != 'podman' or runner.get('rootless') is not True:
        v.append('not_rootless_podman')
    if runner.get('privileged') or runner.get('cap_add') or runner.get('devices'):
        v.append('privileged_container')
    net = runner.get('network') or {}
    if net.get('test') != 'none':
        v.append('test_network_not_none')
    if net.get('dependency') not in ('none', 'proxy'):
        v.append('dependency_network_not_proxy')
    if net.get('dependency') == 'proxy':
        proxy = runner.get('proxy') or ''
        host = re.sub(r'^[a-z]+://', '', proxy).split('/')[0].rsplit(':', 1)[0].strip('[]')
        if not proxy or host in _data_hosts(facts):
            v.append('proxy_invalid')
    for mount in runner.get('mounts') or []:
        source = mount.get('source', '')
        if _within(source, protected):
            v.append('protected_path_mounted')
        if source.endswith(_SOCKETS):
            v.append('container_socket_mounted')
        if not mount.get('read_only') and mount.get('target') not in runner.get('writable_targets', ['/work', '/tmp']):
            v.append('writable_mount')
    for name, value in (runner.get('env') or {}).items():
        if SECRET_ENV.search(name):
            v.append('secret_env:' + name)
        if isinstance(value, str) and (value.startswith('/') and _within(value, protected)):
            v.append('protected_path_in_env:' + name)
        if isinstance(value, str) and any(h and h in value for h in _data_hosts(facts)):
            v.append('data_host_in_env:' + name)
    if runner.get('publish'):
        v.append('ports_published')
    for entry in runner.get('add_hosts') or []:
        if any(h and h in entry for h in _data_hosts(facts)):
            v.append('data_host_added')
    cg = runner.get('cgroup') or {}
    if not (cg.get('memory_max') and cg.get('pids_max') and cg.get('cpu_weight')):
        v.append('cgroup_limits_missing')
    return sorted(set(v))


def _data_hosts(facts):
    return [h.strip() for h in str(facts.get('DATA_HOSTS', '')).split(',') if h.strip()]


def _group_names():
    import grp
    names = []
    for gid in os.getgroups():
        try:
            names.append(grp.getgrgid(gid).gr_name)
        except KeyError:
            pass
    return names


def podman_argv(runner, stage, workdir, argv):
    """The rootless container command for one stage (D2): loopback-only network for tests, the allowlisted proxy for the
    dependency stage, no published port, read-only root, cgroup limits, only the checkout and the dependency cache mounted."""
    net = runner['network']['dependency'] if stage.get('network') == 'dependency' else runner['network']['test']
    out = ['podman', 'run', '--rm', '--read-only', '--userns=keep-id', '--security-opt=no-new-privileges', '--cap-drop=ALL',
           f'--network={"none" if net == "none" else "slirp4netns:allow_host_loopback=false"}',
           f'--memory={runner["cgroup"]["memory_max"]}', f'--pids-limit={runner["cgroup"]["pids_max"]}', f'--cpu-weight={runner["cgroup"]["cpu_weight"]}',
           '-v', f'{workdir}:/work:rw', '-w', '/work']
    for mount in runner.get('mounts') or []:
        out += ['-v', f'{mount["source"]}:{mount["target"]}:{"ro" if mount.get("read_only") else "rw"}']
    env = {**(runner.get('env') or {}), **(stage.get('env') or {})}
    if net == 'proxy':
        env.update(HTTPS_PROXY=runner['proxy'], HTTP_PROXY=runner['proxy'])
    for k, val in sorted(env.items()):
        out += ['-e', f'{k}={val}']
    return out + [runner['image'], *argv]


# Controller ---------------------------------------------------------------------------------------------------------------
def _stage_env(stage):
    base = {k: os.environ[k] for k in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR') if k in os.environ}
    # Checkout and stages only ever use the local repository: no network protocol is allowed to git (AT-053).
    return {**base, 'GIT_ALLOW_PROTOCOL': 'file', 'GIT_TERMINAL_PROMPT': '0', **(stage.get('env') or {})}


def _attempt_dir(queue, sha):
    root = Path(queue) / 'results' / sha
    n = len(list(root.glob('attempt-*'))) + 1 if root.exists() else 1
    return root / f'attempt-{n}', n


def latest_result(queue, sha):
    root = Path(queue) / 'results' / sha
    attempts = sorted(root.glob('attempt-*/result.json'), key=lambda p: int(p.parent.name.split('-')[1])) if root.exists() else []
    return json.loads(attempts[-1].read_text()) if attempts else None


def _baseline(queue, repo):
    main = git(repo, 'rev-parse', '--verify', '-q', 'refs/heads/main', check=False).stdout.strip()
    result = latest_result(queue, main) if main else None
    return {k: v for k, v in ((result or {}).get('suites') or {}).items()}


def _locks_and_catalog(work):
    locks = {name: hashlib.sha256((work / name).read_bytes()).hexdigest() for name in LOCK_FILES if (work / name).is_file()}
    catalog = work / 'packages/eios-core/src/eios/migrations/catalog.json'
    cat = None
    if catalog.is_file():
        entries = json.loads(catalog.read_text())['migrations']
        cat = {'head': entries[-1]['version'] if entries else None,
               'digest': hashlib.sha256(json.dumps([[m['version'], m['sha256']] for m in entries], separators=(',', ':')).encode()).hexdigest()}
    return locks, cat


def run_check(repo, queue, request, policy, identities, *, runner=None, workroot=None):
    sha, ref = request['sha'], request['ref']
    attempt_dir, attempt = _attempt_dir(queue, sha)
    started, t0 = now(), time.monotonic()
    result = {'schema': 'nexloop-ci-result/1', 'sha': sha, 'ref': ref, 'identity': request['identity'], 'attempt': attempt,
              'controller_version': CONTROLLER_VERSION, 'policy_version': policy['version'], 'policy_digest': policy['digest'],
              'started_at': started, 'load': list(os.getloadavg())}
    # Admission (§2.2).
    admission = []
    if identities.get(request['identity']) is None:
        admission.append('identity_not_allowed')
    if git(repo, 'cat-file', '-t', sha, check=False).stdout.strip() != 'commit':
        admission.append('sha_unknown')
    main = git(repo, 'rev-parse', '--verify', '-q', 'refs/heads/main', check=False).stdout.strip()
    changed = git(repo, 'diff', '--name-only', f'{main}...{sha}', check=False).stdout.split() if main and not admission else []
    result['policy_change'] = any(p.startswith(POLICY_PATHS) for p in changed)
    result['parents'] = git(repo, 'rev-list', '--parents', '-n', '1', sha).split()[1:] if not admission else []
    stages, junit = [], {}
    if admission:
        result.update(status='failed', reasons=admission, stages=[], counts=None, deadline_gate=None, isolation=None)
    else:
        work = Path(workroot or (Path(queue) / 'work')) / f'{sha}-{attempt}'
        if work.exists():
            raise ConfigError('work directory exists')
        env = _stage_env({})
        subprocess.run(['git', 'clone', '-q', '--no-checkout', '--no-hardlinks', str(repo), str(work)], check=True, env=env, capture_output=True)
        subprocess.run(['git', '-C', str(work), 'checkout', '-q', '--detach', sha], check=True, env=env, capture_output=True)
        result['locks'], result['catalog'] = _locks_and_catalog(work)
        failures, kept = {}, attempt_dir / 'junit'
        for stage in policy['stages']['stages']:
            s0 = time.monotonic()
            argv = stage['argv']
            if runner is not None:
                argv = podman_argv(runner, stage, work, argv)
            try:
                run = subprocess.run(argv, cwd=work, env=_stage_env(stage), capture_output=True, text=True, timeout=stage.get('timeout_seconds', 7200))
                rc = run.returncode
            except subprocess.TimeoutExpired:
                rc = 'timeout'
            status = 'passed' if rc == 0 else ('dependency_unavailable' if stage['name'].startswith('dependency') else 'failed')
            entry = {'name': stage['name'], 'status': status, 'exit': rc, 'seconds': round(time.monotonic() - s0, 3), 'junit': {}}
            for pattern in stage.get('junit', []):
                for path in sorted(glob.glob(str(work / pattern))):
                    name = Path(path).stem
                    counts = junit_counts(path)
                    junit[name] = counts
                    entry['junit'][name] = {'sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest(), **counts}
                    # Kept with the result (the work directory is removed below): the JUnit file itself, and the failed names.
                    copy = kept / stage['name'] / Path(path).name
                    copy.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, copy)
                    copy.chmod(0o640)
                    cases = junit_failures(path)
                    if cases:
                        failures[name] = {'stage': stage['name'], 'junit': str(copy.relative_to(attempt_dir)), 'count': len(cases),
                                          'truncated': len(cases) > FAILURES_PER_SUITE, 'cases': cases[:FAILURES_PER_SUITE]}
            stages.append(entry)
            if status != 'passed' and stage.get('stop_on_failure', True):
                break
        reasons = judge(policy, stages, junit, _baseline(queue, repo))
        totals = {k: sum(c[k] for c in junit.values()) for k in ('tests', 'passed', 'failed', 'errors', 'skipped')}
        gate_suite = junit.get(policy['suites'].get('deadline_gate_suite', ''))
        gate = None if gate_suite is None else ('passed' if not (gate_suite['failed'] or gate_suite['errors'] or gate_suite['skipped']) else 'failed')
        probe = next((s['status'] for s in stages if s['name'] == 'isolation-probe'), None)
        result.update(status='passed' if not reasons else 'failed', reasons=reasons, stages=stages, suites=junit, counts=totals,
                      deadline_gate=gate, isolation=probe)
        # Additive to nexloop-ci-result/1: where the failed names are (release.py reads sha, status, policy_version, deadline_gate
        # and counts only; the result's digest still covers this whole file).
        write_atomic(attempt_dir / 'failures.json', {'schema': FAILURES_SCHEMA, 'sha': sha, 'attempt': attempt, 'suites': failures})
        result['failures'] = {'file': 'failures.json', 'count': sum(f['count'] for f in failures.values()),
                              'truncated': any(f['truncated'] for f in failures.values())}
        shutil.rmtree(work)
    result['finished_at'], result['seconds'] = now(), round(time.monotonic() - t0, 3)
    first = next((json.loads(p.read_text()) for p in sorted((Path(queue) / 'results' / sha).glob('attempt-1/result.json'))), None)
    if first and first['status'] != 'passed':
        result['first_failure'] = {'attempt': 1, 'reasons': first.get('reasons')}
    write_atomic(attempt_dir / 'result.json', result)
    return result


def merge(repo, queue, request, policy, identities, *, gates=()):
    """D4: main only moves by fast-forward to a SHA with a passed result whose first parent is the current main, with a
    maintainer's acknowledgement when the candidate changes CI, policy, hooks or deployment."""
    sha = request['sha']
    reasons = []
    if identities.get(request['identity']) not in ('integrator', 'maintainer'):
        reasons.append('merge_requires_integrator')
    result = latest_result(queue, sha)
    if not result or result['status'] != 'passed':
        reasons.append('no_passed_result')
    main = git(repo, 'rev-parse', '--verify', '-q', 'refs/heads/main', check=False).stdout.strip()
    parents = git(repo, 'rev-list', '--parents', '-n', '1', sha).split()[1:]
    if not parents or parents[0] != main:
        reasons.append('first_parent_is_not_main')
    if result and result.get('policy_change') and not (Path(queue) / 'acks' / f'{sha}.json').exists():
        reasons.append('policy_change_not_acknowledged')
    for gate in gates:
        if subprocess.run(gate, cwd=repo, capture_output=True).returncode != 0:
            reasons.append('merge_gate_failed:' + Path(gate[0]).name)
    if reasons:
        return {'merged': False, 'sha': sha, 'reasons': reasons}
    # Compare-and-swap on the ref: a concurrent move of main fails the update instead of being overwritten.
    git(repo, 'update-ref', '-m', f'nexloop-ci merge {sha}', 'refs/heads/main', sha, main)
    record = {'sha': sha, 'previous_main': main, 'identity': request['identity'], 'merged_at': now(),
              'result': {'attempt': result['attempt'], 'counts': result.get('counts'), 'policy_version': result['policy_version']},
              'policy_change_ack': json.loads((Path(queue) / 'acks' / f'{sha}.json').read_text()) if result.get('policy_change') else None}
    write_atomic(Path(queue) / 'merges' / f'{_next_seq(Path(queue)):08d}-{sha}.json', record)
    return {'merged': True, **record}


def ack_policy(queue, sha, identity, identities):
    if identities.get(identity) != 'maintainer':
        return {'acknowledged': False, 'reason': 'maintainer_only'}
    write_atomic(Path(queue_dirs(queue)) / 'acks' / f'{sha}.json', {'sha': sha, 'maintainer': identity, 'acknowledged_at': now()})
    return {'acknowledged': True, 'sha': sha}


def run_once(repo, queue, policy, identities, *, runner=None, workroot=None, gates=()):
    """Take the oldest pending request under the single-build lock (D5: one build at a time)."""
    queue = queue_dirs(queue)
    with open(queue / 'controller.lock', 'a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'ran': False, 'reason': 'busy'}
        pending = sorted((queue / 'pending').glob('*.json'))
        if not pending:
            return {'ran': False, 'reason': 'empty'}
        path = pending[0]
        running = queue / 'running' / path.name
        os.rename(path, running)
        request = json.loads(running.read_text())
        if request['kind'] == 'merge':
            outcome = merge(repo, queue, request, policy, identities, gates=gates)
        else:
            existing = latest_result(queue, request['sha'])
            if existing is not None and existing['status'] == 'passed':
                outcome = {**existing, 'reused': True}  # same SHA, same trusted policy: no second build
            else:
                outcome = run_check(repo, queue, request, policy, identities, runner=runner, workroot=workroot)
        os.rename(running, queue / 'done' / path.name)
        return {'ran': True, 'kind': request['kind'], 'outcome': outcome}


# §6 GitHub sync state (AT-053) -------------------------------------------------------------------------------------------
def _sync_push(repo, remote, sha, ref, *, timeout, env):
    """Push one exact SHA to one remote ref, never forced: 'synced', 'refused' (the remote refuses it, e.g. not a fast-forward) or
    'unreachable' (no answer, a transport error or a timeout)."""
    try:
        pushed = subprocess.run(['git', '-C', str(repo), 'push', '--porcelain', remote, f'{sha}:{ref}'],
                                capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return 'unreachable'
    if pushed.returncode == 0:
        return 'synced'
    # --porcelain prints one line per ref the remote answered; '!' is a refusal (non-fast-forward, fetch first, hook).
    if any(line.startswith('!') and ref in line for line in pushed.stdout.splitlines()):
        return 'refused'
    return 'unreachable'


def sync(repo, queue, remote, *, timeout=30, tag_signers=None):
    """Push main and every review/* branch to the sync remote: each as the exact SHA read once (a merge that lands meanwhile is
    the next round's), fast-forward only, never force, never mirror, never a delete. Unreachable: sync_pending with the backlog
    counted; CI results and merges are not affected and nothing blocks. A review branch the remote refuses (not a fast-forward)
    is that branch's sync_refused; main and the other branches go on. A review branch deleted here stays on the remote
    (local_absent). State: main at the top level (as before), every review branch under 'review'."""
    queue = queue_dirs(queue)
    state_path = queue / 'sync' / f'{remote}.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {'remote': remote, 'last_synced': None}
    main = git(repo, 'rev-parse', 'refs/heads/main')  # the SHA pushed and recorded, never re-read after the push
    env = {**_stage_env({}), 'GIT_ALLOW_PROTOCOL': 'file:ssh:https'}
    result = _sync_push(repo, remote, main, 'refs/heads/main', timeout=timeout, env=env)
    if result == 'synced':
        state.update(status='synced', last_synced=main, backlog=0, synced_at=now())
    else:
        since = state.get('last_synced')
        backlog = int(git(repo, 'rev-list', '--count', f'{since}..{main}' if since else main))
        state.update(status='sync_pending' if result == 'unreachable' else 'sync_refused', backlog=backlog, attempted_at=now(),
                     failures=state.get('failures', 0) + 1)
    reviews = state.get('review', {})
    listed = git(repo, 'for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads/review/')
    local = dict(line.split(' ', 1) for line in listed.splitlines() if line)
    for ref, sha in sorted(local.items()):
        name = ref.removeprefix('refs/heads/')
        entry = reviews.get(name, {'last_synced': None})
        # The remote unreachable for main: the same for every branch this round (no second timeout per branch).
        outcome = 'unreachable' if result == 'unreachable' else _sync_push(repo, remote, sha, ref, timeout=timeout, env=env)
        if outcome == 'synced':
            entry.update(status='synced', last_synced=sha, synced_at=now())
        else:
            entry.update(status='sync_pending' if outcome == 'unreachable' else 'sync_refused', sha=sha, attempted_at=now(),
                         failures=entry.get('failures', 0) + 1)
        reviews[name] = entry
    for name, entry in reviews.items():
        if 'refs/heads/' + name not in local:
            entry['status'] = 'local_absent'  # never deleted on the remote
    if reviews:
        state['review'] = reviews
    if tag_signers is not None:
        # Owner 2026-10-20 (v0.1.0): release tags, each re-verified (signed by the owner, annotated, in main) and pushed by its
        # own explicit refspec, create only: never --tags, never forced, never moved or deleted. A tag already synced with the
        # same object is not pushed again; a remote tag with another object is that tag's sync_refused.
        tags = state.get('tags', {})
        listed = git(repo, 'for-each-ref', '--format=%(refname) %(objectname)', 'refs/tags/')  # a pattern matches whole components
        for ref, obj in sorted(line.split(' ', 1) for line in listed.splitlines() if line.startswith('refs/tags/v')):
            name = ref.removeprefix('refs/tags/')
            entry = tags.get(name, {})
            if entry.get('status') == 'synced' and entry.get('object') == obj:
                continue
            problem = version_tag_problem(repo, obj, name, tag_signers)
            if problem:
                tags[name] = {'status': 'tag_unverified', 'reason': problem, 'object': obj}
                continue
            outcome = 'unreachable' if result == 'unreachable' else _sync_push(repo, remote, obj, ref, timeout=timeout, env=env)
            tags[name] = {'status': {'synced': 'synced', 'refused': 'sync_refused'}.get(outcome, 'sync_pending'), 'object': obj,
                          **({'synced_at': now()} if outcome == 'synced' else {'attempted_at': now()})}
        if tags:
            state['tags'] = tags
    write_atomic(state_path, state)
    return state


def status(queue):
    queue = queue_dirs(queue)
    names = lambda d: [p.name.split('-', 1)[1][:-5] for p in sorted((queue / d).glob('*.json'))]
    results = {}
    for sha_dir in sorted((queue / 'results').iterdir()):
        r = latest_result(queue, sha_dir.name)
        if r:results[sha_dir.name] = {'status': r['status'], 'attempt': r['attempt'], 'counts': r.get('counts')}
    def summary(state):
        out = {k: v for k, v in state.items() if k in ('status', 'backlog')}
        refused = sorted(name for name, entry in (state.get('review') or {}).items() if entry.get('status') == 'sync_refused')
        if refused:
            out['review_refused'] = refused
        held = sorted(name for name, entry in (state.get('tags') or {}).items() if entry.get('status') != 'synced')
        if held:
            out['tags_not_synced'] = held
        return out
    sync_state = {p.stem: summary(json.loads(p.read_text())) for p in (queue / 'sync').glob('*.json')}
    return {'pending': names('pending'), 'running': names('running'), 'results': results, 'sync': sync_state}


# §3.3 isolation probe ----------------------------------------------------------------------------------------------------
def isolation_probe(targets, *, timeout=2.0):
    """Every target must be unreachable: secret and backup paths unreadable, data, service and LAN ports refused or timed out,
    the public network unreachable. Only the target codes are reported, never an address or a path."""
    reachable = []
    for code, path in (targets.get('paths') or {}).items():
        try:
            if os.path.isdir(path):
                os.listdir(path)
            else:
                with open(path, 'rb') as stream:
                    stream.read(1)
            reachable.append(code)
        except OSError:
            pass
    for code, endpoint in (targets.get('tcp') or {}).items():
        host, port = endpoint.rsplit(':', 1)
        try:
            with socket.create_connection((host.strip('[]'), int(port)), timeout=timeout):
                reachable.append(code)
        except OSError:
            pass
    checked = sorted(list(targets.get('paths') or {}) + list(targets.get('tcp') or {}))
    return {'isolated': not reachable, 'reachable': sorted(reachable), 'checked': checked}


def _facts(path):
    """Private facts file: KEY=VALUE lines (SECRET_ROOT, RUNTIME_ROOT, ARTIFACT_ROOT, BACKUP_ROOT, DATA_HOSTS, …)."""
    out = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, _, val = line.partition('=')
            out[k.strip()] = val.strip()
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description='NexLoop local CI queue and controller (NX-033)')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('pre-receive', 'post-receive', 'enqueue', 'run-once', 'ack-policy', 'sync', 'status', 'result', 'check-runner', 'isolation-probe'):
        p = sub.add_parser(name)
        if name in ('pre-receive', 'post-receive', 'enqueue', 'run-once', 'sync'):p.add_argument('--repo', type=Path, required=True)
        if name not in ('pre-receive', 'check-runner', 'isolation-probe'):p.add_argument('--queue', type=Path, required=True)
        if name in ('pre-receive', 'enqueue', 'run-once', 'ack-policy'):p.add_argument('--identities', type=Path, required=True)
        if name in ('enqueue', 'ack-policy', 'result'):
            p.add_argument('--sha', required=True)
        if name == 'result':
            p.add_argument('--wait', type=float, default=0)
        if name in ('enqueue', 'ack-policy'):
            p.add_argument('--identity', required=True)
        if name == 'enqueue':
            p.add_argument('--ref', required=True)
            p.add_argument('--kind', choices=('check', 'merge'), default='check')
        if name == 'run-once':
            p.add_argument('--policy', type=Path, required=True)
            p.add_argument('--stages-version', type=int, choices=(1, 2, 3, 4), default=1)
            p.add_argument('--runner', type=Path)
            p.add_argument('--facts', type=Path)
            p.add_argument('--merge-gate', action='append', default=[])
        if name == 'sync':
            p.add_argument('--remote', required=True)
            p.add_argument('--timeout', type=float, default=30)
        if name in ('pre-receive', 'sync'):
            p.add_argument('--tag-signers', type=Path, help="the owner's SSH allowed signers file (release tags)")
        if name == 'check-runner':
            p.add_argument('--runner', type=Path, required=True)
            p.add_argument('--facts', type=Path, required=True)
        if name == 'isolation-probe':
            p.add_argument('--targets', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'pre-receive':
            identity = os.environ.get('NEXLOOP_PUSH_IDENTITY', '')
            refused = pre_receive(args.repo, load_identities(args.identities), identity, sys.stdin.read().splitlines(), args.tag_signers)
            out, code = {'accepted': not refused, 'refused': refused}, 1 if refused else 0
        elif args.command == 'post-receive':
            out, code = {'enqueued': post_receive(args.repo, args.queue, os.environ.get('NEXLOOP_PUSH_IDENTITY', ''), sys.stdin.read().splitlines())}, 0
        elif args.command == 'enqueue':
            out = enqueue(args.repo, args.queue, args.ref, args.sha, args.identity, args.kind, load_identities(args.identities))
            code = 0 if out['enqueued'] else 1
        elif args.command == 'run-once':
            runner = None
            if args.runner:
                runner = json.loads(args.runner.read_text())
                violations = check_runner(runner, _facts(args.facts) if args.facts else {})
                if violations:
                    print(json.dumps({'ran': False, 'reason': 'runner_refused', 'violations': violations}))
                    return 2
            out = run_once(args.repo, args.queue, load_policy(args.policy, args.stages_version), load_identities(args.identities), runner=runner,
                           gates=[g.split() for g in args.merge_gate])
            o = out.get('outcome') or {}
            code = 0 if (not out['ran'] or o.get('status') == 'passed' or o.get('merged')) else 1
        elif args.command == 'ack-policy':
            out = ack_policy(args.queue, args.sha, args.identity, load_identities(args.identities))
            code = 0 if out['acknowledged'] else 1
        elif args.command == 'sync':
            out, code = sync(args.repo, args.queue, args.remote, timeout=args.timeout, tag_signers=args.tag_signers), 0
        elif args.command == 'status':
            out, code = status(args.queue), 0
        elif args.command == 'result':
            deadline = time.monotonic() + args.wait
            while (out := latest_result(args.queue, args.sha)) is None and time.monotonic() < deadline:
                time.sleep(2)
            if out is None:
                out, code = {'sha': args.sha, 'status': 'not_available'}, 1
            else:
                code = 0 if out['status'] == 'passed' else 1
        elif args.command == 'check-runner':
            violations = check_runner(json.loads(args.runner.read_text()), _facts(args.facts))
            out, code = {'accepted': not violations, 'violations': violations}, 0 if not violations else 2
        else:
            out = isolation_probe(json.loads(args.targets.read_text()))
            code = 0 if out['isolated'] else 1
    except (ConfigError, ValueError, KeyError, OSError) as error:
        print(json.dumps({'error': type(error).__name__, 'detail': str(error) if isinstance(error, ConfigError) else None}))
        return 2
    print(json.dumps(out, sort_keys=True))
    return code


if __name__ == '__main__':
    sys.exit(main())
