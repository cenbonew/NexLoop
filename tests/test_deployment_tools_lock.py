"""NX-035 deploy slice: pgBackRest and restic are pinned in versions.lock.json `deployment_tools` (version and official source
only; nothing is downloaded) and registered in THIRD_PARTY_NOTICES.md through scripts/check_licenses.py, with their license
checked against the license policy."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import check_licenses as C  # noqa: E402


def test_both_tools_are_pinned_by_version_and_official_source_and_not_yet_installed():
    tools = C.deployment_tools(ROOT)
    assert set(tools) == {'pgbackrest', 'restic'}
    assert tools['pgbackrest']['license'] == 'MIT' and tools['restic']['license'] == 'BSD-2-Clause'
    assert tools['pgbackrest']['source'].startswith('https://github.com/pgbackrest/pgbackrest/releases/tag/release/' + tools['pgbackrest']['version'])
    assert tools['restic']['source'] == 'https://github.com/restic/restic/releases/tag/v' + tools['restic']['version']
    # Digests are recorded at installation (deploy runbook steps 2.1, 3.1), never guessed here.
    assert {tools['pgbackrest']['status'], tools['restic']['status']} == {'pinned_not_installed'}
    assert tools['pgbackrest']['package_sha256'] is None and tools['restic']['archive_sha256'] is None


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
