from __future__ import annotations

from typing import Iterable

from .models import (
    PolicyDecision,
    PolicyReasonCode,
    TenantContext,
    normalize_scopes,
)


class PolicyConfigurationError(ValueError):
    code = "policy_configuration_error"


def _validated_context(context: object) -> TenantContext | None:
    if context is None or type(context) is not TenantContext:
        return None
    try:
        model_fields = TenantContext.model_fields
        expected_fields = frozenset(model_fields)
        required_fields = frozenset(
            name for name, field in model_fields.items() if field.is_required()
        )
        values = vars(context)
        fields_set = getattr(context, "__pydantic_fields_set__", None)
        extra = getattr(context, "__pydantic_extra__", None)
        if (
            frozenset(values) != expected_fields
            or type(fields_set) is not set
            or not required_fields.issubset(fields_set)
            or not fields_set.issubset(expected_fields)
            or extra not in (None, {})
        ):
            return None
        raw_values = {field: values[field] for field in model_fields}
        checked = TenantContext.model_validate(
            raw_values,
            strict=True,
        )
        if type(checked) is not TenantContext or vars(checked) != raw_values:
            return None
        return checked
    except Exception:
        return None


def _configured_scopes(scopes: Iterable[str]) -> tuple[str, ...]:
    try:
        return normalize_scopes(scopes)
    except PolicyConfigurationError:
        raise
    except Exception:
        raise PolicyConfigurationError(
            "policy scope configuration is invalid"
        ) from None


class _ScopePolicy:
    reason_code: PolicyReasonCode

    def __init__(self, *, known_scopes: Iterable[str]) -> None:
        self._known_scopes = frozenset(_configured_scopes(known_scopes))

    def decide(
        self,
        context: TenantContext | None,
        required_scopes: Iterable[str],
    ) -> PolicyDecision:
        required = _configured_scopes(required_scopes)
        if not required:
            raise PolicyConfigurationError(
                "policy scope configuration must not be empty"
            )
        if not set(required).issubset(self._known_scopes):
            raise PolicyConfigurationError("policy scope configuration is unknown")
        trusted = _validated_context(context)
        if trusted is None:
            return PolicyDecision(
                allowed=False,
                reason_code=PolicyReasonCode.AUTHENTICATION_REQUIRED,
                required_scopes=required,
                missing_scopes=(),
            )
        missing = tuple(scope for scope in required if scope not in trusted.scopes)
        if missing:
            return PolicyDecision(
                allowed=False,
                reason_code=self.reason_code,
                required_scopes=required,
                missing_scopes=missing,
            )
        return PolicyDecision(
            allowed=True,
            reason_code=PolicyReasonCode.ALLOWED,
            required_scopes=required,
            missing_scopes=(),
        )


class RoutePolicy(_ScopePolicy):
    reason_code = PolicyReasonCode.ROUTE_SCOPE_MISSING


class CapabilityPolicy(_ScopePolicy):
    reason_code = PolicyReasonCode.CAPABILITY_SCOPE_MISSING


def require_tenant(
    context: TenantContext | None,
    asserted_tenant: str | None,
) -> PolicyDecision:
    trusted = _validated_context(context)
    if trusted is None:
        return PolicyDecision(
            allowed=False,
            reason_code=PolicyReasonCode.AUTHENTICATION_REQUIRED,
        )
    if asserted_tenant in (None, "") or asserted_tenant == trusted.tenant_id:
        return PolicyDecision(allowed=True, reason_code=PolicyReasonCode.ALLOWED)
    return PolicyDecision(
        allowed=False,
        reason_code=PolicyReasonCode.TENANT_MISMATCH,
    )
