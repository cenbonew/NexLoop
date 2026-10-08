"""Atomic, revision-fenced projection of ontology objects.

The port is intentionally smaller than :class:`OntologyStore`: an Action can
publish a finite object batch without gaining access to unrelated ontology
mutation operations.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from eios.ontology.store import ObjectRecord


class ObjectProjectionRevisionConflict(RuntimeError):
    """The projected object's current revision does not match its CAS fence."""

    code = "ontology_projection_revision_conflict"


class AtomicObjectUpsert(BaseModel):
    """One object write in an atomic projection batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    type_name: str
    object_id: str
    properties: dict[str, Any]
    idempotency_key: str
    expected_revision: int = Field(ge=0, strict=True)
    source_system: str = ""
    source_ref: str = ""
    actor_id: str = ""
    markings: tuple[str, ...] = ()

    @field_validator("tenant_id", "type_name", "object_id", "idempotency_key")
    @classmethod
    def _require_non_blank(cls, value: str) -> str:
        clean = str(value).strip()
        if not clean:
            raise ValueError("value must not be blank")
        return clean


@runtime_checkable
class AtomicOntologyProjectionPort(Protocol):
    """Persist one non-empty, single-tenant object batch all-or-nothing."""

    def upsert_objects_atomically(
        self,
        items: Sequence[AtomicObjectUpsert],
    ) -> tuple[ObjectRecord, ...]: ...


def validate_atomic_projection_batch(
    items: Sequence[AtomicObjectUpsert],
) -> tuple[AtomicObjectUpsert, ...]:
    batch = tuple(items)
    if not batch:
        raise ValueError("atomic projection requires at least one item")
    tenants = {item.tenant_id for item in batch}
    if len(tenants) != 1:
        raise ValueError("atomic projection batch must belong to one tenant")
    return batch


__all__ = [
    "AtomicObjectUpsert",
    "AtomicOntologyProjectionPort",
    "ObjectProjectionRevisionConflict",
]
