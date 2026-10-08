from __future__ import annotations

from copy import copy, deepcopy
from datetime import UTC, datetime, timedelta
from enum import Enum
from inspect import signature
import pickle
from time import perf_counter
from types import SimpleNamespace

from hypothesis import given, settings, strategies as st
import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

import eios.authz.applications as application_authority
from eios.authz.applications import (
    FROZEN_RESOURCE_TYPE_VALUES,
    MAX_BREAK_GLASS_LIFETIME,
    Agent,
    AgentRelease,
    AgentReleaseStatus,
    AgentStatus,
    Application,
    ApplicationCeilingReason,
    ApplicationMode,
    ApplicationReleaseInvalid,
    ApplicationRestrictionError,
    ApplicationStatus,
    ApplicationVersion,
    ApplicationVersionImmutable,
    ApplicationVersionState,
    OperationRestriction,
    ResourceRestriction,
    restore_attested_application_version,
    restore_bound_agent_release,
)
from eios.authz.operations import Operation


NOW = datetime(2026, 7, 21, 1, 0, tzinfo=UTC)
FROZEN_TYPES = (
    "tenant",
    "project",
    "folder",
    "dataset",
    "connector",
    "object_type",
    "link_type",
    "interface",
    "object_view",
    "object_set",
    "function",
    "action",
    "capability",
    "api_operation",
    "artifact",
    "agent",
    "application",
    "external_effect",
)
DENIED_REASONS = (ApplicationCeilingReason.APPLICATION_CEILING_DENIED,)


class CompatibleResourceType(str, Enum):
    DATASET = "dataset"


class OrdinaryRepositoryVerifier:
    def verify_application_version(self, **_kwargs: object) -> bool:
        return True

    def verify_agent_release(self, **_kwargs: object) -> bool:
        return True


ORDINARY_REPOSITORY_VERIFIER = OrdinaryRepositoryVerifier()


def resource(
    name: str = "finance/revenue",
    *,
    tenant_id: str = "tenant-a",
    resource_type: str | CompatibleResourceType = CompatibleResourceType.DATASET,
) -> SimpleNamespace:
    type_value = (
        resource_type.value
        if isinstance(resource_type, CompatibleResourceType)
        else resource_type
    )
    return SimpleNamespace(
        tenant_id=tenant_id,
        resource_type=resource_type,
        resource_id=f"eios:{type_value}:{name}",
        security_revision=1,
    )


def restriction(
    name: str = "finance/revenue",
    *,
    tenant_id: str = "tenant-a",
    resource_type: str | CompatibleResourceType = CompatibleResourceType.DATASET,
) -> ResourceRestriction:
    type_value = (
        resource_type.value
        if isinstance(resource_type, CompatibleResourceType)
        else resource_type
    )
    return ResourceRestriction(
        tenant_id=tenant_id,
        resource_type=resource_type,
        resource_id=f"eios:{type_value}:{name}",
    )


def draft_version(
    *,
    application_id: str = "eios:application:agent-runtime",
    version: str = "v1",
    resources: tuple[ResourceRestriction, ...] = (),
    operations: tuple[OperationRestriction, ...] = (),
    mode: ApplicationMode = ApplicationMode.RESTRICTED,
    expires_at: datetime | None = None,
    break_glass: bool = False,
    approved_governance_reference: str | None = None,
    approved_at: datetime | None = None,
    revision: int = 1,
) -> ApplicationVersion:
    return ApplicationVersion.create_draft(
        tenant_id="tenant-a",
        application_id=application_id,
        version=version,
        state=ApplicationVersionState.DRAFT,
        resources=resources,
        operations=operations,
        mode=mode,
        expires_at=expires_at,
        break_glass=break_glass,
        approved_governance_reference=approved_governance_reference,
        approved_at=approved_at,
        published_at=None,
        revoked_at=None,
        revision=revision,
    )


def restricted_version(
    *,
    application_id: str = "eios:application:agent-runtime",
    version: str = "v1",
    resources: tuple[ResourceRestriction, ...] | None = None,
    operations: tuple[OperationRestriction, ...] | None = None,
    expires_at: datetime | None = None,
) -> ApplicationVersion:
    draft = draft_version(
        application_id=application_id,
        version=version,
        resources=(restriction(),) if resources is None else resources,
        operations=(OperationRestriction(operation=Operation.READ),)
        if operations is None
        else operations,
        expires_at=expires_at,
    )
    return draft.publish(at=NOW)


def unrestricted_version(*, expires_at: datetime) -> ApplicationVersion:
    return draft_version(
        application_id="eios:application:platform-operator",
        resources=(
            restriction(
                "platform/operator",
                resource_type="application",
            ),
        ),
        operations=(OperationRestriction(operation=Operation.MANAGE_POLICY),),
        mode=ApplicationMode.UNRESTRICTED,
        break_glass=True,
        approved_governance_reference="governance-approval-123",
        approved_at=NOW,
        expires_at=expires_at,
    ).publish(at=NOW)


def application(
    application_id: str = "eios:application:agent-runtime",
    *,
    status: ApplicationStatus = ApplicationStatus.ACTIVE,
    revision: int = 1,
) -> Application:
    return Application(
        tenant_id="tenant-a",
        application_id=application_id,
        display_name="Agent runtime",
        status=status,
        revision=revision,
    )


def agent(
    *,
    parent_agent_id: str | None = None,
    agent_id: str | None = None,
    application_id: str = "eios:application:agent-runtime",
    status: AgentStatus = AgentStatus.ACTIVE,
    revision: int = 1,
) -> Agent:
    return Agent(
        tenant_id="tenant-a",
        agent_id=agent_id
        or ("eios:agent:child" if parent_agent_id else "eios:agent:root"),
        application_id=application_id,
        display_name="Agent",
        parent_agent_id=parent_agent_id,
        status=status,
        revision=revision,
    )


def test_resource_type_guard_is_exactly_the_frozen_eighteen_values() -> None:
    assert FROZEN_RESOURCE_TYPE_VALUES == FROZEN_TYPES + ("object", "property", "relation")
    assert len(FROZEN_RESOURCE_TYPE_VALUES) == len(set(FROZEN_RESOURCE_TYPE_VALUES))


def test_empty_application_restrictions_mean_zero_authority() -> None:
    version = restricted_version(resources=(), operations=())

    decision = version.evaluate(Operation.READ, resource(), at=NOW)

    assert not decision.allowed
    assert decision.reason_codes == DENIED_REASONS
    assert not version.allows(Operation.READ, resource(), at=NOW)


