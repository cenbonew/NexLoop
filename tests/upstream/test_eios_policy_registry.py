from __future__ import annotations

from copy import copy, deepcopy
from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
from inspect import Parameter, getsource, signature
import json
from pathlib import Path
from pickle import dumps as pickle_dumps, loads as pickle_loads
import sys
import tracemalloc

from hypothesis import given, strategies as st
import pytest
from pydantic import ValidationError

from eios.authz.policy import Eq, LiteralValue, Lt, SubjectRef, TrustedTimeRef
from eios.authz.policy.canonical import (
    CanonicalPolicyAst,
    canonicalize_policy_document,
    canonicalize_policy_expression,
    verify_canonical_policy_bytes,
)
import eios.authz.policy as policy_package
import eios.authz.policy.registry as policy_registry
from eios.authz.policy.registry import (
    ActivationEventKind,
    ActivationHead,
    ActivationRevisionWitness,
    DraftState,
    PolicyActivationTransition,
    PolicyBinding,
    PolicyDependencyKind,
    PolicyDependencyReference,
    PolicyEffect,
    PolicyEmergencyRevocation,
    PolicyKind,
    PolicyLifecycle,
    PolicyObligation,
    PolicyObligationKind,
    PolicyPublication,
    PolicyRegistryError,
    PolicySet,
    PolicySetLifecycleTransition,
    PolicyVersion,
    PolicyVersionEligibilityWitness,
    VersionState,
    activate_policy_version,
    activation_revision_witness,
    abandon_draft_revision,
    append_draft_revision,
    create_activation_head,
    create_draft_revision,
    create_policy_binding,
    emergency_revoke_active_policy,
    policy_version_state,
    publish_draft_revision,
    registry_digest,
    rollback_policy_version,
    transition_policy_set_lifecycle,
    validate_registry_model,
)


NOW = datetime(2026, 7, 21, 8, 0, tzinfo=UTC)


class _CallbackPretender:
    def require_published_unrevoked(
        self,
        *,
        tenant_id: str,
        policy_set_id: str,
        version: int,
        version_digest: str,
    ) -> bool:
        del tenant_id, policy_set_id, version, version_digest
        return True


class _DigestSpy:
    def __init__(self) -> None:
        self.update_calls = 0
        self.updated_bytes = 0

    def update(self, value: bytes) -> None:
        self.update_calls += 1
        self.updated_bytes += len(value)

    def hexdigest(self) -> str:
        raise AssertionError("overflowing registry digest reached hexdigest")


def _install_digest_spy(monkeypatch: pytest.MonkeyPatch) -> _DigestSpy:
    spy = _DigestSpy()
    monkeypatch.setattr(policy_registry, "sha256", lambda: spy)
    return spy


def _activate_for_test(
    head: ActivationHead,
    version: PolicyVersion,
    *,
    expected_active_version: int | None,
    expected_revision: int,
    expected_head_digest: str,
    activated_by: str,
    activated_at: datetime,
    event_kind: ActivationEventKind = ActivationEventKind.ACTIVATE,
) -> PolicyActivationTransition:
    return policy_registry._transition_to_version_internal(
        head,
        version,
        expected_active_version=expected_active_version,
        expected_revision=expected_revision,
        expected_head_digest=expected_head_digest,
        activated_by=activated_by,
        activated_at=activated_at,
        event_kind=event_kind,
    )


def _emergency_revoke_for_test(
    head: ActivationHead,
    active_version: PolicyVersion,
    *,
    expected_active_version: int,
    expected_revision: int,
    expected_head_digest: str,
    reason: str,
    revoked_by: str,
    revoked_at: datetime,
    replacement: PolicyVersion | None = None,
) -> PolicyEmergencyRevocation:
    return policy_registry._emergency_revoke_active_policy_internal(
        head,
        active_version,
        expected_active_version=expected_active_version,
        expected_revision=expected_revision,
        expected_head_digest=expected_head_digest,
        reason=reason,
        revoked_by=revoked_by,
        revoked_at=revoked_at,
        replacement=replacement,
    )


def test_registry_vocabularies_are_closed_and_policy_set_is_strict_frozen() -> None:
    assert tuple(kind.value for kind in PolicyKind) == (
        "resource_access",
        "object_security",
        "property_security",
        "function_execution",
        "action_submission",
        "action_approval",
        "export_control",
        "delegation",
    )
    assert tuple(state.value for state in PolicyLifecycle) == (
        "active",
        "disabled",
        "retired",
    )
    assert tuple(state.value for state in DraftState) == (
        "draft",
        "published",
        "abandoned",
    )
    assert tuple(state.value for state in VersionState) == (
        "published",
        "revoked",
    )

    policy_set = PolicySet(
        tenant_id="tenant-a",
        policy_set_id="invoice-read",
        kind=PolicyKind.RESOURCE_ACCESS,
        lifecycle=PolicyLifecycle.ACTIVE,
        revision=1,
        created_by="subject-admin",
        created_at=NOW,
    )
    assert validate_registry_model(policy_set, PolicySet) == policy_set

    with pytest.raises(ValidationError, match="frozen"):
        policy_set.revision = 2
    with pytest.raises(ValidationError, match="extra_forbidden"):
        PolicySet(
            tenant_id="tenant-a",
            policy_set_id="invoice-read",
            kind=PolicyKind.RESOURCE_ACCESS,
            lifecycle=PolicyLifecycle.ACTIVE,
            revision=1,
            created_by="subject-admin",
            created_at=NOW,
            hidden_allow=True,
        )
    with pytest.raises(ValidationError):
        PolicySet.model_validate(
            {
                "tenant_id": "tenant-a",
                "policy_set_id": "invoice-read",
                "kind": "resource_access",
                "lifecycle": "active",
                "revision": 1,
                "created_by": "subject-admin",
                "created_at": NOW.isoformat(),
            },
            strict=True,
        )


def test_registry_validation_rejects_model_construct_and_private_pollution() -> None:
    forged = PolicySet.model_construct(
        tenant_id="tenant-a",
        policy_set_id="invoice-read",
        kind=PolicyKind.RESOURCE_ACCESS,
        lifecycle=PolicyLifecycle.ACTIVE,
        revision=1,
        created_by="subject-admin",
        created_at=NOW,
    )
    with pytest.raises(ValueError, match="registry model is invalid"):
        validate_registry_model(forged, PolicySet)

    valid = PolicySet(
        tenant_id="tenant-a",
        policy_set_id="invoice-read",
        kind=PolicyKind.RESOURCE_ACCESS,
        lifecycle=PolicyLifecycle.ACTIVE,
        revision=1,
        created_by="subject-admin",
        created_at=NOW,
    )
    valid.__pydantic_private__["unexpected"] = "pollution"
    with pytest.raises(ValueError, match="registry model is invalid"):
        validate_registry_model(valid, PolicySet)


