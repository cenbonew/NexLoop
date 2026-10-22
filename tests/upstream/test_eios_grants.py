from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given, strategies as st
import pytest
from pydantic import ValidationError

import eios.authz.grants as grant_models
from eios.authz.context import AuthorizationContext
from eios.authz.grants import (
    MAX_GROUP_NESTING_DEPTH,
    AttributeSchema,
    AttributeSource,
    AttributeValueType,
    CrossTenantReferenceError,
    GrantCycleError,
    GrantDomainError,
    GrantStatus,
    GrantSubjectKind,
    Group,
    GroupDirectory,
    GroupNestingDepthError,
    PageRequest,
    PendingResourceKey,
    Role,
    RoleGrant,
    SubjectAttribute,
    effective_grant_subject_kind,
    effective_grants,
    effective_operations,
    trusted_attribute,
)
from eios.authz.operations import Operation
from eios.authz.resources import ResourceLifecycle, ResourceReference, ResourceType


NOW = datetime(2026, 7, 20, 8, 0, tzinfo=UTC)
FROZEN_RESOURCE_TYPES = {
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
    "identity_user",
    "external_effect",
}
TASK7_RESOURCE_ID_CORPUS = (
    ("artifact", "123/report", None),
    ("dataset", "finance/revenue.v1", None),
    ("dataset", "finance/revenue", "v1"),
    ("function", "customer.score", "2026.07"),
    ("capability", "ontology.object.read", "1.2.0"),
    ("artifact", "report", "release~blue"),
    (
        "api_operation",
        "GET/api/objects/{type_name}/{object_id}~detail",
        None,
    ),
    ("api_operation", "GET/api/objects/{_private_id}", None),
    ("api_operation", "POST/api/actions/{action_name}~submit", "v2"),
)


def _group(group_id: str, *, tenant_id: str = "tenant-a") -> Group:
    return Group(
        tenant_id=tenant_id,
        group_id=group_id,
        display_name=f"Group {group_id}",
        revision=1,
    )


def _role(*, tenant_id: str = "tenant-a") -> Role:
    return Role(
        tenant_id=tenant_id,
        role_id="analyst",
        display_name="Analyst",
        operations=frozenset({Operation.DISCOVER, Operation.READ}),
        revision=1,
    )


def _resource(*, tenant_id: str = "tenant-a") -> ResourceReference:
    return ResourceReference(
        tenant_id=tenant_id,
        resource_type=ResourceType.DATASET,
        resource_id="eios:dataset:finance/revenue.v1",
        security_revision=1,
    )


def _grant(**changes: object) -> RoleGrant:
    fields: dict[str, object] = {
        "tenant_id": "tenant-a",
        "grant_id": "grant-1",
        "subject_kind": GrantSubjectKind.GROUP,
        "subject_id": "finance",
        "role_id": "analyst",
        "resource": _resource(),
        "valid_from": NOW,
        "valid_until": NOW + timedelta(days=30),
        "inherits": True,
        "status": GrantStatus.ACTIVE,
        "revision": 1,
    }
    fields.update(changes)
    return RoleGrant(**fields)


def _effective_grant(**changes: object) -> grant_models.EffectiveRoleGrant:
    fields = _grant().model_dump()
    fields.update(
        {
            "tenant_status": "active",
            "resource_lifecycle": ResourceLifecycle.ACTIVE,
            "evaluated_resource_security_revision": 1,
        }
    )
    fields.update(changes)
    return grant_models.EffectiveRoleGrant(**fields)


def _context(attributes: dict[str, object]) -> AuthorizationContext:
    return AuthorizationContext(
        tenant_id="tenant-a",
        session_id="session-1",
        subject_principal_id="principal-1",
        actor_principal_id="principal-1",
        application_id="portal",
        application_version="portal-v1",
        agent_id=None,
        credential_id=None,
        authentication_method="oidc",
        requested_scopes=frozenset({"ontology.read"}),
        groups=frozenset({"finance"}),
        roles=frozenset({"analyst"}),
        attributes=attributes,
        clearances=frozenset({"internal"}),
        membership_revision=1,
        grant_revision=1,
        clearance_revision=1,
        policy_revision=1,
        request_id="request-1",
        trace_id="trace-1",
    )


def test_group_nesting_depth_is_the_frozen_program_constant() -> None:
    assert MAX_GROUP_NESTING_DEPTH == 16
    assert hasattr(grant_models, "MAX_PAGE_SIZE")
    assert grant_models.MAX_PAGE_SIZE == 100


