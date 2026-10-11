"""NX-035 §6.3: the local JSON delivery's time query for the restore effect window. Its own fsynced records outside the
database list the intents delivered in the window; anything it cannot read exactly makes the channel not queryable (the
reconciliation then keeps the action type paused). Temporary private directories, synthetic intents."""
import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from nexloop_eios.local_json_delivery import JsonExportStore
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.incentives import LocalIncentiveLedger
from nexloop_eios.restore_evidence import incentive_ledger_evidence, local_json_evidence, not_queryable
from support.tenants import TENANT_A

ACTION = 'nexloop.service.request'


@pytest.fixture
def store(tmp_path):
    root = tmp_path / 'json-delivery'
    root.mkdir(mode=0o700)
    s = JsonExportStore(root)
    yield root, s
    s.close()


def deliver(store, message='已办理'):
    root, s = store
    intent = str(uuid.uuid4())
    parameters = {'message': message}
    s.deliver(intent, hashlib.sha256(canonical_payload(parameters).encode()).hexdigest(), parameters)
    return intent


def age(store, intent, when):
    root, _ = store
    os.utime(root / (intent + '.manifest.json'), (when.timestamp(), when.timestamp()))


def evidence(store, start, end):
    return local_json_evidence(store[0], tenant_id=TENANT_A, world='real', action_type=ACTION, window_start=start, window_end=end)


def test_the_window_lists_exactly_the_deliveries_published_in_it(store):
    now = datetime.now(UTC)
    inside, before, after = deliver(store), deliver(store), deliver(store)
    age(store, inside, now - timedelta(minutes=5))
    age(store, before, now - timedelta(hours=2))
    age(store, after, now + timedelta(hours=2))
    out = evidence(store, now - timedelta(hours=1), now)
    assert out == {'tenant_id': TENANT_A, 'world': 'real', 'action_type': ACTION, 'time_queryable': True, 'external_intent_ids': [inside]}
    assert evidence(store, now - timedelta(days=1), now + timedelta(days=1))['external_intent_ids'] == sorted([inside, before, after])
    assert evidence(store, now + timedelta(days=1), now + timedelta(days=2))['external_intent_ids'] == []


def test_a_record_it_cannot_read_exactly_makes_the_channel_not_queryable(store):
    root, _ = store
    now = datetime.now(UTC)
    intent = deliver(store)
    (root / (intent + '.manifest.json')).chmod(0o644)  # not private any more
    assert evidence(store, now - timedelta(hours=1), now + timedelta(hours=1)) == not_queryable(tenant_id=TENANT_A, world='real', action_type=ACTION)
    (root / (intent + '.manifest.json')).chmod(0o600)
    other = str(uuid.uuid4())
    (root / (other + '.manifest.json')).write_text('{"intent_id":"%s"}' % intent)  # names another intent
    (root / (other + '.manifest.json')).chmod(0o600)
    assert evidence(store, now - timedelta(hours=1), now + timedelta(hours=1))['time_queryable'] is False
    (root / (other + '.manifest.json')).unlink()
    root.chmod(0o755)  # the store root itself must be private
    assert evidence(store, now - timedelta(hours=1), now + timedelta(hours=1))['time_queryable'] is False
    root.chmod(0o700)
    assert evidence(store, now - timedelta(hours=1), now + timedelta(hours=1))['external_intent_ids'] == [intent]


def test_missing_store_and_invalid_windows(tmp_path):
    now = datetime.now(UTC)
    assert local_json_evidence(tmp_path / 'absent', tenant_id=TENANT_A, world='real', action_type=ACTION,
                               window_start=now, window_end=now)['time_queryable'] is False
    with pytest.raises(ValueError):
        local_json_evidence(tmp_path, tenant_id=TENANT_A, world='real', action_type=ACTION, window_start=now, window_end=now - timedelta(seconds=1))
    with pytest.raises(ValueError):
        local_json_evidence(tmp_path, tenant_id=TENANT_A, world='real', action_type=ACTION, window_start=now.replace(tzinfo=None), window_end=now)


def test_the_incentive_ledger_lists_the_intents_issued_in_the_window(tmp_path):
    ledger = LocalIncentiveLedger(tmp_path / 'ledger')
    now = datetime.now(UTC)
    for intent in ('incentive-run:a', 'incentive-run:b'):
        ledger.issue({'intent_id': intent, 'incentive_code': 'c', 'amount_minor': '100', 'currency': 'CNY', 'consumer_ref': 'x' * 64})
    old = ledger._path('incentive-run:b')
    os.utime(old, ((now - timedelta(hours=3)).timestamp(),) * 2)
    read = lambda start, end: incentive_ledger_evidence(tmp_path / 'ledger', tenant_id=TENANT_A, world='real', window_start=start, window_end=end)
    assert read(now - timedelta(hours=1), now + timedelta(minutes=1)) == {'tenant_id': TENANT_A, 'world': 'real', 'action_type': 'nexloop.incentive.issue',
                                                                          'time_queryable': True, 'external_intent_ids': ['incentive-run:a']}
    (tmp_path / 'ledger' / 'unfinished.tmp').write_text('{}')  # an interrupted write was never issued
    assert read(now - timedelta(days=1), now + timedelta(minutes=1))['external_intent_ids'] == ['incentive-run:a', 'incentive-run:b']
    os.rename(old, tmp_path / 'ledger' / ('0' * 64 + '.json'))  # a record under another name: not exact
    assert read(now - timedelta(days=1), now + timedelta(minutes=1))['time_queryable'] is False
    assert incentive_ledger_evidence(tmp_path / 'absent', tenant_id=TENANT_A, world='real', window_start=now, window_end=now)['time_queryable'] is False
