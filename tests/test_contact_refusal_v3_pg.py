"""Contact refusal rules v3 and the governed history backfill (ADR-023 §4; owner decision 2026-11-02; plan
docs/implementation/claim-to-consumer-property-plan.md §9.6, §11), on clean catalog PostgreSQL. v3 adds adverbs (不要再主动联系我), English
refusals and self-initiated contact (uncertain, restricts too); time-limited and channel-only phrases and someone else as the object are
not refusals. The backfill re-checks earlier inbound messages as a service holding nexloop.contact.refusal_backfill through the inbound
trigger's control writer, counts only, repeatable, and skips simulated personas, owner releases and erasures. Synthetic data only."""
from pathlib import Path

import pytest

from nexloop_eios.contact_restrictions import ContactRefusalBackfillPort, load_refusal_rules
from test_action_definitions import published_action  # noqa: F401
from test_browser_business_authorization import browser_business  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_conversation_messages import conversations  # noqa: F401

ROOT = Path(__file__).resolve().parents[1] / 'deploy/configuration'
V1, V2, V3 = (load_refusal_rules(ROOT / f'contact-refusal-rules.v{n}.json') for n in (1, 2, 3))
BACKFILL = 'eios:action:nexloop.contact.refusal_backfill:1'

REFUSE = [('不要再主动联系我', 'stop-contact'), ('以后请不要再主动联系我，有需要我会自己来问。', 'stop-contact'), ('别再联系我了', 'stop-contact'),
          ("Please don't contact me", 'stop-contact-en'), ("Please don't proactively contact me anymore", 'stop-contact-en'),
          ('stop messaging me', 'stop-contact-en'), ("PLEASE DON'T CONTACT ME", 'stop-contact-en'), ('Please don’t contact me again', 'stop-contact-en')]
UNCERTAIN = [('少联系我', 'less-contact'), ("I'll reach out myself when I need something", 'self-initiated-only'), ('有需要我会自己来问', 'self-initiated-only')]
# Not a refusal of all contact: channel or time preferences (0228), someone else as the object, goods and discounts, a form of address.
NOT_REFUSAL = ['不要电话和短信', '以后只接收站内消息', '不要联系我同事', '别发错货了', '不要寄顺丰', '不要打折了吗？', '晚上不要打电话', '周末别给我发消息',
               "don't call me sir", "don't call me at night", "don't call me before 9", "don't email me the invoice, message me here", 'you only message me here']


def match(admin, body):
    return admin.execute('select control.nexloop_contact_refusal_match(%s)', (body,)).fetchone()[0]


@pytest.fixture
def rules(admin):
    from nexloop_eios.bootstrap import bootstrap
    bootstrap(admin)
    return admin


def test_v3_is_published_beside_v1_and_v2_and_the_switch_is_recorded(rules):
    admin = rules
    rows = dict(admin.execute('select version,definition from control.nexloop_contact_refusal_rules').fetchall())
    assert rows == {1: V1, 2: V2, 3: V3}
    assert V3['marketing_only'] == V2['marketing_only'] and [r['id'] for r in V3['refuse']] == [
        'stop-contact', 'stop-contact-en', 'unsubscribe', 'block-or-erase', 'threat-if-contacted', 'sms-stop-keyword']
    assert V3['exclusions'][:3] == V2['exclusions'] and V3['uncertain'][:2] == V2['uncertain']
    assert admin.execute('select from_version,to_version from control.nexloop_contact_refusal_rule_switches order by to_version').fetchall() == [(1, 2), (2, 3)]


@pytest.mark.parametrize('body,rule', REFUSE)
def test_v3_refusals(rules, body, rule):
    hit = match(rules, body)
    assert hit['matched'] and hit['certainty'] == 'refuse' and hit['rule_id'] == rule and hit['rule_version'] == 3, (body, hit)


@pytest.mark.parametrize('body,rule', UNCERTAIN)
def test_v3_uncertain_hits_restrict_as_well(rules, body, rule):
    hit = match(rules, body)
    assert hit['matched'] and hit['certainty'] == 'uncertain' and hit['rule_id'] == rule, (body, hit)


@pytest.mark.parametrize('body', NOT_REFUSAL)
def test_v3_counter_examples(rules, body):
    assert match(rules, body) == {'matched': False, 'rule_version': 3}, body


def test_a_refusal_next_to_an_excluded_phrase_is_still_a_refusal(rules):
    assert match(rules, '晚上不要打电话，以后都别联系我了')['rule_id'] == 'stop-contact'
    assert match(rules, "don't call me at night. Actually, stop messaging me")['rule_id'] == 'stop-contact-en'


def restriction(admin, consumer):
    return admin.execute('select active,rule_version,rule_id,certainty,control_revision from control.nexloop_contact_restrictions where consumer_id=%s',
                         (consumer,)).fetchone()


def historic(admin, f, bodies, key, conversation=None):
    """Earlier inbound messages that were never checked with v3: the inbound check is off while they are accepted (disposable database)."""
    from test_claim_extraction_jobs_pg import make_window
    admin.execute('alter table runtime.nexloop_conversation_messages disable trigger nx025_inbound_message_protect')
    try:
        if conversation is None:
            return make_window(f, bodies, key)
        return conversation, [f['port'].accept_message(conversation_id=conversation, idempotency_key=f'{key}-{i}', body=b)['message']['id'] for i, b in enumerate(bodies)]
    finally:
        admin.execute('alter table runtime.nexloop_conversation_messages enable trigger nx025_inbound_message_protect')


