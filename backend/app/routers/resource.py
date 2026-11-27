"""统一资源接口：查询、导出、登记和动作按模块 key 分派到领域服务。"""
from __future__ import annotations

import csv
import inspect
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services import service_for

router = APIRouter(prefix="/api", tags=["业务资源"])


def require_service(module: str):
    service = service_for(module)
    if service is None:
        raise HTTPException(status_code=404, detail=f"未知业务模块：{module}")
    return service


@router.get("/{module}", response_model=PageResult[dict])
def list_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    service = require_service(module)
    reserved = {"keyword", "status", "page", "size"}
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key not in reserved
        and value.strip()
        and (
            key in service.LIST_FIELDS
            or key in getattr(service, "FILTER_FIELDS", set())
            or key in getattr(service, "EXTENDED_FILTER_FIELDS", set())
            # 承办组/处理人/签阅组等跨模块扩展字段也允许过滤。
            or key in {
                "承办组", "承办课程组", "承担组", "课程组", "签阅组",
                "处理人", "当前处理人", "签阅人", "request_id",
            }
        )
    }
    items, total = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{module}/export")
def export_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    service = require_service(module)
    reserved = {"keyword", "status", "page", "size"}
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key not in reserved
        and value.strip()
        and (
            key in service.LIST_FIELDS
            or key in getattr(service, "FILTER_FIELDS", set())
            or key in getattr(service, "EXTENDED_FILTER_FIELDS", set())
            # 承办组/处理人/签阅组等跨模块扩展字段也允许过滤。
            or key in {
                "承办组", "承办课程组", "承担组", "课程组", "签阅组",
                "处理人", "当前处理人", "签阅人", "request_id",
            }
        )
    }
    items, _ = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=1, size=1_000_000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *service.LIST_FIELDS, "status"])
    for item in items:
        values = [item.get("id"), *(item.get(field, "") for field in service.LIST_FIELDS),
                  item.get("status", "")]
        writer.writerow([
            "'" + str(value) if str(value or "").startswith(("=", "+", "-", "@")) else value
            for value in values
        ])
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{module}.csv"'},
    )


@router.get("/{module}/{entry_id}", response_model=dict)
def get_entry(module: str, entry_id: int) -> dict[str, Any]:
    service = require_service(module)
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"{service.ENTITY} {entry_id} 不存在或已归档")
    return entry


@router.post("/{module}", response_model=ActionResult)
def create_entry(module: str, payload: EntryPayload) -> ActionResult:
    service = require_service(module)
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message=f"{service.ENTITY}已登记", entry=entry)


@router.post("/{module}/{entry_id}/actions", response_model=ActionResult)
def run_action(
    module: str,
    entry_id: int,
    payload: ActionPayload,
    request: Request,
) -> ActionResult:
    service = require_service(module)
    action = payload.action.strip()

    # 收集签阅/解散上下文（显式字段 + 未声明的扩展字段），仅在服务方法
    # 接受 **options 时传入，保证其余模块原有两参签名不受影响。
    context: dict[str, Any] = {}
    data = payload.model_dump(exclude_none=True)
    data.pop("action", None)
    context.update(data)
    if payload.model_extra:
        context.update({k: v for k, v in payload.model_extra.items() if v is not None})

    # 支持标准幂等头：响应掉线时客户端可用同一 Idempotency-Key 重放。
    header_request_id = (
        request.headers.get("idempotency-key")
        or request.headers.get("x-request-id")
        or request.headers.get("x-idempotency-key")
    )
    if header_request_id and not context.get("request_id"):
        context["request_id"] = header_request_id

    run = service.run_action
    accepts_options = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in inspect.signature(run).parameters.values()
    )
    if accepts_options:
        entry, message = run(entry_id, action, **context)
    elif "remark" in inspect.signature(run).parameters and context.get("remark") is not None:
        entry, message = run(entry_id, action, context["remark"])
    else:
        entry, message = run(entry_id, action)

    receipt = _resolve_action_receipt(module, entry_id, action, context, entry)
    return ActionResult(
        ok=entry is not None, message=message, entry=entry, receipt=receipt
    )


def _resolve_action_receipt(
    module: str,
    entry_id: int,
    action: str,
    context: dict[str, Any],
    entry: Any,
) -> dict[str, Any] | None:
    """尽力把本次动作对应的回执带回响应，方便掉线后直接核对。"""
    try:
        from app.services.receipts import find_receipt, latest_receipt

        request_id = context.get("request_id")
        if request_id:
            receipt = find_receipt(str(request_id))
            if receipt is not None:
                return receipt
        # “查询回执”动作返回的 entry 本身就是回执行。
        if isinstance(entry, dict) and entry.get("request_id") and entry.get("biz_type") is not None:
            return entry
        if module in ("course_group", "lesson_prep"):
            return latest_receipt(module=module, entry_id=entry_id)
    except Exception:
        return None
    return None
