from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import gc
import importlib
import pickle
from typing import Iterator, Never
from weakref import ref, WeakKeyDictionary

import pytest
from pydantic import BaseModel, ValidationError

from eios.authz.applications import (
    ApplicationMode,
    OperationRestriction,
    ResourceRestriction,
)
from eios.authz.facts import (
    ActorFacts,
    AgentFacts,
    AgentInvocationBinding,
    AgentReleaseFacts,
    ApplicationFacts,
    AuthorizationFactDenied,
    AuthorizationFactDenialReason,
    AuthorizationFactQuery,
    AuthorizationFactsResolver,
    AuthorizationRisk,
    AuthorizationTarget,
    BrowserAuthenticationBinding,
    BrowserAuthenticationFacts,
    ControlDominance,
    ControlFacts,
    ControlKind,
    CredentialAuthenticationBinding,
    CredentialAuthenticationFacts,
    DelegatedAuthenticationBinding,
    DelegatedAuthenticationFacts,
    EffectiveGrantFact,
    GrantFacts,
    MembershipFacts,
    ParentAgentReleaseFact,
    ParentApplicationAuthorityFact,
    PolicyEffect,
    PolicyFacts,
    PolicyRuleFact,
    ReadOnlyAuthorityView,
    ResourceControlRequirementFact,
    ResourceDependencyFact,
    ResourceGraphFacts,
    ResourceNodeFact,
    RevisionSourceFacts,
    RevisionedAuthority,
    ResolvedAuthorizationContext,
    ScopeAuthority,
    ScopeAuthorityFacts,
    SubjectClearanceFact,
    SubjectAuthorityFacts,
    SubjectFacts,
)
from eios.authz.errors import AuthorizationUnavailable
from eios.authz.grants import GrantSubjectKind
from eios.authz.intersection import DecisionReason, evaluate_intersection
from eios.authz.operations import Operation
from eios.authz.policy import Eq, LiteralValue, canonicalize_policy_expression
from eios.authz.resources import ResourceReference, ResourceType
from eios.identity.models import FrozenJsonMap, MembershipKind, SubjectKind


NOW = datetime(2026, 7, 21, 2, 0, tzinfo=UTC)
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def browser_binding(**changes: object) -> BrowserAuthenticationBinding:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "session_id": "session-a",
        "credential_kind": "external_identity",
        "credential_tenant_id": "tenant-home",
        "credential_id": "credential-a",
        "subject_id": "subject-a",
        "subject_kind": SubjectKind.HUMAN,
        "subject_principal_id": "principal-a",
        "subject_revision": 4,
        "membership_revision": 7,
        "session_revision": 9,
        "credential_revision": 11,
        "credential_epoch": 3,
        "caller_application_id": "eios:application:portal",
        "caller_application_version": "v2",
        "caller_application_digest": DIGEST_A,
        "requested_scopes": frozenset({"ontology.read"}),
    }
    values.update(changes)
    return BrowserAuthenticationBinding(**values)


def credential_binding(**changes: object) -> CredentialAuthenticationBinding:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "credential_kind": "api_key",
        "credential_tenant_id": "tenant-a",
        "credential_id": "api-key-a",
        "subject_id": "service-a",
        "subject_kind": SubjectKind.SERVICE,
        "subject_principal_id": "service-principal-a",
        "subject_revision": 4,
        "membership_revision": 7,
        "credential_revision": 11,
        "credential_epoch": 3,
        "caller_application_id": "eios:application:integration",
        "caller_application_version": "v4",
        "caller_application_digest": DIGEST_A,
        "requested_scopes": frozenset({"dataset.read"}),
    }
    values.update(changes)
    return CredentialAuthenticationBinding(**values)


def delegated_binding(**changes: object) -> DelegatedAuthenticationBinding:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "credential_kind": "delegation",
        "credential_tenant_id": "tenant-home",
        "credential_id": "credential-a",
        "subject_id": "subject-a",
        "subject_kind": SubjectKind.HUMAN,
        "subject_principal_id": "principal-a",
        "subject_revision": 4,
        "membership_revision": 7,
        "credential_revision": 11,
        "credential_epoch": 3,
        "caller_application_id": "eios:application:orchestrator",
        "caller_application_version": "v4",
        "caller_application_digest": DIGEST_A,
        "requested_scopes": frozenset({"orders.refund.approve"}),
        "root_delegation_id": "delegation-root-a",
        "source_session_id": "session-source-a",
        "tenant_epoch": 2,
        "subject_epoch": 3,
        "agent_epoch": 4,
        "session_epoch": 5,
        "delegation_expires_at": NOW + timedelta(hours=2),
        "delegation_ceiling_digest": DIGEST_B,
    }
    values.update(changes)
    return DelegatedAuthenticationBinding(**values)


def target() -> AuthorizationTarget:
    return AuthorizationTarget(
        tenant_id="tenant-a",
        resource_type=ResourceType.DATASET,
        resource_id="eios:dataset:orders",
        operation=Operation.READ,
    )


def scope_authorities(
    scope: str = "ontology.read", *, dependency_scope: str | None = None
) -> tuple[ScopeAuthority, ...]:
    return (
        ScopeAuthority(
            scope=scope,
            resource_type=ResourceType.DATASET,
            operation=Operation.READ,
            resource_id="eios:dataset:orders",
        ),
        ScopeAuthority(
            scope=dependency_scope or scope,
            resource_type=ResourceType.CONNECTOR,
            operation=Operation.READ,
            resource_id="eios:connector:crm",
        ),
    )


def test_authentication_bindings_are_strict_frozen_and_forbid_extra() -> None:
    binding = browser_binding()

    with pytest.raises(ValidationError):
        BrowserAuthenticationBinding.model_validate(
            {**binding.model_dump(), "session_revision": True}, strict=True
        )
    with pytest.raises(ValidationError):
        BrowserAuthenticationBinding.model_validate(
            {**binding.model_dump(), "groups": frozenset({"admins"})}, strict=True
        )
    with pytest.raises(ValidationError):
        binding.session_revision = 10  # type: ignore[misc]


def test_browser_binding_is_human_only_and_credential_binding_rejects_system() -> None:
    with pytest.raises(ValidationError):
        browser_binding(subject_kind=SubjectKind.SERVICE)
    with pytest.raises(ValidationError):
        credential_binding(subject_kind=SubjectKind.SYSTEM)


def test_delegated_binding_carries_exact_root_epochs_and_ceiling() -> None:
    binding = delegated_binding()

    assert binding.credential_kind == "delegation"
    assert binding.root_delegation_id == "delegation-root-a"
    assert binding.source_session_id == "session-source-a"
    assert (
        binding.tenant_epoch,
        binding.subject_epoch,
        binding.agent_epoch,
        binding.session_epoch,
    ) == (2, 3, 4, 5)
    assert binding.delegation_expires_at == NOW + timedelta(hours=2)
    assert binding.delegation_ceiling_digest == DIGEST_B


def test_delegated_binding_rejects_naive_expiry_and_non_human_subject() -> None:
    with pytest.raises(ValidationError):
        delegated_binding(delegation_expires_at=datetime(2026, 7, 21, 4, 0))
    with pytest.raises(ValidationError):
        delegated_binding(subject_kind=SubjectKind.SERVICE)


def test_guest_session_keeps_exact_credential_tenant_and_kind() -> None:
    binding = browser_binding()

    assert binding.tenant_id == "tenant-a"
    assert binding.credential_tenant_id == "tenant-home"
    assert binding.credential_kind == "external_identity"


def test_api_key_requires_an_exact_caller_application_binding() -> None:
    with pytest.raises(ValidationError):
        credential_binding(caller_application_version="")
    with pytest.raises(ValidationError):
        credential_binding(caller_application_digest="latest")


def test_query_separates_caller_application_from_agent_application() -> None:
    agent = AgentInvocationBinding(
        tenant_id="tenant-a",
        actor_principal_id="agent-principal-a",
        agent_id="eios:agent:planner",
        agent_revision=8,
        release_id="planner-r3",
        release_revision=5,
        release_digest=DIGEST_B,
        agent_application_id="eios:application:planner-runtime",
        agent_application_version="v3",
        agent_application_digest="c" * 64,
    )
    query = AuthorizationFactQuery(
        tenant_id="tenant-a",
        authentication=browser_binding(),
        agent_invocation=agent,
        target=target(),
        request_id="request-a",
        trace_id="trace-a",
    )

    assert query.authentication.caller_application_id == "eios:application:portal"
    assert query.agent_invocation is not None
    assert (
        query.agent_invocation.agent_application_id
        == "eios:application:planner-runtime"
    )


def test_query_abi_has_no_caller_supplied_authority_fields() -> None:
    assert {
        "groups",
        "roles",
        "attributes",
        "clearances",
        "application_revision",
    }.isdisjoint(AuthorizationFactQuery.model_fields)

    with pytest.raises(ValidationError):
        AuthorizationFactQuery(
            tenant_id="tenant-a",
            authentication=browser_binding(),
            agent_invocation=None,
            target=target(),
            request_id="request-a",
            trace_id="trace-a",
            groups=frozenset({"admins"}),  # type: ignore[call-arg]
        )


def test_fact_revisions_and_attribute_payloads_are_database_bounded() -> None:
    with pytest.raises(ValidationError):
        RevisionedAuthority(
            authority_id="role-reader",
            revision=2**63,
            digest=DIGEST_A,
        )
    with pytest.raises(ValidationError):
        SubjectAuthorityFacts(
            tenant_id="tenant-a",
            subject_kind=GrantSubjectKind.HUMAN,
            principal_id="principal-a",
            groups=(),
            roles=(),
            attributes=FrozenJsonMap({"department": "x" * 17_000}),
            attribute_revisions=(),
            clearances=(),
            revision=1,
            valid_until=None,
            **repository_fields(),
        )
    with pytest.raises(ValidationError):
        SubjectAuthorityFacts(
            tenant_id="tenant-a",
            subject_kind=GrantSubjectKind.HUMAN,
            principal_id="principal-a",
            groups=(),
            roles=(),
            attributes=FrozenJsonMap({"department": "finance"}),
            attribute_revisions=(),
            clearances=(),
            revision=1,
            valid_until=None,
            **repository_fields(),
        )


def test_query_rejects_cross_tenant_nested_bindings() -> None:
    with pytest.raises(ValidationError):
        AuthorizationFactQuery(
            tenant_id="tenant-a",
            authentication=browser_binding(tenant_id="tenant-b"),
            agent_invocation=None,
            target=target(),
            request_id="request-a",
            trace_id="trace-a",
        )


def test_scope_catalog_required_scopes_are_and_semantics_and_bounded() -> None:
    facts = ScopeAuthorityFacts(
        tenant_id="tenant-a",
        principal_id="principal-a",
        binding_id="route-dataset-read",
        catalog_id="scope-catalog",
        catalog_version="v1",
        catalog_revision=1,
        catalog_digest=DIGEST_A,
        catalog_scopes=frozenset({"dataset.read", "metadata.read"}),
        required_scopes=frozenset({"dataset.read", "metadata.read"}),
        authorities=(
            ScopeAuthority(
                scope="dataset.read",
                resource_type=ResourceType.DATASET,
                operation=Operation.READ,
                resource_id=None,
            ),
        ),
        risk=AuthorizationRisk.SENSITIVE,
        authorized_scopes=frozenset({"dataset.read", "metadata.read"}),
        revision=1,
        valid_until=None,
        **repository_fields(),
    )

    assert facts.required_scopes == frozenset({"dataset.read", "metadata.read"})
    with pytest.raises(ValidationError):
        ScopeAuthorityFacts(
            tenant_id="tenant-a",
            principal_id="principal-a",
            binding_id="route-dataset-read",
            catalog_id="scope-catalog",
            catalog_version="v1",
            catalog_revision=1,
            catalog_digest=DIGEST_A,
            catalog_scopes=frozenset(f"scope.{index}" for index in range(65)),
            required_scopes=frozenset(f"scope.{index}" for index in range(65)),
            authorities=(
                ScopeAuthority(
                    scope="scope.0",
                    resource_type=ResourceType.DATASET,
                    operation=Operation.READ,
                    resource_id=None,
                ),
            ),
            risk=AuthorizationRisk.SENSITIVE,
            authorized_scopes=frozenset(f"scope.{index}" for index in range(65)),
            revision=1,
            valid_until=None,
            **repository_fields(),
        )