def test_policy_set_lifecycle_transition_is_adjacent_audited_and_cas_bound() -> None:
    active = PolicySet(
        tenant_id="tenant-a",
        policy_set_id="invoice-read",
        kind=PolicyKind.RESOURCE_ACCESS,
        lifecycle=PolicyLifecycle.ACTIVE,
        revision=1,
        created_by="subject-admin",
        created_at=NOW,
    )

    disabled = transition_policy_set_lifecycle(
        active,
        expected_revision=1,
        lifecycle=PolicyLifecycle.DISABLED,
        updated_by="subject-security",
        updated_at=NOW.replace(hour=9),
    )

    assert type(disabled) is PolicySetLifecycleTransition
    assert disabled.previous_policy_set == active
    assert disabled.policy_set == PolicySet(
        tenant_id=active.tenant_id,
        policy_set_id=active.policy_set_id,
        kind=active.kind,
        lifecycle=PolicyLifecycle.DISABLED,
        revision=2,
        created_by=active.created_by,
        created_at=active.created_at,
    )
    assert disabled.updated_by == "subject-security"
    assert disabled.updated_at == NOW.replace(hour=9)

    retired = transition_policy_set_lifecycle(
        disabled.policy_set,
        expected_revision=2,
        lifecycle=PolicyLifecycle.RETIRED,
        updated_by="subject-security",
        updated_at=NOW.replace(hour=10),
    )
    assert retired.policy_set.lifecycle is PolicyLifecycle.RETIRED
    assert retired.policy_set.revision == 3


@pytest.mark.parametrize(
    ("source", "target"),
    (
        (PolicyLifecycle.ACTIVE, PolicyLifecycle.ACTIVE),
        (PolicyLifecycle.ACTIVE, PolicyLifecycle.RETIRED),
        (PolicyLifecycle.DISABLED, PolicyLifecycle.ACTIVE),
        (PolicyLifecycle.DISABLED, PolicyLifecycle.DISABLED),
        (PolicyLifecycle.RETIRED, PolicyLifecycle.ACTIVE),
        (PolicyLifecycle.RETIRED, PolicyLifecycle.DISABLED),
        (PolicyLifecycle.RETIRED, PolicyLifecycle.RETIRED),
    ),
)
def test_policy_set_lifecycle_transition_rejects_restore_same_state_and_skip(
    source: PolicyLifecycle,
    target: PolicyLifecycle,
) -> None:
    current = PolicySet(
        tenant_id="tenant-a",
        policy_set_id="invoice-read",
        kind=PolicyKind.RESOURCE_ACCESS,
        lifecycle=source,
        revision=4,
        created_by="subject-admin",
        created_at=NOW,
    )
    with pytest.raises(PolicyRegistryError, match="lifecycle transition is invalid"):
        transition_policy_set_lifecycle(
            current,
            expected_revision=4,
            lifecycle=target,
            updated_by="subject-security",
            updated_at=NOW.replace(hour=9),
        )


def test_policy_set_lifecycle_transition_rejects_stale_or_invalid_audit_data() -> None:
    current = PolicySet(
        tenant_id="tenant-a",
        policy_set_id="invoice-read",
        kind=PolicyKind.RESOURCE_ACCESS,
        lifecycle=PolicyLifecycle.ACTIVE,
        revision=2,
        created_by="subject-admin",
        created_at=NOW,
    )
    with pytest.raises(PolicyRegistryError, match="lifecycle CAS mismatch"):
        transition_policy_set_lifecycle(
            current,
            expected_revision=1,
            lifecycle=PolicyLifecycle.DISABLED,
            updated_by="subject-security",
            updated_at=NOW.replace(hour=9),
        )
    with pytest.raises(PolicyRegistryError, match="lifecycle transition is invalid"):
        transition_policy_set_lifecycle(
            current,
            expected_revision=2,
            lifecycle=PolicyLifecycle.DISABLED,
            updated_by=" subject-security ",
            updated_at=NOW.replace(hour=9),
        )
    with pytest.raises(PolicyRegistryError, match="lifecycle transition is invalid"):
        transition_policy_set_lifecycle(
            current,
            expected_revision=2,
            lifecycle=PolicyLifecycle.DISABLED,
            updated_by="subject-security",
            updated_at=NOW.replace(hour=7),
        )


def test_policy_registry_public_contract_is_exported_from_policy_package() -> None:
    assert policy_package.PolicyKind is PolicyKind
    assert policy_package.PolicyVersion is PolicyVersion
    assert policy_package.PolicySetLifecycleTransition is PolicySetLifecycleTransition
    assert (
        policy_package.transition_policy_set_lifecycle
        is transition_policy_set_lifecycle
    )
    assert (
        policy_package.PolicyVersionEligibilityWitness
        is PolicyVersionEligibilityWitness
    )
    assert policy_package.canonicalize_policy_document is canonicalize_policy_document
    assert policy_package.create_policy_binding is create_policy_binding
    assert (
        policy_package.emergency_revoke_active_policy is emergency_revoke_active_policy
    )


def test_repository_eligibility_protocol_is_not_a_public_security_boundary() -> None:
    assert not hasattr(policy_package, "PolicyVersionEligibilityAuthority")
    assert not hasattr(
        __import__(
            "eios.authz.policy.registry",
            fromlist=["PolicyVersionEligibilityAuthority"],
        ),
        "PolicyVersionEligibilityAuthority",
    )


def test_create_policy_binding_has_db_recomputable_digest_golden() -> None:
    parameters = signature(create_policy_binding).parameters
    assert tuple(parameters) == (
        "tenant_id",
        "binding_id",
        "policy_set_id",
        "resource_type",
        "resource_id",
        "operation",
    )
    assert all(
        parameter.kind is Parameter.KEYWORD_ONLY
        and parameter.default is Parameter.empty
        for parameter in parameters.values()
    )
    binding = create_policy_binding(
        tenant_id="tenant-a",
        binding_id="binding-invoice-read",
        policy_set_id="invoice-read",
        resource_type="ontology-object",
        resource_id="invoice-42",
        operation="read",
    )

    assert type(binding) is PolicyBinding
    assert binding == PolicyBinding(
        tenant_id="tenant-a",
        binding_id="binding-invoice-read",
        policy_set_id="invoice-read",
        resource_type="ontology-object",
        resource_id="invoice-42",
        operation="read",
        binding_digest=(
            "8b6986f4fe24df7f0b74e92516986d500109ba55d6b730f439cc5d951d697c79"
        ),
    )
    assert binding.binding_digest == registry_digest(
        (
            ("record_type", "policy_binding"),
            ("tenant_id", "tenant-a"),
            ("binding_id", "binding-invoice-read"),
            ("policy_set_id", "invoice-read"),
            ("resource_type", "ontology-object"),
            ("resource_id", "invoice-42"),
            ("operation", "read"),
        )
    )


def _eq_literal(value: object) -> Eq:
    return Eq(
        op="eq",
        node_id="department-match",
        left=SubjectRef(kind="subject", attribute="department"),
        right=LiteralValue(kind="literal", value=value),
    )


def test_policy_ast_canonical_golden_vector_and_digest() -> None:
    canonical = canonicalize_policy_expression(_eq_literal("finance"))
    expected = (
        b'{"left":{"attribute":"department","kind":"subject"},'
        b'"node_id":"department-match","op":"eq",'
        b'"right":{"kind":"literal","value":"finance",'
        b'"value_type":"scalar"}}'
    )

    assert canonical == CanonicalPolicyAst(
        utf8=expected,
        sha256=sha256(expected).hexdigest(),
    )
    assert (
        canonical.sha256
        == "4f26fa2aa438accfe46ada71fb0fc93549d4a904374c27043d37d3db1b02a365"
    )


