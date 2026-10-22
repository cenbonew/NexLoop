from __future__ import annotations

import pytest
from pydantic import ValidationError

from eios.authz.context import AuthorizationContext


EXPECTED_CONTEXT_FIELDS = {
    "tenant_id",
    "session_id",
    "subject_principal_id",
    "actor_principal_id",
    "application_id",
    "application_version",
    "agent_id",
    "credential_id",
    "authentication_method",
    "requested_scopes",
    "groups",
    "roles",
    "attributes",
    "clearances",
    "membership_revision",
    "grant_revision",
    "clearance_revision",
    "policy_revision",
    "request_id",
    "trace_id",
}


def delegated_context(
    *, subject: str = "human-1", actor: str = "agent-1", application: str = "app-1"
) -> AuthorizationContext:
    return AuthorizationContext(
        tenant_id="tenant-1",
        session_id="session-1",
        subject_principal_id=subject,
        actor_principal_id=actor,
        application_id=application,
        application_version="app-version-1",
        agent_id="agent-1",
        credential_id=None,
        authentication_method="oidc",
        requested_scopes=frozenset({"ontology.read"}),
        groups=frozenset({"finance"}),
        roles=frozenset({"analyst"}),
        attributes={"department": "finance", "regions": ["cn"]},
        clearances=frozenset({"internal"}),
        membership_revision=3,
        grant_revision=4,
        clearance_revision=5,
        policy_revision=6,
        request_id="request-1",
        trace_id="trace-1",
    )


def test_context_contains_every_frozen_specification_field() -> None:
    assert set(AuthorizationContext.model_fields) == EXPECTED_CONTEXT_FIELDS


def test_context_separates_subject_actor_and_application() -> None:
    context = delegated_context(subject="human-1", actor="agent-1", application="app-1")
    assert context.subject_principal_id == "human-1"
    assert context.actor_principal_id == "agent-1"
    assert context.application_id == "app-1"
    assert context.agent_id == "agent-1"


def test_context_is_strict_frozen_extra_forbid_and_deeply_immutable() -> None:
    context = delegated_context()

    with pytest.raises(ValidationError, match="frozen"):
        context.actor_principal_id = "human-1"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        AuthorizationContext(**{**context.model_dump(), "caller_id": "forged"})
    with pytest.raises(ValidationError, match="frozen_set_type"):
        AuthorizationContext(**{**context.model_dump(), "groups": ["finance"]})
    with pytest.raises(TypeError):
        context.attributes["department"] = "engineering"
    assert context.attributes["regions"] == ("cn",)


def test_context_detaches_attribute_aliases_and_is_hashable_for_cache_keys() -> None:
    attributes = {"department": "finance", "regions": ["cn"]}
    context = AuthorizationContext(
        **{**delegated_context().model_dump(), "attributes": attributes}
    )
    attributes["department"] = "engineering"
    attributes["regions"].append("us")

    assert context.attributes["department"] == "finance"
    assert context.attributes["regions"] == ("cn",)
    assert {context: "decision"}[context] == "decision"


def test_context_requires_a_session_or_credential_binding() -> None:
    context = delegated_context()
    with pytest.raises(ValidationError, match="session_id or credential_id"):
        AuthorizationContext(
            **{
                **context.model_dump(),
                "session_id": None,
                "credential_id": None,
            }
        )


def test_human_session_does_not_collapse_subject_actor_and_application() -> None:
    context = AuthorizationContext(
        **{
            **delegated_context().model_dump(),
            "actor_principal_id": "human-1",
            "application_id": "portal",
            "application_version": "portal-version-1",
            "agent_id": None,
        }
    )
    assert context.subject_principal_id == context.actor_principal_id
    assert context.application_id == "portal"
    assert context.agent_id is None
