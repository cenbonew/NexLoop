"""Claim → Consumer phase three, the profile read (migration workbench_property_sources; contract workbench-consumer-property-sources;
design claim-consumer-delivery-windows v2 §4.3, L6 C3): GET /api/v1/workbench/consumers/{id} carries property_sources - for each
structured Consumer property the member may read, its value, profile text, source Claim, stated_at (source Message time) and
effective_at (applied time, display only), status unknown / applied / corrected. Identifiers only, never the quote. A property in an
owner-restricted group is not listed. Fixture and Claims: tests/test_k5_evidence_views_pg.py."""
import json
import uuid

import jsonschema
from psycopg.types.json import Jsonb

from eios.ontology.models import PropertyTypeDescriptor
from nexloop_eios.delivery_windows import DESCRIPTOR
from test_k5_evidence_views_pg import QUOTE, claim, contract
from test_workbench_read_pg import (TENANT, browser_business, conversations, identity, provision, published_action, uow,  # noqa: F401
    workbench, workbench_login)

VALUE = [{'weekday': 'fri', 'part': 'daytime'}, {'weekday': 'sat', 'part': 'evening'}]


def structured_consumer(w):
    """Consumer version 97: favorite_sport and delivery_windows (group preference); the fixture's Consumer moved to it."""
    admin, consumer = w['admin'], w['f']['consumer']
    definition = {'type_name': 'Consumer', 'version': 97, 'properties': [{'property_name': 'favorite_sport', 'value_type': 'string'},
        {'property_name': 'delivery_windows', 'value_type': 'json', 'nullable': True,
         'type_descriptor': PropertyTypeDescriptor.model_validate(DESCRIPTOR).model_dump(mode='json')}],
        'property_groups': [{'group_name': 'preference', 'property_names': ['favorite_sport', 'delivery_windows']}]}
    admin.execute("insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,'Consumer',97,%s)", (TENANT, Jsonb(definition)))
    admin.execute("update ontology.objects set schema_version=97,properties='{}' where tenant_id=%s and type_name='Consumer' and object_id=%s", (TENANT, consumer))


def applied(w, claim_id, value):
    """What the matcher leaves after applying (tests/test_schema_structured_property_pg.py drives the real path)."""
    admin, consumer = w['admin'], w['f']['consumer']
    pid = str(uuid.uuid4())
    admin.execute("""insert into ontology.nexloop_mutation_proposals(tenant_id,world,proposal_id,business_intent_ref,claim_id,outcome,status,op,type_name,
        target_ref,property_name,effective_at,proposal,receipt,reason) values(%s,'real',%s,%s,%s,'partial_match','applied','set_property','Consumer',%s,
        'delivery_windows',now(),%s,'{"applied":true}','')""",
        (TENANT, pid, 'nx020:' + pid.replace('-', '') * 2, claim_id, 'eios:object:Consumer/' + consumer,
         Jsonb({'operations': [{'op': 'set_property', 'property_name': 'delivery_windows', 'value': value}]})))
    admin.execute("update ontology.objects set properties=properties||%s where tenant_id=%s and type_name='Consumer' and object_id=%s",
                  (Jsonb({'delivery_windows': value}), TENANT, consumer))


def sources(client, consumer):
    got = client.get(f'/api/v1/workbench/consumers/{consumer}')
    assert got.status_code == 200, got.text
    block = got.json()['property_sources']
    jsonschema.validate(block, contract('workbench-consumer-property-sources'))
    return block


def test_unknown_then_applied_then_corrected(workbench):
    w = workbench
    provision(w)
    consumer = w['f']['consumer']
    structured_consumer(w)
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        block = sources(client, consumer)
        assert block['status'] == 'ok' and block['items'] == [{'property_name': 'delivery_windows', 'value': None, 'display': None, 'status': 'unknown',
                                                               'source': None, 'stated_at': None, 'effective_at': None}]
        said = claim(w, 'delivery', predicate='送货时间', kind='preference', resolution='resolved')
        applied(w, said, VALUE)
        item = sources(client, consumer)['items'][0]
        anchor = w['admin'].execute("select valid_time->>'anchor' from ontology.nexloop_claims where claim_id=%s", (said,)).fetchone()[0]
        assert item['status'] == 'applied' and item['value'] == VALUE and item['display'] == '周五 白天；周六 晚上'
        assert item['source'] == {'claim_id': said, 'conversation_id': w['conversation']['id'], 'message_id': w['refusal']['id']}
        assert item['stated_at'] == anchor and item['effective_at'] is not None
        assert QUOTE not in json.dumps(block, ensure_ascii=False) and QUOTE not in json.dumps(item, ensure_ascii=False)
        # Withdrawn through the correction chain: no value, an applied history.
        w['admin'].execute("update ontology.objects set properties=properties-'delivery_windows' where tenant_id=%s and object_id=%s", (TENANT, consumer))
        item = sources(client, consumer)['items'][0]
        assert (item['status'], item['value'], item['source'], item['stated_at']) == ('corrected', None, None, None)


def test_operator_reads_it_and_a_restricted_group_is_not_listed(workbench):
    w = workbench
    provision(w)
    consumer = w['f']['consumer']
    structured_consumer(w)
    applied(w, claim(w, 'delivery-op', predicate='送货时间', resolution='resolved'), VALUE)
    with w['client']() as client:
        assert workbench_login(client, w['operator']).status_code == 200
        assert [i['status'] for i in sources(client, consumer)['items']] == ['applied']
    w['admin'].execute('insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) values(%s,%s,%s,%s)',
        (TENANT, 'property_group_restriction', ['Consumer'], Jsonb({'tenant_id': TENANT, 'type_name': 'Consumer', 'restricted_groups': ['preference'],
                                                                   'decision': 'owner 2026-11-03 (synthetic)'})))
    with w['client']() as client:
        assert workbench_login(client, w['operator']).status_code == 200
        block = sources(client, consumer)
        assert block['items'] == [] and 'fri' not in json.dumps(block)
