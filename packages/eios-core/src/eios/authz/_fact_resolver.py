from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import operator
import os
from enum import Enum
from types import MappingProxyType
from types import TracebackType
from threading import Lock
from typing import ContextManager, Never, Protocol, TypeVar
from weakref import WeakKeyDictionary, ref

from pydantic import BaseModel

from eios.identity.models import FrozenJsonMap, SubjectKind

from .applications import ApplicationMode
from .errors import AuthorizationUnavailable
from .facts import (
    ActorFacts,
    AgentFacts,
    AgentReleaseFacts,
    ApplicationFacts,
    AuthenticationBinding,
    AuthenticationFacts,
    AuthorizationFactDenied,
    AuthorizationFactDenialReason,
    AuthorizationFactQuery,
    AuthorizationTarget,
    BrowserAuthenticationBinding,
    BrowserAuthenticationFacts,
    ControlFacts,
    CredentialAuthenticationBinding,
    CredentialAuthenticationFacts,
    DelegatedAuthenticationBinding,
    DelegatedAuthenticationFacts,
    GrantFacts,
    MembershipFacts,
    ParentApplicationAuthorityFact,
    PolicyFacts,
    ResourceGraphFacts,
    RevisionSourceFacts,
    RevisionVector,
    RevisionVectorEntry,
    ScopeAuthorityFacts,
    SubjectAuthorityFacts,
    SubjectFacts,
    _ResolvedAuthorizationPayload,
    _aware_utc,
    _canonical_digest,
)
from .grants import GrantSubjectKind
from .operations import Operation
from .policy import canonicalize_policy_expression, validate_policy_expression
from .resources import (
    MAX_RESOURCE_DEPENDENCY_DEPTH,
    MAX_RESOURCE_PARENT_DEPTH,
    ResourceReference,
)


_RESOLVED_PUBLIC_FIELDS = frozenset(
    {
        "query",
        "trusted_now",
        "subject",
        "membership",
        "actor",
        "authentication",
        "caller_application",
        "agent",
        "agent_release",
        "agent_application",
        "subject_authority",
        "resource_graph",
        "grants",
        "scope_authority",
        "controls",
        "policies",
        "revision_vector",
        "authoritative_expires_at",
    }
)


class _FrozenAuthorityViewMeta(type):
    def __setattr__(cls, name: str, value: object) -> Never:
        del cls, name, value
        raise TypeError("authority view classes are immutable")

    def __delattr__(cls, name: str) -> Never:
        del cls, name
        raise TypeError("authority view classes are immutable")


class ReadOnlyAuthorityView(metaclass=_FrozenAuthorityViewMeta):
    """Structural, recursively immutable projection of an authority record."""

    __slots__ = ("__weakref__",)

    def __setattr__(self, name: str, value: object) -> Never:
        del name, value
        raise TypeError("authority views are immutable")

    def __delattr__(self, name: str) -> Never:
        del name
        raise TypeError("authority views are immutable")

    def __copy__(self) -> ReadOnlyAuthorityView:
        return self

    def __deepcopy__(
        self, memo: dict[int, object] | None = None
    ) -> ReadOnlyAuthorityView:
        del memo
        return self


def _build_authority_projector():
    record_values: WeakKeyDictionary[ReadOnlyAuthorityView, Mapping[str, object]] = (
        WeakKeyDictionary()
    )

    class _AuthorityRecordView(ReadOnlyAuthorityView):
        __slots__ = ()

        def __getattribute__(self, name: str) -> object:
            if name.startswith("_"):
                return object.__getattribute__(self, name)
            try:
                values = record_values[self]
            except (KeyError, TypeError):
                raise AuthorizationUnavailable(
                    "authority view is unavailable"
                ) from None
            try:
                return values[name]
            except KeyError:
                raise AttributeError(name) from None

        def __repr__(self) -> str:
            return "<ReadOnlyAuthorityView>"

    def project(root: object) -> object:
        memo: dict[int, object] = {}
        retained: list[object] = []

        def convert(value: object) -> object:
            if value is None or type(value) in (bool, int, float, str, bytes):
                return value
            if isinstance(value, (Enum, datetime)):
                return value
            # Several authority properties create temporary tuple/frozenset
            # values. Keep every projected source alive for the whole graph so
            # CPython cannot reuse an id for a later sibling and corrupt memo.
            retained.append(value)
            identity = id(value)
            if identity in memo:
                return memo[identity]
            if isinstance(value, BaseModel):
                view = object.__new__(_AuthorityRecordView)
                memo[identity] = view
                property_names = {
                    name
                    for model_type in type(value).__mro__
                    if model_type is not BaseModel
                    for name, member in vars(model_type).items()
                    if isinstance(member, property) and not name.startswith("_")
                }
                # Retain every raw property value until projection completes.
                # Otherwise a temporary tuple/frozenset may be collected and its
                # id reused, corrupting the identity memo across sibling fields.
                raw_values = {
                    name: getattr(value, name)
                    for name in (*type(value).model_fields, *sorted(property_names))
                }
                fields = MappingProxyType(
                    {name: convert(item) for name, item in raw_values.items()}
                )
                record_values[view] = fields
                return view
            if isinstance(value, Mapping):
                projected = MappingProxyType(
                    {convert(key): convert(item) for key, item in value.items()}
                )
                memo[identity] = projected
                return projected
            if isinstance(value, (tuple, list)):
                projected_tuple = tuple(convert(item) for item in value)
                memo[identity] = projected_tuple
                return projected_tuple
            if isinstance(value, (set, frozenset)):
                projected_set = frozenset(convert(item) for item in value)
                memo[identity] = projected_set
                return projected_set
            raise TypeError(f"unsupported authority view value: {type(value).__name__}")

        return convert(root)

    return project


_project_authority_view = _build_authority_projector()


class _IssuanceRegistry:
    """Single-issuance weak registry with irreversible per-handle revocation."""

    __slots__ = ("_entries", "_lock", "_tombstones")

    def __init__(self) -> None:
        self._entries: WeakKeyDictionary[object, object] = WeakKeyDictionary()
        self._tombstones: WeakKeyDictionary[object, bool] = WeakKeyDictionary()
        self._lock = Lock()

    def create(self, handle: object, entry: object) -> None:
        with self._lock:
            if handle in self._tombstones:
                raise AuthorizationUnavailable(
                    "authorization context issuance is not reusable"
                )
            if handle in self._entries:
                self._tombstones[handle] = True
                self._entries.pop(handle, None)
                raise AuthorizationUnavailable(
                    "authorization context issuance is not reusable"
                )
            self._entries[handle] = entry

    def lookup(self, handle: object) -> object | None:
        with self._lock:
            if handle in self._tombstones:
                return None
            return self._entries.get(handle)

    def revoke(self, handle: object) -> None:
        with self._lock:
            self._tombstones[handle] = True
            self._entries.pop(handle, None)

    def pop(self, handle: object) -> object | None:
        with self._lock:
            entry = self._entries.get(handle)
            self._tombstones[handle] = True
            self._entries.pop(handle, None)
            return entry

    def __getitem__(self, handle: object) -> object:
        with self._lock:
            return self._entries[handle]

    def __setitem__(self, handle: object, entry: object) -> None:
        self.create(handle, entry)

    def __delitem__(self, handle: object) -> None:
        with self._lock:
            if handle not in self._entries:
                self._tombstones[handle] = True
                raise KeyError(handle)
            self._tombstones[handle] = True
            del self._entries[handle]

    def __contains__(self, handle: object) -> bool:
        with self._lock:
            return handle not in self._tombstones and handle in self._entries

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