def test_canonical_numbers_preserve_eios_scalar_types() -> None:
    integer = canonicalize_policy_expression(_eq_literal(1))
    number = canonicalize_policy_expression(_eq_literal(1.0))
    exponent = canonicalize_policy_document(
        b'{"op":"eq","node_id":"department-match",'
        b'"left":{"kind":"subject","attribute":"department"},'
        b'"right":{"kind":"literal","value":1e3}}'
    )
    negative_zero = canonicalize_policy_expression(_eq_literal(-0.0))

    assert b'"value":1,' in integer.utf8
    assert b'"value":1.0,' in number.utf8
    assert b'"value":1000.0,' in exponent.utf8
    assert b'"value":0.0,' in negative_zero.utf8
    assert integer.sha256 != number.sha256
    assert exponent.sha256 != canonicalize_policy_expression(_eq_literal(1000)).sha256


def test_canonical_datetime_is_utc_rfc3339_with_fixed_microseconds() -> None:
    expression = Lt(
        op="lt",
        node_id="before-deadline",
        left=TrustedTimeRef(kind="trusted_time", value="now"),
        right=LiteralValue(
            kind="literal",
            value_type="datetime",
            value=datetime(2026, 7, 21, 16, 2, 3, 4, tzinfo=UTC),
        ),
    )

    canonical = canonicalize_policy_expression(expression)

    assert b'"value":"2026-07-21T16:02:03.000004Z"' in canonical.utf8


def test_canonical_document_rejects_non_json_or_non_eios_ast_inputs() -> None:
    valid = (
        b'{"op":"exists","node_id":"x",'
        b'"value":{"kind":"subject","attribute":"department"}}'
    )
    invalid_documents = (
        valid.replace(b'"node_id":"x"', b'"node_id":"x","node_id":"y"'),
        valid + b" trailing",
        valid.replace(b'"department"', b'"\xff"'),
        valid.replace(b'"department"', b'"\\ud800"'),
        valid.replace(b'"exists"', b'"unknown"'),
        b'{"op":"eq","node_id":"x","left":NaN,"right":null}',
        b" " * 1_048_577,
    )
    for document in invalid_documents:
        with pytest.raises(ValueError, match="policy document is invalid"):
            canonicalize_policy_document(document)


def test_canonicalization_is_order_stable_and_semantic_changes_change_digest() -> None:
    first = (
        b'{"op":"exists","node_id":"x",'
        b'"value":{"kind":"subject","attribute":"department"}}'
    )
    reordered = (
        b'{ "value": {"attribute":"department","kind":"subject"},'
        b' "node_id":"x", "op":"exists" }'
    )
    changed = first.replace(b"department", b"cost_center")

    assert canonicalize_policy_document(first) == canonicalize_policy_document(
        reordered
    )
    assert (
        canonicalize_policy_document(first).sha256
        != canonicalize_policy_document(changed).sha256
    )


def test_verify_canonical_bytes_reparses_revalidates_and_requires_byte_identity() -> (
    None
):
    canonical = canonicalize_policy_expression(_eq_literal("finance"))

    verified = verify_canonical_policy_bytes(canonical.utf8, canonical.sha256)
    assert verified == canonical

    with pytest.raises(ValueError, match="canonical policy bytes are invalid"):
        verify_canonical_policy_bytes(b" " + canonical.utf8, canonical.sha256)
    with pytest.raises(ValueError, match="canonical policy bytes are invalid"):
        verify_canonical_policy_bytes(canonical.utf8, "0" * 64)


@given(
    st.one_of(
        st.booleans(),
        st.integers(min_value=-(2**63), max_value=2**63 - 1),
        st.floats(allow_nan=False, allow_infinity=False, width=64),
        st.text(
            alphabet=st.characters(
                blacklist_categories=("Cc", "Cs"),
                blacklist_characters=" \t\r\n",
            ),
            min_size=1,
            max_size=40,
        ).filter(lambda value: value == value.strip()),
    )
)
def test_canonical_round_trip_property_preserves_valid_scalar(value: object) -> None:
    try:
        expression = _eq_literal(value)
    except (UnicodeError, ValidationError):
        return
    canonical = canonicalize_policy_expression(expression)

    assert verify_canonical_policy_bytes(canonical.utf8, canonical.sha256) == canonical
    assert json.loads(canonical.utf8)["right"]["value"] == value


def _dependency(
    *,
    tenant_id: str = "tenant-a",
    dependency_id: str = "invoice-schema",
    digest: str = "a" * 64,
) -> PolicyDependencyReference:
    return PolicyDependencyReference(
        tenant_id=tenant_id,
        kind=PolicyDependencyKind.SCHEMA,
        dependency_id=dependency_id,
        revision=4,
        digest=digest,
    )


def _binding(
    *,
    tenant_id: str = "tenant-a",
    binding_id: str = "invoice-read-binding",
) -> PolicyBinding:
    return PolicyBinding(
        tenant_id=tenant_id,
        binding_id=binding_id,
        policy_set_id="invoice-read",
        resource_type="object_type",
        resource_id="Invoice",
        operation="read",
        binding_digest="b" * 64,
    )


def _deny_export() -> PolicyObligation:
    return PolicyObligation(kind=PolicyObligationKind.DENY_EXPORT)


def _draft(*, tenant_id: str = "tenant-a"):
    return create_draft_revision(
        tenant_id=tenant_id,
        policy_set_id="invoice-read",
        draft_id="draft-1",
        effect=PolicyEffect.DENY_IF,
        canonical_ast=canonicalize_policy_expression(_eq_literal("blocked")),
        obligations=(),
        dependencies=(_dependency(tenant_id=tenant_id),),
        author="subject-admin",
        created_at=NOW,
    )


def _version(number: int = 1, *, tenant_id: str = "tenant-a") -> PolicyVersion:
    draft = _draft(tenant_id=tenant_id)
    publication = publish_draft_revision(
        draft,
        expected_revision=draft.revision,
        expected_digest=draft.draft_digest,
        version=number,
        published_by="subject-publisher",
        published_at=NOW,
    )
    return publication.version


def _forged_eligibility_witness(
    policy_version: PolicyVersion,
    *,
    registry_revision: int = 0,
    **changes: object,
) -> PolicyVersionEligibilityWitness:
    data: dict[str, object] = {
        "tenant_id": policy_version.tenant_id,
        "policy_set_id": policy_version.policy_set_id,
        "version": policy_version.version,
        "version_digest": policy_version.version_digest,
        "record_digest": policy_version.version_digest,
        "published": True,
        "not_revoked": True,
        "registry_revision": registry_revision,
        "issuer_identity": "eios.authz.policy-registry.repository.0057",
    }
    data.update(changes)
    data.setdefault(
        "repository_witness",
        registry_digest(
            (
                ("record_type", "policy_version_eligibility_witness"),
                ("tenant_id", data["tenant_id"]),
                ("policy_set_id", data["policy_set_id"]),
                ("version", data["version"]),
                ("version_digest", data["version_digest"]),
                ("record_digest", data["record_digest"]),
                ("published", data["published"]),
                ("not_revoked", data["not_revoked"]),
                ("registry_revision", data["registry_revision"]),
                ("issuer_identity", data["issuer_identity"]),
            )
        ),
    )
    return PolicyVersionEligibilityWitness(**data)


