from __future__ import annotations

import pytest

from eios.identity.settings import (
    IdentityRealm,
    IdentityRealmRepository,
    IdentityTenantSettings,
    StaticTrustedHostTenantResolver,
    normalize_return_to,
)


def test_identity_realm_is_one_typed_database_snapshot() -> None:
    realm = IdentityRealm(
        tenant_id="tenant-a",
        mode="mixed",
        oidc_provider_ids=("provider-a", "provider-b"),
        revision=3,
    )

    assert realm.local_enabled is True
    assert IdentityRealmRepository.get_realm.__annotations__["return"] == (
        "IdentityRealm | None"
    )

    with pytest.raises(ValueError):
        IdentityRealm(
            tenant_id="tenant-a",
            mode="local_only",
            oidc_provider_ids=("unexpected",),
            revision=1,
        )


def _settings(**changes: object) -> IdentityTenantSettings:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "canonical_origin": "https://portal.example.com",
        "mode": "mixed",
        "local_enabled": True,
        "oidc_provider_ids": ("workforce",),
        "application_id": "identity-bff",
        "application_revision": 3,
    }
    values.update(changes)
    return IdentityTenantSettings(**values)


@pytest.mark.parametrize(
    ("mode", "local_enabled", "providers"),
    (
        ("local_only", True, ()),
        ("sso_only", False, ("workforce",)),
        ("mixed", True, ("workforce",)),
    ),
)
def test_identity_modes_are_explicit_and_internally_consistent(
    mode: str, local_enabled: bool, providers: tuple[str, ...]
) -> None:
    settings = _settings(
        mode=mode,
        local_enabled=local_enabled,
        oidc_provider_ids=providers,
    )

    assert settings.mode == mode


@pytest.mark.parametrize(
    ("mode", "local_enabled", "providers"),
    (
        ("local_only", False, ()),
        ("local_only", True, ("workforce",)),
        ("sso_only", True, ("workforce",)),
        ("sso_only", False, ()),
        ("mixed", False, ("workforce",)),
        ("mixed", True, ()),
    ),
)
def test_missing_or_contradictory_identity_mode_fails_closed(
    mode: str, local_enabled: bool, providers: tuple[str, ...]
) -> None:
    with pytest.raises(ValueError):
        _settings(
            mode=mode,
            local_enabled=local_enabled,
            oidc_provider_ids=providers,
        )


def test_trusted_host_resolver_ignores_browser_tenant_claims() -> None:
    settings = _settings()
    resolver = StaticTrustedHostTenantResolver({"portal.example.com": settings})

    assert resolver.resolve("portal.example.com") == settings
    assert resolver.resolve("PORTAL.EXAMPLE.COM:443") == settings
    assert resolver.resolve("attacker.example") is None


@pytest.mark.parametrize(
    "value",
    (
        "https://evil.example/path",
        "//evil.example/path",
        "/\\evil",
        "relative",
        "/path\nnext",
        "/path\rnext",
        "/path\x00next",
        "/%2f%2fevil.example",
        "/%5cevil",
        "/api/auth/oidc/workforce/callback",
    ),
)
def test_return_to_rejects_cross_origin_and_auth_callback_targets(value: str) -> None:
    with pytest.raises(ValueError, match="return target is invalid"):
        normalize_return_to(value)


def test_return_to_accepts_only_normalized_same_origin_paths() -> None:
    assert normalize_return_to(None) == "/"
    assert normalize_return_to("/console?tab=security") == "/console?tab=security"