class _FrozenResolvedAuthorizationMeta(type):
    def __setattr__(cls, name: str, value: object) -> Never:
        del cls, name, value
        raise TypeError("resolved authorization context classes are immutable")

    def __delattr__(cls, name: str) -> Never:
        del cls, name
        raise TypeError("resolved authorization context classes are immutable")


class ResolvedAuthorizationContext(metaclass=_FrozenResolvedAuthorizationMeta):
    """Opaque issued authorization handle for the trusted in-process boundary."""

    __slots__ = ("__weakref__",)

    def __new__(cls, *args: object, **kwargs: object) -> Never:
        del cls, args, kwargs
        raise TypeError(
            "resolved authorization contexts are issued only by the resolver"
        )

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls.__module__ != __name__:
            raise TypeError("resolved authorization contexts cannot be subclassed")

    def __getattribute__(self, name: str) -> object:
        if name in {
            "_ResolvedAuthorizationContext__payload",
            "_ResolvedAuthorizationContext__issued_check",
            "_resolver_verified_payload",
        }:
            raise AttributeError("authorization context internals are opaque")
        try:
            return object.__getattribute__(self, name)
        except AuthorizationUnavailable:
            raise
        except AttributeError:
            if name in _RESOLVED_PUBLIC_FIELDS:
                raise AuthorizationUnavailable(
                    "authorization context payload field is unavailable"
                ) from None
            raise

    def __checked_payload(self) -> ReadOnlyAuthorityView:
        try:
            verifier = object.__getattribute__(self, "_resolver_verified_payload")
            payload = verifier(verify_integrity=False)
        except AuthorizationUnavailable:
            raise
        except Exception:
            raise AuthorizationUnavailable(
                "authorization context was not issued by the resolver"
            ) from None
        if not isinstance(payload, ReadOnlyAuthorityView):
            raise AuthorizationUnavailable(
                "authorization context issuance or payload is invalid"
            )
        return payload

    def _resolver_verified_payload(self, *, verify_integrity: bool) -> Never:
        del verify_integrity
        raise AuthorizationUnavailable(
            "authorization context was not issued by the resolver"
        )

    def verify_integrity(self) -> None:
        try:
            verifier = object.__getattribute__(self, "_resolver_verified_payload")
            payload = verifier(verify_integrity=True)
        except AuthorizationUnavailable:
            raise
        except Exception:
            raise AuthorizationUnavailable(
                "authorization context integrity could not be verified"
            ) from None
        if not isinstance(payload, ReadOnlyAuthorityView):
            raise AuthorizationUnavailable(
                "authorization context issuance or payload is invalid"
            )

    @property
    def query(self) -> ReadOnlyAuthorityView:
        return self.__checked_payload().query

    @property
    def trusted_now(self) -> datetime:
        return self.__checked_payload().trusted_now

    @property
    def subject(self) -> ReadOnlyAuthorityView:
        return self.__checked_payload().subject

    @property
    def membership(self) -> ReadOnlyAuthorityView:
        return self.__checked_payload().membership

    @property
    def actor(self) -> ReadOnlyAuthorityView:
        return self.__checked_payload().actor

    @property
    def authentication(self) -> ReadOnlyAuthorityView:
        return self.__checked_payload().authentication

    @property
    def caller_application(self) -> ReadOnlyAuthorityView:
        return self.__checked_payload().caller_application

    @property
    def agent(self) -> ReadOnlyAuthorityView | None:
        return self.__checked_payload().agent

    @property
    def agent_release(self) -> ReadOnlyAuthorityView | None:
        return self.__checked_payload().agent_release

    @property
    def agent_application(self) -> ReadOnlyAuthorityView | None:
        return self.__checked_payload().agent_application

    @property
    def subject_authority(self) -> ReadOnlyAuthorityView:
        return self.__checked_payload().subject_authority

    @property
    def resource_graph(self) -> ReadOnlyAuthorityView:
        return self.__checked_payload().resource_graph

    @property
    def grants(self) -> ReadOnlyAuthorityView:
        return self.__checked_payload().grants

    @property
    def scope_authority(self) -> ReadOnlyAuthorityView:
        return self.__checked_payload().scope_authority

    @property
    def controls(self) -> ReadOnlyAuthorityView:
        return self.__checked_payload().controls

    @property
    def policies(self) -> ReadOnlyAuthorityView:
        return self.__checked_payload().policies

    @property
    def revision_vector(self) -> ReadOnlyAuthorityView:
        return self.__checked_payload().revision_vector

    @property
    def authoritative_expires_at(self) -> datetime | None:
        return self.__checked_payload().authoritative_expires_at

    def __setattr__(self, name: str, value: object) -> Never:
        del name, value
        raise AuthorizationUnavailable("authorization context is immutable")

    def __delattr__(self, name: str) -> Never:
        del name
        raise AuthorizationUnavailable("authorization context is immutable")

    def __iter__(self) -> Never:
        raise TypeError("authorization context is opaque and not iterable")

    def __copy__(self) -> Never:
        raise AuthorizationUnavailable("authorization context cannot be copied")

    def __deepcopy__(self, memo: dict[int, object] | None = None) -> Never:
        del memo
        raise AuthorizationUnavailable("authorization context cannot be copied")

    def __reduce__(self) -> Never:
        raise AuthorizationUnavailable("authorization context cannot be serialized")

    def __reduce_ex__(self, protocol: int) -> Never:
        del protocol
        raise AuthorizationUnavailable("authorization context cannot be serialized")

    def __repr__(self) -> str:
        return "<ResolvedAuthorizationContext opaque>"


class AuthorizationFactsUnitOfWork(Protocol):
    repeatable_read: bool
    read_only: bool

    def trusted_now(self) -> datetime: ...

    def verify_repository_witness(
        self, *, fact_kind: str, snapshot_digest: str, repository_witness: str
    ) -> bool: ...

    def load_subject(self, tenant_id: str, subject_id: str) -> SubjectFacts | None: ...

    def load_membership(
        self, tenant_id: str, subject_id: str, principal_id: str
    ) -> MembershipFacts | None: ...

    def load_actor(
        self, tenant_id: str, actor_principal_id: str
    ) -> ActorFacts | None: ...

    def load_browser_authentication(
        self, binding: BrowserAuthenticationBinding
    ) -> BrowserAuthenticationFacts | None: ...

    def load_credential_authentication(
        self, binding: CredentialAuthenticationBinding
    ) -> CredentialAuthenticationFacts | None: ...

    def load_delegated_authentication(
        self, binding: DelegatedAuthenticationBinding
    ) -> DelegatedAuthenticationFacts | None: ...

    def load_caller_application(
        self, tenant_id: str, application_id: str, version: str
    ) -> ApplicationFacts | None: ...

    def load_agent(self, tenant_id: str, agent_id: str) -> AgentFacts | None: ...

    def load_agent_release(
        self, tenant_id: str, release_id: str
    ) -> AgentReleaseFacts | None: ...

    def load_agent_application(
        self, tenant_id: str, application_id: str, version: str
    ) -> ApplicationFacts | None: ...

    def load_subject_authority(
        self,
        tenant_id: str,
        subject_kind: GrantSubjectKind,
        principal_id: str,
    ) -> SubjectAuthorityFacts | None: ...

    def load_resource_graph(
        self, target: AuthorizationTarget
    ) -> ResourceGraphFacts | None: ...

    def load_grants(
        self,
        tenant_id: str,
        subject_kind: GrantSubjectKind,
        principal_id: str,
        graph: ResourceGraphFacts,
    ) -> GrantFacts | None: ...

    def load_scope_authority(
        self, query: AuthorizationFactQuery
    ) -> ScopeAuthorityFacts | None: ...

    def load_controls(
        self, query: AuthorizationFactQuery, graph: ResourceGraphFacts
    ) -> ControlFacts | None: ...

    def load_policies(
        self, query: AuthorizationFactQuery, graph: ResourceGraphFacts
    ) -> PolicyFacts | None: ...

    def load_revision_source(
        self, query: AuthorizationFactQuery
    ) -> RevisionSourceFacts | None: ...


