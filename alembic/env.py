# -*- coding: utf-8 -*-
"""
Alembic environment for daily_stock_analysis.

Runtime contract:

* The connection / DSN is **not** read from ``alembic.ini``. Migrations are
  invoked from inside ``DatabaseManager._apply_alembic_migrations`` using
  the same ``Engine`` the rest of the app uses. This keeps SQLite WAL
  pragmas, busy timeouts, and connection options consistent.
* Manual ``alembic upgrade head`` from the shell uses the URL from
  ``alembic.ini`` (defaults to ``sqlite:///stock_analysis.db``); operators
  should override via ``-x sqlalchemy.url=...`` when targeting a non-default
  database.
* ``target_metadata`` is the central ``Base.metadata`` from ``src.storage``
  so ``alembic revision --autogenerate`` can detect ORM drift.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import engine_from_config, pool

# Allow importing project modules when alembic is invoked from the project root.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.storage import Base  # noqa: E402  (import after sys.path adjustment)


# this is the Alembic Config object, which provides access to the values
# within the .ini file in use.
config = context.config

# Interpret the config file for Python logging when running standalone.
# We pass ``disable_existing_loggers=False`` because ``fileConfig`` defaults
# to ``True``, which would silently mute every logger not declared in
# alembic.ini -- including ``caplog`` handlers attached by pytest. That made
# 39 unrelated logging-assertion tests fail when alembic ran on startup.
if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name, disable_existing_loggers=False)
    except Exception:
        # Embedded invocation (DatabaseManager) wires its own logger;
        # silently fall through if alembic.ini logging keys are not present.
        pass

# Add your model's MetaData object here for 'autogenerate' support.
target_metadata = Base.metadata


def _get_attribute(name: str, default: Any = None) -> Any:
    """Read attributes injected by the embedded driver (``EnvironmentContext``)."""
    return context.get_x_argument(as_dictionary=True).get(name, default)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Generates SQL scripts without connecting to the database. Used by
    operators who want to review the SQL before applying.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite ALTER TABLE is limited; alembic batch mode rewrites the
        # table to apply changes that vanilla SQLite cannot (e.g.
        # DROP COLUMN, change nullability).
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    Reuses an existing ``Connection`` injected by the embedded driver if
    one is provided in ``config.attributes``; otherwise creates an Engine
    from ``alembic.ini`` settings.

    Operators can override ``sqlalchemy.url`` from the command line via
    ``alembic -x sqlalchemy.url=sqlite:///some.db ...``. This avoids the
    surprising "phantom stock_analysis.db in cwd" you otherwise hit
    because ``alembic.ini`` defaults to a relative SQLite path.
    """
    connectable = config.attributes.get("connection")

    if connectable is None:
        section = config.get_section(config.config_ini_section, {}) or {}
        x_url = _get_attribute("sqlalchemy.url")
        if x_url:
            section["sqlalchemy.url"] = x_url
        # Standalone invocation (e.g. ``alembic upgrade head`` from CLI).
        connectable = engine_from_config(
            section,
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )

    if hasattr(connectable, "connect"):
        # Engine (CLI path).
        with connectable.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                render_as_batch=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    else:
        # Connection (embedded path: DatabaseManager passes an active conn).
        context.configure(
            connection=connectable,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
