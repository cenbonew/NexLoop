from __future__ import annotations

from time import monotonic

from datetime import UTC, date, datetime
from typing import Any, Sequence, TypeVar
import uuid

from psycopg import errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from eios.kernel.ports.repositories import OntologyRegistry
from eios.ontology.models import (
    validate_relation_properties,
    EventTypeDefinition,
    ObjectTypeDefinition,
    OntologyValidationError,
    RelationCardinality,
    RelationTypeDefinition,
    validate_event_payload,
    validate_object_properties,
)
from eios.ontology.atomic_projection import (
    AtomicObjectUpsert,
    ObjectProjectionRevisionConflict,
    validate_atomic_projection_batch,
)
from eios.ontology.action_write_context import current_action_write
from eios.ontology.registry import OntologyRegistryError
from eios.ontology.semantics import (
    natural_object_id,
    payload_digest,
    require_idempotency_key,
)
from eios.ontology.store import (
    RelationNotFound,
    object_request_payload,
    EventRecord,
    IdempotencyConflict,
    object_request_digest_matches,
    ObjectNotFound,
    ObjectProjection,
    ObjectRecord,
    ObjectTombstone,
    ObjectWriteRequiresAction,
    ObjectSubgraph,
    OntologyStoreError,
    RelationCardinalityViolation,
    RelationEndpointMismatch,
    RelationRecord,
    SubgraphEdge,
    SubgraphNode,
)


RecordT = TypeVar("RecordT", ObjectRecord, RelationRecord, EventRecord)