class AuthorizationFactsProvider(Protocol):
    def open_unit_of_work(
        self, query: AuthorizationFactQuery
    ) -> ContextManager[AuthorizationFactsUnitOfWork]: ...


class _UnavailableAuthorizationFactsProvider:
    def open_unit_of_work(
        self, query: AuthorizationFactQuery
    ) -> ContextManager[AuthorizationFactsUnitOfWork]:
        del query
        raise AuthorizationUnavailable("authorization facts provider is unavailable")


FactT = TypeVar("FactT", bound=BaseModel)


class AuthorizationFactsResolver:
    def __init__(self, provider: AuthorizationFactsProvider | None = None) -> None:
        self._provider = provider or _UnavailableAuthorizationFactsProvider()

    def resolve(self, query: AuthorizationFactQuery) -> ResolvedAuthorizationContext:
        checked_query = _exact_model(
            query, AuthorizationFactQuery, "authorization fact query is invalid"
        )
        unresolved = object()
        resolved: object = unresolved
        try:
            manager = self._provider.open_unit_of_work(checked_query)
            unit_of_work = manager.__enter__()
        except BaseException as error:
            _raise_resolution_failure(error, error.__traceback__)

        failure: BaseException | None = None
        failure_traceback: TracebackType | None = None
        try:
            resolved = self._resolve_in_unit_of_work(checked_query, unit_of_work)
        except BaseException as error:
            failure = error
            failure_traceback = error.__traceback__

        try:
            if failure is None:
                manager.__exit__(None, None, None)
            else:
                manager.__exit__(type(failure), failure, failure_traceback)
        except BaseException as exit_error:
            _raise_resolution_failure(exit_error, exit_error.__traceback__)

        if failure is not None:
            _raise_resolution_failure(failure, failure_traceback)
        if (
            resolved is unresolved
            or not isinstance(resolved, ResolvedAuthorizationContext)
            or type(resolved) is ResolvedAuthorizationContext
            or type(resolved).__base__ is not ResolvedAuthorizationContext
        ):
            raise AuthorizationUnavailable("authorization facts could not be resolved")
        return resolved

    def resolve_in_unit_of_work(
        self,
        query: AuthorizationFactQuery,
        unit_of_work: AuthorizationFactsUnitOfWork,
    ) -> ResolvedAuthorizationContext:
        """Resolve without owning the transaction, for atomic decision auditing."""
        checked_query = _exact_model(
            query, AuthorizationFactQuery, "authorization fact query is invalid"
        )
        try:
            return self._resolve_in_unit_of_work(checked_query, unit_of_work)
        except BaseException as error:
            _raise_resolution_failure(error, error.__traceback__)

    def _resolve_in_unit_of_work(
        self,
        query: AuthorizationFactQuery,
        unit_of_work: AuthorizationFactsUnitOfWork,
    ) -> ResolvedAuthorizationContext:
        if (
            type(unit_of_work.repeatable_read) is not bool
            or not unit_of_work.repeatable_read
            or type(unit_of_work.read_only) is not bool
        ):
            raise AuthorizationUnavailable(
                "authorization facts require repeatable-read transaction metadata"
            )
        now = _aware_utc(unit_of_work.trusted_now(), "trusted_now")
        assert now is not None
        authentication_binding = query.authentication

        subject = _load_fact(
            unit_of_work,
            SubjectFacts,
            unit_of_work.load_subject(
                query.tenant_id, authentication_binding.subject_id
            ),
        )
        _same_tenant(query.tenant_id, subject.tenant_id)
        if (
            subject.subject_id != authentication_binding.subject_id
            or subject.kind is not authentication_binding.subject_kind
            or subject.revision != authentication_binding.subject_revision
        ):
            raise AuthorizationUnavailable("subject facts do not match authentication")
        if subject.status != "active":
            raise AuthorizationFactDenied(
                AuthorizationFactDenialReason.SUBJECT_INACTIVE
            )

        membership = _load_fact(
            unit_of_work,
            MembershipFacts,
            unit_of_work.load_membership(
                query.tenant_id,
                authentication_binding.subject_id,
                authentication_binding.subject_principal_id,
            ),
        )
        _same_tenant(query.tenant_id, membership.tenant_id)
        if (
            membership.subject_id != subject.subject_id
            or membership.principal_id != authentication_binding.subject_principal_id
            or membership.revision != authentication_binding.membership_revision
        ):
            raise AuthorizationFactDenied(
                AuthorizationFactDenialReason.AUTHENTICATION_STALE
            )
        if membership.status != "active" or now < membership.valid_from:
            raise AuthorizationFactDenied(
                AuthorizationFactDenialReason.MEMBERSHIP_INACTIVE
            )
        if membership.valid_until is not None and now >= membership.valid_until:
            raise AuthorizationFactDenied(
                AuthorizationFactDenialReason.MEMBERSHIP_EXPIRED
            )

        invocation = query.agent_invocation
        expected_actor_principal = (
            authentication_binding.subject_principal_id
            if invocation is None
            else invocation.actor_principal_id
        )
        actor = _load_fact(
            unit_of_work,
            ActorFacts,
            unit_of_work.load_actor(query.tenant_id, expected_actor_principal),
        )
        _same_tenant(query.tenant_id, actor.tenant_id)
        if actor.status != "active":
            raise AuthorizationFactDenied(AuthorizationFactDenialReason.ACTOR_INACTIVE)
        if invocation is None:
            if (
                actor.actor_id != authentication_binding.subject_id
                or actor.actor_principal_id
                != authentication_binding.subject_principal_id
                or actor.kind is not authentication_binding.subject_kind
                or actor.revision != authentication_binding.subject_revision
            ):
                raise AuthorizationUnavailable("direct actor facts are inconsistent")
        elif (
            actor.actor_id != invocation.agent_id
            or actor.actor_principal_id != invocation.actor_principal_id
            or actor.kind is not SubjectKind.AGENT
            or actor.revision != invocation.agent_revision
        ):
            raise AuthorizationUnavailable("agent actor facts are inconsistent")

        if type(authentication_binding) is BrowserAuthenticationBinding:
            authentication = _load_fact(
                unit_of_work,
                BrowserAuthenticationFacts,
                unit_of_work.load_browser_authentication(authentication_binding),
            )
        elif type(authentication_binding) is CredentialAuthenticationBinding:
            authentication = _load_fact(
                unit_of_work,
                CredentialAuthenticationFacts,
                unit_of_work.load_credential_authentication(authentication_binding),
            )
        elif type(authentication_binding) is DelegatedAuthenticationBinding:
            authentication = _load_fact(
                unit_of_work,
                DelegatedAuthenticationFacts,
                unit_of_work.load_delegated_authentication(authentication_binding),
            )
        else:
            raise AuthorizationUnavailable("authentication binding type is invalid")
        _same_tenant(query.tenant_id, authentication.tenant_id)
        _validate_authentication_binding(authentication_binding, authentication)
        if authentication.status != "active":
            raise AuthorizationFactDenied(
                AuthorizationFactDenialReason.AUTHENTICATION_INACTIVE
            )
        if authentication.expires_at is not None and now >= authentication.expires_at:
            raise AuthorizationFactDenied(
                AuthorizationFactDenialReason.AUTHENTICATION_EXPIRED
            )

        caller_application = _load_fact(
            unit_of_work,
            ApplicationFacts,
            unit_of_work.load_caller_application(
                query.tenant_id,
                authentication_binding.caller_application_id,
                authentication_binding.caller_application_version,
            ),
        )
        _validate_application(
            query.tenant_id,
            caller_application,
            application_id=authentication_binding.caller_application_id,
            version=authentication_binding.caller_application_version,
            version_digest=authentication_binding.caller_application_digest,
            now=now,
        )

        agent: AgentFacts | None = None
        agent_release: AgentReleaseFacts | None = None
        agent_application: ApplicationFacts | None = None
        if invocation is not None:
            agent = _load_fact(
                unit_of_work,
                AgentFacts,
                unit_of_work.load_agent(query.tenant_id, invocation.agent_id),
            )
            _same_tenant(query.tenant_id, agent.tenant_id)
            if agent.status != "active":
                raise AuthorizationFactDenied(
                    AuthorizationFactDenialReason.AGENT_INACTIVE
                )
            if (
                agent.agent_id != invocation.agent_id
                or agent.actor_principal_id != invocation.actor_principal_id
                or agent.application_id != invocation.agent_application_id
                or agent.revision != invocation.agent_revision
            ):
                raise AuthorizationUnavailable("agent facts do not match invocation")

            agent_release = _load_fact(
                unit_of_work,
                AgentReleaseFacts,
                unit_of_work.load_agent_release(query.tenant_id, invocation.release_id),
            )
            _same_tenant(query.tenant_id, agent_release.tenant_id)
            if (
                agent_release.status != "active"
                or not agent_release.eligible
                or agent_release.revoked_at is not None
                or agent_release.released_at > now
            ):
                raise AuthorizationFactDenied(
                    AuthorizationFactDenialReason.AGENT_RELEASE_INACTIVE
                )
            if (
                agent_release.release_id != invocation.release_id
                or agent_release.agent_id != invocation.agent_id
                or agent_release.application_id != invocation.agent_application_id
                or agent_release.application_version
                != invocation.agent_application_version
                or agent_release.application_version_digest
                != invocation.agent_application_digest
                or agent_release.release_digest != invocation.release_digest
                or agent_release.revision != invocation.release_revision
            ):
                raise AuthorizationUnavailable(
                    "agent release facts do not match invocation"
                )
            agent_application = _load_fact(
                unit_of_work,
                ApplicationFacts,
                unit_of_work.load_agent_application(
                    query.tenant_id,
                    invocation.agent_application_id,
                    invocation.agent_application_version,
                ),
            )
            _validate_application(
                query.tenant_id,
                agent_application,
                application_id=invocation.agent_application_id,
                version=invocation.agent_application_version,
                version_digest=invocation.agent_application_digest,
                now=now,
            )
            _validate_agent_release_chain(agent_release, agent_application, now)

        try:
            subject_kind = GrantSubjectKind(authentication_binding.subject_kind.value)
        except ValueError:
            raise AuthorizationUnavailable(
                "authentication subject kind is unsupported"
            ) from None
        subject_authority = _load_fact(
            unit_of_work,
            SubjectAuthorityFacts,
            unit_of_work.load_subject_authority(
                query.tenant_id,
                subject_kind,
                authentication_binding.subject_principal_id,
            ),
        )
        _same_tenant(query.tenant_id, subject_authority.tenant_id)
        if (
            subject_authority.subject_kind is not subject_kind
            or subject_authority.principal_id
            != authentication_binding.subject_principal_id
        ):
            raise AuthorizationUnavailable("subject authority facts are inconsistent")
        _require_future(subject_authority.valid_until, now, "subject authority")

        resource_graph = _load_fact(
            unit_of_work,
            ResourceGraphFacts,
            unit_of_work.load_resource_graph(query.target),
        )
        _validate_resource_graph(query, resource_graph)

        grants = _load_fact(
            unit_of_work,
            GrantFacts,
            unit_of_work.load_grants(
                query.tenant_id,
                subject_kind,
                authentication_binding.subject_principal_id,
                resource_graph,
            ),
        )
        _same_tenant(query.tenant_id, grants.tenant_id)
        if (
            grants.subject_kind is not subject_kind
            or grants.principal_id != authentication_binding.subject_principal_id
            or not grants.complete
            or grants.next_cursor is not None
        ):
            raise AuthorizationUnavailable("grant facts are incomplete or inconsistent")
        group_ids = {group.authority_id for group in subject_authority.groups}
        roles = {role.authority_id: role for role in subject_authority.roles}
        graph_resources = {
            _resource_key(resource): resource.security_revision
            for resource, _ in _graph_targets(
                resource_graph, root_operation=query.target.operation
            )
        }
        graph_resources.update(
            {
                _resource_key(node.resource): node.resource.security_revision
                for node in resource_graph.ancestors
            }
        )
        for grant in grants.grants:
            _same_tenant(query.tenant_id, grant.resource.tenant_id)
            direct_subject = (
                grant.subject_kind is subject_kind
                and grant.subject_id == authentication_binding.subject_principal_id
            )
            group_subject = (
                grant.subject_kind is GrantSubjectKind.GROUP
                and grant.subject_id in group_ids
            )
            if not (direct_subject or group_subject):
                raise AuthorizationUnavailable(
                    "grant subject authority is inconsistent"
                )
            role = roles.get(grant.role_id)
            if (
                role is None
                or role.revision != grant.role_revision
                or role.digest != grant.role_digest
            ):
                raise AuthorizationUnavailable("grant role authority is inconsistent")
            if graph_resources.get(_resource_key(grant.resource)) != (
                grant.resource.security_revision
            ):
                raise AuthorizationUnavailable(
                    "grant resource authority is inconsistent"
                )
            if now < grant.valid_from or (
                grant.valid_until is not None and now >= grant.valid_until
            ):
                raise AuthorizationUnavailable("grant facts contain an inactive grant")
        _require_future(grants.valid_until, now, "grant facts")

        scope_authority = _load_fact(
            unit_of_work,
            ScopeAuthorityFacts,
            unit_of_work.load_scope_authority(query),
        )
        _same_tenant(query.tenant_id, scope_authority.tenant_id)
        requested_scopes = authentication_binding.requested_scopes
        if scope_authority.principal_id != authentication_binding.subject_principal_id:
            raise AuthorizationUnavailable("scope binding facts are inconsistent")
        if not requested_scopes <= scope_authority.catalog_scopes:
            raise AuthorizationUnavailable("requested scope is unknown to the catalog")
        if not scope_authority.required_scopes <= requested_scopes:
            raise AuthorizationUnavailable(
                "scope catalog binding does not match request"
            )
        if any(
            authority.scope not in requested_scopes
            for authority in scope_authority.authorities
        ):
            raise AuthorizationUnavailable("scope catalog mapping is inconsistent")
        if not requested_scopes <= scope_authority.authorized_scopes:
            raise AuthorizationFactDenied(
                AuthorizationFactDenialReason.SCOPE_INSUFFICIENT
            )
        if not scope_authority.required_scopes <= (
            requested_scopes & scope_authority.authorized_scopes
        ):
            raise AuthorizationFactDenied(
                AuthorizationFactDenialReason.SCOPE_INSUFFICIENT
            )
        _validate_scope_graph_authorities(
            scope_authority,
            resource_graph,
            root_operation=query.target.operation,
            usable_scopes=requested_scopes & scope_authority.authorized_scopes,
        )
        _require_future(scope_authority.valid_until, now, "scope authority")

        controls = _load_fact(
            unit_of_work,
            ControlFacts,
            unit_of_work.load_controls(query, resource_graph),
        )
        _same_tenant(query.tenant_id, controls.tenant_id)
        if not controls.complete:
            raise AuthorizationUnavailable("control facts are incomplete")
        _validate_controls(
            controls,
            resource_graph,
            root_operation=query.target.operation,
            subject_kind=subject_kind,
            principal_id=authentication_binding.subject_principal_id,
            now=now,
        )
        _require_future(controls.valid_until, now, "control facts")

        policies = _load_fact(
            unit_of_work,
            PolicyFacts,
            unit_of_work.load_policies(query, resource_graph),
        )
        _same_tenant(query.tenant_id, policies.tenant_id)
        if not policies.complete:
            raise AuthorizationUnavailable("policy facts are incomplete")
        _validate_policies(
            policies,
            resource_graph,
            root_operation=query.target.operation,
            now=now,
        )
        _require_future(policies.valid_until, now, "policy facts")

        revision_source = _load_fact(
            unit_of_work,
            RevisionSourceFacts,
            unit_of_work.load_revision_source(query),
        )
        _same_tenant(query.tenant_id, revision_source.tenant_id)
        revision_vector = _build_revision_vector(
            subject=subject,
            membership=membership,
            actor=actor,
            authentication=authentication,
            caller_application=caller_application,
            agent=agent,
            agent_release=agent_release,
            agent_application=agent_application,
            subject_authority=subject_authority,
            resource_graph=resource_graph,
            grants=grants,
            scope_authority=scope_authority,
            controls=controls,
            policies=policies,
            revision_source=revision_source,
        )
        authoritative_expiry = _earliest_expiry(
            membership.valid_until,
            authentication.expires_at,
            caller_application.expires_at,
            None if agent_application is None else agent_application.expires_at,
            subject_authority.valid_until,
            grants.valid_until,
            *(grant.valid_until for grant in grants.grants),
            scope_authority.valid_until,
            controls.valid_until,
            policies.valid_until,
            *(
                parent.application.expires_at
                for parent in (
                    () if agent_release is None else agent_release.parent_chain
                )
            ),
            *(clearance.valid_until for clearance in controls.subject_clearances),
            *(
                requirement.valid_until
                for requirement in controls.resource_requirements
            ),
        )
        payload = _ResolvedAuthorizationPayload(
            query=query,
            trusted_now=now,
            subject=subject,
            membership=membership,
            actor=actor,
            authentication=authentication,
            caller_application=caller_application,
            agent=agent,
            agent_release=agent_release,
            agent_application=agent_application,
            subject_authority=subject_authority,
            resource_graph=resource_graph,
            grants=grants,
            scope_authority=scope_authority,
            controls=controls,
            policies=policies,
            revision_vector=revision_vector,
            authoritative_expires_at=authoritative_expiry,
        )
        payload_digest = _canonical_digest(payload)
        authority_view = _project_authority_view(payload)
        if not isinstance(authority_view, ReadOnlyAuthorityView):
            raise AuthorizationUnavailable(
                "authorization authority view could not be constructed"
            )
        resolver_issuance = object()
        issuance_registry = _IssuanceRegistry()
        # Threat model: ordinary in-process Python object operations. Arbitrary
        # interpreter-level closure-cell rewriting is intentionally out of scope.
        lookup_issuance = issuance_registry.lookup
        revoke_issuance = issuance_registry.revoke
        issued_type: type[ResolvedAuthorizationContext]
        issued_entry: tuple[
            _ResolvedAuthorizationPayload,
            str,
            ReadOnlyAuthorityView,
            object,
            type[ResolvedAuthorizationContext],
        ]

        class _ResolverIssuedAuthorizationContext(ResolvedAuthorizationContext):
            __slots__ = ()

            def _resolver_verified_payload(
                self, *, verify_integrity: bool, project: bool = True
            ) -> object:
                try:
                    registered = lookup_issuance(self)
                except (KeyError, TypeError, ValueError):
                    registered = None
                if (
                    registered is not issued_entry
                    or type(payload_digest) is not str
                    or type(self) is not issued_type
                ):
                    revoke_issuance(self)
                    raise AuthorizationUnavailable(
                        "authorization context issuance registry is inconsistent"
                    )
                if not verify_integrity:
                    return authority_view if project else payload
                try:
                    current_digest = _canonical_digest(payload)
                except Exception:
                    revoke_issuance(self)
                    raise AuthorizationUnavailable(
                        "authorization context payload is invalid"
                    ) from None
                if type(current_digest) is not str or current_digest != payload_digest:
                    revoke_issuance(self)
                    raise AuthorizationUnavailable(
                        "authorization context payload was modified"
                    )
                return authority_view if project else payload

        issued_type = _ResolverIssuedAuthorizationContext
        context = object.__new__(issued_type)
        issued_entry = (
            payload,
            payload_digest,
            authority_view,
            resolver_issuance,
            issued_type,
        )
        issuance_registry.create(context, issued_entry)
        return context


