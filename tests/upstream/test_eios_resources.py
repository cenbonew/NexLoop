from __future__ import annotations

import base64
from collections.abc import Iterable
import hmac
import json

from hypothesis import given, settings, strategies as st
import pytest
from pydantic import ValidationError

from eios.authz.resources import (
    MAX_RESOURCE_DEPENDENCY_DEPTH,
    MAX_RESOURCE_PARENT_DEPTH,
    CrossTenantResourceError,
    ResourceCursorError,
    ResourceCycleError,
    ResourceDepthError,
    ResourceError,
    ResourceNotFoundError,
    ResourcePageRequest,
    ResourceReference,
    ResourceRegistry,
    ResourceRevisionMismatchError,
    ResourceType,
    ResourceVisibilityError,
    resource_id,
)


FROZEN_RESOURCE_TYPES = (
    "tenant",
    "project",
    "folder",
    "dataset",
    "connector",
    "object",
    "property",
    "relation",
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
)
CURSOR_SIGNING_KEY = b"task7-test-cursor-signing-key-32-bytes-minimum"
TASK6_TASK7_RESOURCE_ID_CORPUS = (
    (ResourceType.ARTIFACT, "123/report", None),
    (ResourceType.DATASET, "finance/revenue.v1", None),
    (ResourceType.DATASET, "finance/revenue", "v1"),
    (ResourceType.FUNCTION, "customer.score", "2026.07"),
    (ResourceType.CAPABILITY, "ontology.object.read", "1.2.0"),
    (ResourceType.ARTIFACT, "report", "release~blue"),
    (
        ResourceType.API_OPERATION,
        "GET/api/objects/{type_name}/{object_id}~detail",
        None,
    ),
    (ResourceType.API_OPERATION, "GET/api/objects/{_private_id}", None),
    (
        ResourceType.API_OPERATION,
        "POST/api/actions/{action_name}~submit",
        "v2",
    ),
)
TASK6_TASK7_REJECTED_RESOURCE_ID_CORPUS = (
    (ResourceType.DATASET, ".private", None),
    (ResourceType.DATASET, "_private", None),
    (ResourceType.DATASET, "~alias", None),
    (ResourceType.DATASET, "-dash", None),
    (ResourceType.DATASET, "/root", None),
    (ResourceType.API_OPERATION, "{root_id}/detail", None),
    (ResourceType.DATASET, "finance:revenue", None),
    (ResourceType.API_OPERATION, "GET/api/{}", None),
    (ResourceType.API_OPERATION, "GET/api/{outer{inner}}", None),
    (ResourceType.CAPABILITY, "ontology.object.read", "_v1"),
    (ResourceType.CAPABILITY, "ontology.object.read", ".v1"),
    (ResourceType.CAPABILITY, "ontology.object.read", "~v1"),
    (ResourceType.CAPABILITY, "ontology.object.read", "-v1"),
    (ResourceType.CAPABILITY, "ontology.object.read", "v1/beta"),
    (ResourceType.CAPABILITY, "ontology.object.read", "{version}"),
    (ResourceType.CAPABILITY, "ontology.object.read", "v1:alias"),
)
SCALING_WIDTHS = (250, 500, 1_000, 2_000, 4_000)


def new_registry() -> ResourceRegistry:
    return ResourceRegistry(cursor_signing_key=CURSOR_SIGNING_KEY)


def test_resource_registry_doc_matches_the_delivered_migration_boundary() -> None:
    documentation = " ".join((ResourceRegistry.__doc__ or "").split())
    assert "Migration 0037 persists the resource graph" in documentation
    assert "0038 and 0039 are not delivered" in documentation
    assert "Future migration 0052" not in documentation


def reference(
    stable_name: str,
    *,
    kind: ResourceType = ResourceType.DATASET,
    tenant_id: str = "tenant-a",
    security_revision: int = 1,
) -> ResourceReference:
    return ResourceReference(
        tenant_id=tenant_id,
        resource_id=resource_id(kind, stable_name),
        resource_type=kind,
        security_revision=security_revision,
    )


def register_chain(
    registry: ResourceRegistry,
    *,
    prefix: str,
    edge_count: int,
    dependency: bool,
) -> tuple[ResourceReference, ...]:
    nodes = tuple(reference(f"{prefix}-{index}") for index in range(edge_count + 1))
    for node in nodes:
        registry.register(node)
    for parent, child in zip(nodes, nodes[1:]):
        if dependency:
            registry.bind_dependency(parent, child)
        else:
            registry.reparent(child, parent=parent)
    return nodes


def collect_pages(
    registry: ResourceRegistry,
    *,
    tenant_id: str = "tenant-a",
    limit: int = 17,
    resource_type: ResourceType | None = None,
) -> tuple[ResourceReference, ...]:
    cursor: str | None = None
    collected: list[ResourceReference] = []
    while True:
        page = registry.list_resources(
            tenant_id=tenant_id,
            resource_type=resource_type,
            page=ResourcePageRequest(limit=limit, cursor=cursor),
        )
        collected.extend(page.items)
        if page.next_cursor is None:
            return tuple(collected)
        cursor = page.next_cursor


def test_resource_type_is_the_exact_frozen_twenty_two_value_vocabulary() -> None:
    assert len(ResourceType) == 22
    assert tuple(item.value for item in ResourceType) == FROZEN_RESOURCE_TYPES
    assert {item.value for item in ResourceType} == set(FROZEN_RESOURCE_TYPES)