def test_omitted_allowlists_are_restricted_draft_with_zero_authority() -> None:
    version = ApplicationVersion(
        tenant_id="tenant-a",
        application_id="eios:application:agent-runtime",
        version="v1",
    )

    assert version.mode is ApplicationMode.RESTRICTED
    assert version.state is ApplicationVersionState.DRAFT
    assert version.resources == ()
    assert version.operations == ()
    assert version.evaluate(Operation.READ, resource(), at=NOW).reason_codes == (
        ApplicationCeilingReason.APPLICATION_CEILING_DENIED,
    )


def test_all_ceiling_mismatches_have_identical_external_type_code_and_message() -> None:
    version = restricted_version()
    operation_only = version.evaluate(Operation.EDIT, resource(), at=NOW)
    resource_only = version.evaluate(Operation.READ, resource("finance/cost"), at=NOW)
    both = version.evaluate(
        Operation.EDIT,
        resource("finance/cost", tenant_id="tenant-b"),
        at=NOW,
    )

    assert type(operation_only) is type(resource_only) is type(both)
    assert operation_only == resource_only == both
    assert operation_only.reason_codes == DENIED_REASONS


def test_matching_resource_and_operation_are_allowed() -> None:
    decision = restricted_version().evaluate(Operation.READ, resource(), at=NOW)

    assert decision.allowed
    assert decision.reason_codes == (
        ApplicationCeilingReason.APPLICATION_CEILING_MATCH,
    )


def test_restriction_accepts_the_resource_type_enum_shape_from_task_7() -> None:
    item = restriction(resource_type=CompatibleResourceType.DATASET)
    version = restricted_version(resources=(item,))

    assert item.resource_type == "dataset"
    assert version.allows(
        Operation.READ,
        resource(resource_type=CompatibleResourceType.DATASET),
        at=NOW,
    )


def test_version_digest_is_canonical_order_stable_and_authority_sensitive() -> None:
    revenue = restriction("finance/revenue")
    costs = restriction("finance/costs")
    read = OperationRestriction(operation=Operation.READ)
    edit = OperationRestriction(operation=Operation.EDIT)
    left = restricted_version(
        resources=(revenue, costs), operations=(read, edit), version="v7"
    )
    right = restricted_version(
        resources=(costs, revenue), operations=(edit, read), version="v7"
    )
    narrower = restricted_version(
        resources=(revenue,), operations=(read, edit), version="v7"
    )

    assert left.version_digest == right.version_digest
    assert len(left.version_digest) == 64
    assert left.version_digest != narrower.version_digest
    assert left.reference == (
        "tenant-a",
        "eios:application:agent-runtime",
        "v7",
        left.version_digest,
    )


def test_canonical_application_agent_and_resource_ids_are_required() -> None:
    cases = (
        (Application, {**application().model_dump(), "application_id": "portal"}),
        (Agent, {**agent().model_dump(), "agent_id": "agent-root"}),
        (
            ResourceRestriction,
            {**restriction().model_dump(), "resource_id": "finance/revenue"},
        ),
        (
            ResourceRestriction,
            {**restriction().model_dump(), "resource_type": "unknown"},
        ),
        (
            ResourceRestriction,
            {**restriction().model_dump(), "resource_id": "eios:dataset:a:b:c"},
        ),
        (
            ResourceRestriction,
            {**restriction().model_dump(), "resource_id": "eios:dataset:a:"},
        ),
        (
            ResourceRestriction,
            {**restriction().model_dump(), "resource_id": "eios:dataset:{bad"},
        ),
        (
            ResourceRestriction,
            {
                **restriction().model_dump(),
                "resource_type": "api_operation",
                "resource_id": "eios:api_operation:GET/api/{}",
            },
        ),
    )
    for model, values in cases:
        with pytest.raises(ValidationError):
            model(**values)


def test_version_rejects_cross_tenant_restrictions_at_configuration_time() -> None:
    with pytest.raises(ValidationError, match="same tenant"):
        draft_version(resources=(restriction(tenant_id="tenant-b"),))


def test_published_application_version_is_frozen_and_cannot_expand() -> None:
    published = restricted_version()

    with pytest.raises(ValidationError, match="frozen"):
        published.version = "v2"
    with pytest.raises(ApplicationVersionImmutable):
        published.with_authority(
            resources=(restriction(), restriction("finance/costs")),
            operations=published.operations,
        )
    with pytest.raises(ApplicationVersionImmutable):
        published.model_copy(
            update={
                "resources": (restriction(), restriction("finance/payroll")),
            }
        )


def test_draft_authority_change_produces_a_new_value_without_mutating_source() -> None:
    original = draft_version()
    replacement = original.with_authority(
        resources=(restriction(),),
        operations=(OperationRestriction(operation=Operation.READ),),
    )

    assert original.resources == ()
    assert replacement.resources == (restriction(),)
    assert original.version_digest != replacement.version_digest


def test_publish_is_one_way_and_requires_a_utc_timestamp() -> None:
    published = draft_version().publish(at=NOW)

    assert published.state is ApplicationVersionState.PUBLISHED
    assert published.published_at == NOW
    with pytest.raises(ApplicationVersionImmutable):
        published.publish(at=NOW + timedelta(seconds=1))
    with pytest.raises(ValueError, match="timezone-aware"):
        draft_version().publish(at=NOW.replace(tzinfo=None))


def test_version_publish_and_revoke_are_monotonic_but_content_digest_is_stable() -> (
    None
):
    draft = draft_version(
        resources=(restriction(),),
        operations=(OperationRestriction(operation=Operation.READ),),
    )
    published = draft.publish(at=NOW)
    revoked = published.revoke(at=NOW + timedelta(seconds=1))

    assert (draft.revision, published.revision, revoked.revision) == (1, 2, 3)
    assert published.state is ApplicationVersionState.PUBLISHED
    assert revoked.state is ApplicationVersionState.REVOKED
    assert revoked.revoked_at == NOW + timedelta(seconds=1)
    assert draft.version_digest == published.version_digest == revoked.version_digest
    assert (
        revoked.evaluate(Operation.READ, resource(), at=NOW).reason_codes
        == DENIED_REASONS
    )
    with pytest.raises(ApplicationVersionImmutable):
        draft.revoke(at=NOW)
    with pytest.raises(ApplicationVersionImmutable):
        revoked.revoke(at=NOW + timedelta(seconds=2))
    with pytest.raises(ValueError, match="publication"):
        published.revoke(at=NOW - timedelta(seconds=1))


