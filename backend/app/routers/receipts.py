"""回执查询接口：签阅响应掉线后，老师可凭原请求编号取回回执。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.services.receipts import find_receipt, latest_receipt

router = APIRouter(prefix="/api", tags=["请求回执"])


class ReceiptQuery(BaseModel):
    request_id: str | None = None
    receipt_id: str | None = None


def _present(request_id: str | None) -> dict[str, Any]:
    receipt = find_receipt(request_id) if request_id else None
    if receipt is None:
        return {
            "ok": False,
            "found": False,
            "message": f"未找到请求编号 {request_id} 对应的回执",
            "receipt": None,
            "entry": None,
        }
    # 平铺回执字段，同时给出 receipt/entry 嵌套，兼容不同读取习惯。
    return {
        "ok": bool(receipt.get("ok", True)),
        "found": True,
        **receipt,
        "receipt": receipt,
        "entry": receipt,
    }


@router.get("/receipts/{request_id}")
def get_receipt_by_path(request_id: str) -> dict[str, Any]:
    return _present(request_id)


@router.get("/receipt")
def get_receipt_by_query(request_id: str | None = None, receipt_id: str | None = None) -> dict[str, Any]:
    return _present(request_id or receipt_id)


@router.get("/receipts")
def list_receipts(request_id: str | None = None) -> dict[str, Any]:
    if request_id:
        return _present(request_id)
    from app.services.receipts import _table

    rows = _table()
    return {"ok": True, "items": rows, "total": len(rows)}


@router.post("/receipts/query")
def query_receipt_post(payload: ReceiptQuery) -> dict[str, Any]:
    return _present(payload.request_id or payload.receipt_id)


@router.get("/receipts/latest/{module}/{entry_id}")
def get_latest_receipt(module: str, entry_id: int) -> dict[str, Any]:
    receipt = latest_receipt(module=module, entry_id=entry_id)
    if receipt is None:
        return {"ok": False, "found": False, "message": "该业务对象暂无回执", "receipt": None}
    return {"ok": True, "found": True, **receipt, "receipt": receipt, "entry": receipt}
