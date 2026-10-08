from __future__ import annotations

import logging

import argparse
from contextlib import nullcontext
from dataclasses import dataclass
import hashlib
from importlib import resources
import os
import re
from typing import Any, Iterable, Sequence

from psycopg import sql
from psycopg.pq import TransactionStatus

from eios.adapters.postgres.database import StorageUnavailable, create_pool
from eios.persistence.settings import StorageSettings

_LOGGER = logging.getLogger("eios.migrations")


_MIGRATION_NAME = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z][a-z0-9_]*)\.sql$")
_MIGRATION_LOCK = "nexloop_eios_migrations"
_PHASED_MIGRATION_DIRECTIVE = "-- eios:phased-migration-v1"
_PHASE_DIRECTIVES = (
    "-- eios:phase prepare",
    "-- eios:phase validate",
)
_SCHEMA_MIGRATIONS_DDL = """
create table if not exists control.schema_migrations (
  version text primary key,
  checksum text not null,
  applied_at timestamptz not null default now()
)
"""


class MigrationDrift(RuntimeError):
    code = "migration_drift"


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    checksum: str
    sql: str


def load_migrations(package: str = "eios.migrations") -> list[Migration]:
    catalog: list[Migration] = []
    versions: set[str] = set()
    for entry in resources.files(package).iterdir():
        match = _MIGRATION_NAME.fullmatch(entry.name)
        if match is None or not entry.is_file():
            continue
        version = match.group("version")
        if version in versions:
            raise MigrationDrift(f"duplicate migration version {version}")
        versions.add(version)
        contents = entry.read_bytes()
        catalog.append(
            Migration(
                version=version,
                name=match.group("name"),
                checksum=hashlib.sha256(contents).hexdigest(),
                sql=contents.decode("utf-8"),
            )
        )
    return sorted(catalog, key=lambda item: item.version)


def _stored_migrations(connection: Any) -> list[tuple[str, str]]:
    rows = connection.execute(
        "select version, checksum from control.schema_migrations order by version"
    ).fetchall()
    return [(str(version), str(checksum)) for version, checksum in rows]


def _connection(resource: Any):
    connection_factory = getattr(resource, "connection", None)
    return (
        connection_factory() if callable(connection_factory) else nullcontext(resource)
    )


def _migration_phases(migration: Migration) -> tuple[str, str] | None:
    lines = migration.sql.splitlines(keepends=True)
    stripped = [line.strip() for line in lines]
    if _PHASED_MIGRATION_DIRECTIVE not in stripped:
        if any(directive in stripped for directive in _PHASE_DIRECTIVES):
            raise MigrationDrift(
                f"migration {migration.version} has phase markers without directive"
            )
        return None
    if stripped.count(_PHASED_MIGRATION_DIRECTIVE) != 1:
        raise MigrationDrift(
            f"migration {migration.version} has an invalid phase directive"
        )
    non_empty = [line for line in stripped if line]
    if not non_empty or non_empty[0] != _PHASED_MIGRATION_DIRECTIVE:
        raise MigrationDrift(
            f"migration {migration.version} must start with the phase directive"
        )
    found_phase_directives = tuple(
        line for line in stripped if line.startswith("-- eios:phase ")
    )
    if found_phase_directives != _PHASE_DIRECTIVES:
        raise MigrationDrift(f"migration {migration.version} must define exact phases")
    positions: list[int] = []
    for directive in _PHASE_DIRECTIVES:
        if stripped.count(directive) != 1:
            raise MigrationDrift(
                f"migration {migration.version} must define exact phases"
            )
        positions.append(stripped.index(directive))
    header = stripped.index(_PHASED_MIGRATION_DIRECTIVE)
    prepare, validate = positions
    if not header < prepare < validate:
        raise MigrationDrift(f"migration {migration.version} has phases out of order")
    if any(
        line and not line.startswith("--") for line in stripped[header + 1 : prepare]
    ):
        raise MigrationDrift(f"migration {migration.version} has SQL outside a phase")
    phases = (
        "".join(lines[prepare + 1 : validate]).strip(),
        "".join(lines[validate + 1 :]).strip(),
    )
    if not all(phases):
        raise MigrationDrift(f"migration {migration.version} has an empty phase")
    return f"{phases[0]}\n", f"{phases[1]}\n"


def _set_local_role(connection: Any, role: str | None) -> None:
    if role:
        # Historical SQL may COMMIT and clear SET LOCAL inside a catalog run.
        # Restore the caller's exact role in a real transaction, including
        # autocommit connections; never recover an aborted or unknown state.
        status = getattr(getattr(connection, "info", None), "transaction_status", None)
        if status == TransactionStatus.IDLE:
            connection.execute("begin")
            status = connection.info.transaction_status
        if status != TransactionStatus.INTRANS:
            raise MigrationDrift("migration role requires a known active transaction")
        connection.execute(sql.SQL("set local role {}").format(sql.Identifier(role)))


def _validate_history(
    connection: Any,
    catalog: Sequence[Migration],
    by_version: dict[str, Migration],
) -> list[tuple[str, str]]:
    stored_rows = _stored_migrations(connection)
    catalog_versions = [migration.version for migration in catalog]
    stored_versions = [version for version, _checksum in stored_rows]
    for version, _checksum in stored_rows:
        if version not in by_version:
            raise MigrationDrift(f"unknown database migration {version}")
    if stored_versions != catalog_versions[: len(stored_versions)]:
        raise MigrationDrift(
            "database migration history is not a continuous catalog prefix"
        )
    for version, checksum in stored_rows:
        migration = by_version[version]
        if migration.checksum != checksum:
            raise MigrationDrift(f"checksum drift for migration {version}")
    return stored_rows


