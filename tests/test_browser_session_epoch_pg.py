"""AUTH-DOS (browser_session_epoch): a browser session binds the account's session_epoch, not the account row's revision. Someone
else's wrong passwords and a good login elsewhere (its failure reset) raise the revision and leave the session and a held claim valid;
a lockout stops new password logins only (owner 2026-11-02, ADR-044 §4); a password reset, a kick (member change), a disabled account
and an erasure still end it. Real PostgreSQL, the actual app and browser session; synthetic data only."""
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from fastapi.testclient import TestClient

from nexloop_eios.browser_authorization import authenticate_browser_business
from support.tenants import TENANT_A
from test_browser_fingerprint_pg import account_payload, digest, inspect, snapshot
from test_browser_http import ORIGIN, browser, identity  # noqa: F401
from test_conversation_messages import browser_business, conversations, published_action, uow  # noqa: F401
from test_web_chat_http import actual_chat, create_actual  # noqa: F401


def setup(actual_chat, admin):
    from nexloop_eios.conversation_messages import ConversationMessagePort
    client, headers, fixture = actual_chat
    created = create_actual(client, headers)
    path = '/api/v1/conversations/'+created['id']+'/messages'
    backend = client.app.state.backend
    held = ConversationMessagePort(backend._pool, authenticate_browser_business(backend._pool, inspect(client)), backend._signer)
    account = account_payload(admin, fixture['principal'])
    token = digest(admin, fixture['principal'])
    return client, fixture, created['id'], path, held, account, token


def wrong_password(client, fixture):
    other = TestClient(client.app, base_url=ORIGIN)  # another browser: no cookie of the victim
    return other.post('/api/v1/auth/login', headers={'Origin': ORIGIN},
                      json={'username': fixture['base']['identity'][3].username, 'password': 'synthetic-wrong-password'}).status_code


def revision(admin, account):
    return admin.execute('select (payload->>\'revision\')::bigint from control.nexloop_browser_accounts where local_account_id=%s',
                         (account['local_account_id'],)).fetchone()[0]


def refused(client, fixture, conversation, path, held, token):
    from nexloop_eios.conversation_messages import ConversationUnavailable
    assert client.get(path).status_code != 200
    with pytest.raises(ConversationUnavailable):
        held.read_messages(conversation_id=conversation)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        snapshot(fixture, token)


def test_wrong_passwords_of_someone_else_below_the_lockout_keep_the_session(actual_chat, admin):
    client, fixture, conversation, path, held, account, token = setup(actual_chat, admin)
    before, hashed = revision(admin, account), snapshot(fixture, token)['directory_hash']
    for _ in range(4):  # N-1: the fifth failure locks (0023)
        assert wrong_password(client, fixture) == 401
    assert revision(admin, account) == before + 4  # the row version still moves (0023's receipts reconcile on it)
    assert client.get(path).status_code == 200
    assert held.read_messages(conversation_id=conversation)['items'] == []  # a claim held across the failures stays valid
    assert snapshot(fixture, token)['directory_hash'] == hashed
    # A good login elsewhere resets the failures (0024, revision + 1): the first session still holds.
    assert TestClient(client.app, base_url=ORIGIN).post('/api/v1/auth/login', headers={'Origin': ORIGIN}, json={
        'username': fixture['base']['identity'][3].username, 'password': fixture['base']['identity'][-1]}).status_code == 200
    assert revision(admin, account) == before + 5 and client.get(path).status_code == 200


def test_wrong_passwords_while_reading(actual_chat, admin):
    client, fixture, conversation, path, held, account, token = setup(actual_chat, admin)
    reader = TestClient(client.app, base_url=ORIGIN, cookies=dict(client.cookies))
    with ThreadPoolExecutor(2) as pool:
        failures = pool.submit(lambda: [wrong_password(client, fixture) for _ in range(4)])
        reads = pool.submit(lambda: [reader.get(path).status_code for _ in range(30)])
        assert failures.result() == [401]*4
        assert reads.result() == [200]*30
    assert held.read_messages(conversation_id=conversation)['items'] == []


def test_a_lockout_stops_new_password_logins_only(actual_chat, admin):
    """Owner 2026-11-02 (ADR-044 §4): the fifth failure locks the account; a new login, even with the right password, is refused, the
    established session and a held claim are not. A password reset (session_epoch + 1) then ends them."""
    client, fixture, conversation, path, held, account, token = setup(actual_chat, admin)
    hashed = snapshot(fixture, token)['directory_hash']
    for _ in range(5):
        assert wrong_password(client, fixture) == 401
    assert admin.execute("select (payload->>'locked_until')::timestamptz>now() from control.nexloop_browser_accounts where local_account_id=%s",
                         (account['local_account_id'],)).fetchone()[0]
    assert TestClient(client.app, base_url=ORIGIN).post('/api/v1/auth/login', headers={'Origin': ORIGIN}, json={
        'username': fixture['base']['identity'][3].username, 'password': fixture['base']['identity'][-1]}).status_code == 401
    assert client.get(path).status_code == 200
    assert held.read_messages(conversation_id=conversation)['items'] == []
    assert snapshot(fixture, token)['directory_hash'] == hashed  # the lockout is not in the account fingerprint
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT_A,))
    admin.execute('select control.nexloop_browser_password_write(%s,a,%s,now()) from control.nexloop_browser_accounts a where local_account_id=%s',
                  (TENANT_A, '$argon2id$v=19$m=65536,t=3,p=4$c3ludGhldGlj$c3ludGhldGljLWhhc2g', account['local_account_id']))
    refused(client, fixture, conversation, path, held, token)