def _raise_resolution_failure(
    error: BaseException, traceback: TracebackType | None
) -> Never:
    if isinstance(error, (AuthorizationFactDenied, AuthorizationUnavailable)):
        raise error.with_traceback(traceback)
    if isinstance(error, Exception):
        raise AuthorizationUnavailable(
            "authorization facts could not be resolved"
        ) from None
    raise error.with_traceback(traceback)


# NexLoop adaptation (NX-049 8a): verified-instance registry.
#
# Strict frozen fact models revalidate on every model_validate
# (revalidate_instances="always"), which recomputes every seal digest of a fact
# that was already fully validated when it was parsed. The registry lets
# _exact_model return such an instance unchanged instead of revalidating it.
#
# Only verified_model_from_json() adds entries, and only for the instance it
# has just produced itself by a full model_validate_json; no function accepts a
# caller-supplied instance for registration. Each entry keeps the identity of
# every object reachable from the instance at registration (model fields,
# private attributes, container items, scalars and class references). A lookup
# re-walks the instance and compares identities, so any later in-place change
# (object.__setattr__, __dict__ edits, container mutation) misses and takes the
# upstream full revalidation unchanged. Entries die with their instance (weak
# reference). Instances from model_construct, model_copy or any other path are
# never registered and are always fully revalidated.
# NEXLOOP_EIOS_FULL_REVALIDATION=1 (or _TRUST_VERIFIED = False) disables the
# shortcut.
_TRUST_VERIFIED = os.environ.get("NEXLOOP_EIOS_FULL_REVALIDATION") != "1"
_VERIFIED: dict[int, tuple[object, list[object]]] = {}
_VERIFIED_LOCK = Lock()
_SCALARS = (type(None), bool, int, float, str, bytes)


