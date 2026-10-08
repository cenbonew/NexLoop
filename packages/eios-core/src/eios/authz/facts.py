from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
from json import dumps
import re
from typing import (
    Any,
    Annotated,
    Literal,
    TypeAlias,
)

from pydantic import (
    BaseModel,
    AfterValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from eios.identity.models import (
    FrozenJsonMap,
    FrozenJsonObject,
    MembershipKind,
    SubjectKind,
)

from .applications import (
    MAX_BREAK_GLASS_LIFETIME,
    ApplicationMode,
    OperationRestriction,
    ResourceRestriction,
)
from .errors import AuthorizationError
from .grants import GrantSubjectKind
from .operations import Operation
from .policy import canonicalize_policy_expression, validate_policy_expression
from .policy.models import PolicyExpression
from .policy.registry import PolicyDependencyReference, PolicyEffect, PolicyObligation
from .resources import (
    MAX_RESOURCE_PARENT_DEPTH,
    ResourceReference,
    ResourceType,
)


MAX_FACT_ITEMS = 4096
_CANONICAL_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/~-]{0,254}")
_CANONICAL_SCOPE = re.compile(r"[a-z][a-z0-9]*(?:[._:-][a-z0-9]+){0,15}")
_CANONICAL_ATTRIBUTE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,254}")
_CANONICAL_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,254}")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


class AuthorizationRisk(str, Enum):
    READ_ONLY = "read_only"
    SENSITIVE = "sensitive"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        arbitrary_types_allowed=True,
        revalidate_instances="always",
    )


def _identifier(value: str, field_name: str) -> str:
    if (
        type(value) is not str
        or len(value.encode("utf-8")) > 255
        or _CANONICAL_IDENTIFIER.fullmatch(value) is None
    ):
        raise ValueError(f"{field_name} must be a canonical bounded identifier")
    return value


def _application_id(value: str, field_name: str) -> str:
    checked = _identifier(value, field_name)
    if not checked.startswith("eios:application:"):
        raise ValueError(f"{field_name} must be a canonical application resource id")
    return checked


def _agent_id(value: str, field_name: str) -> str:
    checked = _identifier(value, field_name)
    if not checked.startswith("eios:agent:"):
        raise ValueError(f"{field_name} must be a canonical agent resource id")
    return checked


def _version(value: str, field_name: str) -> str:
    if type(value) is not str or _CANONICAL_VERSION.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a canonical bounded version")
    return value


def _digest(value: str, field_name: str) -> str:
    if type(value) is not str or _SHA256_HEX.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a full lowercase SHA-256")
    return value


def _scopes(value: frozenset[str], field_name: str) -> frozenset[str]:
    if not value or len(value) > 64:
        raise ValueError(f"{field_name} must contain 1 to 64 scopes")
    if any(
        type(item) is not str
        or len(item.encode("utf-8")) > 128
        or _CANONICAL_SCOPE.fullmatch(item) is None
        for item in value
    ):
        raise ValueError(f"{field_name} contains a non-canonical scope")
    return value


CanonicalIdentifier = Annotated[
    str, AfterValidator(lambda value: _identifier(value, "identifier"))
]
ApplicationIdentifier = Annotated[
    str, AfterValidator(lambda value: _application_id(value, "application_id"))
]
AgentIdentifier = Annotated[
    str, AfterValidator(lambda value: _agent_id(value, "agent_id"))
]
VersionIdentifier = Annotated[
    str, AfterValidator(lambda value: _version(value, "version"))
]
Sha256Digest = Annotated[str, AfterValidator(lambda value: _digest(value, "digest"))]
ScopeName = Annotated[
    str, AfterValidator(lambda value: next(iter(_scopes(frozenset({value}), "scope"))))
]
RevisionNumber = Annotated[int, Field(ge=1, le=2**63 - 1)]
HighWaterMark = Annotated[int, Field(ge=0, le=2**63 - 1)]


class _AuthenticationBinding(_StrictFrozenModel):
    tenant_id: CanonicalIdentifier
    credential_tenant_id: CanonicalIdentifier
    credential_id: CanonicalIdentifier
    subject_id: CanonicalIdentifier
    subject_kind: SubjectKind
    subject_principal_id: CanonicalIdentifier
    subject_revision: RevisionNumber
    membership_revision: RevisionNumber
    credential_revision: RevisionNumber
    credential_epoch: RevisionNumber
    caller_application_id: ApplicationIdentifier
    caller_application_version: VersionIdentifier
    caller_application_digest: Sha256Digest
    requested_scopes: frozenset[str]

    @field_validator("requested_scopes")
    @classmethod
    def _validate_requested_scopes(cls, value: frozenset[str]) -> frozenset[str]:
        return _scopes(value, "requested_scopes")


class BrowserAuthenticationBinding(_AuthenticationBinding):
    credential_kind: Literal["local_account", "external_identity"]
    session_id: CanonicalIdentifier
    session_revision: RevisionNumber

    @model_validator(mode="after")
    def _require_human(self) -> BrowserAuthenticationBinding:
        if self.subject_kind is not SubjectKind.HUMAN:
            raise ValueError("browser authentication requires a human subject")
        return self


class CredentialAuthenticationBinding(_AuthenticationBinding):
    credential_kind: Literal["api_key"]

    @model_validator(mode="after")
    def _reject_system(self) -> CredentialAuthenticationBinding:
        if self.subject_kind is SubjectKind.SYSTEM:
            raise ValueError("credential authentication cannot bind a system subject")
        return self


