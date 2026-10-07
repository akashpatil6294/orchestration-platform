"""Alembic environment.

The database URL comes from application settings (``DATABASE_URL`` / ``.env``) so
migrations and the running app can never disagree about which database they use.
"""
from __future__ import annotations

from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import JSON, pool

from app.config import settings
from app.database import build_engine
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# A shorter lock timeout keeps migrations from hanging behind a busy API.
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))


def _compare_server_default(
    context: Any,
    inspected_column: Any,
    metadata_column: Any,
    inspected_default: Any,
    metadata_default: Any,
    rendered_metadata_default: Any,
) -> bool | None:
    """Skip server-default comparison for JSON columns.

    PostgreSQL's ``json`` type has no equality operator, so Alembic's default
    comparison emits ``SELECT '{}'::json = '{}'`` which raises
    ``UndefinedFunction: operator does not exist: json = unknown``.

    Returning ``False`` tells Alembic the defaults match (i.e. "no change"),
    which is the safe default for these columns. ``None`` means "fall back to
    Alembic's normal comparison logic" for everything else.
    """
    if isinstance(metadata_column.type, JSON):
        return False
    return None


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing (``alembic upgrade head --sql``)."""
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=_compare_server_default,
        render_as_batch=settings.is_sqlite,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = build_engine()
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=_compare_server_default,
            # SQLite cannot ALTER most columns, so Alembic recreates tables.
            render_as_batch=settings.is_sqlite,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()