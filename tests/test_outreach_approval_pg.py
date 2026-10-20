"""Proactive outreach slice 6 (docs/implementation/outreach-impl-plan.md; design P5): optional approval. A send for a purpose with
requires_approval waits as pending_approval; the governed nexloop.outreach.approve:1 approves or rejects it within its window, never
by the requester; approved is only accepted and the executor still admits it through every dispatch gate (the contact restriction
first); rejected and expired are terminal and the provider receives nothing. Clean catalog PostgreSQL, synthetic data, loopback
outbox; the real dispatch switch is opened only in this disposable database for the pass cases (as the slice 4 tests do)."""
import pytest
from psycopg.types.json import Jsonb

from commercial_fixture import publish_registry_actions
from commitment_fixture import TENANT, commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from nexloop_eios.workbench_roles import apply_members, compile_members
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_outreach_send_pg import NOTHING, _restrict, open_everything, outreach, row, send, tick  # noqa: F401
from test_restore_fence_pg import restoring  # noqa: F401
from test_workbench_actions_http import act
from test_workbench_read_pg import ROLES, add_human, members_file, workbench_login, write_facts

pytestmark = pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)
PURPOSE = 'approved_followup'


@pytest.fixture
def approvals(outreach, admin, tmp_path):
    """A second owner (the approver; the first owner requests) and a purpose list v2 with one purpose that needs approval."""
    c = outreach
    publish_registry_actions(admin, ('nexloop.outreach.approve',))
    second = add_human(admin, 'owner-second')
    members = members_file((c['owner_member'], 'owner'), (c['operator_member'], 'operator'), (second, 'owner'))
    apply_members(ROLES, members, database_url_file=tmp_path / 'configurator-dsn')
    write_facts(admin, compile_members(ROLES, members))
    current = admin.execute('select definition from control.nexloop_outreach_purpose_lists order by version desc limit 1').fetchone()[0]
    base = next(p for p in current['purposes'] if p['purpose_id'] == 'commitment_followup')
    nxt = {**current, 'version': current['version'] + 1,
           'purposes': current['purposes'] + [{**base, 'purpose_id': PURPOSE, 'topic': PURPOSE, 'requires_approval': True, 'default_enabled': True}]}
    admin.execute("""insert into control.nexloop_outreach_purpose_lists(version,definition,definition_digest,published_by)
        values(%s,%s,encode(sha256(convert_to(runtime.nexloop_canonical_json(%s),'UTF8')),'hex'),'synthetic-config')""",
                  (nxt['version'], Jsonb(nxt), Jsonb(nxt)))
    c['approver'] = second
    return c


def decide(c, intent, decision, *, member=None, reason='负责人审批'):
    with c['app']() as client:
        csrf = workbench_login(client, member or c['approver']).json()['csrf_token']
        return act(client, csrf, 'decide_outreach_approval', {'intent_id': intent, 'decision': decision, 'reason': reason})


def pending(c, member=None):
    with c['app']() as client:
        workbench_login(client, member or c['owner_member'])
        return client.get('/api/v1/workbench/outreach-approvals')


def approval(admin, intent):
    return admin.execute('select decision,decided_by from runtime.nexloop_outreach_approvals where intent_id=%s', (intent,)).fetchone()


def test_a_send_waits_for_approval_and_an_approved_one_still_passes_every_gate(approvals, admin, monkeypatch):
    c = approvals
    open_everything(c, admin, monkeypatch)
    accepted = send(c, purpose=PURPOSE)
    assert accepted.status_code == 200, accepted.text
    intent = accepted.json()['intent_id']
    assert accepted.json()['state'] == 'pending_approval'
    assert tick(c) == NOTHING and row(admin, intent)[0] == 'pending_approval' and c['outbox'].requests == []  # nothing before approval
    listed = pending(c)
    assert listed.status_code == 200, listed.text
    (item,) = listed.json()['pending']
    assert set(item) == {'intent_id', 'purpose_id', 'requested_at', 'expires_at', 'requested_by'} and item['intent_id'] == intent
    assert item['purpose_id'] == PURPOSE and item['requested_by'] == c['owner_member']['principal_id'] and '您好' not in listed.text
    assert listed.json()['window_seconds'] == 86400
    assert pending(c, c['operator_member']).status_code == 403
    # Four eyes: the requester never approves their own request; an operator holds no grant.
    own = decide(c, intent, 'approve', member=c['owner_member'])
    assert own.status_code == 409 and own.json()['details'] == {'reason': 'self_approval'}
    assert decide(c, intent, 'approve', member=c['operator_member']).status_code == 403
    assert row(admin, intent)[0] == 'pending_approval' and approval(admin, intent) == (None, None)
    ok = decide(c, intent, 'approve')
    assert ok.status_code == 200, ok.text
    assert ok.json()['state'] == 'accepted' and approval(admin, intent) == ('approved', c['approver']['principal_id'])
    assert decide(c, intent, 'approve').json()['replayed'] is True
    again = decide(c, intent, 'reject')
    assert again.status_code == 409 and again.json()['details'] == {'reason': 'not_pending'}
    assert pending(c).json()['pending'] == []
    assert tick(c) == {**NOTHING, 'admitted': 1, 'fulfilled': 1} and c['outbox'].requests == [('send', intent)]


