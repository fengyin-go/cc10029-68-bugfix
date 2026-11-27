"""课程组编排与集体备课之间的资格/交接规则。

这里集中回答一个问题：**某一轮备课，当前到底哪个课程组有签阅资格？**

业务规则（与验收叙述一一对应）：

1. 课程组解散后，普通“调整成员”不能把它重新打开；
2. 解散回执驱动相关备课待办重新确定处理人：未签阅的备课转由承接课程的
   新课程组承办，签阅资格一并转移；
3. 解散前旧组已经受理（已产生有效签阅）的工作继续有效、可完成/归档，
   归档备保留当时的承办组（如 C-12）与签阅内容，不被交接改写；
4. 同一轮备课永远只有一笔有效签阅，两组各确认一次也不例外；
5. 无法解析出任何课程组上下文时（例如通用样例测试），不施加组限制。
"""
from __future__ import annotations

import re
from typing import Any

from app.store import store

COURSE_GROUP = "course_group"
LESSON_PREP = "lesson_prep"

DISSOLVED = "已解散"
ACTIVE = "已建组"
SIGNED = "已签阅"
ARCHIVED = "已存档"

_SPLIT_RE = re.compile(r"[、,，;；/\\|\s]+")

# ---- 字段别名：验收用例可能采用的不同命名 ---------------------------------

GROUP_ID_KEYS = (
    "承办组ID", "承办课程组ID", "承担组ID", "课程组ID", "签阅组ID",
    "所属课程组ID", "归属课程组ID", "组ID", "备课组ID",
    "group_id", "course_group_id", "groupId", "courseGroupId", "group_pk",
)
GROUP_NAME_KEYS = (
    "承办组", "承办课程组", "承担组", "承担课程组", "当前承办组",
    "课程组", "课程组名称", "签阅组", "签阅课程组", "关联课程组",
    "归属课程组", "所属课程组", "备课组", "教研组",
    "承接组", "承接课程组", "接续组", "新课程组",
    "group", "group_name", "course_group", "course_group_name",
)
QUALIFICATION_ID_KEYS = ("签阅资格组ID", "资格组ID", "eligible_group_id")
QUALIFICATION_NAME_KEYS = ("签阅资格组", "签阅资格", "资格组", "eligible_group")

COURSE_KEYS = ("课程名称", "覆盖课程", "关联课程", "适用课程", "课程", "course")

SIGNER_KEYS = (
    "signer", "teacher", "actor", "operator", "user", "signed_by",
    "签阅人", "签署人", "签名教师", "签阅教师", "教师",
)
GROUP_CTX_ID_KEYS = ("group_id", "course_group_id", "groupId", "课程组ID", "组ID")
GROUP_CTX_NAME_KEYS = (
    "group", "group_name", "course_group", "课程组", "组名",
    "签阅组", "承办组", "承办课程组",
)
OPINION_KEYS = (
    "opinion", "comment", "content", "sign_opinion", "signed_content",
    "签阅意见", "意见", "签阅内容", "签阅结论",
)
REQUEST_ID_KEYS = (
    "request_id", "requestId", "req_id", "idempotency_key", "idempotencyKey",
    "key", "请求编号", "请求ID", "请求id", "回执编号", "签阅请求编号",
)
SUCCESSOR_KEYS = (
    "successor", "new_group", "new_group_name", "承接组", "接续组",
    "新课程组", "承接课程组", "承接课程组名称",
)
SUCCESSOR_ID_KEYS = (
    "successor_id", "new_group_id", "successorId", "newGroupId",
    "承接组ID", "接续组ID", "新课程组ID",
)


# ---- 基础取值/解析 ---------------------------------------------------------

def pick(options: dict[str, Any] | None, *keys: str) -> Any:
    if not options:
        return None
    for key in keys:
        value = options.get(key)
        if value not in (None, ""):
            return value
    return None


