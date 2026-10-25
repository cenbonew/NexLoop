"""NX-058 slice C, dispatcher option 1 (0329): the governed entry's recheck of the requester keeps the tenant revision a governed handler's
own authority change raised in this transaction - and nothing else. On PostgreSQL:
- authz.nexloop_effective_tenant_revision returns the revision before only for a mark of this very transaction whose increments are this
  transaction's (the mark holds the tenant row); without a mark, with another transaction's mark, or with an increment before the mark,
  it is the current revision, so the recheck fails exactly as before;
- no application role can write a mark or call the marking function: there is no flag a caller could set;
- through the actual app, a member change by the owner passes the owner's recheck, and the changed member's sessions and permits fail at
  once (tests/test_workbench_member_changes_pg.py covers the change itself)."""
import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from nexloop_eios.bootstrap import bootstrap
from support.tenants import TENANT_A


@pytest.fixture
def db(admin):
    bootstrap(admin)
    admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (TENANT_A,))
    return admin


def revision(c):
    return c.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s', (TENANT_A,)).fetchone()[0]


def bump(c):
    c.execute("update control.nexloop_tenants set status='active' where tenant_id=%s", (TENANT_A,))  # the revision guard adds one


def effective(c):
    return c.execute('select authz.nexloop_effective_tenant_revision(%s,%s)', (TENANT_A, revision(c))).fetchone()[0]


def test_only_this_transactions_own_increments_after_its_mark_are_kept(db):
    c = db
    start = revision(c)
    with c.transaction():  # no mark: the current revision
        bump(c)
        assert effective(c) == start + 1
    start = revision(c)
    with c.transaction():  # a handler's mark, then its authority writes: the revision before
        c.execute('select control.nexloop_authority_handler_mark(%s)', (TENANT_A,))
        bump(c)
        bump(c)
        assert revision(c) == start + 2 and effective(c) == start
    start = revision(c)
    with c.transaction():  # an increment before the mark (not the handler's): kept current, the recheck fails as before
        bump(c)
        c.execute('select control.nexloop_authority_handler_mark(%s)', (TENANT_A,))
        assert effective(c) == start + 1
    start = revision(c)
    with c.transaction():  # review I-1: the handler inside a savepoint (the NX-056 gate) - the row's xmin is the subtransaction's
        with c.transaction():
            c.execute('select control.nexloop_authority_handler_mark(%s)', (TENANT_A,))
            bump(c)
        assert revision(c) == start + 1 and effective(c) == start
    start = revision(c)
    with c.transaction():  # a mark in a savepoint that rolled back leaves nothing
        try:
            with c.transaction():
                c.execute('select control.nexloop_authority_handler_mark(%s)', (TENANT_A,))
                raise RuntimeError('rolled back')
        except RuntimeError:
            pass
        bump(c)
        assert effective(c) == start + 1
    # Another transaction's mark is no mark here.
    with c.transaction():
        c.execute('select control.nexloop_authority_handler_mark(%s)', (TENANT_A,))
    start = revision(c)
    with c.transaction():
        bump(c)
        assert effective(c) == start + 1


def test_a_concurrent_increment_waits_for_the_marked_transaction(db, pg):
    """The mark holds the tenant row: another transaction's increment cannot land between the mark and the recheck."""
    import threading
    import time
    c = db
    done = []
    with c.transaction():
        c.execute('select control.nexloop_authority_handler_mark(%s)', (TENANT_A,))
        start = revision(c)

        def other():
            with psycopg.connect(pg, autocommit=True) as o:
                bump(o)
                done.append(True)
        t = threading.Thread(target=other)
        t.start()
        time.sleep(0.5)
        assert done == [] and effective(c) == start
    t.join()
    assert done == [True]


@pytest.mark.parametrize('role', ['nexloop_api', 'nexloop_identity', 'nexloop_domain_worker', 'nexloop_configurator'])
def test_no_application_role_can_mark(db, pg, role):
    c = db
    if role == 'nexloop_configurator':
        c.execute('alter role nexloop_configurator login')
    with psycopg.connect(make_conninfo(pg, user=role), autocommit=True) as r:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            r.execute('select control.nexloop_authority_handler_mark(%s)', (TENANT_A,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            r.execute("insert into control.nexloop_authority_handler_marks values(txid_current(),%s,1)", (TENANT_A,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            r.execute('select authz.nexloop_effective_tenant_revision(%s,1)', (TENANT_A,))
        # A session setting changes nothing: there is no flag, only the owner's table.
        r.execute("select set_config('nexloop.authority_handler_mark','on',false)")
    assert c.execute('select count(*) from control.nexloop_authority_handler_marks').fetchone() == (0,)


def test_the_requester_rechecks_its_own_rows_at_their_current_values(db):
    """Only the tenant revision term changes: the snapshot body still compares the session, subject, account (revision, session_epoch)
    and membership as they are now (the copy guard registers exactly one difference from 0045)."""
    import re
    from pathlib import Path
    # The temporary migration number is the dispatcher's to change: found by its name.
    body = next((Path(__file__).resolve().parents[1] / 'packages/eios-core/src/eios/migrations').glob('*_governed_authority_change.sql')).read_text()
    start = body.index('create or replace function authz.nexloop_browser_identity_snapshot')
    snapshot = body[start:body.index('end $$;', start)]
    assert snapshot.count('nexloop_effective_tenant_revision') == 1
    for check in ("acc->'revision' is distinct from b->'credential_revision'", "acc->'session_epoch' is distinct from b->'credential_session_epoch'",
                  "mem->'revision' is distinct from b->'membership_revision'", "mem->>'status' is distinct from 'active'"):
        assert check in snapshot
    assert re.search(r"'account',acc,'membership',mem", snapshot)


def test_one_mark_per_transaction(db):
    """Review I-3: a transaction carries one governed handler's authority change - a second mark (a second handler, or anything trying to
    ride on the first) is refused, and the transaction with it."""
    c = db
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='one per transaction'), c.transaction():
        c.execute('select control.nexloop_authority_handler_mark(%s)', (TENANT_A,))
        bump(c)
        c.execute('select control.nexloop_authority_handler_mark(%s)', (TENANT_A,))
    assert c.execute('select count(*) from control.nexloop_authority_handler_marks').fetchone() == (0,)


def test_old_marks_are_cleaned_at_the_next_mark(db):
    c = db
    with c.transaction():
        c.execute('select control.nexloop_authority_handler_mark(%s)', (TENANT_A,))
    c.execute("update control.nexloop_authority_handler_marks set created_at=clock_timestamp()-interval '2 days'")
    with c.transaction():
        c.execute('select control.nexloop_authority_handler_mark(%s)', (TENANT_A,))
    assert c.execute('select count(*) from control.nexloop_authority_handler_marks').fetchone() == (1,)
