"""Secret-safe domain contracts for durable password-reset delivery."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import Field, SecretBytes, SecretStr, field_validator, model_validator

from .models import _FrozenModel, _aware_utc, _non_blank
from .ports import TrustedIdentityOperator


_MAX_IDENTIFIER_LENGTH = 320
_MAX_DELIVERY_ENVELOPE_BYTES = 16_384
_MAX_RESET_URL_LENGTH = 4_096
_MAX_EMAIL_LENGTH = 254
_MAX_EMAIL_LOCAL_LENGTH = 64
_MAX_EMAIL_DOMAIN_LENGTH = 253
_EMAIL_LOCAL_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!#$%&'*+-/=?^_`{|}~."
)
_EMAIL_DOMAIN_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-."
)


def _identifier(value: str, info: Any) -> str:
    checked = _non_blank(value, info.field_name)
    if len(checked) > _MAX_IDENTIFIER_LENGTH or checked != value:
        raise ValueError(f"{info.field_name} is invalid")
    return checked


def _time(value: datetime, info: Any) -> datetime:
    checked = _aware_utc(value, info.field_name)
    assert checked is not None
    return checked


def _email(value: str) -> str:
    if (
        type(value) is not str
        or not 3 <= len(value) <= _MAX_EMAIL_LENGTH
        or not value.isascii()
        or value != value.strip()
        or value.count("@") != 1
    ):
        raise ValueError("recipient is invalid")
    local, domain = value.split("@")
    labels = domain.split(".")
    if (
        not 1 <= len(local) <= _MAX_EMAIL_LOCAL_LENGTH
        or not 1 <= len(domain) <= _MAX_EMAIL_DOMAIN_LENGTH
        or any(character not in _EMAIL_LOCAL_CHARACTERS for character in local)
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or any(character not in _EMAIL_DOMAIN_CHARACTERS for character in domain)
        or len(labels) < 2
        or any(
            not 1 <= len(label) <= 63
            or not label[0].isalnum()
            or not label[-1].isalnum()
            for label in labels
        )
    ):
        raise ValueError("recipient is invalid")
    return value


def _https_origin(value: str) -> str:
    if type(value) is not str or len(value) > _MAX_RESET_URL_LENGTH:
        raise ValueError("canonical_origin is invalid")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        host = parsed.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port
        canonical = "https://" + host
        if port not in (None, 443):
            canonical += f":{port}"
        if value != canonical:
            raise ValueError
        return canonical
    except (TypeError, ValueError):
        raise ValueError("canonical_origin is invalid") from None


class PasswordResetDeliveryClaim(_FrozenModel):
    """One fenced database lease over an opaque sealed envelope."""

    tenant_id: str
    password_reset_id: str
    local_account_id: str
    delivery_envelope: SecretBytes = Field(repr=False, exclude=True)
    delivery_attempt: int = Field(ge=1, le=10)
    delivery_fence: int = Field(ge=1)
    lease_until: datetime
    expires_at: datetime

    _validate_identifiers = field_validator(
        "tenant_id", "password_reset_id", "local_account_id"
    )(_identifier)
    _validate_times = field_validator("lease_until", "expires_at")(_time)

    @field_validator("delivery_envelope")
    @classmethod
    def _validate_delivery_envelope(cls, value: SecretBytes) -> SecretBytes:
        raw = value.get_secret_value()
        if not raw or len(raw) > _MAX_DELIVERY_ENVELOPE_BYTES:
            raise ValueError("delivery_envelope is invalid")
        return value


class PasswordResetDeliveryState(_FrozenModel):
    """Secret-free durable state returned by fenced ack/retry operations."""

    password_reset_id: str
    delivery_status: Literal["pending", "delivered", "dead"]
    delivery_attempts: int = Field(ge=1, le=10)
    delivery_fence: int = Field(ge=1)
    next_action_at: datetime | None

    _validate_password_reset_id = field_validator("password_reset_id")(_identifier)

    @field_validator("next_action_at")
    @classmethod
    def _validate_next_action_at(
        cls, value: datetime | None, info: Any
    ) -> datetime | None:
        return None if value is None else _time(value, info)


class ClaimedPasswordResetEnvelope(_FrozenModel):
    """Authenticated plaintext available only after a durable queue claim."""

    tenant_id: str
    password_reset_id: str
    local_account_id: str
    claim_id: str
    recipient: str
    reset_url: SecretStr = Field(repr=False, exclude=True)
    canonical_origin: str
    expires_at: datetime

    _validate_identifiers = field_validator(
        "tenant_id", "password_reset_id", "local_account_id", "claim_id"
    )(_identifier)
    _validate_expires_at = field_validator("expires_at")(_time)

    @field_validator("recipient")
    @classmethod
    def _validate_recipient(cls, value: str) -> str:
        return _email(value)

    @field_validator("canonical_origin")
    @classmethod
    def _validate_canonical_origin(cls, value: str) -> str:
        return _https_origin(value)

    @model_validator(mode="after")
    def _validate_reset_url(self) -> ClaimedPasswordResetEnvelope:
        raw = self.reset_url.get_secret_value()
        try:
            raw.encode("utf-8")
            parsed = urlsplit(raw)
            if (
                not 0 < len(raw) <= _MAX_RESET_URL_LENGTH
                or any(
                    ord(character) < 32 or ord(character) == 127 for character in raw
                )
                or parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or not parsed.path
                or not parsed.query
                or parsed.fragment
                or _origin_of_url(raw) != self.canonical_origin
            ):
                raise ValueError
        except (TypeError, UnicodeError, ValueError):
            raise ValueError("reset_url is invalid") from None
        return self


class PasswordResetDeliverySealer(Protocol):
    """Seal a reset token and recipient into an authenticated durable envelope."""

    def seal(
        self,
        *,
        tenant_id: str,
        password_reset_id: str,
        local_account_id: str,
        recipient: str,
        token: SecretStr,
        expires_at: datetime,
        canonical_origin: str,
    ) -> SecretBytes: ...


class PasswordResetDeliveryQueue(Protocol):
    """Claim and finish durable delivery leases using a fencing token."""

    def claim(
        self,
        worker_id: str,
        *,
        operator: TrustedIdentityOperator,
    ) -> PasswordResetDeliveryClaim | None: ...

    def acknowledge(
        self,
        password_reset_id: str,
        worker_id: str,
        delivery_fence: int,
        *,
        operator: TrustedIdentityOperator,
    ) -> PasswordResetDeliveryState: ...

    def retry(
        self,
        password_reset_id: str,
        worker_id: str,
        delivery_fence: int,
        *,
        operator: TrustedIdentityOperator,
    ) -> PasswordResetDeliveryState: ...


class PasswordResetDeliveryOpener(Protocol):
    """Open and context-check an already claimed delivery envelope."""

    def open(
        self,
        value: SecretBytes,
        *,
        tenant_id: str,
        password_reset_id: str,
        local_account_id: str,
        claim_id: str,
        expires_at: datetime,
        canonical_origin: str,
    ) -> ClaimedPasswordResetEnvelope: ...


class PasswordResetDeliveryRelay(Protocol):
    """Send one authenticated claimed envelope to its bound recipient."""

    @property
    def maximum_delivery_seconds(self) -> int:
        """Worst-case external send budget; implementations guarantee 1..30."""
        ...

    def send(self, envelope: ClaimedPasswordResetEnvelope) -> None: ...


class PasswordResetDeliveryWorkerResult(_FrozenModel):
    """Secret-free outcome of at most one queue claim."""

    status: Literal["idle", "delivered", "retry_scheduled", "dead"]
    password_reset_id: str | None
    delivery_attempt: int | None = Field(default=None, ge=1, le=10)

    @field_validator("password_reset_id")
    @classmethod
    def _validate_password_reset_id(cls, value: str | None, info: Any) -> str | None:
        return None if value is None else _identifier(value, info)

    @model_validator(mode="after")
    def _validate_outcome(self) -> PasswordResetDeliveryWorkerResult:
        details_are_absent = (
            self.password_reset_id is None and self.delivery_attempt is None
        )
        details_are_complete = (
            self.password_reset_id is not None and self.delivery_attempt is not None
        )
        if (self.status == "idle" and not details_are_absent) or (
            self.status != "idle" and not details_are_complete
        ):
            raise ValueError("worker result details are invalid")
        return self


class PasswordResetDeliveryWorker(Protocol):
    """Claim, authenticate, relay, then fenced-ack or fenced-retry one item."""

    def run_once(
        self,
        worker_id: str,
        *,
        operator: TrustedIdentityOperator,
    ) -> PasswordResetDeliveryWorkerResult: ...


class PasswordResetDeliveryWorkerFactory(Protocol):
    """Production composition seam for the concrete delivery coordinator."""

    def build_password_reset_delivery_worker(
        self,
        *,
        queue: PasswordResetDeliveryQueue,
        opener: PasswordResetDeliveryOpener,
        relay: PasswordResetDeliveryRelay,
        canonical_origin: str,
    ) -> PasswordResetDeliveryWorker: ...


def _origin_of_url(value: str) -> str:
    parsed = urlsplit(value)
    host = parsed.hostname
    if host is None:
        raise ValueError
    host = host.lower()
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port
    return "https://" + host + (f":{port}" if port not in (None, 443) else "")


__all__ = [
    "ClaimedPasswordResetEnvelope",
    "PasswordResetDeliveryClaim",
    "PasswordResetDeliveryOpener",
    "PasswordResetDeliveryQueue",
    "PasswordResetDeliveryRelay",
    "PasswordResetDeliverySealer",
    "PasswordResetDeliveryState",
    "PasswordResetDeliveryWorker",
    "PasswordResetDeliveryWorkerFactory",
    "PasswordResetDeliveryWorkerResult",
]