@pytest.mark.parametrize('published_action', ['with-preference'], indirect=True)
def test_backfill_restricts_through_the_control_writer_counts_only_and_is_repeatable(conversations, admin):
    from eios.authz.operations import Operation
    from eios.authz.resources import ResourceType
    from multi_authority_fixture import seed_multi_authority
    from nexloop_eios.assembly import open_core
    from nexloop_eios.authorization import authenticate_service
    from nexloop_eios.plan_reevaluation import PlanUnavailable
    from psycopg.conninfo import make_conninfo
    f = conversations
    consumer = f['consumer']
    conversation, ids = historic(admin, f, ['你好，续费多少钱？', '以后请不要再主动联系我，有需要我会自己来问。'], 'v3-history')
    assert restriction(admin, consumer) is None
    with open_core(make_conninfo(f['pg'], user='nexloop_domain_worker')) as worker:
        _, token = seed_multi_authority(admin, worker, [(BACKFILL, ResourceType.ACTION, Operation.EXECUTE)], identity_suffix='-refusal-backfill')
        _, other = seed_multi_authority(admin, worker, [('eios:action:nexloop.contact.read:1', ResourceType.ACTION, Operation.EXECUTE)],
                                        identity_suffix='-no-backfill')
        port = lambda credential=None: ContactRefusalBackfillPort(worker, authenticate_service(worker, credential or token, world='real'), f['reader'].signer)
        # Only a principal holding the backfill grant.
        with pytest.raises(PlanUnavailable):
            port(other).run(rule_version=3)
        # A simulated persona is skipped.
        admin.execute("insert into control.nexloop_consumer_personas(tenant_id,world,consumer_id,persona,revision,updated_by) "
                      "select tenant_id,world,consumer_id,'simulated_agent',1,'synthetic-owner' from runtime.nexloop_conversations where consumer_id=%s limit 1", (consumer,))
        simulated = port().run(rule_version=3, apply=True)
        assert simulated['counts']['skipped_persona'] == 1 and simulated['consumers_restricted'] == 0 and restriction(admin, consumer) is None
        admin.execute("update control.nexloop_consumer_personas set persona='real',revision=2 where consumer_id=%s", (consumer,))
        # The caller names the reviewed rule version; any other refuses.
        with pytest.raises(Exception):
            port().run(rule_version=2)
        dry = port().run(rule_version=3)
        assert dry['verb'] == 'dry_run' and dry['counts']['matched'] == 1 and dry['counts']['restricted'] == 1 and dry['by_rule'] == {'stop-contact': 1}
        assert restriction(admin, consumer) is None and admin.execute('select count(*) from control.nexloop_contact_refusal_hits').fetchone() == (0,)
        assert set(dry) == {'verb', 'rule_version', 'counts', 'by_rule', 'consumers_restricted'}  # counts only, no ids or text
        done = port().run(rule_version=3, apply=True)
        assert done['counts']['restricted'] == 1 and done['consumers_restricted'] == 1
        active, version, rule, certainty, revision = restriction(admin, consumer)
        assert (active, version, rule, certainty) == (True, 3, 'stop-contact', 'refuse')
        event = admin.execute("select event_kind,scope_ref,detail->>'message_id',principal_id from control.nexloop_control_events where revision=%s", (revision,)).fetchone()
        assert event == ('contact_restricted', consumer, ids[1], 'system:contact-refusal-backfill')
        # Repeatable: the second run finds the first run's hit; no new control revision.
        again = port().run(rule_version=3, apply=True)
        assert again['counts']['already_hit'] == 1 and again['counts']['restricted'] == 0 and restriction(admin, consumer)[4] == revision
        # The owner's release stands: an earlier message is not used to restrict again.
        admin.execute("update control.nexloop_contact_restrictions set active=false,released_at=clock_timestamp(),released_by='synthetic-owner' where consumer_id=%s", (consumer,))
        # Service seeding advanced the realm epoch (the fixture's port is stale): copy an earlier inbound row as one more unchecked message.
        admin.execute('alter table runtime.nexloop_conversation_messages disable trigger nx025_inbound_message_protect')
        try:
            admin.execute("select set_config('eios.tenant_id',(select tenant_id from runtime.nexloop_conversation_messages where message_id=%s),false)", (ids[1],))
            admin.execute("""insert into runtime.nexloop_conversation_messages(tenant_id,world,conversation_id,sequence,message_id,idempotency_key,payload_digest,record)
                select tenant_id,world,conversation_id,sequence+100,encode(sha256(convert_to(message_id||'-later','UTF8')),'hex'),idempotency_key||'-later',payload_digest,jsonb_set(record,'{body}','"别再联系我了"')
                from runtime.nexloop_conversation_messages where message_id=%s""", (ids[1],))
        finally:
            admin.execute('alter table runtime.nexloop_conversation_messages enable trigger nx025_inbound_message_protect')
        released = port().run(rule_version=3, apply=True)
        assert released['counts']['skipped_released'] == 1 and restriction(admin, consumer)[0] is False
        # A Consumer with an erasure request is skipped.
        admin.execute("insert into control.nexloop_consumer_erasures(tenant_id,world,request_id,consumer_id,source,requested_by,reason,state,watermark) "
                      "select tenant_id,world,'erasure-v3-synthetic',consumer_id,'owner','synthetic-owner','synthetic','pending_confirmation',0 "
                      "from runtime.nexloop_conversations where consumer_id=%s limit 1", (consumer,))
        erased = port().run(rule_version=3)
        assert erased['counts']['skipped_erasure'] == 1 and erased['counts']['skipped_released'] == 0
