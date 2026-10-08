from __future__ import annotations

from contextlib import suppress

from psycopg import Connection, OperationalError
from psycopg.conninfo import conninfo_to_dict
from psycopg_pool import ConnectionPool

from eios.persistence.settings import StorageSettings


class StorageUnavailable(RuntimeError):
    """Stable storage-boundary error that never exposes connection details."""

    code = "storage_unavailable"


class _SecretSafeConnection(Connection):
    """Prevent driver connection details from reaching pool log records."""

    @classmethod
    def connect(cls, conninfo: str = "", **kwargs: object) -> _SecretSafeConnection:
        connection: _SecretSafeConnection | None = None
        failed = False
        try:
            connection = super().connect(conninfo, **kwargs)
        except Exception:
            failed = True
        conninfo = ""
        kwargs.clear()
        if failed or connection is None:
            raise OperationalError("PostgreSQL connection failed")
        return connection


def _parse_conninfo(raw_conninfo: str) -> dict[str, object] | None:
    parsed: dict[str, object] | None = None
    try:
        parsed = dict(conninfo_to_dict(raw_conninfo))
    except Exception:
        pass
    raw_conninfo = ""
    return parsed


def create_pool(
    settings: StorageSettings,
    *,
    open_pool: bool = False,
    role: str | None = None,
) -> ConnectionPool:
    """Build a bounded PostgreSQL pool, opening it only when explicitly asked."""

    # T-002(2026-09-24):关 JIT。本进程族的查询全是 OLTP 短语句或周期扫描,
    # JIT 只付编译成本。生产实测 `data_health_reports` 按 report_id 查找
    # 一次 3.18s → jit=off 1.15s(编译 102 个函数)——而 DB 机 4 核已被
    # 这类查询吃到空闲 <4%,按需采集端点的每条短语句都在它们后面排队。
    # 射程:仅经本函数起的池;identity_pool 与 apps/* 里直接 ConnectionPool(
    # / psycopg.connect( 的点**不受影响**(见 handoffs/2026-09-24-T002)。
    option_parts = [
        "-c timezone=UTC",
        "-c jit=off",
        f"-c statement_timeout={settings.statement_timeout_ms}",
        f"-c lock_timeout={settings.lock_timeout_ms}",
        "-c idle_in_transaction_session_timeout="
        f"{settings.idle_transaction_timeout_ms}",
    ]
    allowed_roles = frozenset(
        {
            "nex_eios_archiver",
            "nex_eios_backup",
            "nex_eios_bucket_audit_recorder",
            "nex_eios_retention",
            "nex_eios_retention_executor",
        }
    )
    if role is not None:
        if role not in allowed_roles:
            raise StorageUnavailable("PostgreSQL role is unavailable")
        option_parts.append(f"-c role={role}")
    options = " ".join(option_parts)
    pool_min_size = settings.pool_min_size
    pool_max_size = settings.pool_max_size
    connect_timeout_seconds = settings.connect_timeout_seconds
    raw_conninfo = settings.database_url or ""
    connection_kwargs = _parse_conninfo(raw_conninfo)
    raw_conninfo = ""
    settings = None
    if connection_kwargs is None:
        raise StorageUnavailable("PostgreSQL storage is unavailable")
    connection_kwargs.update(
        {
            "connect_timeout": connect_timeout_seconds,
            "options": options,
            "application_name": "nex-eios",
        }
    )
    pool: ConnectionPool | None = None
    failed = False
    try:
        pool = ConnectionPool(
            conninfo="",
            connection_class=_SecretSafeConnection,
            kwargs=connection_kwargs,
            min_size=pool_min_size,
            max_size=pool_max_size,
            name="nex-eios",
            open=False,
        )
        if open_pool:
            pool.open(wait=True, timeout=connect_timeout_seconds)
    except Exception:
        failed = True
    if failed or pool is None:
        if pool is not None:
            with suppress(Exception):
                pool.close()
        pool = None
        connection_kwargs.clear()
        connection_kwargs = None
        raise StorageUnavailable("PostgreSQL storage is unavailable")
    return pool