def test_scope_authorities_are_exact_canonical_and_do_not_infer_suffixes() -> None:
    authority = ScopeAuthority(
        scope="dataset.read",
        resource_type=ResourceType.DATASET,
        operation=Operation.READ,
        resource_id=None,
    )
    assert authority.resource_id is None

    with pytest.raises(ValidationError):
        ScopeAuthority(
            scope="dataset.read",
            resource_type=ResourceType.DATASET,
            operation=Operation.READ,
            resource_id="orders",
        )
    with pytest.raises(ValidationError):
        ScopeAuthority(
            scope="dataset.read",
            resource_type=ResourceType.DATASET,
            operation=Operation.READ,
            resource_id="eios:connector:orders",
        )


def test_scope_catalog_rejects_duplicate_authorities() -> None:
    authority = ScopeAuthority(
        scope="dataset.read",
        resource_type=ResourceType.DATASET,
        operation=Operation.READ,
        resource_id=None,
    )
    with pytest.raises(ValidationError):
        ScopeAuthorityFacts(
            tenant_id="tenant-a",
            principal_id="principal-a",
            binding_id="route-dataset-read",
            catalog_id="scope-catalog",
            catalog_version="v1",
            catalog_revision=1,
            catalog_digest=DIGEST_A,
            catalog_scopes=frozenset({"dataset.read"}),
            required_scopes=frozenset({"dataset.read"}),
            authorities=(authority, authority),
            risk=AuthorizationRisk.READ_ONLY,
            authorized_scopes=frozenset({"dataset.read"}),
            revision=1,
            valid_until=None,
            **repository_fields(),
        )


def reference(
    resource_type: ResourceType,
    name: str,
    revision: int,
) -> ResourceReference:
    return ResourceReference(
        tenant_id="tenant-a",
        resource_id=f"eios:{resource_type.value}:{name}",
        resource_type=resource_type,
        security_revision=revision,
    )


def repository_fields() -> dict[str, object]:
    return {"repository_witness": "repository-witness"}


class RecordingUnitOfWork:
    repeatable_read = True
    read_only = True

    def __init__(self, *, query: AuthorizationFactQuery) -> None:
        self.query = query
        self.calls: list[str] = []
        self.now = NOW
        self.subject = SubjectFacts(
            tenant_id="tenant-a",
            subject_id=query.authentication.subject_id,
            kind=query.authentication.subject_kind,
            status="active",
            revision=query.authentication.subject_revision,
            **repository_fields(),
        )
        self.membership = MembershipFacts(
            tenant_id="tenant-a",
            subject_id=query.authentication.subject_id,
            principal_id=query.authentication.subject_principal_id,
            kind=MembershipKind.HOME,
            status="active",
            valid_from=NOW - timedelta(days=10),
            valid_until=NOW + timedelta(hours=4),
            revision=query.authentication.membership_revision,
            **repository_fields(),
        )
        actor_kind = (
            SubjectKind.AGENT
            if query.agent_invocation is not None
            else query.authentication.subject_kind
        )
        actor_id = (
            query.agent_invocation.agent_id
            if query.agent_invocation is not None
            else query.authentication.subject_id
        )
        actor_principal = (
            query.agent_invocation.actor_principal_id
            if query.agent_invocation is not None
            else query.authentication.subject_principal_id
        )
        actor_revision = (
            query.agent_invocation.agent_revision
            if query.agent_invocation is not None
            else query.authentication.subject_revision
        )
        self.actor = ActorFacts(
            tenant_id="tenant-a",
            actor_id=actor_id,
            actor_principal_id=actor_principal,
            kind=actor_kind,
            status="active",
            revision=actor_revision,
            **repository_fields(),
        )
        auth_values = {
            **query.authentication.model_dump(),
            "status": "active",
            "expires_at": NOW + timedelta(hours=2),
            **repository_fields(),
        }
        if type(query.authentication) is BrowserAuthenticationBinding:
            auth_values["absolute_expires_at"] = NOW + timedelta(days=1)
            self.authentication = BrowserAuthenticationFacts(**auth_values)
        elif type(query.authentication) is DelegatedAuthenticationBinding:
            auth_values["expires_at"] = query.authentication.delegation_expires_at
            self.authentication = DelegatedAuthenticationFacts(**auth_values)
        else:
            self.authentication = CredentialAuthenticationFacts(**auth_values)
        self.caller_application = ApplicationFacts(
            tenant_id="tenant-a",
            application_id=query.authentication.caller_application_id,
            application_revision=5,
            version=query.authentication.caller_application_version,
            version_revision=4,
            version_digest=query.authentication.caller_application_digest,
            record_digest="d" * 64,
            application_status="active",
            version_status="published",
            eligible=True,
            mode=ApplicationMode.RESTRICTED,
            break_glass=False,
            resources=(
                ResourceRestriction(
                    tenant_id="tenant-a",
                    resource_type=ResourceType.DATASET,
                    resource_id="eios:dataset:orders",
                ),
                ResourceRestriction(
                    tenant_id="tenant-a",
                    resource_type=ResourceType.CONNECTOR,
                    resource_id="eios:connector:crm",
                ),
            ),
            operations=(OperationRestriction(operation=Operation.READ),),
            published_at=NOW - timedelta(days=1),
            revoked_at=None,
            expires_at=NOW + timedelta(hours=3),
            **repository_fields(),
        )
        invocation = query.agent_invocation
        self.agent = (
            None
            if invocation is None
            else AgentFacts(
                tenant_id="tenant-a",
                agent_id=invocation.agent_id,
                actor_principal_id=invocation.actor_principal_id,
                application_id=invocation.agent_application_id,
                status="active",
                revision=invocation.agent_revision,
                **repository_fields(),
            )
        )
        self.agent_release = (
            None
            if invocation is None
            else AgentReleaseFacts(
                tenant_id="tenant-a",
                release_id=invocation.release_id,
                agent_id=invocation.agent_id,
                application_id=invocation.agent_application_id,
                application_version=invocation.agent_application_version,
                application_version_digest=invocation.agent_application_digest,
                release_digest=invocation.release_digest,
                status="active",
                revision=invocation.release_revision,
                eligible=True,
                released_at=NOW - timedelta(days=1),
                revoked_at=None,
                parent_release_id=None,
                parent_chain=(),
                chain_complete=True,
                **repository_fields(),
            )
        )
        self.agent_application = (
            None
            if invocation is None
            else ApplicationFacts(
                tenant_id="tenant-a",
                application_id=invocation.agent_application_id,
                application_revision=3,
                version=invocation.agent_application_version,
                version_revision=6,
                version_digest=invocation.agent_application_digest,
                record_digest="e" * 64,
                application_status="active",
                version_status="published",
                eligible=True,
                mode=ApplicationMode.RESTRICTED,
                break_glass=False,
                resources=(
                    ResourceRestriction(
                        tenant_id="tenant-a",
                        resource_type=ResourceType.DATASET,
                        resource_id="eios:dataset:orders",
                    ),
                    ResourceRestriction(
                        tenant_id="tenant-a",
                        resource_type=ResourceType.CONNECTOR,
                        resource_id="eios:connector:crm",
                    ),
                ),
                operations=(OperationRestriction(operation=Operation.READ),),
                published_at=NOW - timedelta(days=1),
                revoked_at=None,
                expires_at=NOW + timedelta(hours=1),
                **repository_fields(),
            )
        )
        self.subject_authority = SubjectAuthorityFacts(
            tenant_id="tenant-a",
            subject_kind=GrantSubjectKind(query.authentication.subject_kind.value),
            principal_id=query.authentication.subject_principal_id,
            groups=(
                RevisionedAuthority(
                    authority_id="group-analysts", revision=3, digest="1" * 64
                ),
            ),
            roles=(
                RevisionedAuthority(
                    authority_id="role-reader", revision=5, digest="2" * 64
                ),
            ),
            attributes=FrozenJsonMap({"department": "finance"}),
            attribute_revisions=(
                RevisionedAuthority(
                    authority_id="department", revision=7, digest="3" * 64
                ),
            ),
            clearances=(
                RevisionedAuthority(
                    authority_id="internal", revision=2, digest="4" * 64
                ),
            ),
            revision=9,
            valid_until=NOW + timedelta(minutes=50),
            **repository_fields(),
        )
        root = ResourceNodeFact(
            resource=reference(ResourceType.DATASET, "orders", 10),
            parent=reference(ResourceType.PROJECT, "main", 2),
            active=True,
        )
        ancestor = ResourceNodeFact(
            resource=reference(ResourceType.PROJECT, "main", 2),
            parent=None,
            active=True,
        )
        self.resource_graph = ResourceGraphFacts(
            tenant_id="tenant-a",
            root=root,
            ancestors=(ancestor,),
            dependencies=(
                ResourceDependencyFact(
                    source=root.resource,
                    target=reference(ResourceType.CONNECTOR, "crm", 4),
                    target_active=True,
                    required_operation=Operation.READ,
                ),
            ),
            registry_revision=12,
            registry_digest="5" * 64,
            closure_complete=True,
            next_cursor=None,
            **repository_fields(),
        )
        self.grants = GrantFacts(
            tenant_id="tenant-a",
            subject_kind=self.subject_authority.subject_kind,
            principal_id=query.authentication.subject_principal_id,
            grants=(
                EffectiveGrantFact(
                    grant_id="grant-reader",
                    subject_kind=self.subject_authority.subject_kind,
                    subject_id=query.authentication.subject_principal_id,
                    role_id="role-reader",
                    role_revision=5,
                    role_digest="2" * 64,
                    resource=reference(ResourceType.DATASET, "orders", 10),
                    operations=frozenset({Operation.READ}),
                    inherited=False,
                    valid_from=NOW - timedelta(days=1),
                    valid_until=NOW + timedelta(minutes=45),
                    revision=8,
                    digest="6" * 64,
                ),
                EffectiveGrantFact(
                    grant_id="grant-group-reader",
                    subject_kind=GrantSubjectKind.GROUP,
                    subject_id="group-analysts",
                    role_id="role-reader",
                    role_revision=5,
                    role_digest="2" * 64,
                    resource=reference(ResourceType.DATASET, "orders", 10),
                    operations=frozenset({Operation.READ}),
                    inherited=False,
                    valid_from=NOW - timedelta(days=1),
                    valid_until=NOW + timedelta(minutes=45),
                    revision=9,
                    digest="a" * 64,
                ),
            ),
            valid_until=NOW + timedelta(minutes=45),
            complete=True,
            next_cursor=None,
            revision=8,
            **repository_fields(),
        )
        requested_scopes = frozenset(query.authentication.requested_scopes)
        required_scope = (
            "ontology.read"
            if "ontology.read" in requested_scopes
            else min(requested_scopes)
        )
        dependency_scope = (
            "metadata.read" if "metadata.read" in requested_scopes else required_scope
        )
        self.scope_authority = ScopeAuthorityFacts(
            tenant_id="tenant-a",
            principal_id=query.authentication.subject_principal_id,
            binding_id="route-dataset-read",
            catalog_id="scope-catalog",
            catalog_version="v6",
            catalog_revision=6,
            catalog_digest=DIGEST_B,
            catalog_scopes=requested_scopes,
            required_scopes=frozenset({required_scope}),
            authorities=scope_authorities(
                required_scope, dependency_scope=dependency_scope
            ),
            risk=AuthorizationRisk.READ_ONLY,
            authorized_scopes=requested_scopes,
            revision=4,
            valid_until=NOW + timedelta(minutes=40),
            **repository_fields(),
        )
        self.controls = ControlFacts(
            tenant_id="tenant-a",
            subject_clearances=(
                SubjectClearanceFact(
                    clearance_id="clearance-public",
                    subject_kind=self.subject_authority.subject_kind,
                    principal_id=query.authentication.subject_principal_id,
                    kind=ControlKind.CLASSIFICATION,
                    namespace="classification",
                    value="public",
                    rank=0,
                    dominates=frozenset(),
                    revision=2,
                    digest="7" * 64,
                    valid_until=NOW + timedelta(minutes=35),
                ),
            ),
            resource_requirements=tuple(
                ResourceControlRequirementFact(
                    requirement_id=f"classification-{resource.resource_id}",
                    resource=resource,
                    operation=operation,
                    kind=ControlKind.CLASSIFICATION,
                    namespace="classification",
                    value="public",
                    minimum_rank=0,
                    dominance=ControlDominance.EXACT,
                    revision=2,
                    digest=digest,
                    valid_until=NOW + timedelta(minutes=35),
                )
                for resource, operation, digest in (
                    (root.resource, Operation.READ, "b" * 64),
                    (
                        reference(ResourceType.CONNECTOR, "crm", 4),
                        Operation.READ,
                        "c" * 64,
                    ),
                )
            ),
            complete=True,
            valid_until=NOW + timedelta(minutes=35),
            revision=2,
            **repository_fields(),
        )
        always = Eq(
            node_id="always",
            op="eq",
            left=LiteralValue(kind="literal", value=True),
            right=LiteralValue(kind="literal", value=True),
        )
        canonical_always = canonicalize_policy_expression(always)
        self.policies = PolicyFacts(
            tenant_id="tenant-a",
            rules=tuple(
                PolicyRuleFact(
                    binding_id=f"binding-{resource.resource_id}",
                    resource=resource,
                    operation=operation,
                    effect=PolicyEffect.REQUIRE,
                    binding_digest="f" * 64,
                    policy_set_id=f"policy-{resource.resource_id}",
                    active_version=6,
                    version_digest=digest,
                    canonical_ast_utf8=canonical_always.utf8,
                    canonical_ast_sha256=canonical_always.sha256,
                    condition=always,
                    activation_head_revision=6,
                    activation_head_digest="d" * 64,
                    registry_revision=6,
                    registry_digest="9" * 64,
                    activation_witness="a" * 64,
                    published_at=NOW - timedelta(days=1),
                )
                for resource, operation, digest in (
                    (root.resource, Operation.READ, "8" * 64),
                    (
                        reference(ResourceType.CONNECTOR, "crm", 4),
                        Operation.READ,
                        "e" * 64,
                    ),
                )
            ),
            complete=True,
            valid_until=NOW + timedelta(minutes=30),
            revision=6,
            **repository_fields(),
        )
        self.revision_source = RevisionSourceFacts(
            tenant_id="tenant-a",
            schema_revision=40,
            schema_digest="9" * 64,
            event_high_water=101,
            revision=11,
            **repository_fields(),
        )

    def trusted_now(self) -> datetime:
        self.calls.append("trusted_now")
        return self.now

    def verify_repository_witness(
        self, *, fact_kind: str, snapshot_digest: str, repository_witness: str
    ) -> bool:
        del fact_kind, snapshot_digest
        return repository_witness == "repository-witness"

    def load_subject(self, *args: object) -> SubjectFacts | None:
        self.calls.append("subject")
        return self.subject

    def load_membership(self, *args: object) -> MembershipFacts | None:
        self.calls.append("membership")
        return self.membership

    def load_actor(self, *args: object) -> ActorFacts | None:
        self.calls.append("actor")
        return self.actor

    def load_browser_authentication(
        self, *args: object
    ) -> BrowserAuthenticationFacts | None:
        self.calls.append("browser_authentication")
        assert type(self.authentication) is BrowserAuthenticationFacts
        return self.authentication

    def load_credential_authentication(
        self, *args: object
    ) -> CredentialAuthenticationFacts | None:
        self.calls.append("credential_authentication")
        assert type(self.authentication) is CredentialAuthenticationFacts
        return self.authentication

    def load_delegated_authentication(
        self, *args: object
    ) -> DelegatedAuthenticationFacts | None:
        self.calls.append("delegated_authentication")
        assert type(self.authentication) is DelegatedAuthenticationFacts
        return self.authentication

    def load_caller_application(self, *args: object) -> ApplicationFacts | None:
        self.calls.append("caller_application")
        return self.caller_application

    def load_agent(self, *args: object) -> AgentFacts | None:
        self.calls.append("agent")
        return self.agent

    def load_agent_release(self, *args: object) -> AgentReleaseFacts | None:
        self.calls.append("agent_release")
        return self.agent_release

    def load_agent_application(self, *args: object) -> ApplicationFacts | None:
        self.calls.append("agent_application")
        return self.agent_application

    def load_subject_authority(self, *args: object) -> SubjectAuthorityFacts | None:
        self.calls.append("subject_authority")
        assert args[1] is self.subject_authority.subject_kind
        return self.subject_authority

    def load_resource_graph(self, *args: object) -> ResourceGraphFacts | None:
        self.calls.append("resource_graph")
        return self.resource_graph

    def load_grants(self, *args: object) -> GrantFacts | None:
        self.calls.append("grants")
        return self.grants

    def load_scope_authority(
        self, query: AuthorizationFactQuery
    ) -> ScopeAuthorityFacts | None:
        self.calls.append("scope_authority")
        assert query == self.query
        return self.scope_authority

    def load_controls(self, *args: object) -> ControlFacts | None:
        self.calls.append("controls")
        return self.controls

    def load_policies(self, *args: object) -> PolicyFacts | None:
        self.calls.append("policies")
        return self.policies

    def load_revision_source(self, *args: object) -> RevisionSourceFacts | None:
        self.calls.append("revision_source")
        return self.revision_source


