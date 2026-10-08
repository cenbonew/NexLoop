"""Fail-closed, instance-aware Ontology Property authorization.

The projection boundary is deliberately independent of storage.  Callers must
authorize query properties before filtering/sorting/exporting raw objects and
must project every returned row through the same boundary.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableSequence, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PropertyOperation(str, Enum):
    READ = "read"
    FILTER = "filter"
    SORT = "sort"
    EXPORT = "export"


class RedactionContract(str, Enum):
    OMIT = "omit"
    NULL = "null"


class PropertyAuthorizationUnavailable(RuntimeError):
    code = "property_authorization_unavailable"


class PropertyUseDenied(PermissionError):
    code = "property_use_denied"


class PropertySubjectContext(BaseModel):
    """Authz-owned, transport-neutral trusted subject/session projection."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    tenant_id: str
    principal_id: str
    api_key_id: str
    request_id: str
    trace_id: str
    browser_session_id: str | None = None
    browser_session_revision: int | None = None

    @field_validator(
        "tenant_id", "principal_id", "api_key_id", "request_id", "trace_id"
    )
    @classmethod
    def _identifier(cls, value: str) -> str:
        if not value or value != value.strip() or len(value.encode()) > 255:
            raise ValueError("property subject identifier is invalid")
        return value

    @model_validator(mode="after")
    def _session_shape(self) -> PropertySubjectContext:
        if (self.browser_session_id is None) != (
            self.browser_session_revision is None
        ):
            raise ValueError("property subject session is invalid")
        if self.browser_session_id is not None and (
            not self.browser_session_id
            or self.browser_session_id != self.browser_session_id.strip()
            or self.browser_session_revision is None
            or self.browser_session_revision < 1
        ):
            raise ValueError("property subject session is invalid")
        return self


class PropertyResource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    tenant_id: str
    object_type: str
    object_id: str
    property_name: str

    @field_validator("tenant_id", "object_type", "object_id", "property_name")
    @classmethod
    def _bounded_identifier(cls, value: str) -> str:
        if (
            not value
            or value != value.strip()
            or len(value.encode()) > 255
            or any(ord(char) < 32 for char in value)
        ):
            raise ValueError("property resource identifier is invalid")
        return value

    @property
    def resource_id(self) -> str:
        return (
            f"eios:property:{self.object_type}/{self.object_id}"
            f"/{self.property_name}"
        )


