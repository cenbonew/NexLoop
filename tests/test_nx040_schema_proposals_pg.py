"""NX-040 F2 (design §3, §9; D21-D27, owner 2026-10-20): Schema proposals and the ontology_schema window for proposal input only.
An ontology_schema candidate is accepted narrowly (Object Type / Property scope, §4.1 whitelist change kinds); a proposal frozen from
it fixes the parent version and digest, the candidate's snapshot and the change, and never changes; it moves once, frozen ->
published (F3's internal function, with a permit) or frozen -> withdrawn. Evaluation of the dimension stays closed (dimension_closed).
Another tenant reads nothing. Nothing here writes a Schema version, a definition, an object or an authorization fact.

Real PostgreSQL with the E4 / NX-039 V1 fixtures (login roles nexloop_evo_writer and nexloop_evo_evaluator); admin seeds one synthetic
Object Type version and probes."""
import json
import uuid

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from support.tenants import TENANT_A
from test_evolution_e4_pg import body_of, evo, opened, propose_raw, table_digest  # noqa: F401
from test_evolution_e5_pg import proposed  # noqa: F401
from test_evolution_v1_pg import evaluation, refused, suspended  # noqa: F401

SCOPE = ['eios:object_type:Product', 'eios:property:Product.material']
DEFINITION = {'type_name': 'Product', 'version': 3, 'properties': [{'property_name': 'title', 'value_type': 'string', 'required': True}]}
CHANGE = {'property_name': 'material', 'value_type': 'string'}


def schema_candidate(f, **over):
    cid = str(uuid.uuid4())
    body = body_of(f, candidate={'candidate_id': cid, 'dimension': 'ontology_schema', 'allowed_change_scope': SCOPE, **over.get('candidate', {})},
                   patch={'change_kind': 'add_optional_property', 'target_refs': ['eios:object_type:Product'], **over.get('patch', {})})
    return propose_raw(f, body), cid


def seed_type(admin, tenant=TENANT_A):
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute('insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,%s,3,%s) on conflict do nothing',
                      (tenant, 'Product', Jsonb(DEFINITION)))


def call(f, sql, *args, digest=None):
    with psycopg.connect(make_conninfo(f['pg'], user='nexloop_evo_writer'), autocommit=True) as db:
        return db.execute(sql, (digest or f['digest'], *args)).fetchone()[0]


def freeze(f, candidate, **over):
    body = {'proposal_id': str(uuid.uuid4()), 'candidate_id': candidate, 'operation': 'add_optional_property', 'type_name': 'Product', 'change': CHANGE,
            'claim_ids': ['c' * 64], 'candidate_definition_ids': [], 'compatibility_report_digest': 'a' * 64, 'permission_proof_digest': 'b' * 64, **over}
    return body, call(f, 'select authz.nexloop_schema_proposal_freeze(%s,%s)', json.dumps(body))


def test_the_window_opens_ontology_schema_for_proposal_input_only(evo):
    f = evo
    opened(f)
    out, cid = schema_candidate(f)
    assert out['proposed'] is True and out['candidate']['dimension'] == 'ontology_schema'
    assert f['admin'].execute('select dimension from runtime.nexloop_evo_candidates where candidate_id=%s', (cid,)).fetchone() == ('ontology_schema',)
    # Narrow: Object Type / Property refs only, whitelist change kinds only; semantic_tool stays closed.
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='change scope not allowed'):
        schema_candidate(f, candidate={'allowed_change_scope': SCOPE + ['nexloop:vocabulary:color/red']})
    for kind in ('alias', 'add_relation_type', 'remove_property'):
        with pytest.raises(psycopg.Error, match='patch invalid'):
            schema_candidate(f, patch={'change_kind': kind})
    with pytest.raises(psycopg.Error, match='dimension semantic_tool not open'):
        propose_raw(f, body_of(f, candidate={'candidate_id': str(uuid.uuid4()), 'dimension': 'semantic_tool'}))
    # A semantic_content candidate cannot use a Schema change kind.
    with pytest.raises(psycopg.Error, match='patch invalid'):
        propose_raw(f, body_of(f, candidate={'candidate_id': str(uuid.uuid4())}, patch={'change_kind': 'add_optional_property'}))


def test_evaluation_of_ontology_schema_is_open_and_its_row_alone_closes_it(evaluation):
    """NX-040 F4 opened the dimension with a migration of its own (D21); removing that one row (as a reversal would) closes it again."""
    f = evaluation
    assert {r[0] for r in f['admin'].execute('select dimension from control.nexloop_evo_open_dimensions')} == {'semantic_content', 'ontology_schema'}
    suspended(f['admin'], "update runtime.nexloop_evo_candidates set dimension='ontology_schema' where candidate_id=%s", (f['candidate'],))
    suspended(f['admin'], "delete from control.nexloop_evo_open_dimensions where dimension='ontology_schema'")
    refused(f, 'dimension_closed')


