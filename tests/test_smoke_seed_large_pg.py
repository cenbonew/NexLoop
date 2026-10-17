"""The large synthetic seed of the workbench smoke stack (layout batch 2 acceptance, 2026-10-18): deterministic records of the
load template's shape, written through its governed entries, and visible on the workbench through the production app (owner
session): many consumers (multi-row tables, paging) and a long conversation."""
import os

from fastapi.testclient import TestClient

from conftest import admin, pg  # noqa: F401
from test_action_definitions import published_action  # noqa: F401
from test_browser_business_authorization import browser_business  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_conversation_messages import conversations  # noqa: F401
from smoke_seed import LONG_BODY, MULTILINE_BODY, large_records
from workbench_stack_fixture import workbench_stack


def test_large_records_are_deterministic_synthetic_and_varied():
    records = large_records()
    assert records == large_records() and len(records) == 40
    assert len({r['username'] for r in records}) == 40 and all(r['username'].startswith('seed-customer-') for r in records)
    assert len(records[0]['conversations']) == 1 and len(records[0]['conversations'][0]['messages']) == 60
    bodies = [m['body'] for r in records for c in r['conversations'] for m in c['messages']]
    assert LONG_BODY in bodies and MULTILINE_BODY in bodies and len(LONG_BODY) == 400
    assert {len(r['conversations']) for r in records} == {1, 2, 3}
    assert len({c['key'] for r in records for c in r['conversations']}) == sum(len(r['conversations']) for r in records)


def test_the_seeded_stack_shows_many_consumers_and_a_long_conversation_on_the_workbench(conversations, admin, pg, tmp_path, monkeypatch):  # noqa: F811
    from nexloop_eios.http_api import create_app
    monkeypatch.setenv('REAL_DISPATCH_ENABLED', 'false')
    web = tmp_path / 'web'
    web.mkdir()
    (web / 'index.html').write_text('<!doctype html><title>seed</title>')
    seed = large_records(customers=8, long_conversation_turns=12)
    config, _, _, credentials = workbench_stack(conversations, admin, pg, tmp_path, port=8443, web=web, seed=seed)
    assert credentials['seed'] == {'consumers': 8, 'conversations': sum(len(r['conversations']) for r in seed),
                                   'messages': sum(len(c['messages']) for r in seed for c in r['conversations'])}
    assert 'seed-customer-001' not in str(credentials)  # no seeded account is handed out
    origin = credentials['origin']
    with TestClient(create_app(config), base_url=origin) as client:
        login = client.post('/api/v1/workbench/auth/login', headers={'Origin': origin},
                            json={'username': credentials['owner']['username'], 'password': credentials['owner']['password']})
        assert login.status_code == 200
        first = client.get('/api/v1/workbench/consumers?limit=5', headers={'Origin': origin})
        assert first.status_code == 200 and len(first.json()['items']) == 5 and first.json()['next_cursor']  # paging
        listed = client.get('/api/v1/workbench/consumers?limit=50', headers={'Origin': origin}).json()['items']
        assert len(listed) == 9  # the fixture's customer and the eight seeded ones
        long = admin.execute("""select c.conversation_id from runtime.nexloop_conversations c
                                  join runtime.nexloop_conversation_messages m on m.conversation_id=c.conversation_id
                                 group by c.conversation_id having count(*)=12""").fetchall()
        assert len(long) == 1
        view = client.get('/api/v1/workbench/conversations/' + long[0][0], headers={'Origin': origin})
        assert view.status_code == 200 and len(view.json()['messages']) == 12
    assert os.environ['REAL_DISPATCH_ENABLED'] == 'false'