def test_application_and_agent_lifecycle_status_and_revision_are_strict() -> None:
    active_application = application()
    disabled_application = active_application.disable()
    revoked_application = disabled_application.revoke()
    active_agent = agent()
    disabled_agent = active_agent.disable()
    revoked_agent = disabled_agent.revoke()

    assert (disabled_application.status, disabled_application.revision) == (
        ApplicationStatus.DISABLED,
        2,
    )
    assert (revoked_application.status, revoked_application.revision) == (
        ApplicationStatus.REVOKED,
        3,
    )
    assert (disabled_agent.status, disabled_agent.revision) == (AgentStatus.DISABLED, 2)
    assert (revoked_agent.status, revoked_agent.revision) == (AgentStatus.REVOKED, 3)
    with pytest.raises(ValidationError):
        application(revision=True)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        agent(revision=True)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        draft_version(revision=True)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Application(
            **{
                **active_application.model_dump(),
                "status": "active",
            }
        )
    release = AgentRelease.bind(
        release_id="agent-release-strict",
        application=active_application,
        agent=active_agent,
        application_version=restricted_version(),
        released_at=NOW,
    )
    with pytest.raises(ValidationError):
        AgentRelease(**{**release.model_dump(), "revision": True})


def test_unvalidated_construct_and_unbound_base_copy_can_never_evaluate_allow() -> None:
    version = restricted_version()
    constructed = ApplicationVersion.model_construct(**version.model_dump())
    copied = BaseModel.model_copy(
        version,
        update={"resources": (restriction("finance/payroll"),)},
    )

    for forged in (constructed, copied):
        with pytest.raises(ApplicationRestrictionError, match="validated"):
            forged.evaluate(Operation.READ, resource(), at=NOW)
        with pytest.raises(ApplicationRestrictionError, match="validated"):
            forged.is_narrower_than(version, at=NOW)


def test_bind_rejects_unvalidated_application_agent_version_and_parent_release() -> (
    None
):
    version = restricted_version()
    valid_application = application()
    valid_agent = agent()
    forged_inputs = (
        (
            Application.model_construct(**valid_application.model_dump()),
            valid_agent,
            version,
        ),
        (
            valid_application,
            Agent.model_construct(**valid_agent.model_dump()),
            version,
        ),
        (
            valid_application,
            valid_agent,
            ApplicationVersion.model_construct(**version.model_dump()),
        ),
        (
            BaseModel.model_copy(
                application(status=ApplicationStatus.DISABLED),
                update={"status": ApplicationStatus.ACTIVE},
            ),
            valid_agent,
            version,
        ),
        (
            valid_application,
            BaseModel.model_copy(
                agent(status=AgentStatus.DISABLED),
                update={"status": AgentStatus.ACTIVE},
            ),
            version,
        ),
    )
    for forged_application, forged_agent, forged_version in forged_inputs:
        with pytest.raises(ApplicationReleaseInvalid, match="not validated"):
            AgentRelease.bind(
                release_id="agent-release-forged",
                application=forged_application,
                agent=forged_agent,
                application_version=forged_version,
                released_at=NOW,
            )

    parent = AgentRelease.bind(
        release_id="agent-release-parent",
        application=valid_application,
        agent=valid_agent,
        application_version=version,
        released_at=NOW,
    )
    unbound_parent = AgentRelease(**parent.model_dump())
    with pytest.raises(ApplicationReleaseInvalid, match="not validated"):
        AgentRelease.bind(
            release_id="agent-release-child",
            application=valid_application,
            agent=agent(parent_agent_id=valid_agent.agent_id),
            application_version=restricted_version(version="child-v1"),
            released_at=NOW,
            parent_release=unbound_parent,
            parent_application_version=version,
        )


def test_version_seal_covers_tuple_identity_and_every_lifecycle_security_field() -> (
    None
):
    version = restricted_version()
    updates = (
        {"resources": tuple([*version.resources])},
        {"operations": tuple([*version.operations])},
        {"state": ApplicationVersionState.REVOKED},
        {"revision": version.revision + 1},
        {"expires_at": NOW + timedelta(minutes=1)},
        {"published_at": NOW + timedelta(seconds=1)},
        {"revoked_at": NOW + timedelta(seconds=1)},
    )

    for update in updates:
        forged = BaseModel.model_copy(version, update=update)
        with pytest.raises(ApplicationRestrictionError, match="validated"):
            forged.evaluate(Operation.READ, resource(), at=NOW)


def test_nested_resource_mutation_is_rejected_by_evaluate_digest_dump_and_bind() -> (
    None
):
    version = restricted_version()
    item = version.resources[0]
    original_digest = version.version_digest
    object.__setattr__(item, "resource_id", "eios:dataset:finance/payroll")

    with pytest.raises(ApplicationRestrictionError, match="resource restriction"):
        version.evaluate(Operation.READ, resource(), at=NOW)
    with pytest.raises(ApplicationRestrictionError, match="resource restriction"):
        _ = version.version_digest
    with pytest.raises(ApplicationRestrictionError, match="resource restriction"):
        version.model_dump()
    with pytest.raises(ApplicationReleaseInvalid, match="not validated"):
        AgentRelease.bind(
            release_id="agent-release-mutated-resource",
            application=application(),
            agent=agent(),
            application_version=version,
            released_at=NOW,
        )
    assert original_digest != ""


def test_nested_operation_mutation_is_rejected_by_evaluate_digest_dump_and_bind() -> (
    None
):
    version = restricted_version()
    item = version.operations[0]
    object.__setattr__(item, "operation", Operation.EDIT)

    with pytest.raises(ApplicationRestrictionError, match="operation restriction"):
        version.evaluate(Operation.READ, resource(), at=NOW)
    with pytest.raises(ApplicationRestrictionError, match="operation restriction"):
        _ = version.version_digest
    with pytest.raises(ApplicationRestrictionError, match="operation restriction"):
        version.model_dump()
    with pytest.raises(ApplicationReleaseInvalid, match="not validated"):
        AgentRelease.bind(
            release_id="agent-release-mutated-operation",
            application=application(),
            agent=agent(),
            application_version=version,
            released_at=NOW,
        )


def test_nested_resource_tamper_cannot_launder_through_any_public_serializer() -> None:
    version = restricted_version()
    item = version.resources[0]
    object.__setattr__(item, "resource_id", "eios:dataset:finance/payroll")

    with pytest.raises(ApplicationRestrictionError, match="resource restriction"):
        version.model_dump_json()
    with pytest.raises(ApplicationRestrictionError, match="resource restriction"):
        dict(version)
    with pytest.raises(ApplicationRestrictionError, match="resource restriction"):
        copy(version)
    with pytest.raises(ApplicationRestrictionError, match="resource restriction"):
        deepcopy(version)
    with pytest.raises(ApplicationRestrictionError, match="resource restriction"):
        pickle.dumps(version)

    raw_json = version.__pydantic_serializer__.to_json(version)
    raw_python = version.__pydantic_serializer__.to_python(version, mode="python")
    adapter = TypeAdapter(ApplicationVersion)
    adapter_json = adapter.dump_json(version)
    adapter_python = adapter.dump_python(version, mode="python")
    with pytest.raises(ValidationError, match="digest"):
        ApplicationVersion.model_validate_json(raw_json)
    with pytest.raises(ValidationError, match="digest"):
        ApplicationVersion.model_validate(raw_python)
    with pytest.raises(ValidationError, match="digest"):
        adapter.validate_json(adapter_json)
    with pytest.raises(ValidationError, match="digest"):
        adapter.validate_python(adapter_python)