def test_approval_is_not_an_exemption_from_the_contact_restriction(approvals, admin, monkeypatch):
    c = approvals
    open_everything(c, admin, monkeypatch)
    intent = send(c, purpose=PURPOSE).json()['intent_id']
    assert decide(c, intent, 'approve').status_code == 200
    _restrict(c, admin)
    assert tick(c) == {**NOTHING, 'refused': 1}
    assert row(admin, intent)[:2] == ('refused', 'contact_restricted') and c['outbox'].requests == []


def test_a_rejected_send_is_terminal(approvals, admin, monkeypatch):
    c = approvals
    open_everything(c, admin, monkeypatch)
    intent = send(c, purpose=PURPOSE).json()['intent_id']
    out = decide(c, intent, 'reject', reason='时机不合适')
    assert out.status_code == 200 and out.json()['state'] == 'refused'
    assert row(admin, intent)[:2] == ('refused', 'approval_rejected') and approval(admin, intent)[0] == 'rejected'
    assert tick(c) == NOTHING and c['outbox'].requests == []
    assert decide(c, intent, 'approve').status_code == 409
    replay = send(c, purpose=PURPOSE).json()  # the same request is the same intent
    assert (replay['intent_id'], replay['state'], replay['replayed']) == (intent, 'refused', True)


def _age(admin, intent):
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')  # the window, not the guard, is under test
        admin.execute("update runtime.nexloop_outreach_approvals set requested_at=requested_at-interval '2 days',expires_at=clock_timestamp()-interval '1 second' where intent_id=%s",
                      (intent,))


def test_an_approval_past_its_window_expires_at_the_decision_or_the_next_due_listing(approvals, admin, monkeypatch):
    c = approvals
    open_everything(c, admin, monkeypatch)
    late = send(c, purpose=PURPOSE).json()['intent_id']
    _age(admin, late)
    assert pending(c).json()['pending'] == []
    out = decide(c, late, 'approve')
    assert out.status_code == 200
    assert {k: out.json()[k] for k in ('intent_id', 'state', 'refusal_code', 'replayed')} == {'intent_id': late, 'state': 'refused', 'refusal_code': 'approval_expired', 'replayed': False}
    assert row(admin, late)[:2] == ('refused', 'approval_expired') and approval(admin, late) == ('expired', None)
    idle = send(c, purpose=PURPOSE, origin='commitment:synthetic-2').json()['intent_id']
    _age(admin, idle)
    assert tick(c) == NOTHING  # the executor's due listing ends it; nothing reaches the provider
    assert row(admin, idle)[:2] == ('refused', 'approval_expired') and c['outbox'].requests == []
    with pytest.raises(Exception, match='immutable'), admin.transaction():
        admin.execute("update runtime.nexloop_outreach_approvals set decision='approved' where intent_id=%s", (idle,))


def test_a_purpose_without_approval_is_unchanged_and_bad_decisions_are_refused(approvals, admin, monkeypatch):
    c = approvals
    open_everything(c, admin, monkeypatch)
    plain = send(c).json()
    assert plain['state'] == 'accepted' and admin.execute('select count(*) from runtime.nexloop_outreach_approvals').fetchone() == (0,)
    assert decide(c, plain['intent_id'], 'approve').json()['details'] == {'reason': 'not_pending'}
    assert decide(c, 'outreach:' + 'f' * 64, 'approve').status_code == 409
    assert decide(c, plain['intent_id'], 'maybe').status_code == 422
    assert decide(c, plain['intent_id'], 'approve', reason='').status_code == 422
