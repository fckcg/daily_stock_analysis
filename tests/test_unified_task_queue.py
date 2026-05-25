# -*- coding: utf-8 -*-
"""
Regression tests for the unified analysis task queue (May 2026 audit Top 5).

The bot path used to live on its own ``TaskService`` ThreadPoolExecutor that
was invisible to ``AnalysisTaskQueue`` (the API-side queue). Same stock
could be analyzed twice concurrently – one by ``/analyze 600519`` from a
bot session, one by ``POST /api/v1/analysis/start`` from web – because the
two pools maintained independent dedupe sets.

These tests pin down the unified behaviour:

* ``TaskService.submit_analysis`` routes through ``AnalysisTaskQueue``.
* Submitting the same stock from both surfaces returns the same task_id;
  the second call gets a ``duplicated=True`` marker rather than spawning
  a parallel worker.
* ``source_message`` is propagated all the way through
  ``AnalysisTaskQueue → AnalysisService → StockAnalysisPipeline`` so
  bot integrations can still push results back to the originating chat.
* Legacy ``TaskService._run_analysis`` (covered by ``test_task_service.py``)
  continues to work as a backward-compat stub.
"""

from __future__ import annotations

import os
import sys
import threading
import unittest
from types import ModuleType, SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

from src.enums import ReportType
from src.services import task_queue as task_queue_module
from src.services.task_queue import AnalysisTaskQueue, DuplicateTaskError
from src.services.task_service import TaskService, get_task_service


def _reset_queue_singleton() -> AnalysisTaskQueue:
    """Build a fresh ``AnalysisTaskQueue`` for the test, replacing the singleton."""
    AnalysisTaskQueue._instance = None  # type: ignore[attr-defined]
    queue = AnalysisTaskQueue(max_workers=2)
    return queue


class TestTaskServiceUnifiedPath(unittest.TestCase):
    """
    bot ``TaskService`` should now delegate to ``AnalysisTaskQueue`` -- not
    create its own ``ThreadPoolExecutor``.
    """

    def setUp(self) -> None:
        self.queue = _reset_queue_singleton()
        TaskService._instance = None  # type: ignore[attr-defined]

    def tearDown(self) -> None:
        AnalysisTaskQueue._instance = None  # type: ignore[attr-defined]
        TaskService._instance = None  # type: ignore[attr-defined]

    def test_submit_analysis_delegates_to_task_queue(self) -> None:
        captured: Dict[str, Any] = {}

        def fake_submit_task(**kwargs: Any) -> SimpleNamespace:
            captured.update(kwargs)
            return SimpleNamespace(task_id="abc-123")

        service = get_task_service()
        with patch.object(service._queue, "submit_task", side_effect=fake_submit_task):
            result = service.submit_analysis(
                code="600519",
                report_type=ReportType.SIMPLE,
                source_message=SimpleNamespace(user_id="u1"),
                query_source="bot",
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["task_id"], "abc-123")
        self.assertEqual(result["report_type"], ReportType.SIMPLE.value)
        # bot context fields propagate through:
        self.assertEqual(captured["stock_code"], "600519")
        self.assertEqual(captured["report_type"], ReportType.SIMPLE.value)
        self.assertEqual(captured["query_source"], "bot")
        self.assertEqual(captured["source_message"].user_id, "u1")

    def test_duplicate_submission_returns_existing_task(self) -> None:
        service = get_task_service()
        with patch.object(
            service._queue,
            "submit_task",
            side_effect=DuplicateTaskError(
                stock_code="600519", existing_task_id="task-orig"
            ),
        ):
            result = service.submit_analysis(
                code="600519",
                report_type=ReportType.SIMPLE,
            )

        self.assertTrue(result["success"])
        self.assertTrue(result.get("duplicated"))
        # Crucially: same task_id is reused, no new worker is started.
        self.assertEqual(result["task_id"], "task-orig")

    def test_get_task_status_reads_from_queue(self) -> None:
        service = get_task_service()
        with patch.object(
            service._queue, "submit_task"
        ) as submit, patch.object(
            service._queue, "get_task"
        ) as get_task:
            submit.return_value = SimpleNamespace(task_id="t-1")
            from src.services.task_queue import TaskInfo, TaskStatus

            stored = TaskInfo(
                task_id="t-1",
                stock_code="600519",
                status=TaskStatus.PROCESSING,
                progress=42,
                report_type="simple",
            )
            get_task.return_value = stored

            service.submit_analysis(code="600519", report_type=ReportType.SIMPLE)
            status = service.get_task_status("t-1")

        self.assertIsNotNone(status)
        self.assertEqual(status["task_id"], "t-1")
        self.assertEqual(status["status"], TaskStatus.PROCESSING.value)


