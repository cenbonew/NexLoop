from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel

from .models import (
    assert_descriptor_compatible,
    EventTypeDefinition,
    ObjectTypeDefinition,
    PropertyDefinition,
    RelationTypeDefinition,
)


class OntologyRegistryError(RuntimeError):
    code = "ontology_registry_error"


class SchemaCompatibilityError(OntologyRegistryError):
    code = "schema_incompatible"


# Fields added to the schema models after their digests were first frozen, with
# the JSON-mode dump of each field default. A payload entry equal to its
# default is dropped before hashing, so digests computed before the field
# existed keep matching: idempotent re-registration replays across upgrades and
# schema_revision stays stable for tenants that don't use the new metadata.
_ADDITIVE_PROPERTY_DEFAULTS: dict[str, object] = {
    "display_name": "",
    "render_hint": "plain",
    "visibility": "normal",
    "type_descriptor": None,
}
_ADDITIVE_OBJECT_TYPE_DEFAULTS: dict[str, object] = {
    "title_property": "",
    "icon": "",
    "color": "",
    "plural_display_name": "",
    "property_groups": [],
    "primary_key": [],
    "derived_properties": [],
    "only_edit_via_actions": False,
}


def _prune_defaults(payload: dict[str, Any], defaults: dict[str, object]) -> None:
    for name, default in defaults.items():
        if name in payload and payload[name] == default:
            del payload[name]


def _prune_additive_defaults(definition: BaseModel, payload: dict[str, Any]) -> None:
    """Drop additive fields still at their defaults (digest stability)."""

    if isinstance(definition, ObjectTypeDefinition):
        _prune_defaults(payload, _ADDITIVE_OBJECT_TYPE_DEFAULTS)
        for item in payload.get("properties", ()):
            _prune_defaults(item, _ADDITIVE_PROPERTY_DEFAULTS)
    elif isinstance(definition, EventTypeDefinition):
        for item in payload.get("payload_properties", ()):
            _prune_defaults(item, _ADDITIVE_PROPERTY_DEFAULTS)
    elif isinstance(definition, RelationTypeDefinition):
        # Stage 6: typed link properties are additive on relation types.
        _prune_defaults(payload, {"properties": []})
        for item in payload.get("properties", ()):
            _prune_defaults(item, _ADDITIVE_PROPERTY_DEFAULTS)


def definition_digest(definition: BaseModel, *, allow_breaking: bool) -> str:
    """Return the storage-independent idempotency digest for a schema write."""

    payload = definition.model_dump(mode="json", exclude={"version"})
    _prune_additive_defaults(definition, payload)
    payload["allow_breaking"] = allow_breaking
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _revision_payload(item: BaseModel) -> dict[str, Any]:
    payload = item.model_dump(mode="json")
    _prune_additive_defaults(item, payload)
    return payload


def schema_revision(
    object_types: Sequence[ObjectTypeDefinition],
    relation_types: Sequence[RelationTypeDefinition],
    event_types: Sequence[EventTypeDefinition],
) -> str:
    payload = {
        "object_types": [_revision_payload(item) for item in object_types],
        "relation_types": [_revision_payload(item) for item in relation_types],
        "event_types": [_revision_payload(item) for item in event_types],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def schema_contract_digest(definition: BaseModel) -> str:
    """Stable SHA-256 identity of one exact schema version.

    This is the value carried by ``OntologySchemaReference.schema_digest`` and
    verified by the schema-contract resolver, so Definitions pin the exact
    schema they were authored against. Unlike ``definition_digest`` it includes
    ``version``; additive display metadata at defaults is pruned so identities
    published before those fields existed keep matching.
    """

    payload = definition.model_dump(mode="json")
    _prune_additive_defaults(definition, payload)
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def payload_digest(payload: dict[str, Any]) -> str:
    """Return the stable idempotency digest used by ontology instance writes."""

    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def natural_object_id(
    definition: ObjectTypeDefinition, properties: dict[str, Any]
) -> str | None:
    """Deterministic object_id derived from the declared natural key.

    Returns None when the type declares no primary_key. Missing key values
    fail closed — an entity without its business key cannot be ingested.
    """

    if not definition.primary_key:
        return None
    values = []
    for name in definition.primary_key:
        if name not in properties or properties[name] is None:
            raise OntologyRegistryError(
                f"primary_key property is required for ingest: {name}"
            )
        values.append(properties[name])
    return natural_object_id_from_key(definition.type_name, values)


def natural_object_id_from_key(
    type_name: str, values: list[Any] | tuple[Any, ...]
) -> str:
    """Derive the canonical object identity from an already resolved natural key."""

    if (
        type(type_name) is not str
        or not type_name
        or type_name != type_name.strip()
        or not isinstance(values, (list, tuple))
        or not values
        or any(value is None for value in values)
    ):
        raise OntologyRegistryError("natural object key is invalid")
    encoded = json.dumps(
        {"type": type_name, "key": list(values)},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return "nk_" + hashlib.sha256(encoded).hexdigest()[:40]


def require_idempotency_key(value: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise ValueError("idempotency_key is required")
    return clean


def assert_properties_compatible(
    current_map: dict[str, PropertyDefinition],
    candidate_map: dict[str, PropertyDefinition],
) -> None:
    removed_required = sorted(
        name
        for name, definition in current_map.items()
        if definition.required and name not in candidate_map
    )
    if removed_required:
        raise SchemaCompatibilityError(
            f"required property cannot be removed: {removed_required[0]}"
        )
    for name in sorted(set(current_map) & set(candidate_map)):
        if current_map[name].value_type != candidate_map[name].value_type:
            raise SchemaCompatibilityError(f"property type cannot change: {name}")
        current_descriptor = current_map[name].type_descriptor
        candidate_descriptor = candidate_map[name].type_descriptor
        if current_descriptor is None and candidate_descriptor is None:
            continue
        if (current_descriptor is None) != (candidate_descriptor is None):
            # Attaching a descriptor to a legacy scalar is compatible only
            # when it restates the same scalar kind; anything else (or
            # dropping a composite descriptor) changes the effective type.
            attached = candidate_descriptor or current_descriptor
            if (
                attached is None
                or attached.kind.value != current_map[name].value_type.value
            ):
                raise SchemaCompatibilityError(f"property type cannot change: {name}")
            continue
        try:
            assert_descriptor_compatible(
                current_descriptor, candidate_descriptor, path=name
            )
        except ValueError as error:
            raise SchemaCompatibilityError(str(error)) from None


def assert_object_compatible(
    current: ObjectTypeDefinition,
    candidate: ObjectTypeDefinition,
) -> None:
    if current.primary_key and current.primary_key != candidate.primary_key:
        raise SchemaCompatibilityError("primary_key cannot change")
    assert_properties_compatible(current.property_map(), candidate.property_map())


def assert_relation_compatible(
    current: RelationTypeDefinition,
    candidate: RelationTypeDefinition,
) -> None:
    if (
        current.source_type != candidate.source_type
        or current.target_type != candidate.target_type
        or current.cardinality != candidate.cardinality
    ):
        raise SchemaCompatibilityError(
            "relation endpoints or cardinality cannot change"
        )
    assert_properties_compatible(current.property_map(), candidate.property_map())


def assert_event_compatible(
    current: EventTypeDefinition,
    candidate: EventTypeDefinition,
) -> None:
    if current.object_type != candidate.object_type:
        raise SchemaCompatibilityError("event object type cannot change")
    assert_properties_compatible(
        {item.property_name: item for item in current.payload_properties},
        {item.property_name: item for item in candidate.payload_properties},
    )
