from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from threading import Lock
from typing import Protocol

from .errors import AuthorizationUnavailable
from .intersection import IntersectionResult
from .policy.registry import PolicyObligationKind


@dataclass(frozen=True)
class RevisionValidation:
    valid: bool
    trusted_now: datetime
    event_high_water: int

    def __post_init__(self) -> None:
        if type(self.valid) is not bool or type(self.event_high_water) is not int:
            raise ValueError("revision validation is invalid")
        if self.trusted_now.tzinfo is None or self.trusted_now.utcoffset() is None:
            raise ValueError("trusted_now must be timezone aware")


class RevisionVectorValidator(Protocol):
    def validate(
        self, *, tenant_id: str, revision_vector: object
    ) -> RevisionValidation: ...


@dataclass(frozen=True)
class _Entry:
    tenant_id: str
    revision_vector: object
    result: IntersectionResult
    expires_at: datetime
    event_high_water: int


def _identity(payload: object) -> str:
    query = payload.query
    authentication = query.authentication
    agent = query.agent_invocation
    graph = payload.resource_graph
    material = repr(
        (
            query.tenant_id,
            (authentication.subject_id, authentication.subject_revision),
            authentication.membership_revision,
            (
                authentication.credential_id,
                authentication.credential_revision,
                authentication.credential_epoch,
                getattr(authentication, "session_id", None),
                getattr(authentication, "session_revision", None),
            ),
            (
                authentication.caller_application_id,
                authentication.caller_application_version,
                authentication.caller_application_digest,
            ),
            None if agent is None else (
                agent.agent_id,
                agent.agent_revision,
                agent.release_id,
                agent.release_revision,
                agent.release_digest,
                agent.agent_application_id,
                agent.agent_application_version,
                agent.agent_application_digest,
            ),
            ()
            if payload.agent_release is None
            else tuple(
                (
                    item.release_id,
                    item.revision,
                    item.release_digest,
                    item.application.record_digest,
                )
                for item in payload.agent_release.parent_chain
            ),
            (
                ((_resource_tuple(graph.root.resource)), query.target.operation.value),
                tuple(
                    (_resource_tuple(edge.target), edge.required_operation.value)
                    for edge in graph.dependencies
                ),
            ),
            tuple(sorted(authentication.requested_scopes)),
            tuple(sorted(query.request_attributes.items())),
        )
    )
    return sha256(material.encode("utf-8")).hexdigest()


def _resource_tuple(resource: object) -> tuple[str, str, str]:
    return (resource.tenant_id, resource.resource_type.value, resource.resource_id)


def _material(context: object) -> tuple[str, str, object, datetime, int]:
    context.verify_integrity()
    return (
        _identity(context),
        context.query.tenant_id,
        context.revision_vector,
        context.trusted_now,
        next(
            (
                entry.revision
                for entry in context.revision_vector.entries
                if entry.category == "event_high_water"
            ),
            0,
        ),
    )


def _obligation_ttl(result: IntersectionResult) -> int | None:
    values: list[int] = []
    for obligation in result.obligations:
        if obligation.kind is PolicyObligationKind.MAX_CACHE_TTL:
            values.extend(
                entry.value
                for entry in obligation.config
                if entry.name == "seconds" and type(entry.value) is int
            )
    return min(values) if values else None


class AuthorizationDecisionCache:
    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._lock = Lock()
        self._tenant_high_water: dict[str, int] = {}

    def get(
        self, context: object, validator: RevisionVectorValidator
    ) -> IntersectionResult | None:
        key, tenant_id, _vector, _now, _event = _material(context)
        with self._lock:
            entry = self._entries.get(key)
        if entry is None:
            return None
        try:
            checked = validator.validate(
                tenant_id=tenant_id, revision_vector=entry.revision_vector
            )
            trusted_now = checked.trusted_now.astimezone(UTC)
            valid = (
                checked.valid
                and entry.tenant_id == tenant_id
                and trusted_now < entry.expires_at
                and checked.event_high_water == entry.event_high_water
                and checked.event_high_water
                >= self._tenant_high_water.get(tenant_id, 0)
            )
        except Exception:
            with self._lock:
                self._entries.pop(key, None)
            raise AuthorizationUnavailable(
                "revision vector validation is unavailable"
            ) from None
        if not valid:
            with self._lock:
                self._entries.pop(key, None)
            return None
        return entry.result

    def put(self, context: object, result: IntersectionResult) -> None:
        key, tenant_id, vector, trusted_now, event_high_water = _material(context)
        base_seconds = 5 if result.allowed else 1
        obligation_seconds = _obligation_ttl(result)
        seconds = min(base_seconds, obligation_seconds or base_seconds)
        expires_at = min(result.expires_at, trusted_now + timedelta(seconds=seconds))
        if expires_at <= trusted_now:
            return
        with self._lock:
            self._entries[key] = _Entry(
                tenant_id=tenant_id,
                revision_vector=vector,
                result=result,
                expires_at=expires_at,
                event_high_water=event_high_water,
            )

    def evict_tenant(self, tenant_id: str, *, event_high_water: int) -> None:
        with self._lock:
            self._tenant_high_water[tenant_id] = max(
                event_high_water, self._tenant_high_water.get(tenant_id, 0)
            )
            self._entries = {
                key: entry
                for key, entry in self._entries.items()
                if entry.tenant_id != tenant_id
                or entry.event_high_water >= event_high_water
            }


__all__ = [
    "AuthorizationDecisionCache",
    "RevisionValidation",
    "RevisionVectorValidator",
]
