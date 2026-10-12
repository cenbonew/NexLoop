"""NX-031 load checks (scripts/load/collect.py): duplicate replies are counted over the whole tenant and split by where the
trigger message comes from, the template's history (accepted before the cell's window) or the window itself. Counts only.

Since 0177 the database refuses a second reply to one message in a customer-visible state (partial unique index), so the
synthetic duplicates here are written with that index dropped inside a transaction that is rolled back."""
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from psycopg.types.json import Jsonb

from nexloop_eios.bootstrap import bootstrap

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/load'))
import collect  # noqa: E402


class Rollback(Exception):
    pass


def test_duplicate_replies_are_split_into_template_history_and_window(admin):
    bootstrap(admin)
    tenant, world = collect.TENANT, collect.WORLD
    start = datetime.now(UTC)
    try:
        with admin.transaction():
            admin.execute('set local session_replication_role=replica')  # test harness: rows without their chain
            admin.execute('drop index runtime.nexloop_outbound_messages_one_reply')  # rolled back below
            admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (tenant,))
            conversation = 'c' * 64
            admin.execute("insert into runtime.nexloop_conversations(tenant_id,world,conversation_id,consumer_id,principal_id,idempotency_key) values(%s,%s,%s,%s,'p','k')", (tenant, world, conversation, 'a' * 64))

            def message(seq, accepted):
                mid = f'{seq:064x}'
                admin.execute("""insert into runtime.nexloop_conversation_messages(tenant_id,world,conversation_id,sequence,message_id,idempotency_key,payload_digest,record)
                    values(%s,%s,%s,%s,%s,%s,%s,%s)""", (tenant, world, conversation, seq, mid, f'k-{seq}', 'd' * 64, Jsonb({'accepted_at': accepted.isoformat()})))
                return mid

            def reply(trigger, state):
                admin.execute("""insert into runtime.nexloop_outbound_messages(tenant_id,world,intent_id,receipt_id,conversation_id,trigger_message_id,run_id,sender_kind,
                    sender_principal,role_ref,body_digest,delivery_state) values(%s,%s,%s,%s,%s,%s,%s,'agent','p','role:x',%s,%s)""",
                              (tenant, world, uuid.uuid4(), uuid.uuid4(), conversation, trigger, uuid.uuid4(), 'b' * 64, state))

            history, window, single = message(1, start - timedelta(hours=2)), message(2, start + timedelta(seconds=5)), message(3, start + timedelta(seconds=9))
            for trigger, states in ((history, ('delivered', 'delivered')), (window, ('provider_accepted', 'delivered')), (single, ('delivered', 'failed'))):
                for state in states:
                    reply(trigger, state)
            result = collect.checks(admin, start, start + timedelta(minutes=10), {'checked': 0, 'violations': 0})['duplicate_effects']
            raise Rollback()
    except Rollback:
        pass
    assert admin.execute("select count(*) from pg_indexes where indexname='nexloop_outbound_messages_one_reply'").fetchone() == (1,)
    assert result == {'checked': 3, 'violations': 2, 'window_violations': 1, 'history_violations': 1}
