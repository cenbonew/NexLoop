"""NX-036 §3 and §6 (scripts/release/release.py): the vulnerability policy (D5, D6, D12 recommended values, provisional),
its link to `sbom` (missing tools are still never a pass), the source-SBOM cross-check with the license inventory, and the
nine-step public preflight in which every missing condition fails. Stand-in tools and runners only; nothing is tagged,
published or sent anywhere."""
import json
import os
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts' / 'release'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import release  # noqa: E402
from test_release_local import cli, fake_tool, inputs, make  # noqa: E402,F401

TODAY = date(2026, 10, 11)


def policy(**changes):
    value = release.load_vulnerability_policy()
    value.update(changes)
    return value


def finding(identifier='CVE-2026-0001', package='libx', version='1.2.3', severity='Critical', fix='fixed', scope='source'):
    return {'id': identifier, 'package': package, 'version': version, 'severity': severity, 'fix': fix, 'scope': scope}


def waiver(**changes):
    value = {'id': 'CVE-2026-0001', 'package': 'libx', 'version_pattern': r'1\.2\..*', 'scope': 'source', 'reason': 'synthetic: not reachable',
             'approved_by': 'owner decision (synthetic)', 'created': '2026-10-01', 'expires': '2026-12-01'}
    value.update(changes)
    return value


def test_the_committed_policy_has_the_recommended_provisional_values():
    p = release.load_vulnerability_policy()
    assert p['thresholds']['stage']['block'] == [] and p['thresholds']['public']['block'] == [{'severity': 'Critical', 'fix': 'fixed'}]
    assert p['database_max_age_days'] == {'stage': None, 'public': 7} and p['waiver_max_days'] == 90 and p['waivers'] == []
    assert 'pending' in p['decision'] and all(d in p['decision'] for d in ('D5', 'D6', 'D12'))


def test_stage_lists_but_does_not_block_and_public_blocks_an_unwaived_fixable_critical():
    rows = [finding(), finding('CVE-2026-0002', severity='High'), finding('CVE-2026-0003', severity='Critical', fix='not-fixed'), finding('CVE-2026-0004', severity='Low')]
    stage = release.evaluate_vulnerabilities(rows, policy(), 'stage', None, TODAY)
    assert stage['problems'] == [] and stage['summary']['listed'] == ['CVE-2026-0001', 'CVE-2026-0002']
    public = release.evaluate_vulnerabilities(rows, policy(), 'public', '2026-10-10T00:00:00Z', TODAY)
    assert public['summary']['blocking'] == ['CVE-2026-0001'] and public['summary']['listed'] == ['CVE-2026-0002']
    assert any('above the public threshold' in p for p in public['problems'])


def test_a_valid_waiver_clears_exactly_its_finding_and_an_unused_one_is_listed_for_cleanup():
    rows = [finding()]
    verdict = release.evaluate_vulnerabilities(rows, policy(waivers=[waiver(), waiver(id='CVE-2026-0999')]), 'public', '2026-10-10', TODAY)
    assert verdict['problems'] == [] and verdict['summary']['waived'] == ['CVE-2026-0001'] and verdict['summary']['cleanup'] == ['CVE-2026-0999']
    other_version = release.evaluate_vulnerabilities([finding(version='2.0.0')], policy(waivers=[waiver()]), 'public', '2026-10-10', TODAY)
    assert other_version['summary']['blocking'] == ['CVE-2026-0001']


@pytest.mark.parametrize('changes,reason', [({'expires': '2026-10-10'}, 'expired'), ({'created': '2026-08-01', 'expires': '2026-11-15'}, 'outside the allowed period'),
                                            ({'created': '2026-10-20'}, 'outside the allowed period'), ({'reason': ''}, 'malformed'),
                                            ({'version_pattern': '('}, 'malformed'), ({'expires': 'soon'}, 'malformed')])
def test_an_expired_or_invalid_waiver_fails_every_category(changes, reason):
    for category in ('stage', 'public'):
        verdict = release.evaluate_vulnerabilities([], policy(waivers=[waiver(**changes)]), category, '2026-10-10', TODAY)
        assert any(reason in p for p in verdict['problems'])
    extra = waiver()
    extra['note'] = 'x'
    assert any('malformed' in p for p in release.evaluate_vulnerabilities([], policy(waivers=[extra]), 'stage', None, TODAY)['problems'])