class PropertyAuthorizationQuery(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    resource: PropertyResource
    subject_principal_id: str
    subject_session_id: str
    subject_session_revision: int
    request_id: str
    trace_id: str
    operation: PropertyOperation
    subject_attributes: dict[str, str] = Field(default_factory=dict)
    object_attributes: dict[str, object] = Field(default_factory=dict)

    @field_validator(
        "subject_principal_id",
        "subject_session_id",
        "request_id",
        "trace_id",
    )
    @classmethod
    def _subject(cls, value: str) -> str:
        if not value or value != value.strip() or len(value.encode()) > 255:
            raise ValueError("subject principal is invalid")
        return value

    @field_validator("subject_session_revision")
    @classmethod
    def _session_revision(cls, value: int) -> int:
        if type(value) is not int or type(value) is bool or value < 0:
            raise ValueError("subject session revision is invalid")
        return value


class PropertyAuthorizationDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    allowed: bool
    policy_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
    redaction_contract: RedactionContract = RedactionContract.OMIT


class PropertyDecisionProvider(Protocol):
    def decide(
        self, query: PropertyAuthorizationQuery
    ) -> PropertyAuthorizationDecision: ...


class PropertyAuditSink(Protocol):
    def record(
        self,
        query: PropertyAuthorizationQuery,
        decision: PropertyAuthorizationDecision,
    ) -> None: ...


@dataclass(frozen=True)
class PropertyRule:
    policy_id: str
    tenant_id: str
    object_type: str
    property_name: str
    operations: frozenset[PropertyOperation]
    effect: Literal["allow", "deny"]
    subject_principal_id: str | None = None
    subject_attribute: tuple[str, str] | None = None
    object_attribute: tuple[str, object] | None = None

    def matches(self, query: PropertyAuthorizationQuery) -> bool:
        resource = query.resource
        return (
            resource.tenant_id == self.tenant_id
            and resource.object_type == self.object_type
            and resource.property_name == self.property_name
            and query.operation in self.operations
            and (
                self.subject_principal_id is None
                or query.subject_principal_id == self.subject_principal_id
            )
            and (
                self.subject_attribute is None
                or query.subject_attributes.get(self.subject_attribute[0])
                == self.subject_attribute[1]
            )
            and (
                self.object_attribute is None
                or query.object_attributes.get(self.object_attribute[0])
                == self.object_attribute[1]
            )
        )


class InMemoryPropertyDecisionProvider:
    """Reference semantics: matching deny wins; matching allow is required."""

    def __init__(self, rules: Sequence[PropertyRule]) -> None:
        self._rules = tuple(rules)

    def decide(
        self, query: PropertyAuthorizationQuery
    ) -> PropertyAuthorizationDecision:
        matches = tuple(rule for rule in self._rules if rule.matches(query))
        policy_ids = tuple(sorted({rule.policy_id for rule in matches}))
        if any(rule.effect == "deny" for rule in matches):
            return PropertyAuthorizationDecision(
                allowed=False,
                policy_ids=policy_ids,
                reason_codes=("property_explicit_deny",),
            )
        if any(rule.effect == "allow" for rule in matches):
            return PropertyAuthorizationDecision(
                allowed=True,
                policy_ids=policy_ids,
                reason_codes=("property_allow",),
            )
        return PropertyAuthorizationDecision(
            allowed=False,
            policy_ids=(),
            reason_codes=("property_no_matching_allow",),
        )


class PropertySecurityProjector:
    def __init__(
        self,
        provider: PropertyDecisionProvider,
        *,
        audit_sink: PropertyAuditSink | None = None,
        redaction: RedactionContract = RedactionContract.OMIT,
    ) -> None:
        self._provider = provider
        self._audit = audit_sink
        self._redaction = redaction

    @staticmethod
    def _subject_attributes(context: PropertySubjectContext) -> dict[str, str]:
        attributes = {
            "api_key_id": context.api_key_id,
            "authentication_transport": (
                "browser_session"
                if context.browser_session_id is not None
                else "api_key"
            ),
        }
        if context.browser_session_id is not None:
            attributes["session_id"] = context.browser_session_id
            attributes["session_revision"] = str(context.browser_session_revision)
        return attributes

    def _decide(
        self,
        context: PropertySubjectContext,
        *,
        object_type: str,
        object_id: str,
        property_name: str,
        properties: Mapping[str, object],
        operation: PropertyOperation,
    ) -> PropertyAuthorizationDecision:
        if type(context) is not PropertySubjectContext:
            raise PropertyAuthorizationUnavailable("trusted context is required")
        query = PropertyAuthorizationQuery(
            resource=PropertyResource(
                tenant_id=context.tenant_id,
                object_type=object_type,
                object_id=object_id,
                property_name=property_name,
            ),
            subject_principal_id=context.principal_id,
            subject_session_id=(
                context.browser_session_id
                if context.browser_session_id is not None
                else context.api_key_id
            ),
            subject_session_revision=(
                context.browser_session_revision
                if context.browser_session_revision is not None
                else 0
            ),
            request_id=context.request_id,
            trace_id=context.trace_id,
            operation=operation,
            subject_attributes=self._subject_attributes(context),
            object_attributes=dict(properties),
        )
        try:
            decision = self._provider.decide(query)
            if type(decision) is not PropertyAuthorizationDecision:
                raise TypeError("invalid property authorization decision")
            if self._audit is not None:
                self._audit.record(query, decision)
        except Exception as error:
            raise PropertyAuthorizationUnavailable(
                "property authorization is unavailable"
            ) from error
        return decision

    def project(
        self,
        context: PropertySubjectContext,
        *,
        object_type: str,
        object_id: str,
        properties: Mapping[str, object],
        authorization_properties: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        policy_properties = (
            properties
            if authorization_properties is None
            else authorization_properties
        )
        projected: dict[str, object] = {}
        for name, value in properties.items():
            decision = self._decide(
                context,
                object_type=object_type,
                object_id=object_id,
                property_name=name,
                properties=policy_properties,
                operation=PropertyOperation.READ,
            )
            if decision.allowed:
                projected[name] = value
            elif (
                decision.redaction_contract is RedactionContract.NULL
                or self._redaction is RedactionContract.NULL
            ):
                projected[name] = None
        return projected

    def readable_property_names(
        self,
        context: PropertySubjectContext,
        *,
        object_type: str,
        object_id: str,
        properties: Mapping[str, object],
    ) -> frozenset[str]:
        """Authorize raw dependencies without exposing redaction placeholders."""

        return frozenset(
            name
            for name in properties
            if self._decide(
                context,
                object_type=object_type,
                object_id=object_id,
                property_name=name,
                properties=properties,
                operation=PropertyOperation.READ,
            ).allowed
        )

    def explain(
        self,
        context: PropertySubjectContext,
        *,
        object_type: str,
        object_id: str,
        property_name: str,
        properties: Mapping[str, object],
        operation: PropertyOperation = PropertyOperation.READ,
    ) -> PropertyAuthorizationDecision:
        """Return the same audited decision evidence used by enforcement."""

        return self._decide(
            context,
            object_type=object_type,
            object_id=object_id,
            property_name=property_name,
            properties=properties,
            operation=operation,
        )

    def require_query_properties(
        self,
        context: PropertySubjectContext,
        *,
        object_type: str,
        object_id: str,
        properties: Mapping[str, object],
        filter_properties: Sequence[str] = (),
        sort_properties: Sequence[str] = (),
        export_properties: Sequence[str] = (),
    ) -> None:
        requested = (
            (PropertyOperation.FILTER, filter_properties),
            (PropertyOperation.SORT, sort_properties),
            (PropertyOperation.EXPORT, export_properties),
        )
        for operation, names in requested:
            for name in names:
                decision = self._decide(
                    context,
                    object_type=object_type,
                    object_id=object_id,
                    property_name=name,
                    properties=properties,
                    operation=operation,
                )
                if not decision.allowed:
                    raise PropertyUseDenied(
                        f"{operation.value} is denied for property"
                    )


class InMemoryPropertyAuditSink:
    def __init__(self) -> None:
        self.records: MutableSequence[
            tuple[PropertyAuthorizationQuery, PropertyAuthorizationDecision]
        ] = []

    def record(
        self,
        query: PropertyAuthorizationQuery,
        decision: PropertyAuthorizationDecision,
    ) -> None:
        self.records.append((query, decision))


__all__ = [
    "InMemoryPropertyAuditSink",
    "InMemoryPropertyDecisionProvider",
    "PropertyAuthorizationDecision",
    "PropertyAuthorizationQuery",
    "PropertyAuthorizationUnavailable",
    "PropertyOperation",
    "PropertyResource",
    "PropertyRule",
    "PropertySecurityProjector",
    "PropertySubjectContext",
    "PropertyUseDenied",
    "RedactionContract",
]
