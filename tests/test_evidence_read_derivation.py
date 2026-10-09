"""Evidence readers through the accepted-Message READ derivation (0077/0080/0086).

No per-Message or per-Conversation authority is configured anywhere in this file: the
reading service holds only its Action authority, current READ on the Consumer and an
explicit message_read_rule. Synthetic disposable PG only.
"""
import hashlib
import json
import psycopg
import pytest
from psycopg.types.json import Jsonb
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.claim_store import ConversationClaimExtractor
from nexloop_eios.message_read import MessageReadRule, RULE_FIELDS
from multi_authority_fixture import seed_multi_authority
from test_claim_store_pg import window, registered, response  # noqa: F401
from test_conversation_messages import conversations  # noqa: F401
from test_browser_business_authorization import browser_business  # noqa: F401
from test_action_definitions import published_action  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from local_message_assembly_fixture import assembled_message, business_plan, configured  # noqa: F401


def publish_rule(admin, tenant, principal, fields=RULE_FIELDS, *, active=True, valid_until='2999-01-01T00:00:00Z'):
    rule = MessageReadRule(tenant_id=tenant, principal_id=principal, type_name='Message', fields=tuple(fields), active=active, valid_until=valid_until)
    admin.execute("insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) values(%s,'message_read_rule',%s,%s) "
                  "on conflict(tenant_id,fact_kind,entity_key) do update set payload=excluded.payload", (tenant, [principal], Jsonb(rule.model_dump(mode='json'))))


def consumer_of(admin, conversation_id):
    return admin.execute('select consumer_id from runtime.nexloop_conversations where conversation_id=%s', (conversation_id,)).fetchone()[0]


extractor_token = [None]


def claim_extractor(fixture, admin, conversation_id, *, fields=RULE_FIELDS, consumer_read=True, suffix='-claim-derived'):
    reader = fixture['reader']
    targets = [('eios:action:nexloop.claim.extract:1', ResourceType.ACTION, Operation.EXECUTE)]
    if consumer_read:
        targets.append(('eios:object:Consumer/' + consumer_of(admin, conversation_id), ResourceType.OBJECT, Operation.READ))
    session, token = seed_multi_authority(admin, reader.pool, targets, identity_suffix=suffix)
    publish_rule(admin, 'synthetic-a', session.authentication.subject_principal_id, fields)
    session = authenticate_service(reader.pool, token, world='real')
    extractor_token[0] = token
    assert admin.execute("select count(*) from authz.nexloop_authority_facts where fact_kind='grants' and entity_key[1]=%s and "
                         "(entity_key[2] like '%%Message/%%' or entity_key[2] like '%%Conversation/%%')",
                         (session.authentication.subject_principal_id,)).fetchone() == (0,)
    return ConversationClaimExtractor(reader.pool, session, reader.signer, None, timezone='Asia/Shanghai')


def test_claim_extraction_reads_its_window_through_derivation(window, admin):
    fixture, conversation_id, ids = window
    extractor = claim_extractor(fixture, admin, conversation_id)
    provider, messages = registered(extractor, conversation_id, ids)
    assert [m.body for m in messages][:1] and len(messages) == len(ids)
    result = extractor.extract(conversation_id=conversation_id, message_ids=ids)
    assert result['replay'] is False and len(result['claim_ids']) == 6 and provider.calls == 1
    assert admin.execute('select count(*) from ontology.nexloop_claims').fetchone() == (6,)


@pytest.mark.parametrize('case', ['rule_without_metadata_fields', 'rule_without_conversation', 'no_consumer_read'])
def test_claim_extraction_fails_closed_without_full_derivation(window, admin, case):
    fixture, conversation_id, ids = window
    fields = {'rule_without_metadata_fields': ('actor', 'body'),
              'rule_without_conversation': ('accepted_at', 'actor', 'body', 'sequence')}.get(case, RULE_FIELDS)
    extractor = claim_extractor(fixture, admin, conversation_id, fields=fields, consumer_read=case != 'no_consumer_read', suffix='-claim-' + case.replace('_', '-'))
    with pytest.raises(Exception):
        extractor.load_window(conversation_id, ids)
    assert admin.execute('select count(*) from ontology.nexloop_claims').fetchone() == (0,)