def test_public_validation_never_reissues_application_authority() -> None:
    version = restricted_version()
    clean_payload = version.model_dump()
    adapter = TypeAdapter(ApplicationVersion)

    public_copies = (
        ApplicationVersion.model_validate(version),
        ApplicationVersion.model_validate(clean_payload),
        ApplicationVersion.model_validate_json(version.model_dump_json()),
        adapter.validate_python(version),
        adapter.validate_python(clean_payload),
    )
    for restored in public_copies:
        assert restored.version_digest == version.version_digest
        with pytest.raises(ApplicationRestrictionError, match="attested"):
            restored.evaluate(Operation.READ, resource(), at=NOW)
    assert version.allows(Operation.READ, resource(), at=NOW)

    restored_draft = ApplicationVersion.model_validate(draft_version().model_dump())
    with pytest.raises(ApplicationRestrictionError, match="attested"):
        restored_draft.publish(at=NOW)

    object.__setattr__(
        version.resources[0],
        "resource_id",
        "eios:dataset:finance/payroll",
    )
    with pytest.raises(ValidationError, match="digest"):
        ApplicationVersion.model_validate(version)


def test_repository_restore_freezes_0038_db_witness_upgrade_contract() -> None:
    assert not hasattr(
        application_authority,
        "install_application_repository_attestation_verifier",
    )
    assert not hasattr(
        application_authority,
        "_APPLICATION_REPOSITORY_VERIFIER",
    )
    assert not hasattr(
        application_authority.ApplicationRepositoryAttestationVerifier,
        "attest_application_version",
    )
    with pytest.raises(TypeError):
        application_authority.ApplicationRepositoryAttestationVerifier()

    verifier = application_authority.ApplicationRepositoryAttestationVerifier
    assert isinstance(ORDINARY_REPOSITORY_VERIFIER, verifier)
    assert tuple(signature(verifier.verify_application_version).parameters) == (
        "self",
        "frozen_authority_digest",
        "frozen_record_digest",
        "repository_attestation",
    )
    assert tuple(signature(verifier.verify_agent_release).parameters) == (
        "self",
        "frozen_authority_digest",
        "frozen_release_digest",
        "repository_attestation",
    )
    assert tuple(signature(restore_attested_application_version).parameters) == (
        "payload",
        "frozen_authority_digest",
        "frozen_record_digest",
        "repository_attestation",
        "repository_verifier",
    )
    assert tuple(signature(restore_bound_agent_release).parameters) == (
        "payload",
        "application_version",
        "frozen_authority_digest",
        "frozen_release_digest",
        "repository_attestation",
        "repository_verifier",
    )
    for restore in (
        restore_attested_application_version,
        restore_bound_agent_release,
    ):
        documentation = " ".join((restore.__doc__ or "").split())
        assert "0038" in documentation
        assert "DB-backed verification" in documentation
        assert "does not claim" in documentation


def test_repository_restore_fails_closed_without_trusted_witness_binding() -> None:
    version = restricted_version()
    payload = version.model_dump()
    repository_attestation = version.version_digest
    restored = ApplicationVersion.model_validate(payload)

    with pytest.raises(ApplicationRestrictionError, match="invalid issuer"):
        restored._mark_authority_attested(object())
    with pytest.raises(ApplicationRestrictionError, match="attested"):
        restored.evaluate(Operation.READ, resource(), at=NOW)

    expanded_payload = version.model_dump()
    expanded_payload["resources"][0]["resource_id"] = "eios:dataset:finance/payroll"
    expanded_payload["resources"][0]["restriction_digest"] = None
    expanded_payload["authority_digest"] = None
    expanded_payload["record_digest"] = None
    expanded = ApplicationVersion.model_validate(expanded_payload)
    assert expanded.version_digest != version.version_digest
    with pytest.raises(ApplicationRestrictionError, match="attested"):
        expanded.evaluate(Operation.READ, resource("finance/payroll"), at=NOW)

    with pytest.raises(ApplicationRestrictionError, match="frozen authority digest"):
        restore_attested_application_version(
            expanded_payload,
            frozen_authority_digest=version.version_digest,
            frozen_record_digest=version.record_digest,
            repository_attestation=repository_attestation,
            repository_verifier=ORDINARY_REPOSITORY_VERIFIER,
        )

    lifecycle_payload = version.model_dump()
    lifecycle_payload["revision"] = version.revision + 1
    lifecycle_payload["record_digest"] = None
    lifecycle_copy = ApplicationVersion.model_validate(lifecycle_payload)
    assert lifecycle_copy.version_digest == version.version_digest
    assert lifecycle_copy.record_digest != version.record_digest
    with pytest.raises(ApplicationRestrictionError, match="frozen record digest"):
        restore_attested_application_version(
            lifecycle_payload,
            frozen_authority_digest=version.version_digest,
            frozen_record_digest=version.record_digest,
            repository_attestation=repository_attestation,
            repository_verifier=ORDINARY_REPOSITORY_VERIFIER,
        )

    for self_attestation in (version.version_digest, "0" * 64):
        with pytest.raises(
            ApplicationRestrictionError,
            match="attestation verifier is unavailable",
        ):
            restore_attested_application_version(
                payload,
                frozen_authority_digest=version.version_digest,
                frozen_record_digest=version.record_digest,
                repository_attestation=self_attestation,
                repository_verifier=ORDINARY_REPOSITORY_VERIFIER,
            )

    with pytest.raises(ApplicationRestrictionError, match="attested"):
        restored.evaluate(Operation.READ, resource(), at=NOW)


def test_nested_restrictions_cannot_individually_launder_through_serializers() -> None:
    resource_item = restriction()
    operation_item = OperationRestriction(operation=Operation.READ)
    object.__setattr__(resource_item, "resource_id", "eios:dataset:finance/payroll")
    object.__setattr__(operation_item, "operation", Operation.EDIT)

    for item, model in (
        (resource_item, ResourceRestriction),
        (operation_item, OperationRestriction),
    ):
        with pytest.raises(ApplicationRestrictionError, match="restriction"):
            item.model_dump_json()
        with pytest.raises(ApplicationRestrictionError, match="restriction"):
            dict(item)
        with pytest.raises(ApplicationRestrictionError, match="restriction"):
            copy(item)
        with pytest.raises(ApplicationRestrictionError, match="restriction"):
            pickle.dumps(item)
        raw_json = item.__pydantic_serializer__.to_json(item)
        with pytest.raises(ValidationError, match="digest"):
            model.model_validate_json(raw_json)


