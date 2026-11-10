"""ADR addendum A' (owner 2026-11-10; stage 11-09/10: after Consumer v2 every relay reply to an existing customer failed): the relay's
full chain - issue, effect_bind, context, runtime accept, effect intent - after a Consumer publication, on actual PostgreSQL.

The publication is set up as F3 leaves it: Consumer v2 (one optional nullable property appended) and the successor :2 of every
Consumer-bound Action inserted as F3 inserts them, the planner's authority facts following to the registrar's :2 (as register's
follow_latest_version derives them), the Consumer objects moved to v2, and the human publication record naming the successors. The
effect Action stays :1 (pinned by the allow-list, the Host and the contracts); its Consumer scope is proven by its successor :2.
Each refusal case breaks exactly one condition of authz.nexloop_effect_scope_successor_proof. Synthetic data."""
import copy
import json
import uuid

import pytest
from eios.ontology.definitions import ActionDefinition

from psycopg.types.json import Jsonb

from nexloop_eios.message_relay import MessageRelay, MessageRelayUnavailable
from nexloop_eios.schema_publication import successors
from system_catalog_fixture import insert_active_actions
from goal_fixture import MODELS
from test_context_artifacts import active_worker, context_message  # noqa: F401
from local_message_assembly_fixture import assembled_message, business_plan, configured  # noqa: F401

BIND1 = 'eios:action:nexloop.plan.bind_effect_context:1'
BIND2 = 'eios:action:nexloop.plan.bind_effect_context:2'
EFFECT = 'nexloop.service.request'
ADDED = {'property_name': 'delivery_windows', 'value_type': 'string', 'required': False, 'nullable': True, 'default_value': None,
         'description': '', 'display_name': '', 'render_hint': 'plain', 'visibility': 'normal', 'type_descriptor': None}



def publish_v2(f, admin, *, v2=None, **kwargs):
    """Consumer v2 published as F3 leaves it (F3 writes these rows itself; the trusted configuration accepts no property increment);
    each keyword breaks one condition for the refusal cases."""
    return publish_version(f, admin, 1, 2, schema=v2, **kwargs)


def publish_version(f, admin, before, after, *, schema=None, follow=True, edit_successor=None, objects=True, publisher='human:synthetic-reviewer',
                    record=True, effect=None, record_as=None):
    """Consumer `after` published over `before` as F3 leaves it. effect places the effect Action's successor for the refusal cases:
    'skip' (not published), 'inactive', ('world', name) or ('tenant', id) (published elsewhere)."""
    tenant = f['original']['tenant']
    published = {}
    old = admin.execute("select definition from ontology.object_type_versions where tenant_id=%s and type_name='Consumer' and version=%s", (tenant, before)).fetchone()[0]
    new = schema(old) if schema else {**old, 'version': after, 'properties': list(old['properties']) + [ADDED if after == 2 else {**ADDED, 'property_name': f'added_v{after}'}]}
    admin.execute("insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,'Consumer',%s,%s)", (tenant, after, Jsonb(new)))
    bound = [{'definition': d, 'capability': c} for d, c in admin.execute("""select definition,capability from control.nexloop_action_definitions
        where tenant_id=%s and world='real' and active and definition->'object_types' @> %s::jsonb""",
        (tenant, json.dumps([{'stable_name': 'Consumer', 'version': before}]))).fetchall()]
    rows = successors({'actions': bound}, new)
    if edit_successor:
        for row in rows:
            if row['definition']['stable_name'] == EFFECT:
                edit_successor(row)
                row['definition'].pop('contract_digest', None)
                row['definition'] = ActionDefinition.model_validate_json(json.dumps(row['definition'])).model_dump(mode='json')
    effect_rows = [r for r in rows if r['definition']['stable_name'] == EFFECT]
    insert_active_actions(admin, tenant, [r for r in rows if r['definition']['stable_name'] != EFFECT])
    if effect in (None, 'inactive'):
        insert_active_actions(admin, tenant, effect_rows)
        if effect == 'inactive':
            admin.execute("update control.nexloop_action_definitions set active=false where tenant_id=%s and resource_id=%s",
                          (tenant, f'eios:action:{EFFECT}:{after}'))
    elif effect != 'skip' and effect[0] == 'world':
        insert_active_actions(admin, tenant, effect_rows, world=effect[1])
    elif effect != 'skip' and effect[0] == 'tenant':
        admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict(tenant_id) do nothing", (effect[1],))
        insert_active_actions(admin, effect[1], json.loads(json.dumps(effect_rows).replace(tenant, effect[1])))
    published['successors'] = [f"eios:action:{r['definition']['stable_name']}:{r['definition']['version']}" for r in rows]
    if follow:  # register's follow_latest_version: the planner's registrar authority facts follow (digests recomputed)
        follow_facts(admin, tenant, BIND1[:-1] + str(before), BIND1[:-1] + str(after))
    reauthenticate(f)
    if objects:
        admin.execute("update ontology.objects set schema_version=%s where tenant_id=%s and world='real' and type_name='Consumer' and schema_version=%s",
                      (after, tenant, before))
    if record:
        with admin.transaction():
            admin.execute('set local session_replication_role=replica')  # the proposal row of the F3 transaction is not part of this setup
            admin.execute("""insert into ontology.nexloop_schema_publications(tenant_id,world,publication_id,proposal_id,kind,type_name,version_before,version_after,
                successor_actions,permission_digest_before,permission_digest_after,compatibility_report_digest,published_by,rationale)
                values(%s,'real',%s,%s,'publish',%s,%s,%s,%s,%s,%s,%s,%s,'synthetic publication')""",
                (tenant, published.setdefault('publication', str(uuid.uuid4())), str(uuid.uuid4()), *(record_as or ('Consumer', before, after)), published['successors'],
                 'a' * 64, 'a' * 64, 'b' * 64, publisher))
    return published