def test_grant_domain_byte_and_attribute_bounds_are_public_and_frozen() -> None:
    assert grant_models.MAX_IDENTIFIER_BYTES == 255
    assert grant_models.MAX_DISPLAY_NAME_BYTES == 255
    assert grant_models.MAX_ATTRIBUTE_JSON_BYTES == 16_384
    assert grant_models.MAX_ATTRIBUTE_STRING_BYTES == 255
    assert grant_models.MAX_ATTRIBUTE_STRING_SET_ITEMS == 100


def test_grant_subject_source_and_status_vocabularies_are_closed() -> None:
    assert {item.value for item in GrantSubjectKind} == {
        "human",
        "service",
        "agent",
        "group",
    }
    assert {item.value for item in AttributeSource} == {
        "oidc",
        "scim",
        "local_admin",
    }
    assert {item.value for item in GrantStatus} == {
        "active",
        "revoked",
    }


def test_effective_grant_subject_kind_is_an_exact_non_group_principal_kind() -> None:
    for subject_kind in (
        GrantSubjectKind.HUMAN,
        GrantSubjectKind.SERVICE,
        GrantSubjectKind.AGENT,
    ):
        assert effective_grant_subject_kind(subject_kind) is subject_kind
    for invalid_kind in (GrantSubjectKind.GROUP, "human", object()):
        with pytest.raises(GrantDomainError):
            effective_grant_subject_kind(invalid_kind)


def test_group_role_grant_attribute_contracts_are_strict_and_frozen() -> None:
    group = _group("finance")
    role = _role()
    grant = _grant()
    schema = AttributeSchema(
        tenant_id="tenant-a",
        name="region",
        value_type=AttributeValueType.STRING,
        source=AttributeSource.OIDC,
        sensitive=False,
        revision=1,
    )
    attribute = SubjectAttribute(
        tenant_id="tenant-a",
        subject_principal_id="principal-1",
        name="region",
        value_type=AttributeValueType.STRING,
        source=AttributeSource.OIDC,
        sensitive=False,
        value="east",
        schema_revision=1,
        valid_from=NOW,
        valid_until=None,
        revision=1,
    )

    for model in (group, role, grant, schema, attribute):
        with pytest.raises(ValidationError, match="frozen"):
            model.revision = 2
        with pytest.raises(ValidationError, match="extra_forbidden"):
            type(model)(**{**model.model_dump(), "actor_id": "caller-forged"})

    with pytest.raises(ValidationError, match="frozen_set_type"):
        Role(**{**role.model_dump(), "operations": [Operation.READ]})
    with pytest.raises(ValidationError, match="is_instance_of"):
        Role(**{**role.model_dump(), "operations": frozenset({"read"})})


def test_trusted_operator_and_group_membership_are_strict_database_contracts() -> None:
    assert hasattr(grant_models, "TrustedGrantOperator")
    assert hasattr(grant_models, "GroupMembership")
    operator = grant_models.TrustedGrantOperator(
        tenant_id="tenant-a",
        claimed_actor_principal_id="principal-admin",
        operator_principal_id="nex_eios_access_operator",
        request_id="request-grants-1",
        trace_id="trace-grants-1",
    )
    membership = grant_models.GroupMembership(
        tenant_id="tenant-a",
        group_id="finance",
        member_kind=GrantSubjectKind.HUMAN,
        member_id="principal-1",
        revision=1,
    )

    assert operator.claimed_actor_principal_id == "principal-admin"
    assert not hasattr(operator, "actor_principal_id")
    assert membership.member_kind is GrantSubjectKind.HUMAN
    for model in (operator, membership):
        with pytest.raises(ValidationError, match="frozen"):
            model.revision = 2
        with pytest.raises(ValidationError, match="extra_forbidden"):
            type(model)(**{**model.model_dump(), "trusted": True})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tenant_id", " tenant-a"),
        ("claimed_actor_principal_id", ""),
        ("operator_principal_id", "nex_eios_access_operator\x00"),
        ("request_id", "a" * 256),
        ("trace_id", "\u8d22\u52a1"),
    ],
)
def test_trusted_operator_rejects_noncanonical_or_unbounded_facts(
    field: str,
    value: str,
) -> None:
    fields = {
        "tenant_id": "tenant-a",
        "claimed_actor_principal_id": "principal-admin",
        "operator_principal_id": "nex_eios_access_operator",
        "request_id": "request-grants-1",
        "trace_id": "trace-grants-1",
    }
    fields[field] = value
    with pytest.raises(ValidationError, match=field):
        grant_models.TrustedGrantOperator(**fields)


