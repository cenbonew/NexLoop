from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from eios.identity.admin_lifecycle import (
    IdentityAdminAction,
    IdentityDeliveryOutboxItem,
    IdentityLifecycleEvent,
)
from eios.identity.models import FrozenJsonMap, SecretDigest32


NOW = datetime(2026, 7, 23, 3, 0, tzinfo=UTC)


def _action(**changes: object) -> IdentityAdminAction:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "action_id": "iact-1",
        "idempotency_key": "idem-1",
        "payload_digest": SecretDigest32(b"p" * 32),
        "actor_session_id": "session-admin",
        "actor_subject_id": "subject-admin",
        "actor_principal_id": "principal-admin",
        "target_subject_id": "subject-user",
        "target_principal_id": "principal-user",
        "target_local_account_id": "account-user",
        "operation": "identity.user.disable",
        "authorization_decision_id": "decision-1",
        "authorization_decision_version": 3,
        "authorization_vector": FrozenJsonMap({"membership_revision": 4}),
        "status": "pending",
        "created_at": NOW,
        "completed_at": None,
        "revision": 1,
    }
    values.update(changes)
    return IdentityAdminAction(**values)


def test_admin_action_is_tenant_actor_target_and_decision_bound() -> None:
    action = _action()

    assert action.operation == "identity.user.disable"
    assert action.authorization_vector == {"membership_revision": 4}
    assert "payload_digest" not in action.model_dump()
    assert "b'pp" not in repr(action)


@pytest.mark.parametrize(
    ("status", "completed_at"),
    [("pending", NOW), ("succeeded", None), ("failed", None)],
)
def test_admin_action_completion_state_is_exact(
    status: str, completed_at: datetime | None
) -> None:
    with pytest.raises(ValidationError):
        _action(status=status, completed_at=completed_at)


def test_lifecycle_event_carries_revision_fences_without_secrets() -> None:
    event = IdentityLifecycleEvent(
        event_id="ile-1",
        tenant_id="tenant-a",
        action_id="iact-1",
        actor_subject_id="subject-admin",
        actor_principal_id="principal-admin",
        target_subject_id="subject-user",
        target_principal_id="principal-user",
        target_local_account_id="account-user",
        event_type="local_account.disabled",
        before_revision=8,
        after_revision=9,
        details=FrozenJsonMap({"reason": "employment-ended"}),
        request_id="request-1",
        trace_id="trace-1",
        created_at=NOW,
    )

    assert event.after_revision == event.before_revision + 1

    with pytest.raises(ValidationError):
        IdentityLifecycleEvent(**{**event.model_dump(), "after_revision": 10})


def test_delivery_outbox_requires_sealed_payload_and_bounded_lease_state() -> None:
    pending = IdentityDeliveryOutboxItem(
        tenant_id="tenant-a",
        delivery_id="idel-1",
        action_id="iact-1",
        kind="invitation",
        sealed_envelope=b"ciphertext",
        status="pending",
        attempts=0,
        next_action_at=NOW,
        lease_owner_id=None,
        lease_until=None,
        lease_fence=0,
        created_at=NOW,
        updated_at=NOW,
        completed_at=None,
        revision=1,
    )

    assert "sealed_envelope" not in pending.model_dump()
    assert "ciphertext" not in repr(pending)

    with pytest.raises(ValidationError):
        IdentityDeliveryOutboxItem(
            **{
                **pending.model_dump(),
                "sealed_envelope": b"ciphertext",
                "status": "leased",
                "attempts": 1,
                "lease_fence": 1,
                "lease_owner_id": "worker-1",
                "lease_until": NOW + timedelta(minutes=1),
                "next_action_at": NOW,
            }
        )