def test_resource_reference_is_strict_frozen_extra_forbid_and_hashable() -> None:
    item = reference("revenue")

    assert {item: "visible"}[item] == "visible"
    with pytest.raises(ValidationError, match="frozen"):
        item.security_revision = 2
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ResourceReference(**{**item.model_dump(), "owner": "human-a"})
    with pytest.raises(ValidationError, match="is_instance_of"):
        ResourceReference(**{**item.model_dump(), "resource_type": "dataset"})
    with pytest.raises(ValidationError, match="int_type"):
        ResourceReference(**{**item.model_dump(), "security_revision": True})


@pytest.mark.parametrize(
    ("kind", "stable_name", "version", "expected"),
    (
        (
            ResourceType.API_OPERATION,
            "GET/api/definitions/{kind}/{name}~route",
            None,
            "eios:api_operation:GET/api/definitions/{kind}/{name}~route",
        ),
        (
            ResourceType.CAPABILITY,
            "ontology.object.get",
            "1.2.0",
            "eios:capability:ontology.object.get:1.2.0",
        ),
        (ResourceType.OBJECT_TYPE, "Account", 7, "eios:object_type:Account:7"),
    ),
)
def test_resource_id_has_one_canonical_versioned_form(
    kind: ResourceType,
    stable_name: str,
    version: str | int | None,
    expected: str,
) -> None:
    assert resource_id(kind, stable_name, version) == expected


@pytest.mark.parametrize(
    ("kind", "stable_name", "version"),
    (
        ("dataset", "revenue", None),
        (ResourceType.DATASET, "", None),
        (ResourceType.DATASET, " revenue", None),
        (ResourceType.DATASET, "revenue\x00", None),
        (ResourceType.DATASET, "\u8d22\u52a1", None),
        (ResourceType.DATASET, "revenue", " 1"),
        (ResourceType.DATASET, "revenue", True),
        (ResourceType.DATASET, "revenue", -1),
        (ResourceType.DATASET, "revenue:current", None),
        (ResourceType.DATASET, "revenue", "v1:beta"),
        (ResourceType.API_OPERATION, "GET/api/{}", None),
        (ResourceType.API_OPERATION, "GET/api/{outer{inner}}", None),
        (ResourceType.API_OPERATION, "GET/api/{missing", None),
        (ResourceType.API_OPERATION, "GET/api/missing}", None),
        (ResourceType.DATASET, "x" * 256, None),
    ),
)
def test_resource_id_rejects_noncanonical_or_unbounded_parts(
    kind: object,
    stable_name: str,
    version: object,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        resource_id(kind, stable_name, version)  # type: ignore[arg-type]


def test_resource_reference_requires_its_exact_type_prefix_and_bounded_ids() -> None:
    item = reference("revenue")

    for field, value in (
        ("tenant_id", " tenant-a"),
        ("tenant_id", "tenant\x00a"),
        ("tenant_id", "a" * 256),
        ("resource_id", "revenue"),
        ("resource_id", "eios:project:revenue"),
        ("resource_id", "eios:dataset:revenue "),
        ("resource_id", "eios:api_operation:GET/api/{}"),
        ("resource_id", "eios:api_operation:GET/api/{outer{inner}}"),
        ("resource_id", "eios:dataset:revenue:v1:duplicate"),
    ):
        with pytest.raises(ValidationError, match=field):
            ResourceReference(
                **{
                    **item.model_dump(),
                    field: value,
                }
            )


def test_resource_id_cannot_alias_stable_names_and_versions() -> None:
    with pytest.raises(ValueError, match="stable_name"):
        resource_id(ResourceType.DATASET, "revenue:v1")
    with pytest.raises(ValueError, match="version"):
        resource_id(ResourceType.DATASET, "revenue", "v1:beta")

    assert resource_id(ResourceType.DATASET, "revenue", "v1") != resource_id(
        ResourceType.DATASET,
        "revenue-v1",
    )


@pytest.mark.parametrize(
    ("kind", "stable_name", "version"),
    TASK6_TASK7_RESOURCE_ID_CORPUS,
)
def test_resource_id_matches_the_frozen_task6_corpus_in_both_directions(
    kind: ResourceType,
    stable_name: str,
    version: str | None,
) -> None:
    identifier = resource_id(kind, stable_name, version)
    item = ResourceReference(
        tenant_id="tenant-a",
        resource_id=identifier,
        resource_type=kind,
        security_revision=1,
    )
    suffix = item.resource_id.removeprefix(f"eios:{kind.value}:")
    decoded_stable, separator, decoded_version = suffix.partition(":")

    assert decoded_stable == stable_name
    assert decoded_version == ("" if version is None else version)
    assert bool(separator) is (version is not None)


@pytest.mark.parametrize(
    ("kind", "stable_name", "version"),
    TASK6_TASK7_REJECTED_RESOURCE_ID_CORPUS,
)
def test_resource_id_rejects_the_frozen_task6_invalid_corpus(
    kind: ResourceType,
    stable_name: str,
    version: str | None,
) -> None:
    with pytest.raises(ValueError):
        resource_id(kind, stable_name, version)


@given(
    kind=st.sampled_from(tuple(ResourceType)),
    stable_name=st.from_regex(r"[A-Za-z0-9][A-Za-z0-9._/~\-]{0,40}", fullmatch=True),
    version=st.one_of(
        st.none(),
        st.from_regex(r"[A-Za-z0-9][A-Za-z0-9._~\-]{0,15}", fullmatch=True),
        st.integers(min_value=0, max_value=9999),
    ),
)
@settings(max_examples=100, deadline=None)
def test_resource_id_and_reference_are_bidirectionally_canonical(
    kind: ResourceType,
    stable_name: str,
    version: str | int | None,
) -> None:
    identifier = resource_id(kind, stable_name, version)
    item = ResourceReference(
        tenant_id="tenant-a",
        resource_id=identifier,
        resource_type=kind,
        security_revision=1,
    )
    suffix = item.resource_id.removeprefix(f"eios:{kind.value}:")
    parsed_stable, separator, parsed_version = suffix.partition(":")

    assert (
        resource_id(
            kind,
            parsed_stable,
            parsed_version if separator else None,
        )
        == identifier
    )


@given(
    placeholder=st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,20}", fullmatch=True),
    tail=st.from_regex(r"[A-Za-z0-9][A-Za-z0-9._/~\-]{0,20}", fullmatch=True),
)
@settings(max_examples=60, deadline=None)
def test_legal_route_placeholders_and_tilde_round_trip(
    placeholder: str,
    tail: str,
) -> None:
    stable_name = f"GET/api/{{{placeholder}}}/{tail}~route"
    identifier = resource_id(ResourceType.API_OPERATION, stable_name)

    assert (
        ResourceReference(
            tenant_id="tenant-a",
            resource_id=identifier,
            resource_type=ResourceType.API_OPERATION,
            security_revision=1,
        ).resource_id
        == identifier
    )