def test_pending_resource_key_accepts_only_frozen_types_and_canonical_bounded_ids() -> (
    None
):
    assert {
        PendingResourceKey(
            tenant_id="tenant-a",
            resource_type=resource_type,
            resource_id=f"eios:{resource_type}:stable/{resource_type}.v1",
        ).resource_type
        for resource_type in FROZEN_RESOURCE_TYPES
    } == FROZEN_RESOURCE_TYPES

    route = PendingResourceKey(
        tenant_id="tenant-a",
        resource_type="api_operation",
        resource_id=(
            "eios:api_operation:GET/api/objects/{type_name}/{object_id}~detail"
        ),
    )
    assert route.resource_id.endswith("/{object_id}~detail")

    prefix = "eios:dataset:"
    maximum = PendingResourceKey(
        tenant_id="tenant-a",
        resource_type="dataset",
        resource_id=prefix + ("a" * (255 - len(prefix))),
    )
    assert len(maximum.resource_id) == 255

    with pytest.raises(ValidationError, match="resource_type"):
        PendingResourceKey(
            tenant_id="tenant-a",
            resource_type="ontology",
            resource_id="dataset-a",
        )


@pytest.mark.parametrize(
    ("tenant_id", "resource_type", "resource_id", "error"),
    [
        (
            " tenant-a",
            "dataset",
            "eios:dataset:finance/revenue.v1",
            "tenant_id",
        ),
        ("tenant-a", "dataset", "finance/revenue.v1", "exact canonical"),
        (
            "tenant-a",
            "dataset",
            "eios:project:finance/revenue.v1",
            "exact canonical",
        ),
        (
            "tenant-a",
            "dataset",
            "eios:dataset:dataset-a ",
            "canonical stable name",
        ),
        (
            "tenant-a",
            "dataset",
            "eios:dataset:dataset\x00a",
            "canonical stable name",
        ),
        (
            "tenant-a",
            "dataset",
            "eios:dataset:\u8d22\u52a1",
            "canonical stable name",
        ),
        (
            "tenant-a",
            "dataset",
            "eios:dataset:" + ("a" * (256 - len("eios:dataset:"))),
            "exact canonical",
        ),
        (
            "tenant-a",
            "api_operation",
            "eios:api_operation:GET/api/objects/{object_id",
            "unmatched route brace",
        ),
        (
            "tenant-a",
            "api_operation",
            "eios:api_operation:GET/api/objects/object_id}",
            "unmatched route brace",
        ),
        (
            "tenant-a",
            "api_operation",
            "eios:api_operation:GET/api/objects/{}",
            "invalid route parameter",
        ),
        (
            "tenant-a",
            "api_operation",
            "eios:api_operation:GET/api/objects/{{object_id}}",
            "invalid route parameter",
        ),
        (
            "tenant-a",
            "api_operation",
            "eios:api_operation:GET:/api/objects/{object_id}",
            "version",
        ),
        (
            "tenant-a",
            "dataset",
            "eios:dataset:finance/revenue:v1:alias",
            "at most one version separator",
        ),
        (
            "tenant-a",
            "dataset",
            "eios:dataset:finance/revenue:",
            "version must be non-empty",
        ),
        (
            "tenant-a",
            "dataset",
            "eios:dataset:finance%3Arevenue",
            "canonical stable name",
        ),
        (
            "tenant-a",
            "dataset",
            "eios:dataset:finance\uff1arevenue",
            "canonical stable name",
        ),
        (
            "tenant-a",
            "capability",
            "eios:capability:ontology.object.read:_v1",
            "version must start with an alphanumeric",
        ),
        (
            "tenant-a",
            "capability",
            "eios:capability:ontology.object.read:.v1",
            "version must start with an alphanumeric",
        ),
        (
            "tenant-a",
            "capability",
            "eios:capability:ontology.object.read:~v1",
            "version must start with an alphanumeric",
        ),
        (
            "tenant-a",
            "capability",
            "eios:capability:ontology.object.read:-v1",
            "version must start with an alphanumeric",
        ),
        (
            "tenant-a",
            "capability",
            "eios:capability:ontology.object.read:v1/beta",
            "version must be canonical",
        ),
        (
            "tenant-a",
            "capability",
            "eios:capability:ontology.object.read:{version}",
            "version must start with an alphanumeric",
        ),
        ("tenant-a", "dataset", "eios:dataset:/root", "canonical stable name"),
        ("tenant-a", "dataset", "eios:dataset:~blue", "canonical stable name"),
        ("tenant-a", "dataset", "eios:dataset:{id}", "canonical stable name"),
        (
            "tenant-a",
            "dataset",
            "eios:dataset:_private",
            "canonical stable name",
        ),
        ("tenant-a", "dataset", "eios:dataset:.hidden", "canonical stable name"),
        ("tenant-a", "dataset", "eios:dataset:-dash", "canonical stable name"),
    ],
    ids=(
        "tenant-whitespace",
        "bare-resource-id",
        "wrong-resource-prefix",
        "resource-whitespace",
        "resource-control-character",
        "resource-non-ascii",
        "resource-over-255",
        "route-open-brace",
        "route-close-brace",
        "route-empty-parameter",
        "route-nested-braces",
        "legacy-route-colon",
        "multiple-version-colons",
        "empty-version",
        "percent-colon-alias",
        "fullwidth-colon-alias",
        "version-leading-underscore",
        "version-leading-dot",
        "version-leading-tilde",
        "version-leading-hyphen",
        "version-slash",
        "version-braces",
        "stable-leading-slash",
        "stable-leading-tilde",
        "stable-leading-brace",
        "stable-leading-underscore",
        "stable-leading-dot",
        "stable-leading-hyphen",
    ),
)
def test_pending_resource_key_rejects_noncanonical_id_for_the_exact_type(
    tenant_id: str,
    resource_type: str,
    resource_id: str,
    error: str,
) -> None:
    with pytest.raises(ValidationError, match=error):
        PendingResourceKey(
            tenant_id=tenant_id,
            resource_type=resource_type,
            resource_id=resource_id,
        )