class RecordingProvider:
    def __init__(self, query: AuthorizationFactQuery) -> None:
        self.unit_of_work = RecordingUnitOfWork(query=query)

    @contextmanager
    def open_unit_of_work(
        self, query: AuthorizationFactQuery
    ) -> Iterator[RecordingUnitOfWork]:
        assert query == self.unit_of_work.query
        yield self.unit_of_work


class SuppressingContextManager:
    def __init__(self, value: object, *, exit_error: Exception | None = None) -> None:
        self.value = value
        self.exit_error = exit_error
        self.exit_calls: list[
            tuple[type[BaseException] | None, BaseException | None, object]
        ] = []
        self.received_original_traceback = False

    def __enter__(self) -> object:
        return self.value

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: object,
    ) -> bool:
        self.exit_calls.append((exception_type, exception, traceback))
        self.received_original_traceback = (
            exception is not None and traceback is exception.__traceback__
        )
        if self.exit_error is not None:
            raise self.exit_error
        return True


class SuppressingProvider:
    def __init__(self, value: object, *, exit_error: Exception | None = None) -> None:
        self.context = SuppressingContextManager(value, exit_error=exit_error)

    def open_unit_of_work(
        self, query: AuthorizationFactQuery
    ) -> SuppressingContextManager:
        del query
        return self.context


def agent_query() -> AuthorizationFactQuery:
    return AuthorizationFactQuery(
        tenant_id="tenant-a",
        authentication=browser_binding(),
        agent_invocation=AgentInvocationBinding(
            tenant_id="tenant-a",
            actor_principal_id="agent-principal-a",
            agent_id="eios:agent:planner",
            agent_revision=8,
            release_id="planner-r3",
            release_revision=5,
            release_digest=DIGEST_B,
            agent_application_id="eios:application:planner-runtime",
            agent_application_version="v3",
            agent_application_digest="c" * 64,
        ),
        target=target(),
        request_id="request-a",
        trace_id="trace-a",
    )


def direct_query(*, credential: bool = False) -> AuthorizationFactQuery:
    authentication = credential_binding() if credential else browser_binding()
    return AuthorizationFactQuery(
        tenant_id="tenant-a",
        authentication=authentication,
        agent_invocation=None,
        target=target(),
        request_id="request-a",
        trace_id="trace-a",
    )


def delegated_query() -> AuthorizationFactQuery:
    return AuthorizationFactQuery(
        tenant_id="tenant-a",
        authentication=delegated_binding(),
        agent_invocation=None,
        target=target(),
        request_id="request-delegated",
        trace_id="trace-delegated",
    )


def test_resolver_loads_delegated_fact_and_attests_it_in_revision_vector() -> None:
    query = delegated_query()
    provider = RecordingProvider(query)

    resolved = AuthorizationFactsResolver(provider).resolve(query)

    assert "delegated_authentication" in provider.unit_of_work.calls
    assert "browser_authentication" not in provider.unit_of_work.calls
    assert "credential_authentication" not in provider.unit_of_work.calls
    delegation_entries = [
        entry
        for entry in resolved.revision_vector.entries
        if entry.category == "delegation"
    ]
    assert len(delegation_entries) == 1
    assert delegation_entries[0].key == "delegation-root-a"
    assert delegation_entries[0].revision == 5
    assert delegation_entries[0].digest == resolved.authentication.snapshot_digest


def test_resolver_uses_delegation_expiry_not_source_session_ttl() -> None:
    query = delegated_query()
    provider = RecordingProvider(query)
    provider.unit_of_work.now = query.authentication.delegation_expires_at

    with pytest.raises(AuthorizationFactDenied) as caught:
        AuthorizationFactsResolver(provider).resolve(query)

    assert caught.value.reason is AuthorizationFactDenialReason.AUTHENTICATION_EXPIRED


def test_resolver_loads_all_facts_once_in_frozen_order() -> None:
    query = agent_query()
    provider = RecordingProvider(query)

    resolved = AuthorizationFactsResolver(provider).resolve(query)

    assert provider.unit_of_work.calls == [
        "trusted_now",
        "subject",
        "membership",
        "actor",
        "browser_authentication",
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
        "revision_source",
    ]
    assert resolved.caller_application.application_id == "eios:application:portal"
    assert resolved.agent_application is not None
    assert (
        resolved.agent_application.application_id == "eios:application:planner-runtime"
    )
    assert resolved.authoritative_expires_at == NOW + timedelta(minutes=30)


def test_suppressing_context_manager_preserves_denial_and_original_exc_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = direct_query()
    unit_of_work = RecordingUnitOfWork(query=query)
    provider = SuppressingProvider(unit_of_work)
    resolver = AuthorizationFactsResolver(provider)  # type: ignore[arg-type]
    denial = AuthorizationFactDenied(AuthorizationFactDenialReason.SUBJECT_INACTIVE)

    def fail_resolution(*args: object, **kwargs: object) -> Never:
        del args, kwargs
        raise denial

    monkeypatch.setattr(resolver, "_resolve_in_unit_of_work", fail_resolution)

    with pytest.raises(AuthorizationFactDenied) as caught:
        resolver.resolve(query)

    assert caught.value is denial
    assert caught.value.reason is AuthorizationFactDenialReason.SUBJECT_INACTIVE
    exception_type, exception, traceback = provider.context.exit_calls[0]
    assert exception_type is AuthorizationFactDenied
    assert exception is denial
    assert traceback is not None
    assert provider.context.received_original_traceback