def test_registry_requires_a_bounded_secret_that_is_never_repr_exposed() -> None:
    with pytest.raises(TypeError, match="cursor_signing_key"):
        ResourceRegistry()
    with pytest.raises(ValueError, match="32"):
        ResourceRegistry(cursor_signing_key=b"short")
    with pytest.raises(TypeError, match="bytes"):
        ResourceRegistry(cursor_signing_key="x" * 32)  # type: ignore[arg-type]

    created = new_registry()
    rendered = repr((created, vars(created)))
    assert CURSOR_SIGNING_KEY.hex() not in rendered
    assert CURSOR_SIGNING_KEY.decode("ascii") not in rendered


def test_registry_revalidates_forged_references_before_index_mutation() -> None:
    valid = reference("forged")
    forged_values = (
        ResourceReference.model_construct(
            **{**valid.model_dump(), "resource_id": "not-canonical"}
        ),
        ResourceReference.model_construct(
            **{**valid.model_dump(), "resource_type": "dataset"}
        ),
        ResourceReference.model_construct(
            **{**valid.model_dump(), "security_revision": True}
        ),
    )
    forged_extra = ResourceReference.model_construct(**valid.model_dump())
    object.__setattr__(forged_extra, "poison", "present")
    forged_private = ResourceReference.model_construct(**valid.model_dump())
    object.__setattr__(forged_private, "__pydantic_private__", {"poison": True})

    for forged in (*forged_values, forged_extra, forged_private):
        registry = new_registry()
        with pytest.raises(ResourceError, match="reference is invalid"):
            registry.register(forged)
        assert collect_pages(registry) == ()


def test_failed_forged_edge_mutation_preserves_registry_and_indexes() -> None:
    registry = new_registry()
    root = reference("root")
    child = reference("child")
    registry.register(root)
    registry.register(child)
    poisoned = ResourceReference.model_construct(**child.model_dump())
    object.__setattr__(poisoned, "operation", "read")

    with pytest.raises(ResourceError, match="reference is invalid"):
        registry.reparent(poisoned, parent=root)
    with pytest.raises(ResourceError, match="reference is invalid"):
        registry.bind_dependency(root, poisoned)

    assert registry.parent_of(child) is None
    assert registry.dependencies_of(root) == ()
    assert collect_pages(registry) == (child, root)


def test_every_reference_and_visible_registry_entry_revalidates_forged_models() -> None:
    registry = new_registry()
    root = reference("root")
    dependency = reference("dependency")
    registry.register(root)
    registry.register(dependency)
    registry.bind_dependency(root, dependency)
    poisoned = ResourceReference.model_construct(**root.model_dump())
    object.__setattr__(poisoned, "poison", "present")

    calls = (
        lambda: registry.parent_of(poisoned),
        lambda: registry.dependencies_of(poisoned),
        lambda: registry.ancestors(poisoned),
        lambda: registry.dependency_closure(poisoned),
        lambda: registry.public_projection(poisoned, visible={root}),
        lambda: registry.public_projection(root, visible=(root, poisoned)),
    )
    for call in calls:
        with pytest.raises(ResourceError):
            call()

    assert registry.dependencies_of(root) == (dependency,)
    assert collect_pages(registry) == (dependency, root)


def test_parent_and_dependency_edges_reject_cross_tenant_references_atomically() -> (
    None
):
    registry = new_registry()
    tenant_a = reference("a")
    tenant_b = reference("b", tenant_id="tenant-b")
    registry.register(tenant_a)
    registry.register(tenant_b)

    with pytest.raises(CrossTenantResourceError):
        registry.reparent(tenant_a, parent=tenant_b)
    with pytest.raises(CrossTenantResourceError):
        registry.bind_dependency(tenant_a, tenant_b)

    assert registry.parent_of(tenant_a) is None
    assert registry.dependencies_of(tenant_a) == ()