@pytest.mark.parametrize(
    ("resource_type", "stable", "version"),
    TASK7_RESOURCE_ID_CORPUS,
)
def test_pending_resource_key_round_trips_the_expected_task7_abi_corpus(
    resource_type: str,
    stable: str,
    version: str | None,
) -> None:
    suffix = stable if version is None else f"{stable}:{version}"
    expected = f"eios:{resource_type}:{suffix}"

    key = PendingResourceKey(
        tenant_id="tenant-a",
        resource_type=resource_type,
        resource_id=expected,
    )
    decoded_suffix = key.resource_id.removeprefix(f"eios:{resource_type}:")
    decoded_stable, separator, decoded_version = decoded_suffix.partition(":")

    assert key.resource_id == expected
    assert decoded_stable == stable
    assert decoded_version == ("" if version is None else version)
    assert bool(separator) is (version is not None)


_ABI_IDENTIFIER = st.from_regex(r"[A-Za-z][A-Za-z0-9_]{0,15}", fullmatch=True)
_ABI_STABLE_NAME = st.one_of(
    _ABI_IDENTIFIER,
    st.builds(
        lambda method, parameter: f"{method}/api/items/{{{parameter}}}~detail",
        st.sampled_from(("GET", "POST", "PATCH", "DELETE")),
        _ABI_IDENTIFIER,
    ),
)
_ABI_VERSION = st.one_of(st.none(), _ABI_IDENTIFIER)


@given(
    resource_type=st.sampled_from(tuple(sorted(FROZEN_RESOURCE_TYPES))),
    stable=_ABI_STABLE_NAME,
    version=_ABI_VERSION,
)
def test_pending_resource_key_property_matches_task7_build_and_parse(
    resource_type: str,
    stable: str,
    version: str | None,
) -> None:
    suffix = stable if version is None else f"{stable}:{version}"
    expected = f"eios:{resource_type}:{suffix}"

    key = PendingResourceKey(
        tenant_id="tenant-a",
        resource_type=resource_type,
        resource_id=expected,
    )
    decoded = key.resource_id.removeprefix(f"eios:{resource_type}:").split(":")

    expected_parts = [stable] if version is None else [stable, version]
    assert decoded == expected_parts


