"""NX-068 Q8 (docs/planning-drafts/nx068-publish-gate-and-smoke-20261111.md, L6 N1-N4, owner 2026-11-11): the pre-publication gate
authz.nexloop_schema_publish_gate on the 0446 fixture (the effect chain of a relay reply, Consumer v1). Each case builds the
publication F3 would send (schema_publication.successors over the Actions bound to Consumer:1) and asks the gate; the real F3
transaction (refusal writes nothing, the permit stays unused) is tests/test_nx068_publish_path_pg.py. Synthetic data."""
import json
import re
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from nexloop_eios.schema_publication import GATE_REMEDIES, gate_remedy, successors
from nx068_fixture import mark
from test_context_artifacts import context_message  # noqa: F401
from local_message_assembly_fixture import assembled_message, business_plan, configured  # noqa: F401
from test_effect_scope_successor_proof_pg import ADDED, EFFECT, appended, changed_first, extra_type, publish_v2

MIGRATION = next((Path(__file__).resolve().parents[1] / 'packages/eios-core/src/eios/migrations').glob('*_nx068_publish_gate_smoke.sql'))
BIND = 'nexloop.plan.bind_effect_context'


def consumer(admin, tenant, version=1):
    return admin.execute("select definition from ontology.object_type_versions where tenant_id=%s and type_name='Consumer' and version=%s",
                         (tenant, version)).fetchone()[0]


def publication(admin, tenant, *, schema=None, edit=None, drop=None):
    """What F3 sends for the next Consumer version: the Schema and a successor of each Action bound to Consumer:1."""
    old = consumer(admin, tenant)
    new = schema(old) if schema else {**old, 'version': 2, 'properties': list(old['properties']) + [ADDED]}
    bound = [{'definition': d, 'capability': c} for d, c in admin.execute("""select definition,capability from control.nexloop_action_definitions
        where tenant_id=%s and world='real' and active and definition->'object_types' @> %s::jsonb""",
        (tenant, json.dumps([{'stable_name': 'Consumer', 'version': 1}]))).fetchall()]
    rows = successors({'actions': bound}, new)
    for row in rows:
        if edit and row['definition']['stable_name'] in edit:
            edit[row['definition']['stable_name']](row)
    return {'schema': new, 'actions': [r for r in rows if r['definition']['stable_name'] != drop]}


def gate(admin, tenant, pub):
    return admin.execute("select authz.nexloop_schema_publish_gate(%s,'Consumer',%s)", (tenant, Jsonb(pub))).fetchone()[0]


def codes(result, level='refuse'):
    return sorted(i['code'] for i in result['items'] if i['level'] == level)


def holders(admin, tenant, name):
    return sorted(k for (k,) in admin.execute("""select distinct entity_key[1] from authz.nexloop_authority_facts where tenant_id=%s and fact_kind='grants'
        and payload->>'subject_kind'='service' and regexp_replace(entity_key[2],':[0-9]+$','')=%s""", (tenant, 'eios:action:' + name)).fetchall())


def followed(admin, tenant):
    for name in (BIND, 'Consumer.create'):
        mark(admin, tenant, name)


def test_the_p0_publication_is_refused_until_the_registrar_holders_follow(context_message, admin):  # noqa: F811
    """The 11-09 shape: Consumer v2 (an added optional property) while the registrar's holders carry no follow marker."""
    tenant = context_message['original']['tenant']
    planners = holders(admin, tenant, BIND)
    assert planners
    refused = gate(admin, tenant, publication(admin, tenant))
    assert refused['verdict'] == 'refuse' and refused['from_version'] == 1 and refused['to_version'] == 2
    assert {f'registrar_holder_without_follow:{p}' for p in planners} <= set(codes(refused))
    followed(admin, tenant)
    passed = gate(admin, tenant, publication(admin, tenant))
    assert codes(passed) == [] and passed['verdict'] in ('pass', 'warn'), passed
    assert not any(c.startswith('effect_successor_unprovable') for c in codes(refused))