def test_registry_digest_uses_typed_length_prefixed_framing_golden_vector() -> None:
    digest = registry_digest(
        (
            ("tenant", "tenant-a"),
            ("revision", 7),
            ("payload", b"policy"),
            ("created_at", NOW),
            ("kind", PolicyKind.RESOURCE_ACCESS),
        )
    )

    assert digest == "24b31e7cc12603f19d25527af630d1b11c855db19ea61e77cf79cce1d53fd557"
    assert registry_digest((("first", "ab"), ("second", "c"))) != registry_digest(
        (("first", "a"), ("second", "bc"))
    )
    assert registry_digest((("value", 1),)) != registry_digest((("value", "1"),))


def test_registry_digest_enum_identity_and_value_have_independent_golden_frames() -> (
    None
):
    left = Enum("A", {"X": "B:C"}, module="stable.test").X
    right = Enum("A:B", {"X": "C"}, module="stable.test").X

    left_digest = registry_digest((("value", left),))
    right_digest = registry_digest((("value", right),))

    assert (
        left_digest
        == "49c09688dc9a7d21e5e6c51f8f84668e6193909cf8153133bcc3fdfadf9b8c1f"
    )
    assert (
        right_digest
        == "402d4ef46b1e30b46ae851b4826096879e56cf3c33b874ab79ce33eabde78baf"
    )
    assert left_digest != right_digest

    module_left = Enum("B:C", {"X": "D"}, module="A").X
    module_right = Enum("C", {"X": "D"}, module="A:B").X
    assert registry_digest((("value", module_left),)) != registry_digest(
        (("value", module_right),)
    )


def test_registry_digest_scalar_tags_and_nested_field_boundaries_do_not_collide() -> (
    None
):
    scalar_values = (
        "1",
        b"1",
        1,
        True,
        Enum("StableScalar", {"X": "1"}, module="stable.test").X,
    )
    digests = tuple(registry_digest((("value", value),)) for value in scalar_values)

    assert len(digests) == len(set(digests))
    assert registry_digest((("first", "ab"), ("second", "c"))) != registry_digest(
        (("first", "a"), ("second", "bc"))
    )


def test_registry_digest_budgets_are_an_explicit_public_contract() -> None:
    assert policy_package.MAX_REGISTRY_DIGEST_FIELDS == 4_096
    assert policy_package.MAX_REGISTRY_DIGEST_VALUE_BYTES == 1_048_576
    assert policy_package.MAX_REGISTRY_DIGEST_CANONICAL_BYTES == 8_388_608


def test_registry_digest_total_canonical_budget_accepts_boundary_and_rejects_plus_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    field_count = 8
    field_name_bytes = 8
    canonical_header = len(b"EIOS-POLICY-REGISTRY\x00\x01") + 4
    field_overhead = 4 + field_name_bytes + 1 + 8
    full_value = b"x" * policy_package.MAX_REGISTRY_DIGEST_VALUE_BYTES
    final_value_bytes = (
        policy_package.MAX_REGISTRY_DIGEST_CANONICAL_BYTES
        - canonical_header
        - field_count * field_overhead
        - (field_count - 1) * len(full_value)
    )
    final_value = b"y" * final_value_bytes
    boundary_fields = tuple(
        (f"field-{index:02d}", full_value) for index in range(field_count - 1)
    ) + (("field-07", final_value),)

    assert len(registry_digest(boundary_fields)) == 64
    spy = _install_digest_spy(monkeypatch)
    with pytest.raises(PolicyRegistryError, match="registry digest fields are invalid"):
        registry_digest(boundary_fields[:-1] + (("field-07", final_value + b"z"),))
    assert (spy.update_calls, spy.updated_bytes) == (0, 0)


@pytest.mark.parametrize(
    ("field_count", "overflow_index"),
    (
        (3, 1),
        (9, 4),
        (9, 8),
    ),
)
def test_registry_digest_preflight_hashes_nothing_when_field_crosses_total_budget(
    monkeypatch: pytest.MonkeyPatch,
    field_count: int,
    overflow_index: int,
) -> None:
    canonical_header = len(b"EIOS-POLICY-REGISTRY\x00\x01") + 4
    one_byte_field_size = 4 + 8 + 1 + 8 + 1
    monkeypatch.setattr(
        policy_registry,
        "MAX_REGISTRY_DIGEST_CANONICAL_BYTES",
        canonical_header + overflow_index * one_byte_field_size,
    )
    fields = tuple((f"field-{index:02d}", b"x") for index in range(field_count))
    spy = _install_digest_spy(monkeypatch)

    with pytest.raises(PolicyRegistryError, match="registry digest fields are invalid"):
        registry_digest(fields)

    assert (spy.update_calls, spy.updated_bytes) == (0, 0)


def test_registry_digest_per_value_budget_accepts_boundary_and_rejects_plus_one() -> (
    None
):
    boundary = b"x" * policy_package.MAX_REGISTRY_DIGEST_VALUE_BYTES
    assert len(registry_digest((("payload", boundary),))) == 64
    with pytest.raises(PolicyRegistryError, match="registry digest fields are invalid"):
        registry_digest((("payload", boundary + b"x"),))


def test_registry_digest_rejects_repeated_megabyte_before_large_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_bytearray = bytearray

    class _AllocationGuard(original_bytearray):
        def extend(self, value: object) -> None:
            if len(self) + len(value) > 16_777_216:  # type: ignore[arg-type]
                raise AssertionError("registry digest attempted a large allocation")
            super().extend(value)  # type: ignore[arg-type]

    monkeypatch.setattr(
        policy_registry,
        "bytearray",
        _AllocationGuard,
        raising=False,
    )
    repeated = b"x" * policy_package.MAX_REGISTRY_DIGEST_VALUE_BYTES
    fields = tuple(
        (f"payload-{index}", repeated)
        for index in range(policy_package.MAX_REGISTRY_DIGEST_FIELDS)
    )
    spy = _install_digest_spy(monkeypatch)

    with pytest.raises(PolicyRegistryError, match="registry digest fields are invalid"):
        registry_digest(fields)
    assert (spy.update_calls, spy.updated_bytes) == (0, 0)


def test_registry_digest_rejects_multibyte_text_before_encoding_allocation() -> None:
    oversized_text = "🙂" * policy_package.MAX_REGISTRY_DIGEST_VALUE_BYTES

    tracemalloc.start()
    try:
        with pytest.raises(
            PolicyRegistryError,
            match="registry digest fields are invalid",
        ):
            registry_digest((("payload", oversized_text),))
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak_bytes < 2_097_152