def beside(value, old, new):
    """Each list entry naming `old` gets a copy naming `new` next to it (its own digest dropped, recomputed on validation)."""
    if isinstance(value, dict):
        return {k: beside(v, old, new) for k, v in value.items()}
    if isinstance(value, list):
        out = []
        for item in value:
            out.append(beside(item, old, new))
            if isinstance(item, dict) and old in item.values():
                out.append({k: (new if v == old else v) for k, v in item.items() if k != 'restriction_digest'})
        return out
    return value


def follow_facts(admin, tenant, old, new):
    """What register's follow_latest_version derives, at the fact level: every authority fact keyed by `old` copied for `new`, and
    the caller application's resource restrictions naming `new` beside `old` (snapshot and restriction digests recomputed)."""
    for kind, key, payload in admin.execute("select fact_kind,entity_key,payload from authz.nexloop_authority_facts where tenant_id=%s and %s=any(entity_key)",
                                            (tenant, old)).fetchall():
        body = json.loads(json.dumps(payload).replace(old, new))
        body.pop('snapshot_digest', None)
        fact = MODELS[kind].model_validate_json(json.dumps(body))
        admin.execute('insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) values(%s,%s,%s,%s)',
                      (tenant, kind, [new if k == old else k for k in key], Jsonb(fact.model_dump(mode='json'))))
    for kind, key, payload in admin.execute("""select fact_kind,entity_key,payload from authz.nexloop_authority_facts
            where tenant_id=%s and not (%s=any(entity_key)) and payload::text like %s""", (tenant, old, '%' + old + '%')).fetchall():
        body = beside(payload, old, new)
        body.pop('snapshot_digest', None)
        fact = MODELS[kind].model_validate_json(json.dumps(body))
        admin.execute('update authz.nexloop_authority_facts set payload=%s where tenant_id=%s and fact_kind=%s and entity_key=%s',
                      (Jsonb(fact.model_dump(mode='json')), tenant, kind, key))


def reauthenticate(f):
    """Authority facts changed (a new directory hash): sessions authenticate again, as test_context_artifacts.reconfigure does."""
    backend = f['backend']
    f['source'] = backend.authenticate(f['source_token'], world='real')
    f['route'] = backend.authenticate(f['f']['tokens']['assembly-route'], world='real')
    f['planner'] = backend.authenticate(f['f']['tokens']['assembly-planner'], world='real')
    f['relay'] = MessageRelay(route=f['route'], source=f['source'], planner=f['planner'], executor_token=f['f']['tokens']['assembly-executor'],
                              vault=f['vault'], recipe=f['f']['recipe'])


