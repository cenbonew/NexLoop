"""Enterprise world model W4 "data assets": the `data_assets` block of the M12 metrics snapshot (0225), served by the workbench
metrics read. Counts, profile ids, dimensions and times only; never a Claim value, quote, recall body or vector; embedding spend is
not recorded anywhere and is reported as unavailable, not 0. Another tenant's rows are never counted. Reading writes nothing.

Real PostgreSQL and the workbench login cookies of the NX-030 observability fixture (test_observability_pg). Admin only seeds
synthetic rows (Claims, an extraction run, recall entries and vectors, a backfill record) and probes.
"""
import hashlib
from datetime import UTC, datetime

from psycopg.types.json import Jsonb

from support.tenants import TENANT_B
from test_observability_pg import observed  # noqa: F401
from test_real_dispatch_switch_pg import owner_sql
from test_workbench_read_pg import (TENANT, browser_business, conversations, identity, published_action, uow, workbench,  # noqa: F401
    workbench_login)

QUOTE = '下周三之前寄出'


def hexid(label):
    return hashlib.sha256(label.encode()).hexdigest()


def claim(admin, conversation, consumer, message, label, kind, state):
    """A Claim on the fixture's inbound message (source span, sequence and content hash as the extractor records them)."""
    sequence, body = admin.execute("""select m.sequence,o.properties->>'body' from runtime.nexloop_conversation_messages m join ontology.objects o
        on o.tenant_id=m.tenant_id and o.world=m.world and o.type_name='Message' and o.object_id=m.message_id where m.message_id=%s""", (message,)).fetchone()
    admin.execute("""insert into ontology.nexloop_claims(tenant_id,world,claim_id,conversation_id,consumer_id,first_input_digest,topic_key,subject_kind,subject_ref,
        subject_text,predicate,value,speaker,polarity,modality,condition_text,time_expression,valid_time,source_message_id,source_sequence,span_start,span_end,
        source_content_hash,quote,extractor_version,confidence,epistemic_kind,resolution_state,derived_from,correlation_key,guard_flags)
        values(%s,'real',%s,%s,%s,%s,'order','consumer',%s,'客户','delivery_promise',%s,'agent','affirmed','asserted','','下周三',%s,%s,%s,0,2,%s,%s,'synthetic-1',0.9,
        %s,%s,'[]',%s,'[]')""",
        (TENANT, hexid(label), conversation, consumer, 'd' * 64, consumer, Jsonb({'type': 'text', 'value': '寄出'}),
         Jsonb({'kind': 'none', 'status': 'absent', 'start': None, 'end': None, 'anchor': datetime.now(UTC).isoformat(), 'timezone': 'Asia/Shanghai', 'expression': ''}),
         message, sequence, hashlib.sha256(body.encode()).hexdigest(), QUOTE, kind, state, hexid('correlation-' + label)))


def recall(admin, tenant, profile, label):
    admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (tenant,))
    admin.execute("insert into ontology.nexloop_embedding_profiles(tenant_id,profile_id,model,dimension) values(%s,%s,'synthetic-model',3) on conflict do nothing",
                  (tenant, profile))
    entries = []
    for kind, world, object_id, field in (('definition', None, None, 'name'), ('instance', 'real', hexid(label), 'title')):
        entries.append(admin.execute("""insert into ontology.nexloop_recall_entries(tenant_id,world,index_kind,source_key,source_revision,ref,target_kind,gate_type,object_id,field,
            body,tokenizer_version,tsv,ngrams) values(%s,%s,%s,%s,1,%s,%s,'Consumer',%s,%s,%s,'t1',to_tsvector('simple','synthetic'),'{}') returning entry_id""",
            (tenant, world, kind, label + '-' + kind, 'eios:object_type:Consumer' if kind == 'definition' else 'eios:object:Consumer/' + hexid(label),
             'object_type' if kind == 'definition' else 'object_instance', object_id, field, 'synthetic recall body ' + label)).fetchone()[0])
    with admin.transaction():  # the embedding guard reads the profile under the tenant's row security
        admin.execute("select set_config('eios.tenant_id',%s,true)", (tenant,))
        admin.execute("insert into ontology.nexloop_recall_embeddings(entry_id,tenant_id,profile_id,embedding) values(%s,%s,%s,'[1,0,0]')", (entries[1], tenant, profile))
    admin.execute("""insert into ontology.nexloop_recall_backfills(tenant_id,world,profile_id,runs,last_queued,last_definition_types) values(%s,'real',%s,2,7,3)""",
                  (tenant, profile))


