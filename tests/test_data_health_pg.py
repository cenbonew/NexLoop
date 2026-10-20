"""Data health (D-G5, migration 0242): the health of NexLoop's own data flows, as the `data_health` block of the metrics snapshot,
alert rules v6 and the owner's workbench read GET /api/v1/workbench/data-health. Every item has a positive control: admin seeds one
synthetic fault per flow (a dead-lettered extraction job, a stale extraction feed row, a recall entry without an embedding, vectors
under another profile, a review decision waiting for reflow, a signature-rejected commercial receipt, an orphan / expired / stuck
Artifact, a stalled erasure and a retried purge item) and the read shows it. Counts, ratios, ages and codes only; reading writes
nothing; another tenant's rows are never counted. Real PostgreSQL with the NX-030 observability fixture."""
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from psycopg.types.json import Jsonb

from nexloop_eios.observability import apply_rules, load_rules
from support.tenants import TENANT_B
from test_data_assets_metrics_pg import counts, recall
from test_observability_pg import observed  # noqa: F401
from test_real_dispatch_switch_pg import owner_sql
from test_workbench_read_pg import (TENANT, browser_business, conversations, identity, published_action, uow, workbench,  # noqa: F401
    workbench_login)

V6 = Path(__file__).resolve().parents[1] / 'deploy/configuration/alert-rules.v6.json'
LONG_AGO = datetime.now(UTC) - timedelta(days=2)
SECTIONS = ['extraction', 'recall', 'reflow', 'ingestion', 'artifacts', 'erasure']


def health(admin, tenant=TENANT):
    return owner_sql(admin, "select runtime.nexloop_data_health(%s,'real')", (tenant,))


def replica(admin, sql, params):
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute(sql, params)


def dead_letter(admin):
    replica(admin, """insert into runtime.jobs(job_id,invocation_id,capability_name,capability_version,capability_type,execution_mode,tenant_id,actor_id,trace_id,
        request_id,normalized_input,plan_snapshot,status,created_at,updated_at,world,queue,attempts,max_attempts)
        values(%s,%s,'nexloop.claim.extract','1','atomic','async',%s,'synthetic','synthetic',%s,'{}','{}','dead_lettered',now(),now(),'real','claim-extraction',3,3)""",
        (str(uuid.uuid4()), str(uuid.uuid4()), TENANT, str(uuid.uuid4())))


def stale_feed(admin, conversation):
    replica(admin, """insert into runtime.nexloop_claim_extraction_feed(tenant_id,world,conversation_id,message_id,sequence,created_at) values(%s,'real',%s,%s,99,%s)""",
            (TENANT, conversation, secrets.token_hex(32), datetime.now(UTC) - timedelta(hours=2)))


def reflow_waiting(admin):
    replica(admin, """insert into ontology.nexloop_review_decisions(tenant_id,world,decision_id,candidate_id,decision,outcome,reviewer_ref,reviewer_principal,
        expected_revision,rationale,record,authority,reflow_status,decided_at) values(%s,'real',%s,'synthetic-candidate','approve','published','human:synthetic',
        'synthetic',1,'合成审核','{}','{}','pending',%s)""", (TENANT, str(uuid.uuid4()), datetime.now(UTC) - timedelta(hours=3)))


def rejected_receipt(admin, tenant=TENANT):
    replica(admin, "insert into runtime.nexloop_commercial_receipts(tenant_id,world,connector_id,outcome,reason) values(%s,'real','synthetic-connector','rejected_signature','bad_signature')",
            (tenant,))


def artifact(admin, *, status='available', created=LONG_AGO, retention=None, updated=None):
    artifact_id = secrets.token_hex(16)
    replica(admin, """insert into runtime.nexloop_local_artifacts(tenant_id,world,artifact_id,principal_id,sha256,size_bytes,media_type,object_key,retention_until,status,
        payload_digest,upload_worker,upload_token,upload_fence,lease_until,permit_id,created_at,updated_at)
        values(%s,'real',%s,'synthetic',%s,10,'application/octet-stream',%s,%s,%s,%s,'synthetic','synthetic',1,now(),'synthetic',%s,%s)""",
        (TENANT, artifact_id, 'a' * 64, 'synthetic/' + artifact_id, retention or datetime.now(UTC) + timedelta(days=30), status, 'b' * 64, created, updated or created))
    return artifact_id


