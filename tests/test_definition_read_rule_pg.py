"""I29 (0381, stage-principals v9): the relay Source's definition_read_rule - definition-level READ of the listed published types
for the recall gate only. Every listed type and its unrestricted type-level properties are readable; an owner-restricted group
is not; no object, instance or property value read accepts it; an inactive or expired rule, another world or another principal
reads nothing. Actual PostgreSQL, synthetic schema."""
import json
from pathlib import Path

import pytest

from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from multi_authority_fixture import seed_multi_authority
from nexloop_eios.object_reads import AuthorizedObjectReader
from nexloop_eios.recall import EiosRecallAuthorizer
from test_recall_pg import A,put_object
from test_semantic_access_record_pg import recall_db,semantic  # noqa: F401  (fixtures)
from test_semantic_terms_pg import definition_rule,relay_source_targets

ROOT = Path(__file__).resolve().parents[1]
CONSUMER_PROPERTIES = ('budget_level','display_name','member_no','monthly_income','nickname','waterproof_concern')


def relay(f, suffix='-definitions'):
    session, token = seed_multi_authority(f['admin'], f['worker'], relay_source_targets(), identity_suffix=suffix, world='real', tenant=A)
    return session, token


def rule(f, token, **options):
    """Write the rule, then authenticate again: a fact write moves the authority revision (the earlier session is stale)."""
    from nexloop_eios.authorization import authenticate_service
    principal = authenticate_service(f['worker'], token, world='real').authentication.subject_principal_id
    definition_rule(f['admin'], principal, **options)
    return authenticate_service(f['worker'], token, world='real')


def readable(f, session, kind, names):
    return EiosRecallAuthorizer(f['worker'], session).readable(kind, names)


def restrict(f, token, groups):
    admin = f['admin']
    admin.execute("""insert into authz.nexloop_authority_facts values(%s,'property_group_restriction',array['Consumer'],%s)
        on conflict(tenant_id,fact_kind,entity_key) do update set payload=excluded.payload""",
        (A, json.dumps({'tenant_id': A, 'type_name': 'Consumer', 'restricted_groups': groups, 'decision': 'synthetic'})))
    from nexloop_eios.authorization import authenticate_service
    return authenticate_service(f['worker'], token, world='real')


def test_the_stage_rule_lists_the_twenty_published_types_and_adds_no_grant():
    stage = json.loads((ROOT / 'deploy/authorization/stage-principals.v1.json').read_text())
    glossary = json.loads((ROOT / 'deploy/configuration/world-model-glossary.v1.json').read_text())
    [rule] = stage['definition_read_rules']
    # A new published type is added here explicitly: this assertion fails until both lists agree.
    assert rule['principal'] == 'relay_source' and rule['type_names'] == sorted(glossary['object_types']) and len(rule['type_names']) == 20
    assert not any(g['principal'] == 'relay_source' and g['resource_type'] in ('object_type', 'property') for g in stage['grants'])


def test_register_dry_run_shows_the_rule_and_unchanged_grants():
    import sys
    sys.path.insert(0, str(ROOT / 'scripts/deploy'))
    import stage_secrets
    report = stage_secrets.register(None, tenant=A, database_url_file=None, signing_key_id='k', credential_expires_at=None, dry_run=True)
    assert report['definition_read_rules'] == {'relay_source': 20}
    assert not any(g.startswith('relay_source ') and (' object_type ' in g or ' property ' in g) for g in report['grants'])
    facts = stage_secrets._definition_read_facts(*stage_secrets.merged_manifest(), A)
    assert [(k, key) for k, key, _ in facts] == [('definition_read_rule', ['nexloop-service-relay-source-principal'])]


def test_listed_types_and_their_unrestricted_properties_are_readable_restricted_groups_are_not(semantic):
    f, _, _ = semantic; session, token = relay(f)
    props = [f'Consumer/{p}' for p in CONSUMER_PROPERTIES]
    assert readable(f, session, ResourceType.OBJECT_TYPE, ['Consumer']) == frozenset()  # no rule yet
    session = rule(f, token)
    assert readable(f, session, ResourceType.OBJECT_TYPE, ['Consumer', 'Umbrella']) == {'Consumer'}  # Umbrella is not listed
    assert readable(f, session, ResourceType.PROPERTY, props + ['Consumer/no_such']) == set(props)
    session = restrict(f, token, ['spending_power'])  # budget_level and monthly_income
    assert readable(f, session, ResourceType.PROPERTY, props) == set(props) - {'Consumer/budget_level', 'Consumer/monthly_income'}


def test_no_instance_or_value_read_accepts_the_rule(semantic):
    f, _, signer = semantic; admin = f['admin']; _, token = relay(f)
    oid = put_object(admin, A, 'real', 'definition-rule-instance', {'member_no': 'm-1', 'display_name': 'x'})
    session = rule(f, token)
    assert readable(f, session, ResourceType.OBJECT, [f'Consumer/{oid}']) == frozenset()
    assert readable(f, session, ResourceType.PROPERTY, [f'Consumer/{oid}/member_no']) == frozenset()
    with pytest.raises(Exception):
        AuthorizedObjectReader(f['worker'], session, signer).get('Consumer', oid, fields=('member_no',))
    # The SQL reader itself refuses an object or per-object property id.
    call = "select authz.nexloop_definition_read(%s,'real',%s,%s)"
    with f['worker'].connection() as c:
        assert c.execute(call, (session.token_digest, 'property', [f'Consumer/{oid}/member_no'])).fetchone()[0] == []
        assert c.execute(call, (session.token_digest, 'object', [f'Consumer/{oid}'])).fetchone()[0] == []


