"""EIOS gap A1 (NX-061): nexloop_eios.schema_impact over the vendored dependency graph, without PostgreSQL (the SQL rows and the
HTTP check are tests/test_schema_publication_http_pg.py)."""
from datetime import UTC, datetime

from eios.ontology.definitions import (ActionDefinition, DefinitionStatus, ObjectSetDefinition, OntologySchemaReference,
                                       OntologySchemaType, PropertyReference)
from nexloop_eios.schema_impact import changed_properties, impact

TENANT = 'tenant-synthetic'
NOW = datetime(2026, 10, 22, tzinfo=UTC)


def ref(name, version):
    return OntologySchemaReference(tenant_id=TENANT, schema_type=OntologySchemaType.OBJECT_TYPE, stable_name=name, version=version,
                                   schema_digest='0' * 64)


def object_set(name, type_name, version, prop):
    return ObjectSetDefinition(tenant_id=TENANT, stable_name=name, version=1, status=DefinitionStatus.PUBLISHED, created_by='synthetic',
                               created_at=NOW, object_type=ref(type_name, version),
                               projection=(PropertyReference(object_type=ref(type_name, version), property_name=prop),)
                               ).model_dump(mode='json')


def dependents(*sets, operation='add_vocabulary_value', change=None, actions=()):
    return {'type_name': 'Consumer', 'operation': operation, 'change': change or {'property_name': 'level', 'value': '5'},
            'truncated': False, 'object_sets': list(sets), 'object_views': [], 'functions': [], 'actions': list(actions)}


def test_changed_properties_from_both_change_shapes():
    assert changed_properties('add_optional_property', {'property_name': 'p'}) == ['p']
    assert changed_properties('add_optional_property', {'property': {'property_name': 'p'}}) == ['p']
    assert changed_properties('add_vocabulary_value', {'property_name': 'level', 'value': 'x'}) == ['level']
    assert changed_properties('add_object_type', {'definition': {}}) == [] and changed_properties(None, None) == []


def test_type_and_property_dependents_any_pinned_version_and_not_a_prefix_sharing_type():
    out = impact(dependents(object_set('by.level', 'Consumer', 3, 'level'), object_set('by.sport', 'Consumer', 4, 'sport'),
                            object_set('other.level', 'ConsumerArchive', 1, 'level')))
    got = {d['stable_name']: d for d in out['dependents']}
    assert set(got) == {'by.level', 'by.sport'}  # ConsumerArchive shares the prefix only
    assert got['by.level']['changed_properties'] == ['level'] and got['by.level']['pinned_type_version'] == 3
    assert got['by.sport']['changed_properties'] == [] and got['by.sport']['pinned_type_version'] == 4
    assert out['affected_by_changed_properties'] == 1 and out['dependents'][0]['stable_name'] == 'by.level'


def test_an_unparsable_row_is_listed_never_dropped_and_an_unknown_proposal_is_none():
    out = impact(dependents({'stable_name': 'broken.set', 'definition_type': 'object_set'}, object_set('by.level', 'Consumer', 3, 'level')))
    assert out['unreadable'] == [{'kind': 'object_sets', 'stable_name': 'broken.set'}] and len(out['dependents']) == 1
    assert impact(None) is None
