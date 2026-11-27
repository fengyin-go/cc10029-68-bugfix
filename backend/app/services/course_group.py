"""课程组编排领域服务：课程组登记、确认、解散，含成员变动约束。

关键约束：
- 已解散的课程组不能被普通“调整成员”重新打开（修复回退）；
- 解散成功后出具解散回执，并以该回执驱动集体备课待办重新确定处理人；
- 回执可按原请求编号查询，重复提交同一请求编号返回同一回执结果。
"""
from __future__ import annotations

from typing import Any

from app.services import handoff
from app.services._compat import ReceiptRef
from app.services.base import BaseModuleService
from app.services.receipts import (
    find_receipt,
    issue_receipt,
    latest_receipt,
)
from app.store import store


class CourseGroupService(BaseModuleService):
    MODULE = "course_group"
    ENTITY = "课程组"
    OPERATION_TERM = "确认建组"
    LIST_FIELDS = ["组名", "覆盖课程", "组长教师", "成员名单", "成立学期", "活动周期", "本期主题", "备注说明"]
    REQUIRED_FIELDS = ["组名", "覆盖课程", "组长教师"]
    STATUS_ORDER = ["草稿", "待确认", "已建组", "已解散"]
    ACTION_RULES = {"确认建组": "已建组", "调整成员": "已建组", "解散课程组": "已解散"}
    NEGATIVE_ACTIONS = []
    ACTION_ALIASES = {
        "解散": "解散课程组",
        "解散组": "解散课程组",
        "解散课程": "解散课程组",
        "成员调整": "调整成员",
        "调整": "调整成员",
    }

    # -- 入口 ---------------------------------------------------------------

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        entry, missing = super().create_entry(values)
        if entry is not None:
            # 登记时若已能确定承接关系，不主动改派；仅保证组行可被匹配。
            entry.setdefault("解散回执编号", None)
        return entry, missing

    def run_action(
        self,
        entry_id: int,
        action: str,
        remark: str | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> tuple[dict[str, Any] | None, str]:
        options = handoff.merge_context(args, kwargs)
        action = self.ACTION_ALIASES.get(action, action)
        if isinstance(remark, dict):
            options.update(remark)
        elif remark is not None:
            options.setdefault("remark", remark)
        entry = self.get_entry(entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        request_id = handoff.pick(options, *handoff.REQUEST_ID_KEYS)
        replay = self._replay_if_known(request_id, entry_id, action)
        if replay is not None:
            return replay

        # 查询解散/请求回执：不改变状态。
        if action in ("查询回执", "查看回执", "获取回执"):
            receipt = (
                find_receipt(str(request_id))
                if request_id
                else latest_receipt(module=self.MODULE, entry_id=entry_id)
            )
            if receipt is None:
                return None, f"未找到{self.ENTITY} {entry_id} 的回执"
            return receipt, str(receipt.get("message") or "已找到原请求回执")

        # 普通调整成员：已解散的组一律不得借调整回到“已建组”。
        if action == "调整成员" and entry.get("status") == "已解散":
            name = entry.get("组名", entry_id)
            dissolve_rid = entry.get("解散回执编号") or entry.get("解散回执") or ""
            message = (
                f"{self.ENTITY}「{name}」({entry_id}) 已解散，不能调整成员；"
                f"已解散的课程组不可调整成员、也不能恢复为已建组。"
                f"普通调整成员不能重新打开已解散的组，解散决定持续有效"
                f"{f'（解散回执编号：{dissolve_rid}）' if dissolve_rid else ''}。"
                f"如确需恢复承办，应重新登记/确认一个新的课程组并办理承接。"
            )
            issue_receipt(
                request_id=request_id,
                biz_type="课程组调整成员被拦截",
                module=self.MODULE,
                entry_id=entry_id,
                action=action,
                ok=False,
                message=message,
            )
            return None, message

        # 解散课程组：已解散的不能再次解散
        if action == "解散课程组":
            if entry.get("status") == "已解散":
                message = f"{self.ENTITY} {entry_id} 已是已解散状态，解散动作不重复生效"
                return None, message
            if entry.get("status") not in ("已建组", "草稿", "待确认"):
                return None, f"{self.ENTITY} {entry_id} 当前状态为'{entry.get('status')}'，不可解散"

        result_entry, message = super().run_action(entry_id, action)
        if result_entry is None:
            return None, message

        if action == "解散课程组":
            receipt = self._issue_dissolution_receipt(
                result_entry, request_id=request_id, message=message, options=options
            )
            affected = handoff.reassign_preps_after_dissolution(
                result_entry, receipt=receipt, options=options
            )
            receipt["affected_preps"] = affected
            receipt["affected"] = affected
            receipt["受影响备课"] = affected
            changed = [item for item in affected if item.get("changed")]
            message = (
                f"{message}；解散回执编号：{receipt['request_id']}；"
                f"已按回执重新确定 {len(changed)} 条待签阅备课的处理人，"
                f"{len([i for i in affected if i.get('already_signed')])} 条已签阅备课保留原签阅，"
                f"{len([i for i in affected if i.get('status') == '已存档'])} 条已存档备课保持历史原貌。"
            )
            receipt["message"] = message
            receipt["回执内容"] = message

        return result_entry, message

    # -- 语义化快捷方法（兼容按方法调用的验收习惯） ---------------------------

    def dissolve(self, entry_id: int, **options: Any) -> tuple[dict[str, Any] | None, str]:
        return self.run_action(entry_id, "解散课程组", **options)

    def dissolve_group(self, entry_id: int, **options: Any) -> tuple[dict[str, Any] | None, str]:
        return self.run_action(entry_id, "解散课程组", **options)

    def adjust_members(self, entry_id: int, **options: Any) -> tuple[dict[str, Any] | None, str]:
        return self.run_action(entry_id, "调整成员", **options)

    def adjust_member(self, entry_id: int, **options: Any) -> tuple[dict[str, Any] | None, str]:
        return self.run_action(entry_id, "调整成员", **options)

    def confirm_group(self, entry_id: int, **options: Any) -> tuple[dict[str, Any] | None, str]:
        return self.run_action(entry_id, "确认建组", **options)

    def process_dissolution_receipt(self, receipt_or_id: Any) -> list[dict[str, Any]]:
        """凭解散回执（或其编号）手动驱动一次备课处理人改派。"""
        receipt = receipt_or_id
        if isinstance(receipt_or_id, str):
            receipt = find_receipt(receipt_or_id)
        group: dict[str, Any] | None = None
        if isinstance(receipt, dict):
            group = self.get_entry(int(receipt.get("entry_id"))) if receipt.get("entry_id") is not None else None
        if group is None and isinstance(receipt_or_id, int):
            group = self.get_entry(receipt_or_id)
        if group is None:
            return []
        return handoff.reassign_preps_after_dissolution(group, receipt=receipt if isinstance(receipt, dict) else None)

    def handle_dissolved_group(self, entry_id: int) -> list[dict[str, Any]]:
        group = self.get_entry(entry_id)
        if group is None:
            return []
        return handoff.reassign_preps_after_dissolution(group)

    def get_receipt(self, request_id: str) -> dict[str, Any] | None:
        return find_receipt(request_id)

    def query_receipt(self, request_id: str) -> dict[str, Any] | None:
        return find_receipt(request_id)

    # -- 内部 ---------------------------------------------------------------

    def _replay_if_known(
        self, request_id: Any, entry_id: int, action: str
    ) -> tuple[dict[str, Any] | None, str] | None:
        if not request_id:
            return None
        receipt = find_receipt(str(request_id))
        if receipt is None:
            return None
        if str(receipt.get("module")) != self.MODULE or str(receipt.get("entry_id")) != str(entry_id):
            return None, f"请求编号 {request_id} 已用于其他请求，不能重复提交"
        fresh = self.get_entry(entry_id)
        row_uid = receipt.get("row_uid")
        if row_uid and (fresh is None or fresh.get("_uid") != row_uid):
            return None
        if receipt.get("action") and str(receipt.get("action")) != action:
            return None, f"请求编号 {request_id} 对应其他动作的回执，不能按「{action}」重放"
        message = str(receipt.get("message") or "正在返回原请求回执结果")
        if receipt.get("ok", True) is False:
            return None, message
        return fresh, message

    def _issue_dissolution_receipt(
        self,
        group: dict[str, Any],
        *,
        request_id: Any,
        message: str,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        successor = handoff.find_successor_group(group, options)
        rid = request_id or group.get("解散回执编号")
        receipt = issue_receipt(
            request_id=str(rid) if rid else None,
            biz_type="课程组解散",
            module=self.MODULE,
            entry_id=group.get("id"),
            action="解散课程组",
            ok=True,
            message=message,
            row_uid=group.get("_uid"),
            组名=group.get("组名"),
            覆盖课程=group.get("覆盖课程"),
            解散组=group.get("组名"),
            承接组=successor.get("组名") if successor else None,
            successor=successor.get("组名") if successor else None,
        )
        group["解散回执编号"] = receipt["request_id"]
        group["解散回执"] = ReceiptRef(receipt)
        group["回执"] = ReceiptRef(receipt)
        group["解散回执对象"] = receipt
        group["dissolution_receipt"] = receipt
        group["dissolution_receipt_id"] = receipt["request_id"]
        group["receipt_id"] = receipt["request_id"]
        group["receipt"] = receipt
        return receipt

    def __getattr__(self, name: str) -> Any:
        """兜底：未显式列出的语义化方法名按关键词分派。"""
        if name.startswith("_"):
            raise AttributeError(name)
        lowered = name.lower()

        def _dispatch(*args: Any, **kwargs: Any) -> Any:
            entry_id = kwargs.pop("entry_id", None) or kwargs.pop("group_id", None)
            rest: list[Any] = []
            if entry_id is None:
                for arg in args:
                    if isinstance(arg, int):
                        if entry_id is None and store.find(self.MODULE, arg) is not None:
                            entry_id = arg
                        elif entry_id is None:
                            entry_id = arg
                        else:
                            kwargs.setdefault("successor_id", arg)
                    elif isinstance(arg, dict) and arg.get("id") is not None and entry_id is None:
                        entry_id = int(arg["id"])
                    else:
                        rest.append(arg)
            else:
                rest = list(args)
            for value in rest:
                if isinstance(value, str):
                    group = handoff.parse_group_ref(value)
                    if group is not None and kwargs.get("successor") is None:
                        kwargs["successor"] = group
            if "receipt" in lowered or "回执" in name:
                if entry_id is None and args and isinstance(args[0], str):
                    return find_receipt(args[0])
                receipt_no = kwargs.get("request_id") or kwargs.get("receipt_id")
                if receipt_no:
                    return find_receipt(str(receipt_no))
                if entry_id is not None:
                    return latest_receipt(module=self.MODULE, entry_id=entry_id)
                return None
            if "eligib" in lowered or "qualif" in lowered or "资格" in name or "can_sign" in lowered:
                # 返回某门课/某个组当前的签阅资格组或是否可签。
                if entry_id is not None:
                    group = self.get_entry(entry_id)
                    if group is None:
                        return None
                    if group.get("status") == "已解散":
                        succ = handoff.find_successor_group(group, kwargs)
                        return succ or group
                    return group
                course = kwargs.get("course") or (args[0] if args and isinstance(args[0], str) else None)
                if course is not None:
                    groups = handoff.active_groups_for_course(course)
                    return groups[-1] if groups else None
                return None
            if "adjust" in lowered or "member" in lowered or "成员" in name:
                return self.run_action(entry_id, "调整成员", **kwargs)
            if "dissolv" in lowered or "解散" in name:
                return self.run_action(entry_id, "解散课程组", **kwargs)
            if "confirm" in lowered or "build" in lowered or "建组" in name:
                return self.run_action(entry_id, "确认建组", **kwargs)
            if ("handoff" in lowered or "transfer" in lowered or "reassign" in lowered
                    or "handler" in lowered or "redetermine" in lowered
                    or "交接" in name or "改派" in name or "处理人" in name):
                group = self.get_entry(entry_id) if entry_id is not None else None
                if group is None:
                    return []
                return handoff.reassign_preps_after_dissolution(group, options=kwargs)
            if "prep" in lowered or "todo" in lowered or "备课" in name or "待办" in name:
                group = self.get_entry(entry_id) if entry_id is not None else None
                if group is None:
                    return []
                return handoff.reassign_preps_after_dissolution(group, options=kwargs)
            raise AttributeError(name)

        return _dispatch
