from __future__ import annotations

from datetime import datetime
from typing import Protocol

from .models import (
    ApiKeyMutationResult,
    AuthenticationResult,
    BootstrapApiKeyCommand,
    CreatePrincipalCommand,
    IssueApiKeyCommand,
    RevokeApiKeyCommand,
    UpdateApiKeyScopesCommand,
    RotateApiKeyCommand,
    TrustedOperatorContext,
)


class ApiKeyRepository(Protocol):
    def authenticate(
        self, token_digest: bytes, *, now: datetime
    ) -> AuthenticationResult: ...

    def bootstrap(
        self,
        command: BootstrapApiKeyCommand,
        *,
        operator: TrustedOperatorContext,
    ) -> ApiKeyMutationResult: ...

    def create_principal(
        self,
        command: CreatePrincipalCommand,
        *,
        operator: TrustedOperatorContext,
    ) -> ApiKeyMutationResult: ...

    def issue(
        self,
        command: IssueApiKeyCommand,
        *,
        operator: TrustedOperatorContext,
    ) -> ApiKeyMutationResult: ...

    def rotate(
        self,
        command: RotateApiKeyCommand,
        *,
        operator: TrustedOperatorContext,
    ) -> ApiKeyMutationResult: ...

    def api_key_scopes(self, tenant_id: str, api_key_id: str) -> tuple[str, ...] | None:
        """P0-5:该租户下这把 key 的 scope;不存在 ⇒ None(调用方据此 fail closed)。"""
        ...

    def revoke(
        self,
        command: RevokeApiKeyCommand,
        *,
        operator: TrustedOperatorContext,
    ) -> ApiKeyMutationResult: ...

    def update_scopes(
        self,
        command: UpdateApiKeyScopesCommand,
        *,
        operator: TrustedOperatorContext,
    ) -> ApiKeyMutationResult: ...

    def disable_principal(self, tenant_id: str, principal_id: str) -> None: ...

    def delete_api_key(self, tenant_id: str, api_key_id: str) -> datetime: ...

    def purge_revoked_api_keys(self, tenant_id: str, principal_id: str) -> int: ...

    def close(self) -> None: ...