def _require_idle_connection_for_phases(
    connection: Any, phases: Sequence[tuple[str, str] | None]
) -> None:
    if not any(phase is not None for phase in phases):
        return
    info = getattr(connection, "info", None)
    status = getattr(info, "transaction_status", None)
    if status is not None and status != TransactionStatus.IDLE:
        raise MigrationDrift("phased migrations require an idle database connection")


def apply_migrations(
    resource: Any,
    *,
    role: str | None = None,
    migrations: Sequence[Migration] | None = None,
) -> list[str]:
    catalog = list(migrations) if migrations is not None else load_migrations()
    by_version = {migration.version: migration for migration in catalog}
    phases = [_migration_phases(migration) for migration in catalog]
    try:
        with _connection(resource) as connection:
            _require_idle_connection_for_phases(connection, phases)
            lock_acquired = False
            try:
                with connection.transaction():
                    connection.execute(
                        "select pg_advisory_lock(hashtext(%s))", (_MIGRATION_LOCK,)
                    )
                lock_acquired = True

                applied: list[str] = []
                with connection.transaction():
                    _set_local_role(connection, role)
                    connection.execute("create schema if not exists control")
                    connection.execute(_SCHEMA_MIGRATIONS_DDL)
                    stored_rows = _validate_history(connection, catalog, by_version)
                    stored = dict(stored_rows)
                    next_index = 0
                    while next_index < len(catalog):
                        migration = catalog[next_index]
                        if migration.version in stored:
                            next_index += 1
                            continue
                        if phases[next_index] is not None:
                            break
                        _set_local_role(connection, role)
                        connection.execute(migration.sql)
                        _set_local_role(connection, role)
                        connection.execute(
                            "insert into control.schema_migrations "
                            "(version, checksum) values (%s, %s)",
                            (migration.version, migration.checksum),
                        )
                        applied.append(migration.version)
                        next_index += 1

                while next_index < len(catalog):
                    migration = catalog[next_index]
                    migration_phases = phases[next_index]
                    if migration_phases is not None:
                        for phase in migration_phases:
                            with connection.transaction():
                                _set_local_role(connection, role)
                                connection.execute(phase)
                        with connection.transaction():
                            _set_local_role(connection, role)
                            connection.execute(
                                "insert into control.schema_migrations "
                                "(version, checksum) values (%s, %s)",
                                (migration.version, migration.checksum),
                            )
                        applied.append(migration.version)
                        next_index += 1
                        continue

                    with connection.transaction():
                        _set_local_role(connection, role)
                        while next_index < len(catalog):
                            migration = catalog[next_index]
                            if phases[next_index] is not None:
                                break
                            _set_local_role(connection, role)
                            connection.execute(migration.sql)
                            _set_local_role(connection, role)
                            connection.execute(
                                "insert into control.schema_migrations "
                                "(version, checksum) values (%s, %s)",
                                (migration.version, migration.checksum),
                            )
                            applied.append(migration.version)
                            next_index += 1
                return applied
            finally:
                if lock_acquired:
                    with connection.transaction():
                        connection.execute(
                            "select pg_advisory_unlock(hashtext(%s))",
                            (_MIGRATION_LOCK,),
                        )
    except MigrationDrift:
        raise
    except Exception as exc:
        import os as _os

        if _os.getenv("NEX_EIOS_MIGRATION_DEBUG", "").strip() == "1":
            raise
        # 只记异常**类型名**:2026-09-03 之前这里既 from None 又不记日志,于是
        # 发版失败时只剩「PostgreSQL storage is unavailable」一句,连「是超时、
        # 连接被服务端掐断、还是别的」都答不上来 —— 昨夜 9 败与今日这条 scale
        # 红都卡在这里。类型名(OperationalError / TimeoutError / …)恰好回答它。
        # ⚠️ from None 必须保留:底层消息里可能带 DSN(含密码),见
        # tests/security/test_data_protection_redaction.py。
        _LOGGER.warning("migration storage unavailable: %s", type(exc).__name__)
        raise StorageUnavailable("PostgreSQL storage is unavailable") from None


def current_revision(resource: Any) -> str:
    try:
        with _connection(resource) as connection:
            rows = connection.execute(
                "select version, checksum from control.schema_migrations order by version"
            ).fetchall()
    except Exception as exc:
        _LOGGER.warning("revision read unavailable: %s", type(exc).__name__)
        raise StorageUnavailable("PostgreSQL storage is unavailable") from None
    return max((str(version) for version, _checksum in rows), default="none")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apply NEX-EIOS PostgreSQL migrations")
    parser.add_argument(
        "--database-url",
        default=os.getenv("NEX_EIOS_DATABASE_URL", "").strip(),
    )
    parser.add_argument("--role")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not args.database_url:
        parser.error("--database-url or NEX_EIOS_DATABASE_URL is required")
    settings = StorageSettings(mode="postgres", database_url=args.database_url)
    pool = None
    try:
        pool = create_pool(settings, open_pool=True)
        for version in apply_migrations(pool, role=args.role):
            print(version)
    except (MigrationDrift, StorageUnavailable) as exc:
        parser.exit(1, f"error: {exc}\n")
    finally:
        if pool is not None:
            pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
