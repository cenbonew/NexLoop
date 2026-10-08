from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import threading
from typing import Literal

from eios.runtime.artifacts import (
    MAX_ARTIFACT_BYTES,
    ArtifactConfigurationError,
    ArtifactLocation,
    ArtifactObjectStat,
    ArtifactStorageConflictError,
    ArtifactStorageNotFoundError,
    ArtifactTooLargeError,
    ArtifactUnavailableError,
    ArtifactUploadReceipt,
    ArtifactUploadRequest,
)


def _crc64_placeholder(payload_sha256: str) -> str:
    """Return deterministic test metadata; production CRC comes from TOS."""

    return str(int(payload_sha256[:16], 16))


def _request_id(payload_sha256: str) -> str:
    return f"local-{payload_sha256[:24]}"


class LocalArtifactStore:
    """Development-only immutable object store backed by local files."""

    def __init__(
        self,
        *,
        root: Path,
        environment: Literal["development", "test", "production"] = "development",
    ) -> None:
        if environment not in {"development", "test"}:
            raise ArtifactConfigurationError()
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._closed = False

    def _path(self, location: ArtifactLocation) -> Path:
        path = (self._root / location.bucket / location.object_key).resolve()
        if not path.is_relative_to(self._root):
            raise ArtifactConfigurationError()
        return path

    def _ensure_open(self) -> None:
        if self._closed:
            raise ArtifactUnavailableError()

    @staticmethod
    def _read_bounded(path: Path) -> bytes:
        with path.open("rb") as stream:
            payload = stream.read(MAX_ARTIFACT_BYTES + 1)
        if len(payload) > MAX_ARTIFACT_BYTES:
            raise ArtifactTooLargeError()
        return payload

    @staticmethod
    def _receipt(request: ArtifactUploadRequest) -> ArtifactUploadReceipt:
        return ArtifactUploadReceipt(
            location=request.location,
            size_bytes=request.size_bytes,
            payload_sha256=request.payload_sha256,
            etag=request.payload_sha256,
            crc64=_crc64_placeholder(request.payload_sha256),
            request_id=_request_id(request.payload_sha256),
        )

    def put(self, request: ArtifactUploadRequest) -> ArtifactUploadReceipt:
        with self._lock:
            self._ensure_open()
            path = self._path(request.location)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=path.parent,
                    prefix=f".{path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as temporary:
                    temporary_path = Path(temporary.name)
                    temporary.write(request.payload)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                for collision_attempt in range(2):
                    try:
                        os.link(temporary_path, path)
                        break
                    except FileExistsError:
                        try:
                            existing = self._read_bounded(path)
                        except FileNotFoundError:
                            if collision_attempt == 0:
                                continue
                            raise ArtifactUnavailableError() from None
                        if (
                            len(existing) != request.size_bytes
                            or hashlib.sha256(existing).hexdigest()
                            != request.payload_sha256
                        ):
                            raise ArtifactStorageConflictError() from None
                        break
                temporary_path.unlink()
                temporary_path = None
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
            return self._receipt(request)

    def head(self, location: ArtifactLocation) -> ArtifactObjectStat | None:
        with self._lock:
            self._ensure_open()
            path = self._path(location)
            if not path.is_file():
                return None
            try:
                payload = self._read_bounded(path)
            except FileNotFoundError:
                return None
            digest = hashlib.sha256(payload).hexdigest()
            return ArtifactObjectStat(
                location=location,
                size_bytes=len(payload),
                payload_sha256=digest,
                etag=digest,
                crc64=_crc64_placeholder(digest),
                request_id=_request_id(digest),
            )

    def open(self, location: ArtifactLocation):
        with self._lock:
            self._ensure_open()
            path = self._path(location)
            if not path.is_file():
                raise ArtifactStorageNotFoundError()
            try:
                return path.open("rb")
            except FileNotFoundError:
                raise ArtifactStorageNotFoundError() from None

    def delete(self, location: ArtifactLocation) -> None:
        with self._lock:
            self._ensure_open()
            self._path(location).unlink(missing_ok=True)

    def presign_get(self, location: ArtifactLocation, expires_in_seconds: int):
        del location, expires_in_seconds
        raise ArtifactConfigurationError()

    def close(self) -> None:
        with self._lock:
            self._closed = True