def test_claim_extraction_denied_after_rule_deactivated(window, admin):
    fixture, conversation_id, ids = window
    extractor = claim_extractor(fixture, admin, conversation_id)
    provider, messages = registered(extractor, conversation_id, ids)
    publish_rule(admin, 'synthetic-a', extractor.session.authentication.subject_principal_id, active=False)
    extractor.session = authenticate_service(fixture['reader'].pool, extractor_token[0], world='real')
    with pytest.raises(Exception):
        extractor.extract(conversation_id=conversation_id, message_ids=ids)
    assert admin.execute('select count(*) from ontology.nexloop_claims').fetchone() == (0,)


def test_derived_message_field_must_be_listed_and_not_superseded(window, admin):
    fixture, conversation_id, ids = window
    extractor = claim_extractor(fixture, admin, conversation_id, fields=('actor', 'body'), suffix='-claim-fields')
    from nexloop_eios.object_reads import AuthorizedObjectReader
    reader = AuthorizedObjectReader(fixture['reader'].pool, extractor.session, fixture['reader'].signer)
    assert set(reader.get('Message', ids[0], fields=('actor', 'body'))['properties']) == {'actor', 'body'}
    with pytest.raises(Exception):
        reader.get('Message', ids[0], fields=('sequence',))
    with pytest.raises(Exception):
        reader.get('Conversation', conversation_id, fields=('consumer_id',))


@pytest.mark.parametrize('assembled_message', [False], indirect=True)
def test_assessment_user_statement_evidence_through_derivation(assembled_message, admin):
    from psycopg.conninfo import make_conninfo
    from nexloop_eios.assembly import open_core
    from nexloop_eios.browser_identity import open_browser_identity
    from nexloop_eios.browser_sessions import PostgresBrowserSessionUnitOfWork
    from test_browser_evidence import authenticate
    from test_browser_session_creation import create
    from nexloop_eios.browser_authorization import authenticate_browser_business
    from nexloop_eios.conversation_messages import ConversationMessagePort
    from nexloop_eios.postgres_artifacts import AuthoritySigner
    from nexloop_eios.action_definitions import PostgresActionDefinitionReader
    from nexloop_eios.assessment_actions import AuthorizedAssessmentProjection
    from eios.ontology.definitions import ActionDefinition
    from eios.ontology.version_resolution import CapabilityContractSnapshot
    from test_assessment_candidate import prepare_assessment
    f = assembled_message; o = f['original']; tenant = o['tenant']; human = f['base']['human']
    signer = AuthoritySigner('explicit-configuration', o['paths']['backend_signing'].read_bytes())
    with open_browser_identity(make_conninfo(o['pg'], user='nexloop_identity')) as identity_pool, open_core(make_conninfo(o['pg'], user='nexloop_api')) as pool:
        session_uow = PostgresBrowserSessionUnitOfWork(identity_pool, tenant_id=tenant, application_id=o['application'])
        ident = (make_conninfo(o['pg'], user='nexloop_identity'), session_uow.get_subject(human['subject_id']), session_uow.get_membership(tenant, human['principal_id']),
                 session_uow.get_local_account(tenant, human['account_id']), o['paths']['password'].read_text())
        issued = create(session_uow, ident, authenticate(session_uow, ident).evidence)
        actual = authenticate_browser_business(pool, issued.session, world='real')
        messages = ConversationMessagePort(pool, actual, signer); conversation = messages.create_conversation(idempotency_key='derived-assessment-conversation')
        body = 'I have not ended the relationship; I am away this month.'
        mid = messages.accept_message(conversation_id=conversation['id'], idempotency_key='derived-assessment-message', body=body)['message']['id']
        original = next(a for a in f['manifest']['actions'] if a['definition']['stable_name'] == 'Consumer.create')
        definition = ActionDefinition.model_validate_json(json.dumps(original['definition'])); cap = CapabilityContractSnapshot.model_validate_json(json.dumps(original['capability']))
        reader = PostgresActionDefinitionReader(pool, actual, signer)
        consumer = consumer_of(admin, conversation['id'])
        # Only the Consumer READ: no Message/<id> targets (contrast test_assessment_candidate).
        port, creator, p, obj, token = prepare_assessment(reader, definition, cap, admin, f['setup']['consumer_id'], tenant,
                                                          [('eios:object:Consumer/' + consumer, ResourceType.OBJECT, Operation.READ)])
        publish_rule(admin, tenant, port.session.authentication.subject_principal_id, ('actor', 'body'))
        port.session = authenticate_service(pool, token, world='real')
        values = p | {'conclusion': body, 'epistemic_kind': 'user_statement', 'evidence_message_id': mid,
                      'evidence_content_hash': hashlib.sha256(body.encode()).hexdigest(), 'corrects_revision': 1}
        got = port.correct(action_name='RelationshipAssessment.correct', action_version=1, intent_id='derived-human-correction', object_id=obj, expected_revision=1, properties=values)
        current = AuthorizedAssessmentProjection(pool, port.session, signer).current(obj)
        assert got['revision'] == 2 and current['formal'][0]['properties'] == values
        # Deactivating the rule denies the next evidence-bearing correction.
        publish_rule(admin, tenant, port.session.authentication.subject_principal_id, ('actor', 'body'), active=False)
        port.session = authenticate_service(pool, token, world='real')
        with pytest.raises(Exception):
            port.correct(action_name='RelationshipAssessment.correct', action_version=1, intent_id='derived-human-correction-2', object_id=obj, expected_revision=2,
                         properties=values | {'corrects_revision': 2})


