from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, TypedDict, Unpack, runtime_checkable

from eios.capabilities.models import CapabilityType, ExecutionMode

if TYPE_CHECKING:
    from eios.ontology.models import (
        EventTypeDefinition,
        ObjectTypeDefinition,
        OntologySchemaSnapshot,
        RelationTypeDefinition,
    )
    from eios.ontology.store import (
        EventRecord,
        ObjectProjection,
        ObjectRecord,
        ObjectTombstone,
        RelationRecord,
    )
    from eios.runtime.artifacts import (
        ArtifactDeleteFailedCommand,
        ArtifactDeleteResultCommand,
        ArtifactFailCommand,
        ArtifactFinalizeCommand,
        ArtifactGcClaim,
        ArtifactGcClaimCommand,
        ArtifactInternalRecord,
        ArtifactLegalHoldCommand,
        ArtifactReconcileCommand,
        ArtifactReference,
        ArtifactReservation,
        ArtifactReserveCommand,
    )
    from eios.runtime.audit import AuditEvent
    from eios.runtime.jobs import JobEvent, JobSnapshot


class _RequiredJobCreateFields(TypedDict):
    capability_name: str
    capability_version: str
    capability_schema_hash: str
    capability_contract_revision: str
    pack_revision: str
    capability_type: CapabilityType
    execution_mode: ExecutionMode
    tenant_id: str
    actor_id: str
    trace_id: str
    request_id: str
    invocation_id: str
    idempotency_key: str | None
    normalized_input: dict[str, Any]
    plan_snapshot: dict[str, Any]
    queue: str


class JobCreateFields(_RequiredJobCreateFields, total=False):
    invocation_input_digest: str
    invocation_idempotency_key: str | None
    priority: int
    max_attempts: int
    available_at: datetime
    cancel_requested_at: datetime | None
    fencing_token: int
    dead_letter_reason: dict[str, Any] | None


JOB_CREATE_REQUIRED_FIELDS = frozenset(JobCreateFields.__required_keys__)


@runtime_checkable
class JobStore(Protocol):
    def create_or_get(
        self, **fields: Unpack[JobCreateFields]
    ) -> tuple[JobSnapshot, bool]: ...

    def get(self, job_id: str, *, tenant_id: str) -> JobSnapshot | None: ...

    def get_by_invocation_id(
        self, invocation_id: str, *, tenant_id: str
    ) -> JobSnapshot | None: ...

    def list(self, *, tenant_id: str) -> list[JobSnapshot]: ...

    def events(self, job_id: str, *, tenant_id: str) -> list[JobEvent]: ...

    def mark_running(self, job_id: str, *, tenant_id: str) -> JobSnapshot: ...

    def mark_succeeded(
        self,
        job_id: str,
        *,
        tenant_id: str,
        result: dict[str, Any],
        artifact_refs: tuple[dict[str, Any], ...] = (),
    ) -> JobSnapshot: ...

    def mark_failed(
        self, job_id: str, *, tenant_id: str, error: dict[str, Any]
    ) -> JobSnapshot: ...

    def mark_cancelled(
        self,
        job_id: str,
        *,
        tenant_id: str,
        error: dict[str, Any] | None = None,
    ) -> JobSnapshot: ...


@runtime_checkable
class AuditRepository(Protocol):
    def add(self, event: AuditEvent) -> None: ...

    def list(
        self,
        *,
        tenant_id: str,
        action: str | None = None,
    ) -> list[AuditEvent]: ...


@runtime_checkable
class ArtifactRepository(Protocol):
    def add(self, reference: ArtifactReference) -> None: ...

    def get(self, artifact_id: str, *, tenant_id: str) -> ArtifactReference | None: ...

    def list(self, *, tenant_id: str) -> list[ArtifactReference]: ...


@runtime_checkable
class ArtifactMetadataRepository(Protocol):
    def reserve(self, command: ArtifactReserveCommand) -> ArtifactReservation: ...

    def finalize(self, command: ArtifactFinalizeCommand) -> ArtifactInternalRecord: ...

    def fail(self, command: ArtifactFailCommand) -> ArtifactInternalRecord: ...

    def get(
        self, artifact_id: str, *, tenant_id: str
    ) -> ArtifactInternalRecord | None: ...

    def list(self, *, tenant_id: str) -> list[ArtifactInternalRecord]: ...


@runtime_checkable
class ArtifactRetentionRepository(Protocol):
    def set_legal_hold(
        self, command: ArtifactLegalHoldCommand
    ) -> ArtifactInternalRecord: ...

    def claim(self, command: ArtifactGcClaimCommand) -> tuple[ArtifactGcClaim, ...]: ...

    def claim_is_current(self, claim: ArtifactGcClaim) -> bool: ...

    def mark_deleted(self, command: ArtifactDeleteResultCommand) -> ArtifactInternalRecord: ...

    def mark_delete_failed(
        self, command: ArtifactDeleteFailedCommand
    ) -> ArtifactInternalRecord: ...

    def reconcile(self, command: ArtifactReconcileCommand) -> ArtifactInternalRecord: ...


