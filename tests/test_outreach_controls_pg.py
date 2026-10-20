"""Proactive outreach slice 2 (0232; docs/implementation/outreach-impl-plan.md, owner decisions P3, P8, O1-O5): the tenant's
outreach controls are the owner's governed Action; switching marketing on confirms the consent mode and is recorded (O5); the
slice 1 dry run and the consumer's quiet-hours preference read the tenant's settings and time zone (O1). Over the workbench HTTP
entry with real owner and operator logins; clean catalog PostgreSQL; synthetic data. Nothing is sent: no outreach Action yet."""
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from commercial_fixture import publish_registry_actions
from commitment_fixture import TENANT, commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_commitments_pg import fresh
from test_contact_preferences_pg import statement
from test_outreach_candidates_pg import candidates, gate
from test_restore_fence_pg import restoring  # noqa: F401
from test_workbench_actions_http import act
from test_workbench_read_pg import workbench_login

pytestmark = pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)
DEFAULT = {'time_zone': 'Asia/Shanghai', 'quiet_hours': '21:00-09:00', 'per_24h': 1, 'per_7d': 2,
           'marketing': {'enabled': False, 'consent_mode': 'opt_out', 'unsubscribe_notice': '不想再收到这类消息，回复“退订”即可。', 'per_7d': 1, 'sender_label': ''}}


def settings(**change):
    marketing = {**DEFAULT['marketing'], **change.pop('marketing', {})}
    return {**DEFAULT, **change, 'marketing': marketing}


def window(zone, minutes=60):
    now = datetime.now(zone)
    return (now - timedelta(minutes=minutes)).strftime('%H:%M') + '-' + (now + timedelta(minutes=minutes)).strftime('%H:%M')


def set_controls(client, csrf, body, confirm=None, reason='调整外联控制'):
    return act(client, csrf, 'set_outreach_controls', {'settings': body, 'confirm_consent_mode': confirm, 'reason': reason})


def test_the_owner_sets_the_controls_and_switching_marketing_on_confirms_the_consent_mode(restoring, admin):
    c = restoring
    publish_registry_actions(admin, ('nexloop.outreach_controls.set',))
    with c['app']() as client:
        csrf = workbench_login(client, c['operator_member']).json()['csrf_token']
        assert set_controls(client, csrf, settings()).status_code == 403  # owner only
    with c['app']() as client:
        csrf = workbench_login(client, c['owner_member']).json()['csrf_token']
        for bad in (settings(time_zone='Mars/Base'), settings(quiet_hours='25:00-08:00'), settings(per_24h=1.5), settings(marketing={'per_7d': 3}),
                    settings(marketing={'consent_mode': 'implied'}), {**settings(), 'extra': 1}):
            refused = set_controls(client, csrf, bad)
            assert refused.status_code == 409 and refused.json()['details'] == {'reason': 'invalid'}, bad
        assert set_controls(client, csrf, settings()).json()['changed'] is False  # the defaults, unchanged
        # O5: marketing on needs the consent mode confirmed in the same request.
        on = settings(marketing={'enabled': True})
        assert set_controls(client, csrf, on).json()['details'] == {'reason': 'consent_mode_unconfirmed'}
        assert set_controls(client, csrf, on, confirm='opt_in').json()['details'] == {'reason': 'consent_mode_unconfirmed'}  # not the chosen mode
        done = set_controls(client, csrf, on, confirm='opt_out')
        assert done.status_code == 200 and done.json()['revision'] == 1, done.text
        assert set_controls(client, csrf, settings(marketing={'enabled': True, 'per_7d': 0})).json()['revision'] == 2  # already on: no confirmation
    events = admin.execute('select revision,consent_mode_confirmed,settings->\'marketing\'->>\'enabled\' from control.nexloop_outreach_control_events order by revision').fetchall()
    assert events == [(1, 'opt_out', 'true'), (2, None, 'true')]
    assert candidates(admin)['settings']['source'] == 'tenant' and candidates(admin)['settings']['revision'] == 2
    with pytest.raises(psycopg.Error, match='append-only'), admin.transaction():
        admin.execute('delete from control.nexloop_outreach_control_events')


def test_the_dry_run_and_the_preference_follow_the_tenant_settings(restoring, admin):
    c = restoring
    publish_registry_actions(admin, ('nexloop.outreach_controls.set',))
    c.configure_categories()
    fresh(c, due_in=timedelta(minutes=30))
    with c['app']() as client:
        csrf = workbench_login(client, c['owner_member']).json()['csrf_token']
        # Quiet hours around now in UTC, with the tenant in UTC: the dry run refuses; without quiet hours it passes.
        assert set_controls(client, csrf, settings(time_zone='UTC', quiet_hours=window(timezone.utc))).status_code == 200
        item = candidates(admin)['candidates'][0]
        assert gate(item, 'quiet_hours')['result'] == 'refuse'
        assert set_controls(client, csrf, settings(time_zone='UTC', quiet_hours=None, per_24h=0)).status_code == 200
        item = candidates(admin)['candidates'][0]
        assert gate(item, 'quiet_hours')['result'] == 'pass' and gate(item, 'frequency')['result'] == 'refuse'  # 0 per 24 h
        # The consumer's own quiet hours are read in the tenant time zone (O1): a window around now in UTC refuses.
        statement(c, 'contact_quiet_hours', window(timezone.utc))
        item = candidates(admin)['candidates'][0]
        assert gate(item, 'consumer_preference') == {'gate': 'consumer_preference', 'result': 'refuse', 'detail': 'preference_quiet_hours'}
        assert set_controls(client, csrf, settings(time_zone='Pacific/Kiritimati', quiet_hours=None)).status_code == 200  # UTC+14
        item = candidates(admin)['candidates'][0]
        assert gate(item, 'consumer_preference')['result'] == 'pass'
        assert gate(item, 'real_dispatch')['result'] == 'refuse' and item['would_dispatch'] is False  # still read only, still closed