def test_suppressing_context_manager_preserves_unavailable_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = direct_query()
    provider = SuppressingProvider(RecordingUnitOfWork(query=query))
    resolver = AuthorizationFactsResolver(provider)  # type: ignore[arg-type]
    unavailable = AuthorizationUnavailable("repository snapshot unavailable")

    def fail_resolution(*args: object, **kwargs: object) -> Never:
        del args, kwargs
        raise unavailable

    monkeypatch.setattr(resolver, "_resolve_in_unit_of_work", fail_resolution)

    with pytest.raises(AuthorizationUnavailable) as caught:
        resolver.resolve(query)

    assert caught.value is unavailable
    assert str(caught.value) == "repository snapshot unavailable"
    assert provider.context.exit_calls[0][1] is unavailable


def test_suppressing_context_manager_maps_unknown_error_after_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = direct_query()
    provider = SuppressingProvider(RecordingUnitOfWork(query=query))
    resolver = AuthorizationFactsResolver(provider)  # type: ignore[arg-type]
    failure = RuntimeError("repository exploded")

    def fail_resolution(*args: object, **kwargs: object) -> Never:
        del args, kwargs
        raise failure

    monkeypatch.setattr(resolver, "_resolve_in_unit_of_work", fail_resolution)

    with pytest.raises(
        AuthorizationUnavailable, match="authorization facts could not be resolved"
    ):
        resolver.resolve(query)

    assert provider.context.exit_calls[0][0] is RuntimeError
    assert provider.context.exit_calls[0][1] is failure


def test_context_manager_exit_error_is_stably_unavailable() -> None:
    query = direct_query()
    provider = SuppressingProvider(
        RecordingUnitOfWork(query=query), exit_error=RuntimeError("close failed")
    )

    with pytest.raises(
        AuthorizationUnavailable, match="authorization facts could not be resolved"
    ):
        AuthorizationFactsResolver(provider).resolve(query)  # type: ignore[arg-type]

    assert provider.context.exit_calls == [(None, None, None)]


def test_normal_resolution_calls_exit_once_and_ignores_true_return() -> None:
    query = direct_query()
    provider = SuppressingProvider(RecordingUnitOfWork(query=query))

    resolved = AuthorizationFactsResolver(provider).resolve(query)  # type: ignore[arg-type]

    assert resolved.subject.status == "active"
    assert provider.context.exit_calls == [(None, None, None)]


