"""UI backend X1-X4 (L9 workbench UX audit; migrations 0366-0368, temporary numbers): read-only fields for the workbench and WebChat.

X1  the approvals view names the requester (`requested_by_username`): the account of that membership in this tenant, null once the
    member was erased, never another tenant's account;
X2  the approvals view labels its summary fields (`summary_labels`);
X3  the consumers list carries `last_conversation_at` and sorts by it; a Consumer being erased has none and never takes part in the
    time order;
X4  the customer's own conversation list carries `started_at`, `last_message_at` and `message_count`.
Every field comes from the existing reads (wrapped, every check of the wrapped function unchanged). Synthetic data only.
"""
import pytest
from psycopg.types.json import Jsonb
from fastapi.testclient import TestClient

from nexloop_eios.http_api import create_app
from nexloop_eios.workbench_reads import WorkbenchQueries
from commercial_fixture import publish_registry_actions
from commitment_fixture import TENANT, commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_http import ORIGIN
from test_action_definitions import published_action  # noqa: F401
from test_browser_business_authorization import browser_business  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_conversation_messages import conversation, conversations  # noqa: F401
from test_nx056_approval_gate_pg import PLAN, gated  # noqa: F401
from test_nx056_approvals_view_pg import view
from test_real_dispatch_switch_pg import switching  # noqa: F401
from test_restore_fence_pg import restoring  # noqa: F401
from test_workbench_actions_http import act
from test_workbench_read_pg import workbench_login


def username(admin, member):
    return admin.execute('select username from control.nexloop_browser_accounts where tenant_id=%s and subject_id=%s', (TENANT, member['subject_id'])).fetchone()[0]


@PLAN
def test_x1_x2_the_approver_sees_whose_request_it_is_and_the_summary_labels(gated, admin):
    c = gated
    approval = c['request']().json()['approval_id']
    row = view(c, c['second'])['to_decide'][0]
    assert row['approval_id'] == approval and row['requested_by'] == c['owner_member']['principal_id']
    assert row['requested_by_username'] == username(admin, c['owner_member'])
    assert row['summary'] == {'scope_kind': 'tenant', 'enabled': True} and row['summary_labels'] == {'scope_kind': '范围', 'enabled': '开关'}
    mine = view(c, c['owner_member'])['mine'][0]  # the requester's own column carries the same fields
    assert mine['requested_by_username'] == username(admin, c['owner_member']) and mine['summary_labels'] == row['summary_labels']


@PLAN
def test_x1_an_erased_member_is_never_named_and_another_tenant_never_answers(gated, admin):
    c = gated
    c['request']()
    owner = c['owner_member']
    # The member erasure's own record (NX-058 slice E) is what the view reads; its workflow is tested in test_member_erasure_pg.
    admin.execute("insert into control.nexloop_workbench_member_erasures(tenant_id,subject_id,principals,erased_at) values(%s,%s,%s,clock_timestamp())",
                  (TENANT, owner['subject_id'], [owner['principal_id']]))
    row = view(c, c['second'])['to_decide'][0]
    assert row['requested_by'] == owner['principal_id'] and row['requested_by_username'] is None
    assert admin.execute('select runtime.nexloop_approval_requester_username(%s,%s)', ('another-tenant', c['second']['principal_id'])).fetchone() == (None,)
    assert admin.execute('select runtime.nexloop_approval_requester_username(%s,%s)', (TENANT, c['second']['principal_id'])).fetchone() == (username(admin, c['second']),)
    assert admin.execute("select control.nexloop_approval_summary_label('not_listed')").fetchone() == ('not_listed',)


@PLAN
def test_x1_x2_the_helpers_and_the_wrapped_functions_are_not_callable_by_the_application(gated, admin):
    """Only the wrappers are granted to nexloop_api; the helpers and the renamed originals are not."""
    for function in ('runtime.nexloop_approval_requester_username(text,text)', 'runtime.nexloop_approval_rows_labelled(text,jsonb)',
                     'runtime.nexloop_consumer_last_conversation_at(text,text,text)', 'control.nexloop_approval_summary_label(text)'):
        assert admin.execute("select has_function_privilege('nexloop_api',%s,'execute')", (function,)).fetchone() == (False,), function
    for wrapped in ('nexloop_workbench_observe_read_before_requester_labels', 'nexloop_workbench_read_before_last_conversation',
                    'nexloop_conversation_command_before_conversation_times'):
        assert admin.execute("select has_function_privilege('nexloop_api',%s,'execute')", (f'authz.{wrapped}(text,text,text,text,text)',)).fetchone() == (False,)


pytestmark_restore = pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)


def consumers(c, member, query=''):
    with c['app']() as client:
        workbench_login(client, member)
        response = client.get('/api/v1/workbench/consumers' + query, headers={'Origin': ORIGIN})
        assert response.status_code == 200, response.text
        return response.json()['items']