def counts(admin):
    return admin.execute("""select (select count(*) from ontology.nexloop_claims),(select count(*) from ontology.nexloop_recall_entries),
        (select count(*) from runtime.audit_events),(select count(*) from runtime.nexloop_workbench_read_audit),(select count(*) from control.nexloop_alert_events)""").fetchone()


def test_data_assets_counts_only_this_tenant_and_says_unavailable_for_what_is_not_recorded(observed):
    o = observed
    w, admin = o['w'], o['admin']
    consumer, conversation = w['f']['consumer'], w['conversation']['id']
    message = w['refusal']['id']
    claim(admin, conversation, consumer, message, 'c1', 'commitment', 'resolved')
    claim(admin, conversation, consumer, message, 'c2', 'preference', 'unresolved')
    claim(admin, conversation, consumer, message, 'c3', 'preference', 'unresolved')
    admin.execute("""insert into ontology.nexloop_extraction_runs(tenant_id,world,input_digest,conversation_id,consumer_id,principal_id,extractor_version,prompt_version,
        provider,model_id,timezone,message_refs,rejected) values(%s,'real',%s,%s,%s,'synthetic','x1','p1','load-rules','load-rules','Asia/Shanghai','[]','[]')""",
                  (TENANT, 'e' * 64, conversation, consumer))
    recall(admin, TENANT, 'synthetic-model@3', 'mine')
    recall(admin, TENANT_B, 'synthetic-model@3', 'theirs')  # another tenant: same profile id, never counted here
    before = counts(admin)
    snapshot = owner_sql(admin, 'select runtime.nexloop_metrics_snapshot(%s,%s)', (TENANT, 'real'))
    d = snapshot['data_assets']
    assert d['claims'] == {'total': 3, 'by_epistemic_kind': {'commitment': 1, 'preference': 2}, 'by_resolution_state': {'resolved': 1, 'unresolved': 2}}
    assert d['extraction_runs']['total'] == 1 and d['extraction_runs']['last_at']
    assert d['extraction_jobs'] == {s: q for s, q in (snapshot['queues'].get('claim-extraction') or {}).items() if s in d['extraction_jobs']}
    assert d['recall']['entries'] == {'definition': 1, 'instance': 1}
    assert [(p['profile_id'], p['dimension'], p['vectors']) for p in d['recall']['profiles']] == [('synthetic-model@3', 3, 1)]
    assert [(b['profile_id'], b['runs'], b['last_queued'], b['last_definition_types']) for b in d['recall']['backfills']] == [('synthetic-model@3', 2, 7, 3)]
    assert d['embedding_budget'] == {'status': 'unavailable', 'reason': 'not_recorded'}  # missing is never 0
    # The existing blocks are reused, not repeated.
    assert 'candidates' in snapshot and 'backup' in snapshot and 'candidates' not in d and 'backup' not in d
    text = str(d)
    assert QUOTE not in text and 'synthetic recall body' not in text and '[1,0,0]' not in text
    # The other tenant reads only its own numbers.
    theirs = owner_sql(admin, 'select runtime.nexloop_metrics_snapshot(%s,%s)', (TENANT_B, 'real'))['data_assets']
    assert theirs['claims']['total'] == 0 and theirs['recall']['entries'] == {'definition': 1, 'instance': 1} and theirs['extraction_runs']['total'] == 0
    # Through the owner's workbench metrics read: the same block; nothing written by reading.
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        got = client.get('/api/v1/workbench/metrics')
        assert got.status_code == 200 and got.json()['data_assets']['claims']['total'] == 3 and QUOTE not in got.text
    assert counts(admin) == before
    with w['client']() as client:
        assert workbench_login(client, w['customer']).status_code == 200
        assert client.get('/api/v1/workbench/metrics').status_code == 403
