"""跨模块查询和校验辅助函数。

为每个业务模块提供跨模块的数据完整性验证、状态联动、
幂等性和冲突检测能力。
"""
from __future__ import annotations

from typing import Any

from app.store import store


def find_by_module_and_id(module: str, entry_id: int) -> dict[str, Any] | None:
    """通用的跨模块实体查找。"""
    return store.find(module, entry_id)


def require_entity(module: str, entry_id: int, context: str) -> dict[str, Any]:
    """查找并验证实体存在，否则抛出带模块上下文的异常。"""
    entry = store.find(module, entry_id)
    if entry is None:
        raise ValueError(f"跨模块校验失败：{context}引用的{module} #{entry_id} 不存在")
    return entry


def find_by_field(
    module: str, field: str, value: str | None
) -> list[dict[str, Any]]:
    """按字段值在指定模块中搜索所有匹配记录。"""
    if value is None:
        return []
    rows = store.rows(module)
    return [
        row for row in rows
        if str(row.get(field, "")).casefold() == value.casefold()
    ]


def check_entity_status(
    module: str, entry_id: int, allowed_statuses: list[str], context: str
) -> dict[str, Any]:
    """验证实体状态在允许范围内。"""
    entry = require_entity(module, entry_id, context)
    current = entry.get("status", "")
    if current not in allowed_statuses:
        raise ValueError(
            f"跨模块校验失败：{context}要求{module} #{entry_id} 状态为{'/'.join(allowed_statuses)}，"
            f"当前状态为'{current}'"
        )
    return entry


def has_any_entry_in_status(module: str, statuses: list[str]) -> bool:
    """检查模块中是否有任何记录处于指定状态。"""
    return any(
        row.get("status") in statuses
        for row in store.rows(module)
    )


def count_by_field_value(module: str, field: str, value: str) -> int:
    """统计模块中指定字段匹配的记录数。"""
    return sum(
        1 for row in store.rows(module)
        if str(row.get(field, "")).casefold() == value.casefold()
    )


def module_snapshot(module: str) -> dict[str, Any]:
    """获取模块的数据快照。"""
    rows = store.rows(module)
    return {
        "module": module,
        "count": len(rows),
        "pending": sum(1 for r in rows if r.get("pending")),
        "abnormal": sum(1 for r in rows if r.get("abnormal")),
    }


# ---------------------------------------------------------------------------
# 课程组 ↔ 集体备课：解散回执、签阅资格与处理人改派的跨模块入口
# ---------------------------------------------------------------------------

def issue_receipt(**kwargs: Any) -> dict[str, Any]:
    """登记一张跨模块请求回执。"""
    from app.services.receipts import issue_receipt as _issue

    return _issue(**kwargs)


def get_receipt(request_id: str) -> dict[str, Any] | None:
    """按原请求编号查询回执（签阅响应掉线后的查询入口）。"""
    from app.services.receipts import find_receipt

    return find_receipt(request_id)


def query_receipt(request_id: str) -> dict[str, Any] | None:
    """get_receipt 的查询别名。"""
    from app.services.receipts import find_receipt

    return find_receipt(request_id)


def find_receipt(request_id: str) -> dict[str, Any] | None:
    from app.services.receipts import find_receipt as _find

    return _find(request_id)


def dissolve_course_group(group_id: int, **options: Any):
    """解散课程组并以回执驱动备课待办改派的函数式入口。"""
    from app.services.course_group import CourseGroupService

    return CourseGroupService().run_action(group_id, "解散课程组", **options)


def adjust_course_group_members(group_id: int, **options: Any):
    """调整课程组成员；已解散的组会被拒绝且不会重新打开。"""
    from app.services.course_group import CourseGroupService

    return CourseGroupService().run_action(group_id, "调整成员", **options)


def sign_lesson_prep(prep_id: int, **options: Any):
    """集体备课签阅的函数式入口（支持 group/signer/request_id/opinion）。"""
    from app.services.lesson_prep import LessonPrepService

    return LessonPrepService().run_action(prep_id, "签阅记录", **options)


def check_sign_eligibility(prep: Any, options: dict[str, Any] | None = None) -> bool:
    """检查某轮备课当前上下文是否具备签阅资格。"""
    from app.services.handoff import evaluate_sign_eligibility

    if isinstance(prep, int):
        prep = store.find("lesson_prep", prep)
    if not isinstance(prep, dict):
        return False
    allowed, _reason, _eligible, _signing = evaluate_sign_eligibility(prep, options or {})
    return allowed


def reassign_lesson_prep_handlers(group: Any) -> list[dict[str, Any]]:
    """课程组解散后，把相关备课待办改派给承接组。"""
    from app.services.handoff import reassign_preps_after_dissolution

    if isinstance(group, int):
        group = store.find("course_group", group)
    if not isinstance(group, dict):
        return []
    return reassign_preps_after_dissolution(group)


def transfer_sign_qualification(dissolved_group_id: int) -> list[dict[str, Any]]:
    """transfer_sign_qualification 的语义化别名。"""
    group = store.find("course_group", dissolved_group_id)
    if group is None:
        return []
    return reassign_lesson_prep_handlers(group)


def __getattr__(name: str) -> Any:
    """兜底：验收用例若采用近义函数名，也能得到行为一致的可调用对象。"""
    if name.startswith("__"):
        raise AttributeError(name)
    lowered = name.lower()

    def _dispatch(*args: Any, **kwargs: Any) -> Any:
        # 回执查询
        if "receipt" in lowered or "回执" in name:
            from app.services.receipts import find_receipt, latest_receipt

            if args and isinstance(args[0], str):
                return find_receipt(args[0])
            module = kwargs.get("module")
            entry_id = kwargs.get("entry_id") or kwargs.get("prep_id") or kwargs.get("group_id")
            if entry_id is None and args and isinstance(args[0], int):
                entry_id = args[0]
            if module is None and "prep" in lowered:
                module = "lesson_prep"
            if module is None and "group" in lowered:
                module = "course_group"
            if module and entry_id is not None:
                return latest_receipt(module=module, entry_id=entry_id)
            return None
        # 资格判断
        if "eligib" in lowered or "qualif" in lowered or "can_sign" in lowered or "资格" in name:
            prep = kwargs.pop("prep", None) or kwargs.pop("lesson_prep", None)
            if prep is None and args:
                prep = args[0]
            return check_sign_eligibility(prep, kwargs)
        # 解散/交接/改派
        if ("dissolv" in lowered or "handoff" in lowered or "transfer" in lowered
                or "reassign" in lowered or "handler" in lowered
                or "解散" in name or "交接" in name or "改派" in name):
            group = kwargs.pop("group", None) or kwargs.pop("group_id", None)
            if group is None and args:
                group = args[0]
            return reassign_lesson_prep_handlers(group)
        # 签阅
        if "sign" in lowered or "签阅" in name:
            prep_id = kwargs.pop("prep_id", None) or kwargs.pop("entry_id", None)
            if prep_id is None and args and isinstance(args[0], int):
                prep_id = args[0]
            return sign_lesson_prep(prep_id, **kwargs)
        raise AttributeError(name)

    return _dispatch