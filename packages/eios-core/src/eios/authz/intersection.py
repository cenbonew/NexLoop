from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from logging import getLogger
from typing import NamedTuple

from .applications import ApplicationMode
from .facts import ControlDominance
from .policy import EvaluationContext, evaluate_policy, parse_policy_expression
from .policy.registry import apply_policy_effect

#: When no authorization fact carries an expiry, a decision still has to expire
#: strictly AFTER the trusted instant: the audit record (authz.audit) refuses
#: ``expires_at <= authoritative_now``.  ``min(expiries) if expiries else now``
#: expired every such decision at ``now`` exactly -- in production every API key
#: has expires_at NULL, so every API-key execution answered 503 (2026-10-03).
#: The bound mirrors the decision cache's base TTL (cache.py: allow 5 s, deny
#: 1 s), so a fact-less decision never outlives what the cache would grant.
FACTLESS_ALLOW_SECONDS = 5
FACTLESS_DENY_SECONDS = 1


_logger = getLogger(__name__)


class DecisionReason(str, Enum):
    INTERSECTION_MATCH = "intersection_match"
    RESOURCE_GRANT_MISSING = "resource_grant_missing"
    APPLICATION_CEILING_DENIED = "application_ceiling_denied"
    SCOPE_DENIED = "scope_denied"
    CONTROL_DENIED = "control_denied"
    POLICY_DEFAULT_DENY = "policy_default_deny"
    POLICY_DENIED = "policy_denied"
    AUTHORITY_INVALID = "authority_invalid"


class IntersectionResult(NamedTuple):
    allowed: bool
    reason_codes: tuple[DecisionReason, ...]
    matched_grants: tuple[str, ...]
    matched_policies: tuple[str, ...]
    obligations: tuple[object, ...]
    expires_at: datetime


def _resource_key(resource: object) -> tuple[object, object, object]:
    return (resource.tenant_id, resource.resource_type, resource.resource_id)


def _application_allows(
    application: object, resource: object, operation: object, now: datetime
) -> bool:
    if not application.eligible or application.application_status != "active":
        return False
    if application.version_status != "published":
        return False
    if application.expires_at is not None and application.expires_at <= now:
        return False
    if application.mode is ApplicationMode.UNRESTRICTED and not (
        application.break_glass
    ):
        # An unrestricted version that is not an approved break-glass version
        # never widens authority.
        return False
    # Break-glass versions carry mandatory non-empty exact allowlists (domain
    # contract); they are matched exactly like restricted versions instead of
    # bypassing the ceiling.
    key = (resource.tenant_id, resource.resource_type.value, resource.resource_id)
    # (tenant, type, None) = 该类型的任意资源(类型级通配,DB 0159)。只在应用
    # **显式声明**了这样一行时才放行;粒度控制仍由委托 ceiling、角色授予、
    # 审批步与逐资源管控要求把守。
    wildcard = (resource.tenant_id, resource.resource_type.value, None)
    return (
        key in application.allowed_resource_keys
        or wildcard in application.allowed_resource_keys
    ) and operation in application.allowed_operations


def _control_satisfied(clearance: object, requirement: object, now: datetime) -> bool:
    if (
        clearance.kind is not requirement.kind
        or clearance.namespace != requirement.namespace
    ):
        return False
    if clearance.valid_until is not None and clearance.valid_until <= now:
        return False
    if requirement.dominance is ControlDominance.EXACT:
        return clearance.value == requirement.value
    if requirement.dominance is ControlDominance.RANK:
        return clearance.rank >= requirement.minimum_rank
    return (
        clearance.value == requirement.value or requirement.value in clearance.dominates
    )


def _representable_subject_attributes(
    attributes: object, *, now: datetime
) -> dict[str, object]:
    """Keep only attributes the policy evaluator can represent.

    Subject attributes come from the trusted repository, but their value shapes
    are not under the caller's control.  A single unrepresentable value (None,
    nested object, empty string, ...) must not fail the whole decision closed;
    it is degraded by dropping only that attribute.  Policies referencing a
    dropped attribute still evaluate fail-closed (missing attribute).
    """
    representable: dict[str, object] = {}
    if type(attributes) is not dict:
        try:
            attributes = dict(attributes)  # type: ignore[call-overload]
        except Exception:
            return representable
    for name, value in attributes.items():
        if type(value) is list:
            value = tuple(value)
        try:
            EvaluationContext(
                subject={name: value},
                application={},
                resource={},
                object_properties={},
                action_parameters={},
                trusted_now=now,
            )
        except Exception:
            continue
        representable[name] = value
    return representable


def _deny(
    reason: DecisionReason,
    context: object,
    *,
    grants: set[str] | None = None,
    policies: set[str] | None = None,
) -> IntersectionResult:
    return IntersectionResult(
        False,
        (reason,),
        tuple(sorted(grants or ())),
        tuple(sorted(policies or ())),
        (),
        context.authoritative_expires_at
        or context.trusted_now + timedelta(seconds=FACTLESS_DENY_SECONDS),
    )


