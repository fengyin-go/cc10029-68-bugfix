"""路由汇总：回执等固定路径在前，通用资源路由最后注册，避免被 /{module} 抢先匹配。"""
from __future__ import annotations

from app.routers import receipts, resource

ROUTERS = [receipts, resource]
