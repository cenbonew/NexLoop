from datetime import timedelta
from types import SimpleNamespace

import pytest

from eios.authz.applications import ApplicationMode
from eios.authz.facts import (
    ApplicationFacts,
    AuthorizationFactsResolver,
    ControlFacts,
    EffectiveGrantFact,
    GrantFacts,
    PolicyFacts,
    PolicyRuleFact,
    ResourceGraphFacts,
    ScopeAuthorityFacts,
)
from eios.authz.applications import OperationRestriction, ResourceRestriction
from eios.authz.intersection import DecisionReason, evaluate_intersection
from eios.authz.operations import Operation
from eios.authz.policy import Eq, LiteralValue
from eios.authz.policy.canonical import canonicalize_policy_expression
from eios.authz.policy.models import SubjectRef
from eios.authz.policy.registry import PolicyEffect
from eios.authz.resources import ResourceType

from test_eios_facts import (
    DIGEST_A,
    NOW,
    RecordingProvider,
    direct_query,
    reference,
    repository_fields,
)


def _resolved(*, include_dependency_grant: bool = True):
    query = direct_query()
    provider = RecordingProvider(query)
    if include_dependency_grant:
        grants = provider.unit_of_work.grants
        provider.unit_of_work.grants = GrantFacts(
            **{
                **grants.model_dump(
                    exclude={"snapshot_digest", "repository_witness", "grants"}
                ),
                "grants": grants.grants
                + (
                    EffectiveGrantFact(
                        grant_id="grant-connector",
                        subject_kind=grants.subject_kind,
                        subject_id=grants.principal_id,
                        role_id="role-reader",
                        role_revision=5,
                        role_digest="2" * 64,
                        resource=reference(ResourceType.PROJECT, "main", 2),
                        effective_resource=reference(ResourceType.CONNECTOR, "crm", 4),
                        operations=frozenset({Operation.READ}),
                        inherited=True,
                        inherits=True,
                        valid_from=NOW - timedelta(days=1),
                        valid_until=NOW + timedelta(minutes=20),
                        revision=10,
                        digest=DIGEST_A,
                    ),
                ),
                **repository_fields(),
            }
        )
    return AuthorizationFactsResolver(provider).resolve(query)


def test_intersection_requires_a_grant_for_every_dependency() -> None:
    result = evaluate_intersection(_resolved(include_dependency_grant=False))

    assert not result.allowed
    assert DecisionReason.RESOURCE_GRANT_MISSING in result.reason_codes


def test_intersection_allows_only_when_every_authority_layer_matches() -> None:
    result = evaluate_intersection(_resolved())

    assert result.allowed
    assert result.reason_codes == (DecisionReason.INTERSECTION_MATCH,)
    assert set(result.matched_grants) == {
        "grant-reader",
        "grant-group-reader",
        "grant-connector",
    }
    assert result.expires_at == NOW + timedelta(minutes=20)


def test_intersection_is_pure_and_does_not_create_authority_from_policy() -> None:
    resolved = _resolved(include_dependency_grant=False)

    first = evaluate_intersection(resolved)
    second = evaluate_intersection(resolved)

    assert first == second
    assert not first.allowed


def _no_dependency_context(**overrides: object) -> SimpleNamespace:
    """Sealed-context stand-in for pure intersection deny-path tests.

    The resolver now fails closed before handing over snapshots whose scope,
    control or policy authorities do not cover the resource graph, so the
    intersection's own per-target bottom lines can only be exercised against a
    directly constructed context object.
    """
    query = direct_query()
    unit_of_work = RecordingProvider(query).unit_of_work
    graph = unit_of_work.resource_graph
    values: dict[str, object] = {
        "trusted_now": NOW,
        "query": query,
        "subject": unit_of_work.subject,
        "actor": unit_of_work.actor,
        "caller_application": unit_of_work.caller_application,
        "agent_application": None,
        "agent_release": None,
        "resource_graph": ResourceGraphFacts(
            **{
                **graph.model_dump(
                    exclude={"repository_witness", "snapshot_digest", "dependencies"}
                ),
                "dependencies": (),
                **repository_fields(),
            }
        ),
        "grants": unit_of_work.grants,
        "controls": unit_of_work.controls,
        "policies": unit_of_work.policies,
        "scope_authority": unit_of_work.scope_authority,
        "subject_authority": unit_of_work.subject_authority,
        "authoritative_expires_at": None,
    }
    values.update(overrides)
    context = SimpleNamespace(**values)
    context.verify_integrity = lambda: None
    return context


