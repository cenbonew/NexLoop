from __future__ import annotations

import threading

from .models import (
    EventTypeDefinition,
    ObjectTypeDefinition,
    OntologySchemaSnapshot,
    RelationTypeDefinition,
)
from .semantics import (
    OntologyRegistryError,
    SchemaCompatibilityError as SchemaCompatibilityError,
    assert_event_compatible,
    assert_object_compatible,
    assert_relation_compatible,
    definition_digest,
    schema_revision,
)


class SchemaNotFound(OntologyRegistryError):
    code = "schema_not_found"


class RegistryIdempotencyConflict(OntologyRegistryError):
    code = "idempotency_conflict"


class InMemoryOntologyRegistry:
    def __init__(self) -> None:
        self._object_types: dict[tuple[str, str], ObjectTypeDefinition] = {}
        self._relation_types: dict[tuple[str, str], RelationTypeDefinition] = {}
        self._event_types: dict[tuple[str, str], EventTypeDefinition] = {}
        self._idempotency: dict[tuple[str, str, str], tuple[str, object]] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _tenant(tenant_id: str) -> str:
        clean = str(tenant_id or "").strip()
        if not clean:
            raise ValueError("tenant_id is required")
        return clean

    def register_object_type(
        self,
        tenant_id: str,
        definition: ObjectTypeDefinition,
        *,
        allow_breaking: bool = False,
        idempotency_key: str | None = None,
    ) -> ObjectTypeDefinition:
        tenant = self._tenant(tenant_id)
        key = (tenant, definition.type_name)
        with self._lock:
            digest = definition_digest(definition, allow_breaking=allow_breaking)
            reused = self._idempotent_result(
                tenant,
                "object_type.register",
                idempotency_key,
                digest,
            )
            if reused is not None:
                return ObjectTypeDefinition.model_validate(reused)
            current = self._object_types.get(key)
            if current is not None and not allow_breaking:
                assert_object_compatible(current, definition)
            version = 1 if current is None else current.version + 1
            stored = definition.model_copy(update={"version": version}, deep=True)
            self._object_types[key] = stored
            self._remember_idempotency(
                tenant,
                "object_type.register",
                idempotency_key,
                digest,
                stored,
            )
            return stored.model_copy(deep=True)

    def get_object_type(self, tenant_id: str, type_name: str) -> ObjectTypeDefinition:
        key = (self._tenant(tenant_id), str(type_name))
        with self._lock:
            item = self._object_types.get(key)
            if item is None:
                raise SchemaNotFound(f"object type not found: {type_name}")
            return item.model_copy(deep=True)

    def register_relation_type(
        self,
        tenant_id: str,
        definition: RelationTypeDefinition,
        *,
        allow_breaking: bool = False,
        idempotency_key: str | None = None,
    ) -> RelationTypeDefinition:
        tenant = self._tenant(tenant_id)
        self.get_object_type(tenant, definition.source_type)
        self.get_object_type(tenant, definition.target_type)
        key = (tenant, definition.relation_name)
        with self._lock:
            digest = definition_digest(definition, allow_breaking=allow_breaking)
            reused = self._idempotent_result(
                tenant,
                "relation_type.register",
                idempotency_key,
                digest,
            )
            if reused is not None:
                return RelationTypeDefinition.model_validate(reused)
            current = self._relation_types.get(key)
            if current and not allow_breaking:
                assert_relation_compatible(current, definition)
            stored = definition.model_copy(update={"version": 1 if current is None else current.version + 1}, deep=True)
            self._relation_types[key] = stored
            self._remember_idempotency(
                tenant,
                "relation_type.register",
                idempotency_key,
                digest,
                stored,
            )
            return stored.model_copy(deep=True)

    def get_relation_type(self, tenant_id: str, relation_name: str) -> RelationTypeDefinition:
        key = (self._tenant(tenant_id), str(relation_name))
        with self._lock:
            item = self._relation_types.get(key)
            if item is None:
                raise SchemaNotFound(f"relation type not found: {relation_name}")
            return item.model_copy(deep=True)

    def register_event_type(
        self,
        tenant_id: str,
        definition: EventTypeDefinition,
        *,
        allow_breaking: bool = False,
        idempotency_key: str | None = None,
    ) -> EventTypeDefinition:
        tenant = self._tenant(tenant_id)
        self.get_object_type(tenant, definition.object_type)
        key = (tenant, definition.event_name)
        with self._lock:
            digest = definition_digest(definition, allow_breaking=allow_breaking)
            reused = self._idempotent_result(
                tenant,
                "event_type.register",
                idempotency_key,
                digest,
            )
            if reused is not None:
                return EventTypeDefinition.model_validate(reused)
            current = self._event_types.get(key)
            if current and not allow_breaking:
                assert_event_compatible(current, definition)
            stored = definition.model_copy(update={"version": 1 if current is None else current.version + 1}, deep=True)
            self._event_types[key] = stored
            self._remember_idempotency(
                tenant,
                "event_type.register",
                idempotency_key,
                digest,
                stored,
            )
            return stored.model_copy(deep=True)

    def _idempotent_result(
        self,
        tenant_id: str,
        operation: str,
        idempotency_key: str | None,
        digest: str,
    ):
        clean = str(idempotency_key or "").strip()
        if not clean:
            return None
        existing = self._idempotency.get((tenant_id, operation, clean))
        if existing is None:
            return None
        existing_digest, result = existing
        if existing_digest != digest:
            raise RegistryIdempotencyConflict(
                f"idempotency key reused with different schema payload: {clean}"
            )
        return result.model_copy(deep=True)

    def _remember_idempotency(
        self,
        tenant_id: str,
        operation: str,
        idempotency_key: str | None,
        digest: str,
        result,
    ) -> None:
        clean = str(idempotency_key or "").strip()
        if clean:
            self._idempotency[(tenant_id, operation, clean)] = (digest, result.model_copy(deep=True))

    def get_event_type(self, tenant_id: str, event_name: str) -> EventTypeDefinition:
        key = (self._tenant(tenant_id), str(event_name))
        with self._lock:
            item = self._event_types.get(key)
            if item is None:
                raise SchemaNotFound(f"event type not found: {event_name}")
            return item.model_copy(deep=True)

    def schema_snapshot(self, tenant_id: str) -> OntologySchemaSnapshot:
        tenant = self._tenant(tenant_id)
        with self._lock:
            objects = tuple(
                value.model_copy(deep=True)
                for (item_tenant, _), value in sorted(self._object_types.items())
                if item_tenant == tenant
            )
            relations = tuple(
                value.model_copy(deep=True)
                for (item_tenant, _), value in sorted(self._relation_types.items())
                if item_tenant == tenant
            )
            events = tuple(
                value.model_copy(deep=True)
                for (item_tenant, _), value in sorted(self._event_types.items())
                if item_tenant == tenant
            )
        revision = schema_revision(objects, relations, events)
        return OntologySchemaSnapshot(
            tenant_id=tenant,
            object_types=objects,
            relation_types=relations,
            event_types=events,
            revision=revision,
        )
