# -*- coding: utf-8 -*-
"""
===================================
全局异常处理器
===================================

职责：
1. 捕获未处理的异常
2. 统一错误响应格式
3. 记录错误日志

注：早期版本曾导出一个 ``ErrorHandlerMiddleware`` 类，但它从未被
``app.add_middleware`` 注册过（``api/app.py`` 仅调用 ``add_error_handlers``
注册 ``@app.exception_handler``），属于死代码且与下方 handler 重复。已删除，
统一以 FastAPI 推荐的 exception handler 形式作为唯一异常处理路径。
"""

import logging
import traceback

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def add_error_handlers(app) -> None:
    """
    添加全局异常处理器

    为 FastAPI 应用添加各类异常的处理器

    Args:
        app: FastAPI 应用实例
    """
    from fastapi import HTTPException
    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        """处理 HTTP 异常"""
        # 如果 detail 已经是 ErrorResponse 格式的 dict，直接使用
        if isinstance(exc.detail, dict) and "error" in exc.detail and "message" in exc.detail:
            return JSONResponse(
                status_code=exc.status_code,
                content=exc.detail
            )
        # 否则将 detail 包装成 ErrorResponse 格式
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": "http_error",
                "message": str(exc.detail) if exc.detail else "HTTP Error",
                "detail": None
            }
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        """处理请求验证异常"""
        return JSONResponse(
            status_code=422,
            content={
                "error": "validation_error",
                "message": "请求参数验证失败",
                "detail": exc.errors()
            }
        )

    @app.exception_handler(Exception)
    async def general_exception_handler(request: Request, exc: Exception):
        """处理通用异常"""
        logger.error(
            f"未处理的异常: {exc}\n"
            f"请求路径: {request.url.path}\n"
            f"堆栈: {traceback.format_exc()}"
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_error",
                "message": "服务器内部错误",
                "detail": None
            }
        )
