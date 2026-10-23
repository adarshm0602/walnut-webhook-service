from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.config import Settings


def make_pool(settings: Settings) -> AsyncConnectionPool:
    return AsyncConnectionPool(
        conninfo=settings.database_url,
        min_size=2,
        max_size=settings.db_pool_max_size,
        timeout=0.2,
        open=False,
        kwargs={"row_factory": dict_row, "connect_timeout": 5},
        check=AsyncConnectionPool.check_connection,
    )


def migrate(database_url: str) -> None:
    """Apply versioned SQL once, serializing concurrent migration runners."""
    directory = Path(__file__).resolve().parent.parent / "migrations"
    with psycopg.connect(database_url) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(701842319)")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        for path in sorted(directory.glob("*.sql")):
            exists = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version = %s", (path.name,)
            ).fetchone()
            if not exists:
                connection.execute(path.read_text())
                connection.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,)
                )
