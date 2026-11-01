"""S90 (migration work_feed_defer; K0 4.2, S89 fix 2): a plan that cannot be reevaluated now waits without failing. A plan whose
own Run was denied for an exhausted budget is deferred (no attempt spent, never dead-lettered, no Run, nothing in the self window)
until Budget.set re-registers it; past MAX_DEFER_SECONDS a run of deferrals falls back to counted retries. Synthetic data only."""
from datetime import UTC, datetime, timedelta

import pytest
from psycopg.types.json import Jsonb

from nexloop_eios import plan_reevaluation
from runtime_effect_fixture import runtime_effect_plan  # noqa: F401
from test_plan_dispatch_denial_throttle_pg import deny
from test_plan_reevaluation_pg import FakeLauncher, feed, make_due, owner_change, planning, spec  # noqa: F401


def row(admin, key):
    return admin.execute('select status,attempts,last_code,deferred_since is not null,available_at>clock_timestamp() from runtime.nexloop_work_feed where item_key=%s', (key,)).fetchone()


def denied_plan(f, cap=2):
    """A plan whose first reevaluation Run was denied at dispatch for an exhausted budget."""
    admin = f['admin']; launcher = FakeLauncher(f['plan'])
    s = spec(f['plan'], settings={'self_window_seconds': 600, 'self_trigger_cap': cap, 'min_reassess_seconds': 0}); f['port']().establish(s)
    admin.execute("select authz.nexloop_plan_feed_touch(%s,'real',%s,%s,null)", (f['tenant'], s['plan_id'], Jsonb({'kind': 'reassess_due', 'cause': 'self'})))
    make_due(admin)
    assert f['worker'](launcher).run_once()['launched'] == 1
    (run_id,) = admin.execute('select run_id from runtime.nexloop_plan_runs where plan_id=%s', (s['plan_id'],)).fetchone()
    deny(admin, f['plan'], run_id, 'budget_exhausted')
    return s, launcher


def runs(admin, s):
    return admin.execute('select count(*) from runtime.nexloop_plan_runs where plan_id=%s', (s['plan_id'],)).fetchone()[0]


def test_an_exhausted_budget_defers_the_plan_and_never_dead_letters(planning):
    f = planning; admin = f['admin']
    s, launcher = denied_plan(f)
    (key, payload, _, _), = feed(admin)
    assert payload['triggers'][0]['kind'] == 'dispatch_denied' and payload['triggers'][0]['code'] == 'budget_exhausted'
    for _ in range(30):  # far beyond max_attempts (5) and the throttle's 20 retries
        make_due(admin)
        summary = f['worker'](launcher).run_once()
        assert summary['deferred'] == 1 and summary['launched'] == 0 and summary['retry'] == 0 and summary['dead_lettered'] == 0
    assert row(admin, key)[:4] == ('pending', 0, 'budget_exhausted', True)  # no attempt spent, a run of deferrals recorded
    assert runs(admin, s) == 1  # no new Run while deferred: nothing counts in the self window


def test_budget_set_lets_the_plan_continue_without_a_manual_touch(planning):
    f = planning; admin = f['admin']
    s, launcher = denied_plan(f)
    make_due(admin)
    assert f['worker'](launcher).run_once()['deferred'] == 1
    (key, _, _, future), = feed(admin)
    assert future
    now = datetime.now(UTC)
    owner_change(admin, f['tenant'], {'operation': 'set_budget', 'budget_kind': 'model', 'unit': 'USD', 'limit_amount': '50',
                                      'period_start': (now - timedelta(days=1)).isoformat(), 'period_end': (now + timedelta(days=30)).isoformat()}, 's90-budget-set')
    (key, payload, status, future), = feed(admin)
    assert payload['triggers'][0]['cause'] == 'external' and status == 'pending' and future is False  # re-registered, due now
    assert f['worker'](launcher).run_once()['launched'] == 1 and runs(admin, s) == 2


