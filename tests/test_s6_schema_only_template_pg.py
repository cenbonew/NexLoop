"""candidate-template --mode schema_only (owner 2026-11-07, option A; docs/implementation/schema-only-candidate-template.md, L6 PASS v2)
on the S6 stage fixture: an owner-directed Schema template is made without any failed Run, only for a listed property, with at least
2 cluster units; its candidate is owner_directed_schema (0419: ontology_schema only); every acceptance check refuses a configuration
whose template is one; a regenerated template opens a new diagnosis; the diagnosis and candidate read back without error.
Real PostgreSQL, synthetic data only."""
from __future__ import annotations

import io
import json
import os
import stat
import sys
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from support.tenants import TENANT_A
from test_s6_stage_evolution_pg import everything, stage  # noqa: F401 - the stage fixture

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/acceptance'))
import s6_stage_evolution as S  # noqa: E402

PROPERTY = {'type_name': 'Consumer', 'property_name': 'delivery_windows', 'decision': 'owner 2026-11-07 / claim-consumer-plan §10-2'}


def configure(stage, owner_directed=(PROPERTY,)):
    path = Path(stage['config'])
    config = json.loads(path.read_text())
    config['inputs']['owner_directed_properties'] = list(owner_directed)
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    return config


def call(stage, *argv):
    out = io.StringIO()
    code = S.main(list(argv) + ['--config', stage['config']], out=out)
    return code, [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def schema_only(stage, *more):
    return call(stage, 'candidate-template', '--mode', 'schema_only', '--property', 'Consumer.delivery_windows', *more)


def no_failed_runs(admin):
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("update runtime.jobs set status='succeeded',result='{}' where tenant_id=%s and job_id like 'task_%%'", (TENANT_A,))


def diagnoses(admin):
    return admin.execute('select diagnosis_id::text,attribution,status from runtime.nexloop_evo_diagnoses where tenant_id=%s order by opened_at', (TENANT_A,)).fetchall()


def template(stage):
    path = json.loads(Path(stage['config']).read_text())['inputs']['candidate_template']
    return path, json.loads(Path(path).read_text())


def writer(stage):
    """The writer's governed entry, as r8's ports call it (the writer role with its own credential)."""
    import hashlib
    token = stage['tokens'][0]
    digest = hashlib.sha256(token.encode()).hexdigest()
    conn = psycopg.connect(make_conninfo(stage['pg'], user='nexloop_evo_writer'), autocommit=True)
    return conn, digest


def propose(stage, body):
    conn, digest = writer(stage)
    with conn:
        return conn.execute('select authz.nexloop_evo_candidate_propose(%s,%s)', (digest, Jsonb(body))).fetchone()[0]


def test_no_failed_run_still_makes_an_owner_directed_template_where_the_default_mode_cannot(stage):  # noqa: F811 - the imported fixture
    admin = stage['admin']
    configure(stage)
    no_failed_runs(admin)
    code, out = call(stage, 'candidate-template')
    assert out[0]['blocked'] and 'no semantic_content draft' in json.dumps(out[0])  # the default mode needs failed Runs
    code, out = schema_only(stage)
    assert code == 0 and out[0]['passed'] is True, json.dumps(out, ensure_ascii=False)
    path, t = template(stage)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert (t['mode'], t['property'], t['decision']) == ('schema_only', {'type_name': 'Consumer', 'property_name': 'delivery_windows'}, PROPERTY['decision'])
    body = t['body']
    assert body['category'] == 'owner_directed_schema' and body['candidate']['dimension'] == 'ontology_schema'
    assert body['candidate']['allowed_change_scope'] == ['eios:object_type:Consumer', 'eios:property:Consumer.delivery_windows']
    assert set(body['candidate']['design_case_ids']).isdisjoint(body['candidate']['validation_case_ids'])
    (did, attribution, status), = diagnoses(admin)
    assert did == body['diagnosis_id'] and attribution['schema_only'] == 1 and status == 'open'
    # The candidate proposes (as r8 does) and reads back: propose's wire (0235:330 line) and the writer's candidate read (0235:344).
    proposed = propose(stage, body)
    assert proposed['proposed'] is True and proposed['candidate']['dimension'] == 'ontology_schema'
    conn, digest = writer(stage)
    with conn:
        read = conn.execute('select authz.nexloop_evo_candidate_read(%s,%s)', (digest, body['candidate']['candidate_id'])).fetchone()[0]
    assert read['category'] == 'owner_directed_schema' and read['candidate']['candidate_id'] == body['candidate']['candidate_id']
    summary = admin.execute("select runtime.nexloop_evo_review_summary(%s,'real')", (TENANT_A,)).fetchone()[0]  # the workbench review queue
    assert isinstance(summary['candidates'], list) and 'stops' in summary
    # 0419: the owner-directed category is ontology_schema only, and the category list stays closed.
    for change in ("dimension='semantic_content'", "category='bogus'"):
        with pytest.raises(psycopg.errors.CheckViolation), admin.transaction():
            admin.execute(f"update runtime.nexloop_evo_candidates set {change} where candidate_id=%s", (body['candidate']['candidate_id'],))


def test_only_a_listed_property_and_at_least_two_units(stage):  # noqa: F811
    admin = stage['admin']
    configure(stage, ())
    code, out = schema_only(stage)
    assert code == 2 and 'owner_directed_properties' in out[0]['refused']
    configure(stage)
    code, out = call(stage, 'candidate-template', '--mode', 'schema_only', '--property', 'Consumer.shoe_size')
    assert code == 2 and 'owner_directed_properties' in out[0]['refused']
    code, out = call(stage, 'at023', '--property', 'Consumer.delivery_windows')
    assert code == 2 and 'candidate-template' in out[0]['refused']
    # One synthetic Consumer left: one cluster unit, no split - no template, no diagnosis.
    admin.execute('delete from control.nexloop_consumer_personas where tenant_id=%s and consumer_id<>%s', (TENANT_A, stage['consumers'][0]))
    before = everything(admin)
    code, out = schema_only(stage)
    assert out[0]['blocked'] and 'at least 2' in json.dumps(out[0]) and not os.path.exists(template_path(stage))
    assert everything(admin) == before


def template_path(stage):
    return json.loads(Path(stage['config']).read_text())['inputs']['candidate_template']


@pytest.mark.parametrize('check', ['at023', 'at048', 'r1', 'r5', 'all'])
def test_every_acceptance_check_refuses_a_schema_only_template(stage, check):  # noqa: F811
    admin = stage['admin']
    configure(stage)
    assert schema_only(stage)[0] == 0
    before = everything(admin)
    code, out = call(stage, check)
    assert code == 2 and 'schema_only template' in out[0]['refused'] and 'acceptance' in out[0]['refused']
    assert everything(admin) == before


def test_a_regenerated_template_opens_a_new_diagnosis_after_the_deadline(stage):  # noqa: F811
    admin = stage['admin']
    configure(stage)
    assert schema_only(stage)[0] == 0
    _, first = template(stage)
    with admin.transaction():  # the 30-minute diagnosis deadline has passed
        admin.execute('set local session_replication_role=replica')
        admin.execute("update runtime.nexloop_evo_diagnoses set opened_at=opened_at-interval '1 hour',deadline=deadline-interval '1 hour' where diagnosis_id=%s",
                      (first['body']['diagnosis_id'],))
    late = propose(stage, first['body'])
    assert late == {'proposed': False, 'reason': 'budget_exhausted', 'tier': 'diagnosis_time'}
    assert schema_only(stage)[0] == 0  # regenerate: the same samples, a new diagnosis (no cost)
    _, second = template(stage)
    assert second['body']['diagnosis_id'] != first['body']['diagnosis_id']
    assert propose(stage, second['body'])['proposed'] is True
    assert [s for _, _, s in diagnoses(admin)] == ['incomplete', 'open']  # the old one lapsed on its own
