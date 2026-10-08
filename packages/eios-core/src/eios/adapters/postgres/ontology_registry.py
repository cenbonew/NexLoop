from __future__ import annotations

from typing import Any, Callable, TypeVar

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from eios.ontology.models import (
    EventTypeDefinition,
    ObjectTypeDefinition,
    OntologySchemaSnapshot,
    RelationTypeDefinition,
)
from eios.ontology.registry import (
    OntologyRegistryError,
    RegistryIdempotencyConflict,
    SchemaNotFound,
)
from eios.ontology.semantics import (
    assert_event_compatible,
    assert_object_compatible,
    assert_relation_compatible,
    definition_digest,
    schema_revision,
)


DefinitionT = TypeVar(
    "DefinitionT",
    ObjectTypeDefinition,
    RelationTypeDefinition,
    EventTypeDefinition,
)


class PostgresOntologyRegistry:
    """PostgreSQL-backed ontology schema registry."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    @staticmethod
    def _tenant(tenant_id: str) -> str:
        clean = str(tenant_id or "").strip()
        if not clean:
            raise ValueError("tenant_id is required")
        return clean

    @staticmethod
    def _idempotency_key(idempotency_key: str | None) -> str:
        return str(idempotency_key or "").strip()

    @staticmethod
    def _bind_tenant(connection: Any, tenant_id: str) -> None:
        connection.execute(
            "select set_config('eios.tenant_id', %s, true)",
            (tenant_id,),
        )

    @staticmethod
    def _lock_schema(connection: Any, tenant_id: str, kind: str, name: str) -> None:
        lock_key = f"ontology-registry:{tenant_id}:{kind}:{name}"
        connection.execute(
            "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (lock_key,),
        )

    @staticmethod
    def _lock_idempotency(
        connection: Any,
        tenant_id: str,
        operation: str,
        idempotency_key: str,
    ) -> None:
        if not idempotency_key:
            return
        lock_key = (
            f"ontology-registry:{tenant_id}:idempotency:{operation}:{idempotency_key}"
        )
        connection.execute(
            "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (lock_key,),
        )

    @staticmethod
    def _idempotent_result(
        cursor: Any,
        *,
        tenant_id: str,
        operation: str,
        idempotency_key: str,
        request_digest: str,
        model: type[DefinitionT],
    ) -> DefinitionT | None:
        if not idempotency_key:
            return None
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
            raise RegistryIdempotencyConflict(
                f"idempotency key reused with different schema payload: {idempotency_key}"
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
        result: DefinitionT,
    ) -> None:
        if not idempotency_key:
            return
        cursor.execute(
            """
            insert into runtime.idempotency_records (
              tenant_id, operation, idempotency_key, request_digest,
              result_type, result
            ) values (%s, %s, %s, %s, %s, %s)
            """,
            (
                tenant_id,
                operation,
                idempotency_key,
                request_digest,
                result_type,
                Jsonb(result.model_dump(mode="json")),
            ),
        )

    def register_object_type(
        self,
        tenant_id: str,
        definition: ObjectTypeDefinition,
        *,
        allow_breaking: bool = False,
        idempotency_key: str | None = None,
    ) -> ObjectTypeDefinition:
        tenant = self._tenant(tenant_id)
        operation = "object_type.register"
        clean_idempotency_key = self._idempotency_key(idempotency_key)
        digest = definition_digest(definition, allow_breaking=allow_breaking)
        try:
            with self._pool.connection() as connection, connection.transaction():
                self._bind_tenant(connection, tenant)
                self._lock_idempotency(
                    connection,
                    tenant,
                    operation,
                    clean_idempotency_key,
                )
                self._lock_schema(
                    connection, tenant, "object_type", definition.type_name
                )
                with connection.cursor(row_factory=dict_row) as cursor:
                    reused = self._idempotent_result(
                        cursor,
                        tenant_id=tenant,
                        operation=operation,
                        idempotency_key=clean_idempotency_key,
                        request_digest=digest,
                        model=ObjectTypeDefinition,
                    )
                    if reused is not None:
                        return reused
                    cursor.execute(
                        """
                        select definition
                        from ontology.object_type_versions
                        where tenant_id = %s and type_name = %s
                        order by version desc
                        limit 1
                        """,
                        (tenant, definition.type_name),
                    )
                    row = cursor.fetchone()
                    current = (
                        ObjectTypeDefinition.model_validate(row["definition"])
                        if row is not None
                        else None
                    )
                    if current is not None and not allow_breaking:
                        assert_object_compatible(current, definition)
                    stored = definition.model_copy(
                        update={
                            "version": 1 if current is None else current.version + 1
                        },
                        deep=True,
                    )
                    cursor.execute(
                        """
                        insert into ontology.object_type_versions (
                          tenant_id, type_name, version, definition
                        ) values (%s, %s, %s, %s)
                        """,
                        (
                            tenant,
                            stored.type_name,
                            stored.version,
                            Jsonb(stored.model_dump(mode="json")),
                        ),
                    )
                    self._remember_idempotency(
                        cursor,
                        tenant_id=tenant,
                        operation=operation,
                        idempotency_key=clean_idempotency_key,
                        request_digest=digest,
                        result_type="object_type",
                        result=stored,
                    )
                    return stored.model_copy(deep=True)
        except OntologyRegistryError:
            raise
        except Exception:
            raise OntologyRegistryError(
                "PostgreSQL ontology registry operation failed"
            ) from None

    def get_object_type(
        self,
        tenant_id: str,
        type_name: str,
    ) -> ObjectTypeDefinition:
        tenant = self._tenant(tenant_id)
        name = str(type_name)
        return self._get_definition(
            tenant,
            table="object_type_versions",
            name_column="type_name",
            name=name,
            model=ObjectTypeDefinition,
            not_found=lambda: SchemaNotFound(f"object type not found: {name}"),
        )

    def register_relation_type(
        self,
        tenant_id: str,
        definition: RelationTypeDefinition,
        *,
        allow_breaking: bool = False,
        idempotency_key: str | None = None,
    ) -> RelationTypeDefinition:
        tenant = self._tenant(tenant_id)
        operation = "relation_type.register"
        clean_idempotency_key = self._idempotency_key(idempotency_key)
        digest = definition_digest(definition, allow_breaking=allow_breaking)
        try:
            with self._pool.connection() as connection, connection.transaction():
                self._bind_tenant(connection, tenant)
                self._lock_idempotency(
                    connection,
                    tenant,
                    operation,
                    clean_idempotency_key,
                )
                self._lock_schema(
                    connection,
                    tenant,
                    "relation_type",
                    definition.relation_name,
                )
                with connection.cursor(row_factory=dict_row) as cursor:
                    reused = self._idempotent_result(
                        cursor,
                        tenant_id=tenant,
                        operation=operation,
                        idempotency_key=clean_idempotency_key,
                        request_digest=digest,
                        model=RelationTypeDefinition,
                    )
                    if reused is not None:
                        return reused
                    self._require_object_type(cursor, tenant, definition.source_type)
                    self._require_object_type(cursor, tenant, definition.target_type)
                    cursor.execute(
                        """
                        select definition
                        from ontology.relation_type_versions
                        where tenant_id = %s and relation_name = %s
                        order by version desc
                        limit 1
                        """,
                        (tenant, definition.relation_name),
                    )
                    row = cursor.fetchone()
                    current = (
                        RelationTypeDefinition.model_validate(row["definition"])
                        if row is not None
                        else None
                    )
                    if current is not None and not allow_breaking:
                        assert_relation_compatible(current, definition)
                    stored = definition.model_copy(
                        update={
                            "version": 1 if current is None else current.version + 1
                        },
                        deep=True,
                    )
                    cursor.execute(
                        """
                        insert into ontology.relation_type_versions (
                          tenant_id, relation_name, version, definition
                        ) values (%s, %s, %s, %s)
                        """,
                        (
                            tenant,
                            stored.relation_name,
                            stored.version,
                            Jsonb(stored.model_dump(mode="json")),
                        ),
                    )
                    self._remember_idempotency(
                        cursor,
                        tenant_id=tenant,
                        operation=operation,
                        idempotency_key=clean_idempotency_key,
                        request_digest=digest,
                        result_type="relation_type",
                        result=stored,
                    )
                    return stored.model_copy(deep=True)
        except OntologyRegistryError:
            raise
        except Exception:
            raise OntologyRegistryError(
                "PostgreSQL ontology registry operation failed"
            ) from None

    def get_relation_type(
        self,
        tenant_id: str,
        relation_name: str,
    ) -> RelationTypeDefinition:
        tenant = self._tenant(tenant_id)
        name = str(relation_name)
        return self._get_definition(
            tenant,
            table="relation_type_versions",
            name_column="relation_name",
            name=name,
            model=RelationTypeDefinition,
            not_found=lambda: SchemaNotFound(f"relation type not found: {name}"),
        )

    def register_event_type(
        self,
        tenant_id: str,
        definition: EventTypeDefinition,
        *,
        allow_breaking: bool = False,
        idempotency_key: str | None = None,
    ) -> EventTypeDefinition:
        tenant = self._tenant(tenant_id)
        operation = "event_type.register"
        clean_idempotency_key = self._idempotency_key(idempotency_key)
        digest = definition_digest(definition, allow_breaking=allow_breaking)
        try:
            with self._pool.connection() as connection, connection.transaction():
                self._bind_tenant(connection, tenant)
                self._lock_idempotency(
                    connection,
                    tenant,
                    operation,
                    clean_idempotency_key,
                )
                self._lock_schema(
                    connection, tenant, "event_type", definition.event_name
                )
                with connection.cursor(row_factory=dict_row) as cursor:
                    reused = self._idempotent_result(
                        cursor,
                        tenant_id=tenant,
                        operation=operation,
                        idempotency_key=clean_idempotency_key,
                        request_digest=digest,
                        model=EventTypeDefinition,
                    )
                    if reused is not None:
                        return reused
                    self._require_object_type(cursor, tenant, definition.object_type)
                    cursor.execute(
                        """
                        select definition
                        from ontology.event_type_versions
                        where tenant_id = %s and event_name = %s
                        order by version desc
                        limit 1
                        """,
                        (tenant, definition.event_name),
                    )
                    row = cursor.fetchone()
                    current = (
                        EventTypeDefinition.model_validate(row["definition"])
                        if row is not None
                        else None
                    )
                    if current is not None and not allow_breaking:
                        assert_event_compatible(current, definition)
                    stored = definition.model_copy(
                        update={
                            "version": 1 if current is None else current.version + 1
                        },
                        deep=True,
                    )
                    cursor.execute(
                        """
                        insert into ontology.event_type_versions (
                          tenant_id, event_name, version, definition
                        ) values (%s, %s, %s, %s)
                        """,
                        (
                            tenant,
                            stored.event_name,
                            stored.version,
                            Jsonb(stored.model_dump(mode="json")),
                        ),
                    )
                    self._remember_idempotency(
                        cursor,
                        tenant_id=tenant,
                        operation=operation,
                        idempotency_key=clean_idempotency_key,
                        request_digest=digest,
                        result_type="event_type",
                        result=stored,
                    )
                    return stored.model_copy(deep=True)
        except OntologyRegistryError:
            raise
        except Exception:
            raise OntologyRegistryError(
                "PostgreSQL ontology registry operation failed"
            ) from None

    def get_event_type(
        self,
        tenant_id: str,
        event_name: str,
    ) -> EventTypeDefinition:
        tenant = self._tenant(tenant_id)
        name = str(event_name)
        return self._get_definition(
            tenant,
            table="event_type_versions",
            name_column="event_name",
            name=name,
            model=EventTypeDefinition,
            not_found=lambda: SchemaNotFound(f"event type not found: {name}"),
        )

    @staticmethod
    def _require_object_type(cursor: Any, tenant_id: str, type_name: str) -> None:
        cursor.execute(
            """
            select 1
            from ontology.object_type_versions
            where tenant_id = %s and type_name = %s
            limit 1
            """,
            (tenant_id, type_name),
        )
        if cursor.fetchone() is None:
            raise SchemaNotFound(f"object type not found: {type_name}")

    def _get_definition(
        self,
        tenant_id: str,
        *,
        table: str,
        name_column: str,
        name: str,
        model: type[DefinitionT],
        not_found: Callable[[], SchemaNotFound],
    ) -> DefinitionT:
        queries = {
            ("object_type_versions", "type_name"): """
                select definition
                from ontology.object_type_versions
                where tenant_id = %s and type_name = %s
                order by version desc
                limit 1
            """,
            ("relation_type_versions", "relation_name"): """
                select definition
                from ontology.relation_type_versions
                where tenant_id = %s and relation_name = %s
                order by version desc
                limit 1
            """,
            ("event_type_versions", "event_name"): """
                select definition
                from ontology.event_type_versions
                where tenant_id = %s and event_name = %s
                order by version desc
                limit 1
            """,
        }
        try:
            query = queries[(table, name_column)]
            with self._pool.connection() as connection, connection.transaction():
                self._bind_tenant(connection, tenant_id)
                with connection.cursor(row_factory=dict_row) as cursor:
                    cursor.execute(query, (tenant_id, name))
                    row = cursor.fetchone()
                    if row is None:
                        raise not_found()
                    return model.model_validate(row["definition"])
        except OntologyRegistryError:
            raise
        except Exception:
            raise OntologyRegistryError(
                "PostgreSQL ontology registry operation failed"
            ) from None

    def schema_snapshot(self, tenant_id: str) -> OntologySchemaSnapshot:
        tenant = self._tenant(tenant_id)
        try:
            with self._pool.connection() as connection, connection.transaction():
                connection.execute(
                    "set transaction isolation level repeatable read, read only"
                )
                self._bind_tenant(connection, tenant)
                with connection.cursor(row_factory=dict_row) as cursor:
                    objects = self._latest_definitions(
                        cursor,
                        tenant_id=tenant,
                        kind="object",
                        model=ObjectTypeDefinition,
                    )
                    relations = self._latest_definitions(
                        cursor,
                        tenant_id=tenant,
                        kind="relation",
                        model=RelationTypeDefinition,
                    )
                    events = self._latest_definitions(
                        cursor,
                        tenant_id=tenant,
                        kind="event",
                        model=EventTypeDefinition,
                    )
            revision = schema_revision(objects, relations, events)
            return OntologySchemaSnapshot(
                tenant_id=tenant,
                object_types=tuple(objects),
                relation_types=tuple(relations),
                event_types=tuple(events),
                revision=revision,
            )
        except OntologyRegistryError:
            raise
        except Exception:
            raise OntologyRegistryError(
                "PostgreSQL ontology registry operation failed"
            ) from None

    @staticmethod
    def _latest_definitions(
        cursor: Any,
        *,
        tenant_id: str,
        kind: str,
        model: type[DefinitionT],
    ) -> list[DefinitionT]:
        queries = {
            "object": """
                select distinct on (type_name) definition
                from ontology.object_type_versions
                where tenant_id = %s
                order by type_name, version desc
            """,
            "relation": """
                select distinct on (relation_name) definition
                from ontology.relation_type_versions
                where tenant_id = %s
                order by relation_name, version desc
            """,
            "event": """
                select distinct on (event_name) definition
                from ontology.event_type_versions
                where tenant_id = %s
                order by event_name, version desc
            """,
        }
        cursor.execute(queries[kind], (tenant_id,))
        return [model.model_validate(row["definition"]) for row in cursor.fetchall()]