def reply(f, tmp_path):
    """The relay issues and binds; the worker's guard submits the reply intent. Returns the receipt or the error type."""
    out = f['relay'].run_once()
    assert out == 'queued', getattr(f['relay'], 'last_handoff', None)
    with active_worker(f, tmp_path) as (worker, activation, command, _):
        try:
            return worker.runtime_effect_tool(activation_ref=activation['activation_ref'], command=command, tool_operation='submit',
                                              parameters={'message': 'v2 reply'})['receipt']
        except Exception as failure:
            return type(failure).__name__


def proofs(admin):
    return admin.execute('select executed_resource,scope_proof_resource,publication_id::text,consumer_version from control.nexloop_effect_scope_proofs').fetchall()


def registrar_versions(admin):
    return sorted({r[0] for r in admin.execute("""select claim->'binding'->'action_reference'->>'version' from runtime.nexloop_action_claims
        where action_name='nexloop.plan.bind_effect_context'""").fetchall()})


def test_an_existing_consumer_moved_to_v2_is_answered_through_the_successor_proof(context_message, admin, tmp_path):  # noqa: F811
    f = context_message
    published = publish_v2(f, admin)
    receipt = reply(f, tmp_path)
    assert isinstance(receipt, dict) and receipt['intent_id'], receipt
    assert registrar_versions(admin) == ['2']  # the registrar :2 covers Consumer:2 and the v1 goal/step/control
    assert proofs(admin) == [(f'eios:action:{EFFECT}:1', f'eios:action:{EFFECT}:2', published['publication'], 2)]


def test_a_consumer_still_at_v1_is_answered_at_1_without_a_proof(context_message, admin, tmp_path):  # noqa: F811
    f = context_message
    publish_v2(f, admin, objects=False)  # customer-5: published v2, its Consumer object still v1
    receipt = reply(f, tmp_path)
    assert isinstance(receipt, dict), receipt
    assert registrar_versions(admin) == ['1'] and proofs(admin) == []


def test_without_the_followed_grant_the_registrar_does_not_bind(context_message, admin, tmp_path):  # noqa: F811
    f = context_message
    publish_v2(f, admin, follow=False)
    with pytest.raises(MessageRelayUnavailable):
        f['relay'].run_once()
    assert admin.execute('select count(*) from control.nexloop_effect_contexts').fetchone()[0] == 0


@pytest.mark.parametrize('case', [
    'no_publication_record', 'publisher_not_human', 'successor_policy_differs', 'successor_scope_extra_type',
    'added_property_required', 'added_property_hidden', 'added_property_default', 'existing_property_changed',
    'added_property_required_nullable', 'added_property_not_nullable', 'other_type_key_changed', 'successor_scope_wrong_version', 'publication_other_type', 'publication_other_version', 'successor_inactive', 'successor_missing', 'successor_other_world', 'successor_other_tenant',
    'successor_target_systems_differ', 'successor_capability_differs', 'successor_receipt_differs',
])
def test_the_successor_proof_refuses_when_one_condition_fails(context_message, admin, tmp_path, case):  # noqa: F811
    f = context_message
    kwargs = {
        'no_publication_record': dict(record=False),
        'publisher_not_human': dict(publisher='service:synthetic'),
        'successor_policy_differs': dict(edit_successor=lambda row: row['definition']['input_schema']['properties']['message'].update(maxLength=4000)),
        'successor_scope_extra_type': dict(edit_successor=lambda row: extra_type(row)),
        'added_property_required': dict(v2=lambda old: appended(old, required=True, nullable=False)),
        'added_property_hidden': dict(v2=lambda old: appended(old, visibility='hidden')),
        'added_property_default': dict(v2=lambda old: appended(old, default_value='morning')),
        'existing_property_changed': dict(v2=lambda old: changed_first(old)),
        'added_property_required_nullable': dict(v2=lambda old: appended(old, required=True)),
        'added_property_not_nullable': dict(v2=lambda old: appended(old, nullable=False)),
        'other_type_key_changed': dict(v2=lambda old: {**appended(old), 'description': (old.get('description') or '') + ' changed'}),
        'successor_scope_wrong_version': dict(edit_successor=lambda row: consumer_ref_version(row, 1)),
        'publication_other_type': dict(record_as=('Goal', 1, 2)),
        'publication_other_version': dict(record_as=('Consumer', 4, 5)),
        'successor_inactive': dict(effect='inactive'),
        'successor_missing': dict(effect='skip'),
        'successor_other_world': dict(effect=('world', 'shadow')),
        'successor_other_tenant': dict(effect=('tenant', str(uuid.uuid4()))),
        'successor_target_systems_differ': dict(edit_successor=lambda row: row['definition']['governance']['change_scope'].update(
            target_systems=row['definition']['governance']['change_scope']['target_systems'] + ['postgres'])),
        'successor_capability_differs': dict(edit_successor=lambda row: row.update(capability={**row['capability'], 'idempotent': not row['capability']['idempotent']})),
        'successor_receipt_differs': dict(edit_successor=lambda row: row['definition']['governance'].update(
            idempotency={**row['definition']['governance']['idempotency'], 'key_fields': list(row['definition']['governance']['idempotency']['key_fields']) + ['message']})),
    }[case]
    publish_v2(f, admin, **kwargs)
    outcome = reply(f, tmp_path)
    assert not isinstance(outcome, dict), outcome
    assert proofs(admin) == [] and admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()[0] == 0


