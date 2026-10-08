from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import Enum
import os
from pathlib import Path
import re
import tempfile
import threading
from types import MappingProxyType
from typing import Any, Literal, Mapping, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictInt,
    field_validator,
    model_validator,
)

from eios.kernel.ports.repositories import ArtifactRepository


class ArtifactNotFound(LookupError):
    pass


class ArtifactConflict(RuntimeError):
    code = "artifact_conflict"


MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class ArtifactLifecycleStatus(str, Enum):
    PENDING = "pending"
    AVAILABLE = "available"
    FAILED = "failed"


class RetentionStatus(str, Enum):
    ACTIVE = "active"
    GC_ELIGIBLE = "gc_eligible"
    DELETING = "deleting"
    DELETED = "deleted"
    DELETE_FAILED = "delete_failed"


_TERMINAL_INVOCATION_STATUSES = frozenset(
    {"succeeded", "failed", "rejected", "cancelled"}
)
_TERMINAL_JOB_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "dead_lettered"}
)


class ArtifactRetentionTerminalProof(BaseModel):
    """Database-clock proof for one Artifact's terminal retention owner."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    tenant_id: str = Field(min_length=1, max_length=512)
    artifact_id: str = Field(min_length=1, max_length=128)
    retention_until: AwareDatetime | None
    database_time: AwareDatetime
    origin_invocation_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    invocation_status: str | None = None
    origin_job_id: str | None = Field(default=None, min_length=1, max_length=128)
    job_status: str | None = None

    @property
    def owner_is_terminal(self) -> bool:
        return (
            self.origin_invocation_id is not None
            and self.invocation_status in _TERMINAL_INVOCATION_STATUSES
            and self.origin_job_id is not None
            and self.job_status in _TERMINAL_JOB_STATUSES
        )

    @property
    def deadline_is_expired(self) -> bool:
        return (
            self.retention_until is not None
            and self.retention_until <= self.database_time
        )


class ArtifactLocation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    bucket: str = Field(min_length=1, max_length=63)
    object_key: str = Field(min_length=1, max_length=1024)

    @field_validator("bucket")
    @classmethod
    def _valid_bucket(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*[a-z0-9]", value):
            raise ValueError("bucket is invalid")
        return value

    @field_validator("object_key")
    @classmethod
    def _valid_object_key(cls, value: str) -> str:
        if (
            value.startswith(("/", "\\"))
            or "\\" in value
            or "\x00" in value
            or "\r" in value
            or "\n" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("object key is invalid")
        return value


class ArtifactUploadRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    location: ArtifactLocation
    content_type: str = Field(min_length=1, max_length=255)
    size_bytes: StrictInt = Field(ge=0, le=MAX_ARTIFACT_BYTES)
    payload_sha256: str
    payload: bytes = Field(repr=False)

    @field_validator("payload_sha256")
    @classmethod
    def _valid_digest(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("payload_sha256 is invalid")
        return value

    @model_validator(mode="after")
    def _payload_matches_metadata(self) -> Self:
        if len(self.payload) != self.size_bytes:
            raise ValueError("payload size does not match metadata")
        if hashlib.sha256(self.payload).hexdigest() != self.payload_sha256:
            raise ValueError("payload checksum does not match metadata")
        return self


class ArtifactUploadReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    location: ArtifactLocation
    size_bytes: StrictInt = Field(ge=0, le=MAX_ARTIFACT_BYTES)
    payload_sha256: str
    etag: str = Field(min_length=1, max_length=512)
    crc64: str = Field(pattern=r"^[0-9]+$", min_length=1, max_length=32)
    request_id: str = Field(min_length=1, max_length=128)

    @field_validator("payload_sha256")
    @classmethod
    def _valid_digest(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("payload_sha256 is invalid")
        return value

    @field_validator("request_id")
    @classmethod
    def _valid_request_id(cls, value: str) -> str:
        if not _SAFE_REQUEST_ID_RE.fullmatch(value):
            raise ValueError("request_id is invalid")
        return value


class ArtifactObjectStat(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    location: ArtifactLocation
    size_bytes: StrictInt = Field(ge=0, le=MAX_ARTIFACT_BYTES)
    payload_sha256: str
    etag: str = Field(default="", max_length=512)
    crc64: str = Field(default="", pattern=r"^[0-9]*$", max_length=32)
    request_id: str = Field(default="", max_length=128)

    @field_validator("payload_sha256")
    @classmethod
    def _valid_digest(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("payload_sha256 is invalid")
        return value

    @field_validator("request_id")
    @classmethod
    def _valid_request_id(cls, value: str) -> str:
        if value and not _SAFE_REQUEST_ID_RE.fullmatch(value):
            raise ValueError("request_id is invalid")
        return value


class ArtifactPresignedResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    url: SecretStr = Field(repr=False)
    expires_at: AwareDatetime
    checksum: str

    @field_validator("checksum")
    @classmethod
    def _valid_checksum(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("checksum is invalid")
        return value

    def __repr__(self) -> str:
        return "ArtifactPresignedResponse(url=<redacted>)"

    __str__ = __repr__


class ArtifactReservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    artifact_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=512)
    location: ArtifactLocation
    upload_owner_id: str = Field(min_length=1, max_length=128)
    upload_fencing_token: StrictInt = Field(ge=1)
    lease_expires_at: AwareDatetime
    upload_required: bool


class ArtifactGcClaim(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    artifact_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=512)
    location: ArtifactLocation
    gc_owner_id: str = Field(min_length=1, max_length=128)
    gc_fencing_token: StrictInt = Field(ge=1)
    gc_claim_id: str = Field(min_length=1, max_length=128)
    lease_expires_at: AwareDatetime


class ArtifactCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    command_id: str = Field(min_length=1, max_length=128)
    actor_id: str = Field(min_length=1, max_length=128)
    trace_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=128)

    @field_validator("request_id")
    @classmethod
    def _valid_request_id(cls, value: str) -> str:
        if not _SAFE_REQUEST_ID_RE.fullmatch(value):
            raise ValueError("request_id is invalid")
        return value


class ArtifactReserveCommand(ArtifactCommand):

    tenant_id: str = Field(min_length=1, max_length=512)
    invocation_id: str = Field(min_length=1, max_length=128)
    job_id: str | None = Field(default=None, min_length=1, max_length=128)
    result_slot: str = Field(min_length=1, max_length=128)
    capability_name: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=255)
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: StrictInt = Field(ge=0, le=MAX_ARTIFACT_BYTES)
    producer_attempt_id: str | None = Field(default=None, min_length=1, max_length=128)
    producer_job_fencing_token: StrictInt | None = Field(default=None, ge=1)
    upload_owner_id: str = Field(min_length=1, max_length=128)
    lease_seconds: StrictInt = Field(ge=1, le=3600)
    retention_until: AwareDatetime | None = None

    @model_validator(mode="after")
    def _valid_origin(self) -> Self:
        async_fields = (
            self.job_id,
            self.producer_attempt_id,
            self.producer_job_fencing_token,
        )
        if any(value is not None for value in async_fields) and not all(
            value is not None for value in async_fields
        ):
            raise ValueError("async producer evidence is incomplete")
        return self


class ArtifactFinalizeCommand(ArtifactCommand):
    tenant_id: str = Field(min_length=1, max_length=512)
    artifact_id: str = Field(min_length=1, max_length=128)
    upload_owner_id: str = Field(min_length=1, max_length=128)
    upload_fencing_token: StrictInt = Field(ge=1)
    receipt: ArtifactUploadReceipt


class ArtifactFailCommand(ArtifactCommand):
    tenant_id: str = Field(min_length=1, max_length=512)
    artifact_id: str = Field(min_length=1, max_length=128)
    upload_owner_id: str = Field(min_length=1, max_length=128)
    upload_fencing_token: StrictInt = Field(ge=1)
    failure_code: str = Field(pattern=r"^artifact_[a-z0-9_]{1,96}$")


class ArtifactGcClaimCommand(ArtifactCommand):
    tenant_id: str = Field(min_length=1, max_length=512)
    gc_owner_id: str = Field(min_length=1, max_length=128)
    gc_claim_id: str = Field(min_length=1, max_length=128)
    batch_size: StrictInt = Field(ge=1, le=100)
    lease_seconds: StrictInt = Field(ge=1, le=3600)


class ArtifactDeleteResultCommand(ArtifactCommand):
    tenant_id: str = Field(min_length=1, max_length=512)
    artifact_id: str = Field(min_length=1, max_length=128)
    gc_owner_id: str = Field(min_length=1, max_length=128)
    gc_fencing_token: StrictInt = Field(ge=1)
    gc_claim_id: str = Field(min_length=1, max_length=128)


class ArtifactDeleteFailedCommand(ArtifactDeleteResultCommand):
    failure_code: str = Field(pattern=r"^artifact_[a-z0-9_]{1,96}$")


class ArtifactReconcileCommand(ArtifactCommand):
    tenant_id: str = Field(min_length=1, max_length=512)
    artifact_id: str = Field(min_length=1, max_length=128)
    phase: Literal["claim", "apply"]
    reconcile_owner_id: str = Field(min_length=1, max_length=128)
    fencing_token: StrictInt = Field(ge=0)
    outcome: Literal["present_valid", "absent", "mismatch", "unknown"] | None = None

    @model_validator(mode="after")
    def _valid_phase(self) -> Self:
        if (self.phase == "claim") != (self.outcome is None):
            raise ValueError("reconcile phase and outcome conflict")
        return self


class ArtifactLegalHoldCommand(ArtifactCommand):
    tenant_id: str = Field(min_length=1, max_length=512)
    artifact_id: str = Field(min_length=1, max_length=128)
    enabled: bool


class ArtifactCredentials(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    access_key_id: SecretStr = Field(repr=False)
    secret_access_key: SecretStr = Field(repr=False)
    session_token: SecretStr | None = Field(default=None, repr=False)
    expires_at: AwareDatetime

    def __repr__(self) -> str:
        return "ArtifactCredentials(<redacted>)"

    __str__ = __repr__


class ArtifactStorageError(RuntimeError):
    """Storage-boundary error with no arbitrary message or raw exception data."""

    def __init__(self, *, code: str, retryable: bool, request_id: str = "") -> None:
        if not re.fullmatch(r"artifact_[a-z0-9_]{1,96}", code):
            raise ValueError("code is invalid")
        if type(retryable) is not bool:
            raise ValueError("retryable must be a boolean")
        if request_id and not _SAFE_REQUEST_ID_RE.fullmatch(request_id):
            raise ValueError("request_id is invalid")
        self.code = code
        self.retryable = retryable
        self.request_id = request_id
        super().__init__(code)

    @property
    def safe_metadata(self) -> dict[str, str]:
        return {"request_id": self.request_id} if self.request_id else {}

    def __repr__(self) -> str:
        return (
            f"ArtifactStorageError(code={self.code!r}, "
            f"retryable={self.retryable!r})"
        )


class ArtifactStorageNotFoundError(ArtifactStorageError):
    def __init__(self, *, request_id: str = "") -> None:
        super().__init__(code="artifact_not_found", retryable=False, request_id=request_id)


class ArtifactStorageGoneError(ArtifactStorageError):
    def __init__(self, *, request_id: str = "") -> None:
        super().__init__(code="artifact_gone", retryable=False, request_id=request_id)


class ArtifactStorageConflictError(ArtifactStorageError):
    def __init__(self, *, request_id: str = "") -> None:
        super().__init__(code="artifact_conflict", retryable=False, request_id=request_id)


class ArtifactIntegrityError(ArtifactStorageError):
    def __init__(self, *, request_id: str = "") -> None:
        super().__init__(
            code="artifact_integrity_mismatch",
            retryable=False,
            request_id=request_id,
        )


class ArtifactTooLargeError(ArtifactStorageError):
    def __init__(self) -> None:
        super().__init__(code="artifact_too_large", retryable=False)


class ArtifactRateLimitedError(ArtifactStorageError):
    def __init__(self, *, request_id: str = "") -> None:
        super().__init__(
            code="artifact_rate_limited", retryable=True, request_id=request_id
        )


class ArtifactTimeoutError(ArtifactStorageError):
    def __init__(self, *, request_id: str = "") -> None:
        super().__init__(code="artifact_timeout", retryable=True, request_id=request_id)


class ArtifactUnavailableError(ArtifactStorageError):
    def __init__(self, *, request_id: str = "") -> None:
        super().__init__(code="artifact_unavailable", retryable=True, request_id=request_id)


class ArtifactAuthenticationError(ArtifactStorageError):
    def __init__(self, *, request_id: str = "") -> None:
        super().__init__(
            code="artifact_authentication_failed",
            retryable=False,
            request_id=request_id,
        )


class ArtifactConfigurationError(ArtifactStorageError):
    def __init__(self) -> None:
        super().__init__(code="artifact_configuration_invalid", retryable=False)


class ArtifactInternalRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    artifact_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=512)
    capability_name: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=255)
    size_bytes: StrictInt = Field(ge=0, le=MAX_ARTIFACT_BYTES)
    payload_sha256: str
    storage_backend: Literal["memory", "file", "tos"]
    bucket: str = ""
    object_key: str = ""
    etag: str = ""
    crc64: str = ""
    lifecycle_status: ArtifactLifecycleStatus
    retention_status: RetentionStatus
    created_at: AwareDatetime
    updated_at: AwareDatetime
    upload_owner_id: str | None = Field(default=None, min_length=1, max_length=128)
    upload_fencing_token: StrictInt = Field(default=0, ge=0)
    upload_lease_expires_at: AwareDatetime | None = None
    available_at: AwareDatetime | None = None
    failure_code: str | None = None
    metadata: Mapping[str, str] = Field(default_factory=dict, repr=False)

    @field_validator("payload_sha256")
    @classmethod
    def _valid_digest(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("payload_sha256 is invalid")
        return value

    @field_validator("metadata", mode="before")
    @classmethod
    def _freeze_metadata(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        return MappingProxyType(dict(value))

    @model_validator(mode="after")
    def _valid_lifecycle(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at precedes created_at")
        if self.available_at is not None and self.available_at < self.created_at:
            raise ValueError("available_at precedes created_at")
        upload_group = (
            self.upload_owner_id,
            self.upload_lease_expires_at,
        )
        if any(value is not None for value in upload_group) != all(
            value is not None for value in upload_group
        ):
            raise ValueError("upload lease evidence is incomplete")
        if self.upload_owner_id is None and self.upload_fencing_token != 0:
            raise ValueError("upload fencing token has no owner")
        if self.upload_owner_id is not None and self.upload_fencing_token < 1:
            raise ValueError("upload fencing token is invalid")
        if (
            self.upload_lease_expires_at is not None
            and self.upload_lease_expires_at < self.created_at
        ):
            raise ValueError("upload lease expires before artifact creation")
        if self.lifecycle_status is ArtifactLifecycleStatus.AVAILABLE:
            if self.available_at is None or self.failure_code is not None:
                raise ValueError("available artifact metadata is invalid")
        elif self.lifecycle_status is ArtifactLifecycleStatus.FAILED:
            if self.available_at is not None or not self.failure_code:
                raise ValueError("failed artifact metadata is invalid")
        elif self.available_at is not None or self.failure_code is not None:
            raise ValueError("pending artifact metadata is invalid")
        if self.storage_backend == "tos":
            ArtifactLocation(bucket=self.bucket, object_key=self.object_key)
        else:
            if self.bucket or not self.object_key:
                raise ValueError("non-TOS location is invalid")
            ArtifactLocation(bucket="local-test", object_key=self.object_key)
        return self


class ArtifactPublicReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    artifact_id: str = Field(min_length=1, max_length=128)
    content_type: str = Field(min_length=1, max_length=255)
    size_bytes: StrictInt = Field(ge=0, le=MAX_ARTIFACT_BYTES)
    checksum: str
    created_at: AwareDatetime

    @field_validator("checksum")
    @classmethod
    def _valid_checksum(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("checksum is invalid")
        return value

    @classmethod
    def from_internal(cls, record: ArtifactInternalRecord) -> ArtifactPublicReference:
        return cls(
            artifact_id=record.artifact_id,
            content_type=record.content_type,
            size_bytes=record.size_bytes,
            checksum=record.payload_sha256,
            created_at=record.created_at,
        )


def tenant_segment(tenant_id: str) -> str:
    tenant = str(tenant_id)
    if not tenant:
        raise ValueError("tenant_id is required")
    return hashlib.sha256(tenant.encode("utf-8")).hexdigest()


def _json_default(value: Any):
    if isinstance(value, datetime):
        normalized = (
            value.astimezone(UTC)
            if value.tzinfo is not None
            else value.replace(tzinfo=UTC)
        )
        return normalized.isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=lambda item: repr(item))
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


class _BoundedJsonWriter:
    """Canonical, non-recursive JSON writer with a hard allocation ceiling."""

    _ESCAPES = {
        '"': b'\\"',
        "\\": b"\\\\",
        "\b": b"\\b",
        "\f": b"\\f",
        "\n": b"\\n",
        "\r": b"\\r",
        "\t": b"\\t",
    }

    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max_bytes
        self._payload = bytearray()
        self._active_containers: set[int] = set()

    def encode(self, value: Any) -> bytes:
        stack: list[tuple[Any, ...]] = [("value", value)]
        while stack:
            action = stack.pop()
            kind = action[0]
            if kind == "value":
                self._write_value(action[1], stack)
            elif kind == "sequence_next":
                self._write_sequence_next(action[1], action[2], stack)
            elif kind == "mapping_next":
                self._write_mapping_next(action[1], action[2], action[3], stack)
            else:
                container_id, closing = action[1], action[2]
                self._active_containers.remove(container_id)
                self._write(closing)
        return bytes(self._payload)

    def _write_value(self, value: Any, stack: list[tuple[Any, ...]]) -> None:
        if value is None:
            self._write(b"null")
        elif value is True:
            self._write(b"true")
        elif value is False:
            self._write(b"false")
        elif isinstance(value, str):
            self._write_string(value)
        elif isinstance(value, int):
            self._write(int.__repr__(value).encode("ascii"))
        elif isinstance(value, float):
            rendered = float.__repr__(value)
            if rendered in {"nan", "inf", "-inf"}:
                raise ValueError(
                    "Out of range float values are not JSON compliant"
                )
            self._write(rendered.encode("ascii"))
        elif isinstance(value, datetime):
            self._write_string(_json_default(value))
        elif isinstance(value, Enum):
            stack.append(("value", value.value))
        elif isinstance(value, BaseModel):
            stack.append(("value", value.model_dump(mode="json")))
        elif isinstance(value, Mapping):
            self._start_mapping(value, stack)
        elif isinstance(value, (list, tuple)):
            self._start_sequence(value, stack)
        elif isinstance(value, (set, frozenset)):
            self._start_sequence(sorted(value, key=lambda item: repr(item)), stack)
        else:
            _json_default(value)

    def _start_sequence(
        self, value: list[Any] | tuple[Any, ...], stack: list[tuple[Any, ...]]
    ) -> None:
        container_id = id(value)
        self._enter(container_id)
        self._write(b"[")
        stack.append(("leave", container_id, b"]"))
        stack.append(("sequence_next", iter(value), True))

    def _write_sequence_next(
        self, iterator, first: bool, stack: list[tuple[Any, ...]]
    ) -> None:
        try:
            value = next(iterator)
        except StopIteration:
            return
        if not first:
            self._write(b",")
        stack.append(("sequence_next", iterator, False))
        stack.append(("value", value))

    def _start_mapping(
        self, value: Mapping[str, Any], stack: list[tuple[Any, ...]]
    ) -> None:
        container_id = id(value)
        self._enter(container_id)
        keys = list(value)
        if any(not isinstance(key, str) for key in keys):
            raise TypeError("JSON object keys must be strings")
        keys.sort()
        self._write(b"{")
        stack.append(("leave", container_id, b"}"))
        stack.append(("mapping_next", iter(keys), value, True))

    def _write_mapping_next(
        self,
        iterator,
        mapping: Mapping[str, Any],
        first: bool,
        stack: list[tuple[Any, ...]],
    ) -> None:
        try:
            key = next(iterator)
        except StopIteration:
            return
        if not first:
            self._write(b",")
        self._write_string(key)
        self._write(b":")
        stack.append(("mapping_next", iterator, mapping, False))
        stack.append(("value", mapping[key]))

    def _enter(self, container_id: int) -> None:
        if container_id in self._active_containers:
            raise ValueError("Circular reference detected")
        self._active_containers.add(container_id)

    def _write_string(self, value: str) -> None:
        self._write(b'"')
        for character in value:
            escaped = self._ESCAPES.get(character)
            if escaped is not None:
                self._write(escaped)
                continue
            codepoint = ord(character)
            if codepoint < 0x20 or 0xD800 <= codepoint <= 0xDFFF:
                self._write(f"\\u{codepoint:04x}".encode("ascii"))
            else:
                self._write(character.encode("utf-8"))
        self._write(b'"')

    def _write(self, chunk: bytes) -> None:
        if len(self._payload) + len(chunk) > self._max_bytes:
            raise ArtifactTooLargeError()
        self._payload.extend(chunk)


def deterministic_json_bytes(
    result: Mapping[str, Any], *, max_bytes: int = MAX_ARTIFACT_BYTES
) -> bytes:
    """Encode canonical JSON incrementally without materializing large tokens."""

    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_ARTIFACT_BYTES:
        raise ValueError("max_bytes is invalid")
    return _BoundedJsonWriter(max_bytes).encode(result)


class ArtifactReference(BaseModel):
    artifact_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=512)
    capability_name: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(le=MAX_ARTIFACT_BYTES)
    checksum: str
    storage_uri: str
    storage_backend: Literal["memory", "file", "tos"] = "file"
    bucket: str = ""
    object_key: str = ""
    etag: str = ""
    crc64: str = ""
    status: Literal["pending", "available", "failed"] = "available"
    storage_class: str = "STANDARD"
    retention_status: Literal[
        "active", "gc_eligible", "deleting", "deleted", "delete_failed"
    ] = "active"
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("size_bytes")
    @classmethod
    def _valid_size(cls, value: int) -> int:
        if type(value) is not int or value < 0:
            raise ValueError("size_bytes is invalid")
        return value

    @model_validator(mode="after")
    def _valid_storage_metadata(self) -> Self:
        if not re.fullmatch(r"[A-Za-z0-9:._-]{8,128}", self.checksum):
            raise ValueError("checksum is invalid")
        if self.storage_backend == "tos":
            ArtifactLocation(bucket=self.bucket, object_key=self.object_key)
            if self.storage_uri != f"tos://{self.bucket}/{self.object_key}":
                raise ValueError("storage_uri is invalid")
        elif self.storage_backend == "file":
            if self.bucket or not self.object_key:
                raise ValueError("file location is invalid")
            ArtifactLocation(bucket="local-test", object_key=self.object_key)
            if not self.storage_uri.startswith("file:///"):
                raise ValueError("storage_uri is invalid")
        else:
            if self.bucket:
                raise ValueError("memory location is invalid")
            if self.object_key:
                ArtifactLocation(bucket="local-test", object_key=self.object_key)
            if self.storage_uri != f"memory://{self.artifact_id}":
                raise ValueError("storage_uri is invalid")
        return self


class StoredResult(BaseModel):
    storage_mode: str
    inline_result: dict[str, Any] | None = None
    artifact_refs: tuple[ArtifactReference, ...] = ()


class InMemoryArtifactRepository:
    def __init__(self) -> None:
        self._items: dict[str, ArtifactReference] = {}
        self._lock = threading.Lock()

    def add(self, reference: ArtifactReference) -> None:
        """Add immutable metadata; lifecycle transitions use a separate future API."""

        with self._lock:
            existing = self._items.get(reference.artifact_id)
            if existing is not None:
                if (
                    existing.tenant_id == reference.tenant_id
                    and existing.checksum == reference.checksum
                ):
                    return
                raise ArtifactConflict("artifact metadata conflicts")
            self._items[reference.artifact_id] = reference.model_copy(deep=True)

    def get(self, artifact_id: str, *, tenant_id: str) -> ArtifactReference | None:
        with self._lock:
            item = self._items.get(artifact_id)
            if item is None or item.tenant_id != tenant_id:
                return None
            return item.model_copy(deep=True)

    def list(self, *, tenant_id: str) -> list[ArtifactReference]:
        tenant = str(tenant_id or "").strip()
        if not tenant:
            raise ValueError("tenant_id is required")
        with self._lock:
            items = [
                item.model_copy(deep=True)
                for item in self._items.values()
                if item.tenant_id == tenant
            ]
        return sorted(items, key=lambda item: (item.created_at, item.artifact_id))


class ArtifactService:
    def __init__(
        self,
        repository: ArtifactRepository,
        *,
        root: Path,
        inline_threshold_bytes: int = 2048,
    ) -> None:
        self._repository = repository
        self._root = Path(root).resolve()
        self._inline_threshold_bytes = max(1, int(inline_threshold_bytes))

    @staticmethod
    def _json_bytes(result: dict[str, Any]) -> bytes:
        return deterministic_json_bytes(result)

    @staticmethod
    def _tenant_segment(tenant_id: str) -> str:
        return tenant_segment(tenant_id)

    def persist_result(
        self,
        *,
        tenant_id: str,
        capability_name: str,
        result: dict[str, Any],
        force_artifact: bool = False,
    ) -> StoredResult:
        payload = self._json_bytes(result)
        if not force_artifact and len(payload) <= self._inline_threshold_bytes:
            return StoredResult(storage_mode="inline", inline_result=result)
        checksum = hashlib.sha256(payload).hexdigest()
        identity = b"\0".join(
            (tenant_id.encode("utf-8"), capability_name.encode("utf-8"), payload)
        )
        artifact_id = f"art_{hashlib.sha256(identity).hexdigest()[:24]}"
        tenant_dir = self._root / self._tenant_segment(tenant_id)
        tenant_dir.mkdir(parents=True, exist_ok=True)
        path = tenant_dir / f"{artifact_id}.json"
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=tenant_dir,
                prefix=f".{artifact_id}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temp_path = Path(temporary.name)
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temp_path, path)
            temp_path = None
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        reference = ArtifactReference(
            artifact_id=artifact_id,
            tenant_id=tenant_id,
            capability_name=capability_name,
            content_type="application/json",
            size_bytes=len(payload),
            checksum=checksum,
            storage_uri=f"file://{path}",
            storage_backend="file",
            object_key=f"{self._tenant_segment(tenant_id)}/{artifact_id}.json",
            status="available",
        )
        self._repository.add(reference)
        canonical = self._repository.get(artifact_id, tenant_id=tenant_id)
        if canonical is None:
            raise ArtifactNotFound(artifact_id)
        return StoredResult(storage_mode="artifact", artifact_refs=(canonical,))

    def read(self, artifact_id: str, *, tenant_id: str) -> dict[str, Any]:
        reference = self._repository.get(artifact_id, tenant_id=tenant_id)
        if reference is None:
            raise ArtifactNotFound(artifact_id)
        prefix = "file://"
        if not reference.storage_uri.startswith(prefix):
            raise ArtifactNotFound(artifact_id)
        path = Path(reference.storage_uri[len(prefix) :])
        if not path.is_file():
            raise ArtifactNotFound(artifact_id)
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != reference.checksum:
            raise ArtifactNotFound(artifact_id)
        return json.loads(payload)