def _without(facts: object, model: type, field: str, replacement: object) -> object:
    return model(
        **{
            **facts.model_dump(
                exclude={"repository_witness", "snapshot_digest", field}
            ),
            field: replacement,
            **repository_fields(),
        }
    )


def test_factless_decisions_expire_strictly_after_trusted_now() -> None:
    """No fact carries an expiry (production: every API key has expires_at NULL).

    The audit record refuses ``expires_at <= authoritative_now``; deciding with
    expiry == now turned every such API-key execution into a 503 (2026-10-03).
    Literal bounds, mirroring the decision cache's base TTL (allow 5 s, deny 1 s).
    """

    grants = _without(
        _no_dependency_context().grants,
        type(_no_dependency_context().grants),
        "grants",
        tuple(
            type(grant).model_validate(dict(grant.model_dump(), valid_until=None))
            for grant in _no_dependency_context().grants.grants
        ),
    )
    allowed = evaluate_intersection(_no_dependency_context(grants=grants))
    assert allowed.allowed
    assert allowed.expires_at == NOW + timedelta(seconds=5)

    name, overrides, reason = _deny_scenarios()[0]
    denied = evaluate_intersection(_no_dependency_context(**overrides))
    assert not denied.allowed and denied.reason_codes == (reason,), name
    assert denied.expires_at == NOW + timedelta(seconds=1)


def test_fake_context_baseline_allows() -> None:
    result = evaluate_intersection(_no_dependency_context())

    assert result.allowed
    assert result.reason_codes == (DecisionReason.INTERSECTION_MATCH,)


def _deny_scenarios() -> list[tuple[str, dict[str, object], DecisionReason]]:
    query = direct_query()
    unit_of_work = RecordingProvider(query).unit_of_work
    scenarios: list[tuple[str, dict[str, object], DecisionReason]] = []

    scenarios.append(
        (
            "policy_default_deny_without_rules",
            {
                "policies": _without(
                    unit_of_work.policies, PolicyFacts, "rules", ()
                )
            },
            DecisionReason.POLICY_DEFAULT_DENY,
        )
    )
    scenarios.append(
        (
            "scope_denied_without_target_authorities",
            {
                "scope_authority": _without(
                    unit_of_work.scope_authority,
                    ScopeAuthorityFacts,
                    "authorities",
                    tuple(
                        authority
                        for authority in unit_of_work.scope_authority.authorities
                        if authority.resource_type is not ResourceType.DATASET
                    ),
                )
            },
            DecisionReason.SCOPE_DENIED,
        )
    )
    scenarios.append(
        (
            "control_denied_without_requirements",
            {
                "controls": _without(
                    unit_of_work.controls,
                    ControlFacts,
                    "resource_requirements",
                    (),
                )
            },
            DecisionReason.CONTROL_DENIED,
        )
    )
    scenarios.append(
        (
            "control_denied_without_satisfying_clearance",
            {
                "controls": _without(
                    unit_of_work.controls,
                    ControlFacts,
                    "subject_clearances",
                    (),
                )
            },
            DecisionReason.CONTROL_DENIED,
        )
    )
    scenarios.append(
        (
            "grant_missing_when_inherited_row_does_not_inherit",
            {
                "grants": _without(
                    unit_of_work.grants,
                    GrantFacts,
                    "grants",
                    tuple(
                        EffectiveGrantFact(
                            **{
                                **grant.model_dump(
                                    exclude={"inherited", "inherits"}
                                ),
                                "inherited": True,
                                "inherits": False,
                            }
                        )
                        for grant in unit_of_work.grants.grants
                    ),
                )
            },
            DecisionReason.RESOURCE_GRANT_MISSING,
        )
    )
    root_rule = next(
        rule
        for rule in unit_of_work.policies.rules
        if rule.resource.resource_type is ResourceType.DATASET
    )
    deny_rule = root_rule.model_copy(
        update={
            "binding_id": "binding-deny-override",
            "effect": PolicyEffect.DENY_IF,
        }
    )
    scenarios.append(
        (
            "policy_denied_deny_override_wins",
            {
                "policies": _without(
                    unit_of_work.policies,
                    PolicyFacts,
                    "rules",
                    (*unit_of_work.policies.rules, deny_rule),
                )
            },
            DecisionReason.POLICY_DENIED,
        )
    )
    return scenarios