def _evaluate_payload(context: object) -> IntersectionResult:
    """Pure fail-closed intersection over one sealed authorization snapshot."""
    try:
        now = context.trusted_now
        query = context.query
        if query.tenant_id != context.subject.tenant_id:
            return _deny(DecisionReason.AUTHORITY_INVALID, context)
        targets = [(context.resource_graph.root.resource, query.target.operation)]
        targets.extend(
            (edge.target, edge.required_operation)
            for edge in context.resource_graph.dependencies
        )
        applications = [context.caller_application]
        if context.agent_application is not None:
            applications.append(context.agent_application)
        if context.agent_release is not None:
            applications.extend(
                parent.application for parent in context.agent_release.parent_chain
            )

        matched_grants: set[str] = set()
        matched_policies: set[str] = set()
        obligations: list[object] = []
        expiries = (
            [context.authoritative_expires_at]
            if context.authoritative_expires_at
            else []
        )
        subject = _representable_subject_attributes(
            context.subject_authority.attributes, now=now
        )
        application_values = {
            "caller_application_id": context.caller_application.application_id,
            "actor_kind": context.actor.kind.value,
        }

        if (
            not context.grants.complete
            or not context.controls.complete
            or not context.policies.complete
        ):
            return _deny(DecisionReason.AUTHORITY_INVALID, context)
        if (
            not context.scope_authority.required_scopes
            <= context.scope_authority.authorized_scopes
        ):
            return _deny(DecisionReason.SCOPE_DENIED, context)

        for resource, operation in targets:
            grants = tuple(
                grant
                for grant in context.grants.grants
                if _resource_key(grant.effective_resource) == _resource_key(resource)
                and operation in grant.operations
                and not (grant.inherited and not grant.inherits)
                and grant.valid_from <= now
                and (grant.valid_until is None or now < grant.valid_until)
            )
            if not grants:
                return _deny(
                    DecisionReason.RESOURCE_GRANT_MISSING,
                    context,
                    grants=matched_grants,
                )
            matched_grants.update(grant.grant_id for grant in grants)
            expiries.extend(
                grant.valid_until for grant in grants if grant.valid_until is not None
            )

            if any(
                not _application_allows(app, resource, operation, now)
                for app in applications
            ):
                return _deny(
                    DecisionReason.APPLICATION_CEILING_DENIED,
                    context,
                    grants=matched_grants,
                )

            scoped = any(
                authority.scope in context.scope_authority.authorized_scopes
                and authority.resource_type is resource.resource_type
                and authority.operation is operation
                and authority.resource_id in (None, resource.resource_id)
                for authority in context.scope_authority.authorities
            )
            if not scoped:
                return _deny(
                    DecisionReason.SCOPE_DENIED, context, grants=matched_grants
                )

            requirements = tuple(
                requirement
                for requirement in context.controls.resource_requirements
                if _resource_key(requirement.resource) == _resource_key(resource)
                and requirement.operation is operation
                and (requirement.valid_until is None or now < requirement.valid_until)
            )
            if not requirements:
                return _deny(
                    DecisionReason.CONTROL_DENIED, context, grants=matched_grants
                )
            for requirement in requirements:
                if not any(
                    _control_satisfied(clearance, requirement, now)
                    for clearance in context.controls.subject_clearances
                ):
                    return _deny(
                        DecisionReason.CONTROL_DENIED, context, grants=matched_grants
                    )

            rules = tuple(
                rule
                for rule in context.policies.rules
                if _resource_key(rule.resource) == _resource_key(resource)
                and rule.operation is operation
                and rule.published_at <= now
            )
            if not rules:
                return _deny(
                    DecisionReason.POLICY_DEFAULT_DENY, context, grants=matched_grants
                )
            for rule in rules:
                evaluation = evaluate_policy(
                    parse_policy_expression(rule.canonical_ast_utf8),
                    EvaluationContext(
                        subject=subject,
                        application=application_values,
                        resource={
                            "tenant_id": resource.tenant_id,
                            "resource_id": resource.resource_id,
                            "resource_type": resource.resource_type.value,
                            "operation": operation.value,
                        },
                        object_properties={},
                        action_parameters=dict(query.request_attributes),
                        trusted_now=now,
                    ),
                )
                narrowed = apply_policy_effect(
                    effect=rule.effect,
                    evaluation=evaluation,
                    declared_obligations=rule.obligations,
                )
                matched_policies.add(rule.binding_id)
                if narrowed.must_deny:
                    return _deny(
                        DecisionReason.POLICY_DENIED,
                        context,
                        grants=matched_grants,
                        policies=matched_policies,
                    )
                obligations.extend(narrowed.obligations)

        return IntersectionResult(
            True,
            (DecisionReason.INTERSECTION_MATCH,),
            tuple(sorted(matched_grants)),
            tuple(sorted(matched_policies)),
            tuple(obligations),
            min(expiries) if expiries else now + timedelta(seconds=FACTLESS_ALLOW_SECONDS),
        )
    except Exception as error:
        try:
            target = context.query.target
            target_key = (
                f"{target.resource_type.value}:{target.resource_id}"
                f"#{target.operation.value}"
            )
        except Exception:
            target_key = "<unavailable>"
        # Attribute values are never logged; only the exception type and the
        # requested target are recorded so fail-closed denials stay diagnosable.
        _logger.warning(
            "authorization intersection failed closed: %s target=%s",
            type(error).__name__,
            target_key,
        )
        try:
            return _deny(DecisionReason.AUTHORITY_INVALID, context)
        except Exception:
            raise TypeError(
                "authorization context is not a sealed resolved snapshot"
            ) from None


def evaluate_intersection(context: object) -> IntersectionResult:
    # Integrity is checked before every public projection is consumed.  Decision
    # code deliberately evaluates the immutable canonical policy bytes rather
    # than reaching through the resolver's private payload boundary.
    context.verify_integrity()
    return _evaluate_payload(context)


__all__ = ["DecisionReason", "IntersectionResult", "evaluate_intersection"]
