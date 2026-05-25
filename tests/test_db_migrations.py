# -*- coding: utf-8 -*-
"""
Regression tests for the alembic migration plumbing (May 2026 audit Top 2).

Until this PR ``DatabaseManager`` only called ``Base.metadata.create_all``
on startup and had no schema versioning. Any column / constraint change
silently left existing user databases stale. The fix wires alembic into
``DatabaseManager`` so:

* Fresh databases get tables from ``create_all`` AND end up stamped to
  the ``head`` revision.
* Pre-existing user databases (no ``alembic_version`` table) are stamped
  to the baseline (``0001``) on first launch and then upgraded.
* Future schema changes ride through ``alembic upgrade`` rather than
  invisible drift.

This file pins down each of those guarantees and the opt-out switch.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from typing import Iterator
from unittest.mock import patch

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _alembic_head_revision() -> str:
    cfg = AlembicConfig(os.path.join(PROJECT_ROOT, "alembic.ini"))
    return ScriptDirectory.from_config(cfg).get_current_head()


@contextmanager
def _temp_sqlite_db() -> Iterator[str]:
    fd, path = tempfile.mkstemp(prefix="dsa_alembic_", suffix=".db")
    os.close(fd)
    try:
        yield path
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _read_alembic_version(db_path: str) -> list:
    """Return rows of ``SELECT version_num FROM alembic_version`` (or [])."""
    con = sqlite3.connect(db_path)
    try:
        cur = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version'"
        )
        if cur.fetchone() is None:
            return []
        return list(con.execute("SELECT version_num FROM alembic_version").fetchall())
    finally:
        con.close()


class TestFreshDatabaseIsStampedToHead(unittest.TestCase):
    """A brand-new DB should be created AND end up stamped to head."""

    def test_first_launch_writes_alembic_version(self) -> None:
        from src.storage import DatabaseManager

        head_rev = _alembic_head_revision()
        self.assertIsNotNone(head_rev, "alembic must have at least one revision (baseline)")

        with _temp_sqlite_db() as path:
            DatabaseManager.reset_instance()
            try:
                DatabaseManager(db_url=f"sqlite:///{path}")

                rows = _read_alembic_version(path)
                self.assertEqual(
                    rows,
                    [(head_rev,)],
                    "fresh DB must be stamped to alembic head after first launch",
                )
            finally:
                DatabaseManager.reset_instance()

    def test_second_launch_is_a_noop(self) -> None:
        """Re-opening a DB already at head should not error or duplicate rows."""
        from src.storage import DatabaseManager

        head_rev = _alembic_head_revision()

        with _temp_sqlite_db() as path:
            DatabaseManager.reset_instance()
            try:
                DatabaseManager(db_url=f"sqlite:///{path}")
                DatabaseManager.reset_instance()
                DatabaseManager(db_url=f"sqlite:///{path}")
                self.assertEqual(_read_alembic_version(path), [(head_rev,)])
            finally:
                DatabaseManager.reset_instance()


class TestLegacyDatabaseAutoStamps(unittest.TestCase):
    """A pre-existing DB with tables but no ``alembic_version`` row should
    be auto-stamped to the baseline. This covers every user that was
    running the project before this PR landed.
    """

    def test_legacy_db_gets_stamped_to_baseline(self) -> None:
        from src.storage import Base, DatabaseManager
        from sqlalchemy import create_engine

        head_rev = _alembic_head_revision()

        with _temp_sqlite_db() as path:
            # Simulate a "pre-alembic" install: tables created via the old
            # ``create_all`` path with no alembic_version row.
            engine = create_engine(f"sqlite:///{path}")
            Base.metadata.create_all(engine)
            engine.dispose()

            self.assertEqual(_read_alembic_version(path), [])

            DatabaseManager.reset_instance()
            try:
                DatabaseManager(db_url=f"sqlite:///{path}")
                self.assertEqual(_read_alembic_version(path), [(head_rev,)])
            finally:
                DatabaseManager.reset_instance()


class TestMigrationOptOut(unittest.TestCase):
    """``DSA_DISABLE_DB_MIGRATIONS=1`` should skip the upgrade entirely."""

    def test_disabled_flag_skips_alembic_run(self) -> None:
        from src.storage import DatabaseManager

        with _temp_sqlite_db() as path, patch.dict(
            os.environ, {"DSA_DISABLE_DB_MIGRATIONS": "1"}
        ):
            DatabaseManager.reset_instance()
            try:
                DatabaseManager(db_url=f"sqlite:///{path}")
                # ``Base.metadata.create_all`` still runs, but no
                # ``alembic_version`` table should exist.
                self.assertEqual(_read_alembic_version(path), [])
            finally:
                DatabaseManager.reset_instance()


class TestAlembicCliWorks(unittest.TestCase):
    """``alembic upgrade head`` from the command line should still work –
    operators sometimes need to apply migrations against an offline DB.
    """

    def test_cli_upgrade_against_fresh_db(self) -> None:
        from alembic import command

        head_rev = _alembic_head_revision()
        cfg = AlembicConfig(os.path.join(PROJECT_ROOT, "alembic.ini"))

        with _temp_sqlite_db() as path:
            cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
            command.upgrade(cfg, "head")
            self.assertEqual(_read_alembic_version(path), [(head_rev,)])


if __name__ == "__main__":
    unittest.main()
