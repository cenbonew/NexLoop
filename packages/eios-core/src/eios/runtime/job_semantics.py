from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import Enum
import json
from types import MappingProxyType
from typing import Any, Final, Protocol

from pydantic import BaseModel, ConfigDict

from eios.capabilities.models import CapabilityType, ExecutionMode
from eios.kernel.ports.repositories import JobCreateFields


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEAD_LETTERED = "dead_lettered"


JOB_ALLOWED_TRANSITIONS: Final[Mapping[JobStatus, frozenset[JobStatus]]] = (
    MappingProxyType(
        {
            JobStatus.PENDING: frozenset(
                {JobStatus.RUNNING, JobStatus.FAILED, JobStatus.CANCELLED}
            ),
            JobStatus.RUNNING: frozenset(
                {
                    JobStatus.RETRY_WAIT,
                    JobStatus.SUCCEEDED,
                    JobStatus.FAILED,
                    JobStatus.CANCELLED,
                    JobStatus.DEAD_LETTERED,
                }
            ),
            JobStatus.RETRY_WAIT: frozenset(
                {JobStatus.RUNNING, JobStatus.CANCELLED, JobStatus.DEAD_LETTERED}
            ),
            JobStatus.SUCCEEDED: frozenset(),
            JobStatus.FAILED: frozenset(),
            JobStatus.CANCELLED: frozenset(),
            JobStatus.DEAD_LETTERED: frozenset(),
        }
    )
)

JOB_REQUEST_OBSERVABILITY_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "actor_id",
        "trace_id",
        "request_id",
        "invocation_input_digest",
        "invocation_idempotency_key",
    }
)

# These fields are owned by the scheduler after enqueue.  They are accepted on
# the legacy JobStore boundary for forward compatibility, but are deliberately
# excluded from the immutable idempotency request contract.
JOB_CREATE_RUNTIME_STATE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "cancel_requested_at",
        "fencing_token",
        "dead_letter_reason",
    }
)


class JobRequestContract(BaseModel):
    """Immutable, durable portion of a JobStore.create_or_get request."""

    model_config = ConfigDict(frozen=True)

    capability_name: str
    capability_version: str
    capability_schema_hash: str
    capability_contract_revision: str
    pack_revision: str
    capability_type: CapabilityType
    execution_mode: ExecutionMode
    tenant_id: str
    invocation_id: str
    idempotency_key: str | None
    normalized_input: dict[str, Any]
    plan_snapshot: dict[str, Any]
    queue: str
    priority: int = 0
    max_attempts: int = 3
    available_at: datetime | None = None


def _typed_dict_fields(create_fields: type) -> frozenset[str]:
    required = getattr(create_fields, "__required_keys__", frozenset())
    optional = getattr(create_fields, "__optional_keys__", frozenset())
    return frozenset(required) | frozenset(optional)


def assert_job_request_contract_complete(
    create_fields: type = JobCreateFields,
) -> None:
    declared = _typed_dict_fields(create_fields)
    classified = JOB_REQUEST_OBSERVABILITY_FIELDS | JOB_CREATE_RUNTIME_STATE_FIELDS
    unknown_classifications = classified - declared
    if unknown_classifications:
        raise RuntimeError(
            "JobCreateFields classifications reference unknown fields: "
            f"{sorted(unknown_classifications)}"
        )
    expected = declared - classified
    actual = frozenset(JobRequestContract.model_fields)
    if expected != actual:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise RuntimeError(
            "JobRequestContract does not cover JobCreateFields: "
            f"missing={missing}, unexpected={unexpected}"
        )


assert_job_request_contract_complete()


class JobRequestSnapshot(Protocol):
    capability_name: str
    capability_version: str
    capability_schema_hash: str
    capability_contract_revision: str
    pack_revision: str
    capability_type: object
    execution_mode: object
    tenant_id: str
    invocation_id: str
    idempotency_key: str | None
    normalized_input: dict[str, Any]
    plan_snapshot: dict[str, Any]
    queue: str
    priority: int
    max_attempts: int
    requested_available_at: datetime | None
    available_at: datetime


_CANONICAL_KEY = "__eios_job_request_value__"


def _descriptor(kind: str, value: Any) -> dict[str, Any]:
    return {_CANONICAL_KEY: {"type": kind, "value": value}}


def canonicalize_job_request_value(value: Any) -> Any:
    """Canonicalize request evidence identically for every storage adapter."""

    if isinstance(value, datetime):
        normalized = (
            value
            if value.tzinfo is None or value.utcoffset() is None
            else value.astimezone(UTC)
        )
        return _descriptor("datetime", normalized.isoformat())
    if isinstance(value, BaseModel):
        return canonicalize_job_request_value(value.model_dump(mode="python"))
    if isinstance(value, Enum):
        return canonicalize_job_request_value(value.value)
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        encoded = {
            key: canonicalize_job_request_value(item) for key, item in value.items()
        }
        if _CANONICAL_KEY in value:
            return _descriptor("dict", encoded)
        return encoded
    if isinstance(value, (list, tuple)):
        return [canonicalize_job_request_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        encoded = [canonicalize_job_request_value(item) for item in value]
        return sorted(
            encoded,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported JSON value type: {type(value).__name__}")


def _contract_from_fields(fields: Mapping[str, Any]) -> JobRequestContract:
    required = frozenset(JobCreateFields.__required_keys__)
    declared = required | frozenset(JobCreateFields.__optional_keys__)
    provided = frozenset(fields)
    if not required <= provided or not provided <= declared:
        missing = sorted(required - provided)
        unexpected = sorted(provided - declared)
        raise RuntimeError(
            "job create fields do not match JobCreateFields: "
            f"missing={missing}, unexpected={unexpected}"
        )
    payload = {
        name: fields[name] for name in JobRequestContract.model_fields if name in fields
    }
    payload["idempotency_key"] = (
        str(fields.get("idempotency_key") or "").strip() or None
    )
    return JobRequestContract.model_validate(payload)


def _contract_from_snapshot(
    snapshot: JobRequestSnapshot,
) -> JobRequestContract:
    payload = {
        name: getattr(snapshot, name) for name in JobRequestContract.model_fields
    }
    # Compare the caller's immutable raw scheduling intent, not the mutable
    # retry schedule stored in ``available_at``.
    payload["available_at"] = snapshot.requested_available_at
    return JobRequestContract.model_validate(payload)


def job_requests_match(
    snapshot: JobRequestSnapshot,
    fields: Mapping[str, Any],
) -> bool:
    """Compare the durable part of an idempotent job request."""

    assert_job_request_contract_complete()
    return canonicalize_job_request_value(
        _contract_from_snapshot(snapshot)
    ) == canonicalize_job_request_value(_contract_from_fields(fields))
