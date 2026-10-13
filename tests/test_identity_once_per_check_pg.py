"""ADR-032: the caller's identity is resolved once per read check; revocation and fact changes keep their meaning.

On the relay fixture (test_enterprise_slot_relay_pg), with the relay Source reading its enrolled catalog:
- a revocation committed before a check: the check refuses (object read and batch fact load);
- a revocation during a check: the check holds the credential row share-locked from its start (lock_credential), so the
  revocation cannot commit until the check's transaction ends; it then commits and the next check refuses;
- no leak between checks: in one transaction, two batch fact loads around a fact change and a revocation each see the
  state at their own start (the identity lives only in one function call's local variable).
Synthetic data; superuser SQL for the revocation and the fact change (as other credential tests do).
"""
import hashlib
import json

import psycopg
import pytest
from psycopg.types.json import Jsonb

from nexloop_eios.service_offerings import OFFERING_FIELDS
from test_enterprise_slot_relay_pg import slots  # noqa: F401
from test_relay_multi_consumer_pg import multi  # noqa: F401
from message_runtime_fixture import message_plan  # noqa: F401
from test_conversation_effect_receipts import receipt_plan  # noqa: F401
from test_message_relay import relay_plan  # noqa: F401


@pytest.fixture
def source(slots, pg):  # noqa: F811
    s = slots
    s['enroll']('D')
    service = s['api'].authenticate(s['slot_source_token'], world='real')
    principal = service._session.authentication.subject_principal_id
    digest = hashlib.sha256(s['slot_source_token'].encode()).hexdigest()
    return dict(s, read=lambda: s['api'].authenticate(s['slot_source_token'], world='real').read_object(
        type_name='ServiceOffering', object_id=s['people']['D']['offerings']['reply'][0], fields=OFFERING_FIELDS), digest=digest, principal=principal, pg=pg)


def keys(s):
    rows = s['admin'].execute("select fact_kind,entity_key from authz.nexloop_authority_facts where tenant_id=%s and fact_kind in ('actor','grants') "
                              'and entity_key[1]=%s order by 1,2 limit 3', (s['tenant'], s['principal'])).fetchall()
    assert len(rows) >= 2
    return [{'kind': kind, 'key': key} for kind, key in rows]


def batch(db, s, wanted):
    return db.execute("select authz.nexloop_load_authority_facts(%s,'real',%s)", (s['digest'], Jsonb(wanted))).fetchone()[0]


def revoke(db, s):
    db.execute("update authz.nexloop_service_credentials set status='revoked' where token_digest=%s", (s['digest'],))


def test_a_revocation_committed_before_the_check_is_refused(source):
    s = source
    assert s['read']()['properties']
    assert all(row['status'] == 'ok' for row in batch(s['admin'], s, keys(s)))
    revoke(s['admin'], s)
    with pytest.raises(Exception):
        s['read']()
    assert {row['status'] for row in batch(s['admin'], s, keys(s))} == {'error'}


def test_a_revocation_during_a_check_waits_for_it_and_the_next_check_is_refused(source):
    s = source
    with psycopg.connect(s['pg'], autocommit=False) as check:
        # What every read check does first: share-lock its credential for the rest of its transaction.
        check.execute("select authz.nexloop_lock_credential(%s,'real',%s)", (s['digest'], 'eios:object:ServiceOffering/' + s['people']['D']['offerings']['reply'][0]))
        assert all(row['status'] == 'ok' for row in batch(check, s, keys(s)))
        with psycopg.connect(s['pg'], autocommit=True) as other:
            other.execute("set lock_timeout='500ms'")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                revoke(other, s)  # cannot commit while the check runs
        check.commit()
    revoke(s['admin'], s)
    with pytest.raises(Exception):
        s['read']()


def test_two_checks_in_one_transaction_each_see_the_state_at_their_start(source):
    s = source
    wanted = keys(s)
    with psycopg.connect(s['pg'], autocommit=False) as db:
        first = batch(db, s, wanted)
        assert all(row['status'] == 'ok' for row in first)
        kind, key = wanted[0]['kind'], wanted[0]['key']
        payload = db.execute('select payload from authz.nexloop_authority_facts where tenant_id=%s and fact_kind=%s and entity_key=%s', (s['tenant'], kind, key)).fetchone()[0]
        changed = {**payload, 'revision': payload.get('revision', 1) + 1}
        db.execute('update authz.nexloop_authority_facts set payload=%s where tenant_id=%s and fact_kind=%s and entity_key=%s', (Jsonb(changed), s['tenant'], kind, key))
        second = batch(db, s, wanted)
        assert second[0]['value']['payload'] == json.loads(json.dumps(changed)) and second[0]['value']['record_hash'] != first[0]['value']['record_hash']
        assert second[1:] == first[1:]
        revoke(db, s)
        assert {row['status'] for row in batch(db, s, wanted)} == {'error'}
        db.rollback()