@pytestmark_restore
def test_x3_the_list_carries_the_latest_message_time_and_an_erased_consumer_has_none(restoring, admin):
    c = restoring
    consumer = c['consumer']
    # Seeded rows of the read's own tables (the governed message write is test_conversation_messages'): two messages of this customer.
    conv = 'f' * 64
    admin.execute("insert into runtime.nexloop_conversations(tenant_id,world,conversation_id,consumer_id,principal_id,idempotency_key,last_sequence) values(%s,'real',%s,%s,'synthetic-x3','synthetic-x3-key',2)",
                  (TENANT, conv, consumer))
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT,))  # the feed triggers' row security
    for n, at in ((1, '2026-10-27 01:00:00+00'), (2, '2026-10-28 02:30:00+00')):
        admin.execute("insert into runtime.nexloop_conversation_messages values(%s,'real',%s,%s,%s,%s,%s,%s)",
                      (TENANT, conv, n, f'{n:064x}', f'synthetic-x3-{n}', '0' * 64, Jsonb({'accepted_at': at})))
    expected = admin.execute("""select runtime.nexloop_commercial_ts(max((m.record->>'accepted_at')::timestamptz)) from runtime.nexloop_conversations t
        join runtime.nexloop_conversation_messages m using(tenant_id,world,conversation_id) where t.tenant_id=%s and t.consumer_id=%s""", (TENANT, consumer)).fetchone()[0]
    assert expected == '2026-10-28T02:30:00.000000Z'
    for query in ('', '?sort=last_conversation_at&order=desc'):
        items = {i['consumer_id']: i for i in consumers(c, c['owner_member'], query)}
        assert items[consumer]['last_conversation_at'] == expected and all('last_conversation_at' in i for i in items.values())
    publish_registry_actions(admin, ('nexloop.consumer.erase',))
    with c['app']() as client:
        csrf = workbench_login(client, c['owner_member']).json()['csrf_token']
        assert act(client, csrf, 'erase_consumer', {'consumer_id': consumer, 'reason': '合成删除'}).status_code == 200
    items = consumers(c, c['owner_member'], '?sort=last_conversation_at&order=desc')
    erased = [i for i in items if i['consumer_id'] == consumer]
    assert all(i['last_conversation_at'] is None for i in erased)
    assert [i['consumer_id'] for i in items if i['last_conversation_at'] is None] == [i['consumer_id'] for i in items[len(items) - len([x for x in items if x['last_conversation_at'] is None]):]]


class Reader:
    def __init__(self, rows):
        self.rows = rows

    def call(self, verb, **payload):
        assert verb == 'consumers'
        return {'consumers': [dict(r) for r in self.rows if 'after' not in payload or r['consumer_id'] > payload['after']][:payload['limit']]}

    def consumer_fields(self, consumer_id):
        return [{'name': 'display_name', 'present': False, 'readable': True}]


def test_x3_sort_by_latest_message_time_keeps_consumers_without_one_last_in_both_directions():
    row = lambda cid, at: {'consumer_id': cid * 64, 'restricted': False, 'open_commitments': 0, 'conversations': 1, 'active_plans': 0, 'last_conversation_at': at}  # noqa: E731
    rows = [row('a', '2026-10-27T01:00:00.000000Z'), row('b', None), row('c', '2026-10-28T01:00:00.000000Z'), row('d', '2026-10-26T01:00:00.000000Z'), row('e', None)]
    q = WorkbenchQueries(Reader(rows))
    order = lambda o: [i['consumer_id'][0] for i in q.consumers(sort='last_conversation_at', order=o)['items']]  # noqa: E731
    assert order('desc') == ['c', 'a', 'd', 'b', 'e'] and order('asc') == ['d', 'a', 'c', 'b', 'e']


def test_x3_the_sort_key_is_accepted_by_the_entry():
    from nexloop_eios.workbench_http import _CONSUMER_SORTS
    assert 'last_conversation_at' in _CONSUMER_SORTS


def test_x4_the_customers_conversation_list_carries_its_times(conversations, admin):
    f = conversations
    port = f['port']
    created = conversation(f)
    [item] = port.list_conversations()['items']
    assert item['id'] == created['id'] and (item['started_at'], item['last_message_at'], item['message_count']) == (None, None, 0)
    first = port.accept_message(conversation_id=created['id'], idempotency_key='synthetic-message-key-1', body='第一句')['message']
    second = port.accept_message(conversation_id=created['id'], idempotency_key='synthetic-message-key-2', body='第二句')['message']
    [item] = port.list_conversations()['items']
    assert (item['started_at'], item['last_message_at'], item['message_count']) == (first['accepted_at'], second['accepted_at'], 2)
    assert '第一句' not in str(item) and set(item) >= {'id', 'consumer_id', 'world_id', 'revision'}  # no body; the earlier fields unchanged
