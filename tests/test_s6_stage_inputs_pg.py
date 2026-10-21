"""S6 stage inputs helper (scripts/acceptance/s6_stage_inputs.py): on the S6 stage fixture (five synthetic Consumers with failed Runs and
semantic reads) it finds every input read only, says blocked with a remedy when the stage lacks one, and prints no private value."""
from __future__ import annotations

import io
import json
import sys
import uuid
from datetime import timedelta
from pathlib import Path

from psycopg.types.json import Jsonb

from support.tenants import TENANT_A
from test_s6_stage_evolution_pg import NOW, everything, stage  # noqa: F401 - the stage fixture

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/acceptance'))
import s6_stage_inputs as I  # noqa: E402

LIMIT = 'eios:property:Consumer/contact_limit'


def call(config):
    out = io.StringIO()
    code = I.main(['--config', config], out=out, now=NOW + timedelta(minutes=5))
    return code, out.getvalue(), json.loads(out.getvalue())


def wide_read_with_a_contact_limit(admin):
    run = admin.execute("select run_id from runtime.nexloop_semantic_accesses where tenant_id=%s and kind='access' limit 1", (TENANT_A,)).fetchone()[0]
    refs = [f'eios:property:Consumer/q{i}' for i in range(9)] + [LIMIT]
    admin.execute("""insert into runtime.nexloop_semantic_accesses(tenant_id,world,access_id,kind,run_id,operation,query_kind,term_hashes,coverage,ambiguity,
        result_refs,result_count,unresolved_count,recorded_at) values(%s,'real',%s,'access',%s,'browse','any',%s,0,false,%s,10,0,%s)""",
                  (TENANT_A, uuid.uuid4(), run, ['d' * 64], Jsonb([{'ref': r, 'revision': '1'} for r in refs]), NOW - timedelta(minutes=20)))


def test_finds_the_window_consumer_and_types_read_only_and_blocks_what_the_stage_lacks(stage):  # noqa: F811 - the imported fixture
    admin = stage['admin']
    wide_read_with_a_contact_limit(admin)
    admin.execute("insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,'Consumer',1,%s)",
                  (TENANT_A, Jsonb({'type_name': 'Consumer', 'properties': [{'property_name': 'contact_limit', 'value_type': 'string'}]})))
    before = everything(admin)
    code, text, doc = call(stage['config'])
    assert everything(admin) == before
    src = doc['sources']
    assert src['window']['status'] == 'ok' and src['window']['evidence']['consumers_with_miss'] == 5
    w = doc['inputs']['evolution']['window']
    assert w['since'] <= (NOW - timedelta(minutes=50)).isoformat() and w['until'] >= (NOW - timedelta(minutes=30)).isoformat()
    assert src['erasure_consumer_id']['value'] in stage['consumers']
    assert src['protected_refs']['value'] == [LIMIT]
    assert src['schema_type']['value'] == 'Consumer' and src['new_property']['status'] == 'ok' and src['new_type']['status'] == 'ok'
    # Nothing published or accepted on the fixture: blocked, with a remedy; the configuration's own value stays in the draft.
    assert src['parent_revision']['status'] == 'blocked' and src['parent_revision']['remedy']
    assert doc['inputs']['evolution']['parent_revision'] == 'sem-7'
    assert src['semantic']['status'] == 'blocked' and 'at023' in src['semantic']['remedy']
    assert src['candidate_template']['status'] == 'blocked'
    assert code == 1 and set(doc['blocked']) >= {'parent_revision', 'semantic', 'candidate_template'}
    # No tenant, path or DSN in the output.
    assert TENANT_A not in text and str(stage['tmp']) not in text and 'postgres' not in text


def test_published_release_and_accepted_candidate_are_found(stage):  # noqa: F811 - the imported fixture
    admin = stage['admin']
    release = uuid.uuid4()
    with admin.transaction():
        admin.execute("set local session_replication_role=replica")
        admin.execute("""insert into ontology.nexloop_semantic_releases(tenant_id,world,release_id,version,major,minor,patch,reason,candidate_id,report_digest,
            change_class,owner_reason,published_by,request_intent) values(%s,'real',%s,'0.1.0',0,1,0,'publish',%s,%s,'patch','s6','owner','i')""",
                      (TENANT_A, release, uuid.uuid4(), 'c' * 64))
        admin.execute("insert into ontology.nexloop_semantic_release_pointers(tenant_id,world,release_id,version) values(%s,'real',%s,'0.1.0')", (TENANT_A, release))
    code, _, doc = call(stage['config'])
    assert doc['sources']['parent_revision'] == {'status': 'ok', 'value': '0.1.0', 'source': doc['sources']['parent_revision']['source']}
    assert doc['inputs']['evolution']['parent_revision'] == '0.1.0'


def test_an_empty_window_is_blocked_with_a_remedy(stage):  # noqa: F811 - the imported fixture
    admin = stage['admin']
    admin.execute("delete from control.nexloop_consumer_personas where tenant_id=%s", (TENANT_A,))
    code, _, doc = call(stage['config'])
    assert code == 1
    assert doc['sources']['window']['status'] == 'blocked' and 'synthetic interactions' in doc['sources']['window']['remedy']
    assert doc['sources']['erasure_consumer_id']['status'] == 'blocked'


def test_the_inspector_must_be_configured(tmp_path):
    path = tmp_path / 'c.json'
    path.write_text(json.dumps({'schema': 'nexloop-s6-stage/1', 'executor': 'x', 'release': 'r', 'tenant_id': 't', 'results_dir': '/r', 'roles': {}}))
    path.chmod(0o600)
    out = io.StringIO()
    assert I.main(['--config', str(path)], out=out) == 2
    assert 'inspector' in json.loads(out.getvalue())['refused']
