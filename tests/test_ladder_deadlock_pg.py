"""NX-031 ladder deadlocks (0410) on clean catalog PostgreSQL. On a 500 customer template the history replies deadlocked seven
times, always the local JSON delivery (authz.nexloop_local_delivery_authority) against a Run's intent submit
(authz.nexloop_effect_intent_command):

  delivery  the tenant's published Action definition row FOR UPDATE, then the effect control ledger row FOR UPDATE;
  submit    the ledger row FOR UPDATE, then the same definition row FOR SHARE.

0410 makes the delivery take the definition FOR SHARE (as the effect execution command, 0042, does). These tests take the two
transactions' locks in exactly that order on real rows, with the mode the current delivery function uses: the former FOR UPDATE
deadlocks, FOR SHARE lets the submit finish and the delivery go on. Synthetic rows only."""
import threading
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from nexloop_eios.bootstrap import bootstrap
from support.tenants import TENANT_A

RESOURCE = 'eios:action:nexloop.service.request:1'
DEFINITION = "select * from control.nexloop_action_definitions where tenant_id=%s and world='real' and resource_id=%s and active for {mode}"
LEDGER = "select * from control.nexloop_effect_control_ledger where control_id=%s and tenant_id=%s and world='real' for update"


@pytest.fixture
def rows(admin):
    bootstrap(admin)
    control = 'control-' + uuid.uuid4().hex
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')  # test harness: the two rows as configuration left them
        admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (TENANT_A,))
        admin.execute("insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,'real',%s,%s,%s)",
                      (TENANT_A, RESOURCE, Jsonb({'tenant_id': TENANT_A, 'status': 'published', 'stable_name': 'nexloop.service.request', 'version': 1}), Jsonb({})))
        admin.execute("""insert into control.nexloop_effect_control_ledger(control_id,tenant_id,world,consumer_id,owner_principal,owner_digest,
            executor_principal,executor_digest,control_revision,budget_units,valid_until) values(%s,%s,'real',%s,'owner','d','executor','d',1,4,%s)""",
                      (control, TENANT_A, 'a' * 64, datetime.now(UTC) + timedelta(hours=1)))
    return control


def delivery_definition_mode(admin):
    """The lock mode the current delivery function takes on the published definition row."""
    source = admin.execute("select pg_get_functiondef('authz.nexloop_local_delivery_authority(text,text,text,text,text)'::regprocedure)").fetchone()[0]
    statement = next(line for line in source.splitlines() if 'from control.nexloop_action_definitions d' in line)
    return 'share' if statement.rstrip().endswith('d.active for share;') else 'update' if statement.rstrip().endswith('d.active for update;') else None


def race(pg, mode, control):
    """Delivery order (definition <mode>, then ledger) against submit order (ledger, then definition FOR SHARE). Returns the
    error class of each side (None when it committed)."""
    outcome = {}
    delivery_has_definition, submit_has_ledger = threading.Event(), threading.Event()

    def delivery():
        try:
            with psycopg.connect(pg, autocommit=True) as db, db.transaction():
                db.execute("set local deadlock_timeout='100ms'; set local lock_timeout='10s'")
                db.execute(DEFINITION.format(mode=mode), (TENANT_A, RESOURCE))
                delivery_has_definition.set()
                submit_has_ledger.wait(10)
                db.execute(LEDGER, (control, TENANT_A))  # waits for the submit's ledger lock
            outcome['delivery'] = None
        except psycopg.Error as error:
            outcome['delivery'] = type(error).__name__

    def submit():
        try:
            with psycopg.connect(pg, autocommit=True) as db, db.transaction():
                db.execute("set local deadlock_timeout='100ms'; set local lock_timeout='10s'")
                delivery_has_definition.wait(10)
                db.execute(LEDGER, (control, TENANT_A))
                submit_has_ledger.set()
                db.execute(DEFINITION.format(mode='share'), (TENANT_A, RESOURCE))  # 0040: the definition FOR SHARE
            outcome['submit'] = None
        except psycopg.Error as error:
            outcome['submit'] = type(error).__name__

    threads = [threading.Thread(target=delivery), threading.Thread(target=submit)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return outcome


def test_the_delivery_takes_the_published_definition_for_share(admin, rows):
    assert delivery_definition_mode(admin) == 'share'


def test_the_former_lock_order_deadlocks_and_the_current_one_completes(pg, admin, rows):
    # Before 0410 (FOR UPDATE): exactly the reproduced cycle; PostgreSQL aborts one side.
    former = race(pg, 'update', rows)
    assert sorted(map(str, former.values())) == ['DeadlockDetected', 'None'], former
    # The current delivery mode: the submit's FOR SHARE is compatible, it commits, and the delivery then gets the ledger row.
    current = race(pg, delivery_definition_mode(admin), rows)
    assert current == {'submit': None, 'delivery': None}, current


def test_deliveries_no_longer_serialize_on_the_tenant_wide_definition_row(pg, admin, rows):
    """Two deliveries hold the definition at the same time (FOR SHARE), while a publication change still waits for them."""
    mode = delivery_definition_mode(admin)
    with psycopg.connect(pg, autocommit=True) as one, psycopg.connect(pg, autocommit=True) as two, psycopg.connect(pg, autocommit=True) as writer:
        with one.transaction(), two.transaction():
            one.execute(DEFINITION.format(mode=mode), (TENANT_A, RESOURCE))
            two.execute("set local lock_timeout='500ms'")
            two.execute(DEFINITION.format(mode=mode), (TENANT_A, RESOURCE))  # no wait
            with pytest.raises(psycopg.errors.LockNotAvailable), writer.transaction():
                writer.execute("set local lock_timeout='300ms'")
                writer.execute("update control.nexloop_action_definitions set active=false where tenant_id=%s and resource_id=%s", (TENANT_A, RESOURCE))
