from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eios.authz.grants import (
    EffectiveRoleGrant,
    GrantDomainError,
    GrantStatus,
    GrantSubjectKind,
    Group,
    GroupDirectory,
    PageRequest,
    Role,
    effective_grants,
    effective_operations,
)
from eios.authz.operations import Operation
from eios.authz.resources import ResourceLifecycle, ResourceReference, ResourceType


NOW = datetime(2026, 7, 21, 3, 0, tzinfo=UTC)


def _group(group_id: str) -> Group:
    return Group(
        tenant_id="tenant-a",
        group_id=group_id,
        display_name=f"Group {group_id}",
        revision=1,
    )


def _role() -> Role:
    return Role(
        tenant_id="tenant-a",
        role_id="reader",
        display_name="Reader",
        operations=frozenset({Operation.READ}),
        revision=1,
    )


def _grant() -> EffectiveRoleGrant:
    return EffectiveRoleGrant(
        tenant_id="tenant-a",
        grant_id="grant-1",
        subject_kind=GrantSubjectKind.HUMAN,
        subject_id="principal-1",
        role_id="reader",
        resource=ResourceReference(
            tenant_id="tenant-a",
            resource_type=ResourceType.DATASET,
            resource_id="eios:dataset:finance/revenue:v1",
            security_revision=1,
        ),
        valid_from=NOW,
        valid_until=NOW + timedelta(days=1),
        inherits=False,
        status=GrantStatus.ACTIVE,
        revision=1,
        tenant_status="active",
        resource_lifecycle=ResourceLifecycle.ACTIVE,
        evaluated_resource_security_revision=1,
    )


def test_group_directory_revalidates_model_construct_before_mutation() -> None:
    directory = GroupDirectory(tenant_id="tenant-a")
    forged = Group.model_construct(
        tenant_id="tenant-a",
        group_id="forged",
        display_name="Forged",
        revision=0,
    )

    with pytest.raises(GrantDomainError):
        directory.add_group(forged)

    assert directory.groups(PageRequest(limit=10)).items == ()


def test_group_directory_revalidates_multibyte_model_construct() -> None:
    directory = GroupDirectory(tenant_id="tenant-a")
    forged = Group.model_construct(
        tenant_id="tenant-a",
        group_id="forged-multibyte",
        display_name="界" * 86,
        revision=1,
    )

    with pytest.raises(GrantDomainError):
        directory.add_group(forged)


def test_group_directory_copies_inputs_and_every_group_output() -> None:
    directory = GroupDirectory(tenant_id="tenant-a")
    supplied = _group("finance")
    directory.add_group(supplied)

    object.__setattr__(supplied, "display_name", "forged-input")
    first = directory.groups(PageRequest(limit=10))
    assert first.items[0].display_name == "Group finance"

    object.__setattr__(first.items[0], "display_name", "forged-output")
    second = directory.groups(PageRequest(limit=10))
    assert second.items[0].display_name == "Group finance"
    assert second.items[0] is not first.items[0]


def test_group_directory_rejects_model_construct_page_request() -> None:
    directory = GroupDirectory(tenant_id="tenant-a")
    directory.add_group(_group("finance"))
    forged_page = PageRequest.model_construct(limit=257, cursor=None)

    with pytest.raises(GrantDomainError):
        directory.groups(forged_page)


def test_domain_authority_helpers_revalidate_forged_models() -> None:
    forged_role = _role()
    object.__setattr__(forged_role, "operations", {Operation.READ})
    with pytest.raises(GrantDomainError):
        effective_operations(forged_role, _grant(), at=NOW)

    forged_grant = _grant()
    object.__setattr__(forged_grant, "subject_kind", "human")
    with pytest.raises(GrantDomainError):
        effective_grants((forged_grant,), at=NOW)