def consumer_ref_version(row, version):
    """7.2: the successor equal to :1 without versions, but its scope names Consumer:`version`, not the published v2."""
    d = row['definition']
    for refs in (d['object_types'], d['governance']['change_scope']['object_types']):
        for ref in refs:
            if ref['stable_name'] == 'Consumer':
                ref['version'] = version


def appended(old, **change):
    return {**old, 'version': 2, 'properties': list(old['properties']) + [{**ADDED, **change}]}


def changed_first(old):
    props = copy.deepcopy(list(old['properties']))
    assert props, 'the fixture Consumer has properties'
    props[0]['description'] = (props[0].get('description') or '') + ' changed'
    return {**old, 'version': 2, 'properties': props + [ADDED]}


def extra_type(row):
    d = row['definition']
    extra = {**d['object_types'][0], 'stable_name': 'Goal'}
    d['object_types'] = d['object_types'] + [extra]
    d['governance']['change_scope']['object_types'] = d['governance']['change_scope']['object_types'] + [extra]


def test_a_wrong_registrar_choice_is_refused_never_widened(context_message, admin, tmp_path, monkeypatch):  # noqa: F811
    """L6 Q1: the Python choice is advisory. Naming :1 for a Consumer at v2 is refused by the registrar command (scope check), nothing
    is bound."""
    from nexloop_eios.effect_contexts import EffectContextRegistrar
    f = context_message
    publish_v2(f, admin)
    monkeypatch.setattr(EffectContextRegistrar, '_version', lambda self, db, verb, payload: 1)
    with pytest.raises(MessageRelayUnavailable):
        f['relay'].run_once()
    assert admin.execute('select count(*) from control.nexloop_effect_contexts').fetchone()[0] == 0
    assert registrar_versions(admin) in ([], ['1'])


def test_the_transitional_rule_goes_with_option_b(admin):
    """A'-6: the successor proof exists only while effect Actions are pinned to one version (stable_name is the allow-list key, every
    row version 1). When option B lets an effect Action carry several versions this fails: remove
    authz.nexloop_effect_scope_successor_proof, its call in the effect intent command and control.nexloop_effect_scope_proofs."""
    from nexloop_eios.bootstrap import bootstrap
    bootstrap(admin)
    key = admin.execute("""select array_agg(a.attname order by a.attname) from pg_index i join pg_attribute a on a.attrelid=i.indrelid and a.attnum=any(i.indkey)
        where i.indrelid='control.nexloop_effect_actions'::regclass and i.indisprimary""").fetchone()[0]
    assert key == ['stable_name']
    assert admin.execute('select count(*) from control.nexloop_effect_actions where version<>1').fetchone()[0] == 0
    assert admin.execute("select count(*) from pg_proc where proname='nexloop_effect_scope_successor_proof'").fetchone()[0] == 1


