"""PostgreSQL persistence infrastructure."""

from eios.adapters.postgres.database import StorageUnavailable, create_pool

__all__ = ["StorageUnavailable", "create_pool"]
