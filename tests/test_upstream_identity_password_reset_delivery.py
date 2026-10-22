from __future__ import annotations

from datetime import UTC, datetime, timedelta
import inspect

from pydantic import SecretBytes, SecretStr
import pytest

from eios.identity.delivery import (
    ClaimedPasswordResetEnvelope,
    PasswordResetDeliveryClaim,
    PasswordResetDeliveryOpener,
    PasswordResetDeliveryQueue,
    PasswordResetDeliveryRelay,
    PasswordResetDeliverySealer,
    PasswordResetDeliveryState,
    PasswordResetDeliveryWorker,
    PasswordResetDeliveryWorkerFactory,
    PasswordResetDeliveryWorkerResult,
)
from eios.identity.ports import TrustedIdentityOperator


NOW = datetime(2026, 7, 20, 13, tzinfo=UTC)


def test_delivery_claim_and_opened_envelope_keep_all_secrets_off_surfaces() -> None:
    sealed = b"sealed-reset-delivery-sentinel"
    claim = PasswordResetDeliveryClaim(
        tenant_id="tenant-a",
        password_reset_id="reset-a",
        local_account_id="account-a",
        delivery_envelope=SecretBytes(sealed),
        delivery_attempt=1,
        delivery_fence=7,
        lease_until=NOW + timedelta(seconds=60),
        expires_at=NOW + timedelta(minutes=30),
    )
    opened = ClaimedPasswordResetEnvelope(
        tenant_id="tenant-a",
        password_reset_id="reset-a",
        local_account_id="account-a",
        claim_id="worker-a:7",
        recipient="alice@example.com",
        reset_url=SecretStr(
            "https://portal.example.com/reset-password?token=reset-token-sentinel"
        ),
        canonical_origin="https://portal.example.com",
        expires_at=NOW + timedelta(minutes=30),
    )

    assert claim.delivery_envelope.get_secret_value() == sealed
    assert "delivery_envelope" not in claim.model_dump()
    assert sealed.decode() not in repr(claim)
    assert "reset-token-sentinel" not in repr(opened)
    assert "reset_url" not in opened.model_dump()


def test_delivery_ports_freeze_claim_open_send_and_fenced_finish_signatures() -> None:
    assert PasswordResetDeliveryQueue._is_protocol
    assert PasswordResetDeliveryOpener._is_protocol
    assert PasswordResetDeliveryRelay._is_protocol
    assert PasswordResetDeliverySealer._is_protocol
    assert PasswordResetDeliveryWorker._is_protocol
    assert PasswordResetDeliveryWorkerFactory._is_protocol

    claim = inspect.signature(PasswordResetDeliveryQueue.claim).parameters
    acknowledge = inspect.signature(PasswordResetDeliveryQueue.acknowledge).parameters
    retry = inspect.signature(PasswordResetDeliveryQueue.retry).parameters
    opened = inspect.signature(PasswordResetDeliveryOpener.open).parameters
    run_once = inspect.signature(PasswordResetDeliveryWorker.run_once).parameters
    seal = inspect.signature(PasswordResetDeliverySealer.seal).parameters
    build = inspect.signature(
        PasswordResetDeliveryWorkerFactory.build_password_reset_delivery_worker
    ).parameters

    assert set(claim) == {"self", "worker_id", "operator"}
    assert set(acknowledge) == {
        "self",
        "password_reset_id",
        "worker_id",
        "delivery_fence",
        "operator",
    }
    assert set(retry) == set(acknowledge)
    assert set(opened) == {
        "self",
        "value",
        "tenant_id",
        "password_reset_id",
        "local_account_id",
        "claim_id",
        "expires_at",
        "canonical_origin",
    }
    assert set(run_once) == {"self", "worker_id", "operator"}
    assert set(seal) == {
        "self",
        "tenant_id",
        "password_reset_id",
        "local_account_id",
        "recipient",
        "token",
        "expires_at",
        "canonical_origin",
    }
    assert set(build) == {
        "self",
        "queue",
        "opener",
        "relay",
        "canonical_origin",
    }
    maximum_delivery = inspect.getattr_static(
        PasswordResetDeliveryRelay, "maximum_delivery_seconds"
    )
    assert isinstance(maximum_delivery, property)
    assert maximum_delivery.fget is not None
    assert inspect.signature(maximum_delivery.fget).return_annotation in {int, "int"}
    assert build["relay"].annotation in {
        PasswordResetDeliveryRelay,
        "PasswordResetDeliveryRelay",
    }
    assert inspect.signature(PasswordResetDeliveryRelay.send).return_annotation in {
        None,
        "None",
    }