def test_legal_json_python_copy_and_pickle_round_trips_remain_stable() -> None:
    version = restricted_version()
    json_round_trip = ApplicationVersion.model_validate_json(version.model_dump_json())
    python_round_trip = ApplicationVersion.model_validate(version.model_dump())
    adapter = TypeAdapter(ApplicationVersion)
    adapter_json_round_trip = adapter.validate_json(adapter.dump_json(version))
    adapter_python_round_trip = adapter.validate_python(
        adapter.dump_python(version, mode="python")
    )
    copied = copy(version)
    deep_copied = deepcopy(version)
    model_copied = version.model_copy()
    deep_model_copied = version.model_copy(deep=True)
    deprecated_copied = version.copy()
    deprecated_deep_copied = version.copy(deep=True)
    pickled = pickle.loads(pickle.dumps(version))

    for restored in (
        copied,
        deep_copied,
        model_copied,
        deep_model_copied,
        deprecated_copied,
        deprecated_deep_copied,
    ):
        assert restored.version_digest == version.version_digest
        assert restored.allows(Operation.READ, resource(), at=NOW)

    for data_only in (
        json_round_trip,
        python_round_trip,
        adapter_json_round_trip,
        adapter_python_round_trip,
        pickled,
    ):
        assert data_only.version_digest == version.version_digest
        with pytest.raises(ApplicationRestrictionError, match="attested"):
            data_only.evaluate(Operation.READ, resource(), at=NOW)


def test_content_digest_covers_authority_and_break_glass_security_fields() -> None:
    base = restricted_version()
    assert (
        base.version_digest
        != restricted_version(resources=(restriction("finance/costs"),)).version_digest
    )
    assert (
        base.version_digest
        != restricted_version(
            operations=(OperationRestriction(operation=Operation.EDIT),)
        ).version_digest
    )
    assert (
        base.version_digest
        != restricted_version(expires_at=NOW + timedelta(minutes=1)).version_digest
    )

    break_glass = unrestricted_version(expires_at=NOW + timedelta(minutes=10))
    later_expiry = unrestricted_version(expires_at=NOW + timedelta(minutes=11))
    other_governance = draft_version(
        application_id="eios:application:platform-operator",
        resources=(restriction("platform/operator", resource_type="application"),),
        operations=(OperationRestriction(operation=Operation.MANAGE_POLICY),),
        mode=ApplicationMode.UNRESTRICTED,
        break_glass=True,
        approved_governance_reference="governance-approval-456",
        approved_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
    ).publish(at=NOW)
    assert break_glass.version_digest != later_expiry.version_digest
    assert break_glass.version_digest != other_governance.version_digest


@pytest.mark.parametrize(
    "overrides",
    (
        {"break_glass": False},
        {"break_glass": 1},
        {"expires_at": None},
        {"expires_at": NOW},
        {"expires_at": NOW.replace(tzinfo=None)},
        {"approved_at": None},
        {"approved_at": NOW.replace(tzinfo=None)},
        {"approved_governance_reference": None},
        {"approved_governance_reference": "   "},
    ),
)
def test_unrestricted_requires_expiry_after_approval_and_approved_governance(
    overrides: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "application_id": "eios:application:platform-operator",
        "resources": (restriction("platform/operator", resource_type="application"),),
        "operations": (OperationRestriction(operation=Operation.MANAGE_POLICY),),
        "mode": ApplicationMode.UNRESTRICTED,
        "break_glass": True,
        "approved_governance_reference": "governance-approval-123",
        "approved_at": NOW,
        "expires_at": NOW + timedelta(minutes=10),
    }
    values.update(overrides)

    with pytest.raises((ValidationError, ValueError)):
        draft_version(**values)  # type: ignore[arg-type]


def test_break_glass_is_platform_capped_nonempty_exact_and_at_most_fifteen_minutes() -> (
    None
):
    assert MAX_BREAK_GLASS_LIFETIME == timedelta(minutes=15)

    for overrides in (
        {"resources": ()},
        {"operations": ()},
        {"resources": (restriction(),)},
        {"expires_at": NOW + timedelta(minutes=15, microseconds=1)},
    ):
        values: dict[str, object] = {
            "application_id": "eios:application:platform-operator",
            "resources": (
                restriction("platform/operator", resource_type="application"),
            ),
            "operations": (OperationRestriction(operation=Operation.MANAGE_POLICY),),
            "mode": ApplicationMode.UNRESTRICTED,
            "break_glass": True,
            "approved_governance_reference": "governance-approval-123",
            "approved_at": NOW,
            "expires_at": NOW + timedelta(minutes=15),
        }
        values.update(overrides)
        with pytest.raises(ValidationError):
            draft_version(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "operation",
    (
        Operation.READ,
        Operation.CREATE,
        Operation.EDIT,
        Operation.DELETE,
        Operation.EXECUTE,
        Operation.APPROVE,
        Operation.DELEGATE,
        Operation.EXPORT,
    ),
)
def test_break_glass_forbids_non_platform_operations(operation: Operation) -> None:
    with pytest.raises(ValidationError, match="platform operations"):
        draft_version(
            application_id="eios:application:platform-operator",
            resources=(restriction("platform/operator", resource_type="application"),),
            operations=(OperationRestriction(operation=operation),),
            mode=ApplicationMode.UNRESTRICTED,
            break_glass=True,
            approved_governance_reference="governance-approval-123",
            approved_at=NOW,
            expires_at=NOW + timedelta(minutes=10),
        )


@pytest.mark.parametrize(
    "operation",
    (
        Operation.DISCOVER,
        Operation.VIEW_METADATA,
        Operation.MANAGE_PERMISSIONS,
        Operation.MANAGE_POLICY,
    ),
)
def test_break_glass_allows_only_explicitly_listed_platform_operations(
    operation: Operation,
) -> None:
    version = draft_version(
        application_id="eios:application:platform-operator",
        resources=(restriction("platform/operator", resource_type="application"),),
        operations=(OperationRestriction(operation=operation),),
        mode=ApplicationMode.UNRESTRICTED,
        break_glass=True,
        approved_governance_reference="governance-approval-123",
        approved_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
    ).publish(at=NOW)
    assert version.allows(
        operation,
        resource("platform/operator", resource_type="application"),
        at=NOW,
    )


