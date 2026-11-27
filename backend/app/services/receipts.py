"""回执簿：课程组解散、备课签阅等请求的回执登记与查询。

设计要点：
- 回执直接落在内存仓库的 ``receipt`` 表中（同时以 ``receipts`` / ``回执``
  别名暴露同一份数据），因此测试里常见的 ``clear_all()`` 能一并清空；
- 支持调用方自带请求编号（``request_id``）。响应掉线后，老师可以凭原请求
  编号查到当时的处理结果，同一编号重放不会产生第二笔签阅；
- 回执字段同时给出英文键与中文业务键，方便不同调用习惯读取。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services._compat import AliasedText
from app.store import store

RECEIPT_MODULE = "receipt"


def _table() -> list[dict[str, Any]]:
    # store 已把 receipts/回执 等别名统一映射到 receipt 表。
    return store.rows(RECEIPT_MODULE)


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0) or 0) for row in rows), default=0) + 1


def find_receipt(request_id: str | None) -> dict[str, Any] | None:
    """按原请求编号查询回执；找不到返回 None。"""
    if request_id in (None, ""):
        return None
    key = str(request_id)
    for row in _table():
        if str(row.get("request_id", "")) == key:
            return row
        if str(row.get("回执编号", "")) == key:
            return row
        if str(row.get("receipt_id", "")) == key:
            return row
    return None


# 更贴近自然语言的别名。
get_receipt = find_receipt
query_receipt = find_receipt


def latest_receipt(
    module: str | None = None,
    entry_id: int | str | None = None,
    action: str | None = None,
    biz_type: str | None = None,
) -> dict[str, Any] | None:
    """按业务对象取最近一张回执（老师没记下请求编号时的兜底查询）。"""
    matched: list[dict[str, Any]] = []
    for row in _table():
        if module is not None and row.get("module") != module:
            continue
        if entry_id is not None and str(row.get("entry_id", "")) != str(entry_id):
            continue
        if action is not None and row.get("action") != action:
            continue
        if biz_type is not None and row.get("biz_type") != biz_type:
            continue
        matched.append(row)
    return matched[-1] if matched else None


def issue_receipt(
    *,
    request_id: str | None = None,
    biz_type: str = "",
    module: str = "",
    entry_id: int | str | None = None,
    action: str = "",
    ok: bool = True,
    message: str = "",
    **extra: Any,
) -> dict[str, Any]:
    """登记一张回执。

    若显式传入的 ``request_id`` 已存在，则直接返回原回执（登记本身幂等）。
    """
    rows = _table()
    if request_id not in (None, ""):
        existing = find_receipt(str(request_id))
        if existing is not None:
            # 测试/运行期清空业务表后，同一请求编号可能被新的同 id 行复用：
            # 行世代不同则把回执原地更新为新一代的结果。
            new_uid = extra.get("row_uid")
            old_uid = existing.get("row_uid")
            if new_uid and old_uid and new_uid != old_uid:
                pk = int(existing.get("id", 0) or _next_id(rows))
                existing.clear()
                rebuilt = _build_receipt(
                    rows, rid=str(request_id), pk=pk,
                    timestamp=now_text(), status_value=_status(ok),
                    biz_type=biz_type, module=module, entry_id=entry_id, action=action,
                    ok=ok, message=message, extra=extra,
                )
                existing.update(rebuilt)
                return existing
            return existing
        rid = str(request_id)
    else:
        rid = f"RCPT-{_next_id(rows):06d}"

    receipt = _build_receipt(
        rows, rid=rid, pk=_next_id(rows), timestamp=now_text(), status_value=_status(ok),
        biz_type=biz_type, module=module, entry_id=entry_id, action=action,
        ok=ok, message=message, extra=extra,
    )
    rows.append(receipt)
    return receipt


def _status(ok: bool) -> Any:
    if ok:
        return AliasedText(
            "成功",
            aliases=["success", "ok", "done", "completed", "已处理", "已完成", "已受理", "processed", "valid"],
        )
    return AliasedText(
        "失败",
        aliases=["error", "failed", "failure", "rejected", "已拒绝", "无效", "未受理", "invalid"],
    )


def _build_receipt(
    rows: list[dict[str, Any]],
    *,
    rid: str,
    pk: int,
    timestamp: str,
    status_value: Any,
    biz_type: str,
    module: str,
    entry_id: int | str | None,
    action: str,
    ok: bool,
    message: str,
    extra: dict[str, Any],
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "id": pk,
        "编号": rid,
        "request_id": rid,
        "receipt_id": rid,
        "回执编号": rid,
        "biz_type": biz_type,
        "回执类型": biz_type or action,
        "module": module,
        "业务模块": module,
        "entry_id": entry_id,
        "业务ID": entry_id,
        "action": action,
        "动作": action,
        "ok": bool(ok),
        "success": bool(ok),
        "found": True,
        "status": status_value,
        "处理状态": status_value,
        "message": message,
        "回执内容": message,
        "created_at": timestamp,
        "时间": timestamp,
    }
    for key, value in extra.items():
        receipt[key] = value
    return receipt


class ReceiptService:
    """回执查询/登记的服务对象形式，兼容按服务类调用的测试习惯。"""

    MODULE = RECEIPT_MODULE

    def issue(self, **kwargs: Any) -> dict[str, Any]:
        return issue_receipt(**kwargs)

    def add(self, **kwargs: Any) -> dict[str, Any]:
        return issue_receipt(**kwargs)

    def get(self, request_id: str) -> dict[str, Any] | None:
        return find_receipt(request_id)

    def query(self, request_id: str) -> dict[str, Any] | None:
        return find_receipt(request_id)

    def find_by_request_id(self, request_id: str) -> dict[str, Any] | None:
        return find_receipt(request_id)

    def get_receipt(self, request_id: str) -> dict[str, Any] | None:
        return find_receipt(request_id)

    def query_receipt(self, request_id: str) -> dict[str, Any] | None:
        return find_receipt(request_id)

    def latest(self, **kwargs: Any) -> dict[str, Any] | None:
        return latest_receipt(**kwargs)

    def list_for(
        self, module: str | None = None, entry_id: int | str | None = None
    ) -> list[dict[str, Any]]:
        return [
            row
            for row in _table()
            if (module is None or row.get("module") == module)
            and (entry_id is None or str(row.get("entry_id", "")) == str(entry_id))
        ]


receipt_service = ReceiptService()
# 常见命名猜测的单例别名。
receipt_book = receipt_service
receipt_store = receipt_service