@pytest.mark.parametrize('case,expected', [
    ('vocabulary_or_hide', 'effect_successor_unprovable:type_not_cumulative'),
    ('required_property', 'effect_successor_unprovable:type_not_cumulative'),
    ('effect_not_equivalent', 'effect_successor_unprovable:successor_not_equivalent'),
    ('effect_capability', 'effect_successor_unprovable:successor_capability_differs'),
    ('effect_missing', 'effect_successor_unprovable:successor_missing'),
    ('registrar_scope_widened', 'registrar_successor_not_rebinding'),
    ('registrar_missing', 'registrar_successor_missing'),
])
def test_each_condition_refuses_on_its_own(context_message, admin, case, expected):  # noqa: F811
    tenant = context_message['original']['tenant']
    followed(admin, tenant)
    capability = lambda row: row.update(capability={**row['capability'], 'idempotent': not row['capability']['idempotent']})  # noqa: E731
    widen = lambda row: row['definition']['governance']['change_scope'].update(target_systems=row['definition']['governance']['change_scope']['target_systems'] + ['postgres'])  # noqa: E731
    pub = {
        'vocabulary_or_hide': lambda: publication(admin, tenant, schema=lambda old: {**changed_first(old), 'version': 2}),
        'required_property': lambda: publication(admin, tenant, schema=lambda old: {**appended(old, required=True, nullable=False), 'version': 2}),
        'effect_not_equivalent': lambda: publication(admin, tenant, edit={EFFECT: widen}),
        'effect_capability': lambda: publication(admin, tenant, edit={EFFECT: capability}),
        'effect_missing': lambda: publication(admin, tenant, drop=EFFECT),
        'registrar_scope_widened': lambda: publication(admin, tenant, edit={BIND: extra_type}),
        'registrar_missing': lambda: publication(admin, tenant, drop=BIND),
    }[case]()
    result = gate(admin, tenant, pub)
    assert result['verdict'] == 'refuse'
    if case in ('vocabulary_or_hide', 'required_property'):
        # A changed type also changes the successors F3 rebuilds beyond the version (their type references reorder by digest): register
        # would refuse to follow them (service_grants._rebinding_reason, resource_scope_changed), and the gate says so too.
        assert expected in codes(result) and set(codes(result)) <= {expected, 'registrar_successor_not_rebinding'}, result
    else:
        assert codes(result) == [expected], result


def test_the_precheck_previews_the_successors_and_judges_alike(context_message, admin):  # noqa: F811
    """E1 sends the Schema only (the review read payload is bounded): the successors are previewed in SQL as F3 builds them, so the
    precheck and the publish-time gate agree on the real publication."""
    tenant = context_message['original']['tenant']
    pub = publication(admin, tenant)
    assert codes(gate(admin, tenant, {'schema': pub['schema']})) == codes(gate(admin, tenant, pub)) != []
    followed(admin, tenant)
    assert codes(gate(admin, tenant, {'schema': pub['schema']})) == codes(gate(admin, tenant, pub)) == []
    # A changed type: the preview keeps the reference order, F3's rebuilt successors may not; the precheck shows the A' refusal and the
    # publish-time gate (on the real successors) refuses at least as much.
    hide = publication(admin, tenant, schema=lambda old: {**changed_first(old), 'version': 2})
    precheck, real = codes(gate(admin, tenant, {'schema': hide['schema']})), codes(gate(admin, tenant, hide))
    assert precheck == ['effect_successor_unprovable:type_not_cumulative'] and set(precheck) <= set(real)