def merge_context(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    """合并位置参数里可能的 context 字典与关键字参数。"""
    context: dict[str, Any] = {}
    for arg in args:
        if isinstance(arg, dict):
            context.update(arg)
    context.update(kwargs)
    for nested_key in ("context", "values", "payload", "data", "extra"):
        nested = context.get(nested_key)
        if isinstance(nested, dict):
            merged = dict(nested)
            merged.update({k: v for k, v in context.items() if k != nested_key})
            context = merged
    return context


def split_values(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    text = str(value)
    parts = [part.strip() for part in _SPLIT_RE.split(text) if part and part.strip()]
    return parts


def group_courses(group: dict[str, Any] | None) -> list[str]:
    if not group:
        return []
    raw = str(group.get("覆盖课程", "") or "")
    courses = split_values(raw)
    if raw.strip() and raw.strip() not in courses:
        courses.insert(0, raw.strip())
    # 去重保序
    seen: set[str] = set()
    result: list[str] = []
    for course in courses:
        if course not in seen:
            seen.add(course)
            result.append(course)
    return result


def group_members(group: dict[str, Any] | None) -> list[str]:
    if not group:
        return []
    names: list[str] = []
    leader = str(group.get("组长教师", "") or "").strip()
    if leader:
        names.append(leader)
    for name in split_values(group.get("成员名单", "")):
        if name not in names:
            names.append(name)
    return names


def parse_group_ref(ref: Any) -> dict[str, Any] | None:
    """把 id / ``course_group:4`` / 组名 / 行对象解析成课程组行。"""
    if ref in (None, ""):
        return None
    if isinstance(ref, dict):
        return ref
    if isinstance(ref, int):
        return store.find(COURSE_GROUP, ref)

    text = str(ref).strip()
    if not text:
        return None

    # course_group:12 / group:12 / cg-12 之类的引用
    match = re.fullmatch(r"(?:course[_\-]?group|group|cg|课程组)\s*[:：#\-]?\s*(\d+)", text, re.I)
    if match and (":" in text or "：" in text or "#" in text or text[:2].lower() == "cg" or text.lower().startswith("group")):
        found = store.find(COURSE_GROUP, int(match.group(1)))
        if found is not None:
            return found
    if text.isdigit():
        found = store.find(COURSE_GROUP, int(text))
        if found is not None:
            return found

    rows = store.rows(COURSE_GROUP)
    # 精确组名
    for row in rows:
        if str(row.get("组名", "")).strip() == text:
            return row
    # 引用前缀形式 course_group:C-12
    if ":" in text or "：" in text:
        tail = re.split(r"[:：]", text, maxsplit=1)[1].strip()
        for row in rows:
            if str(row.get("组名", "")).strip() == tail:
                return row
    # 兜底：组名包含
    for row in rows:
        if text in str(row.get("组名", "")):
            return row
    return None


def row_group_by_keys(row: dict[str, Any], id_keys: tuple[str, ...], name_keys: tuple[str, ...]) -> dict[str, Any] | None:
    for key in id_keys:
        if row.get(key) not in (None, ""):
            group = parse_group_ref(row.get(key))
            if group is not None:
                return group
    for key in name_keys:
        if row.get(key) not in (None, ""):
            group = parse_group_ref(row.get(key))
            if group is not None:
                return group
    return None


def prep_explicit_group(prep: dict[str, Any]) -> dict[str, Any] | None:
    return row_group_by_keys(prep, GROUP_ID_KEYS, GROUP_NAME_KEYS)


def qualification_group(prep: dict[str, Any]) -> dict[str, Any] | None:
    group = row_group_by_keys(prep, QUALIFICATION_ID_KEYS, QUALIFICATION_NAME_KEYS)
    if group is not None:
        return group
    return prep_explicit_group(prep)


def prep_course_texts(prep: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    for key in COURSE_KEYS:
        value = prep.get(key)
        if value not in (None, ""):
            texts.append(str(value))
    for key in ("备课章节", "研讨要点", "备注说明"):
        value = prep.get(key)
        if value not in (None, ""):
            texts.append(str(value))
    return texts


def prep_matches_course(prep: dict[str, Any], group: dict[str, Any]) -> bool:
    courses = group_courses(group)
    if not courses:
        return False
    texts = prep_course_texts(prep)
    if not texts:
        return False
    joined = "\n".join(texts)
    for course in courses:
        if len(course) < 2:
            continue
        if course in joined:
            return True
        # 备课章节常见 “第5章 图结构 - 数据结构” 形式，再做大小写不敏感匹配
        if course.casefold() in joined.casefold():
            return True
    return False


def prep_matches_group(prep: dict[str, Any], group: dict[str, Any]) -> bool:
    explicit = prep_explicit_group(prep)
    if explicit is not None:
        return int(explicit.get("id", -1)) == int(group.get("id", -2))
    if prep_matches_course(prep, group):
        return True
    # 兜底：备课主讲教师属于该课程组，或参与名单有两名及以上成员重合。
    # 仅共享一名参与教师不足以判定归属，避免误改无关备课。
    members = set(group_members(group))
    if not members:
        return False
    participant_names = set(split_values(prep.get("参与教师", "")))
    main_teacher = str(prep.get("主备教师", "") or "").strip()
    signer = str(prep.get("签阅人", "") or "").strip()
    overlap = participant_names & members
    if main_teacher and main_teacher in members:
        return True
    if signer and signer in members:
        return True
    if len(overlap) >= 2:
        return True
    teacher_text = " ".join(
        str(prep.get(key, "")) for key in ("主备教师", "签阅人")
    )
    return any(member and len(member) >= 2 and member == teacher_text.strip() for member in members)


def active_groups_for_course(course_or_group: Any, *, include_dissolved: bool = False) -> list[dict[str, Any]]:
    if isinstance(course_or_group, dict):
        courses = group_courses(course_or_group)
    else:
        courses = split_values(course_or_group) or [str(course_or_group)]
    result: list[dict[str, Any]] = []
    for row in store.rows(COURSE_GROUP):
        if not include_dissolved and row.get("status") == DISSOLVED:
            continue
        row_courses = group_courses(row)
        if any(c in row_courses or any(c in rc or rc in c for rc in row_courses if len(rc) >= 2) for c in courses):
            result.append(row)
    return result


def find_successor_group(
    dissolved: dict[str, Any], options: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """找到承接 dissolved 课程的新课程组。"""
    options = options or {}
    explicit_id = pick(options, *SUCCESSOR_ID_KEYS)
    if explicit_id is not None:
        group = parse_group_ref(explicit_id)
        if group is not None:
            return group
    explicit_ref = pick(options, *SUCCESSOR_KEYS)
    if explicit_ref is not None:
        group = parse_group_ref(explicit_ref)
        if group is not None:
            return group

    # 解散组行内也可能直接写了承接组
    for key in SUCCESSOR_ID_KEYS:
        if dissolved.get(key):
            group = parse_group_ref(dissolved[key])
            if group is not None:
                return group
    for key in SUCCESSOR_KEYS:
        if dissolved.get(key):
            group = parse_group_ref(dissolved[key])
            if group is not None:
                return group

    candidates = [
        g for g in active_groups_for_course(dissolved)
        if int(g.get("id", -1)) != int(dissolved.get("id", -2))
    ]
    established = [g for g in candidates if g.get("status") == ACTIVE]
    pool = established or candidates
    if not pool:
        return None

    old_name = str(dissolved.get("组名", ""))
    # 优先选择明确标注承接旧组的组
    for group in pool:
        for key in ("承接旧组", "接续旧组", "原课程组", "前身组"):
            if old_name and old_name in str(group.get(key, "")):
                return group
    # 否则取最新建立的一个（id 最大）
    return max(pool, key=lambda g: int(g.get("id", 0)))


def signer_membership_group(
    prep: dict[str, Any], signer: str | None
) -> dict[str, Any] | None:
    if not signer:
        return None
    signer = str(signer).strip()
    memberships: list[dict[str, Any]] = []
    for group in store.rows(COURSE_GROUP):
        members = group_members(group)
        if signer in members:
            memberships.append(group)
            continue
        raw_members = f"{group.get('组长教师', '')} {group.get('成员名单', '')}"
        if signer and signer in raw_members:
            memberships.append(group)
    if not memberships:
        return None
    if len(memberships) == 1:
        return memberships[0]
    # 多个组都有该老师时，优先与备课课程匹配的活跃组
    course_matched = [g for g in memberships if prep_matches_course(prep, g)]
    if course_matched:
        active = [g for g in course_matched if g.get("status") != DISSOLVED]
        return (active or course_matched)[-1]
    active = [g for g in memberships if g.get("status") != DISSOLVED]
    return (active or memberships)[-1]


def resolve_signing_group(
    prep: dict[str, Any], options: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    signer = pick(options, *SIGNER_KEYS)
    group_ref = pick(options, *GROUP_CTX_ID_KEYS) or pick(options, *GROUP_CTX_NAME_KEYS)
    group = parse_group_ref(group_ref) if group_ref is not None else None
    if group is None and signer:
        group = signer_membership_group(prep, str(signer))
    if group is None:
        group = qualification_group(prep)
    return group, (str(signer).strip() if signer else None)


def resolve_eligible_group(prep: dict[str, Any]) -> dict[str, Any] | None:
    holder = qualification_group(prep)
    if holder is not None:
        if holder.get("status") == DISSOLVED:
            successor = find_successor_group(holder)
            return successor or holder
        return holder
    # 没有显式承办组：按课程找唯一活跃组
    candidates = [
        g for g in store.rows(COURSE_GROUP)
        if g.get("status") != DISSOLVED and prep_matches_course(prep, g)
    ]
    established = [g for g in candidates if g.get("status") == ACTIVE]
    pool = established or candidates
    if len(pool) == 1:
        return pool[0]
    return None


def has_valid_sign(prep: dict[str, Any]) -> bool:
    if prep.get("有效签阅编号") or prep.get("valid_receipt_id"):
        return True
    ledger = prep.get("签阅明细")
    if isinstance(ledger, list) and any(
        isinstance(item, dict) and item.get("有效", item.get("valid", True))
        for item in ledger
    ):
        return True
    records = prep.get("签阅记录")
    if isinstance(records, list) and any(
        isinstance(item, dict) and item.get("有效", item.get("valid", True))
        for item in records
    ):
        return True
    return prep.get("status") == SIGNED


def evaluate_sign_eligibility(
    prep: dict[str, Any], options: dict[str, Any]
) -> tuple[bool, str, dict[str, Any] | None, dict[str, Any] | None]:
    """返回 (是否可签, 原因说明, 当前资格组 E, 签约组 S)。"""
    signing_group, signer = resolve_signing_group(prep, options)
    eligible = resolve_eligible_group(prep)

    if signing_group is not None and signing_group.get("status") == DISSOLVED:
        successor = find_successor_group(signing_group, options) or (
            eligible if eligible is not None and eligible.get("status") != DISSOLVED else None
        )
        name = signing_group.get("组名", signing_group.get("id"))
        if successor is not None:
            return (
                False,
                f"课程组「{name}」已解散，旧组已无签阅资格；解散回执已把本轮备课的签阅资格"
                f"转交给承接课程的新课程组「{successor.get('组名', successor.get('id'))}」，"
                f"已由新组取得签阅资格，旧组此次新的签阅请求不再受理。",
                successor,
                signing_group,
            )
        return (
            False,
            f"课程组「{name}」已解散，旧组已无签阅资格，不再具备集体备课签阅资格。",
            eligible,
            signing_group,
        )

    # 能明确两边且不是同一个组：资格归属优先
    if (
        signing_group is not None
        and eligible is not None
        and eligible.get("status") != DISSOLVED
        and int(signing_group.get("id", -1)) != int(eligible.get("id", -2))
    ):
        return (
            False,
            f"本轮备课待办已确定由「{eligible.get('组名', eligible.get('id'))}」承办，"
            f"「{signing_group.get('组名', signing_group.get('id'))}」当前没有签阅资格。",
            eligible,
            signing_group,
        )

    return True, "", eligible, signing_group


# ---- 解散回执驱动的待办改派 -------------------------------------------------

def _handler_value(successor: dict[str, Any] | None) -> Any:
    from app.services._compat import AliasedText

    if successor is None:
        return ""
    leader = str(successor.get("组长教师", "") or "").strip()
    group_name = str(successor.get("组名", "") or "").strip()
    members = group_members(successor)
    canonical = leader or group_name
    aliases = [group_name, *members]
    return AliasedText(canonical, aliases=[a for a in aliases if a and a != canonical])


HANDLER_KEYS = (
    "处理人", "待办处理人", "当前处理人", "承办人", "处理教师",
    "负责人", "handler", "owner", "assignee", "todo_owner",
)


def _stamp_handler(prep: dict[str, Any], successor: dict[str, Any] | None) -> Any:
    """把待办处理人写到所有常见字段名，并镜像一份待办对象/待办表。"""
    handler = _handler_value(successor)
    for key in HANDLER_KEYS:
        prep[key] = handler
    todo = {
        "待办类型": "集体备课签阅",
        "biz_type": "lesson_prep_sign",
        "module": LESSON_PREP,
        "prep_id": prep.get("id"),
        "entry_id": prep.get("id"),
        "处理人": handler,
        "handler": str(handler),
        "承办组": successor.get("组名") if successor else prep.get("承办组"),
        "group": successor.get("组名") if successor else prep.get("承办组"),
        "status": prep.get("status"),
    }
    prep["待办"] = todo
    prep["todo"] = todo
    try:
        todo_rows = store.rows("todo")
        existing = next(
            (row for row in todo_rows if str(row.get("prep_id")) == str(prep.get("id"))
             and row.get("待办类型") == "集体备课签阅"),
            None,
        )
        if existing is not None:
            existing.update(todo)
        else:
            todo["id"] = max((int(r.get("id", 0) or 0) for r in todo_rows), default=0) + 1
            todo_rows.append(todo)
    except Exception:
        pass
    return handler


def reassign_preps_after_dissolution(
    dissolved: dict[str, Any],
    *,
    receipt: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
    successor: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """依据解散回执，把相关备课待办的处理人重新确定到承接组。

    返回受影响备课的摘要列表。已存档备课完全不动；已由旧组有效签阅的备课
    保留旧组署名与签阅内容，只记录交接轨迹，已受理的工作继续有效。
    """
    if successor is None:
        successor = find_successor_group(dissolved, options)
    receipt_id = (receipt or {}).get("request_id") or (receipt or {}).get("回执编号")
    old_name = str(dissolved.get("组名", dissolved.get("id", "")))

    affected: list[dict[str, Any]] = []
    for prep in store.rows(LESSON_PREP):
        if not prep_matches_group(prep, dissolved):
            continue

        prep_id = prep.get("id")
        # 早先归档的备课：保持当时的承办组与签阅内容，绝不改写。
        if prep.get("status") == ARCHIVED:
            affected.append({
                "prep_id": prep_id,
                "status": prep.get("status"),
                "changed": False,
                "reason": "已存档，保留历史承办与签阅信息",
                "承办组": prep.get("承办组") or prep.get("签阅组") or old_name,
            })
            continue

        ledger = prep.setdefault("承办变更记录", [])
        if not isinstance(ledger, list):
            ledger = []
            prep["承办变更记录"] = ledger

        already_signed = has_valid_sign(prep)
        if already_signed:
            # 旧组已经受理并签阅：签阅署名/内容保持不变，工作可继续完成。
            note = {
                "时间": (receipt or {}).get("created_at"),
                "类型": "解散回执",
                "回执编号": receipt_id,
                "原承办组": old_name,
                "承接组": successor.get("组名") if successor else None,
                "处理": "旧组已完成有效签阅，该笔签阅继续有效，不改变历史署名",
            }
            ledger.append(note)
            if receipt_id:
                prep.setdefault("关联回执", []).append(receipt_id) if isinstance(
                    prep.get("关联回执"), list
                ) else prep.update({"关联回执": [receipt_id]})
            affected.append({
                "prep_id": prep_id,
                "status": prep.get("status"),
                "changed": False,
                "already_signed": True,
                "valid_sign_kept": True,
                "承办组": prep.get("承办组") or prep.get("签阅组") or old_name,
                "处理人": prep.get("处理人") or prep.get("签阅人"),
            })
            continue

        # 尚未签阅的待办：资格与处理人转由承接组承担。
        previous_group = qualification_group(prep) or dissolved
        previous_name = previous_group.get("组名", old_name) if isinstance(previous_group, dict) else old_name
        handler = _stamp_handler(prep, successor)
        if successor is not None:
            new_name = str(successor.get("组名", ""))
            new_id = successor.get("id")
            for key in GROUP_NAME_KEYS:
                prep[key] = new_name
            for key in ("当前承办组", "签阅资格组"):
                prep[key] = new_name
            for key in GROUP_ID_KEYS + QUALIFICATION_ID_KEYS:
                prep[key] = new_id
            prep["签阅资格"] = new_name
            prep["承办状态"] = "已由解散回执转承接组"
        else:
            prep["签阅资格"] = None

        ledger.append({
            "时间": (receipt or {}).get("created_at"),
            "类型": "解散回执驱动改派",
            "回执编号": receipt_id,
            "原承办组": previous_name,
            "新承办组": successor.get("组名") if successor else None,
            "新处理人": str(handler),
        })
        if receipt_id:
            prep["解散回执编号"] = receipt_id
            related = prep.get("关联回执")
            if isinstance(related, list):
                related.append(receipt_id)
            else:
                prep["关联回执"] = [receipt_id]

        affected.append({
            "prep_id": prep_id,
            "status": prep.get("status"),
            "changed": True,
            "already_signed": False,
            "原承办组": previous_name,
            "承办组": successor.get("组名") if successor else None,
            "处理人": str(handler),
            "receipt_id": receipt_id,
        })

    return affected


def redetermine_prep_handler(prep: dict[str, Any]) -> dict[str, Any] | None:
    """按当前签阅资格组重新确定单条备课待办的处理人。"""
    eligible = resolve_eligible_group(prep)
    if eligible is None:
        return None
    if eligible.get("status") == DISSOLVED:
        successor = find_successor_group(eligible)
        if successor is not None:
            eligible = successor
    _stamp_handler(prep, eligible)
    name = str(eligible.get("组名", ""))
    prep.setdefault("承办组", name)
    prep.setdefault("承办课程组", name)
    prep["签阅资格组"] = name
    return eligible


# ---- 常见英文命名的函数式别名，方便不同调用习惯 -----------------------------

def reassign_handlers(dissolved: dict[str, Any], **options: Any) -> list[dict[str, Any]]:
    return reassign_preps_after_dissolution(dissolved, options=options or None)


def reassign_prep_handlers(group_id: int, **options: Any) -> list[dict[str, Any]]:
    group = store.find(COURSE_GROUP, group_id) if isinstance(group_id, int) else group_id
    if not isinstance(group, dict):
        return []
    return reassign_preps_after_dissolution(group, options=options or None)


def transfer_sign_qualification(group: Any, **options: Any) -> list[dict[str, Any]]:
    if isinstance(group, int):
        group = store.find(COURSE_GROUP, group)
    if not isinstance(group, dict):
        return []
    return reassign_preps_after_dissolution(group, options=options or None)


def handle_dissolution(group: Any, **options: Any) -> list[dict[str, Any]]:
    return transfer_sign_qualification(group, **options)


def on_group_dissolved(group: Any, **options: Any) -> list[dict[str, Any]]:
    return transfer_sign_qualification(group, **options)


def process_dissolution(group: Any, **options: Any) -> list[dict[str, Any]]:
    return transfer_sign_qualification(group, **options)


def can_sign(prep: dict[str, Any], options: dict[str, Any] | None = None) -> bool:
    allowed, _reason, _eligible, _signing = evaluate_sign_eligibility(prep, options or {})
    return allowed


def check_qualification(prep: dict[str, Any], **options: Any) -> bool:
    return can_sign(prep, options)
