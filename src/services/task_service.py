# -*- coding: utf-8 -*-
"""
===================================
异步任务服务层 (兼容包装)
===================================

历史上 ``TaskService`` 维护着自己独立的 ``ThreadPoolExecutor`` (max_workers=3)
和任务字典，与 ``AnalysisTaskQueue`` (api 路径) 平行存在。两套池互不可见，
出现过同一只股票在 bot/api 两条路径上被同时分析的并发竞争 (May 2026 audit Top 5)。

本模块现在是 ``AnalysisTaskQueue`` 的薄兼容层：

* 公开 API（``submit_analysis`` / ``get_task_status`` / ``list_tasks`` /
  ``get_analysis_history``）保持不变，bot 调用方无需改动。
* 内部统一走 ``AnalysisTaskQueue.submit_task``，享受其 dedupe + SSE +
  动态 max_workers + 任务历史清理能力。
* 重复提交同一只股票时，旧实现会启动第二条分析线程；现在会复用同一个
  在途 task_id，向调用方返回 ``"已存在"`` 风格的成功响应。

仅保留 ``_run_analysis`` 作为兼容存根，供历史单元测试探测失败处理；
新代码应直接使用 ``get_task_queue().submit_task(...)``。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from src.enums import ReportType
from src.services.task_queue import (
    AnalysisTaskQueue,
    DuplicateTaskError,
    TaskInfo,
    TaskStatus,
    get_task_queue,
)
from src.storage import get_db

logger = logging.getLogger(__name__)


def _task_info_to_legacy_dict(task: TaskInfo) -> Dict[str, Any]:
    """Render a ``TaskInfo`` in the dict shape the legacy bot path expects."""
    payload = task.to_dict()
    legacy: Dict[str, Any] = {
        "task_id": payload["task_id"],
        "code": payload.get("stock_code"),
        "status": payload.get("status"),
        "start_time": payload.get("started_at") or payload.get("created_at"),
        "end_time": payload.get("completed_at"),
        "result": task.result,
        "error": payload.get("error"),
        "report_type": payload.get("report_type"),
    }
    return legacy


class TaskService:
    """
    异步任务服务（兼容包装）

    所有提交、状态查询都委托给单例 ``AnalysisTaskQueue``。两条路径合并后：

    * bot ``/analyze 600519`` 与 web ``POST /api/v1/analysis/start`` 共享
      去重表，避免同一只股票被同时分析两次。
    * SSE 订阅者（web 端）能看到 bot 触发的 ``task_*`` 事件流。
    """

    _instance: Optional["TaskService"] = None
    _lock = threading.Lock()

    def __init__(self, max_workers: int = 3):
        # max_workers 由 AnalysisTaskQueue 通过 ``sync_max_workers`` 统一管理；
        # 仅当调用方显式指定且与当前不同时同步过去（向后兼容路径）。
        if max_workers and max_workers != self._queue.max_workers:
            self._queue.sync_max_workers(max_workers, log=False)

    @property
    def _queue(self) -> AnalysisTaskQueue:
        return get_task_queue()

    @classmethod
    def get_instance(cls) -> "TaskService":
        """获取单例实例。"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------
    def submit_analysis(
        self,
        code: str,
        report_type: Union[ReportType, str] = ReportType.SIMPLE,
        source_message: Optional[Any] = None,
        save_context_snapshot: Optional[bool] = None,  # noqa: ARG002 - kept for back-compat
        query_source: str = "bot",
    ) -> Dict[str, Any]:
        """
        提交异步分析任务（统一路径）。

        Args:
            code: 股票代码
            report_type: 报告类型枚举或字符串
            source_message: 来源消息（bot 用于把结果推回原会话）
            save_context_snapshot: 旧参数，已无效；保留签名兼容；分析快照行为
                现在由 ``StockAnalysisPipeline`` 默认配置决定。
            query_source: 任务来源标识（bot/api/cli/system）。

        Returns:
            兼容字典：``{"success": bool, "code": str, "task_id": str,
            "report_type": str, ...}``。
        """
        if isinstance(report_type, str):
            report_type = ReportType.from_str(report_type)

        try:
            task = self._queue.submit_task(
                stock_code=code,
                report_type=report_type.value,
                source_message=source_message,
                query_source=query_source,
            )
            logger.info(
                "[TaskService] 已提交股票 %s 分析任务 task_id=%s report_type=%s "
                "(via AnalysisTaskQueue)",
                code,
                task.task_id,
                report_type.value,
            )
            return {
                "success": True,
                "message": "分析任务已提交，将异步执行并推送通知",
                "code": code,
                "task_id": task.task_id,
                "report_type": report_type.value,
            }
        except DuplicateTaskError as exc:
            # bot 路径接到重复请求时，我们返回带原 task_id 的成功响应，
            # 让用户知道"已经在分析中"而不是直接失败。
            logger.info(
                "[TaskService] 股票 %s 已在分析中，复用 task_id=%s",
                code,
                exc.existing_task_id,
            )
            return {
                "success": True,
                "duplicated": True,
                "message": f"股票 {code} 正在分析中，请等待结果",
                "code": code,
                "task_id": exc.existing_task_id,
                "report_type": report_type.value,
            }

    def get_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """获取任务状态（dict，旧调用方约定）。"""
        task = self._queue.get_task(task_id)
        if task is not None:
            return _task_info_to_legacy_dict(task)
        # 旧 ``_run_analysis`` 兼容路径写入的快照（仅老测试 / 旧调用方使用）。
        if hasattr(self, "_legacy_tasks"):
            with self._legacy_tasks_lock:
                snapshot = self._legacy_tasks.get(task_id)
                return dict(snapshot) if snapshot else None
        return None

    def list_tasks(self, limit: int = 20) -> List[Dict[str, Any]]:
        """列出最近的任务（dict，旧调用方约定）。"""
        merged: List[Dict[str, Any]] = [
            _task_info_to_legacy_dict(t) for t in self._queue.list_all_tasks(limit=limit)
        ]
        seen = {entry["task_id"] for entry in merged if entry.get("task_id")}
        if hasattr(self, "_legacy_tasks"):
            with self._legacy_tasks_lock:
                for snapshot in self._legacy_tasks.values():
                    if snapshot.get("task_id") not in seen:
                        merged.append(dict(snapshot))
        merged.sort(key=lambda entry: entry.get("start_time") or "", reverse=True)
        return merged[:limit]

    def get_analysis_history(
        self,
        code: Optional[str] = None,
        query_id: Optional[str] = None,
        days: int = 30,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """获取分析历史记录。"""
        db = get_db()
        records = db.get_analysis_history(code=code, query_id=query_id, days=days, limit=limit)
        return [r.to_dict() for r in records]

    # ------------------------------------------------------------------
    # 历史兼容存根
    # ------------------------------------------------------------------
    def _run_analysis(
        self,
        code: str,
        task_id: str,
        report_type: ReportType = ReportType.SIMPLE,
        source_message: Optional[Any] = None,
        save_context_snapshot: Optional[bool] = None,  # noqa: ARG002
        query_source: str = "bot",
    ) -> Dict[str, Any]:
        """
        历史内部入口。仅保留以兼容老的回归测试 (``test_task_service.py``)。

        新代码不要直接调用 —— 走 ``submit_analysis`` 即可。这里直接执行一次
        分析（不走线程池、不走 dedupe），把结果以旧形态写回内部 ``_tasks``
        以便测试断言。
        """
        if not hasattr(self, "_legacy_tasks"):
            self._legacy_tasks: Dict[str, Dict[str, Any]] = {}
            self._legacy_tasks_lock = threading.Lock()

        with self._legacy_tasks_lock:
            self._legacy_tasks[task_id] = {
                "task_id": task_id,
                "code": code,
                "status": "running",
                "start_time": datetime.now().isoformat(),
                "result": None,
                "error": None,
                "report_type": report_type.value,
            }

        try:
            from src.config import get_config
            from main import StockAnalysisPipeline

            logger.info("[TaskService][legacy] 开始分析股票: %s", code)

            config = get_config()
            pipeline = StockAnalysisPipeline(
                config=config,
                max_workers=1,
                source_message=source_message,
                query_id=task_id,
                query_source=query_source,
            )
            result = pipeline.process_single_stock(
                code=code,
                skip_analysis=False,
                single_stock_notify=True,
                report_type=report_type,
            )

            if result and getattr(result, "success", False):
                result_data = {
                    "code": result.code,
                    "name": result.name,
                    "sentiment_score": result.sentiment_score,
                    "operation_advice": result.operation_advice,
                    "trend_prediction": result.trend_prediction,
                    "analysis_summary": result.analysis_summary,
                }
                with self._legacy_tasks_lock:
                    self._legacy_tasks[task_id].update({
                        "status": "completed",
                        "end_time": datetime.now().isoformat(),
                        "result": result_data,
                    })
                return {"success": True, "task_id": task_id, "result": result_data}

            fail_message = "分析返回空结果"
            if result is not None:
                fail_message = getattr(result, "error_message", None) or fail_message
            with self._legacy_tasks_lock:
                self._legacy_tasks[task_id].update({
                    "status": "failed",
                    "end_time": datetime.now().isoformat(),
                    "error": fail_message,
                })
            return {"success": False, "task_id": task_id, "error": fail_message}

        except Exception as exc:  # pragma: no cover - mirrors historical handler
            error_msg = str(exc)
            logger.error("[TaskService][legacy] 股票 %s 分析异常: %s", code, error_msg)
            with self._legacy_tasks_lock:
                self._legacy_tasks[task_id].update({
                    "status": "failed",
                    "end_time": datetime.now().isoformat(),
                    "error": error_msg,
                })
            return {"success": False, "task_id": task_id, "error": error_msg}

    # ------------------------------------------------------------------
    # 旧测试兼容：``service._tasks`` 直接读 / 写
    # ------------------------------------------------------------------
    @property
    def _tasks(self) -> Dict[str, Dict[str, Any]]:
        if not hasattr(self, "_legacy_tasks"):
            self._legacy_tasks = {}
            self._legacy_tasks_lock = threading.Lock()
        return self._legacy_tasks

    @_tasks.setter
    def _tasks(self, value: Dict[str, Dict[str, Any]]) -> None:
        # 保留 setter 让旧测试可以重置内部状态。
        self._legacy_tasks = value
        if not hasattr(self, "_legacy_tasks_lock"):
            self._legacy_tasks_lock = threading.Lock()

    @property
    def _tasks_lock(self) -> threading.Lock:
        if not hasattr(self, "_legacy_tasks_lock"):
            self._legacy_tasks_lock = threading.Lock()
        return self._legacy_tasks_lock

    @_tasks_lock.setter
    def _tasks_lock(self, value: threading.Lock) -> None:
        self._legacy_tasks_lock = value


# ============================================================
# 便捷函数
# ============================================================

def get_task_service() -> TaskService:
    """获取任务服务单例。"""
    return TaskService.get_instance()


# ``TaskStatus`` 在历史 API 中暴露过，保留作为命名空间出口以避免破坏导入路径。
__all__ = ["TaskService", "get_task_service", "TaskStatus"]