def test_nothing_bound_to_the_current_version_is_a_warning_never_a_silent_pass(context_message, admin):  # noqa: F811
    tenant = context_message['original']['tenant']
    followed(admin, tenant)
    old = consumer(admin, tenant)
    # Consumer v2 exists already and nothing is bound to it: publishing v3 has an empty affected set while v1 is bound.
    admin.execute("insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,'Consumer',2,%s)",
                  (tenant, Jsonb({**old, 'version': 2, 'properties': list(old['properties']) + [ADDED]})))
    v2 = consumer(admin, tenant, 2)
    result = gate(admin, tenant, {'schema': {**v2, 'version': 3, 'properties': list(v2['properties']) + [{**ADDED, 'property_name': 'added_v3'}]}, 'actions': []})
    assert codes(result, 'warn') == ['scope_lineage_gap'] and result['verdict'] in ('warn', 'refuse')


def test_the_gate_reads_only_and_writes_no_proof(context_message, admin):  # noqa: F811
    tenant = context_message['original']['tenant']
    count = lambda: admin.execute("""select (select count(*) from control.nexloop_effect_scope_proofs),(select count(*) from control.nexloop_action_definitions),
        (select count(*) from ontology.object_type_versions),(select count(*) from authz.nexloop_authority_facts)""").fetchone()  # noqa: E731
    before = count()
    gate(admin, tenant, publication(admin, tenant))
    gate(admin, tenant, {'schema': publication(admin, tenant)['schema']})
    assert count() == before


def gate_codes():
    """Every code the gate can return (the literal codes and the prefixes built with a suffix)."""
    body = MIGRATION.read_text().split('create function authz.nexloop_schema_publish_gate(', 1)[1].split('$$;', 1)[0]
    found = set(re.findall(r"'code','([a-z_]+)[:']", body))
    found |= {kind + '_' + rest for rest in re.findall(r"'code',kind\|\|'_([a-z_]+)", body) for kind in ('registrar', 'create')}
    return found


def test_every_gate_code_has_a_remedy():
    found = gate_codes()
    assert {'effect_successor_unprovable', 'registrar_holder_without_follow', 'create_holder_without_follow', 'registrar_successor_missing',
            'create_successor_scope_other_type', 'scope_lineage_gap', 'successor_coexists'} <= found
    assert found <= set(GATE_REMEDIES), found - set(GATE_REMEDIES)
    assert all(gate_remedy(c + ':x') for c in found)


def body(admin, signature):
    return admin.execute('select prosrc from pg_proc where oid=%s::regprocedure', (signature,)).fetchone()[0]


def test_one_judgement_serves_the_proof_the_gate_and_the_smoke(admin):
    """N1/N4: the A' conditions live in authz.nexloop_effect_successor_admissible only; the proof (which alone records), the gate's G1
    and the smoke's S3 call it, and none of them compares the type definitions or the unversioned Action definitions itself (the
    gate's registrar/create re-binding check is Q4, not A')."""
    admissible = 'authz.nexloop_effect_successor_admissible('
    proof = body(admin, 'authz.nexloop_effect_scope_successor_proof(uuid,text,text,control.nexloop_action_definitions,integer)')
    smoke = body(admin, 'runtime.nexloop_schema_publish_smoke_checks(text,text,text,boolean)')
    gate_body = body(admin, 'authz.nexloop_schema_publish_gate(text,text,jsonb)')
    for text in (proof, smoke, gate_body):
        assert admissible in text and "->'properties'" not in text
    for text in (proof, smoke):
        assert 'nexloop_effect_definition_unversioned' not in text
    assert 'insert into control.nexloop_effect_scope_proofs' in proof
    pure = admin.execute("""select provolatile,prosecdef,prosrc from pg_proc
        where oid='authz.nexloop_effect_successor_admissible(text,text,jsonb,jsonb,jsonb,jsonb,integer,jsonb)'::regprocedure""").fetchone()
    assert pure[0] == 'i' and pure[1] is False and not re.search(r'\b(insert|update|delete)\b|\b(control|ontology|runtime)\.', pure[2])


