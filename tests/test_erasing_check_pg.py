"""NX-029 (migration 0140): the erasing read refusal at near O(1) cost, same semantics as 0132.

On the read-assertion memo fixture (a service holding a configured Consumer READ). A completed erasure of another
Consumer does not affect this Consumer's reads; an erasure committed by another connection refuses the very next read
(no memo window, ADR-025); the lookups the check runs use the new indexes (generic plans, as PL/pgSQL caches them,
on a stream of 20 000 messages). Synthetic data only.
"""
import hashlib

import psycopg
import pytest
from psycopg.types.json import Jsonb
from erasure_support import ERASING
from test_read_assert_memo import memo  # noqa: F401
from test_action_definitions import published_action  # noqa: F401
from support.tenants import TENANT_A

TENANT=TENANT_A


def assert_read(m):
    """The public entry on a fresh connection of its own (the erasure was committed by another one)."""
    with psycopg.connect(m['pg'],autocommit=True) as other:
        with other.transaction():
            return other.execute('select authz.nexloop_assert_read_authority(%s,%s,%s)',(m['session'].token_digest,'real',Jsonb(m['claims']()))).fetchone()[0]


def erasure(admin,consumer,state,request_id):
    with admin.transaction():   # committed: another connection's transaction
        admin.execute("select set_config('eios.tenant_id',%s,true)",(TENANT,))
        admin.execute("""insert into control.nexloop_consumer_erasures(tenant_id,world,request_id,consumer_id,source,requested_by,reason,state,watermark)
            values(%s,'real',%s,%s,'owner','synthetic-owner','synthetic erasure',%s,nextval('control.nexloop_deletion_watermark'))""",(TENANT,request_id,consumer,state))


def test_completed_erasure_of_another_consumer_leaves_reads_alone_and_a_new_erasure_refuses_the_next_read(memo,admin):
    m=memo
    assert assert_read(m)
    # Another Consumer's erasure (completed, and one only pending confirmation): the fast path no longer applies, the
    # per-object check still lets this Consumer through.
    erasure(admin,hashlib.sha256(b'someone-else').hexdigest(),'completed','done-1')
    erasure(admin,hashlib.sha256(b'someone-pending').hexdigest(),'pending_confirmation','pending-1')
    assert assert_read(m)
    # An erasure of this Consumer committed now: the very next read, on another connection, is refused.
    erasure(admin,m['object_id'],'blocking','now-1')
    with pytest.raises(psycopg.errors.InsufficientPrivilege,match=ERASING):assert_read(m)


def test_the_check_uses_the_new_indexes(memo,admin):
    """Generic plans of the check's lookups (PL/pgSQL caches generic plans), on a realistic stream."""
    m=memo
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)",(TENANT,))
        admin.execute("""insert into runtime.nexloop_conversations(tenant_id,world,conversation_id,consumer_id,principal_id,idempotency_key)
          select %s,'real',md5('c'||g)||md5('d'||g),%s,'p','k'||g from generate_series(1,200) g""",(TENANT,m['object_id']))
        admin.execute("""insert into runtime.nexloop_conversation_messages(tenant_id,world,conversation_id,sequence,message_id,idempotency_key,payload_digest,record)
          select %s,'real',md5('c'||(g%%200+1))||md5('d'||(g%%200+1)),g,md5('m'||g)||md5('n'||g),'i'||g,repeat('a',64),'{"body":"x"}'::jsonb
          from generate_series(1,20000) g""",(TENANT,))
        admin.execute("""insert into control.nexloop_consumer_erasures(tenant_id,world,request_id,consumer_id,source,requested_by,reason,state,watermark)
          select %s,'real','p'||g,md5('x'||g)||md5('y'||g),'consumer_request','c','r','pending_confirmation',nextval('control.nexloop_deletion_watermark')
          from generate_series(1,2000) g""",(TENANT,))
    admin.execute('analyze runtime.nexloop_conversation_messages');admin.execute('analyze control.nexloop_consumer_erasures');admin.execute('analyze runtime.nexloop_conversations')
    plans={}
    with admin.transaction():
        admin.execute("set local plan_cache_mode=force_generic_plan");admin.execute("set local role nexloop_owner")
        admin.execute("select set_config('eios.tenant_id',%s,true)",(TENANT,))
        admin.execute("prepare fast(text,text) as select 1 from control.nexloop_consumer_erasures e where e.tenant_id=$1 and e.world=$2 and e.state<>'pending_confirmation' limit 1")
        admin.execute("""prepare msg(text,text,text) as select cv.consumer_id from runtime.nexloop_conversation_messages m join runtime.nexloop_conversations cv
          on cv.tenant_id=m.tenant_id and cv.world=m.world and cv.conversation_id=m.conversation_id where m.tenant_id=$1 and m.world=$2 and m.message_id=$3 limit 1""")
        admin.execute("prepare one(text,text,text) as select 1 from control.nexloop_consumer_erasures e where e.tenant_id=$1 and e.world=$2 and e.consumer_id=$3 and e.state<>'pending_confirmation' limit 1")
        for name,args in (('fast',"('00000000-0000-4000-8000-00000000000a','real')"),('msg',"('00000000-0000-4000-8000-00000000000a','real','"+'a'*64+"')"),('one',"('00000000-0000-4000-8000-00000000000a','real','"+'b'*64+"')")):
            plans[name]='\n'.join(r[0] for r in admin.execute(f'explain (costs off) execute {name}{args}').fetchall())
    print('NX029_PLANS',plans)
    assert 'nexloop_consumer_erasures_erasing' in plans['fast']
    assert 'nexloop_conversation_messages_message' in plans['msg'] and 'Seq Scan on nexloop_conversation_messages' not in plans['msg']
    assert 'nexloop_consumer_erasures_erasing' in plans['one'] and 'consumer_id = $3' in plans['one'].split('Index Cond')[1] and 'Seq Scan' not in plans['one']