def test_role_grant_rejects_cross_tenant_resource_and_invalid_interval() -> None:
    with pytest.raises(ValidationError, match="same tenant"):
        _grant(resource=_resource(tenant_id="tenant-b"))
    with pytest.raises(ValidationError, match="valid_until"):
        _grant(valid_until=NOW)
    with pytest.raises(ValidationError, match="timezone-aware"):
        _grant(valid_from=NOW.replace(tzinfo=None))


def test_management_role_grant_cannot_reconstruct_authority() -> None:
    grant = _grant()

    assert not hasattr(grant, "is_effective_at")
    with pytest.raises(GrantDomainError):
        effective_grants((grant,), at=NOW + timedelta(days=1))
    with pytest.raises(GrantDomainError):
        effective_operations(_role(), grant, at=NOW + timedelta(days=1))


def test_trusted_effective_read_requires_active_current_resource_facts() -> None:
    grant = _effective_grant()

    assert grant.is_effective_at(NOW + timedelta(days=1)) is True
    assert effective_grants((grant,), at=NOW + timedelta(days=1)) == (grant,)
    assert effective_operations(_role(), grant, at=NOW + timedelta(days=1)) == (
        _role().operations
    )
    for change in (
        {"tenant_status": "disabled"},
        {"resource_lifecycle": ResourceLifecycle.DISABLED},
        {"evaluated_resource_security_revision": 2},
    ):
        with pytest.raises(ValidationError):
            _effective_grant(**change)


def test_inherited_evaluation_excludes_non_inheriting_parent_grants() -> None:
    direct_only = _effective_grant(inherits=False)
    inheriting = _effective_grant(grant_id="grant-inheriting", inherits=True)

    assert effective_grants(
        (direct_only, inheriting), at=NOW + timedelta(days=1), inherited=False
    ) == (direct_only, inheriting)
    assert effective_grants(
        (direct_only, inheriting), at=NOW + timedelta(days=1), inherited=True
    ) == (inheriting,)


def test_active_resource_grant_contributes_authority_everywhere() -> None:
    role = _role()
    grant = _effective_grant()

    assert grant.is_effective_at(NOW + timedelta(days=1)) is True
    assert effective_grants((grant,), at=NOW + timedelta(days=1)) == (grant,)
    assert effective_grants((grant,), at=NOW + timedelta(days=1), inherited=True) == (
        grant,
    )
    assert effective_grants((grant,), at=NOW + timedelta(days=1), cache_input=True) == (
        grant,
    )
    assert effective_operations(role, grant, at=NOW + timedelta(days=1)) == (
        role.operations
    )


def test_task7_active_grant_uses_canonical_resource_and_validity_interval() -> None:
    resource = ResourceReference(
        tenant_id="tenant-a",
        resource_type=ResourceType.DATASET,
        resource_id="eios:dataset:finance/revenue.v1",
        security_revision=7,
    )
    grant = grant_models.EffectiveRoleGrant(
        tenant_id="tenant-a",
        grant_id="grant-active",
        subject_kind=GrantSubjectKind.HUMAN,
        subject_id="principal-1",
        role_id="analyst",
        resource=resource,
        valid_from=NOW,
        valid_until=NOW + timedelta(days=1),
        inherits=False,
        status=GrantStatus.ACTIVE,
        revision=2,
        tenant_status="active",
        resource_lifecycle=ResourceLifecycle.ACTIVE,
        evaluated_resource_security_revision=7,
    )

    assert grant.resource == resource
    assert grant.is_effective_at(NOW + timedelta(hours=1)) is True
    assert grant.is_effective_at(NOW - timedelta(seconds=1)) is False
    assert grant.is_effective_at(NOW + timedelta(days=1)) is False
    assert effective_grants((grant,), at=NOW + timedelta(hours=1)) == (grant,)
    assert effective_operations(_role(), grant, at=NOW) == _role().operations


def test_revoked_management_grant_cannot_contribute_authority() -> None:
    grant = _grant(status=GrantStatus.REVOKED)

    assert not hasattr(grant, "is_effective_at")
    with pytest.raises(GrantDomainError):
        effective_grants((grant,), at=NOW + timedelta(days=1))
    with pytest.raises(GrantDomainError):
        effective_operations(_role(), grant, at=NOW + timedelta(days=1))


def test_effective_operation_validation_rejects_cross_tenant_or_wrong_role() -> None:
    with pytest.raises(CrossTenantReferenceError):
        effective_operations(_role(tenant_id="tenant-b"), _effective_grant(), at=NOW)
    with pytest.raises(ValueError, match="role_id"):
        effective_operations(
            Role(
                **{
                    **_role().model_dump(),
                    "role_id": "operator",
                }
            ),
            _effective_grant(),
            at=NOW,
        )


