"""I35 diagnosis (scripts/ops/i35_owner_property_read.sql) on real PostgreSQL: the script runs read-only, and it shows that the owner
reads no Consumer v1 profile property - 0123 derives a Consumer property only inside a property group and v1 declares none. Synthetic
tenant only."""
from pathlib import Path

import pytest

from support.tenants import TENANT_A as TENANT
from test_workbench_read_pg import browser_business, conversations, identity, provision, published_action, uow, workbench  # noqa: F401

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/ops/i35_owner_property_read.sql'


def statements():
    text = SCRIPT.read_text().replace(":'tenant'", '%(tenant)s')
    body = [line for line in text.splitlines() if not line.startswith('\\') and not line.startswith('--')]
    return [s.strip() for s in '\n'.join(body).split(';') if s.strip() and s.strip() not in ('begin transaction read only', 'rollback')]


def test_the_diagnosis_runs_read_only_and_shows_the_consumer_profile_unreadable(workbench):
    w = workbench; admin = w['admin']
    provision(w)
    consumer = admin.execute("select object_id from ontology.objects where tenant_id=%s and world='real' and type_name='Consumer' limit 1",
                                                  (TENANT,)).fetchone()
    if consumer is None:
        pytest.skip('the workbench fixture created no Consumer')
    results = []
    with admin.transaction():
        admin.execute('set transaction read only')
        for sql in statements():
            results.append(admin.execute(sql, {'tenant': TENANT}).fetchall())
    per_type = {r[0]: r for r in results[-1]}
    assert per_type['Message'][1:4] == (True, 5, 5)  # the owner reads every Message field (0123)
    # The fixture's Consumer is a bare v1 (no declared property), so section 5 has no row for it; the profile property the stage
    # Consumer declares (consumer_profile_schema, no property group) is not derived for the owner although the object is.
    target = 'eios:{}:Consumer/' + consumer[0]  # the row's object_id
    may = "select authz.nexloop_workbench_member_may_read(%s,'real','owner',%s)"
    assert admin.execute(may, (TENANT, target.format('object'))).fetchone()[0] is True
    assert admin.execute(may, (TENANT, target.format('property') + '/display_name')).fetchone()[0] is False