def test_parent_and_dependency_cycles_are_rejected_without_partial_mutation() -> None:
    registry = new_registry()
    project = reference("project", kind=ResourceType.PROJECT)
    folder = reference("folder", kind=ResourceType.FOLDER)
    function = reference("function", kind=ResourceType.FUNCTION)
    dataset = reference("dataset")
    for item in (project, folder, function, dataset):
        registry.register(item)
    registry.reparent(folder, parent=project)
    registry.bind_dependency(function, dataset)

    with pytest.raises(ResourceCycleError, match="parent"):
        registry.reparent(project, parent=folder)
    with pytest.raises(ResourceCycleError, match="dependency"):
        registry.bind_dependency(dataset, function)
    with pytest.raises(ResourceCycleError, match="dependency"):
        registry.bind_dependency(function, function)

    assert registry.parent_of(project) is None
    assert registry.parent_of(folder) == project
    assert registry.dependencies_of(function) == (dataset,)
    assert registry.dependencies_of(dataset) == ()


@given(edge_count=st.integers(min_value=0, max_value=35))
@settings(max_examples=36, deadline=None)
def test_parent_depth_is_exactly_bounded_and_fails_closed(edge_count: int) -> None:
    registry = new_registry()

    if edge_count <= MAX_RESOURCE_PARENT_DEPTH:
        nodes = register_chain(
            registry,
            prefix="parent",
            edge_count=edge_count,
            dependency=False,
        )
        assert len(registry.ancestors(nodes[-1])) == edge_count
    else:
        with pytest.raises(ResourceDepthError, match="parent"):
            register_chain(
                registry,
                prefix="parent",
                edge_count=edge_count,
                dependency=False,
            )
        assert (
            max((len(registry.ancestors(item)) for item in collect_pages(registry)))
            == 32
        )


@given(edge_count=st.integers(min_value=0, max_value=35))
@settings(max_examples=36, deadline=None)
def test_dependency_depth_is_exactly_bounded_and_fails_closed(edge_count: int) -> None:
    registry = new_registry()

    if edge_count <= MAX_RESOURCE_DEPENDENCY_DEPTH:
        nodes = register_chain(
            registry,
            prefix="dependency",
            edge_count=edge_count,
            dependency=True,
        )
        assert len(registry.dependency_closure(nodes[0])) == edge_count
    else:
        with pytest.raises(ResourceDepthError, match="dependency"):
            register_chain(
                registry,
                prefix="dependency",
                edge_count=edge_count,
                dependency=True,
            )
        roots = collect_pages(registry)
        assert max(len(registry.dependency_closure(item)) for item in roots) == 32


def test_reparent_validates_the_depth_of_every_affected_descendant() -> None:
    registry = new_registry()
    deep = register_chain(
        registry,
        prefix="deep",
        edge_count=MAX_RESOURCE_PARENT_DEPTH,
        dependency=False,
    )
    new_root = reference("new-root")
    registry.register(new_root)

    with pytest.raises(ResourceDepthError, match="parent"):
        registry.reparent(deep[0], parent=new_root)

    assert registry.parent_of(deep[0]) is None
    assert len(registry.ancestors(deep[-1])) == MAX_RESOURCE_PARENT_DEPTH


def test_public_projection_never_serializes_or_traverses_hidden_dependencies() -> None:
    registry = new_registry()
    function = reference("visible-function", kind=ResourceType.FUNCTION)
    hidden_type = reference("hidden-object-type", kind=ResourceType.OBJECT_TYPE)
    visible_dataset = reference("visible-dataset")
    for item in (function, hidden_type, visible_dataset):
        registry.register(item)
    registry.bind_dependency(function, hidden_type)
    registry.bind_dependency(hidden_type, visible_dataset)

    projection = registry.public_projection(
        function, visible={function, visible_dataset}
    )
    serialized = projection.model_dump_json()

    assert projection.resource == function
    assert projection.dependencies == ()
    assert "hidden-object-type" not in serialized
    assert "visible-dataset" not in serialized


def test_public_projection_requires_the_root_to_be_visible() -> None:
    registry = new_registry()
    function = reference("function", kind=ResourceType.FUNCTION)
    registry.register(function)

    with pytest.raises(ResourceVisibilityError):
        registry.public_projection(function, visible=set())


def test_public_projection_has_one_bounded_non_disclosing_error_contract() -> None:
    registry = new_registry()
    hidden = reference("hidden", kind=ResourceType.FUNCTION)
    current = reference(
        "stale",
        kind=ResourceType.FUNCTION,
        security_revision=2,
    )
    stale = reference(
        "stale",
        kind=ResourceType.FUNCTION,
        security_revision=1,
    )
    missing = reference("missing", kind=ResourceType.FUNCTION)
    visible = reference("visible", kind=ResourceType.FUNCTION)
    foreign = reference("foreign", tenant_id="tenant-b")
    for item in (hidden, current, visible, foreign):
        registry.register(item)

    calls = (
        lambda: registry.public_projection(hidden, visible=set()),
        lambda: registry.public_projection(missing, visible={missing}),
        lambda: registry.public_projection(stale, visible={stale}),
        lambda: registry.public_projection(visible, visible={visible, foreign}),
    )
    observed: list[tuple[type[Exception], str, str]] = []
    for call in calls:
        with pytest.raises(ResourceVisibilityError) as captured:
            call()
        observed.append(
            (type(captured.value), captured.value.code, str(captured.value))
        )

    assert (
        observed
        == [
            (ResourceVisibilityError, "resource_not_visible", "resource is not visible")
        ]
        * 4
    )