def _reachable(root: object) -> list[object] | None:
    """Every object reachable from a frozen fact, in deterministic order."""
    out: list[object] = []
    stack: list[object] = [root]
    while stack:
        value = stack.pop()
        out.append(value)
        kind = type(value)
        if kind in _SCALARS or isinstance(value, (Enum, datetime, type)):
            continue
        if isinstance(value, BaseModel):
            fields = value.__dict__
            private = value.__pydantic_private__
            extra = value.__pydantic_extra__
            out.append(fields)
            out.append(private)
            out.append(extra)
            stack.extend(fields.values())
            if private:
                stack.extend(private.values())
            if extra:
                stack.extend(extra.values())
        elif kind in (tuple, list, frozenset, set):
            stack.extend(value)  # type: ignore[arg-type]
        elif kind is dict:
            for key, item in value.items():  # type: ignore[attr-defined]
                stack.append(key)
                stack.append(item)
        elif kind is FrozenJsonMap:
            data = value._data  # type: ignore[attr-defined]
            out.append(data)
            for key, item in data.items():
                stack.append(key)
                stack.append(item)
        else:
            return None
    return out


def verified_model_from_json(model: type[FactT], text: str | bytes) -> FactT:
    """Fully validate JSON into a strict frozen model and register the result."""
    instance = model.model_validate_json(text)
    config = model.model_config
    if (
        _TRUST_VERIFIED
        and type(instance) is model
        and config.get("frozen") is True
        and config.get("strict") is True
        and config.get("revalidate_instances") == "always"
    ):
        nodes = _reachable(instance)
        if nodes is not None:
            key = id(instance)

            def forget(dead: object, key: int = key) -> None:
                entry = _VERIFIED.get(key)
                if entry is not None and entry[0] is dead:
                    _VERIFIED.pop(key, None)

            with _VERIFIED_LOCK:
                _VERIFIED[key] = (ref(instance, forget), nodes[1:])
    return instance


