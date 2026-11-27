"""模块服务基类：共享查询、登记、动作和统计骨架，业务差异留在各模块文件。"""
from __future__ import annotations

import uuid
from typing import Any

from app.store import store


class BaseModuleService:
    MODULE = ""
    ENTITY = ""
    LIST_FIELDS: list[str] = []
    REQUIRED_FIELDS: list[str] = []
    STATUS_ORDER: list[str] = []
    ACTION_RULES: dict[str, str] = {}
    NEGATIVE_ACTIONS: list[str] = []

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(self.MODULE))
        for field, expected in (filters or {}).items():
            # 既支持 LIST_FIELDS 内字段，也支持承办组/处理人等扩展字段过滤。
            if field not in self.LIST_FIELDS and field not in getattr(self, "EXTRA_FILTER_FIELDS", set()):
                # 未显式登记的字段，只要行数据里出现过也允许过滤。
                if not any(field in row for row in rows):
                    continue
            rows = [
                row for row in rows
                if str(expected).casefold() in str(row.get(field, "")).casefold()
            ]
        if keyword and self.LIST_FIELDS:
            field = self.LIST_FIELDS[0]
            rows = [row for row in rows if keyword.casefold() in str(row.get(field, "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(self.MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [
            field for field in self.REQUIRED_FIELDS
            if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, missing
        rows = store.rows(self.MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        # 行世代标记：表被 clear 后 id 会从 1 重新开始，用它区分“同 id 的新行”，
        # 避免旧请求编号的幂等回执错误重放到新行上。
        entry["_uid"] = uuid.uuid4().hex
        entry.update({field: values.get(field) for field in self.LIST_FIELDS})
        # 保留各领域扩展字段（如承办组、请求编号等），但不允许登记时覆盖受管键。
        protected = {"id", "status", "pending", "abnormal"}
        for key, value in (values or {}).items():
            if key in protected or key in entry:
                continue
            entry[key] = value
        entry["status"] = self.STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(
        self, entry_id: int, action: str, **options: Any
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        target = self.ACTION_RULES.get(action)
        if target is None:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"
        entry["status"] = target
        entry["pending"] = target != self.STATUS_ORDER[-1]
        entry["abnormal"] = action in self.NEGATIVE_ACTIONS
        return entry, f"{self.ENTITY}已{action}"

    def domain_snapshot(self) -> dict[str, Any]:
        """返回本模块的状态统计与业务对象，供其他模块读取。"""
        rows = store.rows(self.MODULE)
        by_status = {
            status: sum(1 for row in rows if row.get("status") == status)
            for status in self.STATUS_ORDER
        }
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}状态记录",
            "count": len(rows),
            "by_status": by_status,
            "abnormal": sum(1 for row in rows if row.get("abnormal")),
        }
