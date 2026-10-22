"""NX-056 A5 (docs/design/unified-approvals.md v5 §8; 0326): frozen request bodies under retention, and the guard around them.

A request's body is redacted to {operation, redacted} once its due_by is older than the governance_audit retention (365 days by
default); ids, digest, status and decisions stay, a tombstone records the pass, and a second pass finds nothing. Outside an erasure
scope, and for anything but the body inside one, the request stays immutable. Real PostgreSQL on a clean catalog; admin only moves
recorded times back to stand for elapsed time. Synthetic data only.
"""
import psycopg
import pytest

from support.tenants import TENANT_A
from test_nx056_approvals_store_pg import decide, open_request, row, store  # noqa: F401


def age(admin, approval, days):
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT_A,))
        admin.execute('set local session_replication_role=replica')
        admin.execute('update control.nexloop_approval_requests set requested_at=requested_at-make_interval(days=>%s),due_by=due_by-make_interval(days=>%s) '
                      'where approval_id=%s', (days, days, approval))


def redact(admin, limit=100):
    return admin.execute("select control.nexloop_approval_redact_due(%s,'real',%s)", (TENANT_A, limit)).fetchone()[0]


def body(admin, approval):
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT_A,))
    return admin.execute('select body,request_digest from control.nexloop_approval_requests where approval_id=%s', (approval,)).fetchone()


def test_bodies_past_retention_are_redacted_once_and_nothing_else_changes(store):
    f = store
    admin = f['admin']
    old, fresh = open_request(f)['approval_id'], open_request(f, request_id='r-2', scope_kind='action_type')['approval_id']
    decide(f, 'approver', old, 'reject')
    digest = body(admin, old)[1]
    age(admin, old, 400)
    assert redact(admin) == {'redacted': 1}
    assert body(admin, old) == ({'operation': 'set_real_dispatch', 'redacted': True}, digest) and row(f, old)[0] == 'rejected'
    assert body(admin, fresh)[0]['reason'] == '打开'  # within retention
    assert redact(admin) == {'redacted': 0}
    # A consumed request is redacted too (the 0321 guard lets the body change inside the scope whatever the request's state).
    done = open_request(f, request_id='r-3', reason='另一个')['approval_id']
    decide(f, 'approver', done)
    admin.execute("select control.nexloop_approval_consume(%s,'real',%s,%s,'executed','[]'::jsonb)", (TENANT_A, done, f['requester']['principal_id']))
    age(admin, done, 400)
    assert redact(admin) == {'redacted': 1} and body(admin, done)[0] == {'operation': 'set_real_dispatch', 'redacted': True}
    assert admin.execute("select action,counts from runtime.nexloop_erasure_tombstones where subject_kind='approval_request'").fetchall() == [
        ('redacted', {'approval_requests': 1})] * 2  # one tombstone per pass that redacted


def test_the_body_changes_only_inside_an_erasure_scope_and_alone(store):
    f = store
    admin = f['admin']
    approval = open_request(f)['approval_id']
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT_A,))
    with pytest.raises(psycopg.Error, match='immutable'), admin.transaction():
        admin.execute("""update control.nexloop_approval_requests set body=body||'{"enabled":false}' where approval_id=%s""", (approval,))
    with pytest.raises(psycopg.Error, match='immutable'), admin.transaction():
        admin.execute("select runtime.nexloop_erasure_open('control.nexloop_approval_requests',array['body'],false,'test')")
        admin.execute("""update control.nexloop_approval_requests set body=body||'{"x":1}',status='cancelled',revision=revision+1 where approval_id=%s""", (approval,))
    for change in ("""body||'{"x":1}'""", "jsonb_set(body,'{reason}','\"another text\"')"):  # neither redaction nor a pseudonym
        with pytest.raises(psycopg.Error, match='immutable'), admin.transaction():
            admin.execute('set local role nexloop_owner')
            admin.execute("select runtime.nexloop_erasure_open('control.nexloop_approval_requests',array['body'],false,'test')")
            admin.execute(f'update control.nexloop_approval_requests set body={change} where approval_id=%s', (approval,))
    with pytest.raises(psycopg.Error, match='immutable'), admin.transaction():  # the right form, but not the owner's functions
        admin.execute("select runtime.nexloop_erasure_open('control.nexloop_approval_requests',array['body'],false,'test')")
        admin.execute("update control.nexloop_approval_requests set body=jsonb_set(body,'{reason}','\"pseudonym:" + '0' * 32 + "\"') where approval_id=%s", (approval,))
    with admin.transaction():
        admin.execute('set local role nexloop_owner')
        admin.execute("select runtime.nexloop_erasure_open('control.nexloop_approval_requests',array['body'],false,'test')")
        admin.execute("update control.nexloop_approval_requests set body=jsonb_set(body,'{reason}','\"pseudonym:" + '0' * 32 + "\"') where approval_id=%s", (approval,))
    assert body(admin, approval)[0]['reason'] == 'pseudonym:' + '0' * 32 and row(f, approval)[:2] == ('waiting_approval', 1)
