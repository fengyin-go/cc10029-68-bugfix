"""通用接口契约；模块字段由领域服务持有，不再为每个模块复制一份模型类。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    ok: bool
    message: str
    entry: dict[str, Any] | None = None
    receipt: dict[str, Any] | None = None


class EntryPayload(BaseModel):
    # 允许 request_id/承办组 等领域扩展字段随登记请求一起提交。
    model_config = ConfigDict(extra="allow")

    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class ActionPayload(BaseModel):
    # 动作请求可携带 request_id、group、signer、opinion 等签阅上下文。
    model_config = ConfigDict(extra="allow")

    action: str
    remark: str | None = None
    request_id: str | None = None
    group: str | int | None = None
    group_id: int | str | None = None
    signer: str | None = None
    teacher: str | None = None
    opinion: str | None = None