def test_registry_digest_checks_remaining_total_budget_before_text_encoding() -> None:
    prefix_count = 8
    total_field_count = prefix_count + 1
    field_name_bytes = 8
    canonical_header = len(b"EIOS-POLICY-REGISTRY\x00\x01") + 4
    field_overhead = 4 + field_name_bytes + 1 + 8
    allowed_final_value_bytes = 16
    prefix_payload_bytes = (
        policy_package.MAX_REGISTRY_DIGEST_CANONICAL_BYTES
        - canonical_header
        - total_field_count * field_overhead
        - allowed_final_value_bytes
    )
    full_value = b"x" * policy_package.MAX_REGISTRY_DIGEST_VALUE_BYTES
    final_prefix = b"y" * (prefix_payload_bytes - (prefix_count - 1) * len(full_value))
    prefix = tuple(
        (f"field-{index:02d}", full_value) for index in range(prefix_count - 1)
    ) + (("field-07", final_prefix),)
    oversized_tail = "z" * policy_package.MAX_REGISTRY_DIGEST_VALUE_BYTES

    tracemalloc.start()
    try:
        with pytest.raises(
            PolicyRegistryError,
            match="registry digest fields are invalid",
        ):
            registry_digest(prefix + (("final-00", oversized_tail),))
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak_bytes < 524_288


def test_registry_digest_field_count_boundary_is_strict() -> None:
    boundary = tuple(
        (f"field-{index}", index)
        for index in range(policy_package.MAX_REGISTRY_DIGEST_FIELDS)
    )
    assert len(registry_digest(boundary)) == 64
    with pytest.raises(PolicyRegistryError, match="registry digest fields are invalid"):
        registry_digest(boundary + (("field-overflow", 0),))


def test_registry_digest_uses_incremental_hash_updates_without_bytearray() -> None:
    source = getsource(registry_digest)
    assert ".update(" in source
    assert "bytearray" not in source


def test_registry_digest_integer_domain_is_signed_64_bit() -> None:
    minimum = -(2**63)
    maximum = 2**63 - 1

    assert policy_package.MIN_REGISTRY_DIGEST_INTEGER == minimum
    assert policy_package.MAX_REGISTRY_DIGEST_INTEGER == maximum
    assert len(registry_digest((("value", minimum),))) == 64
    assert len(registry_digest((("value", maximum),))) == 64
    assert registry_digest((("value", -1),)) != registry_digest((("value", 1),))
    for outside in (minimum - 1, maximum + 1):
        with pytest.raises(
            PolicyRegistryError,
            match="registry digest fields are invalid",
        ):
            registry_digest((("value", outside),))


@pytest.mark.parametrize("digit_limit", (0, 1_000_000))
def test_registry_digest_rejects_large_integer_before_decimal_conversion(
    digit_limit: int,
) -> None:
    oversized = 1 << 800_000
    previous_limit = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(digit_limit)
    tracemalloc.start()
    try:
        with pytest.raises(
            PolicyRegistryError,
            match="registry digest fields are invalid",
        ):
            registry_digest((("value", oversized),))
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        sys.set_int_max_str_digits(previous_limit)

    assert peak_bytes < 131_072


def test_dependency_and_binding_references_require_full_lowercase_digests() -> None:
    assert _dependency().digest == "a" * 64
    assert _binding().binding_digest == "b" * 64

    for digest in ("a" * 63, "A" * 64, "g" * 64):
        with pytest.raises(ValidationError, match="full lowercase digest"):
            _dependency(digest=digest)


def test_draft_revisions_are_append_only_and_cas_bound() -> None:
    first = _draft()
    second_ast = canonicalize_policy_expression(_eq_literal("quarantined"))
    second = append_draft_revision(
        first,
        expected_revision=1,
        expected_digest=first.draft_digest,
        canonical_ast=second_ast,
        effect=PolicyEffect.REQUIRE,
        obligations=(),
        dependencies=first.dependencies,
        author="subject-editor",
        created_at=NOW,
    )

    assert first.revision == 1
    assert second.revision == 2
    assert second.previous_revision_digest == first.draft_digest
    assert second.draft_digest != first.draft_digest
    with pytest.raises(ValidationError, match="frozen"):
        first.revision = 2
    with pytest.raises(ValueError, match="draft CAS mismatch"):
        append_draft_revision(
            first,
            expected_revision=2,
            expected_digest=first.draft_digest,
            canonical_ast=second_ast,
            effect=PolicyEffect.REQUIRE,
            obligations=(),
            dependencies=first.dependencies,
            author="subject-editor",
            created_at=NOW,
        )


def test_abandon_appends_terminal_fact_and_cannot_be_reopened() -> None:
    draft = _draft()
    with pytest.raises(PolicyRegistryError, match="draft CAS mismatch"):
        abandon_draft_revision(
            draft,
            expected_revision=2,
            expected_digest=draft.draft_digest,
            abandoned_by="subject-admin",
            abandoned_at=NOW,
        )
    abandoned = abandon_draft_revision(
        draft,
        expected_revision=1,
        expected_digest=draft.draft_digest,
        abandoned_by="subject-admin",
        abandoned_at=NOW,
    )

    assert abandoned.state is DraftState.ABANDONED
    assert abandoned.revision == 2
    assert abandoned.previous_revision_digest == draft.draft_digest
    with pytest.raises(ValueError, match="draft is terminal"):
        abandon_draft_revision(
            abandoned,
            expected_revision=2,
            expected_digest=abandoned.draft_digest,
            abandoned_by="subject-admin",
            abandoned_at=NOW,
        )


@pytest.mark.parametrize(
    ("abandoned_by", "abandoned_at"),
    (
        ("", NOW),
        ("subject-admin", datetime(2026, 7, 21, 8, 0)),
    ),
)
def test_abandon_wraps_invalid_actor_and_timestamp_as_registry_error(
    abandoned_by: str,
    abandoned_at: datetime,
) -> None:
    draft = _draft()

    with pytest.raises(
        PolicyRegistryError,
        match="draft revision is invalid",
    ) as caught:
        abandon_draft_revision(
            draft,
            expected_revision=draft.revision,
            expected_digest=draft.draft_digest,
            abandoned_by=abandoned_by,
            abandoned_at=abandoned_at,
        )

    assert type(caught.value) is PolicyRegistryError
    assert caught.value.__cause__ is None


def test_draft_rejects_duplicate_or_cross_tenant_dependencies_and_obligations() -> None:
    canonical = canonicalize_policy_expression(_eq_literal("blocked"))
    with pytest.raises(ValueError, match="draft revision is invalid"):
        create_draft_revision(
            tenant_id="tenant-a",
            policy_set_id="invoice-read",
            draft_id="draft-1",
            effect=PolicyEffect.OBLIGATION_IF,
            canonical_ast=canonical,
            obligations=(_deny_export(), _deny_export()),
            dependencies=(_dependency(),),
            author="subject-admin",
            created_at=NOW,
        )
    with pytest.raises(ValueError, match="draft revision is invalid"):
        create_draft_revision(
            tenant_id="tenant-a",
            policy_set_id="invoice-read",
            draft_id="draft-1",
            effect=PolicyEffect.DENY_IF,
            canonical_ast=canonical,
            obligations=(),
            dependencies=(_dependency(tenant_id="tenant-b"),),
            author="subject-admin",
            created_at=NOW,
        )
    with pytest.raises(ValueError, match="draft revision is invalid"):
        create_draft_revision(
            tenant_id="tenant-a",
            policy_set_id="invoice-read",
            draft_id="draft-1",
            effect=PolicyEffect.DENY_IF,
            canonical_ast=canonical,
            obligations=(),
            dependencies=(_dependency(), _dependency()),
            author="subject-admin",
            created_at=NOW,
        )