def test_trusted_registry_apis_keep_precise_missing_stale_and_cross_tenant_errors() -> (
    None
):
    registry = new_registry()
    current = reference("current", security_revision=2)
    stale = reference("current", security_revision=1)
    missing = reference("missing")
    foreign = reference("foreign", tenant_id="tenant-b")
    registry.register(current)
    registry.register(foreign)

    with pytest.raises(ResourceRevisionMismatchError):
        registry.dependencies_of(stale)
    with pytest.raises(ResourceNotFoundError):
        registry.dependencies_of(missing)
    with pytest.raises(CrossTenantResourceError):
        registry.bind_dependency(current, foreign)


def test_registry_rejects_unknown_and_stale_revision_references() -> None:
    registry = new_registry()
    current = reference("revenue", security_revision=2)
    stale = reference("revenue", security_revision=1)
    missing = reference("missing")
    registry.register(current)

    with pytest.raises(ResourceRevisionMismatchError):
        registry.dependencies_of(stale)
    with pytest.raises(ResourceNotFoundError):
        registry.dependencies_of(missing)


@given(
    names=st.lists(
        st.from_regex(r"[a-z][a-z0-9_-]{0,15}", fullmatch=True),
        min_size=1,
        max_size=180,
        unique=True,
    ),
    page_size=st.integers(min_value=1, max_value=100),
)
@settings(max_examples=40, deadline=None)
def test_keyset_pagination_returns_every_resource_once_without_a_total_cap(
    names: list[str],
    page_size: int,
) -> None:
    registry = new_registry()
    expected = tuple(
        sorted((reference(name) for name in names), key=lambda item: item.resource_id)
    )
    for item in reversed(expected):
        registry.register(item)

    assert collect_pages(registry, limit=page_size) == expected


def test_keyset_cursor_is_stable_and_bound_to_tenant_and_type() -> None:
    registry = new_registry()
    datasets = tuple(reference(f"dataset-{index:04d}") for index in range(257))
    project = reference("project", kind=ResourceType.PROJECT)
    for item in (*reversed(datasets), project):
        registry.register(item)

    first = registry.list_resources(
        tenant_id="tenant-a",
        resource_type=ResourceType.DATASET,
        page=ResourcePageRequest(limit=23),
    )
    repeated = registry.list_resources(
        tenant_id="tenant-a",
        resource_type=ResourceType.DATASET,
        page=ResourcePageRequest(limit=23),
    )
    assert first == repeated
    assert first.next_cursor is not None

    with pytest.raises(ResourceCursorError, match="tenant"):
        registry.list_resources(
            tenant_id="tenant-b",
            resource_type=ResourceType.DATASET,
            page=ResourcePageRequest(limit=23, cursor=first.next_cursor),
        )
    with pytest.raises(ResourceCursorError, match="query"):
        registry.list_resources(
            tenant_id="tenant-a",
            resource_type=ResourceType.PROJECT,
            page=ResourcePageRequest(limit=23, cursor=first.next_cursor),
        )
    with pytest.raises(ResourceCursorError):
        registry.list_resources(
            tenant_id="tenant-a",
            resource_type=ResourceType.DATASET,
            page=ResourcePageRequest(limit=23, cursor=first.next_cursor + "x"),
        )

    assert (
        collect_pages(
            registry,
            limit=23,
            resource_type=ResourceType.DATASET,
        )
        == datasets
    )