def test_a_proposal_is_frozen_at_creation_and_never_changes(evo):
    f = evo
    admin = f['admin']
    opened(f)
    seed_type(admin)
    _, cid = schema_candidate(f)
    before = table_digest(admin)
    body, out = freeze(f, cid)
    p = out['proposal']
    assert out['frozen'] is True and out['replayed'] is False and p['status'] == 'frozen' and p['permit_ref'] is None
    assert (p['parent_schema_version'], p['target_ref'], p['operation']) == (3, 'eios:property:Product.material', 'add_optional_property')
    expected_parent = admin.execute("select encode(sha256(convert_to(runtime.nexloop_canonical_json(definition),'UTF8')),'hex') from ontology.object_type_versions "
                                    "where tenant_id=%s and type_name='Product' and version=3", (TENANT_A,)).fetchone()[0]
    snapshot = admin.execute('select input_snapshot_digest from runtime.nexloop_evo_candidates where candidate_id=%s', (cid,)).fetchone()[0]
    assert p['parent_schema_digest'] == expected_parent and p['source']['input_snapshot_digest'] == snapshot and len(p['change_digest']) == 64
    # The proposal is the EIOS review's input only: no Schema version, definition, object, Claim, budget or authority changed.
    assert table_digest(admin) == before
    # A replay returns it; the same id with anything changed is refused; a direct update or delete is refused too.
    assert freeze(f, cid, proposal_id=body['proposal_id'])[1]['replayed'] is True
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='frozen'):
        freeze(f, cid, proposal_id=body['proposal_id'], change={'property_name': 'colour', 'value_type': 'string'})
    for statement in ("update ontology.nexloop_schema_proposals set change_digest=repeat('0',64)", "update ontology.nexloop_schema_proposals set parent_schema_version=4",
                      'delete from ontology.nexloop_schema_proposals'):
        with pytest.raises(psycopg.errors.InsufficientPrivilege), admin.transaction():
            admin.execute('set local role nexloop_owner'); admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT_A,)); admin.execute(statement)
    # Only §4.1 changes, only inside the candidate's scope, only from an ontology_schema candidate.
    with pytest.raises(psycopg.Error, match='proposal invalid'):
        freeze(f, cid, operation='add_relation_type')
    with pytest.raises(psycopg.Error, match='proposal invalid'):
        freeze(f, cid, change={'property_name': 'material', 'value_type': 'string', 'required': True})
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='outside the candidate scope'):
        freeze(f, cid, type_name='Order', change=CHANGE)
    semantic = f['store'].propose(opened(f)['diagnosis_id'], f['drafts'][0])['candidate']['candidate_id']
    with pytest.raises(psycopg.Error, match='needs an ontology_schema candidate'):
        freeze(f, semantic)
    with pytest.raises(psycopg.Error, match='object type exists'):
        freeze(f, cid, operation='add_object_type', change={'properties': [{'property_name': 'title', 'value_type': 'string'}]})
    # §4.1 as F1 judges it: a new type has no primary key; a new property is of a scalar type.
    with pytest.raises(psycopg.Error, match='proposal invalid'):
        freeze(f, cid, operation='add_object_type', type_name='Material',
               change={'primary_key': ['code'], 'properties': [{'property_name': 'code', 'value_type': 'string'}]})
    with pytest.raises(psycopg.Error, match='proposal invalid'):
        freeze(f, cid, change={'property_name': 'material', 'value_type': 'json'})