class DelegatedAuthenticationBinding(_AuthenticationBinding):
    """Exact long-running delegation, independent of the source Session TTL."""

    credential_kind: Literal["delegation"]
    root_delegation_id: CanonicalIdentifier
    source_session_id: CanonicalIdentifier
    tenant_epoch: RevisionNumber
    subject_epoch: RevisionNumber
    agent_epoch: RevisionNumber
    session_epoch: RevisionNumber
    delegation_expires_at: datetime
    delegation_ceiling_digest: Sha256Digest

    @field_validator("delegation_expires_at")
    @classmethod
    def _validate_delegation_expiry(cls, value: datetime) -> datetime:
        if (
            type(value) is not datetime
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise ValueError("delegation_expires_at must be a timezone-aware datetime")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _require_anchor_matching_subject(self) -> DelegatedAuthenticationBinding:
        """委托的锚决定委托人是谁 —— 不再一律要求人类。

        EIOS 是被调度的无人值守系统:把人类会话当成唯一的锚,等于让整条写平面
        依赖"有人登录着"。DB 0166 起锚可以是**已登记的服务主体**(锚点 id 以
        ``anchor:`` 为前缀存进同一个 session_id 轴,撤销开关因此逐字不变)。
        两条路各自收紧,不是放宽成"随便谁都行":会话锚仍然只认 human,
        锚点锚只认 service。
        """

        if self.source_session_id.startswith("anchor:"):
            if self.subject_kind is not SubjectKind.SERVICE:
                raise ValueError(
                    "service-anchored delegation requires a service subject"
                )
            return self
        if self.subject_kind is not SubjectKind.HUMAN:
            raise ValueError("session-anchored delegation requires a human subject")
        return self


AuthenticationBinding: TypeAlias = (
    BrowserAuthenticationBinding
    | CredentialAuthenticationBinding
    | DelegatedAuthenticationBinding
)


class AgentInvocationBinding(_StrictFrozenModel):
    tenant_id: CanonicalIdentifier
    actor_principal_id: CanonicalIdentifier
    agent_id: AgentIdentifier
    agent_revision: RevisionNumber
    release_id: CanonicalIdentifier
    release_revision: RevisionNumber
    release_digest: Sha256Digest
    agent_application_id: ApplicationIdentifier
    agent_application_version: VersionIdentifier
    agent_application_digest: Sha256Digest


class ScopeAuthority(_StrictFrozenModel):
    scope: ScopeName
    resource_type: ResourceType
    operation: Operation
    resource_id: str | None

    @field_validator("resource_id")
    @classmethod
    def _validate_resource_id(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        checked = _identifier(value, "resource_id")
        resource_type = info.data.get("resource_type")
        if type(resource_type) is not ResourceType or not checked.startswith(
            f"eios:{resource_type.value}:"
        ):
            raise ValueError("resource_id must match the exact resource_type prefix")
        return checked


class AuthorizationTarget(_StrictFrozenModel):
    tenant_id: CanonicalIdentifier
    resource_type: ResourceType
    resource_id: CanonicalIdentifier
    operation: Operation

    @model_validator(mode="after")
    def _validate_resource_prefix(self) -> AuthorizationTarget:
        expected = f"eios:{self.resource_type.value}:"
        if not self.resource_id.startswith(expected):
            raise ValueError("resource_id does not match resource_type")
        return self


class AuthorizationFactQuery(_StrictFrozenModel):
    tenant_id: CanonicalIdentifier
    authentication: AuthenticationBinding
    agent_invocation: AgentInvocationBinding | None
    target: AuthorizationTarget
    request_attributes: FrozenJsonObject = Field(
        default_factory=lambda: FrozenJsonMap({})
    )
    request_id: CanonicalIdentifier
    trace_id: CanonicalIdentifier

    @field_validator("request_attributes")
    @classmethod
    def _validate_request_attributes(cls, value: FrozenJsonObject) -> FrozenJsonObject:
        if len(value) > 64 or any(
            _CANONICAL_ATTRIBUTE_NAME.fullmatch(name) is None for name in value
        ):
            raise ValueError("request attributes contain invalid or excessive names")
        encoded = dumps(
            _canonical_value(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > 8_192:
            raise ValueError("request attributes exceed the canonical JSON byte limit")
        return value

    @model_validator(mode="after")
    def _validate_tenant_boundary(self) -> AuthorizationFactQuery:
        nested_tenants = {self.authentication.tenant_id, self.target.tenant_id}
        if self.agent_invocation is not None:
            nested_tenants.add(self.agent_invocation.tenant_id)
        if nested_tenants != {self.tenant_id}:
            raise ValueError("all authorization query bindings must use one tenant")
        return self


class AuthorizationFactDenialReason(str, Enum):
    SUBJECT_INACTIVE = "subject_inactive"
    MEMBERSHIP_INACTIVE = "membership_inactive"
    MEMBERSHIP_EXPIRED = "membership_expired"
    AUTHENTICATION_INACTIVE = "authentication_inactive"
    AUTHENTICATION_EXPIRED = "authentication_expired"
    AUTHENTICATION_STALE = "authentication_stale"
    ACTOR_INACTIVE = "actor_inactive"
    APPLICATION_INACTIVE = "application_inactive"
    AGENT_INACTIVE = "agent_inactive"
    AGENT_RELEASE_INACTIVE = "agent_release_inactive"
    RESOURCE_INACTIVE = "resource_inactive"
    SCOPE_INSUFFICIENT = "scope_insufficient"


class AuthorizationFactDenied(AuthorizationError):
    """A fact-level denial, optionally carrying what the database actually said.

    ``AUTHENTICATION_STALE`` is where every SQLSTATE 42501 from the authorization
    definers lands, and those definers raise it for at least six distinct
    reasons on the delegated path alone (root authority unavailable, action
    unavailable, approval unavailable, scope exceeds root, effects exceed root,
    resolver context mismatch). Collapsing all of them into "authentication is
    stale" makes the true cause unrecoverable — 2026-08-08 the delegated write
    plane reported it for hours while the real refusal was something else
    entirely. ``detail`` keeps the database's own message so the reason code
    stays a stable enum *and* the diagnosis survives.
    """

    code = "authorization_fact_denied"

    def __init__(
        self,
        reason: AuthorizationFactDenialReason,
        *,
        detail: str = "",
    ) -> None:
        if type(reason) is not AuthorizationFactDenialReason:
            raise TypeError("reason must be an AuthorizationFactDenialReason")
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason.value}: {detail}" if detail else reason.value)


def _aware_utc(value: datetime | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


AwareTimestamp = Annotated[
    datetime, AfterValidator(lambda value: _aware_utc(value, "timestamp"))
]


def _bounded_text(value: str, field_name: str, *, maximum: int = 1024) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field_name} must be bounded non-blank text")
    return value


def _repository_witness(value: str) -> str:
    return _bounded_text(value, "repository_witness", maximum=2048)


RepositoryWitness = Annotated[str, AfterValidator(_repository_witness)]


def _canonical_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if type(value) is bytes:
        return {"$bytes_hex": value.hex()}
    if isinstance(value, BaseModel):
        return _canonical_value(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, frozenset):
        canonical = tuple(_canonical_value(item) for item in value)
        return sorted(
            canonical,
            key=lambda item: dumps(
                item, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ),
        )
    if isinstance(value, (tuple, list)):
        return [_canonical_value(item) for item in value]
    if value is None or type(value) in (bool, int, float, str):
        return value
    raise TypeError("authorization fact contains a non-canonical value")


def _canonical_digest(value: object) -> str:
    encoded = dumps(
        _canonical_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def canonical_authority_digest(value: object) -> str:
    """Return the domain canonical digest for repository authority evidence."""
    return _canonical_digest(value)


class _RepositoryFacts(_StrictFrozenModel):
    repository_witness: RepositoryWitness
    snapshot_digest: Sha256Digest | None = None

    @model_validator(mode="after")
    def _seal_snapshot(self) -> _RepositoryFacts:
        expected = _canonical_digest(
            self.model_dump(
                mode="python",
                exclude={"repository_witness", "snapshot_digest"},
            )
        )
        if self.snapshot_digest is not None and self.snapshot_digest != expected:
            raise ValueError("snapshot_digest does not match authorization facts")
        object.__setattr__(self, "snapshot_digest", expected)
        return self


class SubjectFacts(_RepositoryFacts):
    tenant_id: CanonicalIdentifier
    subject_id: CanonicalIdentifier
    kind: SubjectKind
    status: Literal["active", "disabled"]
    revision: RevisionNumber


class MembershipFacts(_RepositoryFacts):
    tenant_id: CanonicalIdentifier
    subject_id: CanonicalIdentifier
    principal_id: CanonicalIdentifier
    kind: MembershipKind
    status: Literal["invited", "active", "suspended", "revoked"]
    valid_from: AwareTimestamp
    valid_until: AwareTimestamp | None
    revision: RevisionNumber

    @model_validator(mode="after")
    def _validate_interval(self) -> MembershipFacts:
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be later than valid_from")
        return self


class ActorFacts(_RepositoryFacts):
    tenant_id: CanonicalIdentifier
    actor_id: CanonicalIdentifier
    actor_principal_id: CanonicalIdentifier
    kind: SubjectKind
    status: Literal["active", "disabled", "revoked"]
    revision: RevisionNumber


class BrowserAuthenticationFacts(BrowserAuthenticationBinding):
    status: Literal["active", "revoked"]
    expires_at: AwareTimestamp
    absolute_expires_at: AwareTimestamp
    repository_witness: RepositoryWitness
    snapshot_digest: Sha256Digest | None = None

    @model_validator(mode="after")
    def _seal_fact(self) -> BrowserAuthenticationFacts:
        if self.expires_at > self.absolute_expires_at:
            raise ValueError("browser effective expiry exceeds absolute expiry")
        _seal_authentication_fact(self)
        return self


class CredentialAuthenticationFacts(CredentialAuthenticationBinding):
    status: Literal["active", "disabled", "revoked"]
    expires_at: AwareTimestamp | None
    repository_witness: RepositoryWitness
    snapshot_digest: Sha256Digest | None = None

    @model_validator(mode="after")
    def _seal_fact(self) -> CredentialAuthenticationFacts:
        _seal_authentication_fact(self)
        return self


class DelegatedAuthenticationFacts(DelegatedAuthenticationBinding):
    status: Literal["active", "revoked", "expired", "exhausted"]
    expires_at: AwareTimestamp
    repository_witness: RepositoryWitness
    snapshot_digest: Sha256Digest | None = None

    @model_validator(mode="after")
    def _seal_fact(self) -> DelegatedAuthenticationFacts:
        if self.expires_at != self.delegation_expires_at:
            raise ValueError(
                "delegated authentication expiry must come from the delegation"
            )
        _seal_authentication_fact(self)
        return self


AuthenticationFacts: TypeAlias = (
    BrowserAuthenticationFacts
    | CredentialAuthenticationFacts
    | DelegatedAuthenticationFacts
)


def _seal_authentication_fact(
    fact: (
        BrowserAuthenticationFacts
        | CredentialAuthenticationFacts
        | DelegatedAuthenticationFacts
    ),
) -> None:
    expected = _canonical_digest(
        fact.model_dump(
            mode="python", exclude={"repository_witness", "snapshot_digest"}
        )
    )
    if fact.snapshot_digest is not None and fact.snapshot_digest != expected:
        raise ValueError("snapshot_digest does not match authentication facts")
    object.__setattr__(fact, "snapshot_digest", expected)


def _validate_application_authority_contract(
    *,
    application_status: str,
    version_status: str,
    eligible: bool,
    mode: ApplicationMode,
    break_glass: bool,
    published_at: datetime | None,
    revoked_at: datetime | None,
    expires_at: datetime | None,
) -> None:
    if version_status == "draft":
        if published_at is not None or revoked_at is not None:
            raise ValueError(
                "draft application version cannot have lifecycle timestamps"
            )
    elif version_status == "published":
        if published_at is None or revoked_at is not None:
            raise ValueError("published application version has invalid lifecycle")
    elif published_at is None or revoked_at is None:
        raise ValueError("revoked application version requires lifecycle timestamps")
    if published_at is not None:
        if expires_at is not None and published_at >= expires_at:
            raise ValueError("application publication must precede expiry")
        if revoked_at is not None and revoked_at < published_at:
            raise ValueError("application revocation cannot precede publication")
    if (mode is ApplicationMode.RESTRICTED and break_glass) or (
        mode is ApplicationMode.UNRESTRICTED and not break_glass
    ):
        raise ValueError("application mode and break_glass are inconsistent")
    if break_glass:
        # Re-check the domain break-glass contract (applications.py) on the
        # repository fact: break-glass authority is only ever short-lived.
        if expires_at is None:
            raise ValueError("break-glass application authority requires expiry")
        if published_at is not None and not (
            published_at < expires_at
            <= published_at + MAX_BREAK_GLASS_LIFETIME
        ):
            raise ValueError(
                "break-glass expiry must be within fifteen minutes of publication"
            )
    if eligible and (application_status != "active" or version_status != "published"):
        raise ValueError("eligible application authority must be active and published")


class ApplicationFacts(_RepositoryFacts):
    tenant_id: CanonicalIdentifier
    application_id: ApplicationIdentifier
    application_revision: RevisionNumber
    version: VersionIdentifier
    version_revision: RevisionNumber
    version_digest: Sha256Digest
    record_digest: Sha256Digest
    application_status: Literal["active", "disabled", "revoked"]
    version_status: Literal["draft", "published", "revoked"]
    eligible: bool
    mode: ApplicationMode
    break_glass: bool
    resources: tuple[ResourceRestriction, ...] = Field(max_length=MAX_FACT_ITEMS)
    operations: tuple[OperationRestriction, ...] = Field(max_length=len(Operation))
    published_at: AwareTimestamp | None
    revoked_at: AwareTimestamp | None
    expires_at: AwareTimestamp | None

    @model_validator(mode="after")
    def _canonicalize_authority(self) -> ApplicationFacts:
        resources = tuple(sorted(self.resources, key=lambda item: item.key))
        operations = tuple(
            sorted(self.operations, key=lambda item: item.validated_operation.value)
        )
        if len(resources) != len({item.key for item in resources}):
            raise ValueError("application resources must be unique")
        if len(operations) != len({item.validated_operation for item in operations}):
            raise ValueError("application operations must be unique")
        if any(item.tenant_id != self.tenant_id for item in resources):
            raise ValueError("application resources must use the application tenant")
        _validate_application_authority_contract(
            application_status=self.application_status,
            version_status=self.version_status,
            eligible=self.eligible,
            mode=self.mode,
            break_glass=self.break_glass,
            published_at=self.published_at,
            revoked_at=self.revoked_at,
            expires_at=self.expires_at,
        )
        object.__setattr__(self, "resources", resources)
        object.__setattr__(self, "operations", operations)
        object.__setattr__(
            self,
            "snapshot_digest",
            _canonical_digest(
                self.model_dump(
                    mode="python",
                    exclude={"repository_witness", "snapshot_digest"},
                )
            ),
        )
        return self

    @property
    def allowed_resource_keys(self) -> tuple[tuple[str, str, str], ...]:
        return tuple(item.key for item in self.resources)

    @property
    def allowed_operations(self) -> frozenset[Operation]:
        return frozenset(item.validated_operation for item in self.operations)


class AgentFacts(_RepositoryFacts):
    tenant_id: CanonicalIdentifier
    agent_id: AgentIdentifier
    actor_principal_id: CanonicalIdentifier
    application_id: ApplicationIdentifier
    status: Literal["active", "disabled", "revoked"]
    revision: RevisionNumber


class ParentApplicationAuthorityFact(_StrictFrozenModel):
    tenant_id: CanonicalIdentifier
    application_id: ApplicationIdentifier
    application_revision: RevisionNumber
    version: VersionIdentifier
    version_revision: RevisionNumber
    version_digest: Sha256Digest
    record_digest: Sha256Digest
    application_status: Literal["active", "disabled", "revoked"]
    version_status: Literal["draft", "published", "revoked"]
    eligible: bool
    mode: ApplicationMode
    break_glass: bool
    resources: tuple[ResourceRestriction, ...] = Field(max_length=MAX_FACT_ITEMS)
    operations: tuple[OperationRestriction, ...] = Field(max_length=len(Operation))
    published_at: AwareTimestamp | None
    revoked_at: AwareTimestamp | None
    expires_at: AwareTimestamp | None

    @model_validator(mode="after")
    def _canonicalize_authority(self) -> ParentApplicationAuthorityFact:
        resources = tuple(sorted(self.resources, key=lambda item: item.key))
        operations = tuple(
            sorted(self.operations, key=lambda item: item.validated_operation.value)
        )
        if len(resources) != len({item.key for item in resources}):
            raise ValueError("application resources must be unique")
        if len(operations) != len({item.validated_operation for item in operations}):
            raise ValueError("application operations must be unique")
        if any(item.tenant_id != self.tenant_id for item in resources):
            raise ValueError("application resources must use the application tenant")
        _validate_application_authority_contract(
            application_status=self.application_status,
            version_status=self.version_status,
            eligible=self.eligible,
            mode=self.mode,
            break_glass=self.break_glass,
            published_at=self.published_at,
            revoked_at=self.revoked_at,
            expires_at=self.expires_at,
        )
        object.__setattr__(self, "resources", resources)
        object.__setattr__(self, "operations", operations)
        return self

    @property
    def allowed_resource_keys(self) -> frozenset[tuple[str, str, str]]:
        return frozenset(item.key for item in self.resources)

    @property
    def allowed_operations(self) -> frozenset[Operation]:
        return frozenset(item.validated_operation for item in self.operations)


class ParentAgentReleaseFact(_StrictFrozenModel):
    tenant_id: CanonicalIdentifier
    release_id: CanonicalIdentifier
    parent_release_id: CanonicalIdentifier | None
    agent_id: AgentIdentifier
    agent_revision: RevisionNumber
    agent_status: Literal["active", "disabled", "revoked"]
    application_id: ApplicationIdentifier
    application_version: VersionIdentifier
    application_version_digest: Sha256Digest
    release_digest: Sha256Digest
    status: Literal["active", "revoked"]
    eligible: bool
    revision: RevisionNumber
    released_at: AwareTimestamp
    revoked_at: AwareTimestamp | None
    application: ParentApplicationAuthorityFact

    @model_validator(mode="after")
    def _validate_application_binding(self) -> ParentAgentReleaseFact:
        if (
            self.tenant_id != self.application.tenant_id
            or self.application_id != self.application.application_id
            or self.application_version != self.application.version
            or self.application_version_digest != self.application.version_digest
        ):
            raise ValueError("parent release application authority is inconsistent")
        return self


class AgentReleaseFacts(_RepositoryFacts):
    tenant_id: CanonicalIdentifier
    release_id: CanonicalIdentifier
    agent_id: AgentIdentifier
    application_id: ApplicationIdentifier
    application_version: VersionIdentifier
    application_version_digest: Sha256Digest
    release_digest: Sha256Digest
    status: Literal["active", "revoked"]
    eligible: bool
    revision: RevisionNumber
    released_at: AwareTimestamp
    revoked_at: AwareTimestamp | None
    parent_release_id: CanonicalIdentifier | None
    parent_chain: tuple[ParentAgentReleaseFact, ...] = Field(max_length=32)
    chain_complete: bool

    @field_validator("parent_chain")
    @classmethod
    def _validate_parent_chain(
        cls, value: tuple[ParentAgentReleaseFact, ...]
    ) -> tuple[ParentAgentReleaseFact, ...]:
        release_ids = tuple(item.release_id for item in value)
        if len(release_ids) != len(set(release_ids)):
            raise ValueError("agent release parent chain must not contain duplicates")
        return value


class RevisionedAuthority(_StrictFrozenModel):
    authority_id: CanonicalIdentifier
    revision: RevisionNumber
    digest: Sha256Digest


def _unique_authorities(
    value: tuple[RevisionedAuthority, ...], field_name: str
) -> tuple[RevisionedAuthority, ...]:
    keys = tuple(item.authority_id for item in value)
    if len(keys) != len(set(keys)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return tuple(sorted(value, key=lambda item: item.authority_id))


class SubjectAuthorityFacts(_RepositoryFacts):
    tenant_id: CanonicalIdentifier
    subject_kind: GrantSubjectKind
    principal_id: CanonicalIdentifier
    groups: tuple[RevisionedAuthority, ...] = Field(max_length=MAX_FACT_ITEMS)
    roles: tuple[RevisionedAuthority, ...] = Field(max_length=MAX_FACT_ITEMS)
    attributes: FrozenJsonObject = Field(default_factory=lambda: FrozenJsonMap({}))
    attribute_revisions: tuple[RevisionedAuthority, ...] = Field(
        max_length=MAX_FACT_ITEMS
    )
    clearances: tuple[RevisionedAuthority, ...] = Field(max_length=MAX_FACT_ITEMS)
    revision: RevisionNumber
    valid_until: AwareTimestamp | None

    @field_validator("groups", "roles", "attribute_revisions", "clearances")
    @classmethod
    def _validate_authorities(
        cls, value: tuple[RevisionedAuthority, ...], info: Any
    ) -> tuple[RevisionedAuthority, ...]:
        return _unique_authorities(value, info.field_name)

    @field_validator("attributes")
    @classmethod
    def _validate_attributes(cls, value: FrozenJsonObject) -> FrozenJsonObject:
        if len(value) > 100 or any(
            _CANONICAL_ATTRIBUTE_NAME.fullmatch(name) is None for name in value
        ):
            raise ValueError("attributes contain invalid or excessive names")
        encoded = dumps(
            _canonical_value(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > 16_384:
            raise ValueError("attributes exceed the canonical JSON byte limit")
        return value

    @model_validator(mode="after")
    def _require_subject_kind(self) -> SubjectAuthorityFacts:
        if self.subject_kind is GrantSubjectKind.GROUP:
            raise ValueError("subject authority cannot use group as the direct subject")
        if set(self.attributes) != {
            item.authority_id for item in self.attribute_revisions
        }:
            raise ValueError("every attribute must have one revision witness")
        return self


class ResourceNodeFact(_StrictFrozenModel):
    resource: ResourceReference
    parent: ResourceReference | None
    active: bool


class ResourceDependencyFact(_StrictFrozenModel):
    source: ResourceReference
    target: ResourceReference
    target_active: bool
    required_operation: Operation


def _resource_key(resource: ResourceReference) -> tuple[str, str, str]:
    return (
        resource.tenant_id,
        resource.resource_type.value,
        resource.resource_id,
    )


class ResourceGraphFacts(_RepositoryFacts):
    tenant_id: CanonicalIdentifier
    root: ResourceNodeFact
    ancestors: tuple[ResourceNodeFact, ...] = Field(
        max_length=MAX_RESOURCE_PARENT_DEPTH + 1
    )
    dependencies: tuple[ResourceDependencyFact, ...] = Field(max_length=MAX_FACT_ITEMS)
    registry_revision: RevisionNumber
    registry_digest: Sha256Digest
    closure_complete: bool
    next_cursor: str | None

    @field_validator("dependencies")
    @classmethod
    def _canonicalize_dependencies(
        cls, value: tuple[ResourceDependencyFact, ...]
    ) -> tuple[ResourceDependencyFact, ...]:
        ordered = tuple(
            sorted(
                value,
                key=lambda edge: (
                    edge.source.resource_type.value,
                    edge.source.resource_id,
                    edge.source.security_revision,
                    edge.target.resource_type.value,
                    edge.target.resource_id,
                    edge.target.security_revision,
                    edge.required_operation.value,
                ),
            )
        )
        keys = tuple(
            (
                _resource_key(edge.source),
                edge.source.security_revision,
                _resource_key(edge.target),
                edge.target.security_revision,
                edge.required_operation,
            )
            for edge in ordered
        )
        if len(keys) != len(set(keys)):
            raise ValueError("dependencies must not contain duplicate edges")
        return ordered

    @field_validator("next_cursor")
    @classmethod
    def _validate_cursor(cls, value: str | None) -> str | None:
        return None if value is None else _bounded_text(value, "next_cursor")


class EffectiveGrantFact(_StrictFrozenModel):
    grant_id: CanonicalIdentifier
    subject_kind: GrantSubjectKind
    subject_id: CanonicalIdentifier
    role_id: CanonicalIdentifier
    role_revision: RevisionNumber
    role_digest: Sha256Digest
    resource: ResourceReference
    effective_resource: ResourceReference | None = None
    operations: frozenset[Operation]
    inherited: bool
    # Whether the grant row itself allows inheritance onto descendants.  The
    # SQL fact reader already filters non-inheriting ancestor rows; carrying
    # the flag lets the intersection re-check it as defense in depth, so a
    # repository that omits it fails closed for inherited rows.
    inherits: bool = False
    valid_from: AwareTimestamp
    valid_until: AwareTimestamp | None
    revision: RevisionNumber
    digest: Sha256Digest

    @field_validator("operations")
    @classmethod
    def _validate_operations(cls, value: frozenset[Operation]) -> frozenset[Operation]:
        if not value or len(value) > len(Operation):
            raise ValueError("operations must be non-empty and bounded")
        return value

    @model_validator(mode="after")
    def _validate_authority(self) -> EffectiveGrantFact:
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be later than valid_from")
        if self.effective_resource is None:
            object.__setattr__(self, "effective_resource", self.resource)
        if self.effective_resource is not None and (
            self.resource.tenant_id != self.effective_resource.tenant_id
        ):
            raise ValueError("grant source and effective resource must use one tenant")
        return self


class GrantFacts(_RepositoryFacts):
    tenant_id: CanonicalIdentifier
    subject_kind: GrantSubjectKind
    principal_id: CanonicalIdentifier
    grants: tuple[EffectiveGrantFact, ...] = Field(max_length=MAX_FACT_ITEMS)
    valid_until: AwareTimestamp | None
    complete: bool
    next_cursor: str | None
    revision: RevisionNumber

    @field_validator("grants")
    @classmethod
    def _validate_grants(
        cls, value: tuple[EffectiveGrantFact, ...]
    ) -> tuple[EffectiveGrantFact, ...]:
        ordered = tuple(sorted(value, key=lambda item: item.grant_id))
        if len(ordered) != len({item.grant_id for item in ordered}):
            raise ValueError("grants must not contain duplicate grant_id values")
        return ordered

    @field_validator("next_cursor")
    @classmethod
    def _validate_cursor(cls, value: str | None) -> str | None:
        return None if value is None else _bounded_text(value, "next_cursor")

    @model_validator(mode="after")
    def _require_subject_kind(self) -> GrantFacts:
        if self.subject_kind is GrantSubjectKind.GROUP:
            raise ValueError("grant facts cannot use group as the direct subject")
        return self


class ScopeAuthorityFacts(_RepositoryFacts):
    tenant_id: CanonicalIdentifier
    principal_id: CanonicalIdentifier
    binding_id: CanonicalIdentifier
    catalog_id: CanonicalIdentifier
    catalog_version: VersionIdentifier
    catalog_revision: RevisionNumber
    catalog_digest: Sha256Digest
    catalog_scopes: frozenset[str]
    required_scopes: frozenset[str]
    authorities: tuple[ScopeAuthority, ...] = Field(min_length=1, max_length=512)
    risk: AuthorizationRisk
    authorized_scopes: frozenset[str]
    revision: RevisionNumber
    valid_until: AwareTimestamp | None

    @field_validator("required_scopes", "authorized_scopes")
    @classmethod
    def _validate_scopes(cls, value: frozenset[str]) -> frozenset[str]:
        return _scopes(value, "scope set")

    @field_validator("catalog_scopes")
    @classmethod
    def _validate_catalog_scopes(cls, value: frozenset[str]) -> frozenset[str]:
        if not value or len(value) > MAX_FACT_ITEMS:
            raise ValueError("catalog_scopes must be non-empty and bounded")
        for scope in value:
            _scopes(frozenset({scope}), "catalog_scopes")
        return value

    @field_validator("authorities")
    @classmethod
    def _canonicalize_authorities(
        cls, value: tuple[ScopeAuthority, ...]
    ) -> tuple[ScopeAuthority, ...]:
        ordered = tuple(
            sorted(
                value,
                key=lambda item: (
                    item.scope,
                    item.resource_type.value,
                    item.operation.value,
                    "" if item.resource_id is None else item.resource_id,
                ),
            )
        )
        keys = tuple(
            (item.scope, item.resource_type, item.operation, item.resource_id)
            for item in ordered
        )
        if len(keys) != len(set(keys)):
            raise ValueError("scope authorities must not contain duplicates")
        return ordered

    @model_validator(mode="after")
    def _validate_catalog_binding(self) -> ScopeAuthorityFacts:
        if not self.required_scopes <= self.catalog_scopes:
            raise ValueError("required scopes are absent from the catalog")
        if not self.authorized_scopes <= self.catalog_scopes:
            raise ValueError("authorized scopes are absent from the catalog")
        if any(
            authority.scope not in self.catalog_scopes for authority in self.authorities
        ):
            raise ValueError("scope authority references an unknown catalog scope")
        return self


class ControlKind(str, Enum):
    MARKING = "marking"
    CLASSIFICATION = "classification"
    ORGANIZATION = "organization"


class ControlDominance(str, Enum):
    EXACT = "exact"
    RANK = "rank"
    HIERARCHY = "hierarchy"


class SubjectClearanceFact(_StrictFrozenModel):
    clearance_id: CanonicalIdentifier
    subject_kind: GrantSubjectKind
    principal_id: CanonicalIdentifier
    kind: ControlKind
    namespace: CanonicalIdentifier
    value: CanonicalIdentifier
    rank: int = Field(ge=0, le=32_767)
    dominates: frozenset[CanonicalIdentifier] = Field(max_length=256)
    revision: RevisionNumber
    digest: Sha256Digest
    valid_until: AwareTimestamp | None

    @model_validator(mode="after")
    def _validate_clearance(self) -> SubjectClearanceFact:
        if self.subject_kind is GrantSubjectKind.GROUP:
            raise ValueError("control clearance requires a direct subject kind")
        if self.value in self.dominates:
            raise ValueError("control clearance cannot dominate itself")
        return self


class ResourceControlRequirementFact(_StrictFrozenModel):
    requirement_id: CanonicalIdentifier
    resource: ResourceReference
    operation: Operation
    kind: ControlKind
    namespace: CanonicalIdentifier
    value: CanonicalIdentifier
    minimum_rank: int = Field(ge=0, le=32_767)
    dominance: ControlDominance
    revision: RevisionNumber
    digest: Sha256Digest
    valid_until: AwareTimestamp | None


class ControlFacts(_RepositoryFacts):
    tenant_id: CanonicalIdentifier
    subject_clearances: tuple[SubjectClearanceFact, ...] = Field(
        max_length=MAX_FACT_ITEMS
    )
    resource_requirements: tuple[ResourceControlRequirementFact, ...] = Field(
        max_length=MAX_FACT_ITEMS
    )
    complete: bool
    valid_until: AwareTimestamp | None
    revision: RevisionNumber

    @field_validator("subject_clearances")
    @classmethod
    def _canonicalize_clearances(
        cls, value: tuple[SubjectClearanceFact, ...]
    ) -> tuple[SubjectClearanceFact, ...]:
        ordered = tuple(sorted(value, key=lambda item: item.clearance_id))
        if len(ordered) != len({item.clearance_id for item in ordered}):
            raise ValueError("subject clearances must not contain duplicates")
        return ordered

    @field_validator("resource_requirements")
    @classmethod
    def _canonicalize_requirements(
        cls, value: tuple[ResourceControlRequirementFact, ...]
    ) -> tuple[ResourceControlRequirementFact, ...]:
        ordered = tuple(sorted(value, key=lambda item: item.requirement_id))
        if len(ordered) != len({item.requirement_id for item in ordered}):
            raise ValueError(
                "resource control requirements must not contain duplicates"
            )
        return ordered


class PolicyRuleFact(_StrictFrozenModel):
    binding_id: CanonicalIdentifier
    binding_digest: Sha256Digest
    resource: ResourceReference
    operation: Operation
    policy_set_id: CanonicalIdentifier
    active_version: RevisionNumber
    version_digest: Sha256Digest
    canonical_ast_utf8: bytes
    canonical_ast_sha256: Sha256Digest
    effect: PolicyEffect
    obligations: tuple[PolicyObligation, ...] = Field(default=(), max_length=64)
    dependencies: tuple[PolicyDependencyReference, ...] = Field(
        default=(), max_length=512
    )
    condition: PolicyExpression
    activation_head_revision: HighWaterMark
    activation_head_digest: Sha256Digest
    registry_revision: HighWaterMark
    registry_digest: Sha256Digest
    activation_witness: Sha256Digest
    published_at: AwareTimestamp

    @model_validator(mode="after")
    def _validate_rule(self) -> PolicyRuleFact:
        condition = validate_policy_expression(self.condition)
        canonical = canonicalize_policy_expression(condition)
        if (
            canonical.utf8 != self.canonical_ast_utf8
            or canonical.sha256 != self.canonical_ast_sha256
        ):
            raise ValueError("policy condition does not match immutable canonical AST")
        object.__setattr__(self, "condition", condition)
        return self


class PolicyFacts(_RepositoryFacts):
    tenant_id: CanonicalIdentifier
    rules: tuple[PolicyRuleFact, ...] = Field(max_length=MAX_FACT_ITEMS)
    complete: bool
    valid_until: AwareTimestamp | None
    revision: RevisionNumber

    @field_validator("rules")
    @classmethod
    def _canonicalize_rules(
        cls, value: tuple[PolicyRuleFact, ...]
    ) -> tuple[PolicyRuleFact, ...]:
        ordered = tuple(sorted(value, key=lambda item: item.binding_id))
        if len(ordered) != len({item.binding_id for item in ordered}):
            raise ValueError("policy rules must not contain duplicate bindings")
        return ordered


class DatabaseSourceRevisionFact(_StrictFrozenModel):
    source_name: CanonicalIdentifier
    revision: RevisionNumber
    digest: Sha256Digest
    event_high_water: HighWaterMark


class RevisionSourceFacts(_RepositoryFacts):
    tenant_id: CanonicalIdentifier
    schema_revision: RevisionNumber
    schema_digest: Sha256Digest
    event_high_water: HighWaterMark
    revision: RevisionNumber
    database_sources: tuple[DatabaseSourceRevisionFact, ...] = Field(
        default=(), max_length=128
    )

    @field_validator("database_sources")
    @classmethod
    def _canonicalize_database_sources(
        cls, value: tuple[DatabaseSourceRevisionFact, ...]
    ) -> tuple[DatabaseSourceRevisionFact, ...]:
        ordered = tuple(sorted(value, key=lambda item: item.source_name))
        if len(ordered) != len({item.source_name for item in ordered}):
            raise ValueError("database source revisions must be unique")
        return ordered


class RevisionVectorEntry(_StrictFrozenModel):
    category: CanonicalIdentifier
    key: str
    revision: HighWaterMark
    digest: Sha256Digest

    @field_validator("key")
    @classmethod
    def _validate_key(cls, value: str) -> str:
        return _bounded_text(value, "key")


class RevisionVector(_StrictFrozenModel):
    entries: tuple[RevisionVectorEntry, ...] = Field(max_length=MAX_FACT_ITEMS * 8)
    digest: str | None = None

    @field_validator("entries")
    @classmethod
    def _canonicalize_entries(
        cls, value: tuple[RevisionVectorEntry, ...]
    ) -> tuple[RevisionVectorEntry, ...]:
        ordered = tuple(sorted(value, key=lambda item: (item.category, item.key)))
        keys = tuple((item.category, item.key) for item in ordered)
        if not ordered or len(keys) != len(set(keys)):
            raise ValueError("revision vector entries must be non-empty and unique")
        return ordered

    @field_validator("digest")
    @classmethod
    def _validate_vector_digest(cls, value: str | None) -> str | None:
        return None if value is None else _digest(value, "digest")

    @model_validator(mode="after")
    def _seal_vector(self) -> RevisionVector:
        expected = _canonical_digest(
            tuple(entry.model_dump(mode="python") for entry in self.entries)
        )
        if self.digest is not None and self.digest != expected:
            raise ValueError("revision vector digest does not match its entries")
        object.__setattr__(self, "digest", expected)
        return self


class _ResolvedAuthorizationPayload(_StrictFrozenModel):
    query: AuthorizationFactQuery
    trusted_now: AwareTimestamp
    subject: SubjectFacts
    membership: MembershipFacts
    actor: ActorFacts
    authentication: AuthenticationFacts
    caller_application: ApplicationFacts
    agent: AgentFacts | None
    agent_release: AgentReleaseFacts | None
    agent_application: ApplicationFacts | None
    subject_authority: SubjectAuthorityFacts
    resource_graph: ResourceGraphFacts
    grants: GrantFacts
    scope_authority: ScopeAuthorityFacts
    controls: ControlFacts
    policies: PolicyFacts
    revision_vector: RevisionVector
    authoritative_expires_at: AwareTimestamp | None


from ._fact_resolver import (  # noqa: E402
    AuthorizationFactsProvider,
    AuthorizationFactsResolver,
    AuthorizationFactsUnitOfWork,
    ReadOnlyAuthorityView,
    ResolvedAuthorizationContext,
)


__all__ = [
    "ActorFacts",
    "AgentFacts",
    "AgentInvocationBinding",
    "AgentReleaseFacts",
    "ApplicationFacts",
    "AuthenticationBinding",
    "AuthenticationFacts",
    "AuthorizationFactDenied",
    "AuthorizationFactDenialReason",
    "AuthorizationFactQuery",
    "AuthorizationFactsProvider",
    "AuthorizationFactsResolver",
    "AuthorizationFactsUnitOfWork",
    "AuthorizationRisk",
    "AuthorizationTarget",
    "BrowserAuthenticationBinding",
    "BrowserAuthenticationFacts",
    "canonical_authority_digest",
    "ControlDominance",
    "ControlFacts",
    "ControlKind",
    "CredentialAuthenticationBinding",
    "CredentialAuthenticationFacts",
    "DelegatedAuthenticationBinding",
    "DelegatedAuthenticationFacts",
    "DatabaseSourceRevisionFact",
    "EffectiveGrantFact",
    "GrantFacts",
    "MembershipFacts",
    "ParentAgentReleaseFact",
    "ParentApplicationAuthorityFact",
    "PolicyEffect",
    "PolicyFacts",
    "PolicyRuleFact",
    "ReadOnlyAuthorityView",
    "ResolvedAuthorizationContext",
    "ResourceControlRequirementFact",
    "ResourceDependencyFact",
    "ResourceGraphFacts",
    "ResourceNodeFact",
    "RevisionSourceFacts",
    "RevisionVector",
    "RevisionVectorEntry",
    "RevisionedAuthority",
    "ScopeAuthority",
    "ScopeAuthorityFacts",
    "SubjectAuthorityFacts",
    "SubjectClearanceFact",
    "SubjectFacts",
]
