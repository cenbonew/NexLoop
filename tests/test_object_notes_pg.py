"""NX-062 M5c (docs/implementation/NX-062-M5-design.md §11, owner 2026-11-10 option A; L6 V-1 to V-4, W1 to W3) on real PostgreSQL.

The object note: a machine client writes it through prepare / commit (nexloop_eios.machine_writes, real signing key), a person (owner or
operator now) through the same governed handler; text cleaned and bounded; machine notes capped per client and per object a day, no
policy no machine note (W3); the audit names note, author and object, never the text; the Consumer and the member erasure rewrite the
text, the object's deletion takes its notes (V-3); no runtime role, machine read verb, model caller or export reaches them (V-2, W1, W2).
Keys and texts are generated at run time; the markers are not hex-like."""
import json
import pathlib
import re
import secrets

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool

from commercial_fixture import publish_registry_actions
from nexloop_eios import machine_keys as K
from nexloop_eios import machine_writes as W
from support.tenants import TENANT_A as TENANT
from test_machine_lifecycle_pg import desk, owner  # noqa: F401
from test_machine_reads_pg import commitment, hexid, publish_types, put
from test_machine_realm_pg import publish
from test_workbench_actions_http import act
from test_workbench_read_pg import browser_business, conversations, identity, published_action, uow, workbench  # noqa: F401

ROOT = pathlib.Path(__file__).resolve().parents[1]
CAP = 'object.add_note'
MARK = '备注标记-不应出现在审计里'


def limits(admin, per_client=200, per_object=20):
    rev = admin.execute('select coalesce(max(revision),0)+1 from control.nexloop_machine_note_limits').fetchone()[0]
    admin.execute("insert into control.nexloop_machine_note_limits(revision,notes_per_client_per_day,notes_per_object_per_day,manifest_version,digest) values(%s,%s,%s,%s,%s)",
                  (rev, per_client, per_object, rev, secrets.token_hex(32)))


@pytest.fixture
def notes(desk, pg):
    w = desk
    admin = w['admin']
    publish(w)
    publish_types(admin)
    publish_registry_actions(admin, ('nexloop.machine_client.grant', 'nexloop.machine_client.revoke_grant', 'nexloop.object.add_note'), tenant=TENANT)
    with w['client']() as client:
        csrf = owner(client, w)
        assert act(client, csrf, 'register_machine_client', {'client_id': 'acme-agent', 'display_name': 'A', 'purpose': 'p'}).status_code == 200
        issued = act(client, csrf, 'issue_machine_key', {'client_id': 'acme-agent', 'world': 'real', 'expires_in_days': 30, 'scope': [CAP, 'object.get']})
        assert issued.status_code == 200, issued.text
        granted = act(client, csrf, 'grant_machine_capability', {'client_id': 'acme-agent', 'capability': CAP, 'object_types': ['Commitment', 'CommercialRecord']})
        assert granted.status_code == 200, granted.text
    key = K.parse(issued.json()['key'])
    pool = ConnectionPool(make_conninfo(pg, user='nexloop_api'), min_size=1, max_size=2, open=True)
    consumer = hexid()
    obj = put(admin, 'Commitment', commitment(consumer))
    try:
        yield {**w, 'pool': pool, 'signer': w['f']['reader'].signer, 'prefix': key.prefix_id, 'digest': K.digest(key, w['peppers'], 1).hex(),
               'object': obj, 'consumer': consumer, 'pg': pg, 'key': issued.json()['key']}
    finally:
        pool.close()


def note(w, intent, text='一条备注', obj=None, type_name='Commitment'):
    params = {'type_name': type_name, 'object_id': obj or w['object'], 'text': text}
    p = W.prepare(w['pool'], prefix=w['prefix'], digest=w['digest'], capability=CAP, intent_id=intent, params=params)
    return W.commit(w['pool'], w['signer'], prefix=w['prefix'], digest=w['digest'], capability=CAP, intent_id=intent, params=params,
                    prepare_digest=p['prepare_digest'])


def human(w, who, intent, text='人写的备注', obj=None):
    body = {'operation': 'object_note_add', 'request_id': intent, 'type_name': 'Commitment', 'object_id': obj or w['object'], 'text': text}
    return w['admin'].execute('select runtime.nexloop_governed_object_note(%s,%s,%s,%s,%s,%s)', (TENANT, 'real', who, 'human', intent, json.dumps(body))).fetchone()[0]


def rows(admin):
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT,))
    return admin.execute('select author_kind,client_id,text,erased,author_principal_id from runtime.nexloop_object_notes order by created_at').fetchall()


