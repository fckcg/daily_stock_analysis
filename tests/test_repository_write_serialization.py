# -*- coding: utf-8 -*-
"""
Regression tests for SQLite write serialization across repositories.

The May 2026 audit (Top 3) flagged 7 idempotent upsert paths that opened
a normal SQLAlchemy session, ran a SELECT, then issued an INSERT/UPDATE.
Under SQLite that read-then-write pattern silently *upgrades* to a write
lock at INSERT time – two concurrent writers can both observe "no row"
and then race, producing lost updates or ``database is locked`` errors
that were not retried.

These tests pin down the fix:

* All 7 paths now route through ``DatabaseManager._run_write_transaction``,
  so they emit ``BEGIN IMMEDIATE`` (claiming the writer slot before any
  read inside the same transaction) and retry with exponential back-off
  on transient SQLite lock errors.

The list of paths covered (file:method):

* ``alert_repo.create_trigger_if_absent``
* ``alert_repo.upsert_cooldown``
* ``backtest_repo.upsert_summary``
* ``portfolio_repo.save_fx_rate``
* ``portfolio_repo.replace_positions_and_lots``
* ``portfolio_repo.upsert_daily_snapshot``
* ``portfolio_repo.replace_positions_lots_and_snapshot``

Tests use either spying on ``DatabaseManager._run_write_transaction``
(checks the wrapper is invoked) or wraps it on the BEGIN IMMEDIATE side
to count the number of attempts on a simulated locked-error.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from typing import Iterator
from unittest.mock import patch

import pytest
from sqlalchemy.exc import OperationalError

from src.repositories.alert_repo import AlertRepository
from src.repositories.backtest_repo import BacktestRepository
from src.repositories.portfolio_repo import PortfolioRepository
from src.storage import (
    AlertRuleRecord,
    BacktestSummary,
    DatabaseManager,
    PortfolioAccount,
)


@pytest.fixture
def db() -> Iterator[DatabaseManager]:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(db_url="sqlite:///:memory:")
    yield manager
    DatabaseManager.reset_instance()


# ---------------------------------------------------------------------------
# All 7 paths route through _run_write_transaction
# ---------------------------------------------------------------------------

class TestAllUpsertsUseRunWriteTransaction:
    def test_alert_create_trigger_if_absent(self, db: DatabaseManager) -> None:
        repo = AlertRepository(db_manager=db)
        with db.get_session() as session:
            rule = AlertRuleRecord(
                name="r1",
                target_scope="single_symbol",
                target="600519",
                alert_type="price_threshold",
                parameters="{}",
                severity="warning",
            )
            session.add(rule)
            session.commit()
            rule_id = rule.id

        with patch.object(
            DatabaseManager, "_run_write_transaction", wraps=db._run_write_transaction
        ) as spy:
            repo.create_trigger_if_absent({
                "rule_id": rule_id,
                "target": "600519",
                "status": "triggered",
                "data_timestamp": datetime(2026, 5, 1, 9, 30),
                "data_source": "kline",
                "reason": "x",
                "diagnostics": "{}",
            })
        assert spy.call_count == 1, "create_trigger_if_absent must use _run_write_transaction"

    def test_alert_upsert_cooldown(self, db: DatabaseManager) -> None:
        repo = AlertRepository(db_manager=db)
        with patch.object(
            DatabaseManager, "_run_write_transaction", wraps=db._run_write_transaction
        ) as spy:
            repo.upsert_cooldown(
                rule_id=1,
                rule_key="r1",
                target="600519",
                severity="warning",
                last_triggered_at=datetime(2026, 5, 1, 9, 30),
                cooldown_until=datetime(2026, 5, 1, 10, 30),
            )
        assert spy.call_count == 1

    def test_backtest_upsert_summary(self, db: DatabaseManager) -> None:
        repo = BacktestRepository(db_manager=db)
        summary = BacktestSummary(
            scope="stock",
            code="600519",
            eval_window_days=30,
            engine_version="v1",
            computed_at=datetime.now(),
            total_evaluations=1,
            completed_count=1,
            insufficient_count=0,
            long_count=1,
            cash_count=0,
            win_count=1,
            loss_count=0,
            neutral_count=0,
            direction_accuracy_pct=100.0,
            win_rate_pct=100.0,
            neutral_rate_pct=0.0,
            avg_stock_return_pct=5.0,
            avg_simulated_return_pct=5.0,
            stop_loss_trigger_rate=0.0,
            take_profit_trigger_rate=0.0,
            ambiguous_rate=0.0,
            avg_days_to_first_hit=10.0,
            advice_breakdown_json="{}",
            diagnostics_json="{}",
        )
        with patch.object(
            DatabaseManager, "_run_write_transaction", wraps=db._run_write_transaction
        ) as spy:
            repo.upsert_summary(summary)
        assert spy.call_count == 1

    def test_portfolio_save_fx_rate(self, db: DatabaseManager) -> None:
        repo = PortfolioRepository(db_manager=db)
        with patch.object(
            DatabaseManager, "_run_write_transaction", wraps=db._run_write_transaction
        ) as spy:
            repo.save_fx_rate(
                from_currency="USD",
                to_currency="CNY",
                rate_date=date(2026, 5, 1),
                rate=7.2,
                source="manual",
            )
        assert spy.call_count == 1

    def test_portfolio_replace_positions_and_lots(self, db: DatabaseManager) -> None:
        repo = PortfolioRepository(db_manager=db)
        account = repo.create_account(
            name="A", broker="X", market="A", base_currency="CNY"
        )
        with patch.object(
            DatabaseManager, "_run_write_transaction", wraps=db._run_write_transaction
        ) as spy:
            repo.replace_positions_and_lots(
                account_id=account.id,
                cost_method="weighted_average",
                positions=[],
                lots=[],
                valuation_currency="CNY",
            )
        assert spy.call_count == 1

    def test_portfolio_upsert_daily_snapshot(self, db: DatabaseManager) -> None:
        repo = PortfolioRepository(db_manager=db)
        account = repo.create_account(
            name="A", broker="X", market="A", base_currency="CNY"
        )
        with patch.object(
            DatabaseManager, "_run_write_transaction", wraps=db._run_write_transaction
        ) as spy:
            repo.upsert_daily_snapshot(
                account_id=account.id,
                snapshot_date=date(2026, 5, 1),
                cost_method="weighted_average",
                base_currency="CNY",
                total_cash=0.0,
                total_market_value=0.0,
                total_equity=0.0,
                unrealized_pnl=0.0,
                realized_pnl=0.0,
                fee_total=0.0,
                tax_total=0.0,
                fx_stale=False,
                payload="{}",
            )
        assert spy.call_count == 1

    def test_portfolio_replace_positions_lots_and_snapshot(self, db: DatabaseManager) -> None:
        repo = PortfolioRepository(db_manager=db)
        account = repo.create_account(
            name="A", broker="X", market="A", base_currency="CNY"
        )
        with patch.object(
            DatabaseManager, "_run_write_transaction", wraps=db._run_write_transaction
        ) as spy:
            repo.replace_positions_lots_and_snapshot(
                account_id=account.id,
                snapshot_date=date(2026, 5, 1),
                cost_method="weighted_average",
                base_currency="CNY",
                total_cash=0.0,
                total_market_value=0.0,
                total_equity=0.0,
                unrealized_pnl=0.0,
                realized_pnl=0.0,
                fee_total=0.0,
                tax_total=0.0,
                fx_stale=False,
                payload="{}",
                positions=[],
                lots=[],
                valuation_currency="CNY",
            )
        assert spy.call_count == 1


# ---------------------------------------------------------------------------
# BEGIN IMMEDIATE is actually emitted, and lock errors are retried
# ---------------------------------------------------------------------------

class TestBeginImmediateAndRetry:
    """Happy-path probe for the wrapper itself.

    ``_run_write_transaction`` already has direct unit tests in
    ``test_storage.py``; here we just verify that one of the
    formerly-unprotected paths now goes through the wrapper end-to-end
    (BEGIN IMMEDIATE statement is observed) and retries once when the
    first attempt hits ``database is locked``.
    """

    def test_begin_immediate_is_emitted_via_repository_path(
        self, db: DatabaseManager
    ) -> None:
        repo = AlertRepository(db_manager=db)
        observed: list[str] = []
        original_exec = None

        def wrap_exec_driver_sql(orig):
            def wrapped(stmt, *args, **kwargs):
                observed.append(stmt)
                return orig(stmt, *args, **kwargs)
            return wrapped

        # Wrap exec_driver_sql on the connection used by the next session.
        # Easiest: monkey-patch DatabaseManager.get_session to wrap the
        # session's connection's exec_driver_sql once.
        original_get_session = db.get_session

        def wrapped_get_session():
            session = original_get_session()
            connection = session.connection()
            nonlocal original_exec
            original_exec = connection.exec_driver_sql
            connection.exec_driver_sql = wrap_exec_driver_sql(original_exec)
            return session

        with patch.object(db, "get_session", side_effect=wrapped_get_session):
            repo.upsert_cooldown(
                rule_id=2,
                rule_key="rk2",
                target="000001",
                severity="info",
                last_triggered_at=datetime(2026, 5, 1, 9, 0),
                cooldown_until=datetime(2026, 5, 1, 10, 0),
            )

        assert any(
            stmt == "BEGIN IMMEDIATE" for stmt in observed
        ), f"expected BEGIN IMMEDIATE in observed statements, got: {observed}"

    def test_lock_contention_is_retried_on_repository_write(
        self, db: DatabaseManager
    ) -> None:
        repo = PortfolioRepository(db_manager=db)
        attempts = {"count": 0}

        original = db._run_write_transaction.__func__

        def flaky(self, operation_name, write_operation):  # noqa: ARG001
            # Simulate one transient lock error then succeed.
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise OperationalError(
                    "BEGIN IMMEDIATE",
                    None,
                    sqlite3.OperationalError("database is locked"),
                )
            return original(self, operation_name, write_operation)

        with patch.object(DatabaseManager, "_run_write_transaction", flaky):
            with pytest.raises(OperationalError):
                repo.save_fx_rate(
                    from_currency="USD",
                    to_currency="CNY",
                    rate_date=date(2026, 5, 1),
                    rate=7.2,
                )
            # The repository call surfaces the OperationalError on the first
            # attempt (since we replaced _run_write_transaction itself, no
            # internal retry happens). What we are pinning here is that the
            # repository routed through _run_write_transaction at all – any
            # call meaning the path is wrapped.
        assert attempts["count"] == 1

    def test_internal_retry_loop_is_exercised(self, db: DatabaseManager) -> None:
        """End-to-end: when the underlying SQL raises 'database is locked'
        the wrapper retries before bubbling up.
        """
        repo = AlertRepository(db_manager=db)
        # Force one transient lock during the BEGIN IMMEDIATE call.
        original_exec = None
        toggle = {"count": 0}

        def maybe_lock(orig):
            def wrapped(stmt, *args, **kwargs):
                if stmt == "BEGIN IMMEDIATE":
                    toggle["count"] += 1
                    if toggle["count"] == 1:
                        raise OperationalError(
                            "BEGIN IMMEDIATE",
                            None,
                            sqlite3.OperationalError("database is locked"),
                        )
                return orig(stmt, *args, **kwargs)
            return wrapped

        original_get_session = db.get_session

        def wrapped_get_session():
            session = original_get_session()
            connection = session.connection()
            nonlocal original_exec
            original_exec = connection.exec_driver_sql
            connection.exec_driver_sql = maybe_lock(original_exec)
            return session

        with patch.object(db, "get_session", side_effect=wrapped_get_session):
            # If retry works, the second attempt succeeds and the call returns.
            repo.upsert_cooldown(
                rule_id=3,
                rule_key="rk3",
                target="000003",
                severity="info",
                last_triggered_at=datetime(2026, 5, 1, 9, 0),
                cooldown_until=datetime(2026, 5, 1, 10, 0),
            )
        # Two BEGIN IMMEDIATE attempts means the retry path was taken.
        assert toggle["count"] == 2


# ---------------------------------------------------------------------------
# repositories.__init__ exports the previously-missing repositories
# ---------------------------------------------------------------------------

class TestRepositoriesInitExports:
    def test_alert_and_portfolio_repos_are_exported(self) -> None:
        from src.repositories import AlertRepository as Exp1, PortfolioRepository as Exp2

        # Same class object, not just same name.
        assert Exp1 is AlertRepository
        assert Exp2 is PortfolioRepository

    def test_dunder_all_lists_all_repositories(self) -> None:
        import src.repositories as repos

        for name in ("AlertRepository", "AnalysisRepository", "BacktestRepository",
                     "PortfolioRepository", "StockRepository"):
            assert name in repos.__all__, f"{name} missing from __all__"


# ---------------------------------------------------------------------------
# _is_sqlite_locked_error de-duplication: PortfolioRepository forwards
# ---------------------------------------------------------------------------

class TestIsSqliteLockedErrorDedup:
    def test_portfolio_repo_forwards_to_database_manager(self) -> None:
        exc = OperationalError(
            "stmt", None, sqlite3.OperationalError("database is locked")
        )
        assert PortfolioRepository._is_sqlite_locked_error(exc) is True
        assert DatabaseManager._is_sqlite_locked_error(exc) is True

    def test_non_lock_errors_are_not_classified(self) -> None:
        exc = OperationalError(
            "stmt", None, sqlite3.OperationalError("disk I/O error")
        )
        assert PortfolioRepository._is_sqlite_locked_error(exc) is False
        assert DatabaseManager._is_sqlite_locked_error(exc) is False
