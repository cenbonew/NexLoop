"""Versioned NexLoop system metadata object types (deploy/ontology/system-object-types.v*.json).

These are the object types NexLoop itself needs as typed, digest-bound Action change scopes
(e.g. ContextStrategy for nexloop.context.strategy.publish:1). They are published at
deployment through the same trusted configuration path (0050) as business Actions: never
written to schema tables directly and never part of any service grant.
"""
import json
from pathlib import Path
import re

from eios.ontology.models import ObjectTypeDefinition

SCHEMA='nexloop-system-object-types/1'
TOP={'schema_version','manifest_version','decision','object_types'}
ENTRY={'type_name','version','purpose','source_task','definition'}


class SystemObjectTypesRejected(ValueError):
    pass


def validate(value):
    if type(value) is not dict or set(value)!=TOP or value['schema_version']!=SCHEMA or type(value['manifest_version']) is not int or value['manifest_version']<1:
        raise SystemObjectTypesRejected('manifest shape')
    if type(value['object_types']) is not list or not value['object_types']:raise SystemObjectTypesRejected('object_types')
    seen=set();out=[]
    for item in value['object_types']:
        if type(item) is not dict or set(item)!=ENTRY:raise SystemObjectTypesRejected('entry shape')
        if not re.fullmatch('[A-Z][A-Za-z0-9_]{0,63}',str(item['type_name'])) or type(item['version']) is not int or item['version']<1:
            raise SystemObjectTypesRejected('type identity')
        for name in ('purpose','source_task'):
            if type(item[name]) is not str or not item[name].strip():raise SystemObjectTypesRejected(name)
        try:definition=ObjectTypeDefinition.model_validate_json(json.dumps(item['definition']))
        except Exception:raise SystemObjectTypesRejected('definition') from None
        if (definition.type_name,definition.version)!=(item['type_name'],item['version']) or not definition.only_edit_via_actions:
            raise SystemObjectTypesRejected('definition identity or edit policy')
        if (definition.type_name,definition.version) in seen:raise SystemObjectTypesRejected('duplicate type')
        seen.add((definition.type_name,definition.version));out.append(definition)
    return out


def load(path):
    return validate(json.loads(Path(path).read_text()))


def trusted_object_types(path):
    """Rows for the trusted-configuration manifest ``object_types`` list."""
    return [definition.model_dump(mode='json') for definition in load(path)]