def test_publish_appends_terminal_draft_fact_and_creates_immutable_version() -> None:
    draft = _draft()
    publication = publish_draft_revision(
        draft,
        expected_revision=1,
        expected_digest=draft.draft_digest,
        version=1,
        published_by="subject-publisher",
        published_at=NOW,
    )

    assert publication.published_draft.state is DraftState.PUBLISHED
    assert publication.published_draft.revision == 2
    assert publication.version.version == 1
    assert publication.version.source_draft_digest == draft.draft_digest
    assert "state" not in type(publication.version).model_fields
    assert policy_version_state(publication.version) is VersionState.PUBLISHED
    with pytest.raises(ValidationError, match="frozen"):
        publication.version.effect = PolicyEffect.REQUIRE
    with pytest.raises(ValueError, match="draft is terminal"):
        append_draft_revision(
            publication.published_draft,
            expected_revision=2,
            expected_digest=publication.published_draft.draft_digest,
            canonical_ast=canonicalize_policy_expression(_eq_literal("new")),
            effect=PolicyEffect.DENY_IF,
            obligations=(),
            dependencies=publication.published_draft.dependencies,
            author="subject-editor",
            created_at=NOW,
        )

    forged = PolicyVersion.model_construct(**publication.version.__dict__)
    with pytest.raises(ValueError, match="registry model is invalid"):
        validate_registry_model(forged, PolicyVersion)


def test_version_state_is_derived_from_separate_immutable_revocation_fact() -> None:
    version = _version()
    head = create_activation_head(
        tenant_id="tenant-a",
        policy_set_id="invoice-read",
        bindings=(_binding(),),
    )
    active = _activate_for_test(
        head,
        version,
        expected_active_version=None,
        expected_revision=0,
        expected_head_digest=head.head_digest,
        activated_by="subject-publisher",
        activated_at=NOW,
    ).new_head
    revoked = _emergency_revoke_for_test(
        active,
        version,
        expected_active_version=1,
        expected_revision=1,
        expected_head_digest=active.head_digest,
        reason="incident-42",
        revoked_by="subject-security",
        revoked_at=NOW,
    )

    assert policy_version_state(version) is VersionState.PUBLISHED
    assert policy_version_state(version, (revoked.revocation,)) is VersionState.REVOKED
    assert version.version_digest == revoked.revocation.version_digest


def test_activation_cas_is_revision_monotonic_and_prevents_aba() -> None:
    version_1 = _version(1)
    version_2 = _version(2)
    initial = create_activation_head(
        tenant_id="tenant-a",
        policy_set_id="invoice-read",
        bindings=(_binding(),),
    )
    assert activation_revision_witness(initial) == ActivationRevisionWitness(
        tenant_id="tenant-a",
        policy_set_id="invoice-read",
        expected_active_version=None,
        expected_revision=0,
        expected_head_digest=initial.head_digest,
    )
    at_1 = _activate_for_test(
        initial,
        version_1,
        expected_active_version=None,
        expected_revision=0,
        expected_head_digest=initial.head_digest,
        activated_by="subject-publisher",
        activated_at=NOW,
    )
    at_2 = _activate_for_test(
        at_1.new_head,
        version_2,
        expected_active_version=1,
        expected_revision=1,
        expected_head_digest=at_1.new_head.head_digest,
        activated_by="subject-publisher",
        activated_at=NOW,
    )
    rolled_back = _activate_for_test(
        at_2.new_head,
        version_1,
        expected_active_version=2,
        expected_revision=2,
        expected_head_digest=at_2.new_head.head_digest,
        activated_by="subject-publisher",
        activated_at=NOW,
        event_kind=ActivationEventKind.ROLLBACK,
    )

    assert (
        at_1.new_head.revision,
        at_2.new_head.revision,
        rolled_back.new_head.revision,
    ) == (
        1,
        2,
        3,
    )
    assert rolled_back.new_head.active_version == at_1.new_head.active_version == 1
    assert rolled_back.new_head.head_digest != at_1.new_head.head_digest
    assert rolled_back.event.kind is ActivationEventKind.ROLLBACK
    with pytest.raises(ValueError, match="activation CAS mismatch"):
        _activate_for_test(
            at_2.new_head,
            version_1,
            expected_active_version=1,
            expected_revision=1,
            expected_head_digest=at_1.new_head.head_digest,
            activated_by="subject-publisher",
            activated_at=NOW,
        )


def test_emergency_revoke_replaces_atomically_or_selects_system_default_deny() -> None:
    active_version = _version(1)
    replacement = _version(2)
    initial = create_activation_head(
        tenant_id="tenant-a",
        policy_set_id="invoice-read",
        bindings=(_binding(),),
    )
    active = _activate_for_test(
        initial,
        active_version,
        expected_active_version=None,
        expected_revision=0,
        expected_head_digest=initial.head_digest,
        activated_by="subject-publisher",
        activated_at=NOW,
    ).new_head

    replaced = _emergency_revoke_for_test(
        active,
        active_version,
        expected_active_version=1,
        expected_revision=1,
        expected_head_digest=active.head_digest,
        replacement=replacement,
        reason="incident-42",
        revoked_by="subject-security",
        revoked_at=NOW,
    )
    default_deny = _emergency_revoke_for_test(
        active,
        active_version,
        expected_active_version=1,
        expected_revision=1,
        expected_head_digest=active.head_digest,
        reason="incident-43",
        revoked_by="subject-security",
        revoked_at=NOW,
    )

    assert replaced.new_head.active_version == 2
    assert replaced.new_head.revision == 2
    assert replaced.event.kind is ActivationEventKind.EMERGENCY_REVOKE
    assert default_deny.new_head.active_version is None
    assert default_deny.new_head.uses_system_default_deny is True
    assert default_deny.new_head.revision == 2


def test_cross_tenant_replacement_never_activates_in_internal_state_machine() -> None:
    version = _version(1)
    initial = create_activation_head(
        tenant_id="tenant-a",
        policy_set_id="invoice-read",
        bindings=(_binding(),),
    )
    active = _activate_for_test(
        initial,
        version,
        expected_active_version=None,
        expected_revision=0,
        expected_head_digest=initial.head_digest,
        activated_by="subject-publisher",
        activated_at=NOW,
    ).new_head
    with pytest.raises(ValueError, match="replacement is not eligible"):
        _emergency_revoke_for_test(
            active,
            version,
            expected_active_version=1,
            expected_revision=1,
            expected_head_digest=active.head_digest,
            replacement=_version(3, tenant_id="tenant-b"),
            reason="incident",
            revoked_by="subject-security",
            revoked_at=NOW,
        )