def test_nested_group_cycle_and_self_membership_are_rejected_atomically() -> None:
    directory = GroupDirectory(tenant_id="tenant-a")
    for group_id in ("group-a", "group-b", "group-c"):
        directory.add_group(_group(group_id))

    directory.add_group_member("group-a", "group-b")
    directory.add_group_member("group-b", "group-c")

    with pytest.raises(GrantCycleError):
        directory.add_group_member("group-c", "group-a")
    with pytest.raises(GrantCycleError):
        directory.add_group_member("group-a", "group-a")

    assert directory.group_members("group-c", PageRequest(limit=10)).items == ()


def test_group_nesting_above_sixteen_fails_closed_without_truncation() -> None:
    directory = GroupDirectory(tenant_id="tenant-a")
    group_ids = [f"group-{index:02d}" for index in range(18)]
    for group_id in group_ids:
        directory.add_group(_group(group_id))
    for parent, member in zip(group_ids[:16], group_ids[1:17], strict=True):
        directory.add_group_member(parent, member)

    with pytest.raises(GroupNestingDepthError):
        directory.add_group_member(group_ids[16], group_ids[17])

    assert directory.group_members(group_ids[16], PageRequest(limit=10)).items == ()


def test_group_directory_rejects_cross_tenant_and_unknown_references() -> None:
    directory = GroupDirectory(tenant_id="tenant-a")
    directory.add_group(_group("group-a"))

    with pytest.raises(AttributeError):
        directory.tenant_id = "tenant-b"
    with pytest.raises(CrossTenantReferenceError):
        directory.add_group(_group("group-b", tenant_id="tenant-b"))
    with pytest.raises(ValueError, match="unknown group"):
        directory.add_group_member("group-a", "group-missing")


def test_group_directory_removal_immediately_narrows_nested_reachability() -> None:
    directory = GroupDirectory(tenant_id="tenant-a")
    for group_id in ("parent", "child", "grandchild"):
        directory.add_group(_group(group_id))
    directory.add_group_member("parent", "child")
    directory.add_group_member("child", "grandchild")

    assert directory._reachable("parent", "grandchild")
    directory.remove_group_member("parent", "child")

    assert not directory._reachable("parent", "grandchild")
    assert (
        tuple(
            group.group_id
            for group in directory.group_members("parent", PageRequest(limit=100)).items
        )
        == ()
    )
    with pytest.raises(ValueError, match="unknown group membership"):
        directory.remove_group_member("parent", "child")


