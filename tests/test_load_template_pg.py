"""NX-031 slice 0 on clean catalog PostgreSQL, small scale: the manifest is loaded only through governed entries, the loaded
cluster becomes a template (pg_basebackup), and two cells clone it into identical, independent clusters."""
import importlib.util
import json
from pathlib import Path

import psycopg
from conftest import admin, pg  # noqa: F401
from test_action_definitions import published_action  # noqa: F401
from test_browser_business_authorization import browser_business  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_conversation_messages import conversations  # noqa: F401
from load_template import Clone, load, snapshot

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('load_synthetic',ROOT/'scripts/load/synthetic.py');S=importlib.util.module_from_spec(spec);spec.loader.exec_module(S)


def counts(db):
    return db.execute('''select (select count(*) from runtime.nexloop_conversations where idempotency_key like 'load-%'),
        (select count(*) from runtime.nexloop_conversation_messages where idempotency_key like 'load-%'),
        (select count(*) from control.nexloop_contact_restrictions where active),
        (select count(*) from control.nexloop_consumer_owners where principal_id like 'load-principal-%')''').fetchone()


def test_governed_load_template_and_independent_clones(conversations,admin,pg,tmp_path):
    *records,trailer=[json.loads(x) for x in S.lines(31,6)]
    t=trailer['totals']
    result=load(conversations,admin,records)
    assert result['counts']=={'consumers':6,'conversations':t['conversations'],'messages':t['messages']}
    expected=(t['conversations'],t['messages'],t['restricted'],6)
    assert counts(admin)==expected
    # Every message went through the governed write: a Message object, the customer's own Action claim, provider facts.
    assert admin.execute('''select count(*) from runtime.nexloop_conversation_messages m
        join ontology.objects o on o.tenant_id=m.tenant_id and o.world=m.world and o.type_name='Message' and o.object_id=m.message_id
        join runtime.nexloop_message_provider_facts f on f.tenant_id=m.tenant_id and f.world=m.world and f.message_id=m.message_id
        where m.idempotency_key like 'load-%' ''').fetchone()==(t['messages'],)
    claims=admin.execute("select count(distinct principal_id) from runtime.nexloop_action_claims where principal_id like 'load-principal-%'").fetchone()[0]
    assert claims==6
    # Each conversation belongs to its own customer's Consumer (no cross-assignment).
    assert admin.execute('''select count(*) from runtime.nexloop_conversations cv join control.nexloop_consumer_owners o
        on o.tenant_id=cv.tenant_id and o.world=cv.world and o.consumer_id=cv.consumer_id
        where cv.idempotency_key like 'load-%' and o.principal_id<>cv.principal_id''').fetchone()==(0,)
    assert len(result['credentials'])==6 and len({c['consumer_id'] for c in result['credentials']})==6
    snapshot(pg,tmp_path/'template')
    first,second=Clone(tmp_path/'template',tmp_path/'cell-1'),Clone(tmp_path/'template',tmp_path/'cell-2')
    try:
        with psycopg.connect(first.dsn,autocommit=True) as a,psycopg.connect(second.dsn,autocommit=True) as b:
            assert counts(a)==counts(b)==expected
            a.execute("update runtime.nexloop_conversations set last_sequence=last_sequence where false")  # writable
            a.execute("select pg_catalog.set_config('application_name','cell-1',false)")
            assert first.port!=second.port and counts(admin)==expected  # the clones never touch the loaded cluster
    finally:
        first.stop();second.stop()
    assert not (tmp_path/'cell-1').exists() and not (tmp_path/'cell-2').exists()