def test_authority_changing_transitions_require_exact_repository_witness() -> None:
    for transition in (
        activate_policy_version,
        rollback_policy_version,
        emergency_revoke_active_policy,
    ):
        parameters = signature(transition).parameters
        parameter = parameters["eligibility_witness"]
        assert parameter.default is Parameter.empty
        assert "eligibility_authority" not in parameters
        assert "repository_verifier" not in parameters
        assert "existing_revocations" not in parameters

    assert set(PolicyVersionEligibilityWitness.model_fields) == {
        "tenant_id",
        "policy_set_id",
        "version",
        "version_digest",
        "record_digest",
        "published",
        "not_revoked",
        "repository_witness",
        "registry_revision",
        "issuer_identity",
    }
    for forbidden_name in (
        "issue_policy_version_eligibility_witness",
        "install_policy_version_eligibility_issuer",
        "mark_policy_version_eligibility_witness",
        "_POLICY_VERSION_ELIGIBILITY_TOKEN",
        "_POLICY_VERSION_ELIGIBILITY_ISSUER",
    ):
        assert not hasattr(policy_registry, forbidden_name)


def test_public_transition_rejects_callbacks_ducks_bools_and_forged_models() -> None:
    version = _version(1)
    head = create_activation_head(
        tenant_id="tenant-a",
        policy_set_id="invoice-read",
        bindings=(_binding(),),
    )
    direct = _forged_eligibility_witness(version, registry_revision=0)

    class _WitnessSubclass(PolicyVersionEligibilityWitness):
        pass

    subclass = _WitnessSubclass(**direct.model_dump())
    constructed = PolicyVersionEligibilityWitness.model_construct(**direct.__dict__)
    candidates = (
        object(),
        True,
        _CallbackPretender(),
        direct,
        subclass,
        PolicyVersionEligibilityWitness.model_validate(direct.model_dump()),
        constructed,
        copy(direct),
        deepcopy(direct),
        pickle_loads(pickle_dumps(direct)),
    )
    for untrusted_payload in candidates:
        with pytest.raises(
            PolicyRegistryError,
            match=(
                "repository eligibility witness is invalid|"
                "repository eligibility verifier is unavailable"
            ),
        ):
            activate_policy_version(
                head,
                version,
                eligibility_witness=untrusted_payload,
                expected_active_version=None,
                expected_revision=0,
                expected_head_digest=head.head_digest,
                activated_by="subject-publisher",
                activated_at=NOW,
            )


def test_every_public_authority_transition_fails_closed_without_0039() -> None:
    version_1 = _version(1)
    version_2 = _version(2)
    initial = create_activation_head(
        tenant_id="tenant-a",
        policy_set_id="invoice-read",
        bindings=(_binding(),),
    )
    at_1 = _activate_for_test(
        initial,
        version_1,
        expected_active_version=None,
        expected_revision=0,
        expected_head_digest=initial.head_digest,
        activated_by="subject-publisher",
        activated_at=NOW,
    ).new_head
    at_2 = _activate_for_test(
        at_1,
        version_2,
        expected_active_version=1,
        expected_revision=1,
        expected_head_digest=at_1.head_digest,
        activated_by="subject-publisher",
        activated_at=NOW,
    ).new_head

    with pytest.raises(PolicyRegistryError, match="verifier is unavailable"):
        activate_policy_version(
            initial,
            version_1,
            eligibility_witness=_forged_eligibility_witness(version_1),
            expected_active_version=None,
            expected_revision=0,
            expected_head_digest=initial.head_digest,
            activated_by="subject-publisher",
            activated_at=NOW,
        )
    with pytest.raises(PolicyRegistryError, match="verifier is unavailable"):
        rollback_policy_version(
            at_2,
            version_1,
            eligibility_witness=_forged_eligibility_witness(
                version_1,
                registry_revision=2,
            ),
            expected_active_version=2,
            expected_revision=2,
            expected_head_digest=at_2.head_digest,
            activated_by="subject-publisher",
            activated_at=NOW,
        )
    with pytest.raises(PolicyRegistryError, match="verifier is unavailable"):
        emergency_revoke_active_policy(
            at_2,
            version_2,
            eligibility_witness=_forged_eligibility_witness(
                version_2,
                registry_revision=2,
            ),
            expected_active_version=2,
            expected_revision=2,
            expected_head_digest=at_2.head_digest,
            reason="incident-42",
            revoked_by="subject-security",
            revoked_at=NOW,
        )


@pytest.mark.parametrize(
    "changes",
    (
        {"tenant_id": "tenant-b"},
        {"policy_set_id": "other-policy"},
        {"version": 2},
        {"version_digest": "d" * 64},
        {"record_digest": "e" * 64},
        {"registry_revision": 1},
    ),
)
def test_public_transition_rejects_cross_bound_repository_witnesses(
    changes: dict[str, object],
) -> None:
    version = _version(1)
    head = create_activation_head(
        tenant_id="tenant-a",
        policy_set_id="invoice-read",
        bindings=(_binding(),),
    )
    witness = _forged_eligibility_witness(
        version,
        **changes,
    )
    with pytest.raises(
        PolicyRegistryError,
        match=(
            "repository eligibility witness is invalid|"
            "repository eligibility verifier is unavailable"
        ),
    ):
        activate_policy_version(
            head,
            version,
            eligibility_witness=witness,
            expected_active_version=None,
            expected_revision=0,
            expected_head_digest=head.head_digest,
            activated_by="subject-publisher",
            activated_at=NOW,
        )


def test_repository_witness_issuer_identity_is_closed() -> None:
    version = _version(1)
    for changes in (
        {"issuer_identity": "ordinary-caller"},
        {"published": False},
        {"not_revoked": False},
    ):
        with pytest.raises(ValidationError):
            _forged_eligibility_witness(
                version,
                registry_revision=0,
                **changes,
            )


def test_repository_witness_digest_binds_every_eligibility_field() -> None:
    version = _version(1)
    witness = _forged_eligibility_witness(version)
    assert witness.repository_witness == registry_digest(
        (
            ("record_type", "policy_version_eligibility_witness"),
            ("tenant_id", witness.tenant_id),
            ("policy_set_id", witness.policy_set_id),
            ("version", witness.version),
            ("version_digest", witness.version_digest),
            ("record_digest", witness.record_digest),
            ("published", witness.published),
            ("not_revoked", witness.not_revoked),
            ("registry_revision", witness.registry_revision),
            ("issuer_identity", witness.issuer_identity),
        )
    )
    with pytest.raises(ValidationError, match="repository witness"):
        _forged_eligibility_witness(
            version,
            repository_witness="f" * 64,
        )


def test_revoked_version_cannot_reactivate_without_repository_eligibility() -> None:
    version = _version(1)
    initial = create_activation_head(
        tenant_id="tenant-a",
        policy_set_id="invoice-read",
        bindings=(_binding(),),
    )
    active = _activate_for_test(
        initial,
        version,
        expected_active_version=None,
        expected_revision=0,
        expected_head_digest=initial.head_digest,
        activated_by="subject-publisher",
        activated_at=NOW,
    ).new_head
    default_deny = _emergency_revoke_for_test(
        active,
        version,
        expected_active_version=1,
        expected_revision=1,
        expected_head_digest=active.head_digest,
        reason="incident-42",
        revoked_by="subject-security",
        revoked_at=NOW,
    ).new_head

    for ordinary_fake in (
        _CallbackPretender(),
        _forged_eligibility_witness(version, registry_revision=2),
    ):
        with pytest.raises(
            PolicyRegistryError,
            match=(
                "repository eligibility witness is invalid|"
                "repository eligibility verifier is unavailable"
            ),
        ):
            activate_policy_version(
                default_deny,
                version,
                eligibility_witness=ordinary_fake,
                expected_active_version=None,
                expected_revision=2,
                expected_head_digest=default_deny.head_digest,
                activated_by="subject-publisher",
                activated_at=NOW,
            )