@runtime_checkable
class OntologyRegistry(Protocol):
    def register_object_type(
        self,
        tenant_id: str,
        definition: ObjectTypeDefinition,
        *,
        allow_breaking: bool = False,
        idempotency_key: str | None = None,
    ) -> ObjectTypeDefinition: ...

    def get_object_type(
        self,
        tenant_id: str,
        type_name: str,
    ) -> ObjectTypeDefinition: ...

    def register_relation_type(
        self,
        tenant_id: str,
        definition: RelationTypeDefinition,
        *,
        allow_breaking: bool = False,
        idempotency_key: str | None = None,
    ) -> RelationTypeDefinition: ...

    def get_relation_type(
        self,
        tenant_id: str,
        relation_name: str,
    ) -> RelationTypeDefinition: ...

    def register_event_type(
        self,
        tenant_id: str,
        definition: EventTypeDefinition,
        *,
        allow_breaking: bool = False,
        idempotency_key: str | None = None,
    ) -> EventTypeDefinition: ...

    def get_event_type(
        self,
        tenant_id: str,
        event_name: str,
    ) -> EventTypeDefinition: ...

    def schema_snapshot(self, tenant_id: str) -> OntologySchemaSnapshot: ...


@runtime_checkable
class OntologyStore(Protocol):
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
    ) -> ObjectRecord: ...

    def get_object(
        self,
        tenant_id: str,
        type_name: str,
        object_id: str,
        *,
        as_of: datetime | None = None,
    ) -> ObjectProjection: ...

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
        """Objects ordered by ``(type_name, object_id)``.

        ``type_names`` is the multi-value form of ``type_name`` (one merged,
        ordered stream over several types); passing both is a caller error.

        ``limit``/``after`` enable keyset pagination: ``after`` is the last
        ``(type_name, object_id)`` seen and the result starts strictly past it.
        Both default to the legacy "return every object" behaviour.

        ``court_id``/``starts_after``/``starts_before`` filter on the JSONB
        properties of the same name (``start_at`` compared as text against the
        store's uniform ``+08:00`` ISO form; ``starts_after`` inclusive,
        ``starts_before`` exclusive). Objects without the property never match
        a range filter.

        ``caller_markings`` 为 None 时不做 mandatory access marking 过滤 ——
        那是内部编排(物化/seed/对账)用的。任何面向调用方的列举入口都必须
        显式传 frozenset:传空集是"没有任何 clearance",不是"不过滤"。
        """
        ...

    def list_objects_by_ids(
        self,
        tenant_id: str,
        type_name: str,
        object_ids: tuple[str, ...],
    ) -> list[ObjectRecord]:
        """One bounded batch point-read, ordered by ``object_id``.

        Missing ids are simply absent from the result — the caller decides
        whether that is an error. Saves the N round trips a batch of single
        reads would cost."""
        ...

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
        """Total objects for the tenant (optionally one type, optionally under
        the same property filters as ``list_objects``). Backs the record-count
        header without materialising every page."""
        ...

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
        """Objects with ``updated_at`` strictly after ``updated_since``,
        ordered by ``(updated_at, type_name, object_id)`` — the incremental
        change-feed order. ``after`` is a keyset over the same triple. The
        property filters carry the same semantics as ``list_objects``."""
        ...

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
        """Deletion tombstones with ``deleted_at`` strictly after
        ``deleted_since``, ordered by ``(deleted_at, type_name, object_id)`` so
        the tombstone stream merges with ``list_changed_objects`` under one
        cursor. Tombstones are retained for ``TOMBSTONE_RETENTION_DAYS`` days.

        The property filters apply to the tombstone's own ``court_id`` /
        ``start_at`` snapshot, taken at deletion time — that snapshot is what
        makes a filtered change feed deletion-safe."""
        ...

    def list_relations(
        self,
        tenant_id: str,
        *,
        relation_name: str | None = None,
        limit: int | None = None,
        after: str | None = None,
    ) -> list[RelationRecord]:
        """Active relations ordered by ``relation_id``.

        ``limit``/``after`` enable keyset pagination: ``after`` is the last
        ``relation_id`` seen and the result starts strictly past it. Backs
        the bulk "link already exists" precheck in set-based projections.
        """
        ...

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
    ) -> RelationRecord: ...

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
    ) -> RelationRecord: ...

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
    ) -> EventRecord: ...
