from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
import json
import threading
from typing import Any
import uuid

from pydantic import BaseModel, Field

from eios.kernel.ports.repositories import AuditRepository


DEFAULT_REDACTED_KEYS = frozenset(
    {
        "api-key",
        "api_key",
        "authorization",
        "password",
        "presigned_url",
        "secret",
        "token",
        "token_digest",
    }
)


class AuditConflict(RuntimeError):
    code = "audit_conflict"


class AuditEvent(BaseModel):
    event_id: str
    action: str
    tenant_id: str
    actor_id: str
    trace_id: str
    request_id: str
    capability_name: str = ""
    job_id: str = ""
    invocation_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    # 0315: the API key id, or the browser session's credential id, the request
    # came in with.  None for system writers (worker, expiry sweeps).
    credential_id: str | None = None


class InMemoryAuditRepository:
    def __init__(self) -> None:
        self._events: list[AuditEvent] = []
        self._lock = threading.Lock()

    def add(self, event: AuditEvent) -> None:
        self.add_many((event,))

    def add_many(self, events: tuple[AuditEvent, ...] | list[AuditEvent]) -> None:
        detached = [event.model_copy(deep=True) for event in events]
        with self._lock:
            combined = {event.event_id: event for event in self._events}
            for event in detached:
                existing = combined.get(event.event_id)
                if existing is not None and existing != event:
                    raise AuditConflict("audit event metadata conflicts")
                combined[event.event_id] = event
            known = {event.event_id for event in self._events}
            self._events.extend(
                event for event in detached if event.event_id not in known
            )

    def list(self, *, tenant_id: str, action: str | None = None) -> list[AuditEvent]:
        tenant = str(tenant_id or "").strip()
        if not tenant:
            raise ValueError("tenant_id is required")
        with self._lock:
            items = [item.model_copy(deep=True) for item in self._events]
        items = [item for item in items if item.tenant_id == tenant]
        if action is not None:
            items = [item for item in items if item.action == action]
        return sorted(items, key=lambda item: (item.created_at, item.event_id))


class AuditService:
    def __init__(
        self,
        repository: AuditRepository,
        *,
        max_string_length: int = 4000,
        redacted_keys: frozenset[str] = DEFAULT_REDACTED_KEYS,
    ) -> None:
        self._repository = repository
        self._max_string_length = max(0, int(max_string_length))
        self._redacted_keys = frozenset(key.lower() for key in redacted_keys)

    @classmethod
    def _normalize(cls, value: Any) -> Any:
        """Expand codec-supported containers before any secret inspection."""

        if isinstance(value, BaseModel):
            return cls._normalize(value.model_dump(mode="python"))
        if isinstance(value, Enum):
            return cls._normalize(value.value)
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise TypeError("audit payload object keys must be strings")
            return {key: cls._normalize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._normalize(item) for item in value]
        if isinstance(value, (set, frozenset)):
            normalized = [cls._normalize(item) for item in value]
            return sorted(
                normalized,
                key=lambda item: json.dumps(
                    item,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ),
            )
        if (
            isinstance(value, datetime)
            or value is None
            or isinstance(value, (str, int, float, bool))
        ):
            return value
        raise TypeError(f"unsupported audit payload type: {type(value).__name__}")

    def _sanitize(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): "<redacted>"
                if str(key).lower() in self._redacted_keys
                else self._sanitize(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self._sanitize(item) for item in value]
        if (
            isinstance(value, str)
            and self._max_string_length
            and len(value) > self._max_string_length
        ):
            suffix = "...<truncated>"
            prefix_length = max(0, self._max_string_length - len(suffix))
            return value[:prefix_length] + suffix
        return value

    def record(
        self,
        *,
        action: str,
        tenant_id: str,
        actor_id: str,
        trace_id: str,
        request_id: str,
        capability_name: str = "",
        job_id: str = "",
        invocation_id: str = "",
        payload: dict[str, Any] | None = None,
        credential_id: str | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            event_id=f"audit_{uuid.uuid4().hex}",
            action=action,
            tenant_id=tenant_id,
            actor_id=actor_id,
            trace_id=trace_id,
            request_id=request_id,
            capability_name=capability_name,
            job_id=job_id,
            invocation_id=invocation_id,
            payload=self._sanitize(self._normalize(payload or {})),
            created_at=datetime.now(UTC),
            credential_id=(credential_id or None),
        )
        self._repository.add(event)
        return event
