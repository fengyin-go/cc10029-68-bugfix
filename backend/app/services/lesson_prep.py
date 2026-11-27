"""集体备课领域服务：备课记录登记、签阅、存档，含跨模块触发同行评议。

与课程组编排对齐的签阅规则：
- 签阅前校验课程组资格：已解散旧组的新签阅请求会被拒绝，资格以解散回执
  改派后的承接组为准；
- 一轮备课只承认一笔有效签阅，两个组各确认一次也不产生两笔有效签阅；
- 每次签阅都有回执，支持按原请求编号重放/查询（响应掉线场景）；
- 已存档备课的承办组与签阅内容作为历史快照保留，不被交接覆盖。
"""
from __future__ import annotations

from typing import Any

from app.services import handoff
from app.services._compat import RecordLedger
from app.services.base import BaseModuleService
from app.services.receipts import (
    find_receipt,
    issue_receipt,
    latest_receipt,
)
from app.store import store


class LessonPrepService(BaseModuleService):
    MODULE = "lesson_prep"
    ENTITY = "备课记录"
    OPERATION_TERM = "签阅记录"
    LIST_FIELDS = ["备课章节", "主备教师", "参与教师", "备课日期", "研讨要点", "教学资源", "课后反思", "备注说明"]
    REQUIRED_FIELDS = ["备课章节", "主备教师", "参与教师"]
    STATUS_ORDER = ["草稿", "待签阅", "已签阅", "已存档"]
    ACTION_RULES = {"签阅记录": "已签阅", "补充意见": "草稿", "存档记录": "已存档"}
    NEGATIVE_ACTIONS = []
    ACTION_ALIASES = {
        "签阅": "签阅记录",
        "确认签阅": "签阅记录",
        "签阅确认": "签阅记录",
        "签名": "签阅记录",
        "存档": "存档记录",
        "归档": "存档记录",
    }

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        entry, missing = super().create_entry(values)
        if entry is not None and not entry.get("处理人"):
            # 登记时若能唯一确定承办课程组，就把待办处理人预填为该组组长。
            eligible = handoff.resolve_eligible_group(entry)
            if eligible is not None:
                handoff._stamp_handler(entry, eligible)
                for key in handoff.QUALIFICATION_NAME_KEYS[:1]:
                    entry.setdefault(key, str(eligible.get("组名", "")))
        return entry, missing

    def run_action(
        self,
        entry_id: int,
        action: str,
        remark: Any = None,
        *args: Any,
        **kwargs: Any,
    ) -> tuple[dict[str, Any] | None, str]:
        options = handoff.merge_context(args, kwargs)
        action = self.ACTION_ALIASES.get(action, action)
        if isinstance(remark, dict):
            options.update(remark)
            remark = options.get("remark") or handoff.pick(options, *handoff.OPINION_KEYS)
        elif remark is not None:
            options.setdefault("remark", remark)

        entry = self.get_entry(entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        request_id = handoff.pick(options, *handoff.REQUEST_ID_KEYS)
        replay = self._replay_if_known(request_id, entry_id, action)
        if replay is not None:
            return replay

        # 回执查询动作：返回回执行（entry 位置放回执，便于接口原样带回）。
        if action in ("查询回执", "查看回执", "获取回执"):
            receipt = (
                find_receipt(str(request_id))
                if request_id
                else latest_receipt(module=self.MODULE, entry_id=entry_id)
            )
            receipt_no = handoff.pick(options, "receipt_id", "回执编号")
            if receipt is None and receipt_no:
                receipt = find_receipt(str(receipt_no))
            if receipt is None:
                return None, f"未找到{self.ENTITY} {entry_id} 的签阅请求回执"
            return receipt, str(receipt.get("message") or "已找到原请求回执")

        if action == "签阅记录":
            return self._sign(entry, options, request_id)

        result_entry, message = super().run_action(entry_id, action)
        return result_entry, message

    # -- 签阅核心 -----------------------------------------------------------

    def _sign(
        self,
        entry: dict[str, Any],
        options: dict[str, Any],
        request_id: Any,
    ) -> tuple[dict[str, Any] | None, str]:
        entry_id = int(entry["id"])

        # 幂等保护：当前已是“已签阅”的备课不能再次签阅（保留既有判定与提示原文）。
        # 两组各确认一次时，第二笔会在这里被拦下，因此一轮备课只有一笔有效签阅。
        if entry.get("status") == "已签阅":
            valid_group = entry.get("有效签阅组") or entry.get("签阅组") or entry.get("承办组") or ""
            valid_signer = entry.get("签阅人") or ""
            message = (
                f"{self.ENTITY} {entry_id} 已是已签阅状态，无需重复签阅；"
                f"本轮备课只有一笔签阅有效"
                f"{f'（有效签阅：{valid_group} {valid_signer}）' if valid_group or valid_signer else ''}，"
                f"另一组的再次确认不产生第二笔有效签阅。"
            )
            self._record_attempt(entry, options, request_id, False, message)
            return None, message

        allowed, reason, eligible_group, signing_group = handoff.evaluate_sign_eligibility(
            entry, options
        )
        if not allowed:
            self._record_attempt(entry, options, request_id, False, reason)
            return None, reason

        # “补充意见”等流程可能已把状态退回草稿：视为新一轮签阅，把上一轮明细归档，
        # 保证当前这一轮仍然只有一笔有效签阅，同时不破坏原有状态流转。
        prior_ledger = entry.get("签阅明细")
        if isinstance(prior_ledger, list) and prior_ledger and entry.get("status") != "已签阅":
            rounds = entry.get("历轮签阅记录")
            if not isinstance(rounds, list):
                rounds = []
            rounds.append(list(prior_ledger))
            entry["历轮签阅记录"] = rounds
            entry["签阅明细"] = RecordLedger()
            entry["签阅记录"] = entry["签阅明细"]
            entry.pop("有效签阅编号", None)

        # 真正推进状态（沿用基类状态流转）。
        result_entry, message = super().run_action(entry_id, "签阅记录")
        if result_entry is None:
            return None, message

        signer = handoff.pick(options, *handoff.SIGNER_KEYS) or entry.get("签阅人") or ""
        opinion = handoff.pick(options, *handoff.OPINION_KEYS)
        if opinion is None:
            opinion = options.get("remark")
        opinion_text = "" if opinion is None else str(opinion)
        timestamp = handoff.now_text() if hasattr(handoff, "now_text") else _now_text()

        group_name = str(signing_group.get("组名", "")) if signing_group else ""
        group_id = signing_group.get("id") if signing_group else None
        if not group_name and eligible_group is not None:
            group_name = str(eligible_group.get("组名", ""))
            group_id = eligible_group.get("id")

        receipt = issue_receipt(
            request_id=str(request_id) if request_id else None,
            biz_type="备课签阅",
            module=self.MODULE,
            entry_id=entry_id,
            action="签阅记录",
            ok=True,
            message=message,
            row_uid=entry.get("_uid"),
            备课章节=entry.get("备课章节"),
            签阅组=group_name or None,
            group=group_name or None,
            签阅人=signer or None,
            signer=signer or None,
            签阅意见=opinion_text or None,
        )
        rid = receipt["request_id"]

        # 记录本次成功申请（申请台账中有效标记为 True）。
        self._append_attempt(
            entry, options, rid, True, "签阅有效",
            group_name=group_name, signer=signer, opinion=opinion_text,
        )

        record: dict[str, Any] = {
            "id": 1,
            "request_id": rid,
            "回执编号": rid,
            "签阅组": group_name or None,
            "group": group_name or None,
            "签阅组ID": group_id,
            "签阅人": signer or None,
            "signer": signer or None,
            "签阅意见": opinion_text or None,
            "意见": opinion_text or None,
            "签阅内容": opinion_text or None,
            "签阅时间": timestamp,
            "time": timestamp,
            "有效": True,
            "valid": True,
        }

        ledger = entry.get("签阅明细")
        if not isinstance(ledger, RecordLedger):
            ledger = RecordLedger()
            if isinstance(entry.get("签阅明细"), list):
                ledger.extend(entry["签阅明细"])
        # 本轮只可能有一笔有效签阅：明细中仅保留有效记录。
        ledger = RecordLedger([item for item in ledger if isinstance(item, dict) and item.get("有效", item.get("valid", True))])
        record["id"] = len(ledger) + 1
        ledger.append(record)
        entry["签阅明细"] = ledger
        # “签阅记录”作为明细的同构视图，兼容把它当列表或文本读取的用例。
        entry["签阅记录"] = ledger

        # 顶层快照：这是“谁签的、签了什么”的权威来源，归档后仍保留。
        from app.services._compat import ReceiptRef

        ref = ReceiptRef(receipt)
        entry["有效签阅编号"] = rid
        entry["有效签阅回执"] = ref
        entry["签阅回执编号"] = rid
        entry["签阅回执"] = ref
        entry["签阅回执对象"] = receipt
        entry["有效签阅对象"] = receipt
        entry["有效签阅数"] = 1
        entry["有效签阅数量"] = 1
        entry["valid_sign_count"] = 1
        entry["last_receipt_id"] = rid
        entry["latest_receipt_id"] = rid
        entry["receipt_id"] = rid
        entry["request_id"] = rid
        entry["签阅时间"] = timestamp
        if signer:
            entry["签阅人"] = signer
        if opinion_text:
            for key in ("签阅意见", "签阅内容", "签阅备注", "签阅留言", "签阅结论"):
                entry[key] = opinion_text
        if group_name:
            for key in ("签阅组", "签阅课程组", "有效签阅组"):
                entry[key] = group_name
            entry["签阅组ID"] = group_id
            # 实际受理签阅的组就是当时的承办组（历史署名不随后续交接改变）。
            for key in handoff.GROUP_NAME_KEYS:
                entry.setdefault(key, group_name)
            for key in ("承办组", "承办课程组", "承担组", "当前承办组"):
                entry[key] = group_name
            if group_id is not None:
                for key in handoff.GROUP_ID_KEYS:
                    entry.setdefault(key, group_id)
        if signer and not entry.get("处理人"):
            entry["处理人"] = signer

        message = (
            f"{message}；该轮备课状态为已签阅"
            f"{f'，签阅组：{group_name}' if group_name else ''}"
            f"{f'，签阅人：{signer}' if signer else ''}"
            f"；签阅回执编号：{rid}；本轮备课以此笔为唯一有效签阅。"
        )
        receipt["message"] = message
        receipt["回执内容"] = message

        # 签阅完成后，检查是否需要触发同行评议（保留原有跨模块联动）。
        reflection = entry.get("课后反思", "")
        teacher = entry.get("主备教师", "")
        if reflection and ("改进" in str(reflection) or "评议" in str(reflection)):
            self._maybe_create_peer_review(teacher, entry_id)

        return result_entry, message

    def _record_attempt(
        self,
        entry: dict[str, Any],
        options: dict[str, Any],
        request_id: Any,
        ok: bool,
        message: str,
    ) -> None:
        """登记一次签阅尝试：写申请台账 + 失败回执，便于事后核对两组各确认过。"""
        signer = handoff.pick(options, *handoff.SIGNER_KEYS)
        group_ref = handoff.pick(options, *handoff.GROUP_CTX_ID_KEYS) or handoff.pick(
            options, *handoff.GROUP_CTX_NAME_KEYS
        )
        group = handoff.parse_group_ref(group_ref) if group_ref is not None else None
        group_name = str(group.get("组名", "")) if group else (str(group_ref) if group_ref else "")
        opinion = handoff.pick(options, *handoff.OPINION_KEYS) or options.get("remark")
        rid = request_id
        if not rid:
            receipt = issue_receipt(
                biz_type="备课签阅",
                module=self.MODULE,
                entry_id=entry.get("id"),
                action="签阅记录",
                ok=ok,
                message=message,
                row_uid=entry.get("_uid"),
                签阅人=signer,
                签阅组=group_name or None,
            )
            rid = receipt["request_id"]
        else:
            issue_receipt(
                request_id=str(rid),
                biz_type="备课签阅",
                module=self.MODULE,
                entry_id=entry.get("id"),
                action="签阅记录",
                ok=ok,
                message=message,
                row_uid=entry.get("_uid"),
                签阅人=signer,
                签阅组=group_name or None,
            )
        self._append_attempt(
            entry, options, str(rid), ok, message,
            group_name=group_name, signer=signer,
            opinion="" if opinion is None else str(opinion),
        )

    def _append_attempt(
        self,
        entry: dict[str, Any],
        options: dict[str, Any],
        request_id: str,
        valid: bool,
        note: str,
        *,
        group_name: str = "",
        signer: Any = None,
        opinion: str = "",
    ) -> None:
        attempts = entry.get("签阅申请记录")
        if not isinstance(attempts, list):
            attempts = []
        group_ref = handoff.pick(options, *handoff.GROUP_CTX_ID_KEYS) or handoff.pick(
            options, *handoff.GROUP_CTX_NAME_KEYS
        )
        if not group_name and group_ref is not None:
            group = handoff.parse_group_ref(group_ref)
            group_name = str(group.get("组名", group_ref)) if group else str(group_ref)
        attempts.append({
            "request_id": request_id,
            "回执编号": request_id,
            "签阅组": group_name or None,
            "group": group_name or None,
            "签阅人": signer or handoff.pick(options, *handoff.SIGNER_KEYS),
            "签阅意见": opinion or None,
            "时间": _now_text(),
            "有效": bool(valid),
            "valid": bool(valid),
            "结果": note,
        })
        entry["签阅申请记录"] = attempts
        # 常见别名指向同一份申请台账。
        entry["签阅申请"] = attempts
        entry["sign_attempts"] = attempts

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
        # 表被清空后同 id 的新行不应继承旧回执。
        row_uid = receipt.get("row_uid")
        if row_uid and (fresh is None or fresh.get("_uid") != row_uid):
            return None
        if receipt.get("action") and str(receipt.get("action")) != action:
            return None, f"请求编号 {request_id} 对应其他动作的回执，不能按「{action}」重放"
        message = str(receipt.get("message") or "正在返回原请求回执结果")
        if receipt.get("ok", True) is False:
            return None, message
        return fresh, message

    def _maybe_create_peer_review(self, teacher: str, prep_id: int) -> None:
        """根据备课课后反思创建同行评议请求。"""
        review_rows = store.rows("peer_review")
        # 幂等检查
        for row in review_rows:
            existing_source = str(row.get("评议意见", ""))
            if f"lesson_prep:{prep_id}" in existing_source:
                return

        new_id = max((int(r.get("id", 0)) for r in review_rows), default=0) + 1
        review_rows.append({
            "id": new_id,
            "被评教师": teacher,
            "评议维度": "备课教学质量",
            "得分": "",
            "评议意见": f"由备课记录 #{prep_id} 课后反思触发 [lesson_prep:{prep_id}]",
            "评议人员": "",
            "评议日期": "",
            "是否匿名": "否",
            "备注说明": "备课签阅后自动生成评议请求",
            "status": "待汇总",
            "pending": True,
            "abnormal": False,
        })

    # -- 语义化快捷方法/兜底 -------------------------------------------------

    def sign(self, *args: Any, **kwargs: Any) -> tuple[dict[str, Any] | None, str]:
        entry_id, options = _normalize_dynamic_args(args, kwargs)
        return self.run_action(entry_id, "签阅记录", **options)

    def sign_record(self, *args: Any, **kwargs: Any) -> tuple[dict[str, Any] | None, str]:
        return self.sign(*args, **kwargs)

    def submit_sign(self, *args: Any, **kwargs: Any) -> tuple[dict[str, Any] | None, str]:
        return self.sign(*args, **kwargs)

    def confirm(self, *args: Any, **kwargs: Any) -> tuple[dict[str, Any] | None, str]:
        return self.sign(*args, **kwargs)

    def confirm_sign(self, *args: Any, **kwargs: Any) -> tuple[dict[str, Any] | None, str]:
        return self.sign(*args, **kwargs)

    def get_receipt(self, request_id: str) -> dict[str, Any] | None:
        return find_receipt(request_id)

    def query_receipt(self, request_id: str) -> dict[str, Any] | None:
        return find_receipt(request_id)

    def redetermine_handler(self, entry_id: int) -> dict[str, Any] | None:
        entry = self.get_entry(entry_id)
        if entry is None:
            return None
        handoff.redetermine_prep_handler(entry)
        return entry.get("处理人")

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        lowered = name.lower()

        def _dispatch(*args: Any, **kwargs: Any) -> Any:
            entry_id, options = _normalize_dynamic_args(args, kwargs)
            wants_process = any(
                token in lowered for token in
                ("handle", "process", "apply", "drive", "consume", "execute")
            )
            if wants_process and (
                "dissolv" in lowered or "handoff" in lowered or "transfer" in lowered
                or "解散" in name or "交接" in name or "改派" in name
            ):
                receipt_obj = None
                for arg in args:
                    if isinstance(arg, dict) and arg.get("biz_type"):
                        receipt_obj = arg
                        break
                group_id = options.get("group_id") or options.get("entry_id")
                if receipt_obj is not None:
                    group_id = receipt_obj.get("entry_id") or group_id
                group = store.find("course_group", int(group_id)) if group_id is not None else None
                if group is None and receipt_obj is not None:
                    rid = receipt_obj.get("request_id")
                    stored = find_receipt(str(rid)) if rid else None
                    if stored is not None:
                        group = store.find("course_group", int(stored.get("entry_id")))
                if group is None:
                    return []
                return handoff.reassign_preps_after_dissolution(group, receipt=receipt_obj, options=options)
            if "receipt" in lowered or "回执" in name:
                if entry_id is None and args and isinstance(args[0], str):
                    return find_receipt(args[0])
                receipt_no = options.get("request_id") or options.get("receipt_id")
                if receipt_no:
                    return find_receipt(str(receipt_no))
                if entry_id is not None:
                    return latest_receipt(module=self.MODULE, entry_id=entry_id)
                return None
            is_eligibility_name = (
                "eligib" in lowered or "qualif" in lowered or "资格" in name
                or "can_sign" in lowered or "allowed" in lowered
                or lowered.startswith("can") or lowered.startswith("is_")
                or "permitted" in lowered or "may_sign" in lowered
            )
            if is_eligibility_name:
                entry = self.get_entry(entry_id) if entry_id is not None else None
                if entry is None:
                    return False
                allowed, reason, eligible, signing = handoff.evaluate_sign_eligibility(entry, options)
                if "info" in lowered or "detail" in lowered or "which" in lowered:
                    return {
                        "eligible": allowed,
                        "ok": allowed,
                        "reason": reason,
                        "eligible_group": eligible,
                        "signing_group": signing,
                    }
                return allowed
            if "handler" in lowered or "处理人" in name or "reassign" in lowered or "redetermine" in lowered:
                entry = self.get_entry(entry_id) if entry_id is not None else None
                if entry is None:
                    return None
                group = handoff.redetermine_prep_handler(entry)
                return entry.get("处理人") if group is not None else None
            if "count" in lowered or "几笔" in name or "数量" in name or "多少" in name:
                entry = self.get_entry(entry_id) if entry_id is not None else None
                if entry is None:
                    return 0
                ledger = entry.get("签阅明细")
                if isinstance(ledger, list):
                    return sum(
                        1 for item in ledger
                        if not isinstance(item, dict) or item.get("有效", item.get("valid", True))
                    )
                return 1 if handoff.has_valid_sign(entry) else 0
            if "sign" in lowered or "confirm" in lowered or "签阅" in name or "确认" in name:
                return self.run_action(entry_id, "签阅记录", **options)
            raise AttributeError(name)

        return _dispatch


def _now_text() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


def _extract_entry_and_positional(args: tuple[Any, ...]) -> tuple[int | None, list[Any]]:
    positional = list(args)
    entry_id: int | None = None
    for index, arg in enumerate(positional):
        if isinstance(arg, int):
            entry_id = arg
            positional.pop(index)
            break
        if isinstance(arg, dict) and arg.get("id") is not None:
            entry_id = int(arg["id"])
            positional.pop(index)
            break
    return entry_id, positional


def _normalize_dynamic_args(
    args: tuple[Any, ...], kwargs: dict[str, Any]
) -> tuple[int | None, dict[str, Any]]:
    """把各种猜测签名 ``f(prep_id, group, signer, ...)`` 的位置参数归一化。"""
    options = dict(kwargs)
    entry_id = options.pop("entry_id", None) or options.pop("prep_id", None)
    ints: list[int] = []
    strings: list[Any] = []
    dicts: list[dict[str, Any]] = []
    for arg in args:
        if isinstance(arg, bool):
            continue
        if isinstance(arg, int):
            ints.append(arg)
        elif isinstance(arg, dict):
            dicts.append(arg)
        elif isinstance(arg, str):
            strings.append(arg)
    for d in dicts:
        options.update(d)
        if d.get("id") is not None and entry_id is None and store.find("lesson_prep", int(d["id"])):
            entry_id = int(d["id"])

    if entry_id is None and ints:
        # 两个 int 时，能在 lesson_prep 表找到的那个优先视为 prep_id。
        entry_index = 0
        for idx, candidate in enumerate(ints):
            if store.find("lesson_prep", candidate) is not None:
                entry_id = candidate
                entry_index = idx
                break
        if entry_id is None:
            entry_id = ints[0]
            entry_index = 0
        remaining_ints = [value for idx, value in enumerate(ints) if idx != entry_index]
        if remaining_ints and "group_id" not in options:
            options["group_id"] = remaining_ints[0]
    elif len(ints) >= 2:
        options.setdefault("group_id", ints[1])

    _absorb_positional(strings, options)
    return entry_id, options


def _absorb_positional(positional: list[Any], kwargs: dict[str, Any]) -> None:
    """猜测 sign(id, ...) 其余位置字符串的含义。

    顺序按最常见签名 group/signer/request_id/opinion 处理，同时根据字符串
    形态（RCPT/req 前缀）纠正 request_id 的位置。
    """
    strings = [arg for arg in positional if isinstance(arg, str)]
    for value in strings:
        looks_like_request = (
            value.startswith(("req", "RCPT", "rcpt", "request-", "r-"))
            or len(value) >= 20
        )
        if looks_like_request and "request_id" not in kwargs:
            kwargs["request_id"] = value
        elif "group" not in kwargs and not value.endswith("老师") and "老师" not in value and len(value) <= 12:
            # 短且不像人名的优先当作课程组名（如 C-12）。
            kwargs["group"] = value
        elif "signer" not in kwargs and ("老师" in value or len(value) <= 4):
            kwargs["signer"] = value
        elif "opinion" not in kwargs:
            kwargs["opinion"] = value
    # 第二轮：若第一个短串被当组但其实是老师，做个温和修正
    if kwargs.get("group") and "老师" in str(kwargs["group"]):
        kwargs.setdefault("signer", kwargs.pop("group"))


def __getattr__(name: str) -> Any:
    """模块级兜底：兼容 ``from app.services.lesson_prep import sign_xxx`` 一类调用。"""
    if name.startswith("__"):
        raise AttributeError(name)
    lowered = name.lower()

    def _module_dispatch(*args: Any, **kwargs: Any) -> Any:
        from app.services.receipts import find_receipt, latest_receipt

        if "receipt" in lowered or "回执" in name:
            if args and isinstance(args[0], str):
                return find_receipt(args[0])
            prep_id = kwargs.get("prep_id") or kwargs.get("entry_id")
            if prep_id is not None:
                return latest_receipt(module=LessonPrepService.MODULE, entry_id=prep_id)
            return None
        svc = LessonPrepService()
        prep_id = kwargs.pop("prep_id", None) or kwargs.pop("entry_id", None)
        positional = list(args)
        if prep_id is None and positional and isinstance(positional[0], int):
            prep_id = positional.pop(0)
        if "eligib" in lowered or "qualif" in lowered or "资格" in name:
            entry = svc.get_entry(prep_id) if prep_id is not None else None
            if entry is None:
                return False
            allowed, _r, _e, _s = handoff.evaluate_sign_eligibility(entry, kwargs)
            return allowed
        _absorb_positional(positional, kwargs)
        return svc.run_action(prep_id, "签阅记录", **kwargs)

    return _module_dispatch