def stalled_erasure(admin, consumer):
    replica(admin, """insert into control.nexloop_consumer_erasures(tenant_id,world,request_id,consumer_id,source,requested_by,reason,state,watermark,created_at,updated_at)
        values(%s,'real','synthetic-erasure',%s,'owner','synthetic','合成删除','purging',1,%s,%s)""", (TENANT, consumer, LONG_AGO, LONG_AGO))
    replica(admin, """insert into runtime.nexloop_run_purge_items(tenant_id,world,run_id,request_ref,reason,attempts,last_code) values(%s,'real',%s,'synthetic-erasure',
        'consumer_erasure',3,'artifact_unavailable')""", (TENANT, str(uuid.uuid4())))


def test_a_clean_tenant_is_healthy_and_the_snapshot_carries_the_block(observed):
    o = observed; admin = o['admin']
    h = health(admin)
    assert [h[s]['status'] for s in SECTIONS] == ['ok'] * 6 and h['attention'] == 0
    snapshot = owner_sql(admin, "select runtime.nexloop_metrics_snapshot(%s,'real')", (TENANT,))
    assert set(snapshot['data_health']) == set(h) and [snapshot['data_health'][s] for s in SECTIONS if s != 'ingestion'] == [h[s] for s in SECTIONS if s != 'ingestion']


def test_every_flow_shows_its_positive_control(observed):
    o = observed; admin = o['admin']; w = o['w']
    before = counts(admin)
    # Extraction: one dead letter and a feed row waiting for two hours.
    pending = health(admin)['extraction']['feed_pending']  # the fixture's own inbound messages may wait in the feed
    dead_letter(admin); stale_feed(admin, w['conversation']['id'])
    x = health(admin)['extraction']
    assert x['dead_lettered'] == 1 and x['jobs']['dead_lettered'] == 1 and x['feed_pending'] == pending + 1 and x['oldest_pending_seconds'] >= 7000
    assert x['status'] == 'attention' and set(x['reasons']) >= {'dead_letters', 'backlog_stale'}
    # Recall: entries but no active profile, then the definition entry without an embedding, then vectors under another profile.
    recall(admin, TENANT, 'synthetic-model@3', 'mine')
    r = health(admin)['recall']
    assert (r['entries'], r['active_profile'], r['missing_embeddings']) == (2, None, 2) and set(r['reasons']) == {'no_active_profile', 'missing_embeddings'}
    admin.execute("insert into ontology.nexloop_recall_settings(tenant_id,active_profile_id) values(%s,'synthetic-model@3')", (TENANT,))
    r = health(admin)['recall']
    assert (r['missing_embeddings'], r['coverage_ratio'], r['other_profile_vectors'], r['reasons']) == (1, 0.5, 0, ['missing_embeddings'])
    admin.execute("insert into ontology.nexloop_embedding_profiles(tenant_id,profile_id,model,dimension) values(%s,'other-model@3','other-model',3)", (TENANT,))
    entry = admin.execute("select entry_id from ontology.nexloop_recall_entries where tenant_id=%s and index_kind='instance'", (TENANT,)).fetchone()[0]
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT,))
        admin.execute("insert into ontology.nexloop_recall_embeddings(entry_id,tenant_id,profile_id,embedding) values(%s,%s,'other-model@3','[0,1,0]')", (entry, TENANT))
    r = health(admin)['recall']
    assert r['other_profile_vectors'] == 1 and 'profile_drift' in r['reasons']
    # Review reflow waiting for three hours.
    reflow_waiting(admin)
    rf = health(admin)['reflow']
    assert (rf['pending'], rf['status'], rf['reasons']) == (1, 'attention', ['reflow_stale']) and rf['oldest_seconds'] >= 10000
    # Ingestion: a signed commercial event rejected for its signature.
    rejected_receipt(admin)
    ig = health(admin)['ingestion']
    assert ig['commercial_receipts_24h'] == {'rejected_signature': 1} and ig['commercial_rejected_24h'] == 1 and ig['reasons'] == ['signature_rejections']
    # Artifacts: an unreferenced one older than a day, one past retention, one stuck deleting.
    artifact(admin)
    artifact(admin, created=datetime.now(UTC) - timedelta(hours=1), retention=datetime.now(UTC) - timedelta(minutes=5))
    artifact(admin, status='deleting', created=LONG_AGO, updated=datetime.now(UTC) - timedelta(hours=2))
    a = health(admin)['artifacts']
    assert (a['orphans'], a['retention_passed'], a['stuck_deleting']) == (1, 1, 1) and set(a['reasons']) == {'orphans', 'retention_passed', 'stuck_deleting'}
    # Erasure: open for two days, one purge item retried three times.
    stalled_erasure(admin, w['f']['consumer'])
    e = health(admin)['erasure']
    assert (e['open'], e['purge_items_retrying']) == (1, 1) and e['oldest_open_seconds'] >= 170000 and set(e['reasons']) == {'erasure_stalled', 'purge_retrying'}
    h = health(admin)
    assert h['attention'] == 6
    # Codes and numbers only: no recall body, vector or payload.
    text = json.dumps(h, ensure_ascii=False)
    assert 'synthetic recall body' not in text and '[1,0,0]' not in text and 'bad_signature' not in text
    # Another tenant sees none of it (its own receipt only).
    rejected_receipt(admin, TENANT_B)
    theirs = health(admin, TENANT_B)
    assert theirs['extraction']['dead_lettered'] == 0 and theirs['artifacts']['orphans'] == 0 and theirs['ingestion']['commercial_rejected_24h'] == 1
    assert counts(admin)[2:4] == before[2:4]  # reading wrote nothing: no audit event, no read-audit row