def test_break_glass_tenant_resource_never_inherits_to_project_or_wildcard() -> None:
    with pytest.raises(ValidationError):
        restriction("*", resource_type="project")

    tenant_restriction = restriction("tenant-a", resource_type="tenant")
    version = draft_version(
        application_id="eios:application:platform-operator",
        resources=(tenant_restriction,),
        operations=(OperationRestriction(operation=Operation.DISCOVER),),
        mode=ApplicationMode.UNRESTRICTED,
        break_glass=True,
        approved_governance_reference="governance-approval-123",
        approved_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
    ).publish(at=NOW)

    assert version.allows(
        Operation.DISCOVER,
        resource("tenant-a", resource_type="tenant"),
        at=NOW,
    )
    assert not version.allows(
        Operation.DISCOVER,
        resource("project-a", resource_type="project"),
        at=NOW,
    )


def test_break_glass_still_requires_both_exact_allowlist_matches_and_expires() -> None:
    version = unrestricted_version(expires_at=NOW + timedelta(minutes=10))
    allowed_target = resource("platform/operator", resource_type="application")
    assert version.allows(Operation.MANAGE_POLICY, allowed_target, at=NOW)
    assert not version.allows(Operation.MANAGE_PERMISSIONS, allowed_target, at=NOW)
    assert not version.allows(
        Operation.MANAGE_POLICY,
        resource("platform/other", resource_type="application"),
        at=NOW,
    )

    expired = version.evaluate(
        Operation.MANAGE_POLICY,
        allowed_target,
        at=NOW + timedelta(minutes=10),
    )
    assert not expired.allowed
    assert expired.reason_codes == DENIED_REASONS


def test_unrestricted_version_cannot_be_bound_to_an_ordinary_agent_release() -> None:
    version = unrestricted_version(expires_at=NOW + timedelta(minutes=10))

    with pytest.raises(ApplicationReleaseInvalid, match="unrestricted"):
        AgentRelease.bind(
            release_id="agent-release-root-v1",
            application=application("eios:application:platform-operator"),
            agent=agent(application_id="eios:application:platform-operator"),
            application_version=version,
            released_at=NOW,
        )


def test_agent_release_binds_exact_published_immutable_application_version() -> None:
    version = restricted_version()
    release = AgentRelease.bind(
        release_id="agent-release-root-v1",
        application=application(),
        agent=agent(),
        application_version=version,
        released_at=NOW,
    )

    assert release.application_version == version.version
    assert release.application_version_digest == version.version_digest
    assert release.application_id == version.application_id
    with pytest.raises(ValidationError, match="frozen"):
        release.application_version_digest = "f" * 64
    with pytest.raises(ApplicationReleaseInvalid, match="published"):
        AgentRelease.bind(
            release_id="agent-release-draft",
            application=application(),
            agent=agent(),
            application_version=draft_version(),
            released_at=NOW,
        )


def test_public_release_restore_fails_closed_without_trusted_witness_binding() -> None:
    version = restricted_version()
    release = AgentRelease.bind(
        release_id="agent-release-round-trip",
        application=application(),
        agent=agent(),
        application_version=version,
        released_at=NOW,
    )
    payload = release.model_dump()
    adapter = TypeAdapter(AgentRelease)

    for data_only in (
        AgentRelease.model_validate(release),
        AgentRelease.model_validate_json(release.model_dump_json()),
        adapter.validate_python(release),
        pickle.loads(pickle.dumps(release)),
    ):
        assert data_only.model_dump() == release.model_dump()
        with pytest.raises(ApplicationReleaseInvalid, match="not validated and bound"):
            data_only._ensure_bound()

    for safe_copy in (
        copy(release),
        deepcopy(release),
        release.model_copy(),
        release.model_copy(deep=True),
    ):
        safe_copy._ensure_bound()

    ordinary_release = AgentRelease.model_validate(payload)
    with pytest.raises(ApplicationReleaseInvalid, match="invalid issuer"):
        ordinary_release._mark_bound(object())
    with pytest.raises(ApplicationReleaseInvalid, match="not validated and bound"):
        ordinary_release._ensure_bound()

    tampered_release = copy(release)
    object.__setattr__(tampered_release, "revision", release.revision + 1)
    with pytest.raises(ApplicationReleaseInvalid, match="record is not validated"):
        tampered_release.model_dump_json()
    with pytest.raises(ApplicationReleaseInvalid, match="record is not validated"):
        dict(tampered_release)
    with pytest.raises(ApplicationReleaseInvalid, match="record is not validated"):
        pickle.dumps(tampered_release)
    with pytest.raises(ValidationError, match="release_digest"):
        AgentRelease.model_validate_json(
            tampered_release.__pydantic_serializer__.to_json(tampered_release)
        )

    repository_attestation = release.release_digest
    for self_attestation in (release.release_digest, "0" * 64):
        with pytest.raises(
            ApplicationReleaseInvalid,
            match="attestation verifier is unavailable",
        ):
            restore_bound_agent_release(
                payload,
                application_version=version,
                frozen_authority_digest=version.version_digest,
                frozen_release_digest=release.release_digest,
                repository_attestation=self_attestation,
                repository_verifier=ORDINARY_REPOSITORY_VERIFIER,
            )

    changed_release_payload = release.model_dump()
    changed_release_payload["revision"] = release.revision + 1
    changed_release_payload["release_digest"] = None
    changed_release = AgentRelease.model_validate(changed_release_payload)
    assert changed_release.release_digest != release.release_digest
    with pytest.raises(ApplicationReleaseInvalid, match="frozen release digest"):
        restore_bound_agent_release(
            changed_release_payload,
            application_version=version,
            frozen_authority_digest=version.version_digest,
            frozen_release_digest=release.release_digest,
            repository_attestation=repository_attestation,
            repository_verifier=ORDINARY_REPOSITORY_VERIFIER,
        )

    with pytest.raises(ApplicationReleaseInvalid, match="frozen authority digest"):
        restore_bound_agent_release(
            payload,
            application_version=version,
            frozen_authority_digest="f" * 64,
            frozen_release_digest=release.release_digest,
            repository_attestation=repository_attestation,
            repository_verifier=ORDINARY_REPOSITORY_VERIFIER,
        )