def test_a_machine_note_through_prepare_and_commit_audited_without_its_text(notes):
    w = notes
    admin = w['admin']
    limits(admin)
    done = note(w, 'note-0001', MARK + ' ‮倒序\u0007')
    assert done['phase'] == 'committed', done
    assert rows(admin) == [('machine', 'acme-agent', MARK + ' 倒序', False, 'nexloop-machine:acme-agent')]  # controls removed
    audit = admin.execute("select payload from runtime.audit_events where action='object.note.add'").fetchall()
    assert len(audit) == 1 and set(audit[0][0]) == {'note_id', 'author_kind', 'author_principal_id', 'client_id', 'type_name', 'object_id', 'world'}
    everything = json.dumps([p for (p,) in admin.execute('select payload from runtime.audit_events').fetchall()], ensure_ascii=False)
    assert MARK not in everything
    # The object itself is unchanged: no property, no revision.
    assert admin.execute('select nexloop_revision from ontology.objects where object_id=%s', (w['object'],)).fetchone()[0] == 1
    for bad in ('', ' \u0007 ', 'x' * 2001):
        with pytest.raises(psycopg.Error):
            note(w, 'note-bad-' + str(len(bad)), bad)
    with pytest.raises(psycopg.Error) as e:
        note(w, 'note-invisible', 'x', obj=hexid())
    assert e.value.sqlstate == 'P0002'


def test_machine_caps_per_client_and_per_object_and_no_policy_no_note(notes):
    w = notes
    admin = w['admin']
    with pytest.raises(psycopg.Error) as e:
        note(w, 'note-nopolicy')
    assert e.value.sqlstate == 'NX429'
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT,))
    assert admin.execute("select phase from runtime.nexloop_machine_intents where intent_id='note-nopolicy'").fetchone()[0] == 'prepare'  # not consumed
    limits(admin, per_client=3, per_object=2)
    assert [note(w, f'note-obj-{i}')['phase'] for i in range(2)] == ['committed', 'committed']
    with pytest.raises(psycopg.Error) as e:
        note(w, 'note-obj-2')
    assert e.value.sqlstate == 'NX429'
    other = put(admin, 'Commitment', commitment(w['consumer']))
    assert note(w, 'note-client-2', obj=other)['phase'] == 'committed'
    with pytest.raises(psycopg.Error) as e:
        note(w, 'note-client-3', obj=put(admin, 'Commitment', commitment(w['consumer'])))
    assert e.value.sqlstate == 'NX429'
    # People are not capped by the machine limits.
    for i in range(3):
        assert human(w, w['owner']['principal_id'], f'human-{i}')['replayed'] is False
    assert sum(1 for r in rows(admin) if r[0] == 'machine') == 3


def test_people_write_by_their_current_role(notes):
    w = notes
    admin = w['admin']
    first = human(w, w['owner']['principal_id'], 'human-owner-1')
    assert human(w, w['owner']['principal_id'], 'human-owner-1') == {**first, 'replayed': True}
    assert human(w, w['operator']['principal_id'], 'human-operator-1')['replayed'] is False
    for who in (w['outsider']['principal_id'], 'nobody'):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            human(w, who, 'human-x')
    with pytest.raises(psycopg.errors.InsufficientPrivilege):  # another subject kind
        admin.execute('select runtime.nexloop_governed_object_note(%s,%s,%s,%s,%s,%s)', (TENANT, 'real', w['owner']['principal_id'], 'agent', 'i',
                      json.dumps({'operation': 'object_note_add', 'request_id': 'i', 'type_name': 'Commitment', 'object_id': w['object'], 'text': 'x'})))
    with pytest.raises(psycopg.Error) as e:
        human(w, w['owner']['principal_id'], 'human-gone', obj=hexid())
    assert e.value.sqlstate == 'P0002'
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='append-only'):
        admin.execute("update runtime.nexloop_object_notes set text='changed'")


