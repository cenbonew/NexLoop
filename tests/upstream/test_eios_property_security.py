from __future__ import annotations

import pytest

from eios.authz.property_security import (
    InMemoryPropertyAuditSink,
    InMemoryPropertyDecisionProvider,
    PropertyAuthorizationUnavailable,
    PropertyOperation,
    PropertyRule,
    PropertySecurityProjector,
    PropertySubjectContext,
    PropertyUseDenied,
    RedactionContract,
)
def _context(
    tenant: str = "tenant-a", principal: str = "alice"
) -> PropertySubjectContext:
    return PropertySubjectContext(
        tenant_id=tenant,
        principal_id=principal,
        api_key_id="key-1",
        request_id="request-1",
        trace_id="trace-1",
    )


def _rules() -> tuple[PropertyRule, ...]:
    return (
        PropertyRule(
            policy_id="policy-public",
            tenant_id="tenant-a",
            object_type="Employee",
            property_name="name",
            operations=frozenset(PropertyOperation),
            effect="allow",
        ),
        PropertyRule(
            policy_id="policy-payroll",
            tenant_id="tenant-a",
            object_type="Employee",
            property_name="salary",
            operations=frozenset(PropertyOperation),
            effect="allow",
            subject_attribute=("api_key_id", "payroll-key"),
            object_attribute=("region", "CN"),
        ),
    )


def test_property_projection_is_tenant_subject_instance_and_property_aware() -> None:
    audit = InMemoryPropertyAuditSink()
    projector = PropertySecurityProjector(
        InMemoryPropertyDecisionProvider(_rules()), audit_sink=audit
    )

    assert projector.project(
        _context(),
        object_type="Employee",
        object_id="employee-1",
        properties={"name": "Ada", "salary": 100, "region": "CN"},
    ) == {"name": "Ada"}
    assert [record[0].resource.property_name for record in audit.records] == [
        "name",
        "salary",
        "region",
    ]
    assert audit.records[1][1].reason_codes == ("property_no_matching_allow",)


def test_property_projection_supports_explicit_null_contract() -> None:
    projector = PropertySecurityProjector(
        InMemoryPropertyDecisionProvider(_rules()),
        redaction=RedactionContract.NULL,
    )
    assert projector.project(
        _context(),
        object_type="Employee",
        object_id="employee-1",
        properties={"name": "Ada", "salary": 100},
    ) == {"name": "Ada", "salary": None}


@pytest.mark.parametrize(
    ("argument", "operation"),
    [
        ("filter_properties", PropertyOperation.FILTER),
        ("sort_properties", PropertyOperation.SORT),
        ("export_properties", PropertyOperation.EXPORT),
    ],
)
def test_unauthorized_property_cannot_be_filter_sort_or_export_side_channel(
    argument: str, operation: PropertyOperation
) -> None:
    audit = InMemoryPropertyAuditSink()
    projector = PropertySecurityProjector(
        InMemoryPropertyDecisionProvider(_rules()), audit_sink=audit
    )
    with pytest.raises(PropertyUseDenied):
        projector.require_query_properties(
            _context(),
            object_type="Employee",
            object_id="employee-1",
            properties={"name": "Ada", "salary": 100},
            **{argument: ("salary",)},
        )
    assert audit.records[-1][0].operation is operation
    assert audit.records[-1][1].allowed is False


def test_provider_or_audit_failure_is_fail_closed() -> None:
    class Broken:
        def decide(self, query: object) -> object:
            raise RuntimeError("offline")

    projector = PropertySecurityProjector(Broken())  # type: ignore[arg-type]
    with pytest.raises(PropertyAuthorizationUnavailable):
        projector.project(
            _context(),
            object_type="Employee",
            object_id="employee-1",
            properties={"name": "Ada"},
        )


def test_cross_tenant_rules_never_match() -> None:
    projector = PropertySecurityProjector(
        InMemoryPropertyDecisionProvider(_rules())
    )
    assert (
        projector.project(
            _context("tenant-b"),
            object_type="Employee",
            object_id="employee-1",
            properties={"name": "Ada"},
        )
        == {}
    )


def test_explain_returns_the_same_audited_policy_evidence() -> None:
    audit = InMemoryPropertyAuditSink()
    projector = PropertySecurityProjector(
        InMemoryPropertyDecisionProvider(_rules()), audit_sink=audit
    )
    decision = projector.explain(
        _context(),
        object_type="Employee",
        object_id="employee-1",
        property_name="name",
        properties={"name": "Ada"},
    )
    assert decision.allowed is True
    assert decision.policy_ids == ("policy-public",)
    assert audit.records[-1][1] == decision


def test_readable_names_do_not_treat_null_redaction_as_dependency_authority() -> None:
    projector = PropertySecurityProjector(
        InMemoryPropertyDecisionProvider(_rules()),
        redaction=RedactionContract.NULL,
    )
    assert projector.readable_property_names(
        _context(),
        object_type="Employee",
        object_id="employee-1",
        properties={"name": "Ada", "salary": 100},
    ) == frozenset({"name"})