def test_an_inactive_or_expired_rule_another_world_or_principal_reads_nothing(semantic):
    f, _, _ = semantic; _, token = relay(f); _, other_token = relay(f, '-other-principal')
    session = rule(f, token)
    from nexloop_eios.authorization import authenticate_service
    other = authenticate_service(f['worker'], other_token, world='real')
    assert readable(f, other, ResourceType.OBJECT_TYPE, ['Consumer']) == frozenset()
    with f['worker'].connection() as c:
        assert c.execute("select authz.nexloop_definition_read(%s,'test','object_type',array['Consumer'])", (session.token_digest,)).fetchone()[0] == []
    session = rule(f, token, active=False)
    assert readable(f, session, ResourceType.OBJECT_TYPE, ['Consumer']) == frozenset()
    session = rule(f, token, valid_until='2020-01-01T00:00:00+00:00')
    assert readable(f, session, ResourceType.OBJECT_TYPE, ['Consumer']) == frozenset()


def test_a_definition_proof_is_accepted_only_inside_the_definitions_read(semantic):
    import psycopg
    from psycopg.types.json import Jsonb
    from nexloop_eios.context_engine.authority import ContextDenied, definition_proof
    f, _, _ = semantic; admin = f['admin']; _, token = relay(f)
    session = rule(f, token)
    proof = definition_proof(f['worker'], session, ResourceType.OBJECT_TYPE, 'Consumer')
    assert proof['derivation'] == 'definition-read-v1' and proof['resource_id'] == 'eios:object_type:Consumer'
    with pytest.raises(ContextDenied):
        definition_proof(f['worker'], session, ResourceType.OBJECT, 'Consumer/' + 'a' * 64)
    # Outside authz.nexloop_context_read's definitions entry the shared read assertion refuses it - also with the I30 flag set.
    for flagged in (False, True):
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='outside definitions'):
            with admin.transaction():
                if flagged:
                    admin.execute("select set_config('nexloop.definition_read_scope','definitions',true)")
                admin.execute('select authz.nexloop_assert_read_authority(%s,%s,%s)', (session.token_digest, 'real', Jsonb(proof)))


def test_an_application_role_flag_opens_no_object_or_value_read(semantic):
    """L6 I30: an application role sets any session flag, then reads an object and a property value with definition-read-v1
    proofs in the same transaction: 42501, and no data."""
    import hmac
    import psycopg
    from nexloop_eios.context_engine.authority import definition_proof
    from nexloop_eios.postgres_artifacts import canonical_payload
    f, _, signer = semantic; admin = f['admin']; _, token = relay(f)
    oid = put_object(admin, A, 'real', 'definition-flag-instance', {'member_no': 'm-2', 'display_name': 'y'})
    session = rule(f, token)
    type_proof = definition_proof(f['worker'], session, ResourceType.OBJECT_TYPE, 'Consumer')
    prop_proof = definition_proof(f['worker'], session, ResourceType.PROPERTY, 'Consumer/member_no')
    for top, props in ((type_proof, [prop_proof]),  # as issued
                       ({**type_proof, 'resource_id': f'eios:object:Consumer/{oid}', 'target_resource': f'eios:object:Consumer/{oid}'},
                        [{**prop_proof, 'resource_id': f'eios:property:Consumer/{oid}/member_no', 'target_resource': f'eios:property:Consumer/{oid}/member_no'}])):
        claims = {**top, 'protocol': 'nexloop-object-read-v1', 'key_id': signer.key_id, 'type_name': 'Consumer', 'object_id': oid,
                  'fields': ['member_no'], 'property_authorities': props}
        text = canonical_payload(claims)
        signature = hmac.new(signer.material, ('nexloop-object-read-v1:' + text).encode(), 'sha256').hexdigest()
        with f['worker'].connection() as c:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with c.transaction():
                    c.execute("select set_config('nexloop.definition_read_scope','definitions',true)")
                    c.execute('select authz.nexloop_read_object(%s,%s,%s,%s)', (session.token_digest, 'real', text, signature))
        # Nor may the role call the definitions entry itself.
        with f['worker'].connection() as c:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("select authz.nexloop_context_read_definitions('x','real','{}','x','{\"verb\":\"definitions\"}')")


def test_a_restricted_property_is_not_returned_by_the_semantic_read(semantic):
    from nexloop_eios.context_engine.semantic import SemanticAccessRecorder
    from test_semantic_terms_pg import adapter
    f, _, signer = semantic; _, token = relay(f)
    rule(f, token)
    session = restrict(f, token, ['spending_power'])
    refs = {i['ref'] for i in adapter(f, session, signer, SemanticAccessRecorder(f['worker'], session, signer)).browse('月收入和会员编号', extract_terms=True)['items']}
    assert 'eios:property:Consumer/member_no' in refs and not any('monthly_income' in r or 'budget_level' in r for r in refs)
