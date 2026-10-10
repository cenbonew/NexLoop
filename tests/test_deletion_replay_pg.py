"""NX-029 slice 4 (AT-059): deletion ledger and replay after a restore, aligned with NX-035 §6.2 (restore journal).

A restored database comes from before some deletions. NX-035 exports the ledger entries into its restore journal and,
after a restore, hands back the entries newer than the restored database to runtime.nexloop_replay_deletions. Here the
'restored' state is the fixture database in which those deletions have not happened yet; the entries are exactly what
runtime.nexloop_deletion_ledger_since returns (round trip). Owner-only functions are called as the owner, as the restore
tool does. Synthetic data only.
"""
from datetime import UTC,datetime,timedelta
import json

import psycopg
import pytest
from psycopg.types.json import Jsonb
from erasure_support import ERASING
from test_erasure_pg import ACTIONS,KEEPER,act,erasing,keeper,request  # noqa: F401
from commitment_fixture import TENANT,commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor,execution_plan  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_commitments_pg import fresh

pytestmark=pytest.mark.parametrize('execution_plan',['commitment-service'],indirect=True)


def ledger(admin,after=0):
    with admin.transaction():
        admin.execute('set local role nexloop_owner')
        return admin.execute('select runtime.nexloop_deletion_ledger_since(%s,1000)',(after,)).fetchone()[0]


def owner_call(admin,sql,params=()):
    with admin.transaction():
        admin.execute('set local role nexloop_owner')
        return admin.execute(sql,params).fetchone()[0]


def test_every_deletion_enters_the_ledger_without_content_and_in_order(erasing,admin):
    c=erasing;owner=c['owner_actions'];consumer=c['consumer']
    commitment,source=fresh(c)
    # An expiry pass that removes something enters the ledger as its class with the cutoff it applied (none otherwise).
    old_reply=c.outbound('四个月前的回复')
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("update runtime.nexloop_conversation_messages set record=record||jsonb_build_object('accepted_at',%s::text) where message_id=%s",
            ((datetime.now(UTC)-timedelta(days=120)).isoformat(),old_reply['message_id']))
    keeper(c).run_once()
    classes=[e for e in ledger(admin) if e['kind']=='item_class']
    assert [e['subject_ref'] for e in classes]==['message_text'] and classes[0]['cutoff']
    before=ledger(admin)
    # D1: a Consumer's own unconfirmed request deletes nothing and is not in the ledger.
    with admin.transaction():admin.execute('select runtime.nexloop_consumer_erasure_requested(%s,%s,%s,%s)',(TENANT,'real',consumer,'chat-9'))
    assert ledger(admin)==before
    act(owner,'erase_message','erase-msg-l',message_id=source['message_id'],reason='删除这条消息')
    act(owner,'erase_consumer','erase-l',consumer_id=consumer,reason='负责人确认')
    entries=ledger(admin,before[-1]['seq'] if before else 0)
    assert [(e['kind'],e['subject_ref']) for e in entries]==[('message',source['message_id']),('consumer',consumer)]
    assert entries[0]['seq']<entries[1]['seq'] and entries[1]['request_ref']=='consumer-request:chat-9'
    assert all(set(e)=={'seq','tenant_id','world','kind','subject_ref','request_ref','cutoff'} for e in entries)
    text=json.dumps(ledger(admin),ensure_ascii=False)
    assert '处理进展' not in text and 'cust-1' not in text
    # Append-only, owner only.
    with pytest.raises(psycopg.Error,match='append-only'),admin.transaction():
        admin.execute('set local role nexloop_owner');admin.execute('delete from control.nexloop_deletion_ledger')
    for role in ('nexloop_api','nexloop_domain_worker','nexloop_configurator'):
        for f in ('runtime.nexloop_replay_deletions(text,jsonb)','runtime.nexloop_deletion_ledger_since(bigint,integer)','runtime.nexloop_replay_begin(text,bigint)'):
            assert admin.execute("select has_function_privilege(%s,%s,'execute')",(role,f)).fetchone()==(False,)