def test_alert_rules_v6_fire_on_the_data_health_block(observed):
    o = observed; admin = o['admin']
    rules = load_rules(V6)
    ours = {r['rule_id']: r for r in rules['rules'] if r['rule_id'].startswith('data_health_')}
    assert len(ours) == 10 and all(r['metric'].startswith('data_health.') for r in ours.values())
    assert {r['rule_id'] for r in load_rules(V6.parent / 'alert-rules.v5.json')['rules']} < {r['rule_id'] for r in rules['rules']}  # v5 kept
    assert apply_rules(rules, TENANT, database_url_file=o['w']['configurator'])['configured'] is True
    assert o['evaluate']()['rules_version'] == 6
    firing = lambda: {r[0] for r in admin.execute("select rule_id from control.nexloop_alert_state where tenant_id=%s and firing", (TENANT,)).fetchall()}
    assert not {r for r in firing() if r.startswith('data_health_')}
    dead_letter(admin); stalled_erasure(admin, o['w']['f']['consumer']); rejected_receipt(admin)
    o['evaluate']()
    assert {'data_health_extraction_dead_letters', 'data_health_erasure_stalled', 'data_health_purge_retrying', 'data_health_signature_rejections'} <= firing()


def test_the_read_is_the_owners_and_counts_only(observed):
    o = observed; admin = o['admin']; w = o['w']
    dead_letter(admin)
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        response = client.get('/api/v1/workbench/data-health')
        assert response.status_code == 200, response.text
        body = response.json()
        assert body['extraction']['dead_lettered'] == 1 and body['attention'] == 1 and set(body) == set(SECTIONS) | {'computed_at', 'attention'}
        assert client.get('/api/v1/workbench/data-health?world=test').status_code == 422
    with w['client']() as client:
        assert workbench_login(client, w['customer']).status_code == 200
        assert client.get('/api/v1/workbench/data-health').status_code == 403