@pytest.mark.parametrize(
    ("application_status", "agent_status"),
    (
        (ApplicationStatus.DISABLED, AgentStatus.ACTIVE),
        (ApplicationStatus.REVOKED, AgentStatus.ACTIVE),
        (ApplicationStatus.ACTIVE, AgentStatus.DISABLED),
        (ApplicationStatus.ACTIVE, AgentStatus.REVOKED),
    ),
)
def test_release_bind_requires_active_application_agent_and_eligible_version(
    application_status: ApplicationStatus,
    agent_status: AgentStatus,
) -> None:
    with pytest.raises(ApplicationReleaseInvalid, match="active"):
        AgentRelease.bind(
            release_id="agent-release-ineligible",
            application=application(status=application_status),
            agent=agent(status=agent_status),
            application_version=restricted_version(),
            released_at=NOW,
        )

    expired = restricted_version(expires_at=NOW + timedelta(seconds=1))
    with pytest.raises(ApplicationReleaseInvalid, match="eligible"):
        AgentRelease.bind(
            release_id="agent-release-expired",
            application=application(),
            agent=agent(),
            application_version=expired,
            released_at=NOW + timedelta(seconds=1),
        )


def test_release_revocation_is_monotonic_revisioned_and_blocks_children() -> None:
    version = restricted_version(version="parent-v1")
    parent = AgentRelease.bind(
        release_id="agent-release-parent-v1",
        application=application(),
        agent=agent(),
        application_version=version,
        released_at=NOW,
    )
    revoked = parent.revoke(at=NOW + timedelta(seconds=1))

    assert (parent.status, parent.revision, parent.revoked_at) == (
        AgentReleaseStatus.ACTIVE,
        1,
        None,
    )
    assert (revoked.status, revoked.revision, revoked.revoked_at) == (
        AgentReleaseStatus.REVOKED,
        2,
        NOW + timedelta(seconds=1),
    )
    with pytest.raises(ApplicationReleaseInvalid, match="active"):
        AgentRelease.bind(
            release_id="agent-release-child-v1",
            application=application(),
            agent=agent(parent_agent_id="eios:agent:root"),
            application_version=restricted_version(version="child-v1"),
            released_at=NOW + timedelta(seconds=2),
            parent_release=revoked,
            parent_application_version=version,
        )
    with pytest.raises(ApplicationReleaseInvalid, match="released_at"):
        parent.revoke(at=NOW - timedelta(seconds=1))
    with pytest.raises(ApplicationReleaseInvalid, match="already revoked"):
        revoked.revoke(at=NOW + timedelta(seconds=2))


def test_agent_release_rejects_tenant_or_application_mismatch() -> None:
    wrong_application = restricted_version(
        application_id="eios:application:other-runtime"
    )

    with pytest.raises(ApplicationReleaseInvalid, match="application"):
        AgentRelease.bind(
            release_id="agent-release-mismatch",
            application=application(),
            agent=agent(),
            application_version=wrong_application,
            released_at=NOW,
        )


def test_child_agent_can_only_narrow_parent_release_ceiling() -> None:
    read = OperationRestriction(operation=Operation.READ)
    edit = OperationRestriction(operation=Operation.EDIT)
    revenue = restriction("finance/revenue")
    costs = restriction("finance/costs")
    parent_version = restricted_version(
        resources=(revenue, costs), operations=(read, edit), version="parent-v1"
    )
    parent_release = AgentRelease.bind(
        release_id="agent-release-parent-v1",
        application=application(),
        agent=agent(),
        application_version=parent_version,
        released_at=NOW,
    )
    child_identity = agent(parent_agent_id="eios:agent:root")
    child_version = restricted_version(
        resources=(revenue,), operations=(read,), version="child-v1"
    )

    release = AgentRelease.bind(
        release_id="agent-release-child-v1",
        application=application(),
        agent=child_identity,
        application_version=child_version,
        released_at=NOW,
        parent_release=parent_release,
        parent_application_version=parent_version,
    )
    assert release.parent_release_id == parent_release.release_id

    expanded = restricted_version(
        resources=(revenue, restriction("finance/payroll")),
        operations=(read,),
        version="child-v2",
    )
    with pytest.raises(ApplicationReleaseInvalid, match="expand parent"):
        AgentRelease.bind(
            release_id="agent-release-child-v2",
            application=application(),
            agent=child_identity,
            application_version=expanded,
            released_at=NOW,
            parent_release=parent_release,
            parent_application_version=parent_version,
        )


def test_child_cannot_outlive_an_expiring_parent_ceiling() -> None:
    parent_version = restricted_version(
        version="parent-v1", expires_at=NOW + timedelta(minutes=5)
    )
    parent_release = AgentRelease.bind(
        release_id="agent-release-parent-v1",
        application=application(),
        agent=agent(),
        application_version=parent_version,
        released_at=NOW,
    )
    child_identity = agent(parent_agent_id="eios:agent:root")
    child_version = restricted_version(version="child-v1", expires_at=None)

    with pytest.raises(ApplicationReleaseInvalid, match="expand parent"):
        AgentRelease.bind(
            release_id="agent-release-child-v1",
            application=application(),
            agent=child_identity,
            application_version=child_version,
            released_at=NOW,
            parent_release=parent_release,
            parent_application_version=parent_version,
        )


def test_child_requires_active_exact_parent_version_and_monotonic_release_time() -> (
    None
):
    parent_version = restricted_version(version="parent-v1")
    parent_release = AgentRelease.bind(
        release_id="agent-release-parent-v1",
        application=application(),
        agent=agent(),
        application_version=parent_version,
        released_at=NOW + timedelta(seconds=1),
    )
    child_identity = agent(parent_agent_id="eios:agent:root")
    child_version = restricted_version(version="child-v1")

    with pytest.raises(ApplicationReleaseInvalid, match="released before parent"):
        AgentRelease.bind(
            release_id="agent-release-child-early",
            application=application(),
            agent=child_identity,
            application_version=child_version,
            released_at=NOW,
            parent_release=parent_release,
            parent_application_version=parent_version,
        )

    wrong_parent_versions = (
        restricted_version(version="parent-v2"),
        restricted_version(
            application_id="eios:application:other", version="parent-v1"
        ),
        restricted_version(
            version="parent-v1",
            resources=(restriction("finance/other"),),
        ),
    )
    for wrong_parent_version in wrong_parent_versions:
        with pytest.raises(
            ApplicationReleaseInvalid,
            match="supplied application version",
        ):
            AgentRelease.bind(
                release_id="agent-release-child-wrong-parent",
                application=application(),
                agent=child_identity,
                application_version=child_version,
                released_at=NOW + timedelta(seconds=2),
                parent_release=parent_release,
                parent_application_version=wrong_parent_version,
            )

    revoked_parent_version = parent_version.revoke(at=NOW + timedelta(seconds=1))
    with pytest.raises(ApplicationReleaseInvalid, match="eligible"):
        AgentRelease.bind(
            release_id="agent-release-child-revoked-parent",
            application=application(),
            agent=child_identity,
            application_version=child_version,
            released_at=NOW + timedelta(seconds=2),
            parent_release=parent_release,
            parent_application_version=revoked_parent_version,
        )


