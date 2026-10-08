from __future__ import annotations

from contextlib import AbstractContextManager
from typing import BinaryIO, Protocol, runtime_checkable

from eios.runtime.artifacts import (
    ArtifactCredentials,
    ArtifactLocation,
    ArtifactObjectStat,
    ArtifactPresignedResponse,
    ArtifactUploadReceipt,
    ArtifactUploadRequest,
)


@runtime_checkable
class ArtifactWriter(Protocol):
    def put(self, request: ArtifactUploadRequest) -> ArtifactUploadReceipt: ...

    def head(self, location: ArtifactLocation) -> ArtifactObjectStat | None: ...

    def open(
        self, location: ArtifactLocation
    ) -> AbstractContextManager[BinaryIO]: ...

    def close(self) -> None: ...


@runtime_checkable
class ArtifactReader(Protocol):
    def head(self, location: ArtifactLocation) -> ArtifactObjectStat | None: ...

    def open(
        self, location: ArtifactLocation
    ) -> AbstractContextManager[BinaryIO]: ...

    def presign_get(
        self,
        location: ArtifactLocation,
        expires_in_seconds: int,
    ) -> ArtifactPresignedResponse: ...

    def close(self) -> None: ...


@runtime_checkable
class ArtifactDeleter(Protocol):
    def head(self, location: ArtifactLocation) -> ArtifactObjectStat | None: ...

    def delete(self, location: ArtifactLocation) -> None: ...

    def close(self) -> None: ...


@runtime_checkable
class CredentialProvider(Protocol):
    def get_credentials(self) -> ArtifactCredentials: ...