def good_login(client, fixture):
    return TestClient(client.app, base_url=ORIGIN).post('/api/v1/auth/login', headers={'Origin': ORIGIN}, json={
        'username': fixture['base']['identity'][3].username, 'password': fixture['base']['identity'][-1]}).status_code


@pytest.mark.parametrize('cause', ['failure_reset', 'rehash'])
def test_a_good_login_elsewhere_that_saves_the_account_keeps_the_session(actual_chat, admin, cause):
    """Path (b): a good login in another browser that saves the account through save_browser_account_password (0024, revision + 1)
    - after one wrong password (the failure reset), or with a stored hash whose parameters need a rehash - leaves the established
    session's read, a held claim and the fingerprint as they were."""
    from argon2 import PasswordHasher
    client, fixture, conversation, path, held, account, token = setup(actual_chat, admin)
    if cause == 'failure_reset':
        assert wrong_password(client, fixture) == 401
    else:  # the right password under weaker argon2id parameters still inside the model range (0024 accepts it): the real login's check_needs_rehash is true
        weak = PasswordHasher(time_cost=2, memory_cost=19_456, parallelism=1).hash(fixture['base']['identity'][-1])
        admin.execute('update control.nexloop_browser_accounts set password_hash=%s where local_account_id=%s', (weak, account['local_account_id']))
    before, hashed = revision(admin, account), snapshot(fixture, token)['directory_hash']
    saved = admin.execute("select count(*) from control.nexloop_browser_login_events where event_type='local_password_state'").fetchone()[0]
    assert good_login(client, fixture) == 200
    assert admin.execute("select count(*) from control.nexloop_browser_login_events where event_type='local_password_state'").fetchone()[0] == saved + 1
    assert revision(admin, account) == before + 1
    if cause == 'rehash':
        assert admin.execute('select password_hash from control.nexloop_browser_accounts where local_account_id=%s',
                             (account['local_account_id'],)).fetchone()[0] != weak
    assert client.get(path).status_code == 200
    assert held.read_messages(conversation_id=conversation)['items'] == []
    assert snapshot(fixture, token)['directory_hash'] == hashed


@pytest.mark.parametrize('change', ['password_reset', 'kick', 'disabled', 'erasure'])
def test_writes_that_end_sessions_still_end_them(actual_chat, admin, change):
    client, fixture, conversation, path, held, account, token = setup(actual_chat, admin)
    epoch = account['session_epoch']
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT_A,))  # the definers run under row security
    if change == 'password_reset':  # every reset (0198 since 0334, 0333/0348 codes) writes through this function
        admin.execute('select control.nexloop_browser_password_write(%s,a,%s,now()) from control.nexloop_browser_accounts a where local_account_id=%s',
                      (TENANT_A, '$argon2id$v=19$m=65536,t=3,p=4$c3ludGhldGlj$c3ludGhldGljLWhhc2g', account['local_account_id']))
    elif change == 'kick':  # reset issue and every member change (0340/0346/0348)
        admin.execute('select control.nexloop_workbench_kick(%s,a) from control.nexloop_browser_accounts a where local_account_id=%s',
                      (TENANT_A, account['local_account_id']))
    elif change == 'disabled':
        admin.execute("update control.nexloop_browser_accounts set payload=payload||'{\"status\":\"disabled\"}' where local_account_id=%s",
                      (account['local_account_id'],))
    else:
        admin.execute('select control.nexloop_workbench_member_erase(%s,%s,%s,%s,now())', (TENANT_A, account['subject_id'], uuid.uuid4(), 'synthetic-owner'))
    # A disabled account keeps its session_epoch: the retained status check (0404) refuses it directly.
    assert admin.execute('select (payload->>\'session_epoch\')::bigint from control.nexloop_browser_accounts where local_account_id=%s',
                         (account['local_account_id'],)).fetchone()[0] == epoch + (change != 'disabled')
    if change == 'erasure':  # the sessions are deleted as well
        assert client.get(path).status_code != 200
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            snapshot(fixture, token)
    else:
        refused(client, fixture, conversation, path, held, token)