def _is_verified(value: object) -> bool:
    entry = _VERIFIED.get(id(value))
    if entry is None or entry[0]() is not value:
        return False
    current = _reachable(value)
    recorded = entry[1]
    return (
        current is not None
        and len(current) == len(recorded) + 1
        and all(map(operator.is_, current[1:], recorded))
    )


def _exact_model(value: object, expected: type[FactT], message: str) -> FactT:
    if type(value) is not expected:
        raise AuthorizationUnavailable(message)
    if _TRUST_VERIFIED and _is_verified(value):
        return value  # type: ignore[return-value]
    try:
        return expected.model_validate(value, strict=True)
    except Exception:
        raise AuthorizationUnavailable(message) from None


def _load_fact(
    unit_of_work: AuthorizationFactsUnitOfWork,
    expected: type[FactT],
    value: object,
) -> FactT:
    fact = _exact_model(value, expected, f"{expected.__name__} is unavailable")
    snapshot_digest = getattr(fact, "snapshot_digest", None)
    repository_witness = getattr(fact, "repository_witness", None)
    if type(snapshot_digest) is not str or type(repository_witness) is not str:
        raise AuthorizationUnavailable("authorization fact attestation is missing")
    try:
        verified = unit_of_work.verify_repository_witness(
            fact_kind=expected.__name__,
            snapshot_digest=snapshot_digest,
            repository_witness=repository_witness,
        )
    except Exception:
        raise AuthorizationUnavailable(
            "authorization fact attestation could not be verified"
        ) from None
    if type(verified) is not bool or not verified:
        raise AuthorizationUnavailable("authorization fact attestation is invalid")
    return fact


def _same_tenant(expected: str, actual: str) -> None:
    if actual != expected:
        raise AuthorizationUnavailable("authorization fact crosses tenant boundary")


def _require_future(expires_at: datetime | None, now: datetime, name: str) -> None:
    if expires_at is not None and now >= expires_at:
        raise AuthorizationUnavailable(f"{name} is stale")


def _validate_authentication_binding(
    binding: AuthenticationBinding,
    facts: AuthenticationFacts,
) -> None:
    if (
        type(binding) is BrowserAuthenticationBinding
        and type(facts) is not BrowserAuthenticationFacts
    ) or (
        type(binding) is CredentialAuthenticationBinding
        and type(facts) is not CredentialAuthenticationFacts
    ) or (
        type(binding) is DelegatedAuthenticationBinding
        and type(facts) is not DelegatedAuthenticationFacts
    ):
        raise AuthorizationUnavailable("authentication fact kind is inconsistent")
    for field_name in type(binding).model_fields:
        if getattr(binding, field_name) != getattr(facts, field_name):
            raise AuthorizationFactDenied(
                AuthorizationFactDenialReason.AUTHENTICATION_STALE
            )


def _validate_application(
    tenant_id: str,
    facts: ApplicationFacts,
    *,
    application_id: str,
    version: str,
    version_digest: str,
    now: datetime,
) -> None:
    _same_tenant(tenant_id, facts.tenant_id)
    if (
        facts.application_id != application_id
        or facts.version != version
        or facts.version_digest != version_digest
    ):
        raise AuthorizationUnavailable("application authority does not match binding")
    if (
        facts.application_status != "active"
        or facts.version_status != "published"
        or not facts.eligible
        or facts.published_at is None
        or facts.published_at > now
    ):
        raise AuthorizationFactDenied(
            AuthorizationFactDenialReason.APPLICATION_INACTIVE
        )
    if facts.expires_at is not None and now >= facts.expires_at:
        raise AuthorizationFactDenied(
            AuthorizationFactDenialReason.APPLICATION_INACTIVE
        )


def _validate_agent_release_chain(
    facts: AgentReleaseFacts, application: ApplicationFacts, now: datetime
) -> None:
    if not facts.chain_complete:
        raise AuthorizationUnavailable("agent release parent chain is incomplete")
    if application.mode is not ApplicationMode.RESTRICTED or application.break_glass:
        raise AuthorizationUnavailable("agent release application ceiling is invalid")
    expected_parent = facts.parent_release_id
    child_released_at = facts.released_at
    child_application: ApplicationFacts | ParentApplicationAuthorityFact = application
    seen = {facts.release_id}
    for parent in facts.parent_chain:
        _same_tenant(facts.tenant_id, parent.tenant_id)
        if expected_parent is None or parent.release_id != expected_parent:
            raise AuthorizationUnavailable("agent release parent chain is incomplete")
        if parent.release_id in seen:
            raise AuthorizationUnavailable(
                "agent release parent chain contains a cycle"
            )
        seen.add(parent.release_id)
        if parent.released_at > child_released_at or parent.released_at > now:
            raise AuthorizationUnavailable("agent release parent chain is inconsistent")
        if (
            parent.status != "active"
            or not parent.eligible
            or parent.revoked_at is not None
            or parent.agent_status != "active"
            or parent.application.application_status != "active"
            or parent.application.version_status != "published"
            or not parent.application.eligible
            or parent.application.mode is not ApplicationMode.RESTRICTED
            or parent.application.break_glass
            or parent.application.published_at is None
            or parent.application.published_at > now
            or (
                parent.application.expires_at is not None
                and now >= parent.application.expires_at
            )
        ):
            raise AuthorizationFactDenied(
                AuthorizationFactDenialReason.AGENT_RELEASE_INACTIVE
            )
        child_expiry = child_application.expires_at
        parent_expiry = parent.application.expires_at
        if (
            not set(child_application.allowed_resource_keys)
            <= parent.application.allowed_resource_keys
            or not child_application.allowed_operations
            <= parent.application.allowed_operations
            or (
                parent_expiry is not None
                and (child_expiry is None or child_expiry > parent_expiry)
            )
        ):
            raise AuthorizationUnavailable("child release expands parent authority")
        expected_parent = parent.parent_release_id
        child_released_at = parent.released_at
        child_application = parent.application
    if expected_parent is not None:
        raise AuthorizationUnavailable("agent release parent chain is incomplete")


