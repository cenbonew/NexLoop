"""NX-056 budget approval: a new budget is a loosening (Budget.loosen, another owner approves it; tests/test_budget_approval_pg.py covers
the approval). Tests that only need a budget to exist write it the way the approved loosening does - 0068's set_budget through
control.nexloop_nx022_owner_change - instead of going through an approval each time."""
import uuid

from psycopg.types.json import Jsonb


def approved_budget(admin, tenant, kind, unit, limit, period_start, period_end, world='real'):
    """The budget row as an approved loosening writes it; period_* are aware datetimes or UTC ISO text."""
    intent = 'budget-' + uuid.uuid4().hex
    text = lambda v: v if type(v) is str else v.isoformat()
    return admin.execute('select control.nexloop_nx022_owner_change(%s,%s,%s,%s,%s)', (tenant, world, 'synthetic-owner-principal', intent, Jsonb({
        'request_id': intent, 'operation': 'set_budget', 'budget_kind': kind, 'unit': unit, 'limit_amount': limit,
        'period_start': text(period_start), 'period_end': text(period_end)}))).fetchone()[0]