def test_erasure_rewrites_and_the_object_takes_its_notes(notes):
    w = notes
    admin = w['admin']
    limits(admin)
    note(w, 'note-on-object')                                          # on the Consumer's object
    unlinked = put(admin, 'CommercialRecord', {'kind': 'order', 'currency': 'CNY', 'status': 'paid'})
    note(w, 'note-names-id', '客户 ' + w['consumer'] + ' 来电', obj=unlinked, type_name='CommercialRecord')  # names the Consumer's id
    note(w, 'note-other', '与此客户无关', obj=unlinked, type_name='CommercialRecord')
    human(w, w['operator']['principal_id'], 'human-by-operator', '操作员写的', obj=put(admin, 'Commitment', commitment(hexid())))
    admin.execute("""insert into control.nexloop_consumer_erasures(tenant_id,world,request_id,consumer_id,source,requested_by,reason,state,watermark)
        values(%s,'real','note-erasure',%s,'owner','synthetic','synthetic','blocking',1)""", (TENANT, w['consumer']))
    got = {(t, e) for _, _, t, e, _ in rows(admin)}
    assert ('[已擦除]', True) in got and ('与此客户无关', False) in got and ('操作员写的', False) in got
    assert not any(w['consumer'] in t for _, _, t, _, _ in rows(admin))
    assert sum(1 for r in rows(admin) if r[3]) == 2
    # The member erasure: the operator's notes lose author and text.
    admin.execute("""insert into control.nexloop_workbench_member_erasures(tenant_id,subject_id,principals,erased_at) values(%s,%s,%s,clock_timestamp())""",
                  (TENANT, w['operator']['subject_id'], [w['operator']['principal_id']]))
    assert not any(r[4] == w['operator']['principal_id'] for r in rows(admin)) and ('操作员写的', False) not in {(r[2], r[3]) for r in rows(admin)}
    # Deleted with the object, whatever deletes it. A Commitment is never deleted and a CommercialRecord leaves only by retention
    # (its own guard, NX-029); here that guard is set aside inside a rolled-back transaction so the retention's delete is stood in for.
    gone = put(admin, 'CommercialRecord', {'kind': 'order', 'currency': 'CNY', 'status': 'paid'})
    note(w, 'note-gone-1', '随对象删除', obj=gone, type_name='CommercialRecord')
    with psycopg.connect(admin.info.dsn) as tx:
        tx.execute("select set_config('eios.tenant_id',%s,true)", (TENANT,))
        before = tx.execute('select count(*) from runtime.nexloop_object_notes').fetchone()[0]
        guard = tx.execute("""select t.tgname from pg_trigger t where t.tgrelid='ontology.objects'::regclass
            and t.tgfoid='ontology.nexloop_commercial_record_guard()'::regprocedure and t.tgtype & 8 = 8""").fetchone()[0]
        tx.execute(f'alter table ontology.objects disable trigger {guard}')
        tx.execute('delete from ontology.objects where object_id=%s', (gone,))
        assert tx.execute('select count(*) from runtime.nexloop_object_notes').fetchone()[0] == before - 1
        tx.rollback()


def holders(admin, f):
    return {r for (r,) in admin.execute("""select r.rolname from pg_proc p cross join aclexplode(p.proacl) a join pg_roles r on r.oid=a.grantee
        where p.oid=%s::regprocedure and a.privilege_type='EXECUTE' and a.grantee<>p.proowner""", (f,)).fetchall()}


ALLOWED_READERS = {'runtime.nexloop_governed_object_note(text,text,text,text,text,jsonb)', 'control.nexloop_workbench_object_notes(text,text,text,text)',
                   'runtime.nexloop_object_note_consumer_erasure()', 'runtime.nexloop_object_note_member_erasure()', 'runtime.nexloop_object_note_object_deleted()'}


def test_only_the_workbench_reads_notes(notes):
    """V-2, W1, W2: no runtime role reads the table or the read function; only the registered functions name the table (so no machine
    read verb, no evidence check and no export reaches a note); in the Python package only the workbench modules name the read."""
    w = notes
    admin = w['admin']
    for role in ('nexloop_runtime', 'nexloop_domain_worker', 'nexloop_action_worker', 'nexloop_scheduler', 'nexloop_api'):
        with psycopg.connect(make_conninfo(w['pg']), autocommit=True) as c:
            c.execute(f'set role {role}')
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute('select count(*) from runtime.nexloop_object_notes')
    assert holders(admin, 'control.nexloop_workbench_object_notes(text,text,text,text)') == {'nexloop_api'}
    assert holders(admin, 'runtime.nexloop_governed_object_note(text,text,text,text,text,jsonb)') == set()
    named = {n for (n,) in admin.execute("""select p.oid::regprocedure::text from pg_proc p join pg_namespace s on s.oid=p.pronamespace
        where s.nspname not in ('pg_catalog','information_schema') and p.prosrc like '%%nexloop_object_notes%%'""").fetchall()}
    assert named == ALLOWED_READERS
    readers = {n for (n,) in admin.execute("""select p.oid::regprocedure::text from pg_proc p where p.prosrc like '%%nexloop_workbench_object_notes%%'""").fetchall()}
    assert readers == set()
    package = ROOT / 'packages/eios-core/src/nexloop_eios'
    hits = sorted(p.name for p in package.glob('*.py') if re.search(r'nexloop_(workbench_)?object_notes', p.read_text(encoding='utf-8')))
    assert set(hits) <= {'workbench_read_http.py', 'workbench_notes.py'}, hits
    for module in ('conversation_extraction.py', 'data_export.py', 'data_export_http.py', 'machine_http.py', 'machine_writes.py'):
        assert 'object_note' not in (package / module).read_text(encoding='utf-8'), module