@pytest.mark.parametrize("invalid_result", (None, object()))
def test_resolver_rejects_none_or_wrong_result_after_context_exit(
    invalid_result: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    query = direct_query()
    provider = RecordingProvider(query)
    resolver = AuthorizationFactsResolver(provider)
    monkeypatch.setattr(
        resolver,
        "_resolve_in_unit_of_work",
        lambda *_args, **_kwargs: invalid_result,
    )

    with pytest.raises(
        AuthorizationUnavailable, match="authorization facts could not be resolved"
    ):
        resolver.resolve(query)


def test_disabled_subject_stops_before_membership_or_later_loads() -> None:
    query = direct_query()
    provider = RecordingProvider(query)
    provider.unit_of_work.subject = SubjectFacts(
        tenant_id="tenant-a",
        subject_id="subject-a",
        kind=SubjectKind.HUMAN,
        status="disabled",
        revision=4,
        **repository_fields(),
    )

    with pytest.raises(AuthorizationFactDenied) as caught:
        AuthorizationFactsResolver(provider).resolve(query)

    assert caught.value.reason is AuthorizationFactDenialReason.SUBJECT_INACTIVE
    assert provider.unit_of_work.calls == ["trusted_now", "subject"]


@pytest.mark.parametrize(
    ("status", "valid_until", "reason"),
    [
        ("suspended", NOW + timedelta(hours=1), "membership_inactive"),
        ("active", NOW, "membership_expired"),
    ],
)
def test_inactive_or_expired_membership_is_bounded_denial(
    status: str, valid_until: datetime, reason: str
) -> None:
    query = direct_query()
    provider = RecordingProvider(query)
    provider.unit_of_work.membership = MembershipFacts(
        tenant_id="tenant-a",
        subject_id="subject-a",
        principal_id="principal-a",
        kind=MembershipKind.HOME,
        status=status,
        valid_from=NOW - timedelta(days=1),
        valid_until=valid_until,
        revision=7,
        **repository_fields(),
    )

    with pytest.raises(AuthorizationFactDenied) as caught:
        AuthorizationFactsResolver(provider).resolve(query)

    assert caught.value.reason.value == reason
    assert provider.unit_of_work.calls == ["trusted_now", "subject", "membership"]


def test_represented_human_never_queries_agent_grants() -> None:
    query = agent_query()
    provider = RecordingProvider(query)
    captured_subject_kinds: list[GrantSubjectKind] = []

    def load_grants(*args: object) -> GrantFacts:
        provider.unit_of_work.calls.append("grants")
        captured_subject_kinds.append(args[1])  # type: ignore[arg-type]
        return provider.unit_of_work.grants

    provider.unit_of_work.load_grants = load_grants  # type: ignore[method-assign]

    AuthorizationFactsResolver(provider).resolve(query)

    assert captured_subject_kinds == [GrantSubjectKind.HUMAN]
    assert GrantSubjectKind.AGENT not in captured_subject_kinds


def test_direct_agent_loads_only_agent_or_group_grants() -> None:
    authentication = credential_binding(
        subject_id="agent-subject-a",
        subject_kind=SubjectKind.AGENT,
        subject_principal_id="agent-direct-principal-a",
    )
    query = AuthorizationFactQuery(
        tenant_id="tenant-a",
        authentication=authentication,
        agent_invocation=None,
        target=target(),
        request_id="request-agent",
        trace_id="trace-agent",
    )

    resolved = AuthorizationFactsResolver(RecordingProvider(query)).resolve(query)

    assert resolved.subject_authority.subject_kind is GrantSubjectKind.AGENT
    assert {grant.subject_kind for grant in resolved.grants.grants} == {
        GrantSubjectKind.AGENT,
        GrantSubjectKind.GROUP,
    }


def test_human_on_behalf_of_agent_rejects_actor_agent_grant() -> None:
    query = agent_query()
    provider = RecordingProvider(query)
    provider.unit_of_work.grants = GrantFacts(
        tenant_id="tenant-a",
        subject_kind=GrantSubjectKind.HUMAN,
        principal_id="principal-a",
        grants=(
            EffectiveGrantFact(
                grant_id="grant-agent",
                subject_kind=GrantSubjectKind.AGENT,
                subject_id="agent-principal-a",
                role_id="role-reader",
                role_revision=5,
                role_digest=DIGEST_A,
                resource=reference(ResourceType.DATASET, "orders", 10),
                operations=frozenset({Operation.READ}),
                inherited=False,
                valid_from=NOW - timedelta(days=1),
                valid_until=NOW + timedelta(days=1),
                revision=1,
                digest=DIGEST_B,
            ),
        ),
        valid_until=NOW + timedelta(days=1),
        complete=True,
        next_cursor=None,
        revision=8,
        **repository_fields(),
    )

    with pytest.raises(AuthorizationUnavailable):
        AuthorizationFactsResolver(provider).resolve(query)


def test_tenant_switch_old_session_revision_is_denied() -> None:
    query = direct_query()
    provider = RecordingProvider(query)
    authentication = provider.unit_of_work.authentication
    assert type(authentication) is BrowserAuthenticationFacts
    provider.unit_of_work.authentication = BrowserAuthenticationFacts(
        **{
            **authentication.model_dump(
                exclude={"snapshot_digest", "repository_witness"}
            ),
            "session_revision": authentication.session_revision + 1,
            **repository_fields(),
        }
    )

    with pytest.raises(AuthorizationFactDenied) as caught:
        AuthorizationFactsResolver(provider).resolve(query)

    assert caught.value.reason is AuthorizationFactDenialReason.AUTHENTICATION_STALE


def test_api_key_without_exact_application_binding_is_unavailable() -> None:
    query = direct_query(credential=True)
    provider = RecordingProvider(query)
    provider.unit_of_work.caller_application = None  # type: ignore[assignment]

    with pytest.raises(AuthorizationUnavailable):
        AuthorizationFactsResolver(provider).resolve(query)


def test_second_page_or_partial_graph_never_produces_snapshot() -> None:
    query = direct_query()
    provider = RecordingProvider(query)
    graph = provider.unit_of_work.resource_graph
    provider.unit_of_work.resource_graph = ResourceGraphFacts(
        **{
            **graph.model_dump(exclude={"snapshot_digest", "repository_witness"}),
            "closure_complete": False,
            "next_cursor": "page-2",
            **repository_fields(),
        }
    )

    with pytest.raises(AuthorizationUnavailable):
        AuthorizationFactsResolver(provider).resolve(query)


def test_provider_unavailable_fails_closed() -> None:
    with pytest.raises(AuthorizationUnavailable):
        AuthorizationFactsResolver().resolve(direct_query())


def test_repository_witness_failure_is_unavailable() -> None:
    query = direct_query()
    provider = RecordingProvider(query)

    def reject_witness(**kwargs: object) -> bool:
        del kwargs
        return False

    provider.unit_of_work.verify_repository_witness = reject_witness  # type: ignore[method-assign]

    with pytest.raises(AuthorizationUnavailable):
        AuthorizationFactsResolver(provider).resolve(query)

    assert provider.unit_of_work.calls == ["trusted_now", "subject"]


def test_snapshot_digest_tampering_is_unavailable() -> None:
    query = direct_query()
    provider = RecordingProvider(query)
    object.__setattr__(provider.unit_of_work.subject, "snapshot_digest", "f" * 64)

    with pytest.raises(AuthorizationUnavailable):
        AuthorizationFactsResolver(provider).resolve(query)

    assert provider.unit_of_work.calls == ["trusted_now", "subject"]


def test_bad_fact_type_is_unavailable_without_coercion() -> None:
    query = direct_query()
    provider = RecordingProvider(query)

    def load_subject(*args: object) -> object:
        provider.unit_of_work.calls.append("subject")
        return provider.unit_of_work.subject.model_dump()

    provider.unit_of_work.load_subject = load_subject  # type: ignore[method-assign]

    with pytest.raises(AuthorizationUnavailable):
        AuthorizationFactsResolver(provider).resolve(query)

    assert provider.unit_of_work.calls == ["trusted_now", "subject"]


def test_cross_tenant_fact_is_unavailable() -> None:
    query = direct_query()
    provider = RecordingProvider(query)
    provider.unit_of_work.subject = SubjectFacts(
        tenant_id="tenant-b",
        subject_id="subject-a",
        kind=SubjectKind.HUMAN,
        status="active",
        revision=4,
        **repository_fields(),
    )

    with pytest.raises(AuthorizationUnavailable):
        AuthorizationFactsResolver(provider).resolve(query)


def test_non_repeatable_or_writable_unit_of_work_is_unavailable() -> None:
    query = direct_query()
    provider = RecordingProvider(query)
    provider.unit_of_work.repeatable_read = False

    with pytest.raises(AuthorizationUnavailable):
        AuthorizationFactsResolver(provider).resolve(query)

    assert provider.unit_of_work.calls == []


def test_late_io_error_never_returns_partial_snapshot() -> None:
    query = direct_query()
    provider = RecordingProvider(query)

    def load_policies(*args: object) -> PolicyFacts:
        provider.unit_of_work.calls.append("policies")
        raise OSError("database connection lost")

    provider.unit_of_work.load_policies = load_policies  # type: ignore[method-assign]

    with pytest.raises(AuthorizationUnavailable) as caught:
        AuthorizationFactsResolver(provider).resolve(query)

    assert "database connection lost" not in str(caught.value)
    assert provider.unit_of_work.calls[-1] == "policies"
    assert "revision_source" not in provider.unit_of_work.calls


def test_dependency_cycle_is_unavailable() -> None:
    query = direct_query()
    provider = RecordingProvider(query)
    graph = provider.unit_of_work.resource_graph
    connector = reference(ResourceType.CONNECTOR, "crm", 4)
    provider.unit_of_work.resource_graph = ResourceGraphFacts(
        **{
            **graph.model_dump(
                exclude={"snapshot_digest", "repository_witness", "dependencies"}
            ),
            "dependencies": (
                ResourceDependencyFact(
                    source=graph.root.resource,
                    target=connector,
                    target_active=True,
                    required_operation=Operation.READ,
                ),
                ResourceDependencyFact(
                    source=connector,
                    target=graph.root.resource,
                    target_active=True,
                    required_operation=Operation.READ,
                ),
            ),
            **repository_fields(),
        }
    )

    with pytest.raises(AuthorizationUnavailable):
        AuthorizationFactsResolver(provider).resolve(query)


def test_cross_tenant_dependency_is_unavailable() -> None:
    query = direct_query()
    provider = RecordingProvider(query)
    graph = provider.unit_of_work.resource_graph
    foreign = ResourceReference(
        tenant_id="tenant-b",
        resource_id="eios:connector:crm",
        resource_type=ResourceType.CONNECTOR,
        security_revision=4,
    )
    provider.unit_of_work.resource_graph = ResourceGraphFacts(
        **{
            **graph.model_dump(
                exclude={"snapshot_digest", "repository_witness", "dependencies"}
            ),
            "dependencies": (
                ResourceDependencyFact(
                    source=graph.root.resource,
                    target=foreign,
                    target_active=True,
                    required_operation=Operation.READ,
                ),
            ),
            **repository_fields(),
        }
    )

    with pytest.raises(AuthorizationUnavailable):
        AuthorizationFactsResolver(provider).resolve(query)


def test_duplicate_dependency_edge_is_rejected_before_resolution() -> None:
    graph = RecordingProvider(direct_query()).unit_of_work.resource_graph
    edge = graph.dependencies[0]

    with pytest.raises(ValidationError):
        ResourceGraphFacts(
            **{
                **graph.model_dump(
                    exclude={"snapshot_digest", "repository_witness", "dependencies"}
                ),
                "dependencies": (edge, edge),
                **repository_fields(),
            }
        )


def test_dependency_graph_over_depth_limit_is_unavailable() -> None:
    query = direct_query()
    provider = RecordingProvider(query)
    root = reference(ResourceType.DATASET, "orders", 10)
    chain = tuple(
        reference(ResourceType.DATASET, f"dependency-{index}", index + 20)
        for index in range(33)
    )
    edges = tuple(
        ResourceDependencyFact(
            source=root if index == 0 else chain[index - 1],
            target=dependency,
            target_active=True,
            required_operation=Operation.READ,
        )
        for index, dependency in enumerate(chain)
    )
    provider.unit_of_work.resource_graph = ResourceGraphFacts(
        tenant_id="tenant-a",
        root=ResourceNodeFact(resource=root, parent=None, active=True),
        ancestors=(),
        dependencies=edges,
        registry_revision=12,
        registry_digest="5" * 64,
        closure_complete=True,
        next_cursor=None,
        **repository_fields(),
    )

    with pytest.raises(AuthorizationUnavailable):
        AuthorizationFactsResolver(provider).resolve(query)


def test_dependency_required_operation_is_exact_and_mandatory() -> None:
    with pytest.raises(ValidationError):
        ResourceDependencyFact(
            source=reference(ResourceType.DATASET, "orders", 10),
            target=reference(ResourceType.CONNECTOR, "crm", 4),
            target_active=True,
            required_operation="read",  # type: ignore[arg-type]
        )
    with pytest.raises(ValidationError):
        ResourceDependencyFact(
            source=reference(ResourceType.DATASET, "orders", 10),
            target=reference(ResourceType.CONNECTOR, "crm", 4),
            target_active=True,
        )  # type: ignore[call-arg]


def test_resource_graph_digest_is_stable_when_dependency_rows_reorder() -> None:
    root = ResourceNodeFact(
        resource=reference(ResourceType.DATASET, "orders", 10),
        parent=None,
        active=True,
    )
    dependencies = (
        ResourceDependencyFact(
            source=root.resource,
            target=reference(ResourceType.CONNECTOR, "crm", 4),
            target_active=True,
            required_operation=Operation.READ,
        ),
        ResourceDependencyFact(
            source=root.resource,
            target=reference(ResourceType.DATASET, "customers", 5),
            target_active=True,
            required_operation=Operation.READ,
        ),
    )

    first = ResourceGraphFacts(
        tenant_id="tenant-a",
        root=root,
        ancestors=(),
        dependencies=dependencies,
        registry_revision=12,
        registry_digest="5" * 64,
        closure_complete=True,
        next_cursor=None,
        **repository_fields(),
    )
    second = ResourceGraphFacts(
        tenant_id="tenant-a",
        root=root,
        ancestors=(),
        dependencies=tuple(reversed(dependencies)),
        registry_revision=12,
        registry_digest="5" * 64,
        closure_complete=True,
        next_cursor=None,
        **repository_fields(),
    )

    assert first.dependencies == second.dependencies
    assert first.snapshot_digest == second.snapshot_digest


def test_revision_vector_is_order_stable_and_changes_with_security_fact() -> None:
    query = direct_query()
    first_provider = RecordingProvider(query)
    first = AuthorizationFactsResolver(first_provider).resolve(query)

    second_provider = RecordingProvider(query)
    authority = second_provider.unit_of_work.subject_authority
    second_provider.unit_of_work.subject_authority = SubjectAuthorityFacts(
        **{
            **authority.model_dump(
                exclude={"snapshot_digest", "repository_witness", "groups"}
            ),
            "groups": tuple(reversed(authority.groups)),
            **repository_fields(),
        }
    )
    second = AuthorizationFactsResolver(second_provider).resolve(query)

    assert first.revision_vector.digest == second.revision_vector.digest

    changed_provider = RecordingProvider(query)
    changed_authority = changed_provider.unit_of_work.subject_authority
    changed_provider.unit_of_work.subject_authority = SubjectAuthorityFacts(
        **{
            **changed_authority.model_dump(
                exclude={"snapshot_digest", "repository_witness", "groups"}
            ),
            "groups": (
                RevisionedAuthority(
                    authority_id="group-analysts", revision=4, digest="f" * 64
                ),
            ),
            **repository_fields(),
        }
    )
    changed = AuthorizationFactsResolver(changed_provider).resolve(query)

    assert changed.revision_vector.digest != first.revision_vector.digest


def _policies_with_rules(
    policies: PolicyFacts, rules: tuple[PolicyRuleFact, ...]
) -> PolicyFacts:
    return PolicyFacts(
        **{
            **policies.model_dump(
                exclude={"repository_witness", "snapshot_digest", "rules"}
            ),
            "rules": rules,
            **repository_fields(),
        }
    )


def _rule_with_policy_authority(
    rule: PolicyRuleFact, *, source: PolicyRuleFact, active_version: int | None = None
) -> PolicyRuleFact:
    return PolicyRuleFact(
        **{
            **rule.model_dump(
                exclude={
                    "policy_set_id",
                    "active_version",
                    "version_digest",
                    "canonical_ast_utf8",
                    "canonical_ast_sha256",
                    "condition",
                }
            ),
            "policy_set_id": source.policy_set_id,
            "active_version": active_version or source.active_version,
            "version_digest": source.version_digest,
            "canonical_ast_utf8": source.canonical_ast_utf8,
            "canonical_ast_sha256": source.canonical_ast_sha256,
            "condition": source.condition,
        }
    )


def test_revision_vector_deduplicates_shared_policy_version_across_bindings() -> None:
    query = direct_query()
    provider = RecordingProvider(query)
    policies = provider.unit_of_work.policies
    root_rule, dependency_rule = policies.rules
    shared_dependency_rule = _rule_with_policy_authority(
        dependency_rule, source=root_rule
    )
    provider.unit_of_work.policies = _policies_with_rules(
        policies, (root_rule, shared_dependency_rule)
    )

    resolved = AuthorizationFactsResolver(provider).resolve(query)
    binding_entries = tuple(
        entry
        for entry in resolved.revision_vector.entries
        if entry.category == "policy"
    )
    plan_entries = tuple(
        entry
        for entry in resolved.revision_vector.entries
        if entry.category == "policy_version"
    )

    assert {entry.key for entry in binding_entries} == {
        root_rule.binding_id,
        dependency_rule.binding_id,
    }
    assert tuple(
        (entry.category, entry.key, entry.revision, entry.digest)
        for entry in plan_entries
    ) == (
        (
            "policy_version",
            root_rule.policy_set_id,
            root_rule.active_version,
            root_rule.version_digest,
        ),
    )


def test_revision_vector_rejects_conflicting_shared_policy_authority() -> None:
    query = direct_query()
    provider = RecordingProvider(query)
    policies = provider.unit_of_work.policies
    root_rule, dependency_rule = policies.rules
    provider.unit_of_work.policies = _policies_with_rules(
        policies,
        (
            root_rule,
            _rule_with_policy_authority(
                dependency_rule,
                source=root_rule,
                active_version=root_rule.active_version + 1,
            ),
        ),
    )

    with pytest.raises(
        AuthorizationUnavailable, match="policy plan authority is inconsistent"
    ):
        AuthorizationFactsResolver(provider).resolve(query)


def test_shared_policy_revision_vector_digest_is_order_independent() -> None:
    query = direct_query()
    first_provider = RecordingProvider(query)
    first_policies = first_provider.unit_of_work.policies
    root_rule, dependency_rule = first_policies.rules
    shared_dependency_rule = _rule_with_policy_authority(
        dependency_rule, source=root_rule
    )
    first_provider.unit_of_work.policies = _policies_with_rules(
        first_policies, (root_rule, shared_dependency_rule)
    )
    second_provider = RecordingProvider(query)
    second_provider.unit_of_work.policies = _policies_with_rules(
        second_provider.unit_of_work.policies,
        (shared_dependency_rule, root_rule),
    )

    first = AuthorizationFactsResolver(first_provider).resolve(query)
    second = AuthorizationFactsResolver(second_provider).resolve(query)

    assert tuple(
        (entry.category, entry.key, entry.revision, entry.digest)
        for entry in first.revision_vector.entries
    ) == tuple(
        (entry.category, entry.key, entry.revision, entry.digest)
        for entry in second.revision_vector.entries
    )
    assert first.revision_vector.digest == second.revision_vector.digest


def test_revision_vector_covers_every_security_category_without_max_revision() -> None:
    query = agent_query()
    resolved = AuthorizationFactsResolver(RecordingProvider(query)).resolve(query)
    categories = {entry.category for entry in resolved.revision_vector.entries}

    assert {
        "schema",
        "subject",
        "membership",
        "actor",
        "session",
        "caller_application",
        "agent",
        "agent_release",
        "agent_application",
        "group",
        "role",
        "grant",
        "attribute",
        "clearance",
        "resource_registry",
        "resource_root",
        "resource_ancestor",
        "resource_dependency",
        "policy",
        "control",
        "scope_catalog",
        "event_high_water",
    } <= categories
    assert all(
        isinstance(entry, ReadOnlyAuthorityView)
        for entry in resolved.revision_vector.entries
    )
    assert resolved.revision_vector.digest != str(
        max(entry.revision for entry in resolved.revision_vector.entries)
    )


def test_revision_vector_changes_for_application_expiry_or_scope_authority() -> None:
    query = direct_query()
    baseline = AuthorizationFactsResolver(RecordingProvider(query)).resolve(query)

    application_provider = RecordingProvider(query)
    application = application_provider.unit_of_work.caller_application
    application_provider.unit_of_work.caller_application = ApplicationFacts(
        **{
            **application.model_dump(
                exclude={"snapshot_digest", "repository_witness", "expires_at"}
            ),
            "expires_at": NOW + timedelta(hours=2, minutes=30),
            **repository_fields(),
        }
    )
    application_changed = AuthorizationFactsResolver(application_provider).resolve(
        query
    )

    scope_provider = RecordingProvider(query)
    scope_authority = scope_provider.unit_of_work.scope_authority
    scope_provider.unit_of_work.scope_authority = ScopeAuthorityFacts(
        **{
            **scope_authority.model_dump(
                exclude={
                    "snapshot_digest",
                    "repository_witness",
                    "authorized_scopes",
                    "catalog_scopes",
                }
            ),
            "catalog_scopes": frozenset(
                {*scope_authority.catalog_scopes, "metadata.read"}
            ),
            "authorized_scopes": frozenset(
                {*scope_authority.authorized_scopes, "metadata.read"}
            ),
            **repository_fields(),
        }
    )
    scope_changed = AuthorizationFactsResolver(scope_provider).resolve(query)

    assert application_changed.revision_vector.digest != baseline.revision_vector.digest
    assert scope_changed.revision_vector.digest != baseline.revision_vector.digest


def test_resolved_snapshot_carries_actual_application_control_and_policy_authority() -> (
    None
):
    resolved = AuthorizationFactsResolver(RecordingProvider(direct_query())).resolve(
        direct_query()
    )

    assert resolved.caller_application.mode is ApplicationMode.RESTRICTED
    assert resolved.caller_application.break_glass is False
    assert resolved.caller_application.allowed_resource_keys == (
        ("tenant-a", "connector", "eios:connector:crm"),
        ("tenant-a", "dataset", "eios:dataset:orders"),
    )
    assert resolved.caller_application.allowed_operations == frozenset({Operation.READ})
    assert resolved.subject_authority.attributes["department"] == "finance"
    assert resolved.controls.subject_clearances[0].value == "public"
    assert {
        requirement.resource.resource_id
        for requirement in resolved.controls.resource_requirements
    } == {"eios:dataset:orders", "eios:connector:crm"}
    assert {rule.effect for rule in resolved.policies.rules} == {PolicyEffect.REQUIRE}
    assert all(
        isinstance(rule.condition, ReadOnlyAuthorityView)
        for rule in resolved.policies.rules
    )


def test_incomplete_agent_release_parent_chain_fails_closed() -> None:
    query = agent_query()
    provider = RecordingProvider(query)
    release = provider.unit_of_work.agent_release
    assert release is not None
    provider.unit_of_work.agent_release = AgentReleaseFacts(
        **{
            **release.model_dump(
                exclude={
                    "repository_witness",
                    "snapshot_digest",
                    "parent_release_id",
                    "chain_complete",
                }
            ),
            "parent_release_id": "release-parent",
            "chain_complete": False,
            **repository_fields(),
        }
    )

    with pytest.raises(AuthorizationUnavailable):
        AuthorizationFactsResolver(provider).resolve(query)


def test_agent_release_parent_chain_carries_bound_application_authority() -> None:
    query = agent_query()
    provider = RecordingProvider(query)
    release = provider.unit_of_work.agent_release
    assert release is not None
    parent_application = ParentApplicationAuthorityFact(
        tenant_id="tenant-a",
        application_id="eios:application:parent-agent",
        application_revision=8,
        version="v3",
        version_revision=9,
        version_digest="1" * 64,
        record_digest="2" * 64,
        application_status="active",
        version_status="published",
        eligible=True,
        mode=ApplicationMode.RESTRICTED,
        break_glass=False,
        resources=provider.unit_of_work.agent_application.resources,
        operations=provider.unit_of_work.agent_application.operations,
        published_at=NOW - timedelta(days=3),
        revoked_at=None,
        expires_at=NOW + timedelta(hours=2),
    )
    parent = ParentAgentReleaseFact(
        tenant_id="tenant-a",
        release_id="release-parent",
        parent_release_id=None,
        agent_id="eios:agent:parent",
        agent_revision=4,
        agent_status="active",
        application_id=parent_application.application_id,
        application_version=parent_application.version,
        application_version_digest=parent_application.version_digest,
        release_digest="3" * 64,
        status="active",
        eligible=True,
        revision=5,
        released_at=NOW - timedelta(days=2),
        revoked_at=None,
        application=parent_application,
    )
    provider.unit_of_work.agent_release = AgentReleaseFacts(
        **{
            **release.model_dump(
                exclude={
                    "repository_witness",
                    "snapshot_digest",
                    "parent_release_id",
                    "parent_chain",
                }
            ),
            "parent_release_id": parent.release_id,
            "parent_chain": (parent,),
            **repository_fields(),
        }
    )

    resolved = AuthorizationFactsResolver(provider).resolve(query)

    assert resolved.agent_release is not None
    projected = resolved.agent_release.parent_chain[0].application
    assert projected.application_id == parent_application.application_id
    assert projected.version == parent_application.version
    assert projected.record_digest == parent_application.record_digest
    assert resolved.authoritative_expires_at == NOW + timedelta(minutes=30)

    expected_resources = parent_application.allowed_resource_keys
    expected_operations = parent_application.allowed_operations
    for _ in range(100):
        gc.collect()
        repeated = AuthorizationFactsResolver(provider).resolve(query)
        repeated_application = repeated.agent_release.parent_chain[0].application
        assert repeated_application.allowed_resource_keys == expected_resources
        assert repeated_application.allowed_operations == expected_operations


def test_controls_must_cover_root_and_every_dependency() -> None:
    query = direct_query()
    provider = RecordingProvider(query)
    controls = provider.unit_of_work.controls
    provider.unit_of_work.controls = ControlFacts(
        **{
            **controls.model_dump(
                exclude={
                    "repository_witness",
                    "snapshot_digest",
                    "resource_requirements",
                }
            ),
            "resource_requirements": (controls.resource_requirements[0],),
            **repository_fields(),
        }
    )

    with pytest.raises(AuthorizationUnavailable):
        AuthorizationFactsResolver(provider).resolve(query)


def test_policies_must_carry_active_rules_for_root_and_every_dependency() -> None:
    query = direct_query()
    provider = RecordingProvider(query)
    policies = provider.unit_of_work.policies
    provider.unit_of_work.policies = PolicyFacts(
        **{
            **policies.model_dump(
                exclude={"repository_witness", "snapshot_digest", "rules"}
            ),
            "rules": (policies.rules[0],),
            **repository_fields(),
        }
    )

    result = evaluate_intersection(AuthorizationFactsResolver(provider).resolve(query))
    assert result.reason_codes == (DecisionReason.POLICY_DEFAULT_DENY,)


def test_scope_binding_must_cover_root_and_every_dependency() -> None:
    query = direct_query()
    provider = RecordingProvider(query)
    facts = provider.unit_of_work.scope_authority
    provider.unit_of_work.scope_authority = ScopeAuthorityFacts(
        **{
            **facts.model_dump(
                exclude={"repository_witness", "snapshot_digest", "authorities"}
            ),
            "authorities": tuple(
                authority
                for authority in facts.authorities
                if authority.resource_type is ResourceType.DATASET
            ),
            **repository_fields(),
        }
    )

    with pytest.raises(AuthorizationFactDenied) as denial:
        AuthorizationFactsResolver(provider).resolve(query)
    assert denial.value.reason is AuthorizationFactDenialReason.SCOPE_INSUFFICIENT


def test_missing_scope_catalog_fails_closed() -> None:
    query = direct_query()
    provider = RecordingProvider(query)
    provider.unit_of_work.scope_authority = None  # type: ignore[assignment]

    with pytest.raises(AuthorizationUnavailable):
        AuthorizationFactsResolver(provider).resolve(query)


def test_unknown_requested_scope_fails_closed() -> None:
    query = direct_query()
    provider = RecordingProvider(query)
    facts = provider.unit_of_work.scope_authority
    provider.unit_of_work.scope_authority = ScopeAuthorityFacts(
        **{
            **facts.model_dump(
                exclude={
                    "repository_witness",
                    "snapshot_digest",
                    "catalog_scopes",
                    "required_scopes",
                    "authorities",
                    "authorized_scopes",
                }
            ),
            "catalog_scopes": frozenset({"metadata.read"}),
            "required_scopes": frozenset({"metadata.read"}),
            "authorities": scope_authorities("metadata.read"),
            "authorized_scopes": frozenset({"metadata.read"}),
            **repository_fields(),
        }
    )

    with pytest.raises(AuthorizationUnavailable):
        AuthorizationFactsResolver(provider).resolve(query)


def test_unknown_scope_risk_is_rejected_before_resolution() -> None:
    facts = RecordingProvider(direct_query()).unit_of_work.scope_authority

    with pytest.raises(ValidationError):
        ScopeAuthorityFacts(
            **{
                **facts.model_dump(
                    exclude={"repository_witness", "snapshot_digest", "risk"}
                ),
                "risk": "future-risk",
                **repository_fields(),
            }
        )


def test_scope_authority_cannot_authorize_unknown_catalog_scope() -> None:
    facts = RecordingProvider(direct_query()).unit_of_work.scope_authority

    with pytest.raises(ValidationError):
        ScopeAuthorityFacts(
            **{
                **facts.model_dump(
                    exclude={
                        "repository_witness",
                        "snapshot_digest",
                        "authorized_scopes",
                    }
                ),
                "authorized_scopes": frozenset(
                    {*facts.authorized_scopes, "unknown.read"}
                ),
                **repository_fields(),
            }
        )


def test_extra_requested_scope_may_authorize_dependency_without_being_required() -> (
    None
):
    binding = browser_binding(
        requested_scopes=frozenset({"ontology.read", "metadata.read"})
    )
    query = AuthorizationFactQuery(
        tenant_id="tenant-a",
        authentication=binding,
        agent_invocation=None,
        target=target(),
        request_id="request-a",
        trace_id="trace-a",
    )

    resolved = AuthorizationFactsResolver(RecordingProvider(query)).resolve(query)

    assert resolved.scope_authority.authorized_scopes == frozenset(
        {"ontology.read", "metadata.read"}
    )


def test_resolved_context_is_an_opaque_non_pydantic_handle() -> None:
    query = direct_query()
    resolved = AuthorizationFactsResolver(RecordingProvider(query)).resolve(query)

    assert not issubclass(ResolvedAuthorizationContext, BaseModel)
    for forbidden in (
        "model_validate",
        "model_construct",
        "model_copy",
        "model_dump",
        "model_dump_json",
        "copy",
        "dict",
    ):
        assert not hasattr(ResolvedAuthorizationContext, forbidden)
        assert not hasattr(resolved, forbidden)
    with pytest.raises(TypeError):
        ResolvedAuthorizationContext()
    with pytest.raises(TypeError):
        iter(resolved)
    with pytest.raises(AttributeError):
        _ = resolved._ResolvedAuthorizationContext__payload  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        _ = resolved._ResolvedAuthorizationContext__issued_check  # type: ignore[attr-defined]


def test_unissued_resolved_handle_cannot_expose_authorization_fields() -> None:
    unissued = object.__new__(ResolvedAuthorizationContext)

    for field_name in (
        "query",
        "trusted_now",
        "subject",
        "scope_authority",
        "revision_vector",
        "authoritative_expires_at",
    ):
        with pytest.raises(AuthorizationUnavailable):
            getattr(unissued, field_name)
    with pytest.raises(TypeError):
        iter(unissued)


def test_resolved_handle_rejects_copy_deepcopy_and_pickle() -> None:
    resolved = AuthorizationFactsResolver(RecordingProvider(direct_query())).resolve(
        direct_query()
    )

    with pytest.raises(AuthorizationUnavailable):
        copy.copy(resolved)
    with pytest.raises(AuthorizationUnavailable):
        copy.deepcopy(resolved)
    with pytest.raises(AuthorizationUnavailable):
        pickle.dumps(resolved)


def test_resolved_handle_detects_nested_payload_tampering() -> None:
    resolved = AuthorizationFactsResolver(RecordingProvider(direct_query())).resolve(
        direct_query()
    )
    subject = resolved.subject

    assert not isinstance(subject, BaseModel)
    assert not hasattr(subject, "model_copy")
    with pytest.raises((AttributeError, AuthorizationUnavailable, TypeError)):
        subject.status = "disabled"  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        object.__setattr__(subject, "status", "disabled")
    with pytest.raises((AttributeError, TypeError)):
        object.__delattr__(subject, "status")
    with pytest.raises(TypeError):
        vars(subject)

    assert resolved.subject.status == "active"
    resolved.verify_integrity()


def test_resolved_authority_view_recursively_detaches_every_public_fact() -> None:
    query = direct_query()
    resolved = AuthorizationFactsResolver(RecordingProvider(query)).resolve(query)

    public_records = (
        resolved.query,
        resolved.subject,
        resolved.membership,
        resolved.actor,
        resolved.authentication,
        resolved.caller_application,
        resolved.subject_authority,
        resolved.resource_graph,
        resolved.grants,
        resolved.scope_authority,
        resolved.controls,
        resolved.policies,
        resolved.revision_vector,
    )
    assert all(not isinstance(record, BaseModel) for record in public_records)
    assert all(not hasattr(record, "model_copy") for record in public_records)
    assert all(not hasattr(record, "__dict__") for record in public_records)

    grant = resolved.grants.grants[0]
    rule = resolved.policies.rules[0]
    with pytest.raises((AttributeError, TypeError)):
        object.__setattr__(grant.resource, "resource_id", "eios:dataset:forged")
    with pytest.raises((AttributeError, TypeError)):
        object.__setattr__(rule, "status", "disabled")
    with pytest.raises(TypeError):
        resolved.subject_authority.attributes["department"] = "attacker"  # type: ignore[index]
    with pytest.raises(AttributeError):
        resolved.grants.grants.append(grant)  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        resolved.scope_authority.authorized_scopes.add("admin")  # type: ignore[attr-defined]

    detached = dict(resolved.subject_authority.attributes)
    detached["department"] = "attacker"
    assert resolved.subject_authority.attributes["department"] == "finance"
    assert resolved.grants.grants[0].resource.resource_id == query.target.resource_id
    assert resolved.policies.rules[0].active_version == 6


def test_scanner_miss_style_bindings_cannot_mutate_authority_view() -> None:
    resolved = AuthorizationFactsResolver(RecordingProvider(direct_query())).resolve(
        direct_query()
    )

    def rhs_before_bind() -> None:
        alias = resolved.subject
        object.__setattr__(alias, "status", "disabled")

    def closure_probe() -> None:
        alias = resolved.grants.grants[0]

        def mutate() -> None:
            object.__setattr__(alias, "revision", 999)

        mutate()

    class ClassProbe:
        alias = resolved.policies.rules[0]

        @classmethod
        def mutate(cls) -> None:
            object.__setattr__(cls.alias, "status", "disabled")

    def match_probe() -> None:
        match resolved.resource_graph.dependencies:
            case (edge, *rest):
                del rest
                object.__setattr__(edge.target, "resource_id", "eios:dataset:forged")
            case ():
                object.__setattr__(resolved.resource_graph.root, "active", False)

    for probe in (rhs_before_bind, closure_probe, ClassProbe.mutate, match_probe):
        with pytest.raises((AttributeError, TypeError)):
            probe()

    assert resolved.subject.status == "active"
    assert resolved.grants.grants[0].revision != 999
    assert resolved.policies.rules[0].active_version == 6
    assert resolved.resource_graph.root.active is True
    resolved.verify_integrity()


def test_resolved_public_property_maps_missing_payload_field_to_unavailable() -> None:
    resolved = AuthorizationFactsResolver(RecordingProvider(direct_query())).resolve(
        direct_query()
    )
    verifier = object.__getattribute__(resolved, "_resolver_verified_payload")
    view = verifier(verify_integrity=False)
    with pytest.raises((AttributeError, TypeError)):
        object.__delattr__(view, "subject")

    assert resolved.subject.status == "active"
    resolved.verify_integrity()


def test_cold_integrity_check_revokes_when_registry_raw_payload_changes() -> None:
    resolved = AuthorizationFactsResolver(RecordingProvider(direct_query())).resolve(
        direct_query()
    )
    verifier = object.__getattribute__(resolved, "_resolver_verified_payload")
    closure = verifier.__func__.__closure__
    assert closure is not None
    registry = _issuance_registry_from_verifier_closure(closure)
    raw_payload = registry[resolved][0]
    object.__setattr__(raw_payload.subject, "status", "disabled")

    # The prebuilt public projection remains detached from the raw repository fact.
    assert resolved.subject.status == "active"
    with pytest.raises(AuthorizationUnavailable):
        resolved.verify_integrity()
    with pytest.raises(AuthorizationUnavailable):
        _ = resolved.subject


@pytest.mark.parametrize("cold_verify", [False, True])
@pytest.mark.parametrize(
    "replacement",
    [
        "payload",
        "digest",
        "view",
        "view_subclass",
        "marker",
        "issued_type",
        "short_tuple",
        "identical_content_tuple",
        "tuple_subclass",
        "equal_digest_non_str",
        "deleted",
        "deleted_reinserted_copy",
        "deleted_reinserted_original",
        "combined",
    ],
)
def test_issuance_registry_rejects_every_tuple_identity_substitution(
    replacement: str, cold_verify: bool
) -> None:
    resolved = AuthorizationFactsResolver(RecordingProvider(direct_query())).resolve(
        direct_query()
    )
    other = AuthorizationFactsResolver(RecordingProvider(direct_query())).resolve(
        direct_query()
    )
    verifier = object.__getattribute__(resolved, "_resolver_verified_payload")
    closure = verifier.__func__.__closure__
    assert closure is not None
    registry = _issuance_registry_from_verifier_closure(closure)
    issued_entry = registry[resolved]
    payload, digest, view, marker, issued_type = issued_entry

    class ForgedView(ReadOnlyAuthorityView):
        __slots__ = ()

    forged_view = object.__new__(ForgedView)

    class EntryTuple(tuple):
        pass

    class EqualDigest:
        def __eq__(self, other: object) -> bool:
            return other == digest

    substitutions = {
        "payload": (object(), digest, view, marker, issued_type),
        "digest": (payload, "f" * 64, view, marker, issued_type),
        "view": (payload, digest, other.subject, marker, issued_type),
        "view_subclass": (payload, digest, forged_view, marker, issued_type),
        "marker": (payload, digest, view, object(), issued_type),
        "issued_type": (payload, digest, view, marker, type(other)),
        "short_tuple": (payload, digest, view, marker),
        "identical_content_tuple": tuple([payload, digest, view, marker, issued_type]),
        "tuple_subclass": EntryTuple((payload, digest, view, marker, issued_type)),
        "equal_digest_non_str": (
            payload,
            EqualDigest(),
            view,
            marker,
            issued_type,
        ),
        "combined": (object(), "f" * 64, other.subject, object(), type(other)),
    }
    if replacement == "deleted":
        registry.pop(resolved)
    elif replacement == "deleted_reinserted_copy":
        registry.pop(resolved)
        _force_issuance_registry_entry_for_test(
            registry,
            resolved,
            tuple([payload, digest, view, marker, issued_type]),
        )
    elif replacement == "deleted_reinserted_original":
        registry.pop(resolved)
        _force_issuance_registry_entry_for_test(registry, resolved, issued_entry)
    else:
        _force_issuance_registry_entry_for_test(
            registry, resolved, substitutions[replacement]
        )

    with pytest.raises(AuthorizationUnavailable):
        if cold_verify:
            resolved.verify_integrity()
        else:
            _ = resolved.subject
    assert resolved not in registry


def _issuance_registry_from_verifier_closure(closure: tuple[object, ...]) -> object:
    for cell in closure:
        value = cell.cell_contents  # type: ignore[attr-defined]
        if isinstance(value, WeakKeyDictionary):
            return value
        owner = getattr(value, "__self__", None)
        if type(owner).__name__ == "_IssuanceRegistry":
            return owner
    raise AssertionError("issuance registry is absent from verifier closure")


def _force_issuance_registry_entry_for_test(
    registry: object, handle: ResolvedAuthorizationContext, entry: object
) -> None:
    entries = getattr(registry, "_entries", registry)
    entries[handle] = entry


@pytest.mark.parametrize("mutation", ["overwrite", "delete_set", "failed_reregister"])
def test_issuance_registry_lifecycle_is_single_use_and_irreversible(
    mutation: str,
) -> None:
    resolved = AuthorizationFactsResolver(RecordingProvider(direct_query())).resolve(
        direct_query()
    )
    verifier = object.__getattribute__(resolved, "_resolver_verified_payload")
    closure = verifier.__func__.__closure__
    assert closure is not None
    registry = _issuance_registry_from_verifier_closure(closure)
    issued_entry = registry[resolved]  # type: ignore[index]

    if mutation == "failed_reregister":
        _force_issuance_registry_entry_for_test(
            registry, resolved, tuple([*issued_entry[:-1], object()])
        )
        with pytest.raises(AuthorizationUnavailable):
            _ = resolved.subject
    elif mutation == "delete_set":
        del registry[resolved]  # type: ignore[attr-defined]

    with pytest.raises(AuthorizationUnavailable):
        registry[resolved] = issued_entry  # type: ignore[index]
    with pytest.raises(AuthorizationUnavailable):
        _ = resolved.subject


def test_issuance_registry_supports_concurrent_constant_time_reads() -> None:
    resolved = AuthorizationFactsResolver(RecordingProvider(direct_query())).resolve(
        direct_query()
    )

    with ThreadPoolExecutor(max_workers=8) as executor:
        statuses = tuple(executor.map(lambda _: resolved.subject.status, range(256)))

    assert statuses == ("active",) * 256
    resolved.verify_integrity()


def test_issuance_registry_weak_state_does_not_prevent_handle_gc() -> None:
    resolved = AuthorizationFactsResolver(RecordingProvider(direct_query())).resolve(
        direct_query()
    )
    verifier = object.__getattribute__(resolved, "_resolver_verified_payload")
    closure = verifier.__func__.__closure__
    assert closure is not None
    registry = _issuance_registry_from_verifier_closure(closure)
    handle_reference = ref(resolved)

    del closure, verifier, resolved
    gc.collect()

    assert handle_reference() is None
    assert len(registry) == 0  # type: ignore[arg-type]


def test_resolved_handle_rejects_private_payload_replacement() -> None:
    resolved = AuthorizationFactsResolver(RecordingProvider(direct_query())).resolve(
        direct_query()
    )

    with pytest.raises(AttributeError):
        object.__setattr__(
            resolved,
            "_ResolvedAuthorizationContext__payload",
            object(),
        )

    assert resolved.subject.status == "active"


def test_resolved_handle_rejects_simultaneous_payload_and_checker_replacement() -> None:
    resolved = AuthorizationFactsResolver(RecordingProvider(direct_query())).resolve(
        direct_query()
    )
    with pytest.raises(AttributeError):
        object.__setattr__(
            resolved,
            "_ResolvedAuthorizationContext__payload",
            object(),
        )
    with pytest.raises(AttributeError):
        object.__setattr__(
            resolved,
            "_ResolvedAuthorizationContext__issued_check",
            lambda *_: True,
        )

    assert resolved.subject.status == "active"


def test_resolved_handle_dynamic_class_rejects_ordinary_mutation() -> None:
    resolved = AuthorizationFactsResolver(RecordingProvider(direct_query())).resolve(
        direct_query()
    )
    issued_type = type(resolved)

    with pytest.raises(TypeError, match="immutable"):
        setattr(issued_type, "injected", object())
    with pytest.raises(TypeError, match="immutable"):
        setattr(
            issued_type,
            "_resolver_verified_payload",
            lambda _self: object(),
        )
    with pytest.raises(TypeError, match="immutable"):
        setattr(
            issued_type,
            "_ResolvedAuthorizationContext__checked_payload",
            lambda _self: object(),
        )
    with pytest.raises(TypeError, match="immutable"):
        delattr(issued_type, "_resolver_verified_payload")

    assert resolved.subject.status == "active"


def test_large_grant_handle_property_reads_do_not_rehash_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = direct_query()
    provider = RecordingProvider(query)
    grant_facts = provider.unit_of_work.grants
    template = grant_facts.grants[0]
    grants = tuple(
        EffectiveGrantFact(
            **{
                **template.model_dump(exclude={"grant_id", "revision", "digest"}),
                "grant_id": f"grant-{index:04d}",
                "revision": index + 1,
                "digest": f"{index:064x}",
            }
        )
        for index in range(4096)
    )
    provider.unit_of_work.grants = GrantFacts(
        **{
            **grant_facts.model_dump(
                exclude={"repository_witness", "snapshot_digest", "grants"}
            ),
            "grants": grants,
            **repository_fields(),
        }
    )
    resolver_module = importlib.import_module("eios.authz._fact_resolver")
    canonical_digest = resolver_module._canonical_digest
    project_authority_view = resolver_module._project_authority_view
    digest_calls = 0
    payload_digest_calls = 0
    projection_calls = 0

    def counted_digest(value: object) -> str:
        nonlocal digest_calls, payload_digest_calls
        digest_calls += 1
        if type(value).__name__ == "_ResolvedAuthorizationPayload":
            payload_digest_calls += 1
        return canonical_digest(value)

    monkeypatch.setattr(resolver_module, "_canonical_digest", counted_digest)

    payload_projection_calls = 0

    # NexLoop adaptation (NX-049 8c): the vendored projection takes an optional
    # reuse map and also projects each registered fact once through
    # _cached_view, so the spy forwards every argument and counts payload
    # projections separately; reads after resolution must still project nothing.
    def counted_projection(value: object, *args: object, **kwargs: object) -> object:
        nonlocal projection_calls, payload_projection_calls
        projection_calls += 1
        if type(value).__name__ == "_ResolvedAuthorizationPayload":
            payload_projection_calls += 1
        return project_authority_view(value, *args, **kwargs)

    monkeypatch.setattr(resolver_module, "_project_authority_view", counted_projection)

    resolved = AuthorizationFactsResolver(provider).resolve(query)
    calls_after_resolution = digest_calls
    projections_after_resolution = projection_calls
    assert calls_after_resolution > 0
    assert payload_digest_calls == 1
    assert payload_projection_calls == 1
    assert projections_after_resolution >= 1

    for _ in range(60):
        assert len(resolved.grants.grants) == 4096
        assert resolved.subject.status == "active"
        assert resolved.revision_vector.digest is not None

    assert digest_calls == calls_after_resolution
    assert payload_digest_calls == 1
    assert projection_calls == projections_after_resolution

    resolved.verify_integrity()
    assert digest_calls == calls_after_resolution + 1
    assert payload_digest_calls == 2


def test_raw_dynamic_handle_and_cross_resolver_class_reuse_fail_closed() -> None:
    first = AuthorizationFactsResolver(RecordingProvider(direct_query())).resolve(
        direct_query()
    )
    second = AuthorizationFactsResolver(RecordingProvider(direct_query())).resolve(
        direct_query()
    )
    raw = object.__new__(type(first))

    with pytest.raises(AuthorizationUnavailable):
        _ = raw.subject

    try:
        object.__setattr__(first, "__class__", type(second))
    except (AttributeError, TypeError, AuthorizationUnavailable):
        pass
    else:
        with pytest.raises(AuthorizationUnavailable):
            _ = first.subject


def test_issuer_is_not_importable_or_obtainable_from_fake_resolver() -> None:
    facts_module = importlib.import_module("eios.authz.facts")
    resolver_module = importlib.import_module("eios.authz._fact_resolver")

    assert not hasattr(facts_module, "_RESOLVER_ISSUER")
    assert not hasattr(resolver_module, "_RESOLVER_ISSUER")
    assert not hasattr(ResolvedAuthorizationContext, "_mark_resolved")
    assert not hasattr(
        AuthorizationFactsResolver(RecordingProvider(direct_query())), "issuer"
    )

    class FakeResolver:
        def resolve(
            self, query: AuthorizationFactQuery
        ) -> ResolvedAuthorizationContext:
            del query
            return object.__new__(ResolvedAuthorizationContext)

    forged = FakeResolver().resolve(direct_query())
    with pytest.raises(AuthorizationUnavailable):
        _ = forged.query


def test_query_contains_request_inputs_but_no_scope_catalog_authority() -> None:
    query = AuthorizationFactQuery(
        tenant_id="tenant-a",
        authentication=browser_binding(),
        agent_invocation=None,
        target=target(),
        request_attributes=FrozenJsonMap({"network_zone": "corp"}),
        request_id="request-a",
        trace_id="trace-a",
    )

    assert query.request_attributes["network_zone"] == "corp"
    assert "scope_requirement" not in AuthorizationFactQuery.model_fields
    assert {
        "authorities",
        "catalog_id",
        "catalog_version",
        "catalog_revision",
        "catalog_digest",
        "risk",
    }.isdisjoint(AuthorizationFactQuery.model_fields)

    with pytest.raises(ValidationError):
        AuthorizationFactQuery(
            **query.model_dump(),
            scope_requirement={"caller_supplied": True},  # type: ignore[call-arg]
        )


def test_scope_catalog_authority_is_an_independent_repository_fact() -> None:
    facts = ScopeAuthorityFacts(
        tenant_id="tenant-a",
        principal_id="principal-a",
        binding_id="route-dataset-read",
        catalog_id="scope-catalog",
        catalog_version="v6",
        catalog_revision=6,
        catalog_digest=DIGEST_B,
        catalog_scopes=frozenset({"ontology.read", "metadata.read"}),
        required_scopes=frozenset({"ontology.read"}),
        authorities=scope_authorities(),
        risk=AuthorizationRisk.READ_ONLY,
        authorized_scopes=frozenset({"ontology.read", "metadata.read"}),
        revision=4,
        valid_until=NOW + timedelta(minutes=40),
        **repository_fields(),
    )

    assert facts.catalog_version == "v6"
    assert facts.required_scopes == frozenset({"ontology.read"})


def test_break_glass_application_facts_require_bounded_expiry() -> None:
    template = RecordingProvider(direct_query()).unit_of_work.caller_application
    base = template.model_dump(
        exclude={"repository_witness", "snapshot_digest"}
    )

    def build(**changes: object) -> ApplicationFacts:
        return ApplicationFacts(**{**base, **changes, **repository_fields()})

    valid = build(
        mode=ApplicationMode.UNRESTRICTED,
        break_glass=True,
        published_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(minutes=5),
    )
    assert valid.break_glass

    with pytest.raises(ValidationError):
        build(
            mode=ApplicationMode.UNRESTRICTED,
            break_glass=True,
            published_at=NOW - timedelta(minutes=5),
            expires_at=None,
        )
    with pytest.raises(ValidationError):
        build(
            mode=ApplicationMode.UNRESTRICTED,
            break_glass=True,
            published_at=NOW - timedelta(hours=2),
            expires_at=NOW + timedelta(hours=2),
        )


def test_effective_grant_inherits_defaults_to_fail_closed() -> None:
    grants = RecordingProvider(direct_query()).unit_of_work.grants
    template = grants.grants[0].model_dump(exclude={"inherits"})

    assert EffectiveGrantFact(**template).inherits is False
    assert EffectiveGrantFact(**{**template, "inherits": True}).inherits is True


class TestFactDenialCarriesTheDatabaseReason:
    """42501 是全部授权 definer 的共用拒绝出口,不能把真因塌缩掉。

    委托路径上至少六种不同原因共用 SQLSTATE 42501:root authority unavailable、
    action unavailable、approval unavailable、scope exceeds root、effects exceed
    root、resolver context mismatch。2026-08-08 委托写平面报了几小时的
    authentication_stale,而真正的拒绝理由完全是另一回事 —— 因为这些原因在
    应用层全都长成同一个枚举值。
    """

    def test_reason_stays_a_stable_enum_and_detail_carries_the_message(self) -> None:
        from eios.authz.facts import (
            AuthorizationFactDenialReason,
            AuthorizationFactDenied,
        )

        denied = AuthorizationFactDenied(
            AuthorizationFactDenialReason.AUTHENTICATION_STALE,
            detail="delegated scope exceeds root authority",
        )

        # 稳定枚举不变:上层按 reason 分支的代码不受影响
        assert denied.reason is AuthorizationFactDenialReason.AUTHENTICATION_STALE
        # 但真因不再消失
        assert denied.detail == "delegated scope exceeds root authority"
        assert "scope exceeds root authority" in str(denied)

    def test_detail_is_optional_so_existing_callers_are_unchanged(self) -> None:
        from eios.authz.facts import (
            AuthorizationFactDenialReason,
            AuthorizationFactDenied,
        )

        denied = AuthorizationFactDenied(
            AuthorizationFactDenialReason.SUBJECT_INACTIVE
        )

        assert denied.detail == ""
        assert str(denied) == "subject_inactive"


def _browser_fact_with_expiry(provider, *, expires_at, absolute_expires_at):
    authentication = provider.unit_of_work.authentication
    assert type(authentication) is BrowserAuthenticationFacts
    provider.unit_of_work.authentication = BrowserAuthenticationFacts(
        **{
            **authentication.model_dump(exclude={"snapshot_digest"}),
            "expires_at": expires_at,
            "absolute_expires_at": absolute_expires_at,
        }
    )


def test_browser_idle_renewal_keeps_authority_vector_but_not_fact_seal() -> None:
    query = direct_query()
    before, after = RecordingProvider(query), RecordingProvider(query)
    for provider, minutes in ((before, 10), (after, 20)):
        _browser_fact_with_expiry(
            provider,
            expires_at=NOW + timedelta(minutes=minutes),
            absolute_expires_at=NOW + timedelta(days=1),
        )
    first = AuthorizationFactsResolver(before).resolve(query)
    second = AuthorizationFactsResolver(after).resolve(query)
    assert first.authentication.snapshot_digest != second.authentication.snapshot_digest
    assert first.revision_vector.digest == second.revision_vector.digest
    # Effective expiry still limits the actual decision; it is not discarded.
    assert first.authoritative_expires_at == NOW + timedelta(minutes=10)
    assert second.authoritative_expires_at == NOW + timedelta(minutes=20)
    after.unit_of_work.now = NOW + timedelta(minutes=20)
    with pytest.raises(AuthorizationFactDenied) as caught:
        AuthorizationFactsResolver(after).resolve(query)
    assert caught.value.reason is AuthorizationFactDenialReason.AUTHENTICATION_EXPIRED


def test_browser_absolute_deadline_remains_in_authority_vector() -> None:
    query = direct_query()
    before, after = RecordingProvider(query), RecordingProvider(query)
    for provider, days in ((before, 1), (after, 2)):
        _browser_fact_with_expiry(
            provider,
            expires_at=NOW + timedelta(minutes=10),
            absolute_expires_at=NOW + timedelta(days=days),
        )
    first = AuthorizationFactsResolver(before).resolve(query)
    second = AuthorizationFactsResolver(after).resolve(query)
    assert first.revision_vector.digest != second.revision_vector.digest


def test_browser_absolute_deadline_is_required_and_bounds_effective_expiry() -> None:
    authentication = RecordingProvider(direct_query()).unit_of_work.authentication
    values = authentication.model_dump(exclude={"snapshot_digest"})
    without_absolute = {k: v for k, v in values.items() if k != "absolute_expires_at"}
    with pytest.raises(ValidationError):
        BrowserAuthenticationFacts(**without_absolute)
    with pytest.raises(ValidationError):
        BrowserAuthenticationFacts(**{**values, "absolute_expires_at": NOW})