@pytest.mark.parametrize('built', [None, '', 'yesterday', '2026-10-03T00:00:00Z'])
def test_public_needs_a_vulnerability_database_at_most_seven_days_old(built):
    assert any('database' in p for p in release.evaluate_vulnerabilities([], policy(), 'public', built, TODAY)['problems'])
    assert release.evaluate_vulnerabilities([], policy(), 'stage', built, TODAY)['problems'] == []
    assert release.evaluate_vulnerabilities([], policy(), 'public', '2026-10-04T12:00:00Z', TODAY)['problems'] == []


def test_findings_keep_identifiers_only():
    report = {'matches': [{'vulnerability': {'id': 'GHSA-abcd-2345-wxyz', 'severity': 'High', 'fix': {'state': 'fixed'}, 'description': 'SECRET-TEXT'},
                           'artifact': {'name': 'libx', 'version': '1.0'}},
                          {'vulnerability': {'id': 'free text; rm -rf', 'severity': 'Weird'}, 'artifact': {}}]}
    rows = release.findings(report, 'image:api')
    assert 'SECRET-TEXT' not in json.dumps(rows)
    assert {(r['id'], r['severity'], r['fix']) for r in rows} == {('GHSA-abcd-2345-wxyz', 'High', 'fixed'), ('UNKNOWN', 'Unknown', 'unknown')}


def test_sbom_writes_findings_and_refuses_on_an_expired_waiver_but_still_needs_the_tools(inputs, monkeypatch):
    make(inputs)
    rel = inputs['tmp'] / 'release'
    pinned = release.pinned_tools()
    bin_dir = inputs['tmp'] / 'bin'
    bin_dir.mkdir()
    fake_tool(bin_dir, 'syft', pinned['syft']['version'], 'for a in "$@"; do case "$a" in spdx-json=*) echo \'{"spdxVersion":"SPDX-2.3"}\' > "${a#spdx-json=}";; esac; done')
    report = json.dumps({'matches': [{'vulnerability': {'id': 'CVE-2026-0001', 'severity': 'Critical', 'fix': {'state': 'fixed'}, 'description': 'SECRET-TEXT'},
                                      'artifact': {'name': 'libx', 'version': '1.2.3'}}], 'descriptor': {'db': {'built': '2026-10-10T00:00:00Z'}}})
    fake_tool(bin_dir, 'grype', pinned['grype']['version'], f"echo '{report}'")
    monkeypatch.setenv('PATH', f'{bin_dir}:/usr/bin:/bin')
    result = release.sbom(rel)
    assert result['scan']['policy']['listed'] == ['CVE-2026-0001'] and result['scan']['policy']['category'] == 'stage'
    stored = json.loads((rel / 'sbom' / 'nexloop-api.findings.json').read_text())
    assert stored[0]['id'] == 'CVE-2026-0001' and 'SECRET-TEXT' not in json.dumps(stored)
    expired = policy(waivers=[waiver(expires='2026-01-01', created='2025-12-01')])
    monkeypatch.setattr(release, 'load_vulnerability_policy', lambda root=None: expired)
    with pytest.raises(release.Refused, match='expired'):
        release.sbom(rel)
    monkeypatch.setenv('PATH', '/usr/bin:/bin')
    with pytest.raises(release.NotInstalled):
        release.sbom(rel)


def spdx(*purls):
    return {'packages': [{'externalRefs': [{'referenceType': 'purl', 'referenceLocator': p}]} for p in purls]}


def test_the_source_sbom_and_the_license_inventory_must_list_the_same_third_party_packages():
    inventory = [{'ecosystem': 'python', 'name': 'Liba', 'version': '1.0'}, {'ecosystem': 'node', 'name': '@scope/left', 'version': '1.0.0'}]
    same = spdx('pkg:pypi/liba@1.0', 'pkg:npm/%40scope/left@1.0.0', 'pkg:pypi/nexloop-eios-core@0.1.0', 'pkg:golang/x@1')
    assert release.cross_check(same, inventory, {'nexloop-eios-core'}) == []
    problems = release.cross_check(spdx('pkg:pypi/liba@1.0', 'pkg:npm/extra@2.0.0'), inventory, set())
    assert problems == ['in the license inventory but not in the SBOM: node @scope/left@1.0.0', 'in the SBOM but not in the license inventory: node extra@2.0.0']


class Runner:
    """Stand-in for subprocess: git commands run for real, the dependency phase and the checks return chosen exit codes."""

    def __init__(self, fail=()):
        self.fail, self.calls = set(fail), []

    def __call__(self, command, cwd):
        self.calls.append((command, cwd))
        if command[0] == 'git':
            return release._run(command, cwd)
        joined = ' '.join(command)
        if 'check_licenses.py' in joined:
            Path(command[command.index('--report') + 1]).write_text(json.dumps([
                {'ecosystem': 'python', 'name': 'liba', 'version': '1.0', 'scope': 'runtime', 'license': 'LGPL-3.0-only', 'category': 'conditional', 'source': 'x'}]))
        for name in self.fail:
            if name in joined:
                return 1, ''
        return 0, ''


