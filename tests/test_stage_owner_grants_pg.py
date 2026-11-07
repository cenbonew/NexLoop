"""ADR-045 (R71 finding): the stage manifest's owner grant on a database where the global roles manifest is already published. The
merged roles carry their own version (global x 1000 + stage manifest version), so the template publication (0335, per version and
sha256) takes them as a new version instead of manifest_conflict; a replay is a replay; a later plain apply on that database is refused
as a version going back (the stage grant is never withdrawn silently). Real PostgreSQL with the workbench fixture."""
import json
from pathlib import Path

import psycopg
import pytest

from nexloop_eios.workbench_roles import WorkbenchRolesRejected, apply_members, with_stage_owner_actions
from test_workbench_read_pg import ROLES, browser_business, conversations, identity, members_file, published_action, uow, workbench  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / 'deploy/stage/capabilities.stage.v1.json'
GRANT = 'eios:action:nexloop.synthetic_fault.set:1'


def marker(tmp_path, environment):
    path = tmp_path / f'marker-{environment}.json'
    path.write_text(json.dumps({'schema': 'nexloop-deployment-marker/1', 'environment': environment}))
    path.chmod(0o600)
    return path


def test_the_stage_grant_applies_over_a_published_global_roles_manifest(workbench, tmp_path):
    w = workbench
    members = members_file((w['owner'], 'owner'), (w['operator'], 'operator'))
    plain = apply_members(ROLES, members, database_url_file=w['configurator'])  # the global manifest, as on stage before R71
    merged_roles = with_stage_owner_actions(ROLES, STAGE, marker(tmp_path, 'stage'))
    merged = apply_members(merged_roles, members, database_url_file=w['configurator'])  # R71: manifest_conflict before the fix
    assert merged_roles['manifest_version'] == ROLES['manifest_version'] * 1000 + json.loads(STAGE.read_text())['manifest_version']
    assert merged['roles_version'] == merged_roles['manifest_version'] > plain['roles_version']
    owner = w['admin'].execute("""select grant_facts::text from control.nexloop_workbench_role_templates where tenant_id=%s and role='owner'
        order by version desc limit 1""", (members['tenant_id'],)).fetchone()[0]
    assert GRANT.removeprefix('eios:action:').removesuffix(':1') in owner
    assert apply_members(merged_roles, members, database_url_file=w['configurator'])['role_templates'] == []  # a replay
    # A plain apply afterwards is refused (going back), never a silent withdrawal of the stage grant.
    with pytest.raises((WorkbenchRolesRejected, psycopg.Error)):
        apply_members(ROLES, members, database_url_file=w['configurator'])