@pytest.mark.parametrize('versions,expected', [
    ({}, 1),                                       # all four objects v1: the registrar :1
    ({'Consumer': 2}, 2),                          # an existing customer after the publication: :2 holds Consumer:2 and the v1 rest
    ({'Consumer': 2, 'Goal': 2}, None),            # no registrar version holds Goal:2
    ({'EffectControl': 2}, None),                  # Consumer v1 but EffectControl v2: neither :1 nor :2 holds both
], ids=['all_v1', 'consumer_v2', 'consumer_and_goal_v2', 'control_v2'])
def test_the_registrar_version_covers_each_objects_exact_version(context_message, admin, tmp_path, versions, expected):  # noqa: F811
    """L6 Q2: the highest version whose scope holds the four objects' exact (type, version), not the highest version then a check."""
    import psycopg
    from psycopg.conninfo import make_conninfo
    from psycopg.types.json import Jsonb as J
    f = context_message
    tenant = f['original']['tenant']
    publish_v2(f, admin, objects=False)  # :2 published and granted; the objects still v1
    reply(f, tmp_path)                    # a bound step to ask about
    step = admin.execute("select object_id,properties from ontology.objects where tenant_id=%s and type_name='PlanStep' order by created_at desc limit 1", (tenant,)).fetchone()
    ids = {'PlanStep': step[0], 'Goal': step[1]['goal_id'], 'EffectControl': step[1]['control_id'], 'Consumer': step[1]['consumer_id']}
    for type_name, version in versions.items():
        old = admin.execute('select definition from ontology.object_type_versions where tenant_id=%s and type_name=%s and version=1', (tenant, type_name)).fetchone()[0]
        admin.execute('insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,%s,%s,%s) on conflict do nothing',
                      (tenant, type_name, version, J({**old, 'version': version})))
        admin.execute('update ontology.objects set schema_version=%s where tenant_id=%s and object_id=%s', (version, tenant, ids[type_name]))
    session = f['planner']._session
    with psycopg.connect(make_conninfo(f['original']['pg'], user='nexloop_api'), autocommit=True) as api:
        chosen = api.execute('select authz.nexloop_effect_registrar_version(%s,%s,%s,%s)',
                             (session.token_digest, session.world, 'bind', J({'verb': 'bind', 'step_id': step[0]}))).fetchone()[0]
    assert chosen == expected


def test_the_added_optional_property_is_ignored_on_the_1_path(context_message, admin, tmp_path):  # noqa: F811
    """A'-3 (L6 suggestion): a v2 customer whose new optional property holds a value is answered at :1 exactly as before - the
    registrar, the intent and the proof do not read Consumer properties - and the value reaches neither the Run's context input nor
    its command. Limitation: in this fixture the Source's context carries no Consumer property at all (checked: an existing property's
    value does not appear either), so the context half shows only that nothing new is read; property reads of context assembly stay
    governed by property grants, independent of the Action version."""
    import json as _json
    f = context_message
    tenant = f['original']['tenant']
    publish_v2(f, admin)
    marker = 'tuesday-evening-window'
    admin.execute("""update ontology.objects set properties=properties||jsonb_build_object('delivery_windows',%s::text)
        where tenant_id=%s and type_name='Consumer' and schema_version=2""", (marker, tenant))
    assert f['relay'].run_once() == 'queued'
    with active_worker(f, tmp_path) as (worker, activation, command, text):
        assert marker not in text and marker not in _json.dumps(command)
        receipt = worker.runtime_effect_tool(activation_ref=activation['activation_ref'], command=command, tool_operation='submit',
                                             parameters={'message': 'v2 reply'})['receipt']
    assert receipt['intent_id'] and [p[0] for p in proofs(admin)] == [f'eios:action:{EFFECT}:1']


def test_a_successor_scope_without_the_consumers_version_is_refused(context_message, admin, tmp_path):  # noqa: F811
    """§3 item 8 / 7.2: Consumer moved to v3 while only :2 (Consumer:2) is published - no successor holds Consumer:3."""
    f = context_message
    tenant = f['original']['tenant']
    publish_v2(f, admin)
    old = admin.execute("select definition from ontology.object_type_versions where tenant_id=%s and type_name='Consumer' and version=2", (tenant,)).fetchone()[0]
    admin.execute("insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,'Consumer',3,%s)",
                  (tenant, Jsonb({**old, 'version': 3, 'properties': list(old['properties']) + [{**ADDED, 'property_name': 'added_v3'}]})))
    admin.execute("update ontology.objects set schema_version=3 where tenant_id=%s and type_name='Consumer'", (tenant,))
    reauthenticate(f)  # a new published type version moves the directory hash
    with pytest.raises(MessageRelayUnavailable):  # the registrar has no version holding Consumer:3 either: nothing is bound
        f['relay'].run_once()
    assert f['relay']._diagnostic_failure[1] == 'EffectContextUnavailable'
    assert proofs(admin) == [] and admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()[0] == 0


