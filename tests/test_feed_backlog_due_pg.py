"""Work feed backlog age (migration feed_backlog_due; stage alert feed_backlog_age:commitment-monitor firing since 10-16).

feeds.<feed>.oldest_pending_seconds and oldest_due_seconds count only items that are due and not held by a live lease, from
available_at. An item scheduled for later - the commitment monitor's next stage, rescheduled in place with changed_at at its last pass -
is no backlog, whatever its age; once due and unclaimed it waits from available_at, not from when it was created or last changed.
feeds.<feed>.max_pending_attempts and rules v17 (feed_attempts_over_ceiling) report the crash loop the waiting age cannot see.
Real PostgreSQL through the NX-030 fixture (rules v1, then v17; the real alert evaluator); rows seeded by admin as synthetic operational
state, claims and the retry through the feed's real signed port."""
import json
from pathlib import Path

from eios.authz.operations import Operation
from eios.authz.resources import ResourceType

from multi_authority_fixture import seed_multi_authority
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.observability import apply_rules, load_rules, validate_rules
from nexloop_eios.work_feed import WorkFeed
from test_observability_pg import TENANT, observed  # noqa: F401
from test_workbench_read_pg import browser_business, conversations, identity, published_action, uow, workbench, workbench_login  # noqa: F401

FEEDS = ('commitment-monitor', 'reply-due')
V17 = Path(__file__).resolve().parents[1] / 'deploy/configuration/alert-rules.v17.json'


def seed(admin, feed, key, *, available, changed, lease=None):
    """One pending item: available_at, created_at/changed_at and lease_until as offsets from now (SQL intervals)."""
    admin.execute("""insert into runtime.nexloop_work_feed(tenant_id,world,feed,item_key,payload,created_at,changed_at,available_at,lease_until,lease_seq)
        values(%s,'real',%s,%s,'{}',clock_timestamp()+%s::interval,clock_timestamp()+%s::interval,clock_timestamp()+%s::interval,
               clock_timestamp()+%s::interval,case when %s::text is null then null else 1 end)""",
                  (TENANT, feed, key, changed, changed, available, lease, lease))


def feeds(admin):
    return admin.execute("select runtime.nexloop_metrics_snapshot(%s,'real')->'feeds'", (TENANT,)).fetchone()[0]


def backlog_alert(admin, feed):
    return admin.execute("select firing,last_value from control.nexloop_alert_state where rule_id='feed_backlog_age' and selector=%s",
                         (feed,)).fetchone()


def test_a_scheduled_item_is_no_backlog_however_old_its_last_change(observed):
    admin = observed['admin']
    admin.execute('delete from runtime.nexloop_work_feed where tenant_id=%s and feed = any(%s)', (TENANT, list(FEEDS)))
    # The stage case: commitment monitor items written 3.4 days ago (in place, the last pass), due again in 3.6 days.
    for n in range(3):
        seed(admin, 'commitment-monitor', f'commitment:scheduled-{n}', available='3 days 14 hours', changed='-3 days -10 hours')
    f = feeds(admin)['commitment-monitor']
    assert (f['pending'], f['due'], f['scheduled']) == (3, 0, 3)
    assert f['oldest_pending_seconds'] is None and f['oldest_due_seconds'] is None
    observed['evaluate']()
    assert backlog_alert(admin, 'commitment-monitor') in (None, (False, None))


def test_a_due_unclaimed_item_waits_from_available_at_and_a_live_lease_is_not_waiting(observed):
    admin = observed['admin']
    admin.execute('delete from runtime.nexloop_work_feed where tenant_id=%s and feed = any(%s)', (TENANT, list(FEEDS)))
    # Created a week ago, due 20 minutes ago, never claimed: it has waited 20 minutes, not a week.
    seed(admin, 'commitment-monitor', 'commitment:due', available='-20 minutes', changed='-7 days')
    seed(admin, 'commitment-monitor', 'commitment:later', available='2 days', changed='-7 days')
    f = feeds(admin)['commitment-monitor']
    assert (f['pending'], f['due'], f['scheduled']) == (2, 1, 1)
    assert 1195 <= f['oldest_pending_seconds'] <= 1260 and f['oldest_due_seconds'] == f['oldest_pending_seconds']
    observed['evaluate']()
    firing, value = backlog_alert(admin, 'commitment-monitor')
    assert firing is True and 1195 <= value <= 1260
    # Due 2 hours ago but held by a live lease: being worked on, not waiting. Within the threshold the feed reads as no backlog.
    seed(admin, 'reply-due', 'reply:leased', available='-2 hours', changed='-2 hours', lease='5 minutes')
    seed(admin, 'reply-due', 'reply:fresh', available='-1 minute', changed='-1 minute')
    r = feeds(admin)['reply-due']
    assert r['due'] == 2 and 55 <= r['oldest_pending_seconds'] <= 120
    # The lease expired (the worker is gone): the item waits again, from when it became due.
    admin.execute("update runtime.nexloop_work_feed set lease_until=clock_timestamp()-interval '1 second' where tenant_id=%s and item_key='reply:leased'", (TENANT,))
    assert 7195 <= feeds(admin)['reply-due']['oldest_pending_seconds'] <= 7260
    # Once nothing is due the value is null again and the evaluator resolves the alert.
    admin.execute('delete from runtime.nexloop_work_feed where tenant_id=%s and feed = any(%s) and available_at<=clock_timestamp()', (TENANT, list(FEEDS)))
    assert feeds(admin)['commitment-monitor']['oldest_pending_seconds'] is None
    observed['evaluate']()
    assert backlog_alert(admin, 'commitment-monitor')[0] is False


