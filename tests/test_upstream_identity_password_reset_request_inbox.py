from __future__ import annotations

from datetime import UTC, datetime, timedelta
import inspect
from typing import get_args

from pydantic import SecretBytes
import pytest

from eios.identity.ports import (
    AckPasswordResetRequestCommand,
    ClaimedPasswordResetRequest,
    CompletePasswordResetRequestWithResetCommand,
    CreatePasswordResetCommand,
    EnqueuePasswordResetRequestCommand,
    PasswordResetRequestEnqueueReceipt,
    PasswordResetRequestInboxRepository,
    PasswordResetRequestInboxStatus,
    RetryPasswordResetRequestCommand,
)
from eios.identity.models import PasswordReset, SecretDigest32


NOW = datetime(2026, 7, 20, 14, tzinfo=UTC)


def _ack() -> AckPasswordResetRequestCommand:
    return AckPasswordResetRequestCommand(
        tenant_id="tenant-a",
        inbox_request_id="reset-request-a",
        lease_owner_id="reset-worker-a",
        lease_fence=7,
    )


def _reset_create() -> CreatePasswordResetCommand:
    return CreatePasswordResetCommand(
        password_reset=PasswordReset(
            password_reset_id="reset-a",
            tenant_id="tenant-a",
            local_account_id="account-a",
            token_digest=SecretDigest32(b"r" * 32),
            status="pending",
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=30),
            consumed_at=None,
            revision=1,
        ),
        delivery_envelope=SecretBytes(b"sealed-reset-delivery"),
        prior_pending_reset_ids=(),
        expected_account_revision=3,
        expected_session_epoch=2,
    )


def test_inbox_models_are_frozen_bounded_and_hide_every_sealed_envelope() -> None:
    sealed = b"sealed-identifier-never-logged"
    enqueue = EnqueuePasswordResetRequestCommand(
        tenant_id="tenant-a",
        inbox_request_id="reset-request-a",
        sealed_envelope=SecretBytes(sealed),
        idempotency_key="reset-idempotency-a",
    )
    receipt = PasswordResetRequestEnqueueReceipt(
        tenant_id="tenant-a",
        inbox_request_id="reset-request-a",
        accepted_at=NOW,
    )
    claim = ClaimedPasswordResetRequest(
        tenant_id="tenant-a",
        inbox_request_id="reset-request-a",
        sealed_envelope=SecretBytes(sealed),
        attempt=1,
        lease_owner_id="reset-worker-a",
        lease_until=NOW + timedelta(seconds=60),
        lease_fence=7,
    )

    assert receipt.accepted_at == NOW
    assert enqueue.sealed_envelope.get_secret_value() == sealed
    assert claim.sealed_envelope.get_secret_value() == sealed
    assert "sealed_envelope" not in enqueue.model_dump()
    assert "sealed_envelope" not in claim.model_dump()
    assert sealed.decode() not in repr(enqueue)
    assert sealed.decode() not in repr(claim)
    with pytest.raises(Exception):
        enqueue.tenant_id = "tenant-b"  # type: ignore[misc]


def test_inbox_status_and_fenced_commands_are_exact() -> None:
    assert set(get_args(PasswordResetRequestInboxStatus)) == {
        "pending",
        "leased",
        "acked",
        "dead",
    }
    ack = _ack()
    retry = RetryPasswordResetRequestCommand(
        inbox=ack,
        retry_after_seconds=30,
    )
    complete = CompletePasswordResetRequestWithResetCommand(
        inbox=ack,
        reset=_reset_create(),
    )

    assert retry.inbox.lease_fence == 7
    assert complete.inbox == ack
    assert complete.reset.password_reset.tenant_id == ack.tenant_id

    with pytest.raises(ValueError):
        RetryPasswordResetRequestCommand(
            inbox=ack,
            retry_after_seconds=0,
        )
    with pytest.raises(ValueError):
        CompletePasswordResetRequestWithResetCommand(
            inbox=ack.model_copy(update={"tenant_id": "tenant-b"}),
            reset=_reset_create(),
        )


def test_inbox_repository_protocol_freezes_global_claim_and_atomic_completion() -> None:
    assert PasswordResetRequestInboxRepository._is_protocol
    enqueue = inspect.signature(PasswordResetRequestInboxRepository.enqueue).parameters
    claim = inspect.signature(PasswordResetRequestInboxRepository.claim).parameters
    ack = inspect.signature(PasswordResetRequestInboxRepository.ack_noop).parameters
    retry = inspect.signature(PasswordResetRequestInboxRepository.retry).parameters
    dead = inspect.signature(PasswordResetRequestInboxRepository.dead).parameters
    complete = inspect.signature(
        PasswordResetRequestInboxRepository.complete_reset_request_with_reset
    ).parameters

    assert set(enqueue) == {"self", "command", "operator"}
    assert set(claim) == {
        "self",
        "worker_id",
        "lease_seconds",
        "limit",
        "operator",
    }
    assert set(ack) == {"self", "command", "operator"}
    assert set(retry) == set(ack)
    assert set(dead) == set(ack)
    assert set(complete) == set(ack)


@pytest.mark.parametrize(
    "field,value",
    (
        ("sealed_envelope", SecretBytes(b"")),
        ("idempotency_key", " "),
        ("inbox_request_id", ""),
    ),
)
def test_enqueue_rejects_empty_or_unsafe_durable_values(
    field: str,
    value: object,
) -> None:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "inbox_request_id": "reset-request-a",
        "sealed_envelope": SecretBytes(b"sealed-request"),
        "idempotency_key": "reset-idempotency-a",
    }
    values[field] = value

    with pytest.raises(ValueError):
        EnqueuePasswordResetRequestCommand(**values)
