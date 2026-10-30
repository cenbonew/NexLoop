"""Relay backlog settlement (S42 option A, migration relay_backlog_settle): real PostgreSQL and the real workbench. The owner settles
pending inbound messages that no relay will serve - the Consumer is erased, or has no active and serviceable relay enrollment - with
a rationale; the message becomes settled with an append-only audit row; operators are refused; a retry replays; anything else is
refused with its code. The NX-029 erasure settles the Consumer's pending messages itself. Synthetic tenants and members only."""
import secrets

import psycopg
import pytest
from psycopg.types.json import Jsonb

from commercial_fixture import publish_registry_actions
from erasure_support import mark_erasing
from support.tenants import TENANT_A as TENANT
from test_real_dispatch_switch_pg import owner_sql
from test_workbench_actions_http import act
from test_workbench_read_pg import (browser_business, conversations, identity, provision, published_action, uow, workbench,  # noqa: F401
                                    workbench_login)


def hexid():
    return secrets.token_hex(32)


def pending(admin, consumer, n=1):
    """n pending inbound messages of a Consumer in a Conversation of its own (rows as the message port leaves them)."""
    conversation = hexid()
    admin.execute("insert into runtime.nexloop_conversations(tenant_id,world,conversation_id,consumer_id,principal_id,idempotency_key) "
                  "values(%s,'real',%s,%s,'synthetic-human',%s)", (TENANT, conversation, consumer, 'settle-' + conversation[:12]))
    ids = []
    for sequence in range(1, n + 1):
        message = hexid()
        admin.execute("insert into runtime.nexloop_message_outbox(tenant_id,world,event_id,message_id,conversation_id,sequence,record) "
                      "values(%s,'real',%s,%s,%s,%s,%s)", (TENANT, hexid(), message, conversation, sequence,
                      Jsonb({'actor': 'consumer', 'body': '合成消息', 'accepted_at': '2026-10-22T12:00:00Z'})))
        ids.append(message)
    return ids


def status(admin, message):
    return admin.execute("select status from runtime.nexloop_message_outbox where message_id=%s", (message,)).fetchone()[0]


def audit(admin, message):
    return admin.execute("select reason,source,actor_principal,intent_id is not null,rationale from runtime.nexloop_relay_backlog_settlements "
                         "where message_id=%s", (message,)).fetchone()


@pytest.fixture
def backlog(workbench):
    w = workbench
    publish_registry_actions(w['admin'], ('nexloop.relay.backlog_settle',), tenant=TENANT)
    provision(w)
    return w


def test_the_owner_settles_unserviceable_messages_once_and_nobody_else_can(backlog):
    w = backlog; admin = w['admin']
    stale = pending(admin, hexid(), 2)  # a Consumer with no relay enrollment (customer-1 on stage)
    body = {'message_ids': stale, 'reason': 'S42 负责人决定：不再回复'}
    with w['client']() as client:
        csrf = workbench_login(client, w['operator']).json()['csrf_token']
        assert act(client, csrf, 'settle_relay_backlog', body).status_code == 403
    assert {status(admin, m) for m in stale} == {'pending'}
    with w['client']() as client:
        csrf = workbench_login(client, w['owner']).json()['csrf_token']
        done = act(client, csrf, 'settle_relay_backlog', body, key='wb-settle-0001-' + stale[0][:8])
        assert done.status_code == 200, done.text
        assert done.json()['count'] == 2 and done.json()['replayed'] is False
        assert {s['reason'] for s in done.json()['settled']} == {'no_serviceable_enrollment'}
        again = act(client, csrf, 'settle_relay_backlog', body, key='wb-settle-0001-' + stale[0][:8])
        assert again.status_code == 200 and again.json()['replayed'] is True
        # Settled under another request: no longer pending.
        other = act(client, csrf, 'settle_relay_backlog', body)
        assert other.status_code == 409 and other.json()['details'] == {'reason': 'not_pending'}
        unknown = act(client, csrf, 'settle_relay_backlog', {'message_ids': [hexid()], 'reason': '不存在'})
        assert unknown.status_code == 409 and unknown.json()['details'] == {'reason': 'unknown_message'}
        for bad in ({'message_ids': [], 'reason': 'x'}, {'message_ids': stale[:1] * 2, 'reason': 'x'}, {'message_ids': ['zz'], 'reason': 'x'},
                    {'message_ids': stale[:1]}, {'message_ids': stale[:1], 'reason': 'x', 'tenant_id': 'other'},
                    {'message_ids': [hexid() for _ in range(51)], 'reason': 'x'}):
            assert act(client, csrf, 'settle_relay_backlog', bad).status_code == 422, bad
    assert {status(admin, m) for m in stale} == {'settled'}
    reason, source, actor, has_intent, rationale = audit(admin, stale[0])
    assert (reason, source, actor, has_intent, rationale) == ('no_serviceable_enrollment', 'owner', w['owner']['principal_id'], True, 'S42 负责人决定：不再回复')
    with pytest.raises(psycopg.Error):  # the audit is append-only
        with admin.transaction():
            admin.execute("update runtime.nexloop_relay_backlog_settlements set rationale='x' where message_id=%s", (stale[0],))