def test_rollback_requires_a_strictly_older_active_version() -> None:
    version_1 = _version(1)
    version_2 = _version(2)
    initial = create_activation_head(
        tenant_id="tenant-a",
        policy_set_id="invoice-read",
        bindings=(_binding(),),
    )
    with pytest.raises(PolicyRegistryError, match="rollback target must be older"):
        _activate_for_test(
            initial,
            version_1,
            expected_active_version=None,
            expected_revision=0,
            expected_head_digest=initial.head_digest,
            activated_by="subject-publisher",
            activated_at=NOW,
            event_kind=ActivationEventKind.ROLLBACK,
        )

    at_1 = _activate_for_test(
        initial,
        version_1,
        expected_active_version=None,
        expected_revision=0,
        expected_head_digest=initial.head_digest,
        activated_by="subject-publisher",
        activated_at=NOW,
    ).new_head
    for target in (version_1, version_2):
        with pytest.raises(PolicyRegistryError, match="rollback target must be older"):
            _activate_for_test(
                at_1,
                target,
                expected_active_version=1,
                expected_revision=1,
                expected_head_digest=at_1.head_digest,
                activated_by="subject-publisher",
                activated_at=NOW,
                event_kind=ActivationEventKind.ROLLBACK,
            )


def test_publication_and_transition_aggregates_reject_cross_object_facts() -> None:
    draft_a = _draft(tenant_id="tenant-a")
    publication_a = publish_draft_revision(
        draft_a,
        expected_revision=1,
        expected_digest=draft_a.draft_digest,
        version=1,
        published_by="subject-publisher",
        published_at=NOW,
    )
    draft_b = _draft(tenant_id="tenant-b")
    publication_b = publish_draft_revision(
        draft_b,
        expected_revision=1,
        expected_digest=draft_b.draft_digest,
        version=1,
        published_by="subject-publisher",
        published_at=NOW,
    )
    with pytest.raises(ValidationError, match="publication facts are inconsistent"):
        PolicyPublication(
            published_draft=publication_a.published_draft,
            version=publication_b.version,
        )

    version_1 = publication_a.version
    version_2 = _version(2)
    initial = create_activation_head(
        tenant_id="tenant-a",
        policy_set_id="invoice-read",
        bindings=(_binding(),),
    )
    at_1 = _activate_for_test(
        initial,
        version_1,
        expected_active_version=None,
        expected_revision=0,
        expected_head_digest=initial.head_digest,
        activated_by="subject-publisher",
        activated_at=NOW,
    )
    at_2 = _activate_for_test(
        at_1.new_head,
        version_2,
        expected_active_version=1,
        expected_revision=1,
        expected_head_digest=at_1.new_head.head_digest,
        activated_by="subject-publisher",
        activated_at=NOW,
    )
    with pytest.raises(ValidationError, match="activation facts are inconsistent"):
        PolicyActivationTransition(new_head=at_2.new_head, event=at_1.event)

    forged = PolicyActivationTransition.model_construct(
        new_head=at_2.new_head,
        event=at_1.event,
    )
    with pytest.raises(PolicyRegistryError, match="registry model is invalid"):
        validate_registry_model(forged, PolicyActivationTransition)


def test_emergency_aggregate_binds_revocation_event_and_default_deny_head() -> None:
    version_1 = _version(1)
    version_2 = _version(2)
    initial = create_activation_head(
        tenant_id="tenant-a",
        policy_set_id="invoice-read",
        bindings=(_binding(),),
    )
    active = _activate_for_test(
        initial,
        version_1,
        expected_active_version=None,
        expected_revision=0,
        expected_head_digest=initial.head_digest,
        activated_by="subject-publisher",
        activated_at=NOW,
    ).new_head
    replaced = _emergency_revoke_for_test(
        active,
        version_1,
        expected_active_version=1,
        expected_revision=1,
        expected_head_digest=active.head_digest,
        replacement=version_2,
        reason="incident-42",
        revoked_by="subject-security",
        revoked_at=NOW,
    )
    default_deny = _emergency_revoke_for_test(
        active,
        version_1,
        expected_active_version=1,
        expected_revision=1,
        expected_head_digest=active.head_digest,
        reason="incident-43",
        revoked_by="subject-security",
        revoked_at=NOW,
    )

    assert (
        validate_registry_model(
            default_deny,
            PolicyEmergencyRevocation,
        )
        == default_deny
    )
    with pytest.raises(ValidationError, match="emergency facts are inconsistent"):
        PolicyEmergencyRevocation(
            new_head=default_deny.new_head,
            event=replaced.event,
            revocation=default_deny.revocation,
        )
    with pytest.raises(ValidationError, match="emergency facts are inconsistent"):
        PolicyEmergencyRevocation(
            new_head=replaced.new_head,
            event=default_deny.event,
            revocation=replaced.revocation,
        )


def test_activation_head_rejects_duplicate_or_cross_tenant_bindings() -> None:
    with pytest.raises(ValueError, match="activation head is invalid"):
        create_activation_head(
            tenant_id="tenant-a",
            policy_set_id="invoice-read",
            bindings=(_binding(), _binding()),
        )
    with pytest.raises(ValueError, match="activation head is invalid"):
        create_activation_head(
            tenant_id="tenant-a",
            policy_set_id="invoice-read",
            bindings=(_binding(tenant_id="tenant-b"),),
        )


def test_new_registry_modules_never_accept_or_persist_compiled_sql_plan() -> None:
    source_root = Path(policy_package.__file__).parent
    for path in (source_root / "registry.py", source_root / "canonical.py"):
        source = path.read_text(encoding="utf-8")
        assert "CompiledSqlPlan" not in source
        assert "compile_policy_sql" not in source
        assert "jsonb" not in source.lower()

    assert "canonical_ast_utf8" in PolicyVersion.model_fields
    assert "canonical_ast_sha256" in PolicyVersion.model_fields
    assert not {
        "db_attested",
        "repository_attested",
        "attestation",
        "signature",
    } & set(ActivationRevisionWitness.model_fields)


def test_internal_transition_state_machine_is_not_a_production_import_surface() -> None:
    internal_entries = (
        "_transition_to_version_internal",
        "_emergency_revoke_active_policy_internal",
    )
    source_root = Path(policy_package.__file__).parent.parent
    registry_path = Path(policy_registry.__file__)
    for path in source_root.rglob("*.py"):
        if path == registry_path:
            continue
        source = path.read_text(encoding="utf-8")
        for entry in internal_entries:
            assert entry not in source, f"{path} imports private policy state machine"
    for entry in internal_entries:
        assert entry not in policy_registry.__all__
        assert not hasattr(policy_package, entry)
