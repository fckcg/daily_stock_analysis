# -*- coding: utf-8 -*-
"""
===================================
API 中间件模块初始化
===================================

职责：
1. 导出活跃使用的中间件入口

历史：早期版本曾从这里导出 ``ErrorHandlerMiddleware``，但它从未被
``app.add_middleware`` 注册过，属于死代码并已删除。错误处理统一在
``api/middlewares/error_handler.add_error_handlers`` 内通过
``@app.exception_handler`` 注册（FastAPI 推荐方式）。
"""

from api.middlewares.auth import AuthMiddleware, add_auth_middleware
from api.middlewares.error_handler import add_error_handlers

__all__ = [
    "AuthMiddleware",
    "add_auth_middleware",
    "add_error_handlers",
]
