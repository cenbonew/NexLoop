"""NX-037 stop rule contact_after_refusal (interaction observation, 0190) against ADR-023 rule 6 on clean catalog PostgreSQL.

While a consumer is restricted, one reply bound to each of its inbound messages is allowed contact; only proactive Agent outbound
(not bound to an inbound message of its conversation) and a second visible reply to the same inbound message count. The refusing
message's own reply never counts, whether the recorder wrote it before or after the observation looks. Inbound messages are admin
seeds of the 0046 write (the 0109 refusal trigger runs on them); Agent stream entries are admin seeds of the recorder's write.
Synthetic data."""
import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import pytest
from psycopg.types.json import Jsonb

from commitment_fixture import TENANT, commitments, ts  # noqa: F401
from effect_execution_fixture import execution_plan, governed_effect_executor  # noqa: F401

pytestmark = pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)


def agent_entry(c, *, trigger, body='合成回复', sender='agent'):
    """An outbound stream entry as the recorder (or a staff reply) writes it; trigger None is an unbound outbound."""
    a = c['admin']
    conversation = c.conversation(c['consumer'])
    c['n'] += 1
    message = hashlib.sha256(f'agent-{c["n"]}-{secrets.token_hex(4)}'.encode()).hexdigest()
    with a.transaction():
        a.execute("select set_config('eios.tenant_id',%s,true)", (TENANT,))
        seq = a.execute('update runtime.nexloop_conversations set last_sequence=last_sequence+1 where tenant_id=%s and conversation_id=%s returning last_sequence',
                        (TENANT, conversation)).fetchone()[0]
        record = {'direction': 'outbound', 'sender_kind': sender, 'body': body, 'accepted_at': ts(datetime.now(UTC)), 'conversation_id': conversation,
                  'sequence': seq, 'actor': 'synthetic-agent-principal', 'status': 'accepted', **({'trigger_message_id': trigger} if trigger else {})}
        a.execute('''insert into runtime.nexloop_conversation_messages(tenant_id,world,conversation_id,sequence,message_id,idempotency_key,payload_digest,record)
            values(%s,'real',%s,%s,%s,%s,%s,%s)''', (TENANT, conversation, seq, message, 'agent-entry-' + message[:16], 'd' * 64, Jsonb(record)))
    return message


def contacts(c, since):
    return c['admin'].execute("select authz.nexloop_nx037_interaction_observation(%s,'real',%s)", (TENANT, since)).fetchone()[0]['contacts_after_refusal']


def refused(c):
    since = datetime.now(UTC) - timedelta(seconds=1)
    refusal = c.inbound('不要再联系我了')
    assert c['admin'].execute('select active,message_id from control.nexloop_contact_restrictions where tenant_id=%s', (TENANT,)).fetchone() == (True, refusal)
    return since, refusal


def test_the_refusing_message_own_reply_never_counts_in_any_order(commitments):
    c = commitments
    since, refusal = refused(c)
    assert contacts(c, since) == 0  # observed before the recorder wrote the reply
    agent_entry(c, trigger=refusal)
    assert contacts(c, since) == 0  # observed after it (0172 counted 1 here)


def test_a_later_message_of_the_restricted_customer_gets_one_bound_reply_without_a_stop(commitments):
    c = commitments
    refused_since, refusal = refused(c)
    agent_entry(c, trigger=refusal)
    since = datetime.now(UTC)
    later = c.inbound('我还想问一下合成订单的进度')
    agent_entry(c, trigger=later)
    assert contacts(c, since) == 0  # ADR-023 rule 6: allowed contact (0172 counted 1: the trial stopped)


def test_proactive_outbound_while_restricted_counts(commitments):
    c = commitments
    since, refusal = refused(c)
    agent_entry(c, trigger=refusal)
    agent_entry(c, trigger=None, body='合成主动跟进')
    assert contacts(c, since) == 1
    other = c.inbound('合成：另一条来信')
    agent_entry(c, trigger=hashlib.sha256(b'not-an-inbound-message').hexdigest(), body='合成：绑定到不存在的来信')
    assert contacts(c, since) == 2 and other


def test_a_second_visible_reply_to_the_same_message_counts(commitments):
    c = commitments
    _, refusal = refused(c)
    since = datetime.now(UTC)
    later = c.inbound('合成：拒绝后的来信')
    agent_entry(c, trigger=later)
    assert contacts(c, since) == 0
    agent_entry(c, trigger=later, sender='human_takeover', body='合成：第二条可见回复')
    assert contacts(c, since) == 1 and refusal
