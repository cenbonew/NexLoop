"""Proactive outreach slice 3 (docs/implementation/outreach-impl-plan.md; design §2, P2/P5/P8): the purpose list is trusted,
versioned configuration (v1 = deploy/configuration/outreach-purposes.v1.json, valid against the contract); the owner enables
or disables a listed purpose (governed Action, owner only); the dry run's purpose gate follows it. Nothing is sent."""
import json
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest
from jsonschema import Draft202012Validator

from commercial_fixture import publish_registry_actions
from commitment_fixture import TENANT, commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_http import ORIGIN
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_commitments_pg import fresh
from test_outreach_candidates_pg import candidates, gate
from test_restore_fence_pg import restoring  # noqa: F401
from test_workbench_actions_http import act
from test_workbench_read_pg import workbench_login

ROOT = Path(__file__).resolve().parents[1]
LIST = json.loads((ROOT / 'deploy/configuration/outreach-purposes.v1.json').read_text())
# v2 (owner 2026-10-21): renewal_reminder is a marketing purpose; v2 is the current list, v1 stays as published.
LIST2 = json.loads((ROOT / 'deploy/configuration/outreach-purposes.v2.json').read_text())
PLAN = pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)


def test_the_list_file_is_valid_against_the_contract():
    Draft202012Validator(json.loads((ROOT / 'packages/contracts/outreach-purposes.schema.json').read_text())).validate(LIST)
    assert [p['purpose_id'] for p in LIST['purposes']] == ['commitment_followup', 'service_followup', 'renewal_reminder', 'marketing_general']
    assert {p['purpose_id'] for p in LIST['purposes'] if p['default_enabled']} == {'commitment_followup', 'service_followup'}
    assert not any(p['requires_approval'] for p in LIST['purposes'])  # P5: no approval by default


def test_v2_makes_renewal_reminder_a_marketing_purpose_and_nothing_else():
    """Owner 2026-10-21 (NX-043 X1 review 2.3): renewal_reminder needs consent and is marketing; every other purpose is v1's."""
    Draft202012Validator(json.loads((ROOT / 'packages/contracts/outreach-purposes.schema.json').read_text())).validate(LIST2)
    assert LIST2['version'] == 2
    renewal = next(p for p in LIST2['purposes'] if p['purpose_id'] == 'renewal_reminder')
    assert {k: renewal[k] for k in ('kind', 'topic', 'frequency_class', 'requires_consent', 'default_enabled', 'trigger')} == {
        'kind': 'marketing', 'topic': 'marketing.renewal_reminder', 'frequency_class': 'marketing', 'requires_consent': True,
        'default_enabled': False, 'trigger': 'plan_reassess'}
    assert renewal['content_rules']['unsubscribe_notice_required'] is True
    assert [p for p in LIST2['purposes'] if p['purpose_id'] != 'renewal_reminder'] == [p for p in LIST['purposes'] if p['purpose_id'] != 'renewal_reminder']


@PLAN
def test_the_owner_enables_and_disables_listed_purposes_and_the_dry_run_follows(restoring, admin):
    c = restoring
    publish_registry_actions(admin, ('nexloop.outreach_purposes.set',))
    assert admin.execute('select definition from control.nexloop_outreach_purpose_lists where version=1').fetchone()[0] == LIST  # byte-equal content
    assert admin.execute('select definition from control.nexloop_outreach_purpose_lists where version=2').fetchone()[0] == LIST2
    fresh(c, due_in=timedelta(minutes=30))
    item = candidates(admin)['candidates'][0]
    assert gate(item, 'purpose') == {'gate': 'purpose', 'result': 'pass', 'detail': 'list v2'}
    unknown = admin.execute("select runtime.nexloop_outreach_gates(%s,'real',%s,'flash_sale',clock_timestamp())", (TENANT, c['consumer'])).fetchone()[0]
    assert next(g for g in unknown if g['gate'] == 'purpose') == {'gate': 'purpose', 'result': 'refuse', 'detail': 'purpose_not_allowed'}
    with c['app']() as client:
        csrf = workbench_login(client, c['operator_member']).json()['csrf_token']
        assert act(client, csrf, 'set_outreach_purpose', {'purpose_id': 'commitment_followup', 'enabled': False, 'reason': 'x'}).status_code == 403
        assert client.get('/api/v1/workbench/outreach-purposes', headers={'Origin': ORIGIN}).status_code == 403
    with c['app']() as client:
        csrf = workbench_login(client, c['owner_member']).json()['csrf_token']
        view = client.get('/api/v1/workbench/outreach-purposes', headers={'Origin': ORIGIN}).json()
        assert view['list_version'] == 2 and [(p['purpose_id'], p['enabled'], p['revision']) for p in view['purposes']] == [
            ('commitment_followup', True, 0), ('service_followup', True, 0), ('renewal_reminder', False, 0), ('marketing_general', False, 0)]
        refused = act(client, csrf, 'set_outreach_purpose', {'purpose_id': 'flash_sale', 'enabled': True, 'reason': '新目的'})
        assert refused.status_code == 409 and refused.json()['details'] == {'reason': 'unknown_purpose'}  # the owner never adds a purpose
        done = act(client, csrf, 'set_outreach_purpose', {'purpose_id': 'commitment_followup', 'enabled': False, 'reason': '暂停承诺跟进'})
        assert done.status_code == 200 and done.json()['revision'] == 1, done.text
        assert act(client, csrf, 'set_outreach_purpose', {'purpose_id': 'commitment_followup', 'enabled': False, 'reason': '再次'}).json()['changed'] is False
    item = candidates(admin)['candidates'][0]
    assert gate(item, 'purpose') == {'gate': 'purpose', 'result': 'refuse', 'detail': 'purpose_disabled'} and item['would_dispatch'] is False
    assert admin.execute('select purpose_id,enabled,revision,list_version from control.nexloop_outreach_purpose_events').fetchall() == [('commitment_followup', False, 1, 2)]
    with pytest.raises(psycopg.Error, match='append-only'), admin.transaction():
        admin.execute('delete from control.nexloop_outreach_purpose_events')
    with pytest.raises(psycopg.Error), admin.transaction():
        admin.execute("update control.nexloop_outreach_purpose_lists set published_by='x'")