def test_at059_replay_after_restore_blocks_at_once_purges_and_is_idempotent(erasing,admin):
    """AT-059: the restored database still holds a Consumer and a Message deleted after the backup; the journal's entries
    are replayed before anything is served: no contact, no read, then the keeper purges; a second replay changes nothing."""
    c=erasing;consumer=c['consumer']
    commitment,source=fresh(c)
    other=c.outbound('另一条仍然保留的回复')
    # Journal entries newer than the restored database (exactly the ledger shape; seq from the deployment's journal).
    entries=[{'seq':1001,'tenant_id':TENANT,'world':'real','kind':'message','subject_ref':source['message_id'],'request_ref':'message-erasure:j1','cutoff':None},
             {'seq':1002,'tenant_id':TENANT,'world':'real','kind':'consumer','subject_ref':consumer,'request_ref':'erase-j2','cutoff':None},
             {'seq':1003,'tenant_id':TENANT,'world':'real','kind':'item_class','subject_ref':'message_text','request_ref':'sweep:j3',
              'cutoff':(datetime.now(UTC)-timedelta(days=90)).isoformat()}]
    with pytest.raises(psycopg.errors.InsufficientPrivilege),admin.transaction():
        admin.execute('set local role nexloop_owner');admin.execute('select runtime.nexloop_replay_deletions(%s,%s)',('restore-1',Jsonb(entries)))
    assert owner_call(admin,'select runtime.nexloop_replay_begin(%s,%s)',('restore-1',1000))['state']=='replaying'
    assert owner_call(admin,'select runtime.nexloop_erasure_readiness()')['ready'] is False
    out=owner_call(admin,'select runtime.nexloop_replay_deletions(%s,%s)',('restore-1',Jsonb(entries)))
    assert (out['messages'],out['consumers'],out['classes'],out['already_applied'])==(1,1,1,0)
    # At once: the deleted Message is gone, the Consumer is erasing (contact restricted, reads refused), plans closed.
    assert admin.execute("select record->>'body' from runtime.nexloop_conversation_messages where message_id=%s",(source['message_id'],)).fetchone()==('',)
    assert request(admin,consumer)[:2]==('erase-j2','settling')
    assert admin.execute('select active,rule_id from control.nexloop_contact_restrictions where consumer_id=%s',(consumer,)).fetchone()==(True,'consumer_erasure')
    with pytest.raises(psycopg.errors.InsufficientPrivilege,match=ERASING),admin.transaction():
        admin.execute('select runtime.nexloop_assert_not_erasing(%s,%s)',('real',Jsonb({'tenant_id':TENANT,'resource_id':'eios:object:Consumer/'+consumer})))
    # Idempotent: the same journal again changes nothing.
    again=owner_call(admin,'select runtime.nexloop_replay_deletions(%s,%s)',('restore-1',Jsonb(entries[:2])))
    assert (again['messages'],again['consumers'],again['already_applied'])==(0,0,2)
    assert owner_call(admin,'select runtime.nexloop_replay_finish(%s)',('restore-1',))['state']=='replayed'
    assert owner_call(admin,'select runtime.nexloop_erasure_readiness()')=={'ready':True,'replaying':[]}
    with pytest.raises(psycopg.errors.InsufficientPrivilege),admin.transaction():   # a finished replay takes no more entries
        admin.execute('set local role nexloop_owner');admin.execute('select runtime.nexloop_replay_deletions(%s,%s)',('restore-1',Jsonb(entries)))
    for _ in range(10):
        keeper(c).run_once()
        if request(admin,consumer)[1]=='completed':break
    assert request(admin,consumer)[1]=='completed'
    assert admin.execute("select count(*) from ontology.objects where type_name='Consumer' and object_id=%s",(consumer,)).fetchone()==(0,)
    # The replayed deletions are themselves in the ledger again (the restored deployment's journal continues).
    kinds={(e['kind'],e['subject_ref']) for e in ledger(admin)}
    assert ('consumer',consumer) in kinds and ('message',source['message_id']) in kinds


def test_round_trip_ledger_to_replay_is_a_no_op_on_the_same_database(erasing,admin):
    c=erasing;owner=c['owner_actions'];consumer=c['consumer']
    commitment,source=fresh(c)
    act(owner,'erase_message','erase-msg-r',message_id=source['message_id'],reason='删除')
    act(owner,'erase_consumer','erase-r',consumer_id=consumer,reason='删除')
    for _ in range(10):
        keeper(c).run_once()
        if request(admin,consumer)[1]=='completed':break
    owner_call(admin,'select runtime.nexloop_replay_begin(%s,%s)',('restore-rt',0))
    out=owner_call(admin,'select runtime.nexloop_replay_deletions(%s,%s)',('restore-rt',Jsonb([e for e in ledger(admin) if e['kind']!='item_class'])))
    assert out['consumers']==0 and out['messages']==0 and out['already_applied']>=2
    owner_call(admin,'select runtime.nexloop_replay_finish(%s)',('restore-rt',))


def test_doctor_reports_stuck_erasures_failing_purges_and_overdue_classes(erasing,admin):
    """The retention status (doctor) names what does not progress; injected states only (disposable database)."""
    import uuid
    c=erasing;consumer=c['consumer']
    def warnings():
        with admin.transaction():
            # As the retention port calls it: inside the caller's tenant.
            admin.execute('set local role nexloop_owner');admin.execute("select set_config('eios.tenant_id',%s,true)",(TENANT,))
            return {w['code'] for w in admin.execute('select runtime.nexloop_retention_warnings(%s,%s)',(TENANT,'real')).fetchone()[0]}
    assert warnings()==set()
    keeper(c).run_once()                                                    # every class has a completed sweep now
    with admin.transaction():
        admin.execute('set local session_replication_role=replica');admin.execute("select set_config('eios.tenant_id',%s,true)",(TENANT,))
        admin.execute("""insert into control.nexloop_consumer_erasures(tenant_id,world,request_id,consumer_id,source,requested_by,reason,state,watermark,updated_at)
            values(%s,'real','stuck-1',%s,'owner','synthetic','synthetic','waiting_unknown_effects',nextval('control.nexloop_deletion_watermark'),clock_timestamp()-interval '2 days')""",(TENANT,consumer))
        admin.execute("insert into runtime.nexloop_run_purge_items(tenant_id,world,run_id,request_ref,reason,attempts) values(%s,'real',%s,'erasure:x','consumer_erasure',3)",(TENANT,str(uuid.uuid4())))
        admin.execute("update runtime.nexloop_retention_sweeps set last_completed_at=clock_timestamp()-interval '1 day' where item_class='message_text'")
    assert warnings()=={'erasure_waiting_for_unknown_effects','run_purge_failing','retention_class_overdue'}