def test_a_serviceable_consumer_is_refused_and_all_or_nothing(backlog):
    w = backlog; admin = w['admin']
    first, second = pending(admin, hexid())[0], pending(admin, hexid())[0]
    settle = "select control.nexloop_governed_relay_backlog_settle(%s,'real',%s,'human','intent-serviceable',%s)"
    body = Jsonb({'operation': 'settle_relay_backlog', 'request_id': 'r', 'message_ids': [first, second], 'reason': '测试'})
    # Fault injection on a disposable database: every Consumer has a relay profile now (an active, serviceable enrollment).
    with admin.transaction():
        admin.execute("create or replace function runtime.nexloop_relay_profile(p_tenant text,p_world text,p_consumer text,p_relay text) returns jsonb language sql as $$ select '{}'::jsonb $$")
        admin.execute('set local role nexloop_owner')
        with pytest.raises(psycopg.errors.InvalidParameterValue, match='relay backlog: consumer_serviceable'):
            with admin.transaction():
                admin.execute(settle, (TENANT, w['owner']['principal_id'], body))
        raise psycopg.Rollback()  # the stand-in profile goes with the transaction
    assert status(admin, first) == status(admin, second) == 'pending'
    # The world comes from the entry, never the body.
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='real_world_only'):
        owner_sql(admin, "select control.nexloop_governed_relay_backlog_settle(%s,'simulation',%s,'human','i',%s)", (TENANT, w['owner']['principal_id'], body))
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='not_owner'):
        owner_sql(admin, settle, (TENANT, w['operator']['principal_id'], body))


def test_the_erasure_settles_the_consumers_pending_messages_and_only_those(backlog):
    w = backlog; admin = w['admin']
    erased, kept = hexid(), hexid()
    gone, stays = pending(admin, erased, 3), pending(admin, kept)
    mark_erasing(admin, TENANT, erased)
    assert {status(admin, m) for m in gone} == {'settled'} and status(admin, stays[0]) == 'pending'
    assert audit(admin, gone[0]) == ('consumer_erased', 'erasure', None, False, None)
    # Settled by the erasure, the owner cannot settle it again.
    with w['client']() as client:
        csrf = workbench_login(client, w['owner']).json()['csrf_token']
        again = act(client, csrf, 'settle_relay_backlog', {'message_ids': gone[:1], 'reason': '重复'})
        assert again.status_code == 409 and again.json()['details'] == {'reason': 'not_pending'}


def test_the_owner_settles_the_backlog_with_one_command_and_a_rerun_replays(backlog, tmp_path, capsys):
    import io
    import json
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/deploy'))
    import relay_backlog_settle as T
    from test_browser_http import ORIGIN
    from test_real_dispatch_switch_tool import AppTransport, private
    w = backlog; admin = w['admin']
    ids = pending(admin, hexid(), 3) + pending(admin, hexid(), 52)  # two batches of 50 and 5
    cfg = private(tmp_path, 'settle.json', json.dumps({'schema': T.SCHEMA, 'confirmation_ref': 's42-settle:test-1', 'reason': '不回复', 'message_ids': ids}))
    user = private(tmp_path, 'user', w['owner']['username'] + '\n')

    def run(*extra):
        with w['client']() as client:
            code = T.main(['--origin', ORIGIN, '--password-stdin', '--username-file', str(user), '--config', str(cfg), *extra],
                          stdin=io.StringIO(w['owner']['password'] + '\n'), transport=AppTransport(client))
        return code, json.loads(capsys.readouterr().out)
    code, dry = run('--dry-run')
    assert code == 0 and dry['batches'] == [50, 5] and {status(admin, m) for m in ids} == {'pending'}
    code, out = run()
    assert code == 0 and [b['result'] for b in out['batches']] == ['settled', 'settled'] and {status(admin, m) for m in ids} == {'settled'}
    text = json.dumps(out, ensure_ascii=False)
    assert w['owner']['password'] not in text and '不回复' not in text
    code, again = run()
    assert code == 0 and [b['result'] for b in again['batches']] == ['replayed', 'replayed']
