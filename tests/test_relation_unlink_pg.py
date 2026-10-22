"""NX-054 P1: the governed unlink entry (0318, nexloop_eios.relation_actions.GovernedRelationUnlinker; the 0016 link pattern).
A linked pair is removed only through an Action of capability ontology.relation.unlink with the relation's delete authority and
both endpoints' edit proofs; replay and an absent relation are successful no-ops; a link Action, a missing delete authority or a
revoked endpoint is refused and nothing is removed. Synthetic relation type and Actions, as in test_relation_actions."""
import pytest
import psycopg
from psycopg.types.json import Jsonb
from eios.authz import facts as F
from eios.authz.errors import AuthorizationUnavailable
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.ontology.models import RelationTypeDefinition, RelationCardinality
from eios.ontology.semantics import schema_contract_digest
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.object_actions import GovernedObjectCreator
from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
from nexloop_eios.relation_actions import GovernedRelationLinker, GovernedRelationUnlinker
from authority_fixture import replace_fact
from multi_authority_fixture import seed_multi_authority
from test_action_definitions import published_action  # noqa: F401
from support.tenants import TENANT_A


def action(admin, definition, capability, name, capability_name, schema):
    binding = definition.capability_binding.model_copy(update={'capability_name': capability_name})
    d = definition.model_copy(update={'stable_name': name, 'capability_binding': binding})
    c = capability.model_copy(update={'capability_name': capability_name})
    # catalog-exception: test relation type Peer with its link and unlink Actions
    admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s)',
                  (TENANT_A, 'real', f'eios:action:{name}:1', Jsonb(d.model_dump(mode='json')), Jsonb(c.model_dump(mode='json'))))
    admin.execute('insert into control.nexloop_relation_action_bindings values(%s,%s,%s,%s,%s,%s,%s)',
                  (TENANT_A, 'real', f'eios:action:{name}:1', d.reference().contract_digest, 'Peer', 1, schema_contract_digest(schema)))


@pytest.fixture
def peers(published_action, admin):  # noqa: F811
    reader, definition, capability = published_action
    creator = GovernedObjectCreator(reader.pool, reader.session, reader.signer)
    ids = [creator.create(action_name='Consumer.create', action_version=1, intent_id='synthetic-unlink-object-' + str(i), type_name='Consumer',
                          properties={})['object_id'] for i in range(2)]
    schema = RelationTypeDefinition(relation_name='Peer', source_type='Consumer', target_type='Consumer', cardinality=RelationCardinality.MANY_TO_ONE)
    admin.execute('insert into ontology.relation_type_versions(tenant_id,relation_name,version,definition) values(%s,%s,%s,%s)',
                  (TENANT_A, 'Peer', 1, Jsonb(schema.model_dump(mode='json'))))
    action(admin, definition, capability, 'Peer.link', 'ontology.relation.link', schema)
    action(admin, definition, capability, 'Peer.unlink', 'ontology.relation.unlink', schema)
    targets = [('eios:action:Peer.link:1', ResourceType.ACTION, Operation.EXECUTE), ('eios:action:Peer.unlink:1', ResourceType.ACTION, Operation.EXECUTE),
               ('eios:relation:Peer', ResourceType.RELATION, Operation.CREATE), ('eios:relation:Peer', ResourceType.RELATION, Operation.DELETE)]
    targets += [('eios:object:Consumer/' + o, ResourceType.OBJECT, Operation.EDIT) for o in ids]
    session, token = seed_multi_authority(admin, reader.pool, targets, identity_suffix='-unlinker')
    linker, unlinker = GovernedRelationLinker(reader.pool, session, reader.signer), GovernedRelationUnlinker(reader.pool, session, reader.signer)
    linker.link(action_name='Peer.link', action_version=1, intent_id='synthetic-link-for-unlink', relation_name='Peer', source_id=ids[0], target_id=ids[1], metadata={})
    return dict(unlinker=unlinker, linker=linker, ids=ids, token=token, reader=reader)


def unlink(p, intent='synthetic-unlink-001', action_name='Peer.unlink', service=None):
    return (service or p['unlinker']).unlink(action_name=action_name, action_version=1, intent_id=intent, relation_name='Peer',
                                             source_id=p['ids'][0], target_id=p['ids'][1])


def count(admin):
    return admin.execute('select count(*) from ontology.relations').fetchone()[0]


def test_unlink_removes_the_pair_and_replays(peers, admin):
    p = peers
    assert count(admin) == 1
    got = unlink(p)
    assert got['deleted'] is True and count(admin) == 0
    assert unlink(p)['replayed'] is True  # the same intent: the terminal outcome, nothing else
    assert unlink(p, 'synthetic-unlink-002')['deleted'] is False  # already absent: a successful no-op
    assert admin.execute("select count(*) from runtime.nexloop_action_claims where action_name='Peer.unlink' and claim->>'state'='terminal'").fetchone()[0] == 2


def test_a_link_action_cannot_unlink(peers, admin):
    with pytest.raises(ActionAuthorizationDenied):
        unlink(peers, action_name='Peer.link')
    assert count(admin) == 1


def test_without_the_delete_authority_nothing_is_removed(peers, admin):
    p = peers
    replace_fact(admin, TENANT_A, 'grants', [f'{TENANT_A}-unlinker-principal', 'eios:relation:Peer'], F.GrantFacts, grants=[])
    p['unlinker'].session = authenticate_service(p['unlinker'].pool, p['token'], world='real')
    with pytest.raises((AuthorizationUnavailable, ActionAuthorizationDenied, psycopg.errors.InsufficientPrivilege)):
        unlink(p, 'synthetic-unlink-no-delete')
    assert count(admin) == 1


def test_a_revoked_endpoint_refuses_the_unlink(peers, admin):
    p = peers
    replace_fact(admin, TENANT_A, 'grants', [f'{TENANT_A}-unlinker-principal', 'eios:object:Consumer/' + p['ids'][1]], F.GrantFacts, grants=[])
    p['unlinker'].session = authenticate_service(p['unlinker'].pool, p['token'], world='real')
    with pytest.raises((AuthorizationUnavailable, ActionAuthorizationDenied, psycopg.errors.InsufficientPrivilege)):
        unlink(p, 'synthetic-unlink-revoked')
    assert count(admin) == 1


def test_the_entry_refuses_a_forged_signature(peers, admin):
    with psycopg.connect(admin.info.dsn, autocommit=True) as db:
        db.execute('set role nexloop_domain_worker')
        with pytest.raises(psycopg.Error):
            db.execute("select authz.nexloop_unlink_relation_action(%s,'real',%s,%s,%s)", ('0' * 64, '{"key_id":"x"}', '0' * 64, '{}'))
    assert count(admin) == 1