def test_delivery_state_and_worker_results_are_typed_and_secret_free() -> None:
    state = PasswordResetDeliveryState(
        password_reset_id="reset-a",
        delivery_status="delivered",
        delivery_attempts=1,
        delivery_fence=7,
        next_action_at=NOW,
    )
    delivered = PasswordResetDeliveryWorkerResult(
        status="delivered",
        password_reset_id="reset-a",
        delivery_attempt=1,
    )
    idle = PasswordResetDeliveryWorkerResult(
        status="idle",
        password_reset_id=None,
        delivery_attempt=None,
    )

    assert state.delivery_status == "delivered"
    assert delivered.model_dump() == {
        "status": "delivered",
        "password_reset_id": "reset-a",
        "delivery_attempt": 1,
    }
    assert idle.status == "idle"

    with pytest.raises(ValueError, match="worker result details are invalid"):
        PasswordResetDeliveryWorkerResult(
            status="retry_scheduled",
            password_reset_id="reset-a",
            delivery_attempt=None,
        )


@pytest.mark.parametrize(
    "recipient",
    (
        "alice@example.com,bob",
        "alice@example.com,Bcc:bob;",
        "alice bob@example.com",
        "alice\t@example.com",
        "alice\r\n@example.com",
        "alice@@example.com",
        ".alice@example.com",
        "alice.@example.com",
        "alice..security@example.com",
        "alice@-example.com",
        "alice@example-.com",
        "alice@example..com",
        "alice@example_com",
        f"{'a' * 65}@example.com",
        f"alice@{'a' * 64}.com",
        f"{'a' * 64}@{'b' * 63}.{'c' * 63}.{'d' * 58}.com",
    ),
)
def test_claimed_envelope_rejects_non_single_ascii_addr_spec(recipient: str) -> None:
    with pytest.raises(ValueError, match="recipient is invalid"):
        ClaimedPasswordResetEnvelope(
            tenant_id="tenant-a",
            password_reset_id="reset-a",
            local_account_id="account-a",
            claim_id="worker-a:7",
            recipient=recipient,
            reset_url=SecretStr(
                "https://portal.example.com/reset-password?token=reset-token"
            ),
            canonical_origin="https://portal.example.com",
            expires_at=NOW + timedelta(minutes=30),
        )


@pytest.mark.parametrize(
    "recipient",
    (
        "alice.security+reset@example.com",
        "o'hara@example.co.uk",
        "security_alert-1@example-domain.com",
    ),
)
def test_claimed_envelope_accepts_conservative_ascii_dot_atom(recipient: str) -> None:
    opened = ClaimedPasswordResetEnvelope(
        tenant_id="tenant-a",
        password_reset_id="reset-a",
        local_account_id="account-a",
        claim_id="worker-a:7",
        recipient=recipient,
        reset_url=SecretStr(
            "https://portal.example.com/reset-password?token=reset-token"
        ),
        canonical_origin="https://portal.example.com",
        expires_at=NOW + timedelta(minutes=30),
    )

    assert opened.recipient == recipient


def _operator() -> TrustedIdentityOperator:
    return TrustedIdentityOperator(
        operator_principal_id="nex_eios_access_operator",
        request_id="request-delivery-worker",
        trace_id="trace-delivery-worker",
    )
