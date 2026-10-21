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


def call(config, *more):
    out = io.StringIO()
    code = I.main(['--config', config, *more], out=out, now=NOW + timedelta(minutes=5))
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


def test_a_real_consumer_is_never_offered_for_erasure_even_when_real_is_read(stage):  # noqa: F811 - the imported fixture
    admin, real = stage['admin'], stage['consumers'][0]
    admin.execute("update control.nexloop_consumer_personas set persona='real' where tenant_id=%s and consumer_id=%s", (TENANT_A, real))
    _, text, doc = call(stage['config'], '--personas', 'simulated_agent,synthetic,real')
    assert doc['sources']['window']['evidence']['consumers'] == 5  # real is read for the window counts
    chosen = doc['sources']['erasure_consumer_id']
    assert chosen['value'] in stage['consumers'][1:] and real not in chosen['candidates']
    assert real not in text
    # Only real Consumers in the window: nothing to erase (blocked), never a real one.
    admin.execute("update control.nexloop_consumer_personas set persona='real' where tenant_id=%s", (TENANT_A,))
    _, text, doc = call(stage['config'], '--personas', 'real')
    assert doc['sources']['erasure_consumer_id']['status'] == 'blocked' and doc['inputs']['evolution']['erasure_consumer_id'] == stage['consumers'][2]
    assert not any(c in text for c in stage['consumers'] if c != stage['consumers'][2])  # only the configuration's own value


def test_the_second_probe_changes_semantic_alone(stage):  # noqa: F811 - the imported fixture
    admin = stage['admin']
    config = json.loads(Path(stage['config']).read_text())
    _, _, doc = call(stage['config'], '--only', 'semantic')
    assert doc['inputs']['evolution'] == config['inputs']['evolution']  # window, refs, erasure Consumer as configured
    assert set(doc['sources']) == {'semantic'} and doc['blocked'] == ['semantic']
    cid = uuid.uuid4()
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("""insert into runtime.nexloop_evo_candidates(tenant_id,world,candidate_id,diagnosis_id,category,dimension,parent_revision,candidate_revision,
            primary_hypothesis,patch_id,input_snapshot_digest,design_case_ids,validation_case_ids,case_groups,evaluator_version,model_profile,maximum_rounds,
            allowed_change_scope,status,cost_limit,unit) values(%s,'real',%s,%s,'retrieval_miss','semantic_content','sem-7','sem-7+s6','a synthetic hypothesis',%s,%s,
            '{a}','{b}','{}','l0-1','none',1,'{eios:object_type:Product}','accepted_for_release',1,'CNY')""", (TENANT_A, cid, uuid.uuid4(), uuid.uuid4(), 'e' * 64))
    code, _, doc = call(stage['config'], '--only', 'semantic')
    assert code == 0 and doc['blocked'] == [] and doc['inputs']['semantic']['candidate_id'] == str(cid)
    assert doc['inputs']['semantic']['items'][0]['ref'] == 'eios:object_type:Product'
    assert doc['inputs']['evolution'] == config['inputs']['evolution']


def test_connect_read_only_refuses_a_session_that_stays_writable(monkeypatch, tmp_path):
    import psycopg
    import s6_common as C

    class Conn:
        closed = False
        def execute(self, sql, *a):
            return type('R', (), {'fetchone': lambda self: ('off',)})()
        def close(self):
            Conn.closed = True
    monkeypatch.setattr(psycopg, 'connect', lambda *a, **k: Conn())
    dsn = tmp_path / 'dsn'
    dsn.write_text('dbname=x')
    dsn.chmod(0o600)
    import pytest
    with pytest.raises(C.Refused, match='nx043 reader is not read only'):
        C.connect_read_only(str(dsn), who='nx043 reader')
    assert Conn.closed
