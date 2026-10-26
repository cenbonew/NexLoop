"""NX-035 deploy slice: pgBackRest, restic and rest-server are pinned in versions.lock.json `deployment_tools` (version and official source
only; nothing is downloaded) and registered in THIRD_PARTY_NOTICES.md through scripts/check_licenses.py, with their license
checked against the license policy."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import check_licenses as C  # noqa: E402


def test_both_tools_are_pinned_by_version_and_official_source_and_verified_before_installation():
    tools = C.deployment_tools(ROOT)
    assert set(tools) == {'pgbackrest', 'restic', 'rest_server'}
    assert tools['pgbackrest']['license'] == 'MIT' and tools['restic']['license'] == 'BSD-2-Clause'
    assert tools['pgbackrest']['source'].startswith('https://github.com/pgbackrest/pgbackrest/releases/tag/release/' + tools['pgbackrest']['version'])
    assert tools['restic']['source'] == 'https://github.com/restic/restic/releases/tag/v' + tools['restic']['version']
    # Digests are recorded once the official artifacts are downloaded and checked on the administration machine (2026-10-14,
    # P3 of the deploy-days sheet); the host installation checks them again (deploy runbook steps 2.1, 3.1).
    assert {tools['pgbackrest']['status'], tools['restic']['status']} == {'verified_not_installed'}
    for digest in (tools['pgbackrest']['package_sha256'], tools['restic']['archive_sha256']):
        assert len(digest) == 64 and set(digest) <= set('0123456789abcdef')
    assert tools['pgbackrest']['version'] in tools['pgbackrest']['package'] and tools['restic']['version'] in tools['restic']['archive']
    # rest-server (repo1 append-only, owner 2026-10-27): pending until the dispatcher has downloaded and checked the official archive;
    # pending means no digest at all (the installer refuses an empty one), verified means a full sha256.
    rest = tools['rest_server']
    assert rest['license'] == 'BSD-2-Clause' and rest['source'] == 'https://github.com/restic/rest-server/releases/tag/v' + rest['version']
    assert rest['archive'] == f"rest-server_{rest['version']}_linux_amd64.tar.gz"
    assert (rest['status'], rest['archive_sha256']) == ('pending_download', '') or (
        rest['status'] == 'verified_not_installed' and len(rest['archive_sha256']) == 64 and set(rest['archive_sha256']) <= set('0123456789abcdef'))


def test_the_license_check_refuses_an_unlisted_license_or_a_missing_field():
    policy = C.load_policy(ROOT / 'deploy/compliance/license-policy.v1.json')
    assert C.deployment_tool_problems(C.deployment_tools(ROOT), policy) == []
    assert C.deployment_tool_problems({'x': {'version': '1', 'source': 'https://example.invalid', 'license': 'GPL-3.0-only'}}, policy) == \
        ['deployment tool x: license GPL-3.0-only is not allowed']
    assert C.deployment_tool_problems({'y': {'version': '1', 'license': 'MIT'}}, policy) == ['deployment tool y: version, source and license required']


def test_the_notices_carry_exactly_the_rendered_deployment_tools_section():
    """The section check_licenses.py renders (the full-file comparison runs in the CI licenses stage) is the one in the file."""
    policy = C.load_policy(ROOT / 'deploy/compliance/license-policy.v1.json')
    rendered = C.render_notices([], {**policy, 'off_platform': []}, C.deployment_tools(ROOT))
    section = rendered[rendered.index('## Deployment tools'):]
    notices = (ROOT / 'THIRD_PARTY_NOTICES.md').read_text()
    assert section in notices
    assert notices.index('## Deployment tools') > notices.index('## Declared license without license text') and \
        notices.index('## Deployment tools') < notices.index('## Weak-copyleft dependencies')
    assert json.loads((ROOT / 'versions.lock.json').read_text())['deployment_tools']['decision']