def test_a_run_of_deferrals_past_its_limit_falls_back_to_counted_retries(planning, monkeypatch):
    f = planning; admin = f['admin']
    s, launcher = denied_plan(f)
    make_due(admin)
    assert f['worker'](launcher).run_once()['deferred'] == 1
    (key, _, _, _), = feed(admin)
    admin.execute("update runtime.nexloop_work_feed set deferred_since=clock_timestamp()-interval '8 days' where item_key=%s", (key,))
    make_due(admin)
    summary = f['worker'](launcher).run_once()
    assert summary['deferred'] == 0 and summary['retry'] == 1
    assert row(admin, key)[:3] == ('pending', 1, 'budget_exhausted')  # counted (visible in feed attempts), deferred_since cleared
    assert admin.execute('select deferred_since from runtime.nexloop_work_feed where item_key=%s', (key,)).fetchone()[0] is None


def test_other_denials_stay_on_the_s89_throttle(planning):
    """control_snapshot_missing (and the stale codes) are not 'later': the S89 self throttle bounds them, counted as before."""
    f = planning; admin = f['admin']; launcher = FakeLauncher(f['plan'])
    s = spec(f['plan'], settings={'self_window_seconds': 600, 'self_trigger_cap': 1, 'min_reassess_seconds': 0}); f['port']().establish(s)
    admin.execute("select authz.nexloop_plan_feed_touch(%s,'real',%s,%s,null)", (f['tenant'], s['plan_id'], Jsonb({'kind': 'reassess_due', 'cause': 'self'})))
    make_due(admin)
    assert f['worker'](launcher).run_once()['launched'] == 1
    (run_id,) = admin.execute('select run_id from runtime.nexloop_plan_runs where plan_id=%s', (s['plan_id'],)).fetchone()
    deny(admin, f['plan'], run_id, 'control_snapshot_missing')
    make_due(admin)
    summary = f['worker'](launcher).run_once()
    assert summary['throttled'] == 1 and summary['deferred'] == 0


@pytest.mark.parametrize('payload,status', [
    ({'delay_seconds': 0, 'max_defer_seconds': 60, 'code': 'later'}, 'invalid'),
    ({'delay_seconds': 60, 'max_defer_seconds': 0, 'code': 'later'}, 'invalid'),
    ({'delay_seconds': 60, 'max_defer_seconds': 60, 'code': 'Bad Code'}, 'invalid'),
])
def test_defer_input_is_bounded(planning, payload, status):
    f = planning; admin = f['admin']
    s = spec(f['plan']); f['port']().establish(s)
    admin.execute("select authz.nexloop_plan_feed_touch(%s,'real',%s,%s,null)", (f['tenant'], s['plan_id'], Jsonb({'kind': 'reassess_due', 'cause': 'self'})))
    make_due(admin)
    worker = f['worker'](FakeLauncher(f['plan']))
    (item,) = worker.feed.claim(limit=1, lease_seconds=60)
    import psycopg
    with pytest.raises(psycopg.Error):
        worker.feed.defer(item_key=item['item_key'], fence=item['fence'], **payload)
    assert worker.feed.defer(item_key=item['item_key'], fence=item['fence'] + 1, code='later', delay_seconds=60, max_defer_seconds=60) == 'lease_lost'


def test_constants():
    assert plan_reevaluation.DEFER_SECONDS == 900 and plan_reevaluation.MAX_DEFER_SECONDS == 7 * 86400


@pytest.mark.parametrize('order,deferred', [(('control_snapshot_missing', 'budget_exhausted'), 1), (('budget_exhausted', 'control_snapshot_missing'), 0)])
def test_the_newest_trigger_decides(planning, order, deferred):
    """L6 Q2: authz.nexloop_plan_feed_touch keeps the newest trigger first; the defer looks at that one only. A self denial then an
    exhausted budget defers; the reverse does not. Guards against a silent change of the touch's concatenation order."""
    f = planning; admin = f['admin']
    s = spec(f['plan'], settings={'self_window_seconds': 600, 'self_trigger_cap': 5, 'min_reassess_seconds': 0}); f['port']().establish(s)
    for code in order:
        admin.execute("select authz.nexloop_plan_feed_touch(%s,'real',%s,%s,null)",
                      (f['tenant'], s['plan_id'], Jsonb({'kind': 'dispatch_denied', 'cause': 'self', 'code': code})))
    (_, payload, _, _), = feed(admin)
    assert [t['code'] for t in payload['triggers'][:2]] == list(reversed(order))  # newest first
    make_due(admin)
    summary = f['worker'](FakeLauncher(f['plan'])).run_once()
    assert summary['deferred'] == deferred and summary['launched'] == 1 - deferred
