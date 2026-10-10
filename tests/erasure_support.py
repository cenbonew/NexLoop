"""NX-029 slice 2 test support: put a Consumer into (or out of) the erasing state as the erasure request would.

Admin writes the request row directly (its governed Action and the keeper are covered by tests/test_erasure_pg.py);
'withdraw' is an explicit disposable-database fault (the table refuses deletes), used only to show the same read passes
again once the Consumer is no longer erasing.
"""
ERASING='consumer is being erased'


def mark_erasing(admin,tenant,consumer,world='real'):
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)",(tenant,))
        admin.execute("""insert into control.nexloop_consumer_erasures(tenant_id,world,request_id,consumer_id,source,requested_by,reason,state,watermark)
            values(%s,%s,%s,%s,'owner','synthetic-owner','synthetic erasure','purging',nextval('control.nexloop_deletion_watermark'))""",
            (tenant,world,'test-erasure-'+consumer[:16],consumer))


def withdraw(admin,tenant,consumer,world='real'):
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute('delete from control.nexloop_consumer_erasures where tenant_id=%s and world=%s and consumer_id=%s',(tenant,world,consumer))