def test_rules_v17_are_v16_plus_the_attempts_ceiling_rule():
    root = Path(__file__).resolve().parents[1] / 'deploy/configuration'
    v16, v17 = json.loads((root / 'alert-rules.v16.json').read_text()), load_rules(root / 'alert-rules.v17.json')
    assert v17['version'] == 17 and v17['rules'][:len(v16['rules'])] == v16['rules'] and len(v17['rules']) == len(v16['rules']) + 1
    assert {k: v17['rules'][-1][k] for k in ('rule_id', 'metric', 'condition', 'threshold', 'severity')} == {
        'rule_id': 'feed_attempts_over_ceiling', 'metric': 'feeds.*.max_pending_attempts', 'condition': 'gt', 'threshold': 20, 'severity': 'critical'}
    validate_rules(v17)


def test_a_worker_that_dies_after_each_claim_is_reported_above_the_highest_retry_ceiling(observed):
    """L6: claimed, no retry and no complete, claimed again at each lease expiry - attempts grow and nothing dead-letters; the item is
    nearly always under a live lease, so the backlog age does not see it. Real claims through the feed's signed port."""
    o, admin = observed, observed['admin']
    assert apply_rules(load_rules(V17), TENANT, database_url_file=o['w']['configurator'])['configured'] is True
    admin.execute('delete from runtime.nexloop_work_feed where tenant_id=%s and feed = any(%s)', (TENANT, list(FEEDS)))
    _, token = seed_multi_authority(admin, o['domain'], [('eios:action:NexLoop.feed.commitment-monitor:1', ResourceType.ACTION, Operation.EXECUTE)],
                                    identity_suffix='-crash-loop-monitor')
    feed = WorkFeed(o['domain'], authenticate_service(o['domain'], token, world='real'), o['w']['f']['reader'].signer, feed='commitment-monitor')
    seed(admin, 'commitment-monitor', 'commitment:crash-loop', available='-1 minute', changed='-1 minute')
    assert feeds(admin)['commitment-monitor']['max_pending_attempts'] == 0
    expire = "update runtime.nexloop_work_feed set lease_until=clock_timestamp()-interval '1 second' where tenant_id=%s and item_key='commitment:crash-loop'"
    for attempt in range(1, 22):
        claimed = feed.claim(limit=1, lease_seconds=60)
        assert [(i['item_key'], i['attempts']) for i in claimed] == [('commitment:crash-loop', attempt)]
        f = feeds(admin)['commitment-monitor']
        assert f['max_pending_attempts'] == attempt and f['oldest_pending_seconds'] is None   # under a live lease: not waiting
        o['evaluate']()
        state = admin.execute("select firing,severity,last_value from control.nexloop_alert_state where rule_id='feed_attempts_over_ceiling' and selector='commitment-monitor'").fetchone()
        # Up to 20 a retry could still have explained it (every ceiling is at most 20); the 21st claim cannot.
        assert state == ((True, 'critical', 21) if attempt == 21 else (False, 'critical', attempt)), attempt
        assert backlog_alert(admin, 'commitment-monitor') in (None, (False, None))
        admin.execute(expire, (TENANT,))                                 # the worker died: no retry, no complete; the lease runs out
    # A legitimate outcome ends it: a retry at the ceiling dead-letters the item (feed_dead_letter takes over), the ceiling alert resolves.
    item = feed.claim(limit=1, lease_seconds=60)[0]
    assert feed.retry(item_key=item['item_key'], fence=item['fence'], code='synthetic_failure', delay_seconds=0, max_attempts=20) == 'dead_lettered'
    assert feeds(admin)['commitment-monitor']['max_pending_attempts'] == 0
    o['evaluate']()
    assert admin.execute("select firing from control.nexloop_alert_state where rule_id='feed_attempts_over_ceiling' and selector='commitment-monitor'").fetchone() == (False,)
