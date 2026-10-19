"""Enterprise world model W4: the workbench Artifact list (GET /api/v1/workbench/artifacts, verb `artifacts`).

Real PostgreSQL and workbench login cookies on the slice 1 fixture (test_workbench_read_pg). Metadata only (never content or
storage keys), newest first with a cursor; owners and operators only (ADR-025 / ADR-035); an Artifact of a Consumer being erased,
or one an erasure is deleting, is hidden (counted); each listed Artifact leaves one read audit row (kind artifact, purpose
data_assets); another tenant's Artifacts never appear. Admin only seeds synthetic rows (the context bindings with foreign-key
triggers off inside one transaction: the bindings' Run and context rows are not what this test is about) and probes.
"""
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jsonschema
from psycopg.types.json import Jsonb

from erasure_support import mark_erasing
from support.tenants import TENANT_B
from test_workbench_read_pg import (TENANT, browser_business, conversations, identity, provision, published_action, uow,  # noqa: F401
    workbench, workbench_login)

CONTRACT = json.loads((Path(__file__).resolve().parents[1] / 'packages/contracts/workbench-artifacts.schema.json').read_text())


def art(n):
    return hashlib.sha256(f'w4-artifact-{n}'.encode()).hexdigest()[:32]


def artifact(admin, artifact_id, created, *, tenant=TENANT, status='available'):
    admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (tenant,))
    admin.execute("""insert into runtime.nexloop_local_artifacts(tenant_id,world,artifact_id,principal_id,sha256,size_bytes,media_type,object_key,retention_until,status,
        payload_digest,upload_worker,upload_token,upload_fence,lease_until,permit_id,created_at)
        values(%s,'real',%s,'synthetic-source',%s,1234,'application/vnd.nexloop.context+json','secret/object/key/'||%s,%s,%s,%s,'w','t',1,clock_timestamp(),'permit',%s)""",
        (tenant, artifact_id, hashlib.sha256(artifact_id.encode()).hexdigest(), artifact_id, created + timedelta(days=30), status, 'd' * 64, created))


def seed(w):
    admin = w['admin']
    consumer, message = w['f']['consumer'], w['refusal']['id']
    other_consumer = hashlib.sha256(b'w4-other-consumer').hexdigest()
    now = datetime.now(UTC).replace(microsecond=0)
    ids = {k: art(k) for k in ('other', 'message', 'role_erasing', 'erasure', 'pending', 'tenant_b')}
    artifact(admin, ids['other'], now - timedelta(minutes=1))
    artifact(admin, ids['message'], now - timedelta(minutes=2))
    artifact(admin, ids['role_erasing'], now - timedelta(minutes=3))
    artifact(admin, ids['erasure'], now - timedelta(minutes=4))
    artifact(admin, ids['pending'], now - timedelta(minutes=5), status='pending')
    artifact(admin, ids['tenant_b'], now, tenant=TENANT_B)
    run_message, run_role = uuid.uuid4(), uuid.uuid4()
    with admin.transaction():
        admin.execute('set local session_replication_role = replica')
        admin.execute("""insert into runtime.nexloop_context_artifact_bindings(tenant_id,world,message_id,run_id,source_principal,source_digest,context_id,artifact_id,namespace,
            pack_text,pack_digest,command_binding,created_at) values(%s,'real',%s,%s,'s','d',%s,%s,'n','{}',%s,'{}',clock_timestamp())""",
            (TENANT, message, run_message, uuid.uuid4(), ids['message'], 'e' * 64))
        admin.execute("""insert into runtime.nexloop_role_context_artifacts(run_id,tenant_id,world,source_principal,source_digest,context_id,artifact_id,namespace,pack_text,
            pack_digest,command_binding,created_at) values(%s,%s,'real','s','d',%s,%s,'n','{}',%s,%s,clock_timestamp())""",
            (run_role, TENANT, uuid.uuid4(), ids['role_erasing'], 'e' * 64, Jsonb({'consumer_ref': 'consumer:' + other_consumer})))
    admin.execute("insert into runtime.nexloop_erasure_artifacts(tenant_id,world,artifact_id,request_id) values(%s,'real',%s,'w4-erasure')", (TENANT, ids['erasure']))
    mark_erasing(admin, TENANT, other_consumer)
    return ids, consumer, str(run_message)


def test_owner_lists_artifact_metadata_newest_first_hides_erasing_ones_and_audits_each(workbench):
    w = workbench
    provision(w)
    admin = w['admin']
    ids, consumer, run_message = seed(w)
    audit = lambda: admin.execute("select target_resource from runtime.nexloop_workbench_read_audit where read_purpose='data_assets' and object_kind='artifact' order by audit_id").fetchall()  # noqa: E731
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        first = client.get('/api/v1/workbench/artifacts?limit=1')
        assert first.status_code == 200, first.text
        page = first.json()
        jsonschema.validate(page, CONTRACT)
        assert [i['artifact_id'] for i in page['items']] == [ids['other']] and page['total'] == 2 and page['hidden'] == 2 and page['next_before']
        assert page['items'][0]['purpose'] == 'other' and page['items'][0]['sha256_prefix'] == hashlib.sha256(ids['other'].encode()).hexdigest()[:12]
        rest = client.get('/api/v1/workbench/artifacts', params={'before': page['next_before']}).json()  # the cursor carries '+': URL-encoded
        jsonschema.validate(rest, CONTRACT)
        (item,) = rest['items']
        assert (item['artifact_id'], item['purpose'], item['run_id'], item['consumer_id'], item['size_bytes']) == (ids['message'], 'message_context', run_message, consumer, 1234)
        assert rest['next_before'] is None
        text = first.text + json.dumps(rest)
        # Never content, storage keys or the hidden / pending / other-tenant Artifacts.
        assert 'secret/object/key' not in text and '"{}"' not in text
        assert not any(ids[k] in text for k in ('role_erasing', 'erasure', 'pending', 'tenant_b'))
        # One audit row per listed Artifact.
        assert audit() == [('eios:artifact:' + ids['other'],), ('eios:artifact:' + ids['message'],)]
        assert client.get('/api/v1/workbench/artifacts?limit=101').status_code == 422
        assert client.get('/api/v1/workbench/artifacts?before=yesterday').status_code == 422
    with w['client']() as client:
        assert workbench_login(client, w['operator']).status_code == 200
        assert client.get('/api/v1/workbench/artifacts').json()['total'] == 2
    for human in (w['outsider'], w['customer']):
        with w['client']() as client:
            assert workbench_login(client, human).status_code == 200
            assert client.get('/api/v1/workbench/artifacts').status_code == 403


def test_an_artifact_of_a_consumer_being_erased_disappears_from_the_list(workbench):
    w = workbench
    provision(w)
    ids, consumer, _ = seed(w)
    mark_erasing(w['admin'], TENANT, consumer)
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        page = client.get('/api/v1/workbench/artifacts').json()
    assert [i['artifact_id'] for i in page['items']] == [ids['other']] and page['hidden'] == 3