def test_keyset_cursor_rejects_payload_rewrite_and_validly_signed_noncanonical_json() -> (
    None
):
    resources = new_registry()
    for name in ("a", "b", "c"):
        resources.register(reference(name))
    first = resources.list_resources(
        tenant_id="tenant-a",
        page=ResourcePageRequest(limit=1),
    )
    assert first.next_cursor is not None
    version, encoded_payload, encoded_tag = first.next_cursor.split(".")
    payload = json.loads(
        base64.urlsafe_b64decode(encoded_payload + "=" * (-len(encoded_payload) % 4))
    )

    payload["last_id"] = "eios:dataset:b"
    rewritten_payload = (
        base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("ascii")
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    rewritten = f"{version}.{rewritten_payload}.{encoded_tag}"
    with pytest.raises(ResourceCursorError):
        resources.list_resources(
            tenant_id="tenant-a",
            page=ResourcePageRequest(limit=1, cursor=rewritten),
        )

    noncanonical_payload = (
        base64.urlsafe_b64encode(
            json.dumps(payload, indent=1, sort_keys=False).encode("ascii")
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    signing_input = f"v1.{noncanonical_payload}".encode("ascii")
    valid_tag = (
        base64.urlsafe_b64encode(
            hmac.digest(CURSOR_SIGNING_KEY, signing_input, "sha256")
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    noncanonical = f"v1.{noncanonical_payload}.{valid_tag}"
    with pytest.raises(ResourceCursorError):
        resources.list_resources(
            tenant_id="tenant-a",
            page=ResourcePageRequest(limit=1, cursor=noncanonical),
        )


def test_keyset_pagination_does_not_duplicate_items_inserted_around_a_cursor() -> None:
    registry = new_registry()
    for name in ("b", "d", "f"):
        registry.register(reference(name))
    first = registry.list_resources(
        tenant_id="tenant-a",
        page=ResourcePageRequest(limit=2),
    )
    assert tuple(item.resource_id for item in first.items) == (
        "eios:dataset:b",
        "eios:dataset:d",
    )
    assert first.next_cursor is not None

    registry.register(reference("a"))
    registry.register(reference("e"))
    second = registry.list_resources(
        tenant_id="tenant-a",
        page=ResourcePageRequest(limit=2, cursor=first.next_cursor),
    )

    assert tuple(item.resource_id for item in second.items) == (
        "eios:dataset:e",
        "eios:dataset:f",
    )
    assert set(first.items).isdisjoint(second.items)


def test_registry_revalidates_forged_page_requests_before_reading_indexes() -> None:
    registry = new_registry()
    expected = tuple(reference(f"item-{index:03d}") for index in range(140))
    for item in reversed(expected):
        registry.register(item)
    baseline = collect_pages(registry, limit=100)

    forged = (
        ResourcePageRequest.model_construct(limit=1_000, cursor=None),
        ResourcePageRequest.model_construct(limit=True, cursor=None),
        ResourcePageRequest.model_construct(limit=1.5, cursor=None),
        ResourcePageRequest.model_construct(limit=10, cursor=7),
    )
    forged_extra = ResourcePageRequest.model_construct(limit=10, cursor=None)
    object.__setattr__(forged_extra, "poison", "present")
    forged_private = ResourcePageRequest.model_construct(limit=10, cursor=None)
    object.__setattr__(forged_private, "__pydantic_private__", {"poison": True})

    for page in (*forged, forged_extra, forged_private):
        with pytest.raises(ResourceError, match="page request is invalid"):
            registry.list_resources(tenant_id="tenant-a", page=page)
        assert collect_pages(registry, limit=100) == baseline


def test_page_items_are_detached_from_registry_and_cursor_state() -> None:
    registry = new_registry()
    first_resource = reference("a")
    second_resource = reference("b")
    registry.register(first_resource)
    registry.register(second_resource)
    first = registry.list_resources(
        tenant_id="tenant-a",
        page=ResourcePageRequest(limit=1),
    )
    assert first.items == (first_resource,)
    assert first.next_cursor is not None

    object.__setattr__(first.items[0], "resource_id", "eios:dataset:poisoned")

    repeated = registry.list_resources(
        tenant_id="tenant-a",
        page=ResourcePageRequest(limit=1),
    )
    assert repeated.items == (first_resource,)
    assert repeated.next_cursor == first.next_cursor
    assert collect_pages(registry, limit=1) == (first_resource, second_resource)


@pytest.mark.parametrize(
    "result_name",
    (
        "parent",
        "dependencies",
        "ancestors",
        "closure",
        "projection-resource",
        "projection-parent",
        "projection-dependency",
    ),
)
def test_graph_and_projection_results_are_detached_from_registry(
    result_name: str,
) -> None:
    registry = new_registry()
    parent = reference("parent")
    child = reference("child")
    dependency = reference("dependency")
    registry.register(parent)
    registry.register(child, parent=parent)
    registry.register(dependency)
    registry.bind_dependency(child, dependency)

    if result_name == "parent":
        returned = registry.parent_of(child)
        assert returned is not None
    elif result_name == "dependencies":
        returned = registry.dependencies_of(child)[0]
    elif result_name == "ancestors":
        returned = registry.ancestors(child)[0]
    elif result_name == "closure":
        returned = registry.dependency_closure(child)[0]
    elif result_name.startswith("projection-"):
        projection = registry.public_projection(
            child,
            visible={parent, child, dependency},
        )
        if result_name == "projection-resource":
            returned = projection.resource
        elif result_name == "projection-parent":
            assert projection.parent is not None
            returned = projection.parent
        else:
            returned = projection.dependencies[0]
    else:
        raise AssertionError("unknown result name")

    object.__setattr__(returned, "resource_id", "eios:dataset:poisoned")

    assert registry.parent_of(child) == parent
    assert registry.dependencies_of(child) == (dependency,)
    assert registry.ancestors(child) == (parent,)
    assert registry.dependency_closure(child) == (dependency,)
    projection = registry.public_projection(
        child,
        visible={parent, child, dependency},
    )
    assert projection.resource == child
    assert projection.parent == parent
    assert projection.dependencies == (dependency,)
    assert collect_pages(registry) == (child, dependency, parent)


@pytest.mark.parametrize("limit", (0, 101, True, 1.5))
def test_page_request_rejects_invalid_page_limits(limit: object) -> None:
    with pytest.raises(ValidationError):
        ResourcePageRequest(limit=limit)  # type: ignore[arg-type]


def test_constants_are_the_frozen_program_limits() -> None:
    assert MAX_RESOURCE_PARENT_DEPTH == 32
    assert MAX_RESOURCE_DEPENDENCY_DEPTH == 32


def test_test_helpers_do_not_hide_an_accidental_iterable_truncation() -> None:
    registry = new_registry()
    resources: Iterable[ResourceReference] = (
        reference(f"resource-{i}") for i in range(401)
    )
    for item in resources:
        registry.register(item)

    assert len(collect_pages(registry, limit=100)) == 401


class _CountingAdjacency(dict):
    """邻接 dict 的读计数包装:图扫描无论沿哪个方向都必经 __getitem__。

    perf 测试的墙钟预算判据在满载轮 CPU 争用下必然偶发假红,这里统一还原为
    确定性的算法量计数(第七板斧:墙钟预算改计数)。
    """

    def __init__(self, backing: dict) -> None:
        super().__init__(backing)
        self.reads = 0

    def __getitem__(self, key):
        self.reads += 1
        return super().__getitem__(key)


def _install_counting_adjacency(resources) -> tuple[_CountingAdjacency, _CountingAdjacency]:
    # 白盒换入计数包装(私有属性耦合是有意的:实现不暴露扫描计数)
    counting_dependencies = _CountingAdjacency(resources._dependencies)
    counting_dependents = _CountingAdjacency(resources._dependents)
    resources._dependencies = counting_dependencies
    resources._dependents = counting_dependents
    return counting_dependencies, counting_dependents


@pytest.mark.performance
def test_wide_dependency_mutations_scale_incrementally_without_a_total_cap() -> None:
    # 原墙钟预算(large < 2.0 等)负载敏感;改计数:每次 bind 只应 O(1) 次邻接读,
    # 若对 root 的既有边做整体重算则 O(已有边数)/次,总量 O(N²) 当场超界。
    def build(edge_count: int) -> int:
        resources = new_registry()
        root = reference("wide-root", kind=ResourceType.FUNCTION)
        resources.register(root)
        counting_dependencies, counting_dependents = _install_counting_adjacency(
            resources
        )
        for index in range(edge_count):
            dependency = reference(f"wide-{index:05d}")
            resources.register(dependency)
            resources.bind_dependency(root, dependency)
        reads = counting_dependencies.reads + counting_dependents.reads
        assert len(resources.dependencies_of(root)) == edge_count
        return reads

    for edge_count in (250, 4_000):
        reads = build(edge_count)
        assert reads <= edge_count * 8, (edge_count, reads)


@pytest.mark.performance
def test_sources_hub_leaves_adversarial_width_scales_near_linearly() -> None:
    # 原墙钟预算负载敏感;改计数:两个方向的测量段邻接读都应 O(width)
    # (首条 leaf 会把全部 source 深度推一层,属 O(width) 一次性传播;
    # 若每条边都重扫对侧全宽则 O(width²) 当场超界)。
    def build(width: int, *, leaves_first: bool) -> int:
        resources = new_registry()
        hub = reference("hub", kind=ResourceType.FUNCTION)
        resources.register(hub)
        first_source: ResourceReference | None = None

        def add_sources() -> None:
            nonlocal first_source
            for index in range(width):
                source = reference(f"source-{index:05d}", kind=ResourceType.FUNCTION)
                resources.register(source)
                resources.bind_dependency(source, hub)
                first_source = first_source or source

        def add_leaves() -> None:
            for index in range(width):
                leaf = reference(f"leaf-{index:05d}")
                resources.register(leaf)
                resources.bind_dependency(hub, leaf)

        setup, measured = (
            (add_leaves, add_sources) if leaves_first else (add_sources, add_leaves)
        )
        setup()
        counting_dependencies, counting_dependents = _install_counting_adjacency(
            resources
        )
        measured()
        reads = counting_dependencies.reads + counting_dependents.reads

        assert first_source is not None
        assert len(resources.dependency_closure(first_source)) == width + 1
        return reads

    for leaves_first in (False, True):
        for width in (250, 4_000):
            reads = build(width, leaves_first=leaves_first)
            assert reads <= width * 12, (leaves_first, width, reads)


@pytest.mark.performance
def test_limit_one_full_pagination_uses_an_incremental_sorted_index() -> None:
    # 原墙钟预算负载敏感;改计数:把 _IndexNode 的 left/right slot 描述符临时
    # 换成计数 property,数分页遍历实际触达的树边。limit=1 的每页在 after 裁剪
    # 下只应触达 O(log n) 节点;若每页从最左线性重扫则总量 O(N²) 当场超界。
    from eios.authz.resources import _IndexNode

    for width in (250, 4_000):
        resources = new_registry()
        for index in reversed(range(width)):
            resources.register(reference(f"page-{index:05d}"))

        child_reads = [0]
        original_left = _IndexNode.left
        original_right = _IndexNode.right

        def _counting(descriptor):
            def read(node):
                child_reads[0] += 1
                return descriptor.__get__(node)

            return property(read, descriptor.__set__)

        _IndexNode.left = _counting(original_left)
        _IndexNode.right = _counting(original_right)
        try:
            cursor: str | None = None
            observed: list[str] = []
            while True:
                page = resources.list_resources(
                    tenant_id="tenant-a",
                    page=ResourcePageRequest(limit=1, cursor=cursor),
                )
                observed.extend(item.resource_id for item in page.items)
                if page.next_cursor is None:
                    break
                cursor = page.next_cursor
        finally:
            _IndexNode.left = original_left
            _IndexNode.right = original_right

        assert observed == sorted(observed)
        assert len(observed) == width
        # 每页 O(log₂ width) 边、两棵索引树,留常数余量
        assert child_reads[0] <= width * 64, (width, child_reads[0])


@pytest.mark.performance
def test_reverse_registration_does_not_move_an_unbounded_sorted_prefix() -> None:
    # 原墙钟预算负载敏感;改计数:包装模块级 _index_insert(递归走模块名,
    # 每层触达一个节点),倒序插入在平衡树下应 O(log n)/次;若退化成有序
    # 前缀整体移动/链表则 O(n)/次,总量 O(N²) 当场超界。
    import eios.authz.resources as resources_module

    for width in (250, 4_000):
        resources = new_registry()
        insert_calls = [0]
        original_insert = resources_module._index_insert

        def counting_insert(node, key):
            insert_calls[0] += 1
            return original_insert(node, key)

        resources_module._index_insert = counting_insert
        try:
            for index in reversed(range(width)):
                resources.register(reference(f"reverse-{index:05d}"))
        finally:
            resources_module._index_insert = original_insert

        observed = collect_pages(resources, limit=100)
        assert tuple(item.resource_id for item in observed) == tuple(
            f"eios:dataset:reverse-{index:05d}" for index in range(width)
        )
        # 两棵索引树 × O(log₂ width)/插入,留常数余量
        assert insert_calls[0] <= width * 64, (width, insert_calls[0])


@pytest.mark.performance
def test_redundant_same_component_edges_do_not_rescan_wide_descendants() -> None:
    # 原判据是墙钟预算(timings[4000] < 1.0 等),满载轮 CPU 争用必然偶发假红。
    # 改为计数断言:冗余边只应触发 O(1) 次邻接读(剪枝即返回),重扫 hub 的
    # width 个后代则每次 ≥ width 次读,量级差数百倍,负载免疫。
    for width in SCALING_WIDTHS:
        resources = new_registry()
        hub = reference("redundant-hub", kind=ResourceType.FUNCTION)
        anchor = reference("redundant-anchor", kind=ResourceType.FUNCTION)
        resources.register(hub)
        resources.register(anchor)
        resources.bind_dependency(anchor, hub)
        for index in range(width):
            leaf = reference(f"redundant-leaf-{index:05d}")
            resources.register(leaf)
            resources.bind_dependency(hub, leaf)

        sources: list[ResourceReference] = []
        for index in range(width):
            source = reference(
                f"redundant-source-{index:05d}", kind=ResourceType.FUNCTION
            )
            resources.register(source)
            resources.bind_dependency(source, anchor)
            sources.append(source)

        # 白盒换入计数包装(私有属性耦合是有意的:实现不暴露扫描计数)
        counting_dependencies = _CountingAdjacency(resources._dependencies)
        counting_dependents = _CountingAdjacency(resources._dependents)
        resources._dependencies = counting_dependencies
        resources._dependents = counting_dependents
        for source in sources:
            resources.bind_dependency(source, hub)
        reads = counting_dependencies.reads + counting_dependents.reads

        assert resources.dependencies_of(sources[0]) == (anchor, hub)
        assert reads <= width * 8, (width, reads)


def _oracle_reaches(
    edges: list[set[int]],
    start: int,
    target: int,
) -> bool:
    pending = [start]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if current == target:
            return True
        if current in visited:
            continue
        visited.add(current)
        pending.extend(edges[current])
    return False


def _oracle_max_depth(edges: list[set[int]]) -> int:
    memo: dict[int, int] = {}

    def depth(node: int) -> int:
        if node not in memo:
            memo[node] = max((1 + depth(child) for child in edges[node]), default=0)
        return memo[node]

    return max((depth(node) for node in range(len(edges))), default=0)


@st.composite
def dependency_mutation_cases(
    draw: st.DrawFn,
) -> tuple[int, list[tuple[int, int]]]:
    node_count = draw(st.integers(min_value=2, max_value=36))
    mutations = draw(
        st.lists(
            st.tuples(
                st.integers(min_value=0, max_value=node_count - 1),
                st.integers(min_value=0, max_value=node_count - 1),
            ),
            min_size=1,
            max_size=100,
        )
    )
    return node_count, mutations


@given(case=dependency_mutation_cases())
@settings(max_examples=60, deadline=None)
def test_dependency_mutations_match_a_naive_cycle_and_depth_oracle(
    case: tuple[int, list[tuple[int, int]]],
) -> None:
    node_count, mutations = case
    resources = new_registry()
    nodes = tuple(reference(f"oracle-{index:02d}") for index in range(node_count))
    for node in nodes:
        resources.register(node)
    oracle = [set() for _ in nodes]

    for source, dependency in mutations:
        if dependency in oracle[source]:
            resources.bind_dependency(nodes[source], nodes[dependency])
            continue
        if _oracle_reaches(oracle, dependency, source):
            with pytest.raises(ResourceCycleError):
                resources.bind_dependency(nodes[source], nodes[dependency])
            continue
        candidate = [set(targets) for targets in oracle]
        candidate[source].add(dependency)
        if _oracle_max_depth(candidate) > MAX_RESOURCE_DEPENDENCY_DEPTH:
            with pytest.raises(ResourceDepthError):
                resources.bind_dependency(nodes[source], nodes[dependency])
            continue
        resources.bind_dependency(nodes[source], nodes[dependency])
        oracle = candidate

    for index, node in enumerate(nodes):
        assert {
            nodes.index(dependency) for dependency in resources.dependencies_of(node)
        } == oracle[index]


def test_cycle_wins_over_depth_and_repeated_failures_leave_graph_unchanged() -> None:
    resources = new_registry()
    chain = tuple(
        reference(f"cycle-priority-{index:02d}")
        for index in range(MAX_RESOURCE_DEPENDENCY_DEPTH + 1)
    )
    extra = reference("cycle-priority-extra")
    for node in (*chain, extra):
        resources.register(node)
    for source, dependency in zip(chain, chain[1:]):
        resources.bind_dependency(source, dependency)

    for _ in range(100):
        with pytest.raises(ResourceCycleError):
            resources.bind_dependency(chain[-1], chain[0])
    with pytest.raises(ResourceDepthError):
        resources.bind_dependency(extra, chain[0])

    assert resources.dependencies_of(chain[-1]) == ()
    assert resources.dependencies_of(extra) == ()
    assert len(resources.dependency_closure(chain[0])) == 32