class TestAnalysisServicePropagatesSourceMessage(unittest.TestCase):
    """
    ``AnalysisService.analyze_stock`` should accept ``source_message`` and
    ``query_source`` and forward them to ``StockAnalysisPipeline``. Without
    this the unified path would lose the ability to push bot results back
    to the originating chat.
    """

    def test_pipeline_receives_source_message_and_query_source(self) -> None:
        from src.services.analysis_service import AnalysisService

        captured_kwargs: Dict[str, Any] = {}

        class _FakePipeline:
            def __init__(self, **kwargs: Any) -> None:
                captured_kwargs.update(kwargs)

            def process_single_stock(self, **_: Any) -> SimpleNamespace:
                return SimpleNamespace(
                    success=False,
                    error_message="stub - early return",
                    code="600519",
                )

        # Build a fake ``src.core.pipeline`` module so ``AnalysisService`` picks
        # up our pipeline class on its lazy import.
        fake_pipeline_mod = ModuleType("src.core.pipeline")
        fake_pipeline_mod.StockAnalysisPipeline = _FakePipeline  # type: ignore[attr-defined]

        with patch.dict("sys.modules", {"src.core.pipeline": fake_pipeline_mod}), patch(
            "src.config.get_config", return_value=SimpleNamespace()
        ):
            svc = AnalysisService()
            svc.analyze_stock(
                stock_code="600519",
                report_type="simple",
                source_message=SimpleNamespace(user_id="u-bot"),
                query_source="bot",
                query_id="qid-1",
                send_notification=False,
            )

        self.assertEqual(captured_kwargs.get("query_source"), "bot")
        self.assertEqual(captured_kwargs.get("query_id"), "qid-1")
        self.assertEqual(captured_kwargs.get("source_message").user_id, "u-bot")


class TestQueueSubmitTaskAcceptsSourceMessage(unittest.TestCase):
    """``AnalysisTaskQueue.submit_task`` should pass ``source_message`` to
    ``_execute_task`` (and through to ``AnalysisService``)."""

    def setUp(self) -> None:
        self.queue = _reset_queue_singleton()

    def tearDown(self) -> None:
        AnalysisTaskQueue._instance = None  # type: ignore[attr-defined]

    def test_execute_task_invoked_with_source_message(self) -> None:
        captured: Dict[str, Any] = {}

        # Stub the executor so we can inspect the dispatched args without
        # spinning up a real worker.
        class _SyncExecutor:
            def submit(self, fn, *args, **kwargs):
                captured["args"] = args
                captured["kwargs"] = kwargs

                class _DoneFuture:
                    def cancel(self_inner) -> bool:
                        return False

                return _DoneFuture()

            def shutdown(self, wait: bool = False) -> None:  # pragma: no cover
                pass

        with patch.object(
            type(self.queue), "executor", new=_SyncExecutor(), create=False
        ):
            msg = SimpleNamespace(user_id="bot-u-1")
            self.queue.submit_task(
                stock_code="600519",
                source_message=msg,
                query_source="bot",
                report_type="simple",
            )

        positional = captured["args"]
        kwargs = captured["kwargs"]
        # signature: positional = (task_id, stock_code, report_type,
        #             force_refresh, notify, skills); keyword = source_message,
        # query_source.
        self.assertEqual(len(positional), 6)
        self.assertEqual(kwargs["source_message"].user_id, "bot-u-1")
        self.assertEqual(kwargs["query_source"], "bot")


class TestTaskServiceLegacyApiSurface(unittest.TestCase):
    """
    Make sure the legacy public attributes other code (and the historical
    ``test_task_service.py``) relies on are still readable and writable.
    """

    def setUp(self) -> None:
        _reset_queue_singleton()
        TaskService._instance = None  # type: ignore[attr-defined]

    def tearDown(self) -> None:
        AnalysisTaskQueue._instance = None  # type: ignore[attr-defined]
        TaskService._instance = None  # type: ignore[attr-defined]

    def test_legacy_tasks_dict_is_readable_and_writable(self) -> None:
        service = TaskService()
        self.assertIsInstance(service._tasks, dict)
        service._tasks = {}
        service._tasks_lock = threading.Lock()
        # Directly seed the legacy dict and ensure ``get_task_status`` can
        # still find it (back-compat for tests that drive ``_run_analysis``).
        service._tasks["legacy-1"] = {
            "task_id": "legacy-1",
            "code": "600519",
            "status": "completed",
            "result": {"ok": True},
            "error": None,
        }
        snapshot = service.get_task_status("legacy-1")
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["status"], "completed")


if __name__ == "__main__":
    unittest.main()
