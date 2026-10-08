from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Literal


class StorageConfigurationError(RuntimeError):
    code = "storage_configuration_error"


def _integer(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise StorageConfigurationError(f"{name} must be an integer") from exc


@dataclass(frozen=True)
class StorageSettings:
    mode: Literal["memory", "postgres"] = "postgres"
    database_url: str | None = field(default=None, repr=False)
    pool_min_size: int = 1
    pool_max_size: int = 4
    connect_timeout_seconds: int = 5
    statement_timeout_ms: int = 10_000
    lock_timeout_ms: int = 3_000
    idle_transaction_timeout_ms: int = 10_000

    def __post_init__(self) -> None:
        if self.mode != "postgres":
            raise StorageConfigurationError("NexLoop requires PostgreSQL; Memory fallback is disabled")
        if self.mode == "postgres" and not str(self.database_url or "").strip():
            raise StorageConfigurationError(
                "NEX_EIOS_DATABASE_URL/database_url is required for postgres mode"
            )
        if self.pool_min_size < 1 or self.pool_max_size < self.pool_min_size:
            raise StorageConfigurationError("database pool bounds are invalid")
        timeouts = {
            "connect_timeout_seconds": self.connect_timeout_seconds,
            "statement_timeout_ms": self.statement_timeout_ms,
            "lock_timeout_ms": self.lock_timeout_ms,
            "idle_transaction_timeout_ms": self.idle_transaction_timeout_ms,
        }
        for name, value in timeouts.items():
            if value <= 0:
                raise StorageConfigurationError(f"{name} timeout must be greater than zero")

    @classmethod
    def from_env(cls) -> StorageSettings:
        mode = os.getenv("NEX_EIOS_STORAGE", "postgres").strip().lower()
        database_url = os.getenv("NEX_EIOS_DATABASE_URL", "").strip() or None
        return cls(
            mode=mode,
            database_url=database_url,
            pool_min_size=_integer("NEX_EIOS_DB_POOL_MIN", 1),
            pool_max_size=_integer("NEX_EIOS_DB_POOL_MAX", 4),
            connect_timeout_seconds=_integer("NEX_EIOS_DB_CONNECT_TIMEOUT_SECONDS", 5),
            statement_timeout_ms=_integer("NEX_EIOS_DB_STATEMENT_TIMEOUT_MS", 10_000),
            lock_timeout_ms=_integer("NEX_EIOS_DB_LOCK_TIMEOUT_MS", 3_000),
            idle_transaction_timeout_ms=_integer("NEX_EIOS_DB_IDLE_TRANSACTION_TIMEOUT_MS", 10_000),
        )
