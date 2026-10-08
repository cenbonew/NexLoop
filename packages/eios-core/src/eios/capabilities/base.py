from __future__ import annotations

from abc import ABC, abstractmethod
import hashlib
import json
from typing import Any

from pydantic import BaseModel, ValidationError

from .models import (
    AuthContext,
    CapabilityMetadata,
    CapabilitySummary,
    CapabilityType,
    CostClass,
    EvaluationContext,
    ExecutionMode,
    InvocationContext,
    RiskLevel,
    ValidationErrorItem,
    ValidationResult,
)


class BaseCapability(ABC):
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    capability_type: CapabilityType

    name = ""
    version = "1.0.0"
    category = "general"
    title = ""
    description = ""
    auth_scopes: tuple[str, ...] = ()
    idempotent = False
    has_side_effects = False
    execution_mode = ExecutionMode.SYNC
    queue = "default"
    # Interactive read capabilities must return their result inline in the sync
    # response — the caller reads the data directly, so the result is never
    # offloaded to an artifact regardless of size. Purely behavioural: it does
    # not enter ``metadata()``/``schema_hash`` and so never affects the frozen
    # capability-pack contract. Leave False for anything that produces durable,
    # potentially large outputs (which the artifact pipeline should offload).
    inline_result_only = False
    risk_level = RiskLevel.LOW
    cost_class = CostClass.LOW
    tags: tuple[str, ...] = ()
    deprecated = False

    def metadata(self) -> CapabilityMetadata:
        if not str(self.name).strip():
            raise ValueError("capability name is required")
        payload = {
            "name": self.name,
            "version": self.version,
            "input_schema": self.input_model.model_json_schema(),
            "output_schema": self.output_model.model_json_schema(),
            "auth_scopes": self.auth_scopes,
            "execution_mode": self.execution_mode.value,
            "queue": self.queue,
        }
        schema_hash = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return CapabilityMetadata(
            name=self.name,
            version=self.version,
            capability_type=self.capability_type,
            category=self.category,
            title=self.title or self.name,
            description=self.description,
            input_schema=payload["input_schema"],
            output_schema=payload["output_schema"],
            auth_scopes=self.auth_scopes,
            idempotent=self.idempotent,
            has_side_effects=self.has_side_effects,
            execution_mode=self.execution_mode,
            queue=self.queue,
            risk_level=self.risk_level,
            cost_class=self.cost_class,
            tags=self.tags,
            deprecated=self.deprecated,
            schema_hash=schema_hash,
        )

    def summary(self) -> CapabilitySummary:
        metadata = self.metadata()
        return CapabilitySummary(**metadata.model_dump(include=set(CapabilitySummary.model_fields)))

    def validate(self, raw_input: dict[str, Any], auth: AuthContext) -> ValidationResult:
        tenant_id = str(raw_input.get("tenant_id") or "").strip()
        if tenant_id and not auth.can_access_tenant(tenant_id):
            return ValidationResult(
                valid=False,
                errors=(
                    ValidationErrorItem(
                        field="tenant_id",
                        code="tenant_mismatch",
                        message="input tenant is outside the caller tenant scope",
                    ),
                ),
            )
        try:
            normalized = self.input_model.model_validate(raw_input).model_dump(mode="python")
        except ValidationError as exc:
            errors = tuple(
                ValidationErrorItem(
                    field=".".join(str(part) for part in item["loc"]),
                    code=str(item["type"]),
                    message=str(item["msg"]),
                )
                for item in exc.errors()
            )
            return ValidationResult(valid=False, errors=errors)
        return ValidationResult(valid=True, normalized_input=normalized)

    @abstractmethod
    def build_plan(self, normalized_input: dict[str, Any], context: EvaluationContext):
        raise NotImplementedError

    @abstractmethod
    def run(self, normalized_input: dict[str, Any], context: InvocationContext) -> dict[str, Any]:
        raise NotImplementedError


class AtomicCapability(BaseCapability):
    capability_type = CapabilityType.ATOMIC


class WorkflowCapability(BaseCapability):
    capability_type = CapabilityType.WORKFLOW
