"""Trusted tenant-specific Identity BFF configuration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from .models import _FrozenModel, _non_blank


IdentityMode = Literal["local_only", "sso_only", "mixed"]

#: Minimum length, in characters, for any newly chosen local-account password.
#: Enforced at the single hashing convergence point
#: (:meth:`LocalAccountService.hash_password_for_account`) so every entry that
#: sets a new credential (invitation acceptance, reset, recovery, change)
#: shares one policy, while verification of existing passwords is unaffected.
MINIMUM_NEW_PASSWORD_LENGTH = 12


class IdentityRealm(_FrozenModel):
    """One database snapshot of a tenant's enabled authentication realm."""

    tenant_id: str
    mode: IdentityMode
    oidc_provider_ids: tuple[str, ...]
    revision: int = Field(ge=1)

    _validate_tenant_id = field_validator("tenant_id")(
        lambda value: _non_blank(value, "tenant_id")
    )

    @field_validator("oidc_provider_ids")
    @classmethod
    def _validate_providers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return IdentityTenantSettings._validate_providers(value)

    @model_validator(mode="after")
    def _validate_mode(self) -> IdentityRealm:
        if (self.mode == "local_only") != (not self.oidc_provider_ids):
            raise ValueError("identity realm mode is inconsistent")
        return self

    @property
    def local_enabled(self) -> bool:
        return self.mode in {"local_only", "mixed"}


class IdentityRealmRepository(Protocol):
    def get_realm(self, tenant_id: str) -> IdentityRealm | None:
        """Return settings and active providers from one database snapshot."""
        ...


class IdentityTenantSettings(_FrozenModel):
    """Server-owned Identity configuration selected by the canonical host."""

    tenant_id: str
    canonical_origin: str
    mode: IdentityMode
    local_enabled: bool
    oidc_provider_ids: tuple[str, ...]
    application_id: str
    application_revision: int = Field(ge=1)

    @field_validator("tenant_id", "application_id")
    @classmethod
    def _validate_identifiers(cls, value: str, info: Any) -> str:
        return _non_blank(value, info.field_name)

    @field_validator("canonical_origin")
    @classmethod
    def _validate_origin(cls, value: str) -> str:
        checked = _non_blank(value, "canonical_origin")
        if len(checked) > 2048:
            raise ValueError("canonical_origin is invalid")
        parsed = urlsplit(checked)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or checked != _origin(parsed.hostname, parsed.port)
        ):
            raise ValueError("canonical_origin is invalid")
        return checked

    @field_validator("oidc_provider_ids")
    @classmethod
    def _validate_providers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 16:
            raise ValueError("oidc_provider_ids is invalid")
        checked = tuple(_non_blank(item, "oidc_provider_ids") for item in value)
        if len(checked) != len(set(checked)) or any(
            len(item) > 128
            or not all(
                character.isascii() and (character.isalnum() or character in "-._~")
                for character in item
            )
            for item in checked
        ):
            raise ValueError("oidc_provider_ids is invalid")
        return checked

    @model_validator(mode="after")
    def _validate_mode(self) -> IdentityTenantSettings:
        valid = {
            "local_only": self.local_enabled and not self.oidc_provider_ids,
            "sso_only": not self.local_enabled and bool(self.oidc_provider_ids),
            "mixed": self.local_enabled and bool(self.oidc_provider_ids),
        }
        if not valid[self.mode]:
            raise ValueError("identity mode configuration is invalid")
        return self


class TrustedHostTenantResolver(Protocol):
    def resolve(self, host: str) -> IdentityTenantSettings | None: ...


class StaticTrustedHostTenantResolver:
    """Exact host map; caller-supplied tenant headers and bodies are irrelevant."""

    def __init__(self, settings_by_host: Mapping[str, IdentityTenantSettings]) -> None:
        checked: dict[str, IdentityTenantSettings] = {}
        if not settings_by_host or len(settings_by_host) > 10_000:
            raise ValueError("trusted host registry is invalid")
        for host, value in settings_by_host.items():
            key = _normalize_host(host)
            settings = IdentityTenantSettings.model_validate(value)
            if key is None or key in checked:
                raise ValueError("trusted host registry is invalid")
            if key != _normalize_host(urlsplit(settings.canonical_origin).netloc):
                raise ValueError("trusted host registry is invalid")
            checked[key] = settings
        self._settings = checked

    def resolve(self, host: str) -> IdentityTenantSettings | None:
        key = _normalize_host(host)
        return None if key is None else self._settings.get(key)


def normalize_return_to(value: str | None) -> str:
    """Accept a bounded, already same-origin path or return the Portal root."""

    if value is None:
        return "/"
    if type(value) is not str or not 0 < len(value) <= 2048:
        raise ValueError("return target is invalid")
    lowered = value.lower()
    if (
        not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(encoded in lowered for encoded in ("%2f", "%5c", "%00", "%0a", "%0d"))
    ):
        raise ValueError("return target is invalid")
    parsed = urlsplit(value)
    segments = parsed.path.split("/")
    if (
        parsed.scheme
        or parsed.netloc
        or not parsed.path.startswith("/")
        or any(segment in {".", ".."} for segment in segments)
        or parsed.path.startswith("/api/auth/oidc/")
        and parsed.path.endswith("/callback")
    ):
        raise ValueError("return target is invalid")
    return value


def _origin(hostname: str, port: int | None) -> str:
    normalized = hostname.rstrip(".").lower()
    if not normalized or len(normalized) > 253:
        raise ValueError("canonical_origin is invalid")
    return f"https://{normalized}" + ("" if port in (None, 443) else f":{port}")


def _normalize_host(value: object) -> str | None:
    if type(value) is not str or not 0 < len(value) <= 320:
        return None
    if any(ord(character) <= 32 or ord(character) == 127 for character in value):
        return None
    try:
        parsed = urlsplit("//" + value)
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            return None
        hostname = parsed.hostname.rstrip(".").lower()
        port = parsed.port
    except ValueError:
        return None
    if not hostname or len(hostname) > 253:
        return None
    return hostname + ("" if port in (None, 443) else f":{port}")


__all__ = [
    "IdentityMode",
    "MINIMUM_NEW_PASSWORD_LENGTH",
    "IdentityRealm",
    "IdentityRealmRepository",
    "IdentityTenantSettings",
    "StaticTrustedHostTenantResolver",
    "TrustedHostTenantResolver",
    "normalize_return_to",
]