def test_the_comparison_is_cumulative_from_the_executed_scope(context_message, admin, tmp_path):  # noqa: F811
    """L6 D1: v1 -> v2 hides a property, v2 -> v3 appends one. Executing :1 (Consumer:1) on a v3 object is refused: v1's properties
    are no prefix of v3's, although v3 extends v2 additively."""
    f = context_message
    publish_version(f, admin, 1, 2, schema=lambda old: {**old, 'version': 2, 'properties': [dict(p, visibility='hidden') if i == 0 else p
                                                                                          for i, p in enumerate(old['properties'])]})
    publish_version(f, admin, 2, 3)
    outcome = reply(f, tmp_path)
    assert not isinstance(outcome, dict), outcome
    assert proofs(admin) == [] and admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()[0] == 0


def test_a_cumulative_additive_chain_is_proven_by_the_latest_successor(context_message, admin, tmp_path):  # noqa: F811
    """The positive twin of the D1 case: v1 -> v2 -> v3 each append a property; :1 on a v3 object is proven by :3."""
    f = context_message
    publish_version(f, admin, 1, 2)
    third = publish_version(f, admin, 2, 3)
    receipt = reply(f, tmp_path)
    assert isinstance(receipt, dict), receipt
    assert proofs(admin) == [(f'eios:action:{EFFECT}:1', f'eios:action:{EFFECT}:3', third['publication'], 3)]


def test_a_concurrent_upgrade_after_the_choice_is_refused_and_leaves_no_context(context_message, admin, tmp_path, monkeypatch):  # noqa: F811
    """§3 item 9 (Q3): the registrar version is chosen, then the Consumer moves on before the registration; 0179 re-checks in the
    same transaction and refuses, and no effect context or finalized claim is left."""
    import psycopg
    from nexloop_eios.effect_contexts import EffectContextRegistrar
    f = context_message
    tenant = f['original']['tenant']
    publish_v2(f, admin, objects=False)
    old = admin.execute("select definition from ontology.object_type_versions where tenant_id=%s and type_name='Consumer' and version=2", (tenant,)).fetchone()[0]
    admin.execute("insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,'Consumer',3,%s)",
                  (tenant, Jsonb({**old, 'version': 3, 'properties': list(old['properties']) + [{**ADDED, 'property_name': 'added_v3'}]})))
    reauthenticate(f)  # a new published type version moves the directory hash
    chosen = []
    original = EffectContextRegistrar._version

    def choose_then_upgrade(self, db, verb, payload):
        version = original(self, db, verb, payload)
        chosen.append(version)
        with psycopg.connect(f['original']['pg'], autocommit=True) as other:  # a concurrent publication's object migration
            other.execute("update ontology.objects set schema_version=3 where tenant_id=%s and type_name='Consumer'", (tenant,))
        return version
    monkeypatch.setattr(EffectContextRegistrar, '_version', choose_then_upgrade)
    with pytest.raises(MessageRelayUnavailable):
        f['relay'].run_once()
    assert chosen == [1] and f['relay']._diagnostic_failure[1] == 'EffectContextUnavailable'
    assert admin.execute('select count(*) from control.nexloop_effect_contexts').fetchone()[0] == 0
    assert admin.execute("""select count(*) from runtime.nexloop_action_claims where action_name='nexloop.plan.bind_effect_context'
        and claim->>'state'='terminal'""").fetchone()[0] == 0


def test_a_publication_with_a_changed_permission_digest_cannot_be_recorded(context_message, admin):  # noqa: F811
    """7.5: the proof needs permission_digest_before = permission_digest_after; the table refuses any other row, so no such
    publication can exist to be found."""
    import psycopg
    f = context_message
    with pytest.raises(psycopg.errors.CheckViolation), admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("""insert into ontology.nexloop_schema_publications(tenant_id,world,publication_id,proposal_id,kind,type_name,version_before,version_after,
            successor_actions,permission_digest_before,permission_digest_after,compatibility_report_digest,published_by,rationale)
            values(%s,'real',%s,%s,'publish','Consumer',1,2,%s,%s,%s,%s,'human:synthetic','digest changed')""",
            (f['original']['tenant'], str(uuid.uuid4()), str(uuid.uuid4()), [f'eios:action:{EFFECT}:2'], 'a' * 64, 'c' * 64, 'b' * 64))