def test_three_states_move_once_and_events_are_append_only(evo):
    f = evo
    admin = f['admin']
    opened(f)
    seed_type(admin)
    _, cid = schema_candidate(f)
    a = freeze(f, cid)[1]['proposal']['proposal_id']
    b = freeze(f, cid)[1]['proposal']['proposal_id']
    # frozen -> withdrawn (with a reason code), once.
    assert call(f, 'select authz.nexloop_schema_proposal_withdraw(%s,%s,%s)', a, 'parent_moved') == {'status': 'withdrawn', 'replayed': False}
    assert call(f, 'select authz.nexloop_schema_proposal_withdraw(%s,%s,%s)', a, 'parent_moved')['replayed'] is True
    with pytest.raises(psycopg.Error, match='reason invalid'):
        call(f, 'select authz.nexloop_schema_proposal_withdraw(%s,%s,%s)', b, 'because')
    # frozen -> published only through F3's internal function (no role holds it), with its permit; then nothing moves it again.
    with admin.transaction():
        assert admin.execute("select runtime.nexloop_schema_proposal_mark_published(%s,%s,'permit:synthetic-1','synthetic-review')", (TENANT_A, b)).fetchone()[0]['status'] == 'published'
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='cannot be withdrawn'):
        call(f, 'select authz.nexloop_schema_proposal_withdraw(%s,%s,%s)', b, 'superseded')
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='cannot be published'), admin.transaction():
        admin.execute("select runtime.nexloop_schema_proposal_mark_published(%s,%s,'permit:synthetic-2','synthetic-review')", (TENANT_A, a))
    assert admin.execute("select has_function_privilege('nexloop_evo_writer','runtime.nexloop_schema_proposal_mark_published(text,uuid,text,text)','execute')").fetchone() == (False,)
    read = call(f, 'select authz.nexloop_schema_proposal_read(%s,%s)', b)
    assert read['proposal']['status'] == 'published' and read['proposal']['permit_ref'] == 'permit:synthetic-1'
    events = admin.execute('select proposal_id::text,from_status,to_status,reason from ontology.nexloop_schema_proposal_events order by event_id').fetchall()
    assert events == [(a, None, 'frozen', None), (b, None, 'frozen', None), (a, 'frozen', 'withdrawn', 'parent_moved'), (b, 'frozen', 'published', None)]
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='append-only'), admin.transaction():
        admin.execute('set local role nexloop_owner'); admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT_A,))
        admin.execute('delete from ontology.nexloop_schema_proposal_events')


def test_another_tenant_reads_nothing(evo):
    f = evo
    opened(f)
    seed_type(f['admin'])
    _, cid = schema_candidate(f)
    pid = freeze(f, cid)[1]['proposal']['proposal_id']
    assert call(f, 'select authz.nexloop_schema_proposal_read(%s,%s)', pid, digest=f['other_digest']) is None
    with pytest.raises(psycopg.Error, match='proposal unknown'):
        call(f, 'select authz.nexloop_schema_proposal_withdraw(%s,%s,%s)', pid, 'superseded', digest=f['other_digest'])
    for role in ('nexloop_api', 'nexloop_domain_worker', 'nexloop_evo_writer'):
        assert f['admin'].execute("select has_table_privilege(%s,'ontology.nexloop_schema_proposals','select')", (role,)).fetchone() == (False,)


# ---- end to end with F1 (nexloop_eios.evolution.schema_compat, NX-040 F1): what F2 freezes, F1 allows ----
NEW_TYPE = {'properties': [{'property_name': 'code', 'value_type': 'string'}, {'property_name': 'weight', 'value_type': 'number'}]}


def f1_change(proposal_row):
    """The frozen change as F1's input, built the way F3's publication builds it (schema_publication.build): a new property optional and
    nullable; a new type editable only through Actions, its properties optional, its primary key the frozen one (none)."""
    op, name, change = proposal_row
    if op == 'add_optional_property':
        return {'op': op, 'type_name': name, 'property': {'property_name': change['property_name'], 'value_type': change['value_type'], 'required': False, 'nullable': True}}
    return {'op': op, 'type_name': name, 'definition': {'type_name': name, 'version': 1, 'only_edit_via_actions': True, 'primary_key': list(change.get('primary_key') or []),
            'properties': [{'property_name': p['property_name'], 'value_type': p['value_type'], 'required': False, 'nullable': True} for p in change['properties']]}}


@pytest.mark.parametrize('operation', ['add_optional_property', 'add_object_type'])
def test_what_f2_freezes_f1_allows(evo, operation):
    compat = pytest.importorskip('nexloop_eios.evolution.schema_compat')
    f = evo
    admin = f['admin']
    opened(f)
    seed_type(admin)
    if operation == 'add_object_type':
        _, cid = schema_candidate(f, candidate={'allowed_change_scope': ['eios:object_type:Material']},
                                 patch={'change_kind': 'add_object_type', 'target_refs': ['eios:object_type:Material']})
        out = freeze(f, cid, operation=operation, type_name='Material', change=NEW_TYPE)[1]
    else:
        _, cid = schema_candidate(f)
        out = freeze(f, cid)[1]
    assert out['frozen'] is True
    row = admin.execute('select operation,type_name,change from ontology.nexloop_schema_proposals where proposal_id=%s', (out['proposal']['proposal_id'],)).fetchone()
    parent = {'types': {'Product': admin.execute("select definition from ontology.object_type_versions where type_name='Product'").fetchone()[0]}}
    verdict = compat.judge(f1_change(row), parent)
    assert verdict['allowed'] is True and verdict['change_class'] == 'minor' and verdict['codes'] == [], verdict