def test_group_collections_paginate_without_a_total_cardinality_cap() -> None:
    directory = GroupDirectory(tenant_id="tenant-a")
    expected = tuple(f"group-{index:03d}" for index in range(257))
    for group_id in reversed(expected):
        directory.add_group(_group(group_id))

    observed: list[str] = []
    cursor: str | None = None
    while True:
        page = directory.groups(PageRequest(limit=17, cursor=cursor))
        observed.extend(group.group_id for group in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert tuple(observed) == expected
    assert len(observed) == 257


def test_group_pagination_uses_a_stable_keyset_cursor() -> None:
    directory = GroupDirectory(tenant_id="tenant-a")
    directory.add_group(_group("group-a"))
    directory.add_group(_group("group-c"))

    first = directory.groups(PageRequest(limit=1))
    assert tuple(group.group_id for group in first.items) == ("group-a",)
    assert first.next_cursor == "group-a"

    directory.add_group(_group("group-000"))
    second = directory.groups(PageRequest(limit=2, cursor=first.next_cursor))
    assert tuple(group.group_id for group in second.items) == ("group-c",)
    assert second.next_cursor is None


def test_page_request_rejects_invalid_or_noncanonical_cursors() -> None:
    with pytest.raises(ValidationError, match="greater_than"):
        PageRequest(limit=0)
    for cursor in ("", "-1", " offset", "group\x00a", "\u8d22\u52a1", "a" * 256):
        with pytest.raises(ValidationError, match="cursor"):
            PageRequest(limit=1, cursor=cursor)
    with pytest.raises(ValidationError, match="less_than_equal"):
        PageRequest(limit=grant_models.MAX_PAGE_SIZE + 1)
    with pytest.raises(ValidationError, match="int_type"):
        PageRequest(limit=True)
    assert PageRequest(limit=1, cursor=f"group:{'a' * 255}").cursor == (
        f"group:{'a' * 255}"
    )
    assert PageRequest(limit=1, cursor="group:").cursor == "group:"
    with pytest.raises(ValidationError, match="cursor"):
        PageRequest(limit=1, cursor=f"group:{'a' * 256}")


def test_sensitive_subject_attribute_repr_never_contains_the_raw_value() -> None:
    attribute = SubjectAttribute(
        tenant_id="tenant-a",
        subject_principal_id="principal-1",
        name="sensitive.fact",
        value_type=AttributeValueType.STRING,
        source=AttributeSource.LOCAL_ADMIN,
        sensitive=True,
        value="do-not-log-this-value",
        schema_revision=1,
        valid_from=NOW,
        valid_until=None,
        revision=1,
    )

    assert "do-not-log-this-value" not in repr(attribute)


def test_sensitive_subject_attribute_default_serialization_is_redacted() -> None:
    raw_value = "do-not-serialize-this-value"
    attribute = SubjectAttribute(
        tenant_id="tenant-a",
        subject_principal_id="principal-1",
        name="sensitive.fact",
        value_type=AttributeValueType.STRING,
        source=AttributeSource.LOCAL_ADMIN,
        sensitive=True,
        value=raw_value,
        schema_revision=1,
        valid_from=NOW,
        valid_until=None,
        revision=1,
    )

    dumped = attribute.model_dump()
    dumped_json = attribute.model_dump_json()

    assert raw_value not in str(dumped)
    assert raw_value not in dumped_json
    assert dumped["value"] == grant_models.SENSITIVE_ATTRIBUTE_REDACTION
    assert attribute.value == raw_value


def test_non_sensitive_subject_attribute_default_serialization_keeps_value() -> None:
    attribute = SubjectAttribute(
        tenant_id="tenant-a",
        subject_principal_id="principal-1",
        name="public.fact",
        value_type=AttributeValueType.STRING,
        source=AttributeSource.LOCAL_ADMIN,
        sensitive=False,
        value="safe-to-serialize",
        schema_revision=1,
        valid_from=NOW,
        valid_until=None,
        revision=1,
    )

    assert attribute.model_dump()["value"] == "safe-to-serialize"
    assert "safe-to-serialize" in attribute.model_dump_json()


@pytest.mark.parametrize(
    ("value_type", "value"),
    [
        (AttributeValueType.STRING, "east"),
        (AttributeValueType.INTEGER, 3),
        (AttributeValueType.NUMBER, 3.5),
        (AttributeValueType.BOOLEAN, True),
        (AttributeValueType.STRING_SET, frozenset({"cn", "sg"})),
    ],
)
def test_subject_attribute_enforces_registered_value_type(
    value_type: AttributeValueType,
    value: object,
) -> None:
    attribute = SubjectAttribute(
        tenant_id="tenant-a",
        subject_principal_id="principal-1",
        name="region",
        value_type=value_type,
        source=AttributeSource.SCIM,
        sensitive=True,
        value=value,
        schema_revision=2,
        valid_from=NOW,
        valid_until=NOW + timedelta(days=1),
        revision=3,
    )

    assert attribute.value == value


def test_subject_attribute_rejects_wrong_type_source_or_validity() -> None:
    fields: dict[str, object] = {
        "tenant_id": "tenant-a",
        "subject_principal_id": "principal-1",
        "name": "region",
        "value_type": AttributeValueType.INTEGER,
        "source": AttributeSource.LOCAL_ADMIN,
        "sensitive": True,
        "value": 3,
        "schema_revision": 1,
        "valid_from": NOW,
        "valid_until": None,
        "revision": 1,
    }
    with pytest.raises(ValidationError, match="value_type"):
        SubjectAttribute(**{**fields, "value": True})
    with pytest.raises(ValidationError, match="valid_until"):
        SubjectAttribute(**{**fields, "valid_until": NOW})
    with pytest.raises(ValidationError, match="is_instance_of"):
        SubjectAttribute(**{**fields, "source": "request"})


def test_display_and_attribute_strings_use_utf8_byte_limits() -> None:
    accepted = "界" * 85
    rejected = "界" * 86

    assert len(accepted) < 255
    assert len(accepted.encode("utf-8")) == 255
    assert len(rejected) < 255
    assert len(rejected.encode("utf-8")) == 258
    assert (
        Group(
            tenant_id="tenant-a",
            group_id="multibyte",
            display_name=accepted,
            revision=1,
        ).display_name
        == accepted
    )
    with pytest.raises(ValidationError, match="display_name"):
        Group(
            tenant_id="tenant-a",
            group_id="multibyte-overflow",
            display_name=rejected,
            revision=1,
        )

    common = {
        "tenant_id": "tenant-a",
        "subject_principal_id": "principal-1",
        "name": "region",
        "source": AttributeSource.LOCAL_ADMIN,
        "sensitive": True,
        "schema_revision": 1,
        "valid_from": NOW,
        "valid_until": None,
        "revision": 1,
    }
    assert (
        SubjectAttribute(
            **common,
            value_type=AttributeValueType.STRING,
            value=accepted,
        ).value
        == accepted
    )
    with pytest.raises(ValidationError, match="value"):
        SubjectAttribute(
            **common,
            value_type=AttributeValueType.STRING,
            value=rejected,
        )
    with pytest.raises(ValidationError, match="value"):
        SubjectAttribute(
            **common,
            value_type=AttributeValueType.STRING_SET,
            value=frozenset({rejected}),
        )


def test_string_set_count_and_canonical_json_are_bounded() -> None:
    common = {
        "tenant_id": "tenant-a",
        "subject_principal_id": "principal-1",
        "name": "segments",
        "value_type": AttributeValueType.STRING_SET,
        "source": AttributeSource.LOCAL_ADMIN,
        "sensitive": True,
        "schema_revision": 1,
        "valid_from": NOW,
        "valid_until": None,
        "revision": 1,
    }
    one_hundred = frozenset(f"segment-{index:03d}" for index in range(100))
    assert SubjectAttribute(**common, value=one_hundred).value == one_hundred

    with pytest.raises(ValidationError, match="value"):
        SubjectAttribute(
            **common,
            value=frozenset(f"segment-{index:03d}" for index in range(101)),
        )

    within_item_limit_but_json_too_large = frozenset(
        f"{index:03d}-{'a' * 160}" for index in range(100)
    )
    assert all(
        len(item.encode("utf-8")) <= grant_models.MAX_ATTRIBUTE_STRING_BYTES
        for item in within_item_limit_but_json_too_large
    )
    with pytest.raises(ValidationError, match="value"):
        SubjectAttribute(**common, value=within_item_limit_but_json_too_large)


def test_request_cannot_supply_or_override_trusted_subject_attribute() -> None:
    context = _context({"region": "east", "clearance": "internal"})
    request = {
        "subject": {
            "attributes": {
                "region": "west",
                "clearance": "secret",
                "forged": "yes",
            }
        }
    }

    assert trusted_attribute(context, request, "region") == "east"
    assert trusted_attribute(context, request, "clearance") == "internal"
    assert trusted_attribute(context, request, "forged") is None


def test_trusted_attribute_revalidates_context_and_copies_nested_output() -> None:
    context = _context({"profile": {"region": "east"}})

    returned = trusted_attribute(context, {}, "profile")

    assert returned is not context.attributes["profile"]
    assert returned == {"region": "east"}
    assert isinstance(returned, dict)
    returned["region"] = "forged"
    assert trusted_attribute(context, {}, "profile") == {"region": "east"}

    object.__setattr__(context, "membership_revision", 0)
    with pytest.raises(grant_models.GrantDomainError):
        trusted_attribute(context, {}, "profile")


def test_schema_and_subject_attribute_binding_must_match_exactly() -> None:
    schema = AttributeSchema(
        tenant_id="tenant-a",
        name="region",
        value_type=AttributeValueType.STRING,
        source=AttributeSource.OIDC,
        sensitive=True,
        revision=2,
    )
    attribute = SubjectAttribute(
        tenant_id="tenant-a",
        subject_principal_id="principal-1",
        name="region",
        value_type=AttributeValueType.STRING,
        source=AttributeSource.OIDC,
        sensitive=True,
        value="east",
        schema_revision=2,
        valid_from=NOW,
        valid_until=None,
        revision=1,
    )

    schema.assert_matches(attribute)
    with pytest.raises(ValueError, match="schema"):
        schema.assert_matches(
            SubjectAttribute(
                **{
                    **attribute.model_dump(),
                    "source": AttributeSource.LOCAL_ADMIN,
                }
            )
        )
