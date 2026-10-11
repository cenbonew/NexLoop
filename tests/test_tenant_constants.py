"""NX-052: synthetic test tenants are uuids named through tests/support/tenants.py; the pre-uuid literals do not come back."""
import re
import subprocess
import uuid
from pathlib import Path

import pytest

from support import tenants
from nexloop_eios import browser_test_profile

ROOT = Path(__file__).resolve().parents[1]
# synthetic-a-*/synthetic-b-* were tenant-derived identities; synthetic-other-* are owner/intent names, not tenants.
OLD = re.compile(r'(?<![\w-])(?:(?:synthetic-a|synthetic-b|nexloop-sandbox)(?![\w])|(?:synthetic-other|synthetic-tenant-[ab])(?![\w-]))')
# Deliberate: the claim contract negative (a non-uuid tenant is refused) and the frozen NX-019 extraction dataset, whose
# tenant strings only feed the pure extractor and never reach control.nexloop_tenants.
ALLOWED = {'tests/test_claim_contract.py', 'tests/test_tenant_constants.py', 'tests/support/tenants.py', 'tests/data/nx019_extraction_cases.json'}


def tracked(*patterns):
    return subprocess.run(['git', 'ls-files', *patterns], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()


def test_constants_are_canonical_uuids_and_shared_with_the_profiles():
    values = [tenants.TENANT_A, tenants.TENANT_B, tenants.TENANT_OTHER, tenants.SANDBOX_TENANT]
    assert all(str(uuid.UUID(v)) == v for v in values) and len(set(values)) == 4
    assert browser_test_profile.TENANT == tenants.SANDBOX_TENANT


def test_no_pre_uuid_tenant_literal_in_code_or_tests():
    hits = [(path, n) for path in tracked('tests', 'scripts', 'packages/eios-core/src', 'apps/*/test', 'apps/*/src') if path not in ALLOWED
            and path.endswith(('.py', '.ts', '.tsx', '.sql', '.json', '.sh'))
            for n, line in enumerate((ROOT / path).read_text(encoding='utf-8', errors='replace').splitlines(), 1) if OLD.search(line)]
    assert hits == []



def test_sql_tenant_positions_name_only_synthetic_tenants():
    # SQL text inside tests may carry the tenant uuid literally (tenant_id='...' or set_config('eios.tenant_id','...')).
    known = {tenants.TENANT_A, tenants.TENANT_B, tenants.TENANT_OTHER, tenants.SANDBOX_TENANT}
    position = re.compile(r"tenant_id(?:'\s*,\s*|\s*=\s*)'([0-9a-f-]{36})'")
    found = {m for path in tracked('tests') if path.endswith('.py') for m in position.findall((ROOT / path).read_text(encoding='utf-8'))}
    assert found and found <= known, found - known


def test_tenant_root_accepts_only_canonical_uuids(admin):
    """NX-052 1b: the CHECK on control.nexloop_tenants is validated and refuses non-uuid, uppercase and braced ids."""
    import psycopg
    from nexloop_eios.bootstrap import bootstrap
    bootstrap(admin)
    row = admin.execute("select convalidated from pg_constraint where conname='nexloop_tenants_tenant_id_uuid' and conrelid='control.nexloop_tenants'::regclass").fetchone()
    assert row == (True,)
    for bad in ('synthetic-a', tenants.TENANT_A.upper(), '{' + tenants.TENANT_A + '}', tenants.TENANT_A.replace('-', '')):
        with admin.transaction(), pytest.raises(psycopg.errors.CheckViolation):
            admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active')", (bad,))
    with admin.transaction():
        admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (tenants.TENANT_B,))
