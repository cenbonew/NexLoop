"""NX-036 D12 with grype 0.120.0 (schema v6 vulnerability database): the scan reads only the imported database (never
downloads one, never refuses on age itself), the database must be a valid database of the pinned schema, its build time
comes from the v6 descriptor, and the committed policy applied to an actual v6 scan of the locked Python base keeps the
approved waivers working and does not list CVE-2025-13836 for Python 3.12.15."""
import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts' / 'release'))
import release  # noqa: E402
from test_release_local import fake_tool  # noqa: E402

FIXTURE = ROOT / 'tests' / 'fixtures' / 'grype-v6-python-base.json'
BUILT = '2026-10-16T06:29:51Z'


def report():
    return json.loads(FIXTURE.read_text())


def scanner(tmp_path, monkeypatch, payload, code=0):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir(exist_ok=True)
    (tmp_path / 'report.json').write_text(json.dumps(payload))
    env_file = tmp_path / 'grype-env'
    fake_tool(bin_dir, 'grype', release.pinned_tools()['grype']['version'],
              f'env > {env_file}\ncat {tmp_path / "report.json"}\nexit {code}')
    monkeypatch.setenv('PATH', f'{bin_dir}:/usr/bin:/bin')
    monkeypatch.setenv('GRYPE_DB_AUTO_UPDATE', 'true')  # an ambient setting never reaches the scan
    return release._tool('grype', release.pinned_tools()), env_file


def test_the_lock_pins_a_grype_that_reads_the_current_database_schema():
    grype = release.pinned_tools()['grype']
    assert grype['version'] == '0.120.0' and grype['database_schema'] == 'v6'
    assert grype['replaces']['database_schema'] == 'v5'


def test_the_scan_uses_only_the_imported_database_and_reads_the_v6_build_time(tmp_path, monkeypatch):
    grype, env_file = scanner(tmp_path, monkeypatch, report())
    scanned, built = release.grype_scan(grype, tmp_path / 'x.spdx.json', release.pinned_tools(), 'backend')
    assert built == BUILT and len(scanned['matches']) == 285
    env = dict(line.split('=', 1) for line in env_file.read_text().splitlines() if '=' in line)
    assert env['GRYPE_DB_AUTO_UPDATE'] == 'false' and env['GRYPE_DB_VALIDATE_AGE'] == 'false'
    assert env['GRYPE_CHECK_FOR_APP_UPDATE'] == 'false'


@pytest.mark.parametrize('db', [
    {'built': BUILT},                                                             # 0.86.1 (v5) shape, no schema
    {'status': {'schemaVersion': '5', 'built': BUILT, 'valid': True}},           # a v5 database
    {'status': {'schemaVersion': 'v6.1.10', 'built': BUILT, 'valid': False}},    # grype says it is not valid
    {'status': {'schemaVersion': 'v60.0.1', 'built': BUILT, 'valid': True}},     # not the pinned schema
    {},
])
def test_a_database_that_is_not_a_valid_pinned_schema_database_is_refused(tmp_path, monkeypatch, db):
    payload = report()
    payload['descriptor']['db'] = db
    grype, _ = scanner(tmp_path, monkeypatch, payload)
    with pytest.raises(release.Refused, match='vulnerability database'):
        release.grype_scan(grype, tmp_path / 'x.spdx.json', release.pinned_tools(), 'backend')


def test_a_failed_scan_is_refused_never_a_pass(tmp_path, monkeypatch):
    # grype exits 1 without a database (or with a broken one); the scan is refused.
    grype, _ = scanner(tmp_path, monkeypatch, report(), code=1)
    with pytest.raises(release.Refused, match='backend vulnerability scan failed'):
        release.grype_scan(grype, tmp_path / 'x.spdx.json', release.pinned_tools(), 'backend')


def rows(scope):
    return release.findings(report(), scope)


def test_the_committed_waivers_still_apply_to_a_v6_scan_and_cve_2025_13836_is_not_listed():
    policy = release.load_vulnerability_policy()
    today = date(2026, 10, 17)
    found = rows('image:backend')
    assert {r['fix'] for r in found} <= {'fixed', 'not-fixed', 'wont-fix', 'unknown'}
    assert not [r for r in found if r['id'] == 'CVE-2025-13836']
    python = [r for r in found if r['package'] == 'python']
    assert python and {r['version'] for r in python} == {'3.12.15'} and not [r for r in python if r['severity'] in ('Critical', 'High')]
    stage = release.evaluate_vulnerabilities(found, policy, 'stage', BUILT, today)
    assert stage['problems'] == [] and stage['summary']['blocking'] == []
    # sqlite CVE-2025-7458 (wont-fix, same package version) is still matched by its backend waiver
    assert 'CVE-2025-7458' in stage['summary']['waived']
    # the v6 database no longer matches zlib1g for CVE-2023-45853: its waivers are unused (listed for cleanup), not failures
    assert 'CVE-2023-45853' in stage['summary']['cleanup'] and not [r for r in found if r['id'] == 'CVE-2023-45853']
    assert stage['summary']['listed'] == []  # no fixed Critical or High in the Python base
    public = release.evaluate_vulnerabilities(found, policy, 'public', BUILT, today)
    assert public['problems'] == [] and public['summary']['blocking'] == []


@pytest.mark.parametrize('today,fresh', [(date(2026, 10, 16), True), (date(2026, 10, 23), True), (date(2026, 10, 24), False)])
def test_public_freshness_counts_days_from_the_v6_build_time(today, fresh):
    policy = release.load_vulnerability_policy()
    verdict = release.evaluate_vulnerabilities(rows('image:backend'), policy, 'public', BUILT, today)
    assert ('vulnerability database missing or older than allowed' not in verdict['problems']) is fresh
    stage = release.evaluate_vulnerabilities(rows('image:backend'), policy, 'stage', BUILT, today)
    assert 'vulnerability database missing or older than allowed' not in stage['problems']  # stage only records the date
