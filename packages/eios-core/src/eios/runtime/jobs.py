from __future__ import annotations

from datetime import UTC, datetime
import threading
from typing import Any, Unpack
import uuid

from pydantic import AwareDatetime, BaseModel, Field

from eios.capabilities.models import CapabilityType, ExecutionMode
from eios.kernel.ports.repositories import JobCreateFields
from eios.runtime.job_semantics import (
    JOB_ALLOWED_TRANSITIONS,
    JobStatus,
    job_requests_match,
)


class JobConflict(RuntimeError):
    code = "job_conflict"


class JobSchedulingUnsupported(RuntimeError):
    """The selected persistence revision cannot store scheduling fields."""

    code = "job_scheduling_unsupported"


class InvalidJobTransition(RuntimeError):
    code = "invalid_job_transition"


class JobNotFound(LookupError):
    code = "job_not_found"


class JobEvent(BaseModel):
    sequence: int
    job_id: str
    tenant_id: str
    status: JobStatus
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: AwareDatetime


class JobSnapshot(BaseModel):
    job_id: str
    capability_name: str
    capability_version: str
    capability_schema_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    capability_contract_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    pack_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
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
    created_at: AwareDatetime
    updated_at: AwareDatetime
    queue: str
    priority: int = 0
    max_attempts: int = 3
    requested_available_at: AwareDatetime | None = None
    available_at: AwareDatetime = Field(
        default_factory=lambda data: data["created_at"]
    )
    cancel_requested_at: AwareDatetime | None = None
    fencing_token: int = 0
    dead_letter_reason: dict[str, Any] | None = None
    status: JobStatus = JobStatus.PENDING
    result: dict[str, Any] | None = None
    artifact_refs: tuple[dict[str, Any], ...] = ()
    error: dict[str, Any] | None = None


def validate_job_transition(
    current: JobStatus,
    next_status: JobStatus,
) -> JobStatus:
    """Return ``next_status`` when it is a declared transition, else fail closed."""

    if next_status not in JOB_ALLOWED_TRANSITIONS[current]:
        raise InvalidJobTransition(
            f"cannot transition job from {current.value} to {next_status.value}"
        )
    return next_status


class InMemoryJobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, JobSnapshot] = {}
        self._contracts: dict[tuple[str, str, str, str], str] = {}
        self._invocation_jobs: dict[str, str] = {}
        self._events: dict[str, list[JobEvent]] = {}
        self._lock = threading.RLock()

    def create_or_get(
        self, **fields: Unpack[JobCreateFields]
    ) -> tuple[JobSnapshot, bool]:
        key_value = str(fields.get("idempotency_key") or "").strip()
        contract = (
            str(fields["tenant_id"]),
            str(fields["capability_name"]),
            str(fields["capability_version"]),
            key_value,
        )
        with self._lock:
            if key_value and contract in self._contracts:
                existing = self._jobs[self._contracts[contract]]
                if not job_requests_match(existing, fields):
                    raise JobConflict(
                        "idempotency contract already exists with a different job request"
                    )
                return existing.model_copy(deep=True), False
            now = datetime.now(UTC)
            snapshot_fields = dict(fields)
            snapshot_fields["idempotency_key"] = key_value or None
            snapshot_fields["requested_available_at"] = snapshot_fields.get(
                "available_at"
            )
            job = JobSnapshot(
                job_id=f"job_{uuid.uuid4().hex}",
                created_at=now,
                updated_at=now,
                **snapshot_fields,
            )
            self._jobs[job.job_id] = job
            self._invocation_jobs[job.invocation_id] = job.job_id
            if key_value:
                self._contracts[contract] = job.job_id
            self._events[job.job_id] = []
            self._append_event(job, status=JobStatus.PENDING, message="job accepted")
            return job.model_copy(deep=True), True

    @staticmethod
    def _tenant(tenant_id: str) -> str:
        tenant = str(tenant_id or "").strip()
        if not tenant:
            raise ValueError("tenant_id is required")
        return tenant

    def _append_event(
        self,
        job: JobSnapshot,
        *,
        status: JobStatus,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        events = self._events[job.job_id]
        events.append(
            JobEvent(
                sequence=len(events) + 1,
                job_id=job.job_id,
                tenant_id=job.tenant_id,
                status=status,
                message=message,
                data=data or {},
                created_at=datetime.now(UTC),
            )
        )

    def get(self, job_id: str, *, tenant_id: str) -> JobSnapshot | None:
        tenant = self._tenant(tenant_id)
        with self._lock:
            item = self._jobs.get(job_id)
            return (
                item.model_copy(deep=True)
                if item is not None and item.tenant_id == tenant
                else None
            )

    def get_by_invocation_id(
        self, invocation_id: str, *, tenant_id: str
    ) -> JobSnapshot | None:
        tenant = self._tenant(tenant_id)
        with self._lock:
            job_id = self._invocation_jobs.get(invocation_id)
            item = self._jobs.get(job_id) if job_id is not None else None
            return (
                item.model_copy(deep=True)
                if item is not None and item.tenant_id == tenant
                else None
            )

    def list(self, *, tenant_id: str) -> list[JobSnapshot]:
        tenant = self._tenant(tenant_id)
        with self._lock:
            items = [
                item.model_copy(deep=True)
                for item in self._jobs.values()
                if item.tenant_id == tenant
            ]
        return sorted(items, key=lambda item: (item.created_at, item.job_id))

    def events(self, job_id: str, *, tenant_id: str) -> list[JobEvent]:
        tenant = self._tenant(tenant_id)
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.tenant_id != tenant:
                return []
            return [item.model_copy(deep=True) for item in self._events.get(job_id, [])]

    def _require(self, job_id: str, *, tenant_id: str) -> JobSnapshot:
        job = self._jobs.get(job_id)
        if job is None or job.tenant_id != tenant_id:
            raise JobNotFound(f"job not found: {job_id}")
        return job

    def _transition(
        self,
        job_id: str,
        *,
        tenant_id: str,
        status: JobStatus,
        message: str,
        result: dict[str, Any] | None = None,
        artifact_refs: tuple[dict[str, Any], ...] = (),
        error: dict[str, Any] | None = None,
    ) -> JobSnapshot:
        with self._lock:
            tenant = self._tenant(tenant_id)
            job = self._require(job_id, tenant_id=tenant)
            validate_job_transition(job.status, status)
            updated = job.model_copy(
                update={
                    "status": status,
                    "result": result,
                    "artifact_refs": artifact_refs,
                    "error": error,
                    "updated_at": datetime.now(UTC),
                },
                deep=True,
            )
            self._jobs[job_id] = updated
            self._append_event(
                updated, status=status, message=message, data=result or error or {}
            )
            return updated.model_copy(deep=True)

    def mark_running(self, job_id: str, *, tenant_id: str) -> JobSnapshot:
        return self._transition(
            job_id, tenant_id=tenant_id, status=JobStatus.RUNNING, message="job running"
        )

    def mark_succeeded(
        self,
        job_id: str,
        *,
        tenant_id: str,
        result: dict[str, Any],
        artifact_refs: tuple[dict[str, Any], ...] = (),
    ) -> JobSnapshot:
        return self._transition(
            job_id,
            tenant_id=tenant_id,
            status=JobStatus.SUCCEEDED,
            message="job succeeded",
            result=result,
            artifact_refs=artifact_refs,
        )

    def mark_failed(
        self, job_id: str, *, tenant_id: str, error: dict[str, Any]
    ) -> JobSnapshot:
        return self._transition(
            job_id,
            tenant_id=tenant_id,
            status=JobStatus.FAILED,
            message="job failed",
            error=error,
        )

    def mark_cancelled(
        self,
        job_id: str,
        *,
        tenant_id: str,
        error: dict[str, Any] | None = None,
    ) -> JobSnapshot:
        with self._lock:
            tenant = self._tenant(tenant_id)
            job = self._require(job_id, tenant_id=tenant)
            if job.status is JobStatus.CANCELLED:
                if job.error or error is None:
                    return job.model_copy(deep=True)
                updated = job.model_copy(
                    update={"error": error, "updated_at": datetime.now(UTC)},
                    deep=True,
                )
                self._jobs[job_id] = updated
                self._append_event(
                    updated,
                    status=JobStatus.CANCELLED,
                    message="job cancelled",
                    data=error,
                )
                return updated.model_copy(deep=True)
            return self._transition(
                job_id,
                tenant_id=tenant,
                status=JobStatus.CANCELLED,
                message="job cancelled",
                error=error,
            )
