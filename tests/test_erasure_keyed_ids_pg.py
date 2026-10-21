"""NX-029 erasure, owner decisions 2026-10-21 (0308): tombstones keep a keyed HMAC of the erased Consumer, never the id; outreach intent
ids generated from now on are keyed with the tenant's erasure key (the same inputs still give the same id), and a request replayed
across the change - its legacy (unkeyed) intent already there - finds that intent and never sends twice. Real PostgreSQL.
"""
import hashlib
import json

from commercial_fixture import TENANT
from test_erasure_pg import act, erasing, fault, keeper, request  # noqa: F401
from test_erasure_pg import pytestmark  # noqa: F401  (execution_plan parametrization)
from commitment_fixture import commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401

ORIGIN = 'owner:keyed-ids-test'


def today(admin):
    return admin.execute("""select to_char(runtime.nexloop_world_now(%s,'real') at time zone (runtime.nexloop_outreach_settings(%s)->>'time_zone'),'YYYY-MM-DD')""",
                         (TENANT, TENANT)).fetchone()[0]


def legacy_id(consumer, day, purpose='service_followup'):
    # jsonb_build_array(...)::text renders with ", " separators
    return 'outreach:' + hashlib.sha256(json.dumps([TENANT, consumer, purpose, ORIGIN, day], ensure_ascii=False).encode()).hexdigest()


def accept(admin, consumer, body='您好，回访一下服务情况'):
    return admin.execute("""select runtime.nexloop_outreach_accept(%s,'real',%s,'service_followup',%s,%s,null,'human','synthetic-owner','intent-keyed',null)""",
                         (TENANT, consumer, ORIGIN, body)).fetchone()[0]


def test_outreach_intent_ids_are_keyed_and_still_idempotent(erasing, admin):
    consumer = erasing['consumer']
    day = today(admin)
    keyed = admin.execute("select runtime.nexloop_outreach_intent_id(%s,'real',%s,'service_followup',%s,%s)", (TENANT, consumer, ORIGIN, day)).fetchone()[0]
    assert keyed != legacy_id(consumer, day) and keyed.startswith('outreach:')
    assert admin.execute("select 'outreach:'||encode(sha256(convert_to(jsonb_build_array(%s::text,%s::text,'service_followup'::text,%s::text,%s::text)::text,'UTF8')),'hex')",
                         (TENANT, consumer, ORIGIN, day)).fetchone()[0] == legacy_id(consumer, day)  # the legacy formula, as 0263 had it
    first = accept(admin, consumer)
    again = accept(admin, consumer)
    ids = [r[0] for r in admin.execute("select intent_id from runtime.nexloop_outreach_sends where consumer_id=%s and origin_ref=%s", (consumer, ORIGIN)).fetchall()]
    assert ids == [keyed], (first, again, ids)


def test_a_request_replayed_across_the_change_finds_its_legacy_intent(erasing, admin):
    """The launch day: an intent accepted before the change (legacy id) is found again by the same request after it - no second send."""
    consumer = erasing['consumer']
    accept(admin, consumer)  # a row to copy, then turned into the legacy one
    legacy = legacy_id(consumer, today(admin))
    fault(admin, "update runtime.nexloop_outreach_sends set intent_id=%s where consumer_id=%s and origin_ref=%s", (legacy, consumer, ORIGIN))
    accept(admin, consumer)
    ids = [r[0] for r in admin.execute("select intent_id from runtime.nexloop_outreach_sends where consumer_id=%s and origin_ref=%s", (consumer, ORIGIN)).fetchall()]
    assert ids == [legacy]


def test_tombstones_keep_a_keyed_hmac_and_a_receipt_is_found_by_it(erasing, admin):
    c = erasing
    consumer = c['consumer']
    act(c['owner_actions'], 'erase_consumer', 'erase-keyed', consumer_id=consumer, reason='顾客要求删除')
    for _ in range(12):
        keeper(c, host=lambda run_id: 'purged').run_once()
        if request(admin, consumer)[1] == 'completed':
            break
    assert request(admin, consumer)[1] == 'completed'
    assert admin.execute('select count(*) from runtime.nexloop_erasure_tombstones where subject_key=%s', (consumer,)).fetchone() == (0,)
    key = admin.execute('select runtime.nexloop_erasure_subject_key(%s,%s)', (TENANT, consumer)).fetchone()[0]
    assert key.startswith('hmac:') and consumer not in key
    kinds = {k for (k,) in admin.execute('select subject_kind from runtime.nexloop_erasure_tombstones where subject_key=%s', (key,)).fetchall()}
    assert 'consumer' in kinds  # the receipt, looked up through the keyed key
    # The append-only guard still refuses any change outside an erasure scope.
    import pytest
    with pytest.raises(Exception, match='append-only'), admin.transaction():
        admin.execute("update runtime.nexloop_erasure_tombstones set subject_key=%s where subject_key=%s", (consumer, key))