from test_context_artifacts import context_message  # noqa: E402,F401


def test_relationship_reader_message_envelope_through_derivation(context_message, admin):
    """v4 relationship evidence: the Source (rule + Consumer READ, no Message grants) signs a
    derived Message envelope that the v4 SQL verifier (0072) accepts; deactivation denies it."""
    from nexloop_eios.relationship_context import RelationshipContextReader, RelationshipContextRecipe
    f = context_message; source = f['source']; mid = f['message']['id']; tenant = f['original']['tenant']
    principal = source._session.authentication.subject_principal_id
    assert admin.execute("select count(*) from authz.nexloop_authority_facts where fact_kind='grants' and entity_key[1]=%s and entity_key[2] like %s",
                         (principal, '%Message/' + mid + '%')).fetchone() == (0,)
    reader = RelationshipContextReader(source._backend._pool, source._session, source._backend._signer, RelationshipContextRecipe(('a' * 64,)))
    envelope = reader.message_envelope(mid)
    assert json.loads(envelope['text'])['derivation'] == 'accepted-message-v1'
    value = admin.execute('select authz.nexloop_relationship_message_read(%s,%s,%s,%s)', (source._session.token_digest, 'real', mid, Jsonb(envelope))).fetchone()[0]
    assert value['properties']['body'] == f['message']['body']
    rule = admin.execute("select payload from authz.nexloop_authority_facts where tenant_id=%s and fact_kind='message_read_rule' and entity_key=%s", (tenant, [principal])).fetchone()[0]
    rule['active'] = False
    admin.execute("update authz.nexloop_authority_facts set payload=%s where tenant_id=%s and fact_kind='message_read_rule' and entity_key=%s", (Jsonb(rule), tenant, [principal]))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        admin.execute('select authz.nexloop_relationship_message_read(%s,%s,%s,%s)', (source._session.token_digest, 'real', mid, Jsonb(envelope)))