def test_child_may_use_a_different_application_only_when_full_ceiling_is_subset() -> (
    None
):
    revenue = restriction()
    read = OperationRestriction(operation=Operation.READ)
    parent_version = restricted_version(
        resources=(revenue,), operations=(read,), version="parent-v1"
    )
    parent_release = AgentRelease.bind(
        release_id="agent-release-parent-v1",
        application=application(),
        agent=agent(),
        application_version=parent_version,
        released_at=NOW,
    )
    child_application_id = "eios:application:child-runtime"
    child_version = restricted_version(
        application_id=child_application_id,
        resources=(revenue,),
        operations=(read,),
        version="child-v1",
    )

    release = AgentRelease.bind(
        release_id="agent-release-child-v1",
        application=application(child_application_id),
        agent=agent(
            parent_agent_id="eios:agent:root",
            application_id=child_application_id,
        ),
        application_version=child_version,
        released_at=NOW,
        parent_release=parent_release,
        parent_application_version=parent_version,
    )
    assert release.application_id == child_application_id


def test_each_bound_child_preserves_transitive_ancestor_ceiling_and_expiry() -> None:
    read = OperationRestriction(operation=Operation.READ)
    revenue = restriction("finance/revenue")
    costs = restriction("finance/costs")
    root_version = restricted_version(
        resources=(revenue, costs),
        operations=(read,),
        version="root-v1",
        expires_at=NOW + timedelta(minutes=10),
    )
    root_release = AgentRelease.bind(
        release_id="agent-release-root-v1",
        application=application(),
        agent=agent(),
        application_version=root_version,
        released_at=NOW,
    )
    middle_agent = agent(
        agent_id="eios:agent:middle",
        parent_agent_id="eios:agent:root",
    )
    middle_version = restricted_version(
        resources=(revenue,),
        operations=(read,),
        version="middle-v1",
        expires_at=NOW + timedelta(minutes=8),
    )
    middle_release = AgentRelease.bind(
        release_id="agent-release-middle-v1",
        application=application(),
        agent=middle_agent,
        application_version=middle_version,
        released_at=NOW + timedelta(seconds=1),
        parent_release=root_release,
        parent_application_version=root_version,
    )
    leaf_agent = agent(
        agent_id="eios:agent:leaf",
        parent_agent_id="eios:agent:middle",
    )
    leaf_version = restricted_version(
        resources=(revenue,),
        operations=(read,),
        version="leaf-v1",
        expires_at=NOW + timedelta(minutes=5),
    )

    leaf_release = AgentRelease.bind(
        release_id="agent-release-leaf-v1",
        application=application(),
        agent=leaf_agent,
        application_version=leaf_version,
        released_at=NOW + timedelta(seconds=2),
        parent_release=middle_release,
        parent_application_version=middle_version,
    )
    assert leaf_release.parent_release_id == middle_release.release_id


@pytest.mark.parametrize(
    ("model", "values"),
    (
        (
            Application,
            {
                "tenant_id": "tenant-a",
                "application_id": "eios:application:app",
                "display_name": " ",
            },
        ),
        (
            Application,
            {
                "tenant_id": "tenant-a",
                "application_id": "eios:application:app",
                "display_name": " App ",
            },
        ),
        (
            Agent,
            {
                "tenant_id": "tenant-a",
                "agent_id": "eios:agent:a",
                "application_id": "eios:application:app",
                "display_name": "Agent",
                "parent_agent_id": " ",
            },
        ),
    ),
)
def test_models_reject_blank_fields(
    model: type[object], values: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        model(**values)  # type: ignore[operator]


def test_models_are_extra_forbid_and_reject_bool_as_integer_or_string_coercion() -> (
    None
):
    with pytest.raises(ValidationError, match="extra_forbidden"):
        Application(**{**application().model_dump(), "client_secret": "raw-secret"})
    with pytest.raises(ValidationError, match="extra_forbidden"):
        AgentRelease(
            **{
                **AgentRelease.bind(
                    release_id="agent-release-root-v1",
                    application=application(),
                    agent=agent(),
                    application_version=restricted_version(),
                    released_at=NOW,
                ).model_dump(),
                "credential": "raw-credential",
            }
        )
    with pytest.raises(ValidationError):
        ApplicationVersion(
            **{
                **draft_version().model_dump(),
                "break_glass": 1,
            }
        )
    with pytest.raises(ValidationError):
        Application(
            tenant_id=1,  # type: ignore[arg-type]
            application_id="eios:application:app",
            display_name="App",
        )


def test_serialized_models_never_contain_secret_or_credential_fields() -> None:
    version = restricted_version()
    release = AgentRelease.bind(
        release_id="agent-release-root-v1",
        application=application(),
        agent=agent(),
        application_version=version,
        released_at=NOW,
    )
    serialized = repr(
        (
            application().model_dump(mode="json"),
            version.model_dump(mode="json"),
            release.model_dump(mode="json"),
        )
    ).lower()

    assert "secret" not in serialized
    assert "credential" not in serialized


def test_reason_codes_are_a_closed_bounded_enum() -> None:
    assert tuple(item.value for item in ApplicationCeilingReason) == (
        "application_ceiling_match",
        "application_ceiling_denied",
    )
    assert len(ApplicationCeilingReason) == 2


@given(
    allowed_operations=st.sets(
        st.sampled_from(tuple(Operation)), max_size=len(Operation)
    ),
    requested=st.sampled_from(tuple(Operation)),
    resource_allowed=st.booleans(),
)
@settings(max_examples=100, deadline=None)
def test_generated_restricted_ceiling_is_the_exact_operation_resource_intersection(
    allowed_operations: set[Operation],
    requested: Operation,
    resource_allowed: bool,
) -> None:
    resources = (restriction(),) if resource_allowed else ()
    version = restricted_version(
        resources=resources,
        operations=tuple(
            OperationRestriction(operation=operation)
            for operation in sorted(allowed_operations, key=lambda item: item.value)
        ),
    )

    assert version.allows(requested, resource(), at=NOW) is (
        requested in allowed_operations and resource_allowed
    )


@pytest.mark.performance
def test_large_restriction_sets_have_bounded_lookup_cost_without_truncation() -> None:
    restrictions = tuple(
        restriction(f"datasets/item-{index}") for index in range(5_000)
    )
    version = restricted_version(resources=restrictions)
    target = resource("datasets/item-4999")

    started = perf_counter()
    for _ in range(5_000):
        assert version.allows(Operation.READ, target, at=NOW)
    elapsed = perf_counter() - started

    assert len(version.resources) == 5_000
    assert elapsed < 1.0