class PostgresOntologyStore:
    """Tenant-isolated PostgreSQL persistence for ontology instances."""

    def __init__(self, pool: ConnectionPool, registry: OntologyRegistry) -> None:
        self._pool = pool
        self._registry = registry

    @staticmethod
    def _assert_object_write_authorized(
        tenant_id: str, schema: ObjectTypeDefinition
    ) -> None:
        if not schema.only_edit_via_actions:
            return
        context = current_action_write()
        if context is None or context.tenant_id != tenant_id:
            raise ObjectWriteRequiresAction(
                f"object type {schema.type_name} requires a valid Action execution permit"
            )

    @staticmethod
    def _tenant(tenant_id: str) -> str:
        clean = str(tenant_id or "").strip()
        if not clean:
            raise ValueError("tenant_id is required")
        return clean

    @staticmethod
    def _bind_tenant(connection: Any, tenant_id: str) -> None:
        connection.execute(
            "select set_config('eios.tenant_id', %s, true)",
            (tenant_id,),
        )

    @staticmethod
    def _lock(connection: Any, *scope: str) -> None:
        lock_key = ":".join(("ontology-store", *scope))
        connection.execute(
            "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (lock_key,),
        )

    @classmethod
    def _lock_idempotency(
        cls,
        connection: Any,
        tenant_id: str,
        operation: str,
        idempotency_key: str,
    ) -> None:
        cls._lock(
            connection,
            tenant_id,
            "idempotency",
            operation,
            idempotency_key,
        )

    @classmethod
    def _lock_object(
        cls,
        connection: Any,
        tenant_id: str,
        type_name: str,
        object_id: str,
    ) -> None:
        cls._lock(connection, tenant_id, "object", type_name, object_id)

    @classmethod
    def _lock_relation_constraints(
        cls,
        connection: Any,
        tenant_id: str,
        schema: RelationTypeDefinition,
        source_type: str,
        source_object_id: str,
        target_type: str,
        target_object_id: str,
    ) -> None:
        scopes = {
            (
                "relation",
                schema.relation_name,
                "pair",
                source_type,
                source_object_id,
                target_type,
                target_object_id,
            )
        }
        if schema.cardinality in {
            RelationCardinality.ONE_TO_ONE,
            RelationCardinality.MANY_TO_ONE,
        }:
            scopes.add(
                (
                    "relation",
                    schema.relation_name,
                    "source",
                    source_type,
                    source_object_id,
                )
            )
        if schema.cardinality in {
            RelationCardinality.ONE_TO_ONE,
            RelationCardinality.ONE_TO_MANY,
        }:
            scopes.add(
                (
                    "relation",
                    schema.relation_name,
                    "target",
                    target_type,
                    target_object_id,
                )
            )
        for scope in sorted(scopes):
            cls._lock(connection, tenant_id, *scope)

    @staticmethod
    def _idempotent_result(
        cursor: Any,
        *,
        tenant_id: str,
        operation: str,
        idempotency_key: str,
        request_digest: str,
        model: type[RecordT],
    ) -> RecordT | None:
        cursor.execute(
            """
            select request_digest, result
            from runtime.idempotency_records
            where tenant_id = %s and operation = %s and idempotency_key = %s
            """,
            (tenant_id, operation, idempotency_key),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        if row["request_digest"] != request_digest:
            raise IdempotencyConflict(
                f"idempotency key reused with different payload: {idempotency_key}"
            )
        return model.model_validate(row["result"])

    @staticmethod
    def _remember_idempotency(
        cursor: Any,
        *,
        tenant_id: str,
        operation: str,
        idempotency_key: str,
        request_digest: str,
        result_type: str,
        result: BaseModel,
    ) -> None:
        cursor.execute(
            "select ontology.app_idempotency_remember(%s, %s, %s, %s, %s, %s)",
            (
                tenant_id,
                operation,
                idempotency_key,
                request_digest,
                result_type,
                Jsonb(result.model_dump(mode="json")),
            ),
        )

    @staticmethod
    def _object_from_row(row: dict[str, Any]) -> ObjectRecord:
        return ObjectRecord.model_validate(row)

    @staticmethod
    def _relation_from_row(row: dict[str, Any]) -> RelationRecord:
        return RelationRecord.model_validate(row)

    @staticmethod
    def _object_schema(
        cursor: Any,
        tenant_id: str,
        type_name: str,
        schema_version: int,
    ) -> ObjectTypeDefinition:
        cursor.execute(
            """
            select definition
            from ontology.object_type_versions
            where tenant_id = %s and type_name = %s and version = %s
            """,
            (tenant_id, type_name, schema_version),
        )
        row = cursor.fetchone()
        if row is None:
            raise OntologyStoreError("object schema version not found")
        return ObjectTypeDefinition.model_validate(row["definition"])

    @classmethod
    def _normalize_object_record(
        cls,
        cursor: Any,
        record: ObjectRecord,
    ) -> tuple[ObjectRecord, ObjectTypeDefinition]:
        schema = cls._object_schema(
            cursor,
            record.tenant_id,
            record.type_name,
            record.schema_version,
        )
        normalized = record.model_copy(
            update={
                "properties": validate_object_properties(
                    schema,
                    record.properties,
                )
            },
            deep=True,
        )
        return normalized, schema

    @staticmethod
    def _normalize_joined_object_row(row: dict[str, Any]) -> ObjectRecord:
        record = ObjectRecord.model_validate(row)
        definition = row.get("object_definition")
        if definition is None:
            raise OntologyStoreError("object schema version not found")
        schema = ObjectTypeDefinition.model_validate(definition)
        return record.model_copy(
            update={
                "properties": validate_object_properties(
                    schema,
                    record.properties,
                )
            },
            deep=True,
        )

    @classmethod
    def _object_request_digest(
        cls,
        *,
        type_name: str,
        object_id: str | None,
        properties: dict[str, Any],
        source_system: str,
        source_ref: str,
        schema: ObjectTypeDefinition,
        markings: tuple[str, ...] = (),
    ) -> str:
        """请求指纹。**与内存 store 的 request_payload 键集合逐字段相等**
        —— 两边不一致的话,同一个"补 marking"操作会在内存里报冲突、在真库
        里静默通过(或反过来),而判据只跑得到其中一边。"""

        return payload_digest(
            cls._object_request_payload(
                type_name=type_name,
                object_id=object_id,
                properties=properties,
                source_system=source_system,
                source_ref=source_ref,
                schema=schema,
                markings=markings,
            )
        )

    @classmethod
    def _object_request_payload(
        cls,
        *,
        type_name: str,
        object_id: str | None,
        properties: dict[str, Any],
        source_system: str,
        source_ref: str,
        schema: ObjectTypeDefinition,
        markings: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """载荷本身。读侧要拿它做**双口径**判定,不能只拿到算好的摘要。"""

        normalized = validate_object_properties(schema, properties, partial=True)
        return object_request_payload(
            type_name=type_name,
            object_id=object_id,
            properties=normalized,
            source_system=source_system,
            source_ref=source_ref,
            markings=markings,
        )

    @classmethod
    def _object_idempotent_result(
        cls,
        cursor: Any,
        *,
        tenant_id: str,
        operation: str,
        idempotency_key: str,
        type_name: str,
        object_id: str | None,
        properties: dict[str, Any],
        source_system: str,
        source_ref: str,
        markings: tuple[str, ...] = (),
    ) -> ObjectRecord | None:
        cursor.execute(
            """
            select request_digest, result
            from runtime.idempotency_records
            where tenant_id = %s and operation = %s and idempotency_key = %s
            """,
            (tenant_id, operation, idempotency_key),
        )
        idempotency = cursor.fetchone()
        if idempotency is None:
            return None
        recorded = ObjectRecord.model_validate(idempotency["result"])
        historical_schema = cls._object_schema(
            cursor,
            recorded.tenant_id,
            recorded.type_name,
            recorded.schema_version,
        )
        request_payload = cls._object_request_payload(
            type_name=type_name,
            object_id=object_id,
            properties=properties,
            source_system=source_system,
            source_ref=source_ref,
            schema=historical_schema,
            markings=markings,
        )
        # 读侧走双口径:`3bfe7b70`(markings 进指纹)之前写的记录用旧口径,
        # 同键算出不同摘要 ⇒ 永久 IdempotencyConflict。详见
        # `object_request_digest_matches`(带 markings 非空不回退的硬限制)。
        if not object_request_digest_matches(
            idempotency["request_digest"], request_payload
        ):
            raise IdempotencyConflict(
                f"idempotency key reused with different payload: {idempotency_key}"
            )
        normalized = recorded.model_copy(
            update={
                "properties": validate_object_properties(
                    historical_schema,
                    recorded.properties,
                )
            },
            deep=True,
        )
        return normalized

    @staticmethod
    def _normalize_event_record(row: dict[str, Any]) -> EventRecord:
        record = EventRecord.model_validate(row)
        schema_version = row.get("schema_version")
        if schema_version is None:
            return record
        if record.event_type in {"ObjectCreated", "ObjectUpdated"}:
            definition = row.get("object_definition")
            if definition is None:
                raise OntologyStoreError("object event schema version not found")
            if record.event_type == "ObjectUpdated":
                return record
            schema = ObjectTypeDefinition.model_validate(definition)
            properties = record.payload.get("properties")
            if not isinstance(properties, dict):
                raise OntologyStoreError("object event properties are invalid")
            return record.model_copy(
                update={
                    "payload": {
                        **record.payload,
                        "properties": validate_object_properties(
                            schema,
                            properties,
                        ),
                    }
                },
                deep=True,
            )
        if record.event_type in ("RelationLinked", "RelationUnlinked"):
            definition = row.get("relation_definition")
            if definition is None:
                raise OntologyStoreError("relation event schema version not found")
            schema = RelationTypeDefinition.model_validate(definition)
            if record.payload.get("relation_name") != schema.relation_name:
                raise OntologyStoreError("relation event schema does not match payload")
            required_payload = ("relation_id", "relation_name", "source", "target")
            if any(
                not isinstance(record.payload.get(field), str)
                or not record.payload[field]
                for field in required_payload
            ):
                raise OntologyStoreError("relation event payload is invalid")
            return record
        definition = row.get("event_definition")
        if definition is None:
            raise OntologyStoreError("event schema version not found")
        schema = EventTypeDefinition.model_validate(definition)
        return record.model_copy(
            update={"payload": validate_event_payload(schema, record.payload)},
            deep=True,
        )

    def _event_idempotent_result(
        self,
        cursor: Any,
        *,
        tenant_id: str,
        operation: str,
        idempotency_key: str,
        event_type: str,
        object_type: str,
        object_id: str,
        payload: dict[str, Any],
    ) -> EventRecord | None:
        cursor.execute(
            """
            select request_digest, result
            from runtime.idempotency_records
            where tenant_id = %s and operation = %s and idempotency_key = %s
            """,
            (tenant_id, operation, idempotency_key),
        )
        idempotency = cursor.fetchone()
        if idempotency is None:
            return None
        recorded = EventRecord.model_validate(idempotency["result"])
        cursor.execute(
            """
            select event.tenant_id, event.event_id, event.event_type,
                   event.object_type, event.object_id, event.payload,
                   event.actor_id, event.sequence, event.created_at,
                   event.schema_version,
                   schema.definition as event_definition
            from ontology.events as event
            left join ontology.event_type_versions as schema
              on schema.tenant_id = %s
             and schema.tenant_id = event.tenant_id
             and schema.event_name = event.event_type
             and schema.version = event.schema_version
            where event.tenant_id = %s and event.event_id = %s
            """,
            (tenant_id, tenant_id, recorded.event_id),
        )
        event_row = cursor.fetchone()
        if event_row is None:
            raise OntologyStoreError("idempotent event result not found")
        schema_version = event_row.get("schema_version")
        if schema_version is None:
            normalized = payload
        else:
            definition = event_row.get("event_definition")
            if definition is None:
                raise OntologyStoreError("event schema version not found")
            historical_schema = EventTypeDefinition.model_validate(definition)
            normalized = validate_event_payload(historical_schema, payload)
        request_digest = payload_digest(
            {
                "event_type": event_type,
                "object_type": object_type,
                "object_id": object_id,
                "payload": normalized,
            }
        )
        if idempotency["request_digest"] != request_digest:
            raise IdempotencyConflict(
                f"idempotency key reused with different payload: {idempotency_key}"
            )
        return self._normalize_event_record(event_row)

    @staticmethod
    def _select_object(
        cursor: Any,
        tenant_id: str,
        type_name: str,
        object_id: str,
    ) -> dict[str, Any] | None:
        cursor.execute(
            """
            select tenant_id, type_name, object_id, schema_version, properties,
                   source_system, source_ref, created_at, updated_at, markings
            from ontology.objects
            where tenant_id = %s and type_name = %s and object_id = %s
            """,
            (tenant_id, type_name, object_id),
        )
        return cursor.fetchone()

    @classmethod
    def _require_object(
        cls,
        cursor: Any,
        tenant_id: str,
        type_name: str,
        object_id: str,
    ) -> ObjectRecord:
        row = cls._select_object(cursor, tenant_id, type_name, object_id)
        if row is None:
            raise ObjectNotFound(f"object not found: {type_name}/{object_id}")
        return cls._object_from_row(row)

    @staticmethod
    def _raise_database_error(
        exception: Exception,
        *,
        operation: str,
    ) -> None:
        if isinstance(exception, errors.ForeignKeyViolation):
            raise ObjectNotFound("object not found") from None
        if isinstance(exception, errors.UniqueViolation):
            if operation == "relation.link":
                raise RelationCardinalityViolation(
                    "relation uniqueness constraint violated"
                ) from None
            raise OntologyStoreError(
                "PostgreSQL ontology store uniqueness constraint violated"
            ) from None
        raise OntologyStoreError("PostgreSQL ontology store operation failed") from None

    def upsert_object(
        self,
        *,
        tenant_id: str,
        type_name: str,
        properties: dict[str, Any],
        idempotency_key: str,
        object_id: str | None = None,
        source_system: str = "",
        source_ref: str = "",
        actor_id: str = "",
        markings: tuple[str, ...] = (),
    ) -> ObjectRecord:
        tenant = self._tenant(tenant_id)
        key = require_idempotency_key(idempotency_key)
        schema = self._registry.get_object_type(tenant, type_name)
        self._assert_object_write_authorized(tenant, schema)
        normalized_markings = tuple(sorted(set(markings)))
        derived_id = natural_object_id(schema, properties)
        if derived_id is not None and object_id not in (None, derived_id):
            raise IdempotencyConflict(
                "object_id conflicts with the declared natural key"
            )
        requested_id = str(object_id) if object_id else derived_id
        operation = "object.upsert"
        try:
            with self._pool.connection() as connection, connection.transaction():
                self._bind_tenant(connection, tenant)
                self._lock_idempotency(connection, tenant, operation, key)
                with connection.cursor(row_factory=dict_row) as cursor:
                    reused = self._object_idempotent_result(
                        cursor,
                        tenant_id=tenant,
                        operation=operation,
                        idempotency_key=key,
                        type_name=type_name,
                        object_id=requested_id,
                        properties=properties,
                        source_system=source_system,
                        source_ref=source_ref,
                        markings=normalized_markings,
                    )
                    if reused is not None:
                        return reused
                    digest = self._object_request_digest(
                        type_name=type_name,
                        object_id=requested_id,
                        properties=properties,
                        source_system=source_system,
                        source_ref=source_ref,
                        schema=schema,
                        markings=normalized_markings,
                    )
                    resolved_id = requested_id or f"obj_{uuid.uuid4().hex}"
                    self._lock_object(connection, tenant, type_name, resolved_id)
                    current_row = self._select_object(
                        cursor,
                        tenant,
                        type_name,
                        resolved_id,
                    )
                    current = (
                        self._object_from_row(current_row)
                        if current_row is not None
                        else None
                    )
                    if current is not None:
                        current, _ = self._normalize_object_record(
                            cursor,
                            current,
                        )
                    if current is None:
                        normalized = validate_object_properties(schema, properties)
                        now = datetime.now(UTC)
                        stored = ObjectRecord(
                            tenant_id=tenant,
                            type_name=type_name,
                            object_id=resolved_id,
                            schema_version=schema.version,
                            properties=normalized,
                            source_system=source_system,
                            source_ref=source_ref,
                            created_at=now,
                            updated_at=now,
                            markings=normalized_markings,
                        )
                        event_type = "ObjectCreated"
                        event_payload = {"properties": normalized}
                    else:
                        merged = dict(current.properties)
                        merged.update(properties)
                        normalized = validate_object_properties(schema, merged)
                        changed_fields = sorted(
                            name
                            for name in properties
                            if current.properties.get(name) != normalized.get(name)
                        )
                        # 无实变的重放(merge 后与现存全等、来源/版本/标记均
                        # 未变)直接返回现状:不写行、不发 ObjectUpdated、不
                        # 记幂等——no-op 天然幂等,同键重试仍会落到这里。高频
                        # 采集把同一状态反复投影时,账本因此不随节拍增长;
                        # updated_at 语义相应为「内容最后变化时刻」。
                        if (
                            normalized == current.properties
                            and schema.version == current.schema_version
                            and source_system in ("", current.source_system)
                            and source_ref in ("", current.source_ref)
                            and (
                                not normalized_markings
                                or normalized_markings == current.markings
                            )
                        ):
                            return current.model_copy(deep=True)
                        stored = current.model_copy(
                            update={
                                "schema_version": schema.version,
                                "properties": normalized,
                                "source_system": source_system or current.source_system,
                                "source_ref": source_ref or current.source_ref,
                                "updated_at": datetime.now(UTC),
                            },
                            deep=True,
                        )
                        event_type = "ObjectUpdated"
                        event_payload = {"changed_fields": changed_fields}
                    cursor.execute(
                        "select ontology.app_object_upsert"
                        "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true)",
                        (
                            stored.tenant_id,
                            stored.type_name,
                            stored.object_id,
                            stored.schema_version,
                            Jsonb(stored.model_dump(mode="json")["properties"]),
                            stored.source_system,
                            stored.source_ref,
                            stored.created_at,
                            stored.updated_at,
                            list(stored.markings),
                        ),
                    )
                    self._append_event_record(
                        cursor,
                        tenant_id=tenant,
                        event_type=event_type,
                        object_type=type_name,
                        object_id=resolved_id,
                        payload=event_payload,
                        actor_id=actor_id,
                        schema_version=stored.schema_version,
                    )
                    self._remember_idempotency(
                        cursor,
                        tenant_id=tenant,
                        operation=operation,
                        idempotency_key=key,
                        request_digest=digest,
                        result_type="object",
                        result=stored,
                    )
                    return stored.model_copy(deep=True)
        except (
            OntologyStoreError,
            OntologyRegistryError,
            OntologyValidationError,
        ):
            raise
        except Exception as exception:
            self._raise_database_error(exception, operation=operation)
            raise AssertionError("unreachable")

    def upsert_objects_atomically(
        self,
        items: Sequence[AtomicObjectUpsert],
    ) -> tuple[ObjectRecord, ...]:
        batch = validate_atomic_projection_batch(items)
        tenant = self._tenant(batch[0].tenant_id)
        operation = "object.upsert"
        prepared: list[
            tuple[AtomicObjectUpsert, str, ObjectTypeDefinition, tuple[str, ...]]
        ] = []
        for item in batch:
            if self._tenant(item.tenant_id) != tenant:
                raise ValueError("atomic projection batch must belong to one tenant")
            key = require_idempotency_key(item.idempotency_key)
            schema = self._registry.get_object_type(tenant, item.type_name)
            self._assert_object_write_authorized(tenant, schema)
            derived_id = natural_object_id(schema, item.properties)
            if derived_id is not None and item.object_id != derived_id:
                raise IdempotencyConflict(
                    "object_id conflicts with the declared natural key"
                )
            prepared.append((item, key, schema, tuple(sorted(set(item.markings)))))

        try:
            with self._pool.connection() as connection, connection.transaction():
                self._bind_tenant(connection, tenant)
                for key in sorted({entry[1] for entry in prepared}):
                    self._lock_idempotency(
                        connection,
                        tenant,
                        operation,
                        key,
                    )
                for type_name, object_id in sorted(
                    {(entry[0].type_name, entry[0].object_id) for entry in prepared}
                ):
                    self._lock_object(
                        connection,
                        tenant,
                        type_name,
                        object_id,
                    )

                records: list[ObjectRecord] = []
                with connection.cursor(row_factory=dict_row) as cursor:
                    for item, key, schema, normalized_markings in prepared:
                        reused = self._object_idempotent_result(
                            cursor,
                            tenant_id=tenant,
                            operation=operation,
                            idempotency_key=key,
                            type_name=item.type_name,
                            object_id=item.object_id,
                            properties=item.properties,
                            source_system=item.source_system,
                            source_ref=item.source_ref,
                        )
                        if reused is not None:
                            records.append(reused)
                            continue

                        digest = self._object_request_digest(
                            type_name=item.type_name,
                            object_id=item.object_id,
                            properties=item.properties,
                            source_system=item.source_system,
                            source_ref=item.source_ref,
                            schema=schema,
                        )
                        current_row = self._select_object(
                            cursor,
                            tenant,
                            item.type_name,
                            item.object_id,
                        )
                        current = (
                            self._object_from_row(current_row)
                            if current_row is not None
                            else None
                        )
                        if current is not None:
                            current, _ = self._normalize_object_record(cursor, current)
                        self._check_projection_revision(item, current)

                        if current is None:
                            normalized = validate_object_properties(
                                schema,
                                item.properties,
                            )
                            now = datetime.now(UTC)
                            stored = ObjectRecord(
                                tenant_id=tenant,
                                type_name=item.type_name,
                                object_id=item.object_id,
                                schema_version=schema.version,
                                properties=normalized,
                                source_system=item.source_system,
                                source_ref=item.source_ref,
                                created_at=now,
                                updated_at=now,
                                markings=normalized_markings,
                            )
                            event_type = "ObjectCreated"
                            event_payload = {"properties": normalized}
                        else:
                            merged = dict(current.properties)
                            merged.update(item.properties)
                            normalized = validate_object_properties(schema, merged)
                            changed_fields = sorted(
                                name
                                for name in item.properties
                                if current.properties.get(name) != normalized.get(name)
                            )
                            # 与 upsert_object 相同的无实变短路:不写行、不发
                            # 事件、不记幂等(no-op 天然幂等)。
                            if (
                                normalized == current.properties
                                and schema.version == current.schema_version
                                and item.source_system
                                in ("", current.source_system)
                                and item.source_ref in ("", current.source_ref)
                                and (
                                    not normalized_markings
                                    or normalized_markings == current.markings
                                )
                            ):
                                records.append(current.model_copy(deep=True))
                                continue
                            stored = current.model_copy(
                                update={
                                    "schema_version": schema.version,
                                    "properties": normalized,
                                    "source_system": (
                                        item.source_system or current.source_system
                                    ),
                                    "source_ref": item.source_ref or current.source_ref,
                                    "updated_at": datetime.now(UTC),
                                    "markings": (
                                        normalized_markings
                                        if normalized_markings
                                        else current.markings
                                    ),
                                },
                                deep=True,
                            )
                            event_type = "ObjectUpdated"
                            event_payload = {"changed_fields": changed_fields}

                        cursor.execute(
                            "select ontology.app_object_upsert"
                            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, false)",
                            (
                                stored.tenant_id,
                                stored.type_name,
                                stored.object_id,
                                stored.schema_version,
                                Jsonb(stored.model_dump(mode="json")["properties"]),
                                stored.source_system,
                                stored.source_ref,
                                stored.created_at,
                                stored.updated_at,
                                list(stored.markings),
                            ),
                        )
                        self._append_event_record(
                            cursor,
                            tenant_id=tenant,
                            event_type=event_type,
                            object_type=item.type_name,
                            object_id=item.object_id,
                            payload=event_payload,
                            actor_id=item.actor_id,
                            schema_version=stored.schema_version,
                        )
                        self._remember_idempotency(
                            cursor,
                            tenant_id=tenant,
                            operation=operation,
                            idempotency_key=key,
                            request_digest=digest,
                            result_type="object",
                            result=stored,
                        )
                        records.append(stored.model_copy(deep=True))
                return tuple(records)
        except (
            ObjectProjectionRevisionConflict,
            OntologyStoreError,
            OntologyRegistryError,
            OntologyValidationError,
        ):
            raise
        except Exception as exception:
            self._raise_database_error(exception, operation=operation)
            raise AssertionError("unreachable")

    @staticmethod
    def _check_projection_revision(
        item: AtomicObjectUpsert,
        current: ObjectRecord | None,
    ) -> None:
        if item.expected_revision == 0:
            matches = current is None
        else:
            revision = None if current is None else current.properties.get("revision")
            matches = type(revision) is int and revision == item.expected_revision
        if not matches:
            raise ObjectProjectionRevisionConflict(
                "ontology object projection revision does not match "
                f"{item.type_name}/{item.object_id}"
            )

    def get_object(
        self,
        tenant_id: str,
        type_name: str,
        object_id: str,
        *,
        as_of: datetime | None = None,
    ) -> ObjectProjection:
        tenant = self._tenant(tenant_id)
        # Stage 12: an as-of read narrows active relations to those whose valid
        # interval [coalesce(valid_from, created_at), valid_to) contains the
        # instant. Without as_of the live active set is returned unchanged.
        as_of_clause = (
            " and coalesce(valid_from, created_at) <= %s"
            " and (valid_to is null or %s < valid_to)"
            if as_of is not None
            else ""
        )
        as_of_params: tuple[Any, ...] = (as_of, as_of) if as_of is not None else ()
        columns = (
            "tenant_id, relation_id, relation_name, schema_version, "
            "source_type, source_object_id, target_type, target_object_id, "
            "metadata, created_at, status, deleted_at, valid_from, valid_to"
        )
        try:
            with self._pool.connection() as connection, connection.transaction():
                connection.execute(
                    "set transaction isolation level repeatable read, read only"
                )
                self._bind_tenant(connection, tenant)
                with connection.cursor(row_factory=dict_row) as cursor:
                    stored_record = self._require_object(
                        cursor,
                        tenant,
                        type_name,
                        object_id,
                    )
                    record, _ = self._normalize_object_record(
                        cursor,
                        stored_record,
                    )
                    cursor.execute(
                        f"select {columns} from ontology.relations "
                        "where tenant_id = %s "
                        "and source_type = %s and source_object_id = %s "
                        "and status = 'active'" + as_of_clause + " "
                        "order by relation_id",
                        (tenant, type_name, object_id, *as_of_params),
                    )
                    outgoing = tuple(
                        self._relation_from_row(row) for row in cursor.fetchall()
                    )
                    cursor.execute(
                        f"select {columns} from ontology.relations "
                        "where tenant_id = %s "
                        "and target_type = %s and target_object_id = %s "
                        "and status = 'active'" + as_of_clause + " "
                        "order by relation_id",
                        (tenant, type_name, object_id, *as_of_params),
                    )
                    incoming = tuple(
                        self._relation_from_row(row) for row in cursor.fetchall()
                    )
                    cursor.execute(
                        """
                        select event.tenant_id, event.event_id, event.event_type,
                               event.object_type, event.object_id, event.payload,
                               event.actor_id, event.sequence, event.created_at,
                               event.schema_version,
                               schema.definition as event_definition,
                               object_schema.definition as object_definition,
                               relation_schema.definition as relation_definition
                        from ontology.events as event
                        left join ontology.event_type_versions as schema
                          on schema.tenant_id = %s
                         and schema.tenant_id = event.tenant_id
                         and schema.event_name = event.event_type
                         and schema.version = event.schema_version
                        left join ontology.object_type_versions as object_schema
                          on object_schema.tenant_id = %s
                         and object_schema.tenant_id = event.tenant_id
                         and object_schema.type_name = event.object_type
                         and object_schema.version = event.schema_version
                        left join ontology.relation_type_versions as relation_schema
                          on relation_schema.tenant_id = %s
                         and relation_schema.tenant_id = event.tenant_id
                         and relation_schema.relation_name =
                             event.payload ->> 'relation_name'
                         and relation_schema.version = event.schema_version
                        where event.tenant_id = %s
                          and event.object_type = %s and event.object_id = %s
                        order by event.sequence, event.event_id
                        """,
                        (tenant, tenant, tenant, tenant, type_name, object_id),
                    )
                    events = tuple(
                        self._normalize_event_record(
                            row,
                        )
                        for row in cursor.fetchall()
                    )
                    return ObjectProjection(
                        object=record,
                        outgoing_relations=outgoing,
                        incoming_relations=incoming,
                        events=events,
                    )
        except OntologyStoreError:
            raise
        except Exception as exception:
            self._raise_database_error(exception, operation="object.get")
            raise AssertionError("unreachable")

    def read_object_subgraph(
        self,
        tenant_id: str,
        type_name: str,
        object_id: str,
        *,
        max_depth: int,
        as_of: datetime | None = None,
    ) -> ObjectSubgraph:
        tenant = self._tenant(tenant_id)
        if max_depth < 1:
            raise ValueError("max_depth must be >= 1")
        # Same live/as-of semantics as get_object: without as_of the live active
        # set; with it, only relations whose valid interval contains the instant.
        as_of_pred = (
            " and coalesce(r.valid_from, r.created_at) <= %s"
            " and (r.valid_to is null or %s < r.valid_to)"
            if as_of is not None
            else ""
        )
        as_of_params: tuple[Any, ...] = (as_of, as_of) if as_of is not None else ()
        # A cycle-safe undirected walk: each hop follows an active relation in
        # either direction; the path array both bounds depth and blocks revisits
        # (identical guard to object_set_executor's traversal CTE).
        walk_cte = (
            "with recursive walk (node_type, node_id, depth, path) as ("
            "  select %s::text, %s::text, 0, array[%s || ':' || %s] "
            "  union all "
            "  select e.nt, e.ni, w.depth + 1, w.path || (e.nt || ':' || e.ni) "
            "  from walk as w "
            "  join ("
            "    select r.source_type as ft, r.source_object_id as fi, "
            "           r.target_type as nt, r.target_object_id as ni "
            "    from ontology.relations as r "
            "    where r.tenant_id = %s and r.status = 'active'" + as_of_pred + " "
            "    union all "
            "    select r.target_type as ft, r.target_object_id as fi, "
            "           r.source_type as nt, r.source_object_id as ni "
            "    from ontology.relations as r "
            "    where r.tenant_id = %s and r.status = 'active'" + as_of_pred + " "
            "  ) as e on e.ft = w.node_type and e.fi = w.node_id "
            "  where w.depth < %s "
            "    and not ((e.nt || ':' || e.ni) = any(w.path)) "
            ")"
        )
        walk_params: tuple[Any, ...] = (
            type_name,
            object_id,
            type_name,
            object_id,
            tenant,
            *as_of_params,
            tenant,
            *as_of_params,
            max_depth,
        )
        try:
            with self._pool.connection() as connection, connection.transaction():
                connection.execute(
                    "set transaction isolation level repeatable read, read only"
                )
                self._bind_tenant(connection, tenant)
                with connection.cursor(row_factory=dict_row) as cursor:
                    # Root must exist (→ ObjectNotFound), never an empty graph.
                    self._require_object(cursor, tenant, type_name, object_id)
                    cursor.execute(
                        walk_cte + " select node_type, node_id, min(depth) as depth "
                        "from walk group by node_type, node_id "
                        "order by node_type, node_id",
                        walk_params,
                    )
                    nodes = tuple(
                        SubgraphNode(
                            type_name=row["node_type"],
                            object_id=row["node_id"],
                            depth=int(row["depth"]),
                        )
                        for row in cursor.fetchall()
                    )
                    # Induced edges: active relations with both endpoints reached.
                    # group by collapses parallel records to one topology edge.
                    cursor.execute(
                        walk_cte
                        + ", ns as (select distinct node_type, node_id from walk) "
                        "select r.relation_name, r.source_type, "
                        "       r.source_object_id, r.target_type, "
                        "       r.target_object_id "
                        "from ontology.relations as r "
                        "join ns as s "
                        "  on s.node_type = r.source_type "
                        " and s.node_id = r.source_object_id "
                        "join ns as t "
                        "  on t.node_type = r.target_type "
                        " and t.node_id = r.target_object_id "
                        "where r.tenant_id = %s and r.status = 'active'"
                        + as_of_pred
                        + " group by r.relation_name, r.source_type, "
                        "   r.source_object_id, r.target_type, r.target_object_id "
                        "order by r.source_type, r.source_object_id, "
                        "   r.relation_name, r.target_type, r.target_object_id",
                        (*walk_params, tenant, *as_of_params),
                    )
                    edges = tuple(
                        SubgraphEdge(
                            relation_name=row["relation_name"],
                            source_type=row["source_type"],
                            source_object_id=row["source_object_id"],
                            target_type=row["target_type"],
                            target_object_id=row["target_object_id"],
                        )
                        for row in cursor.fetchall()
                    )
                    return ObjectSubgraph(
                        root_type=type_name,
                        root_object_id=object_id,
                        max_depth=max_depth,
                        nodes=nodes,
                        edges=edges,
                    )
        except OntologyStoreError:
            raise
        except Exception as exception:
            self._raise_database_error(exception, operation="object.subgraph")
            raise AssertionError("unreachable")

    @staticmethod
    def _type_filter_clause(
        params: list[Any],
        column: str,
        *,
        type_name: str | None,
        type_names: tuple[str, ...] | None,
    ) -> str:
        """单类型等值 / 多类型 ``= any(...)``,两者互斥。

        多值形态仍走同一个 ``(tenant_id, type_name, ...)`` 前导索引,只是
        谓词从等值变成集合成员——排序键含 type_name,故多类型归并后仍是
        单一有序流,一个游标够用。
        """

        if type_names is not None:
            if type_name is not None:
                raise ValueError("type_name and type_names are mutually exclusive")
            params.append(list(type_names))
            return f" and {column} = any(%s)"
        if type_name is not None:
            params.append(type_name)
            return f" and {column} = %s"
        return ""

    @staticmethod
    def _tombstone_filter_clauses(
        params: list[Any],
        *,
        court_id: str | None,
        starts_after: str | None,
        starts_before: str | None,
    ) -> str:
        """墓碑属性快照上的同一套过滤谓词(0132)。

        快照列为空串表示删除时对象没有该属性——与活对象侧 ``NULL 比较为假``
        一致,空串在任一范围条件下都不命中。
        """

        clauses = ""
        if court_id is not None:
            clauses += " and tombstone.court_id = %s"
            params.append(court_id)
        if starts_after is not None:
            clauses += " and tombstone.start_at <> '' and tombstone.start_at >= %s"
            params.append(starts_after)
        if starts_before is not None:
            clauses += " and tombstone.start_at <> '' and tombstone.start_at < %s"
            params.append(starts_before)
        return clauses

    # 已退役的投影(0102 的 tennis_window_retired_at)对读路径等同于不存在:
    # 窗口收缩后留下的重影与活对象在同一 (court_id, 业务日期) 上互相矛盾,
    # 外部拉取方不该看到它们。库内投影读(0102 的日历快照函数)一直是这个
    # 谓词,这里让应用侧读路径与之对齐。退役是可逆的(0102 的增量投影重新
    # 写回该对象时会把列清回 null),所以只能过滤、不能当成删除。
    _ACTIVE_OBJECT_CLAUSE = " and object.tennis_window_retired_at is null"

    @staticmethod
    def _property_filter_clauses(
        params: list[Any],
        *,
        court_id: str | None,
        starts_after: str | None,
        starts_before: str | None,
    ) -> str:
        """``properties->>...`` 过滤谓词(0129 表达式索引对应的形态)。

        start_at 是全库统一 ``+08:00`` 偏移的 ISO 字符串,同格式下文本序即
        时间序;starts_after 含端点,starts_before 不含(半开区间)。缺
        start_at 的对象在范围条件下不命中(SQL NULL 比较为假)。
        """

        clauses = ""
        if court_id is not None:
            clauses += " and object.properties->>'court_id' = %s"
            params.append(court_id)
        if starts_after is not None:
            clauses += " and object.properties->>'start_at' >= %s"
            params.append(starts_after)
        if starts_before is not None:
            clauses += " and object.properties->>'start_at' < %s"
            params.append(starts_before)
        return clauses

    @staticmethod
    def _marking_clause(
        params: list[Any], caller_markings: frozenset[str] | None
    ) -> str:
        """0022 说的「layered in the parameterized read SQL」——就是这一句。

        ``markings <@ %s`` 走 0022 建的 GIN 索引。None = 内部编排,不过滤;
        空 frozenset = 有调用方但没有任何 clearance,只看得见未标记的行。
        """

        if caller_markings is None:
            return ""
        params.append(sorted(caller_markings))
        return " and object.markings <@ %s::text[]"

    def list_objects(
        self,
        tenant_id: str,
        *,
        type_name: str | None = None,
        type_names: tuple[str, ...] | None = None,
        limit: int | None = None,
        after: tuple[str, str] | None = None,
        court_id: str | None = None,
        starts_after: str | None = None,
        starts_before: str | None = None,
        caller_markings: frozenset[str] | None = None,
    ) -> list[ObjectRecord]:
        tenant = self._tenant(tenant_id)
        # The join tenant param mirrors the WHERE tenant param, then the
        # optional type / filter / keyset / limit clauses append in reading
        # order.
        query = """
            select object.tenant_id, object.type_name,
                   object.object_id, object.schema_version,
                   object.properties, object.source_system,
                   object.source_ref, object.created_at,
                   object.updated_at, object.markings,
                   schema.definition as object_definition
            from ontology.objects as object
            left join ontology.object_type_versions as schema
              on schema.tenant_id = %s
             and schema.tenant_id = object.tenant_id
             and schema.type_name = object.type_name
             and schema.version = object.schema_version
            where object.tenant_id = %s
        """
        params: list[Any] = [tenant, tenant]
        query += self._ACTIVE_OBJECT_CLAUSE
        query += self._type_filter_clause(
            params, "object.type_name", type_name=type_name, type_names=type_names
        )
        query += self._property_filter_clauses(
            params,
            court_id=court_id,
            starts_after=starts_after,
            starts_before=starts_before,
        )
        query += self._marking_clause(params, caller_markings)
        if after is not None:
            query += " and (object.type_name, object.object_id) > (%s, %s)"
            params.extend([after[0], after[1]])
        query += " order by object.type_name, object.object_id"
        if limit is not None:
            query += " limit %s"
            params.append(limit)
        try:
            with self._pool.connection() as connection, connection.transaction():
                connection.execute(
                    "set transaction isolation level repeatable read, read only"
                )
                self._bind_tenant(connection, tenant)
                with connection.cursor(row_factory=dict_row) as cursor:
                    cursor.execute(query, tuple(params))
                    return [
                        self._normalize_joined_object_row(row)
                        for row in cursor.fetchall()
                    ]
        except OntologyStoreError:
            raise
        except Exception as exception:
            self._raise_database_error(exception, operation="object.list")
            raise AssertionError("unreachable")

    def list_changed_objects(
        self,
        tenant_id: str,
        *,
        updated_since: datetime,
        type_name: str | None = None,
        type_names: tuple[str, ...] | None = None,
        limit: int | None = None,
        after: tuple[datetime, str, str] | None = None,
        court_id: str | None = None,
        starts_after: str | None = None,
        starts_before: str | None = None,
        caller_markings: frozenset[str] | None = None,
    ) -> list[ObjectRecord]:
        """``updated_at`` 严格晚于 ``updated_since`` 的对象,按
        ``(updated_at, type_name, object_id)`` 升序——增量拉取的变更流序,
        走 0013 的 ``ontology_objects_page_keyset`` 索引。"""

        tenant = self._tenant(tenant_id)
        query = """
            select object.tenant_id, object.type_name,
                   object.object_id, object.schema_version,
                   object.properties, object.source_system,
                   object.source_ref, object.created_at,
                   object.updated_at, object.markings,
                   schema.definition as object_definition
            from ontology.objects as object
            left join ontology.object_type_versions as schema
              on schema.tenant_id = %s
             and schema.tenant_id = object.tenant_id
             and schema.type_name = object.type_name
             and schema.version = object.schema_version
            where object.tenant_id = %s
              and object.updated_at > %s
        """
        params: list[Any] = [tenant, tenant, updated_since]
        query += self._ACTIVE_OBJECT_CLAUSE
        query += self._type_filter_clause(
            params, "object.type_name", type_name=type_name, type_names=type_names
        )
        query += self._property_filter_clauses(
            params,
            court_id=court_id,
            starts_after=starts_after,
            starts_before=starts_before,
        )
        if after is not None:
            query += (
                " and (object.updated_at, object.type_name, object.object_id)"
                " > (%s, %s, %s)"
            )
            params.extend([after[0], after[1], after[2]])
        query += self._marking_clause(params, caller_markings)
        query += " order by object.updated_at, object.type_name, object.object_id"
        if limit is not None:
            query += " limit %s"
            params.append(limit)
        try:
            with self._pool.connection() as connection, connection.transaction():
                connection.execute(
                    "set transaction isolation level repeatable read, read only"
                )
                self._bind_tenant(connection, tenant)
                with connection.cursor(row_factory=dict_row) as cursor:
                    cursor.execute(query, tuple(params))
                    return [
                        self._normalize_joined_object_row(row)
                        for row in cursor.fetchall()
                    ]
        except OntologyStoreError:
            raise
        except Exception as exception:
            self._raise_database_error(exception, operation="object.list_changed")
            raise AssertionError("unreachable")

    def list_object_tombstones(
        self,
        tenant_id: str,
        *,
        deleted_since: datetime,
        type_name: str | None = None,
        type_names: tuple[str, ...] | None = None,
        limit: int | None = None,
        after: tuple[datetime, str, str] | None = None,
        court_id: str | None = None,
        starts_after: str | None = None,
        starts_before: str | None = None,
    ) -> list[ObjectTombstone]:
        """``deleted_at`` 严格晚于 ``deleted_since`` 的删除墓碑(0129),排序
        键与 ``list_changed_objects`` 一致,两条流按同一游标归并。属性过滤
        走 0132 的快照列。

        两个来源合并成一条墓碑流:

        * ``ontology.object_tombstones``——prune 硬删对象时留下的真墓碑。
        * **退役对象的合成墓碑**——``tennis_window_retired_at`` 非空的对象。
          退役后它已从活对象流里消失,若不同时给出墓碑,已经同步过它的拉取
          方就会在本地永久残留一份陈旧投影。以退役时刻作为 ``deleted_at``
          参与同一排序键;对象日后被重新投影(0102 会把该列清回 null)时,
          其 ``updated_at`` 必然晚于退役时刻,归并流里就是「先删后加」,
          拉取方按序应用即收敛。合成墓碑放在读路径,因此覆盖历史与将来的
          所有退役写入方,不需要给每个退役点补墓碑行。
        """

        tenant = self._tenant(tenant_id)
        params: list[Any] = [tenant, deleted_since]
        tombstone_query = """
            select tombstone.tenant_id, tombstone.type_name,
                   tombstone.object_id, tombstone.deleted_at,
                   tombstone.court_id, tombstone.start_at
            from ontology.object_tombstones as tombstone
            where tombstone.tenant_id = %s
              and tombstone.deleted_at > %s
        """
        tombstone_query += self._type_filter_clause(
            params,
            "tombstone.type_name",
            type_name=type_name,
            type_names=type_names,
        )
        tombstone_query += self._tombstone_filter_clauses(
            params,
            court_id=court_id,
            starts_after=starts_after,
            starts_before=starts_before,
        )
        if after is not None:
            tombstone_query += (
                " and (tombstone.deleted_at, tombstone.type_name,"
                " tombstone.object_id) > (%s, %s, %s)"
            )
            params.extend([after[0], after[1], after[2]])

        retired_query = """
            select object.tenant_id, object.type_name,
                   object.object_id,
                   object.tennis_window_retired_at as deleted_at,
                   coalesce(object.properties->>'court_id', '') as court_id,
                   coalesce(object.properties->>'start_at', '') as start_at
            from ontology.objects as object
            where object.tenant_id = %s
              and object.tennis_window_retired_at is not null
              and object.tennis_window_retired_at > %s
        """
        params.extend([tenant, deleted_since])
        retired_query += self._type_filter_clause(
            params, "object.type_name", type_name=type_name, type_names=type_names
        )
        retired_query += self._property_filter_clauses(
            params,
            court_id=court_id,
            starts_after=starts_after,
            starts_before=starts_before,
        )
        if after is not None:
            retired_query += (
                " and (object.tennis_window_retired_at, object.type_name,"
                " object.object_id) > (%s, %s, %s)"
            )
            params.extend([after[0], after[1], after[2]])

        query = (
            f"select * from ({tombstone_query} union all {retired_query}) as entry"
            " order by entry.deleted_at, entry.type_name, entry.object_id"
        )
        if limit is not None:
            query += " limit %s"
            params.append(limit)
        try:
            with self._pool.connection() as connection, connection.transaction():
                connection.execute(
                    "set transaction isolation level repeatable read, read only"
                )
                self._bind_tenant(connection, tenant)
                with connection.cursor(row_factory=dict_row) as cursor:
                    cursor.execute(query, tuple(params))
                    return [
                        ObjectTombstone(
                            tenant_id=row["tenant_id"],
                            type_name=row["type_name"],
                            object_id=row["object_id"],
                            deleted_at=row["deleted_at"],
                            court_id=row["court_id"] or "",
                            start_at=row["start_at"] or "",
                        )
                        for row in cursor.fetchall()
                    ]
        except OntologyStoreError:
            raise
        except Exception as exception:
            self._raise_database_error(exception, operation="object.tombstones")
            raise AssertionError("unreachable")

    def list_relations(
        self,
        tenant_id: str,
        *,
        relation_name: str | None = None,
        limit: int | None = None,
        after: str | None = None,
    ) -> list[RelationRecord]:
        """Active relations ordered by ``relation_id``(keyset 分页批量读)。

        收尾投影的「link 已存在即跳过」预检专用:一页顶几百次
        ``link_relation`` 的 exact-row 探测往返。只读 active,软删的边
        不出现(与 ``link_relation`` 的短路判定同一谓词)。
        """

        tenant = self._tenant(tenant_id)
        query = """
            select tenant_id, relation_id, relation_name, schema_version,
                   source_type, source_object_id, target_type,
                   target_object_id, metadata, created_at, status, deleted_at
            from ontology.relations
            where tenant_id = %s and status = 'active'
        """
        params: list[Any] = [tenant]
        if relation_name is not None:
            query += " and relation_name = %s"
            params.append(relation_name)
        if after is not None:
            query += " and relation_id > %s"
            params.append(after)
        query += " order by relation_id"
        if limit is not None:
            query += " limit %s"
            params.append(limit)
        try:
            with self._pool.connection() as connection, connection.transaction():
                connection.execute(
                    "set transaction isolation level repeatable read, read only"
                )
                self._bind_tenant(connection, tenant)
                with connection.cursor(row_factory=dict_row) as cursor:
                    cursor.execute(query, tuple(params))
                    return [
                        self._relation_from_row(row) for row in cursor.fetchall()
                    ]
        except OntologyStoreError:
            raise
        except Exception as exception:
            self._raise_database_error(exception, operation="relation.list")
            raise AssertionError("unreachable")

    def list_objects_by_ids(
        self,
        tenant_id: str,
        type_name: str,
        object_ids: tuple[str, ...],
        *,
        deadline: float | None = None,
    ) -> list[ObjectRecord]:
        """一次批量主键点查(``object_id = any(...)``,走主键索引)。"""

        if not object_ids:
            return []
        tenant = self._tenant(tenant_id)
        query = """
            select object.tenant_id, object.type_name,
                   object.object_id, object.schema_version,
                   object.properties, object.source_system,
                   object.source_ref, object.created_at,
                   object.updated_at, object.markings,
                   schema.definition as object_definition
            from ontology.objects as object
            left join ontology.object_type_versions as schema
              on schema.tenant_id = %s
             and schema.tenant_id = object.tenant_id
             and schema.type_name = object.type_name
             and schema.version = object.schema_version
            where object.tenant_id = %s
              and object.type_name = %s
              and object.object_id = any(%s)
            order by object.object_id
        """
        params: list[Any] = [tenant, tenant, type_name, list(object_ids)]

        def remaining_ms() -> int:
            remaining = int((deadline - monotonic()) * 1000) if deadline is not None else 0
            if remaining <= 0:
                raise TimeoutError("presentation lookup budget exhausted")
            return remaining

        def refresh_timeout(connection: Any) -> None:
            if deadline is not None:
                milliseconds = remaining_ms()
                connection.execute(
                    "select set_config('statement_timeout', %s, true), "
                    "set_config('lock_timeout', %s, true)",
                    (str(milliseconds), str(milliseconds)),
                )
                remaining_ms()

        try:
            options = {} if deadline is None else {"timeout": min(0.05, remaining_ms() / 1000)}
            with self._pool.connection(**options) as connection, connection.transaction():
                connection.execute(
                    "set transaction isolation level repeatable read, read only"
                )
                refresh_timeout(connection)
                self._bind_tenant(connection, tenant)
                refresh_timeout(connection)
                with connection.cursor(row_factory=dict_row) as cursor:
                    cursor.execute(query, tuple(params))
                    if deadline is not None:
                        remaining_ms()
                    result = [
                        self._normalize_joined_object_row(row)
                        for row in cursor.fetchall()
                    ]
                    if deadline is not None:
                        remaining_ms()
                    return result
        except OntologyStoreError:
            raise
        except Exception as exception:
            self._raise_database_error(exception, operation="object.batch_get")
            raise AssertionError("unreachable")

    def count_objects(
        self,
        tenant_id: str,
        *,
        type_name: str | None = None,
        type_names: tuple[str, ...] | None = None,
        court_id: str | None = None,
        starts_after: str | None = None,
        starts_before: str | None = None,
        caller_markings: frozenset[str] | None = None,
    ) -> int:
        tenant = self._tenant(tenant_id)
        query = (
            "select count(*) as total from ontology.objects as object"
            " where object.tenant_id = %s"
        )
        params: list[Any] = [tenant]
        query += self._type_filter_clause(
            params, "object.type_name", type_name=type_name, type_names=type_names
        )
        query += self._property_filter_clauses(
            params,
            court_id=court_id,
            starts_after=starts_after,
            starts_before=starts_before,
        )
        # 总数也要按可见行算 —— 虚高的 total 就是存在性泄漏。
        query += self._marking_clause(params, caller_markings)
        try:
            with self._pool.connection() as connection, connection.transaction():
                connection.execute(
                    "set transaction isolation level repeatable read, read only"
                )
                self._bind_tenant(connection, tenant)
                with connection.cursor(row_factory=dict_row) as cursor:
                    cursor.execute(query, tuple(params))
                    row = cursor.fetchone()
                    return int(row["total"]) if row is not None else 0
        except OntologyStoreError:
            raise
        except Exception as exception:
            self._raise_database_error(exception, operation="object.count")
            raise AssertionError("unreachable")

    def delete_objects(
        self, *, tenant_id: str, type_name: str, object_ids: tuple[str, ...]
    ) -> int:
        """退役滚出维护范围的对象:硬删对象/关系/事件,并留下墓碑。

        走 0211 的 ``ontology.app_object_retire`` definer,不直接写表:
        ``nex_eios_app`` 对 ``ontology.objects`` 无 DELETE、对
        ``ontology.object_tombstones`` 无 INSERT(0100 收走的)。

        ⚠️ 这个方法此前**只存在于 InMemoryOntologyStore**,本类从来没有过。
        调用点把 store 类型擦成 ``Any``,``_Store`` Protocol 因此从没与本类
        核对过,于是「排课退役」在生产上一直不可执行(2026-08-17 幂等冲突
        一解冻就撞出 AttributeError)。配套测试一律用真适配器。
        """

        if isinstance(object_ids, str):
            raise ValueError("object_ids must be a sequence of ids")
        tenant = self._tenant(tenant_id)
        ids = tuple(item for item in object_ids if item)
        if not ids:
            return 0
        try:
            with self._pool.connection() as connection, connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "select ontology.app_object_retire(%s, %s, %s)",
                        (tenant, type_name, list(ids)),
                    )
                    row = cursor.fetchone()
                    return int(row[0]) if row and row[0] is not None else 0
        except OntologyStoreError:
            raise
        except Exception as exception:
            self._raise_database_error(exception, operation="object.retire")
            raise AssertionError("unreachable")

    def prune_expired_inventory_objects(
        self, *, tenant_id: str, window_start: date
    ) -> dict[str, int]:
        """调用固定三类对象的 0072 tenant-bound prune RPC。"""

        tenant = self._tenant(tenant_id)
        if type(window_start) is not date:
            raise ValueError("window_start must be a date")
        try:
            with self._pool.connection() as connection, connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        select object_type, deleted_count
                        from operations.prune_expired_inventory_objects(%s, %s)
                        """,
                        (tenant, window_start),
                    )
                    rows = cursor.fetchall()
                    return {
                        str(object_type): int(deleted_count)
                        for object_type, deleted_count in rows
                    }
        except OntologyStoreError:
            raise
        except Exception as exception:
            self._raise_database_error(
                exception, operation="inventory_object.prune"
            )
            raise AssertionError("unreachable")

    def prune_object_update_events(
        self,
        *,
        tenant_id: str,
        object_types: tuple[str, ...],
        before: datetime,
    ) -> int:
        """删除高频类型早于 ``before`` 的 ObjectUpdated 事件行(账本 TTL)。

        只删 ObjectUpdated:对象 upsert 的幂等结果自包含(result_type=object,
        replay 从不回读事件行),这类事件没有任何重放依赖;ObjectCreated /
        RelationLinked / 领域事件保留——领域事件的幂等 replay 会按 event_id
        回读事件行,删了会 fail-closed,且时间线保留创建与连边骨架。
        0072 RPC 在数据库内固定事件类型、对象类型白名单与保留期下限。
        """

        tenant = self._tenant(tenant_id)
        if not object_types:
            return 0
        try:
            with self._pool.connection() as connection, connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        select operations.prune_write_ledger_events(%s, %s, %s)
                        """,
                        (tenant, list(object_types), before),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise OntologyStoreError(
                            "event prune RPC returned no result"
                        )
                    return int(row[0])
        except OntologyStoreError:
            raise
        except Exception as exception:
            self._raise_database_error(exception, operation="event.prune")
            raise AssertionError("unreachable")

    def prune_idempotency_records(
        self,
        *,
        tenant_id: str,
        key_prefixes: tuple[str, ...],
        before: datetime,
    ) -> int:
        """删除白名单前缀、早于 ``before`` 的幂等记录(重放窗口 TTL)。

        只删调用方点名的前缀:这些写路径重执行天然安全(内容 digest /周期键
        的 upsert 撞上无实变短路;link 撞上既有关系直接复用)。绝不全表删——
        例如 ``tennis-seed:*`` 的 replay 是「不覆盖运行期状态」的守卫,删了
        重跑会把封场打回种子可售态。0072 RPC 还会在数据库内重复校验固定
        前缀白名单与保留期下限。
        """

        tenant = self._tenant(tenant_id)
        if not key_prefixes:
            return 0
        try:
            with self._pool.connection() as connection, connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        select operations.prune_write_idempotency_records(
                            %s, %s, %s
                        )
                        """,
                        (tenant, list(key_prefixes), before),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise OntologyStoreError(
                            "idempotency prune RPC returned no result"
                        )
                    return int(row[0])
        except OntologyStoreError:
            raise
        except Exception as exception:
            self._raise_database_error(exception, operation="idempotency.prune")
            raise AssertionError("unreachable")

    def link_relation(
        self,
        *,
        tenant_id: str,
        relation_name: str,
        source_type: str,
        source_object_id: str,
        target_type: str,
        target_object_id: str,
        idempotency_key: str,
        actor_id: str = "",
        metadata: dict[str, Any] | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> RelationRecord:
        if valid_from is not None and valid_to is not None and valid_to <= valid_from:
            raise RelationEndpointMismatch("valid_to must be after valid_from")
        tenant = self._tenant(tenant_id)
        key = require_idempotency_key(idempotency_key)
        schema = self._registry.get_relation_type(tenant, relation_name)
        if schema.source_type != source_type or schema.target_type != target_type:
            raise RelationEndpointMismatch(
                f"relation {relation_name} requires "
                f"{schema.source_type}->{schema.target_type}"
            )
        normalized_metadata = validate_relation_properties(schema, metadata or {})
        operation = "relation.link"
        digest = payload_digest(
            {
                "relation_name": relation_name,
                "source_type": source_type,
                "source_object_id": source_object_id,
                "target_type": target_type,
                "target_object_id": target_object_id,
                "metadata": normalized_metadata,
            }
        )
        try:
            with self._pool.connection() as connection, connection.transaction():
                self._bind_tenant(connection, tenant)
                self._lock_idempotency(connection, tenant, operation, key)
                self._lock_relation_constraints(
                    connection,
                    tenant,
                    schema,
                    source_type,
                    source_object_id,
                    target_type,
                    target_object_id,
                )
                for endpoint_type, endpoint_id in sorted(
                    {
                        (source_type, source_object_id),
                        (target_type, target_object_id),
                    }
                ):
                    self._lock_object(
                        connection,
                        tenant,
                        endpoint_type,
                        endpoint_id,
                    )
                with connection.cursor(row_factory=dict_row) as cursor:
                    reused = self._idempotent_result(
                        cursor,
                        tenant_id=tenant,
                        operation=operation,
                        idempotency_key=key,
                        request_digest=digest,
                        model=RelationRecord,
                    )
                    if reused is not None:
                        return reused
                    self._require_object(
                        cursor,
                        tenant,
                        source_type,
                        source_object_id,
                    )
                    self._require_object(
                        cursor,
                        tenant,
                        target_type,
                        target_object_id,
                    )
                    cursor.execute(
                        """
                        select tenant_id, relation_id, relation_name,
                               schema_version, source_type, source_object_id,
                               target_type, target_object_id, metadata,
                               created_at, status, deleted_at
                        from ontology.relations
                        where tenant_id = %s and relation_name = %s
                          and source_type = %s and source_object_id = %s
                          and target_type = %s and target_object_id = %s
                          and status = 'active'
                        order by relation_id
                        limit 1
                        """,
                        (
                            tenant,
                            relation_name,
                            source_type,
                            source_object_id,
                            target_type,
                            target_object_id,
                        ),
                    )
                    exact_row = cursor.fetchone()
                    if exact_row is not None:
                        stored = self._relation_from_row(exact_row)
                    else:
                        if schema.cardinality in {
                            RelationCardinality.ONE_TO_ONE,
                            RelationCardinality.MANY_TO_ONE,
                        }:
                            cursor.execute(
                                """
                                select exists (
                                  select 1
                                  from ontology.relations
                                  where tenant_id = %s and relation_name = %s
                                    and source_type = %s
                                    and source_object_id = %s
                                    and status = 'active'
                                    -- Stage 12: conflict only on overlapping
                                    -- valid intervals (half-open, null = +inf).
                                    and (valid_to is null
                                         or valid_to > coalesce(%s, now()))
                                    and (%s::timestamptz is null
                                         or %s > coalesce(valid_from, created_at))
                                  limit 1
                                ) as in_use
                                """,
                                (
                                    tenant,
                                    relation_name,
                                    source_type,
                                    source_object_id,
                                    valid_from,
                                    valid_to,
                                    valid_to,
                                ),
                            )
                            if cursor.fetchone()["in_use"]:
                                raise RelationCardinalityViolation(
                                    f"source already linked for {relation_name}"
                                )
                        if schema.cardinality in {
                            RelationCardinality.ONE_TO_ONE,
                            RelationCardinality.ONE_TO_MANY,
                        }:
                            cursor.execute(
                                """
                                select exists (
                                  select 1
                                  from ontology.relations
                                  where tenant_id = %s and relation_name = %s
                                    and target_type = %s
                                    and target_object_id = %s
                                    and status = 'active'
                                    and (valid_to is null
                                         or valid_to > coalesce(%s, now()))
                                    and (%s::timestamptz is null
                                         or %s > coalesce(valid_from, created_at))
                                  limit 1
                                ) as in_use
                                """,
                                (
                                    tenant,
                                    relation_name,
                                    target_type,
                                    target_object_id,
                                    valid_from,
                                    valid_to,
                                    valid_to,
                                ),
                            )
                            if cursor.fetchone()["in_use"]:
                                raise RelationCardinalityViolation(
                                    f"target already linked for {relation_name}"
                                )
                        stored = RelationRecord(
                            tenant_id=tenant,
                            relation_id=f"rel_{uuid.uuid4().hex}",
                            relation_name=relation_name,
                            schema_version=schema.version,
                            source_type=source_type,
                            source_object_id=source_object_id,
                            target_type=target_type,
                            target_object_id=target_object_id,
                            metadata=normalized_metadata,
                            created_at=datetime.now(UTC),
                            valid_from=valid_from,
                            valid_to=valid_to,
                        )
                        cursor.execute(
                            "select ontology.app_relation_insert"
                            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                            (
                                stored.tenant_id,
                                stored.relation_id,
                                stored.relation_name,
                                stored.schema_version,
                                stored.source_type,
                                stored.source_object_id,
                                stored.target_type,
                                stored.target_object_id,
                                Jsonb(stored.model_dump(mode="json")["metadata"]),
                                stored.created_at,
                                stored.valid_from,
                                stored.valid_to,
                            ),
                        )
                        payload = {
                            "relation_id": stored.relation_id,
                            "relation_name": relation_name,
                            "source": f"{source_type}/{source_object_id}",
                            "target": f"{target_type}/{target_object_id}",
                        }
                        self._append_event_record(
                            cursor,
                            tenant_id=tenant,
                            event_type="RelationLinked",
                            object_type=source_type,
                            object_id=source_object_id,
                            payload=payload,
                            actor_id=actor_id,
                            schema_version=schema.version,
                        )
                        self._append_event_record(
                            cursor,
                            tenant_id=tenant,
                            event_type="RelationLinked",
                            object_type=target_type,
                            object_id=target_object_id,
                            payload=payload,
                            actor_id=actor_id,
                            schema_version=schema.version,
                        )
                    self._remember_idempotency(
                        cursor,
                        tenant_id=tenant,
                        operation=operation,
                        idempotency_key=key,
                        request_digest=digest,
                        result_type="relation",
                        result=stored,
                    )
                    return stored.model_copy(deep=True)
        except (
            OntologyStoreError,
            OntologyRegistryError,
            OntologyValidationError,
        ):
            raise
        except Exception as exception:
            self._raise_database_error(exception, operation=operation)
            raise AssertionError("unreachable")

    def unlink_relation(
        self,
        *,
        tenant_id: str,
        relation_name: str,
        source_type: str,
        source_object_id: str,
        target_type: str,
        target_object_id: str,
        idempotency_key: str,
        actor_id: str = "",
    ) -> RelationRecord:
        tenant = self._tenant(tenant_id)
        key = require_idempotency_key(idempotency_key)
        schema = self._registry.get_relation_type(tenant, relation_name)
        if schema.source_type != source_type or schema.target_type != target_type:
            raise RelationEndpointMismatch(
                f"relation {relation_name} requires "
                f"{schema.source_type}->{schema.target_type}"
            )
        operation = "relation.unlink"
        digest = payload_digest(
            {
                "operation": operation,
                "relation_name": relation_name,
                "source_type": source_type,
                "source_object_id": source_object_id,
                "target_type": target_type,
                "target_object_id": target_object_id,
            }
        )
        try:
            with self._pool.connection() as connection, connection.transaction():
                self._bind_tenant(connection, tenant)
                self._lock_idempotency(connection, tenant, operation, key)
                self._lock_relation_constraints(
                    connection,
                    tenant,
                    schema,
                    source_type,
                    source_object_id,
                    target_type,
                    target_object_id,
                )
                with connection.cursor(row_factory=dict_row) as cursor:
                    reused = self._idempotent_result(
                        cursor,
                        tenant_id=tenant,
                        operation=operation,
                        idempotency_key=key,
                        request_digest=digest,
                        model=RelationRecord,
                    )
                    if reused is not None:
                        return reused
                    cursor.execute(
                        "select * from ontology.app_relation_unlink"
                        "(%s, %s, %s, %s, %s, %s, %s)",
                        (
                            tenant,
                            relation_name,
                            source_type,
                            source_object_id,
                            target_type,
                            target_object_id,
                            datetime.now(UTC),
                        ),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise RelationNotFound(
                            f"active relation not found: {relation_name}"
                        )
                    # The definer's OUT names carry an out_ prefix so they
                    # cannot shadow the target table's columns; strip it back
                    # to the row shape _relation_from_row expects.
                    stored = self._relation_from_row(
                        {
                            key.removeprefix("out_"): value
                            for key, value in row.items()
                        }
                    )
                    payload = {
                        "relation_id": stored.relation_id,
                        "relation_name": relation_name,
                        "source": f"{source_type}/{source_object_id}",
                        "target": f"{target_type}/{target_object_id}",
                    }
                    for event_object_type, event_object_id in (
                        (source_type, source_object_id),
                        (target_type, target_object_id),
                    ):
                        self._append_event_record(
                            cursor,
                            tenant_id=tenant,
                            event_type="RelationUnlinked",
                            object_type=event_object_type,
                            object_id=event_object_id,
                            payload=payload,
                            actor_id=actor_id,
                            schema_version=schema.version,
                        )
                    self._remember_idempotency(
                        cursor,
                        tenant_id=tenant,
                        operation=operation,
                        idempotency_key=key,
                        request_digest=digest,
                        result_type="relation",
                        result=stored,
                    )
                    return stored.model_copy(deep=True)
        except (
            OntologyStoreError,
            OntologyRegistryError,
            OntologyValidationError,
        ):
            raise
        except Exception as exception:
            self._raise_database_error(exception, operation=operation)
            raise AssertionError("unreachable")

    def append_event(
        self,
        *,
        tenant_id: str,
        event_type: str,
        object_type: str,
        object_id: str,
        payload: dict[str, Any],
        idempotency_key: str,
        actor_id: str = "",
    ) -> EventRecord:
        tenant = self._tenant(tenant_id)
        key = require_idempotency_key(idempotency_key)
        schema = self._registry.get_event_type(tenant, event_type)
        operation = "event.append"
        try:
            with self._pool.connection() as connection, connection.transaction():
                self._bind_tenant(connection, tenant)
                self._lock_idempotency(connection, tenant, operation, key)
                self._lock_object(connection, tenant, object_type, object_id)
                with connection.cursor(row_factory=dict_row) as cursor:
                    reused = self._event_idempotent_result(
                        cursor,
                        tenant_id=tenant,
                        operation=operation,
                        idempotency_key=key,
                        event_type=event_type,
                        object_type=object_type,
                        object_id=object_id,
                        payload=payload,
                    )
                    if reused is not None:
                        return reused
                    if schema.object_type != object_type:
                        raise RelationEndpointMismatch(
                            f"event {event_type} requires object type "
                            f"{schema.object_type}"
                        )
                    normalized = validate_event_payload(schema, payload)
                    digest = payload_digest(
                        {
                            "event_type": event_type,
                            "object_type": object_type,
                            "object_id": object_id,
                            "payload": normalized,
                        }
                    )
                    self._require_object(
                        cursor,
                        tenant,
                        object_type,
                        object_id,
                    )
                    stored = self._append_event_record(
                        cursor,
                        tenant_id=tenant,
                        event_type=event_type,
                        object_type=object_type,
                        object_id=object_id,
                        payload=normalized,
                        actor_id=actor_id,
                        schema_version=schema.version,
                    )
                    self._remember_idempotency(
                        cursor,
                        tenant_id=tenant,
                        operation=operation,
                        idempotency_key=key,
                        request_digest=digest,
                        result_type="event",
                        result=stored,
                    )
                    return stored.model_copy(deep=True)
        except (
            OntologyStoreError,
            OntologyRegistryError,
            OntologyValidationError,
        ):
            raise
        except Exception as exception:
            self._raise_database_error(exception, operation=operation)
            raise AssertionError("unreachable")

    @staticmethod
    def _append_event_record(
        cursor: Any,
        *,
        tenant_id: str,
        event_type: str,
        object_type: str,
        object_id: str,
        payload: dict[str, Any],
        actor_id: str,
        schema_version: int | None = None,
    ) -> EventRecord:
        cursor.execute(
            """
            select coalesce(max(sequence), 0) + 1 as next_sequence
            from ontology.events
            where tenant_id = %s and object_type = %s and object_id = %s
            """,
            (tenant_id, object_type, object_id),
        )
        sequence = int(cursor.fetchone()["next_sequence"])
        stored = EventRecord(
            tenant_id=tenant_id,
            event_id=f"evt_{uuid.uuid4().hex}",
            event_type=event_type,
            object_type=object_type,
            object_id=object_id,
            payload=payload,
            actor_id=actor_id,
            sequence=sequence,
            created_at=datetime.now(UTC),
        )
        cursor.execute(
            "select ontology.app_event_append"
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                stored.tenant_id,
                stored.event_id,
                stored.event_type,
                stored.object_type,
                stored.object_id,
                Jsonb(stored.model_dump(mode="json")["payload"]),
                stored.actor_id,
                stored.sequence,
                stored.created_at,
                schema_version,
            ),
        )
        return stored
