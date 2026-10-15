"""``TodoPort`` 的企微实现（D-1：**唯一实现**）。

## 这个模块里没有降级路径，这是决策而不是遗漏

doc2 §9.3 的降级矩阵里有一行"wecom Todo 域不可用 → 切本地 Todo 表 + 模板卡片
提醒"。``01-overall`` 第 12 章 D-1 把这行**作废**了，本模块据后者实现。

理由值得写下来，因为"加个本地表兜底"看起来总是更负责：
本地台账能记住"我们打算让张三做这件事"，但它不会出现在张三的企微待办列表里。
于是 E-4 的确认卡片显示"已创建待办"，E-5 按本地表按时发提醒，两边都绿——而当事人
的待办列表里一条都没有。系统的每一处状态都自洽，唯一不自洽的地方是现实。
这比直接报"待办能力暂不可用"坏得多：后者会被立刻发现，前者要等到有人问
"那个待办你为什么没做"才暴露，而那时已经过了截止时间。

所以：待办域不可用 → :class:`WecomTodoUnavailable`，群内明示能力缺失，
**不写替身**。本模块通篇没有一个 PG 写操作，这是刻意的。

``BINARY_MISSING`` 是终局而非可恢复故障，:meth:`probe` 的返回值直接决定
E-4 / E-5 今日是否在交付范围内（R2）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Sequence

from app.contracts.errors import PortUnavailable
from app.contracts.ids import IdemKey, TodoId, UserId
from app.contracts.ports import TodoCreate, TodoRef, TodoSnapshot, UserRef
from app.platform.cli.result import CliErrorCode, CliResult

logger = logging.getLogger(__name__)

_SOURCE = "wecom.todo"

# follower_status 里代表"已完成"的取值。企微文档给的是数字码，但网关也见过字符串，
# 两种都收。**认不出的值一律当未完成**——把未完成当成已完成会让提醒静默停掉
# （E-5 的失效方式恰好是"什么都不发"，没人会注意到）；反向只是多发一条提醒。
_FINISHED_VALUES = frozenset({"1", "finished", "done", "completed", "true"})


class WecomTodoUnavailable(PortUnavailable):
    """待办域不可用。

    与 ``TapdCliUnavailable`` 形似而语义相反：那个是"换条路"，这个是"这条能力
    今天不存在"。调用方唯一正确的反应是在群内明示缺失，而不是找替代实现。
    """

    def __init__(self, error_code: object, detail: str) -> None:
        super().__init__(_SOURCE, error_code, detail)


class AmbiguousUser(PortUnavailable):
    """显示名匹配到多个人。

    单独成型是因为它**可以被人解决**——请调用方在群里追问"哪个张三"，而不是告警
    或重试。归到 ``CLARIFY`` 动作（``error_map.action_for``）。
    """

    def __init__(self, display_name: str, candidates: Sequence[str]) -> None:
        self.candidates = tuple(candidates)
        super().__init__(
            _SOURCE,
            "AMBIGUOUS_USER",
            f"{display_name!r} 匹配到 {len(candidates)} 人，需要澄清",
        )


class WecomTodoAdapter:
    """``TodoPort`` 的企微实现。

    ``last_batch_missing`` 记录上次 :meth:`get_batch` 里**没能回读到的** todo id。
    它不是统计信息：这些 todo 的完成状态是"未知"，而 :class:`TodoSnapshot` 只有
    ``finished: bool`` 装不下未知。把它们悄悄按未完成处理会一直发提醒（噪音），
    按已完成处理会永久停掉提醒（静默漏报）——所以两种都不做，从结果里剔除并在
    这里挂出来，由调用方决定（§9.3 的"明示覆盖范围"）。
    """

    def __init__(
        self,
        executor: Any,
        *,
        workspace_id: str,
        audit: Any | None = None,
    ) -> None:
        self._cli = executor
        self._ws = workspace_id
        self._audit = audit
        self.last_batch_missing: tuple[TodoId, ...] = ()

    # -- 可用性探测 -------------------------------------------------------
    async def probe(self) -> bool:
        """待办子域今日是否可用。R2 的"开工第一动作"，由探测脚本调用。

        用 ``user search`` 而不是 ``todo create`` 探测：只读，且它同时能证明三件事
        ——二进制存在、待办子命令路径对、凭据有效。建待办能证明的更多，但代价是
        给真人发一条垃圾待办。
        """
        try:
            result = await self._cli.run(
                "wecom.todo.user_search",
                {"keywords": ["__wetag_probe__"], "search_mode": "list"},
            )
        except Exception as exc:  # noqa: BLE001 探测不该把调用方打断
            logger.warning("wecom-cli 探测异常：%s", exc)
            return False

        if result.error_code is CliErrorCode.BINARY_MISSING:
            logger.warning("wecom-cli 未安装：E-4 / E-5 今日不可交付（R2）")
            return False
        if result.error_code in (
            CliErrorCode.PERMISSION_DENIED,
            CliErrorCode.NOT_FOUND,
        ):
            # 二进制与子命令都在，只是没这个人或没这个权限——权限问题要报出来，
            # 但它证明了"子域已下发"，与"二进制没装"是两件不同的事。
            logger.warning(
                "wecom-cli 可执行但待办子域受限 code=%s", result.error_code.value
            )
            return False
        # 查不到"__wetag_probe__"这个人是预期结果（ok 且零命中）。
        return True

    # -- 人员解析 ---------------------------------------------------------
    async def resolve_user(self, display_name: str) -> UserRef:
        """显示名 → userid。**多个候选时抛 :class:`AmbiguousUser`，绝不取第一个**。

        取第一个是这里最容易写出来的一行代码，也是最坏的一行：待办派给了另一个
        同名同事，该做的人没收到，而系统显示已通知。这个错误不会有任何一方报错
        ——被派到的人多半以为自己看漏了上下文，该做的人根本不知道有这件事。
        """
        name = display_name.strip()
        if not name:
            raise WecomTodoUnavailable("INVALID_ARGUMENT", "display_name 为空")

        result: CliResult[Any] = await self._cli.run(
            "wecom.todo.user_search",
            {"keywords": [name], "search_mode": "list"},
        )
        data = self._unwrap(result)
        # CLI 搜索是模糊匹配；派待办必须在本地收紧为姓名/别名完全一致。
        needle = name.casefold()
        rows = [
            row
            for row in _rows_of(data)
            if needle in {
                _str(row, "name").casefold(),
                _str(row, "alias").casefold(),
            }
        ]
        if not rows:
            raise WecomTodoUnavailable("USER_NOT_FOUND", f"{name!r} 在企微内未匹配到人")
        if len(rows) > 1:
            raise AmbiguousUser(name, [_str(r, "name") or _str(r, "userid") for r in rows])

        row = rows[0]
        user_id = _str(row, "userid") or _str(row, "user_id")
        if not user_id:
            # 认出了行但取不到 userid：这是契约变了，不是"没这个人"。
            raise WecomTodoUnavailable(
                "SCHEMA_UNKNOWN", f"user search 返回缺少 userid 字段：{sorted(row)}"
            )
        return UserRef(user_id=UserId(user_id), display_name=_str(row, "name") or name)

    # -- 建待办 -----------------------------------------------------------
    async def create(self, cmd: TodoCreate, idem_key: IdemKey) -> TodoRef:
        """建企微待办。

        ``idem_key`` **不会**被下传给企微——待办 API 没有幂等键（同 R10）。它在这里
        只用于审计关联，真正的去重由调用方的 ``claim/settle`` 完成。写在参数里是
        为了让这件事在签名上就能看见：如果哪天企微加了幂等键，改动点就在这里。

        对应命令 ``max_attempts=1``：重试一次就是第二条待办推到当事人手机上，
        而"疑似没建成"的正确处理是回查而不是重发。
        """
        if not cmd.assignee_ids:
            # 没有负责人的待办谁都收不到，但 API 多半照样返回一个 id，于是系统
            # 认为建成了。在进子进程之前拦掉。
            raise WecomTodoUnavailable("INVALID_ARGUMENT", "assignee_ids 为空")

        item: dict[str, Any] = {
            "title": cmd.title.strip()[:128],
            "follower_ids": [str(a) for a in cmd.assignee_ids],
        }
        if cmd.detail:
            item["description"] = cmd.detail[:4000]
        if cmd.due_at is not None:
            item["deadline"] = {
                "type": "datetime",
                "value": _wecom_datetime(cmd.due_at),
            }

        result: CliResult[Any] = await self._cli.run(
            "wecom.todo.create", {"json": _json_body({"items": [item]})}
        )
        data = self._unwrap(result)
        rows = _rows_of(data)
        if not rows:
            raise WecomTodoUnavailable(
                "SCHEMA_UNKNOWN",
                "create_todo 成功返回但无 items；待办可能已创建，须回查不得重建",
            )
        row = rows[0]
        if row.get("success") is False:
            raise WecomTodoUnavailable(
                "CREATE_FAILED", _str(row, "errmsg") or "企微拒绝创建待办"
            )
        todo_id = _str(row, "todo_id") or _str(row, "todoid") or _str(row, "id")
        if not todo_id:
            # 最棘手的一种失败：命令成功退出，但我们拿不到 id。待办**可能真的建了**，
            # 所以这里抛的是 SCHEMA_UNKNOWN 而不是"建失败"——调用方据此去回查，
            # 不该直接重建。
            raise WecomTodoUnavailable(
                "SCHEMA_UNKNOWN",
                "create_todo 成功返回但无 todo_id；待办可能已创建，须回查不得重建",
            )
        logger.info("企微待办已建 todo_id=%s idem=%s", todo_id, idem_key)
        return TodoRef(todo_id=TodoId(todo_id))

    # -- 批量回读 ---------------------------------------------------------
    async def get_batch(self, todo_ids: Sequence[TodoId]) -> list[TodoSnapshot]:
        """回读 ``follower_status``，E-5 判提醒 / 逾期升级的唯一依据。

        回读不到的 id 进 ``last_batch_missing`` 而不是被当成未完成——见类 docstring。
        """
        ids = [str(t) for t in todo_ids if str(t)]
        self.last_batch_missing = ()
        if not ids:
            return []

        rows: list[dict[str, Any]] = []
        for offset in range(0, len(ids), 20):
            batch = ids[offset : offset + 20]
            result: CliResult[Any] = await self._cli.run(
                "wecom.todo.get",
                {
                    "json": _json_body(
                        {"items": [{"todo_id": todo_id} for todo_id in batch]}
                    )
                },
            )
            data = self._unwrap(result)
            batch_rows = _rows_of(data)
            if not batch_rows:
                # 给了 id 却一行都没回来：这不是"都没了"，是没认出结构。
                raise WecomTodoUnavailable(
                    "SCHEMA_UNKNOWN",
                    f"get_todo_detail 未返回可识别的行（请求 {len(batch)} 条）",
                )
            rows.extend(batch_rows)

        out: list[TodoSnapshot] = []
        seen: set[str] = set()
        for row in rows:
            tid = _str(row, "todo_id") or _str(row, "todoid") or _str(row, "id")
            # 单条失败与响应缺失同样表示状态未知，留给 last_batch_missing 明示。
            if not tid or row.get("success") is False:
                continue
            seen.add(tid)
            followers = _follower_map(row)
            out.append(
                TodoSnapshot(
                    todo_id=TodoId(tid),
                    finished=_is_finished(row, followers),
                    follower_status=followers,
                    updated_at=_parse_epoch(row),
                    content=_str(row, "title")
                    or _str(row, "content")
                    or _str(row, "description"),
                    assignee_ids=_assignee_ids(row, followers),
                    owner_id=_str(row, "owner_id")
                    or _str(row, "creator")
                    or _str(row, "creator_userid"),
                    due_at=_parse_deadline(row),
                    status=(_str(row, "status") or _str(row, "todo_status")).lower(),
                )
            )

        missing = [TodoId(i) for i in ids if i not in seen]
        if missing:
            self.last_batch_missing = tuple(missing)
            logger.warning(
                "%d/%d 条待办回读不到，完成状态未知（不按未完成也不按已完成处理）",
                len(missing),
                len(ids),
            )
        return out

    # -- 内部 -------------------------------------------------------------
    def _unwrap(self, result: CliResult[Any]) -> Any:
        """``CliResult`` → 数据，失败即抛。

        ``BINARY_MISSING`` 在这里**不**特殊处理成切换信号（对比 ``tapd_cli``）：
        待办域没有第二条传输，"没装"就是"这条能力不存在"。
        """
        if not result.ok:
            raise WecomTodoUnavailable(
                result.error_code.value,
                result.stderr_digest or result.stdout_digest or "无输出",
            )
        if result.data is None:
            raise WecomTodoUnavailable(result.error_code.value, "命令成功但无数据")
        return result.data


# --- 无状态解析函数 ---------------------------------------------------------
# 与 tapd 的 normalize.py 不同：待办只有一条传输，不存在两份实现漂移的风险，
# 所以不单独开模块。


def _rows_of(data: Any) -> list[dict[str, Any]]:
    """从各种可能的信封里取出行。认不出就返回空，由调用方决定那是不是错。"""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if not isinstance(data, dict):
        return []
    for key in (
        "todo_list",
        "todos",
        "users",
        "userlist",
        "user_list",
        "items",
        "data",
        "list",
    ):
        value = data.get(key)
        if isinstance(value, list):
            return [r for r in value if isinstance(r, dict)]
        if isinstance(value, dict):
            return [value]
    # 单对象直出（create / 单条 get）
    if any(k in data for k in ("todo_id", "todoid", "userid", "user_id", "id")):
        return [data]
    return []


def _str(row: Any, key: str) -> str:
    if not isinstance(row, dict):
        return ""
    value = row.get(key)
    if value is None or isinstance(value, (dict, list, bool)):
        return ""
    return str(value).strip()


def _follower_map(row: dict[str, Any]) -> dict[str, str]:
    """``follower_status``：userid → 状态字符串。

    企微给的形态见过两种（对象数组 / 映射），都收。取不到就是空 dict——空 dict
    与"所有人都完成了"是不同的东西，:func:`_is_finished` 据此不会把空当完成。
    """
    raw = row.get("follower_status") or row.get("followers") or row.get("follower")
    out: dict[str, str] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            out[str(k)] = str(v).strip().lower()
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            uid = _str(item, "userid") or _str(item, "user_id")
            if uid:
                out[uid] = (
                    _str(item, "user_status")
                    or _str(item, "status")
                    or _str(item, "follow_status")
                ).lower()
    return out


def _is_finished(row: dict[str, Any], followers: dict[str, str]) -> bool:
    """整条待办是否完成。

    口径：顶层 ``status`` 说完成就是完成；否则要求 ``follower_status`` **非空且
    全部**为完成值。空 follower 表不算完成——那是"读不到跟进状态"，
    而不是"没人需要跟进所以算完成了"。
    """
    top = (_str(row, "status") or _str(row, "todo_status")).lower()
    if top in _FINISHED_VALUES:
        return True
    if not followers:
        return False
    return all(v in _FINISHED_VALUES for v in followers.values())


def _wecom_datetime(dt: datetime) -> str:
    """→ CLI 要求的 datetime 字符串。naive 按 UTC 解释（§9.4-6）。"""

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _json_body(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _assignee_ids(row: dict[str, Any], followers: dict[str, str]) -> tuple[str, ...]:
    """参与人。优先 ``follower_ids`` / ``assignee_ids``，否则用跟进状态的 userid。"""
    raw = row.get("follower_ids") or row.get("assignee_ids")
    ids: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                uid = _str(item, "userid") or _str(item, "user_id")
            else:
                uid = str(item).strip() if item is not None else ""
            if uid:
                ids.append(uid)
    if ids:
        return tuple(ids)
    return tuple(followers)


def _parse_deadline(row: dict[str, Any]) -> datetime | None:
    """截止时间。企微 ``deadline`` 是 ``{type, value}``，也见过裸字符串和 epoch。"""
    raw = row.get("deadline")
    if raw is None:
        raw = row.get("due_at") or row.get("due_time")
    if isinstance(raw, dict):
        raw = raw.get("value") or raw.get("datetime")
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(int(raw), tz=timezone.utc)
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return None
    if text.isdigit():
        return datetime.fromtimestamp(int(text), tz=timezone.utc)
    for fmt in ("%Y-%m-%d %H:%M:%S",):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_epoch(row: dict[str, Any]) -> datetime | None:
    for key in ("update_time", "modify_time", "create_time"):
        raw = _str(row, key)
        if raw.isdigit():
            return datetime.fromtimestamp(int(raw), tz=timezone.utc)
        if raw:
            try:
                return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                continue
    return None


__all__ = [
    "AmbiguousUser",
    "WecomTodoAdapter",
    "WecomTodoUnavailable",
]