def _resource_key(resource: ResourceReference) -> tuple[str, str, str]:
    return (
        resource.tenant_id,
        resource.resource_type.value,
        resource.resource_id,
    )


def _validate_resource_graph(
    query: AuthorizationFactQuery, graph: ResourceGraphFacts
) -> None:
    _same_tenant(query.tenant_id, graph.tenant_id)
    if not graph.closure_complete or graph.next_cursor is not None:
        raise AuthorizationUnavailable("resource graph closure is incomplete")
    if len(graph.ancestors) > MAX_RESOURCE_PARENT_DEPTH:
        raise AuthorizationUnavailable("resource parent graph is too deep")
    root = graph.root
    if (
        root.resource.tenant_id != query.tenant_id
        or root.resource.resource_id != query.target.resource_id
        or root.resource.resource_type is not query.target.resource_type
    ):
        raise AuthorizationUnavailable("resource graph root does not match target")
    if not root.active:
        raise AuthorizationFactDenied(AuthorizationFactDenialReason.RESOURCE_INACTIVE)

    parent_nodes = (root, *graph.ancestors)
    parent_keys = tuple(_resource_key(node.resource) for node in parent_nodes)
    if len(parent_keys) != len(set(parent_keys)):
        raise AuthorizationUnavailable("resource parent graph contains a cycle")
    for index, node in enumerate(parent_nodes):
        _same_tenant(query.tenant_id, node.resource.tenant_id)
        if not node.active:
            raise AuthorizationFactDenied(
                AuthorizationFactDenialReason.RESOURCE_INACTIVE
            )
        expected_parent = (
            graph.ancestors[index].resource if index < len(graph.ancestors) else None
        )
        if node.parent != expected_parent:
            raise AuthorizationUnavailable("resource parent graph is incomplete")

    known_revisions = {
        _resource_key(node.resource): node.resource.security_revision
        for node in parent_nodes
    }
    edges: set[tuple[tuple[str, str, str], tuple[str, str, str], Operation]] = set()
    adjacency: dict[tuple[str, str, str], set[tuple[str, str, str]]] = {}
    dependency_targets = {_resource_key(edge.target) for edge in graph.dependencies}
    root_key = _resource_key(root.resource)
    for edge in graph.dependencies:
        _same_tenant(query.tenant_id, edge.source.tenant_id)
        _same_tenant(query.tenant_id, edge.target.tenant_id)
        source_key = _resource_key(edge.source)
        target_key = _resource_key(edge.target)
        edge_key = (source_key, target_key, edge.required_operation)
        if edge_key in edges or source_key == target_key:
            raise AuthorizationUnavailable("resource dependency graph is malformed")
        edges.add(edge_key)
        if source_key != root_key and source_key not in dependency_targets:
            raise AuthorizationUnavailable("resource dependency source is incomplete")
        for resource in (edge.source, edge.target):
            key = _resource_key(resource)
            prior = known_revisions.get(key)
            if prior is not None and prior != resource.security_revision:
                raise AuthorizationUnavailable("resource revision is inconsistent")
            known_revisions[key] = resource.security_revision
        if not edge.target_active:
            raise AuthorizationFactDenied(
                AuthorizationFactDenialReason.RESOURCE_INACTIVE
            )
        adjacency.setdefault(source_key, set()).add(target_key)

    visited: set[tuple[str, str, str]] = set()

    def visit(
        key: tuple[str, str, str],
        path: frozenset[tuple[str, str, str]],
    ) -> int:
        if key in path:
            raise AuthorizationUnavailable("resource dependency graph contains a cycle")
        visited.add(key)
        next_path = path | {key}
        depth = 0
        for child in adjacency.get(key, set()):
            depth = max(depth, 1 + visit(child, next_path))
        return depth

    if visit(root_key, frozenset()) > MAX_RESOURCE_DEPENDENCY_DEPTH:
        raise AuthorizationUnavailable("resource dependency graph is too deep")
    if not dependency_targets <= visited:
        raise AuthorizationUnavailable("resource dependency closure is disconnected")


def _graph_targets(
    graph: ResourceGraphFacts, *, root_operation: Operation
) -> tuple[tuple[ResourceReference, Operation], ...]:
    return (
        (graph.root.resource, root_operation),
        *((edge.target, edge.required_operation) for edge in graph.dependencies),
    )


def _validate_scope_graph_authorities(
    facts: ScopeAuthorityFacts,
    graph: ResourceGraphFacts,
    *,
    root_operation: Operation,
    usable_scopes: frozenset[str],
) -> None:
    for resource, operation in _graph_targets(graph, root_operation=root_operation):
        if not any(
            authority.scope in usable_scopes
            and authority.resource_type is resource.resource_type
            and authority.operation is operation
            and (
                authority.resource_id is None
                or authority.resource_id == resource.resource_id
            )
            for authority in facts.authorities
        ):
            raise AuthorizationFactDenied(
                AuthorizationFactDenialReason.SCOPE_INSUFFICIENT
            )


def _validate_controls(
    facts: ControlFacts,
    graph: ResourceGraphFacts,
    *,
    root_operation: Operation,
    subject_kind: GrantSubjectKind,
    principal_id: str,
    now: datetime,
) -> None:
    targets = {
        (_resource_key(resource), operation)
        for resource, operation in _graph_targets(graph, root_operation=root_operation)
    }
    for clearance in facts.subject_clearances:
        if (
            clearance.subject_kind is not subject_kind
            or clearance.principal_id != principal_id
        ):
            raise AuthorizationUnavailable("control subject authority is inconsistent")
        _require_future(clearance.valid_until, now, "subject clearance")
    covered: set[tuple[tuple[str, str, str], Operation]] = set()
    for requirement in facts.resource_requirements:
        _same_tenant(facts.tenant_id, requirement.resource.tenant_id)
        _require_future(requirement.valid_until, now, "resource control requirement")
        target = (_resource_key(requirement.resource), requirement.operation)
        if target not in targets:
            raise AuthorizationUnavailable(
                "control requirement is outside resource graph"
            )
        covered.add(target)
    if not targets <= covered:
        # Every graph target must carry at least one control requirement before
        # a decision is attempted; an uncovered target would only fall through
        # to the intersection's CONTROL_DENIED bottom line, so failing closed
        # here never turns an allow into a deny.
        raise AuthorizationUnavailable(
            "control requirements do not cover the resource graph"
        )


def _validate_policies(
    facts: PolicyFacts,
    graph: ResourceGraphFacts,
    *,
    root_operation: Operation,
    now: datetime,
) -> None:
    targets = {
        (_resource_key(resource), operation)
        for resource, operation in _graph_targets(graph, root_operation=root_operation)
    }
    for rule in facts.rules:
        _same_tenant(facts.tenant_id, rule.resource.tenant_id)
        target = (_resource_key(rule.resource), rule.operation)
        if target not in targets:
            raise AuthorizationUnavailable("policy binding is outside resource graph")
        if now < rule.published_at:
            raise AuthorizationUnavailable("policy binding is inactive")
        try:
            validate_policy_expression(rule.condition)
            canonical = canonicalize_policy_expression(rule.condition)
            if (
                canonical.utf8 != rule.canonical_ast_utf8
                or canonical.sha256 != rule.canonical_ast_sha256
            ):
                raise ValueError
        except Exception:
            raise AuthorizationUnavailable("policy authority is invalid") from None
    # Deliberately no coverage requirement here: the absence of a policy
    # binding for a target is a designed state (for example after an emergency
    # policy revocation) that the intersection must resolve as an audited
    # POLICY_DEFAULT_DENY decision, not as authorization unavailability.