def test_the_gate_has_its_a_prime_branch_while_effect_actions_are_unversioned(admin):
    """7c: while the effect Action allow-list holds version 1 only (A' in force), G1 refuses through A'. Option B M4 removes A' and
    this branch together (ADR-046 A'-6: tests/test_effect_scope_successor_proof_pg.py turns red at B)."""
    assert admin.execute('select array_agg(distinct version) from control.nexloop_effect_actions').fetchone()[0] == [1]
    assert "'effect_successor_unprovable:'" in body(admin, 'authz.nexloop_schema_publish_gate(text,text,jsonb)')


WRAPPED = {
    'ontology.nexloop_schema_publication_gates(text,ontology.nexloop_schema_proposals,text,jsonb,text)':
        'ontology.nexloop_schema_publication_gates_before_publish_gate(text,ontology.nexloop_schema_proposals,text,jsonb,text)',
    'runtime.nexloop_schema_watch_start(text,text,uuid,text)': 'runtime.nexloop_schema_watch_start_before_publish_smoke(text,text,uuid,text)',
    'runtime.nexloop_observability_maintain()': 'runtime.nexloop_observability_maintain_before_publish_smoke()',
    # alert_evaluate_split stacks one more snapshot wrapper on top of nx068's (now runtime.nexloop_metrics_snapshot_before_alert_split).
    'runtime.nexloop_metrics_snapshot_before_alert_split(text,text)': 'runtime.nexloop_metrics_snapshot_before_publish_smoke(text,text)',
    'runtime.nexloop_metrics_snapshot(text,text)': 'runtime.nexloop_metrics_snapshot_before_alert_split(text,text)',
}


def grantees(admin, signature):
    return admin.execute("""select coalesce(array_agg(r.rolname order by r.rolname),'{}') from pg_proc p cross join aclexplode(p.proacl) a
        join pg_roles r on r.oid=a.grantee where p.oid=%s::regprocedure and a.privilege_type='EXECUTE' and a.grantee<>p.proowner""", (signature,)).fetchone()[0]


def test_each_wrapper_calls_the_renamed_function_which_no_role_may_call(admin):
    for wrapper, renamed in WRAPPED.items():
        assert grantees(admin, renamed) == [], renamed
        assert renamed.split('(')[0] + '(' in body(admin, wrapper), wrapper
    assert grantees(admin, 'authz.nexloop_read_schema_publish_readiness(text,text,text,text,text)') == ['nexloop_api']
    for internal in ('authz.nexloop_schema_publish_gate(text,text,jsonb)', 'runtime.nexloop_run_schema_publish_smokes()',
                     'runtime.nexloop_schema_publish_smoke_checks(text,text,text,boolean)',
                     'authz.nexloop_effect_successor_admissible(text,text,jsonb,jsonb,jsonb,jsonb,integer,jsonb)'):
        assert grantees(admin, internal) == [], internal


@pytest.mark.parametrize('kwargs,expected', [
    ({}, []),
    ({'publisher': 'service:synthetic'}, [f'effect_unprovable:{EFFECT}:publisher_not_human']),
    ({'v2': lambda old: appended(old, required=True, nullable=False)}, [f'effect_unprovable:{EFFECT}:type_not_cumulative']),
])
def test_the_smoke_judges_the_published_effect_chain_with_the_same_judgement(context_message, admin, kwargs, expected):  # noqa: F811
    """S3 (N4): after the publication (0446's setup of what F3 leaves), the smoke's checks name an effect Action that A' could not
    prove, by the same judgement as the proof, and write no proof."""
    tenant = context_message['original']['tenant']
    publish_v2(context_message, admin, **kwargs)
    checks = admin.execute("select runtime.nexloop_schema_publish_smoke_checks(%s,'real','Consumer',false)", (tenant,)).fetchone()[0]
    assert [c for c in checks if c.startswith('effect_unprovable:')] == expected, checks
    assert admin.execute('select count(*) from control.nexloop_effect_scope_proofs').fetchone()[0] == 0