@pytest.mark.parametrize(
    ("name", "overrides", "reason"),
    _deny_scenarios(),
    ids=[name for name, _, _ in _deny_scenarios()],
)
def test_intersection_denies_each_authority_layer(
    name: str, overrides: dict[str, object], reason: DecisionReason
) -> None:
    result = evaluate_intersection(_no_dependency_context(**overrides))

    assert not result.allowed
    assert reason in result.reason_codes


def test_unrepresentable_subject_attribute_degrades_instead_of_denying() -> None:
    query = direct_query()
    unit_of_work = RecordingProvider(query).unit_of_work
    poisoned = SimpleNamespace(
        attributes={
            "department": "finance",
            "broken_none": None,
            "broken_nested": {"deep": "value"},
            "broken_empty": "",
        }
    )

    result = evaluate_intersection(
        _no_dependency_context(subject_authority=poisoned)
    )

    assert result.allowed
    assert result.reason_codes == (DecisionReason.INTERSECTION_MATCH,)
    del unit_of_work


def test_policy_referencing_dropped_attribute_still_fails_closed() -> None:
    query = direct_query()
    unit_of_work = RecordingProvider(query).unit_of_work
    requires_dropped_expression = Eq(
        node_id="requires-dropped",
        op="eq",
        left=SubjectRef(kind="subject", attribute="broken_none"),
        right=LiteralValue(kind="literal", value="finance"),
    )
    requires_dropped = canonicalize_policy_expression(requires_dropped_expression)
    rule = next(
        candidate
        for candidate in unit_of_work.policies.rules
        if candidate.resource.resource_type is ResourceType.DATASET
    )
    strict_rule = PolicyRuleFact(
        **{
            **rule.model_dump(
                exclude={
                    "binding_id",
                    "canonical_ast_utf8",
                    "canonical_ast_sha256",
                    "condition",
                }
            ),
            "binding_id": "binding-requires-dropped",
            "canonical_ast_utf8": requires_dropped.utf8,
            "canonical_ast_sha256": requires_dropped.sha256,
            "condition": requires_dropped_expression,
        }
    )
    context = _no_dependency_context(
        policies=_without(
            unit_of_work.policies, PolicyFacts, "rules", (strict_rule,)
        ),
        subject_authority=SimpleNamespace(
            attributes={"department": "finance", "broken_none": None}
        ),
    )

    result = evaluate_intersection(context)

    assert not result.allowed
    assert DecisionReason.POLICY_DENIED in result.reason_codes


def _break_glass_application(
    template: ApplicationFacts,
    *,
    resources: tuple[ResourceRestriction, ...],
    operations: tuple[OperationRestriction, ...],
) -> ApplicationFacts:
    return ApplicationFacts(
        **{
            **template.model_dump(
                exclude={
                    "repository_witness",
                    "snapshot_digest",
                    "mode",
                    "break_glass",
                    "resources",
                    "operations",
                    "published_at",
                    "expires_at",
                }
            ),
            "mode": ApplicationMode.UNRESTRICTED,
            "break_glass": True,
            "resources": resources,
            "operations": operations,
            "published_at": NOW - timedelta(minutes=5),
            "expires_at": NOW + timedelta(minutes=5),
            **repository_fields(),
        }
    )


def test_break_glass_still_honours_exact_allowlists() -> None:
    query = direct_query()
    unit_of_work = RecordingProvider(query).unit_of_work
    template = unit_of_work.caller_application

    matching = _break_glass_application(
        template,
        resources=template.resources,
        operations=template.operations,
    )
    context = _no_dependency_context(caller_application=matching)
    assert evaluate_intersection(context).allowed

    off_target = _break_glass_application(
        template,
        resources=(
            ResourceRestriction(
                tenant_id="tenant-a",
                resource_type=ResourceType.CONNECTOR,
                resource_id="eios:connector:other",
            ),
        ),
        operations=template.operations,
    )
    denied = evaluate_intersection(
        _no_dependency_context(caller_application=off_target)
    )
    assert not denied.allowed
    assert DecisionReason.APPLICATION_CEILING_DENIED in denied.reason_codes
