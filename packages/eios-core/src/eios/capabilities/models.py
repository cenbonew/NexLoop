from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from eios.control.models import TenantContext
from eios.control.policy import _validated_context


class StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class CapabilityType(StringEnum):
    ATOMIC = "atomic"
    WORKFLOW = "workflow"


class ExecutionMode(StringEnum):
    SYNC = "sync"
    ASYNC = "async"


class RiskLevel(StringEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CostClass(StringEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AuthContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    tenant_id: str
    actor_id: str
    api_key_id: str = ""
    scopes: frozenset[str] = frozenset()
    # Stage 11: the caller's marking clearances. Additive default so every
    # existing construction site keeps working; consumed by marking-aware
    # read paths (e.g. Functions over Object Sets) to include exactly the
    # rows the caller is cleared for and to derive the output's
    # high-water-mark markings.
    markings: frozenset[str] = frozenset()
    trace_id: str
    request_id: str
    allowed_tenants: frozenset[str] = frozenset()

    @field_validator("allowed_tenants")
    @classmethod
    def _validate_allowed_tenants(cls, value: frozenset[str]) -> frozenset[str]:
        if value:
            raise ValueError("cross-tenant access is not supported")
        return value

    @classmethod
    def from_tenant_context(cls, context: TenantContext) -> AuthContext:
        trusted = _validated_context(context)
        if trusted is None:
            raise TypeError("trusted tenant context is required") from None
        return cls(
            tenant_id=trusted.tenant_id,
            actor_id=trusted.principal_id,
            api_key_id=trusted.api_key_id,
            scopes=trusted.scopes,
            markings=frozenset(trusted.markings),
            trace_id=trusted.trace_id,
            request_id=trusted.request_id,
            allowed_tenants=frozenset(),
        )

    def can_access_tenant(self, tenant_id: str) -> bool:
        target = str(tenant_id or "").strip()
        return not target or target == self.tenant_id


class EvaluationContext(BaseModel):
    """Read-only context for validate/plan; it intentionally has no write handles."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        arbitrary_types_allowed=True,
    )

    auth: AuthContext
    registry: Any | None = None
    correlation_id: str = ""
    causation_id: str = ""

    @classmethod
    def from_tenant_context(cls, context: TenantContext) -> EvaluationContext:
        trusted = _validated_context(context)
        if trusted is None:
            raise TypeError("trusted tenant context is required") from None
        return cls(
            auth=AuthContext.from_tenant_context(trusted),
            correlation_id=trusted.correlation_id,
            causation_id=trusted.causation_id,
        )

    @property
    def tenant_id(self) -> str:
        return self.auth.tenant_id

    @property
    def actor_id(self) -> str:
        return self.auth.actor_id

    @property
    def trace_id(self) -> str:
        return self.auth.trace_id

    @property
    def request_id(self) -> str:
        return self.auth.request_id


class InvocationContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    auth: AuthContext
    registry: Any | None = None
    job_store: Any | None = None
    artifact_service: Any | None = None
    emit_event: Any | None = None
    job_id: str | None = None
    idempotency_key: str | None = None
    confirmed: bool = False
    correlation_id: str = ""
    causation_id: str = ""
    invocation_id: str | None = None

    @property
    def tenant_id(self) -> str:
        return self.auth.tenant_id

    @property
    def actor_id(self) -> str:
        return self.auth.actor_id

    @property
    def trace_id(self) -> str:
        return self.auth.trace_id

    @property
    def request_id(self) -> str:
        return self.auth.request_id


class ValidationErrorItem(BaseModel):
    field: str
    code: str
    message: str


class ValidationResult(BaseModel):
    valid: bool
    normalized_input: dict[str, Any] = Field(default_factory=dict)
    errors: tuple[ValidationErrorItem, ...] = ()


class PlanStep(BaseModel):
    step_id: str
    capability_name: str
    title: str
    purpose: str = ""
    sequence: int = 1
    input_preview: dict[str, Any] = Field(default_factory=dict)


class ExecutionPlan(BaseModel):
    capability_name: str
    execution_mode: ExecutionMode
    normalized_input: dict[str, Any]
    steps: tuple[PlanStep, ...] = ()
    requires_confirmation: bool = False
    side_effects: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class CapabilityMetadata(BaseModel):
    name: str
    version: str
    capability_type: CapabilityType
    category: str
    title: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    auth_scopes: tuple[str, ...]
    idempotent: bool
    has_side_effects: bool
    execution_mode: ExecutionMode
    queue: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
    )
    risk_level: RiskLevel
    cost_class: CostClass
    tags: tuple[str, ...]
    deprecated: bool
    schema_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class CapabilitySummary(BaseModel):
    name: str
    version: str
    capability_type: CapabilityType
    category: str
    title: str
    description: str
    execution_mode: ExecutionMode
    risk_level: RiskLevel
    tags: tuple[str, ...]
    deprecated: bool
    schema_hash: str


class CapabilitySearchResult(BaseModel):
    capability: CapabilitySummary
    score: float
    matched_fields: tuple[str, ...]