def _entry(category: str, key: str, revision: int, digest: str) -> RevisionVectorEntry:
    return RevisionVectorEntry(
        category=category, key=key, revision=revision, digest=digest
    )


def _build_revision_vector(
    *,
    subject: SubjectFacts,
    membership: MembershipFacts,
    actor: ActorFacts,
    authentication: AuthenticationFacts,
    caller_application: ApplicationFacts,
    agent: AgentFacts | None,
    agent_release: AgentReleaseFacts | None,
    agent_application: ApplicationFacts | None,
    subject_authority: SubjectAuthorityFacts,
    resource_graph: ResourceGraphFacts,
    grants: GrantFacts,
    scope_authority: ScopeAuthorityFacts,
    controls: ControlFacts,
    policies: PolicyFacts,
    revision_source: RevisionSourceFacts,
) -> RevisionVector:
    entries = [
        _entry(
            "schema",
            "authorization",
            revision_source.schema_revision,
            revision_source.schema_digest,
        ),
        _entry(
            "subject", subject.subject_id, subject.revision, subject.snapshot_digest
        ),
        _entry(
            "membership",
            membership.principal_id,
            membership.revision,
            membership.snapshot_digest,
        ),
        _entry("actor", actor.actor_id, actor.revision, actor.snapshot_digest),
        _authentication_revision_entry(authentication),
        _entry(
            "caller_application",
            f"{caller_application.application_id}:{caller_application.version}",
            caller_application.version_revision,
            caller_application.snapshot_digest,
        ),
        _entry(
            "subject_authority",
            subject_authority.principal_id,
            subject_authority.revision,
            subject_authority.snapshot_digest,
        ),
        _entry(
            "resource_registry",
            resource_graph.tenant_id,
            resource_graph.registry_revision,
            resource_graph.registry_digest,
        ),
        _entry(
            "resource_root",
            resource_graph.root.resource.resource_id,
            resource_graph.root.resource.security_revision,
            _canonical_digest(resource_graph.root),
        ),
        _entry(
            "grant_set", grants.principal_id, grants.revision, grants.snapshot_digest
        ),
        _entry(
            "scope_catalog",
            f"{scope_authority.catalog_id}:{scope_authority.catalog_version}",
            scope_authority.catalog_revision,
            scope_authority.catalog_digest,
        ),
        _entry(
            "scope_authority",
            scope_authority.principal_id,
            scope_authority.revision,
            scope_authority.snapshot_digest,
        ),
        _entry(
            "control_set",
            controls.tenant_id,
            controls.revision,
            controls.snapshot_digest,
        ),
        _entry(
            "policy_set",
            policies.tenant_id,
            policies.revision,
            policies.snapshot_digest,
        ),
        _entry(
            "event_high_water",
            revision_source.tenant_id,
            revision_source.event_high_water,
            _canonical_digest(revision_source.event_high_water),
        ),
        _entry(
            "revision_source",
            revision_source.tenant_id,
            revision_source.revision,
            revision_source.snapshot_digest,
        ),
    ]
    entries.extend(
        _entry("db_source", source.source_name, source.revision, source.digest)
        for source in revision_source.database_sources
    )
    if (
        agent is not None
        and agent_release is not None
        and agent_application is not None
    ):
        entries.extend(
            (
                _entry("agent", agent.agent_id, agent.revision, agent.snapshot_digest),
                _entry(
                    "agent_release",
                    agent_release.release_id,
                    agent_release.revision,
                    agent_release.snapshot_digest,
                ),
                _entry(
                    "agent_application",
                    f"{agent_application.application_id}:{agent_application.version}",
                    agent_application.version_revision,
                    agent_application.snapshot_digest,
                ),
            )
        )
        entries.extend(
            _entry(
                "agent_release_parent",
                parent.release_id,
                parent.revision,
                _canonical_digest(parent),
            )
            for parent in agent_release.parent_chain
        )
    for category, authorities in (
        ("group", subject_authority.groups),
        ("role", subject_authority.roles),
        ("attribute", subject_authority.attribute_revisions),
        ("clearance", subject_authority.clearances),
    ):
        entries.extend(
            _entry(category, item.authority_id, item.revision, item.digest)
            for item in authorities
        )
    entries.extend(
        _entry(
            "control",
            f"clearance:{clearance.clearance_id}",
            clearance.revision,
            clearance.digest,
        )
        for clearance in controls.subject_clearances
    )
    entries.extend(
        _entry(
            "control",
            f"requirement:{requirement.requirement_id}",
            requirement.revision,
            requirement.digest,
        )
        for requirement in controls.resource_requirements
    )
    entries.extend(
        _entry(
            "policy",
            rule.binding_id,
            rule.activation_head_revision,
            rule.binding_digest,
        )
        for rule in policies.rules
    )
    policy_plan_authorities: dict[str, tuple[int, str]] = {}
    for rule in policies.rules:
        authority = (rule.active_version, rule.version_digest)
        existing = policy_plan_authorities.setdefault(rule.policy_set_id, authority)
        if existing != authority:
            raise AuthorizationUnavailable(
                "policy plan authority is inconsistent across bindings"
            )
    entries.extend(
        _entry("policy_version", policy_id, revision, digest)
        for policy_id, (revision, digest) in sorted(policy_plan_authorities.items())
    )
    entries.extend(
        _entry("grant", grant.grant_id, grant.revision, grant.digest)
        for grant in grants.grants
    )
    entries.extend(
        _entry(
            "resource_ancestor",
            node.resource.resource_id,
            node.resource.security_revision,
            _canonical_digest(node),
        )
        for node in resource_graph.ancestors
    )
    entries.extend(
        _entry(
            "resource_dependency",
            f"{edge.source.resource_id}>{edge.target.resource_id}:{edge.required_operation.value}",
            edge.target.security_revision,
            _canonical_digest(edge),
        )
        for edge in resource_graph.dependencies
    )
    return RevisionVector(entries=tuple(entries))


def _authentication_revision_entry(
    authentication: AuthenticationFacts,
) -> RevisionVectorEntry:
    if type(authentication) is BrowserAuthenticationFacts:
        return _entry(
            "session",
            authentication.session_id,
            authentication.session_revision,
            # Idle renewal is telemetry at the same session revision (0093).
            # Keep the full fact seal and effective expiry for freshness/permit
            # bounds, but bind replay authority to the absolute deadline and
            # every identity, scope, status, revision and credential epoch.
            _canonical_digest(
                authentication.model_dump(
                    mode="python",
                    exclude={"repository_witness", "snapshot_digest", "expires_at"},
                )
            ),
        )
    if type(authentication) is CredentialAuthenticationFacts:
        return _entry(
            "credential",
            authentication.credential_id,
            authentication.credential_revision,
            authentication.snapshot_digest,
        )
    if type(authentication) is DelegatedAuthenticationFacts:
        return _entry(
            "delegation",
            authentication.root_delegation_id,
            max(
                authentication.tenant_epoch,
                authentication.subject_epoch,
                authentication.agent_epoch,
                authentication.session_epoch,
            ),
            authentication.snapshot_digest,
        )
    raise AuthorizationUnavailable("authentication fact kind is inconsistent")


def _earliest_expiry(*values: datetime | None) -> datetime | None:
    present = tuple(value for value in values if value is not None)
    return min(present) if present else None


__all__ = [
    "AuthorizationFactsProvider",
    "AuthorizationFactsResolver",
    "AuthorizationFactsUnitOfWork",
]