def sbom_ok():
    return spdx('pkg:pypi/liba@1.0'), [finding(severity='High')], '2026-10-10T00:00:00Z'


def preflight(tmp_path, runner, maker=sbom_ok, previous='HEAD~1', **kw):
    return release.public_preflight('HEAD', tmp_path / 'out', previous_public=previous, runner=runner, sbom_maker=maker, today=TODAY, **kw)


def test_public_preflight_passes_only_when_all_nine_steps_pass_and_summarises_for_the_owner(tmp_path):
    runner = Runner()
    report = preflight(tmp_path, runner)
    assert report['ok'] is True and list(report['steps']) == list(release.PREFLIGHT_STEPS) and len(release.PREFLIGHT_STEPS) == 9
    summary = report['steps']['summary']['detail']['for_owner_confirmation']
    assert summary['listed'] == ['CVE-2026-0001'] and summary['conditional_licenses'] == ['liba 1.0 (LGPL-3.0-only, runtime)']
    assert (tmp_path / 'out/checkout/scripts/check_publication.py').is_file() and (tmp_path / 'out/source.tar').is_file()
    assert not (tmp_path / 'out/checkout/.git').exists()  # tracked files only
    commands = [' '.join(c) for c, _ in runner.calls]
    assert any('check_public_files.py --public' in c for c in commands) and any('prepush-scan.sh' in c and 'HEAD~1' in c for c in commands)
    assert len(report['report_sha256']) == 64
    with pytest.raises(release.InputError):
        preflight(tmp_path, Runner())  # never reuses an earlier preflight's checkout


@pytest.mark.parametrize('failing,step', [('uv sync', 'clean_checkout'), ('check_licenses.py', 'licenses'), ('check_public_files.py', 'public_files'),
                                          ('check_ci_isolation.py', 'ci_isolation'), ('check_publication.py', 'publication_scan'), ('prepush-scan.sh', 'history_scan')])
def test_any_failing_step_fails_the_preflight(tmp_path, failing, step):
    report = preflight(tmp_path, Runner([failing]))
    assert report['ok'] is False and report['steps'][step]['ok'] is False and report['steps']['summary']['ok'] is False


def test_missing_conditions_fail_the_preflight(tmp_path):
    first = preflight(tmp_path / 'a', Runner(), previous=None)
    assert first['ok'] is False and 'full-history audit' in first['steps']['history_scan']['detail']['refused']

    def missing_tool():
        raise release.NotInstalled('syft not installed')
    no_tool = preflight(tmp_path / 'b', Runner(), maker=missing_tool)
    assert no_tool['steps']['source_sbom']['ok'] is False and no_tool['steps']['vulnerabilities']['ok'] is False and no_tool['ok'] is False
    stale = preflight(tmp_path / 'c', Runner(), maker=lambda: (spdx('pkg:pypi/liba@1.0'), [], None))
    assert stale['steps']['vulnerabilities']['ok'] is False
    drift = preflight(tmp_path / 'd', Runner(), maker=lambda: (spdx('pkg:pypi/liba@1.0', 'pkg:pypi/other@2'), [], '2026-10-10'))
    assert drift['steps']['source_sbom']['ok'] is False
    critical = preflight(tmp_path / 'e', Runner(), maker=lambda: (spdx('pkg:pypi/liba@1.0'), [finding()], '2026-10-10'))
    assert critical['steps']['vulnerabilities']['ok'] is False and critical['steps']['summary']['detail']['for_owner_confirmation']['blocking'] == ['CVE-2026-0001']
    (tmp_path / 'empty-release' / 'sbom').mkdir(parents=True)
    no_images = preflight(tmp_path / 'f', Runner(), release_dir=tmp_path / 'empty-release')
    assert no_images['steps']['vulnerabilities']['ok'] is False


def test_public_preflight_cli_is_a_refusal_without_the_tools(tmp_path):
    code, out = cli('public-preflight', '--ref', 'HEAD', '--out', tmp_path / 'out', env={**os.environ, 'PATH': '/usr/bin:/bin'})
    assert code == 1 and out['ok'] is False and out['steps']['source_sbom'] is False and out['steps']['history_scan'] is False
