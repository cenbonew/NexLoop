"""NX-031 slice 0 on clean catalog PostgreSQL, small scale: the manifest is loaded only through governed entries, the loaded
cluster becomes a template (pg_basebackup), and two cells clone it into identical, independent clusters."""
import importlib.util
import json
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo
from conftest import admin, pg  # noqa: F401
from test_action_definitions import published_action  # noqa: F401
from test_browser_business_authorization import browser_business  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_conversation_messages import conversations  # noqa: F401
from commercial_fixture import commercial_env  # noqa: F401
from load_template import Clone, apply_service_grants, load, load_commercial, load_commitments, snapshot

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('load_synthetic',ROOT/'scripts/load/synthetic.py');S=importlib.util.module_from_spec(spec);spec.loader.exec_module(S)


def counts(db):
    return db.execute('''select (select count(*) from runtime.nexloop_conversations where idempotency_key like 'load-%'),
        (select count(*) from runtime.nexloop_conversation_messages where idempotency_key like 'load-%'),
        (select count(*) from control.nexloop_contact_restrictions where active),
        (select count(*) from control.nexloop_consumer_owners where principal_id like 'load-principal-%')''').fetchone()


def test_governed_load_template_and_independent_clones(commercial_env,conversations,admin,pg,tmp_path):
    *records,trailer=[json.loads(x) for x in S.lines(31,8)]
    t=trailer['totals']
    # This fixture tenant has no trusted login policy; seed the test one (the load helpers never write rate policies).
    admin.execute("insert into control.nexloop_browser_rate_policies values('synthetic-a','local_login','nexloop_identity',50,60,true) on conflict do nothing")
    result=load(conversations,admin,records)
    assert result['counts']=={'consumers':8,'conversations':t['conversations'],'messages':t['messages']}
    expected=(t['conversations'],t['messages'],t['restricted'],8)
    assert counts(admin)==expected
    # Every message went through the governed write: a Message object, the customer's own Action claim, provider facts.
    assert admin.execute('''select count(*) from runtime.nexloop_conversation_messages m
        join ontology.objects o on o.tenant_id=m.tenant_id and o.world=m.world and o.type_name='Message' and o.object_id=m.message_id
        join runtime.nexloop_message_provider_facts f on f.tenant_id=m.tenant_id and f.world=m.world and f.message_id=m.message_id
        where m.idempotency_key like 'load-%' ''').fetchone()==(t['messages'],)
    claims=admin.execute("select count(distinct principal_id) from runtime.nexloop_action_claims where principal_id like 'load-principal-%'").fetchone()[0]
    assert claims==8
    # Each conversation belongs to its own customer's Consumer (no cross-assignment).
    assert admin.execute('''select count(*) from runtime.nexloop_conversations cv join control.nexloop_consumer_owners o
        on o.tenant_id=cv.tenant_id and o.world=cv.world and o.consumer_id=cv.consumer_id
        where cv.idempotency_key like 'load-%' and o.principal_id<>cv.principal_id''').fetchone()==(0,)
    assert len(result['credentials'])==8 and len({c['consumer_id'] for c in result['credentials']})==8
    # Commercial events through the signed intake (test connector, world test), recorded by the recorder.
    commercial=load_commercial(commercial_env,records)
    assert commercial['statuses']=={202:t['commercial_events']} if t['commercial_events'] else commercial['statuses']=={}
    assert admin.execute("select count(*) from runtime.nexloop_commercial_events where tenant_id='synthetic-a' and world='test' and data_mode='test'").fetchone()==(t['commercial_events'],)
    assert admin.execute("select count(*) from runtime.nexloop_commercial_events where world='real'").fetchone()==(0,)
    assert admin.execute("select count(*) from runtime.nexloop_work_feed where feed='commercial-record' and status='pending'").fetchone()==(0,)
    # The deployed service principals (service-grants manifest) on top of the loaded tenant, credentials kept privately.
    (tmp_path/'private').mkdir(mode=0o700)
    granted=apply_service_grants(admin,pg,tmp_path/'private')
    assert granted['credentials_created']>=10 and oct((tmp_path/'private'/'service-tokens.json').stat().st_mode)[-3:]=='600'
    # Commitments: staff promises through the workbench write entry, extraction and keeper with their deployed credentials.
    from psycopg.types.json import Jsonb
    from test_workbench_read_pg import CALLER,ROLES,WORKBENCH_APP,add_human,members_file,write_facts
    from nexloop_eios.workbench_roles import apply_members,compile_members
    from commitment_fixture import TAKEOVER_ACTIONS,publish_request_actions
    publish_request_actions(admin,names=TAKEOVER_ACTIONS)
    admin.execute('insert into control.nexloop_browser_applications values(%s,%s,1,true)',('synthetic-a',WORKBENCH_APP))
    admin.execute('insert into control.nexloop_browser_business_applications values(%s,%s,%s,%s,%s)',('synthetic-a',WORKBENCH_APP,CALLER,'1',Jsonb(['action.execute'])))
    operator=add_human(admin,'operator-1');members=members_file((operator,'operator'))
    dsn=tmp_path/'cfg-dsn';dsn.write_text(make_conninfo(pg,user='nexloop_configurator'));dsn.chmod(0o600)
    apply_members(ROLES,members,database_url_file=dsn);write_facts(admin,compile_members(ROLES,members))
    promised=load_commitments(conversations,admin,pg,records,tmp_path/'private',tmp_path/'commitment-work',operator)
    assert promised['staff_replies']=={200:t['commitment']},promised
    assert promised['registered']==t['commitment'],(promised,admin.execute("select epistemic_kind,speaker,count(*) from ontology.nexloop_claims group by 1,2").fetchall(),
        admin.execute("select feed,status,last_code,count(*) from runtime.nexloop_work_feed group by 1,2,3").fetchall(),
        admin.execute("select count(*),count(*) filter (where registered_at is not null) from runtime.nexloop_commitments").fetchall())
    assert admin.execute("select count(*) from runtime.nexloop_commitments where registered_at is not null").fetchone()==(t['commitment'],)
    assert admin.execute("select count(*) from ontology.nexloop_claims where epistemic_kind='commitment' and speaker='agent'").fetchone()==(t['commitment'],)
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
