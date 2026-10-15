"""``todo/wecom.py`` 的契约测试。

全部离线：``FakeSpawn`` 注入真的 ``CliExecutor``，所以覆盖到登记表、参数渲染、
错误码归一整条链——而 ``wecom-cli`` 今日根本没装（计划 R2）。

这些用例守的不是"功能对不对"，而是四条**静默失败**的入口：

* 同名多人 → 待办派错人，两边都不报错；
* 建成了但拿不到 id → 重建 = 第二条待办推到当事人手机；
* 回读不到 → 按已完成处理会永久停掉提醒，按未完成会一直发；
* 本地台账替身 → 系统每处状态自洽，唯独与当事人的待办列表不一致（D-1 禁止）。

最后一条用一条结构断言钉住：本模块不得 import 任何持久化。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from app.capability.integration.todo import wecom as wecom_mod
from app.capability.integration.todo.wecom import (
    AmbiguousUser,
    WecomTodoAdapter,
    WecomTodoUnavailable,
)
from app.contracts.errors import PortUnavailable
from app.contracts.ids import IdemKey, TodoId, UserId
from app.contracts.ports import TodoCreate, TodoPort
from app.platform.cli.executor import CliExecutor
from app.platform.cli.registry import WECOM_COMMANDS

WS = "alpha"
IDEM = IdemKey("todo:finding-1:u-7")


class FakeSpawn:
    def __init__(self, table: Mapping[tuple[str, ...], Any]) -> None:
        self.table = dict(table)
        self.calls: list[list[str]] = []
        self.sleeps: list[float] = []

    async def record_sleep(self, delay: float) -> None:
        self.sleeps.append(delay)

    async def __call__(
        self, argv: Sequence[str], *, env: dict[str, str], cwd: str | None
    ) -> tuple[int, bytes, bytes]:
        self.calls.append(list(argv))
        for keys, value in self.table.items():
            if all(k in argv for k in keys):
                if isinstance(value, BaseException):
                    raise value
                if isinstance(value, tuple):
                    code, out, err = value
                    return code, out.encode(), err.encode()
                return 0, json.dumps(value, ensure_ascii=False).encode(), b""
        return 0, b"{}", b""

    def argv_for(self, *keys: str) -> list[str]:
        for argv in self.calls:
            if all(k in argv for k in keys):
                return argv
        raise AssertionError(f"没有调用过 {keys}；实际：{self.calls}")


def _adapter(table: Mapping[tuple[str, ...], Any]) -> tuple[WecomTodoAdapter, FakeSpawn]:
    spawn = FakeSpawn(table)
    executor = CliExecutor(
        workspace_id=WS,
        binaries={"wecom": "wecom-cli"},
        secrets={"WECOM_CLI_TOKEN": "s3cret-wecom-token"},
        specs=WECOM_COMMANDS,
        spawn=spawn,
        retry_sleep=spawn.record_sleep,
    )
    return WecomTodoAdapter(executor, workspace_id=WS), spawn


# --- D-1：没有降级实现，这是决策 --------------------------------------------


def test_satisfies_todo_port() -> None:
    adapter, _ = _adapter({})
    assert isinstance(adapter, TodoPort)


def test_module_never_touches_persistence() -> None:
    """本模块不得 import 任何持久化——D-1 的"不写本地台账替身"的可执行形式。

    写成结构断言而不是注释，因为这条规则最可能的破坏方式是好意：某次为了"至少
    记一下"而 import 一个 repository，代码评审时看起来完全合理。届时这条用例会红。
    """
    source = Path(wecom_mod.__file__).read_text(encoding="utf-8")
    imports = [
        line.strip()
        for line in source.splitlines()
        if line.startswith(("import ", "from ")) or line.strip().startswith("from app")
    ]
    banned = ("repository", "platform.db", "asyncpg", "outbox")
    offenders = [ln for ln in imports if any(b in ln for b in banned)]
    assert offenders == [], f"待办域禁止落库（D-1），却 import 了：{offenders}"


# --- 人员解析：派错人是静默的 -----------------------------------------------


async def test_resolve_user_single_match() -> None:
    adapter, spawn = _adapter(
        {("users", "search"): {"users": [{"userid": "u-7", "name": "张三"}]}}
    )
    ref = await adapter.resolve_user("张三")
    assert (ref.user_id, ref.display_name) == (UserId("u-7"), "张三")

    argv = spawn.argv_for("users", "search")
    assert argv[0] == "wecom-cli"
    assert argv[1:4] == ["contact", "users", "search"]
    assert argv[argv.index("--keywords") + 1] == "张三"
    assert argv[argv.index("--search-mode") + 1] == "list"
    # 凭据只进子进程环境，绝不进 argv
    assert not any("s3cret" in a for a in argv)


async def test_ambiguous_name_raises_instead_of_picking_first() -> None:
    """两个张三 → 抛，**不取第一个**。

    取第一个是这里最短的一行代码，也是最坏的：被派到的人以为自己看漏了上下文，
    该做的人根本不知道有这件事，系统显示"已通知"。没有任何一方会报错。
    """
    adapter, _ = _adapter(
        {
            ("users", "search"): {
                "users": [
                    {"userid": "u-7", "name": "张三"},
                    {"userid": "u-9", "name": "张三"},
                ]
            }
        }
    )
    with pytest.raises(AmbiguousUser) as err:
        await adapter.resolve_user("张三")
    assert err.value.candidates == ("张三", "张三")
    # 归到可澄清一类，而不是告警或重试
    assert err.value.error_code == "AMBIGUOUS_USER"
    assert isinstance(err.value, PortUnavailable)


async def test_unknown_user_is_not_silently_skipped() -> None:
    adapter, _ = _adapter({("users", "search"): {"users": []}})
    with pytest.raises(WecomTodoUnavailable) as err:
        await adapter.resolve_user("不存在的人")
    assert err.value.error_code == "USER_NOT_FOUND"


async def test_row_without_userid_is_schema_error_not_not_found() -> None:
    """认出了行却没有 userid → 契约变了，不是"查无此人"。

    两者的处置完全不同：查无此人要问人，契约变了要改代码。混成一个码会让后者
    表现为"这个人怎么总也搜不到"。
    """
    adapter, _ = _adapter({("users", "search"): {"users": [{"name": "张三"}]}})
    with pytest.raises(WecomTodoUnavailable) as err:
        await adapter.resolve_user("张三")
    assert err.value.error_code == "SCHEMA_UNKNOWN"


async def test_fuzzy_user_result_is_not_accepted_as_exact_match() -> None:
    adapter, _ = _adapter(
        {("users", "search"): {"users": [{"userid": "u-7", "name": "张三丰"}]}}
    )
    with pytest.raises(WecomTodoUnavailable) as err:
        await adapter.resolve_user("张三")
    assert err.value.error_code == "USER_NOT_FOUND"


# --- 建待办：不可撤回 -------------------------------------------------------


async def test_create_returns_todo_id() -> None:
    adapter, spawn = _adapter(
        {
            ("todo", "create"): {
                "items": [{"success": True, "todo_id": "t-100"}],
                "items_count": 1,
            }
        }
    )
    ref = await adapter.create(
        TodoCreate(
            title="确认限额冲突",
            assignee_ids=(UserId("u-7"),),
            chat_id="chat-1",
            due_at=datetime(2026, 9, 22, 3, 0, 0),  # naive
            detail="见卡片",
        ),
        IDEM,
    )
    assert ref.todo_id == TodoId("t-100")

    argv = spawn.argv_for("todo", "create")
    body = json.loads(argv[argv.index("--json") + 1])
    assert body == {
        "items": [
            {
                "title": "确认限额冲突",
                "follower_ids": ["u-7"],
                "description": "见卡片",
                "deadline": {"type": "datetime", "value": "2026-09-22 03:00:00"},
            }
        ]
    }
    assert "--title" not in argv
    # idem_key 不下传（企微待办无幂等键），只用于审计关联
    assert not any("todo:finding-1" in a for a in argv)


async def test_create_without_assignee_never_reaches_subprocess() -> None:
    """没有负责人的待办谁都收不到，但 API 多半照样返回 id → 系统认为建成了。"""
    adapter, spawn = _adapter({("todo", "create"): {"todo_id": "t-1"}})
    with pytest.raises(WecomTodoUnavailable) as err:
        await adapter.create(TodoCreate(title="孤儿待办", assignee_ids=()), IDEM)
    assert err.value.error_code == "INVALID_ARGUMENT"
    assert spawn.calls == []  # 在启动子进程之前就被拦掉


async def test_create_ok_without_id_says_recheck_not_recreate() -> None:
    """命令成功但拿不到 id：待办**可能真的建了**。

    这里抛 SCHEMA_UNKNOWN 而不是"建失败"，reason 里明写"须回查不得重建"——
    报成失败会引来一次重建，也就是当事人手机上的第二条待办。
    """
    adapter, _ = _adapter({("todo", "create"): {"errcode": 0}})
    with pytest.raises(WecomTodoUnavailable) as err:
        await adapter.create(
            TodoCreate(title="x", assignee_ids=(UserId("u-7"),)), IDEM
        )
    assert err.value.error_code == "SCHEMA_UNKNOWN"
    assert "不得重建" in err.value.reason


async def test_create_never_retries() -> None:
    """建待办 ``max_attempts=1``：可重试的 5xx 也只打一次。

    重试对读操作是好事，对建待办是"再推一条"。"疑似没建成"的正确处理是回查。
    """
    adapter, spawn = _adapter({("todo", "create"): (1, "", "500 upstream error")})
    with pytest.raises(WecomTodoUnavailable):
        await adapter.create(
            TodoCreate(title="x", assignee_ids=(UserId("u-7"),)), IDEM
        )
    assert len(spawn.calls) == 1
    assert spawn.sleeps == []


# --- 批量回读：E-5 的提醒依据 ------------------------------------------------


async def test_missing_ids_are_neither_finished_nor_unfinished() -> None:
    """回读不到的 id 从结果里剔除并挂到 ``last_batch_missing``。

    ``TodoSnapshot.finished`` 是 bool，装不下"未知"。按已完成处理会永久停掉提醒
    （E-5 的失效方式恰好是什么都不发，没人会注意到）；按未完成会一直发。
    两种都不做。
    """
    adapter, _ = _adapter(
        {
            ("todo", "get"): {
                "items": [{"success": True, "todo_id": "t-1", "status": "finished"}]
            }
        }
    )
    out = await adapter.get_batch([TodoId("t-1"), TodoId("t-2"), TodoId("t-3")])
    assert [s.todo_id for s in out] == [TodoId("t-1")]
    assert adapter.last_batch_missing == (TodoId("t-2"), TodoId("t-3"))


async def test_empty_rows_for_nonempty_request_is_schema_error() -> None:
    """给了 id 却一行都没回来 → 不是"都没了"，是没认出结构。"""
    adapter, _ = _adapter({("todo", "get"): {"unexpected": "shape"}})
    with pytest.raises(WecomTodoUnavailable) as err:
        await adapter.get_batch([TodoId("t-1")])
    assert err.value.error_code == "SCHEMA_UNKNOWN"


async def test_empty_request_is_not_a_call() -> None:
    adapter, spawn = _adapter({})
    assert await adapter.get_batch([]) == []
    assert spawn.calls == []


async def test_snapshot_carries_remind_fields() -> None:
    """回读行上的标题、参与人、负责人、截止和原文状态要进 TodoSnapshot。"""
    adapter, _ = _adapter(
        {
            ("todo", "get"): {
                "items": [
                    {
                        "todo_id": "t",
                        "status": "deleted",
                        "title": "补齐联调文档",
                        "follower_ids": ["user-a"],
                        "creator": "owner-1",
                        "deadline": {"type": "datetime", "value": "2026-09-22 03:00:00"},
                    }
                ]
            }
        }
    )
    [snap] = await adapter.get_batch([TodoId("t")])
    assert snap.finished is False
    assert snap.status == "deleted"
    assert snap.content == "补齐联调文档"
    assert snap.assignee_ids == ("user-a",)
    assert snap.owner_id == "owner-1"
    assert snap.due_at == datetime(2026, 9, 22, 3, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "row,expected",
    [
        # 顶层 status 说完成
        ({"todo_id": "t", "status": "finished"}, True),
        # follower 全完成
        (
            {
                "todo_id": "t",
                "follower_status": [
                    {"userid": "a", "status": "1"},
                    {"userid": "b", "status": "1"},
                ],
            },
            True,
        ),
        # 一个没完成
        (
            {
                "todo_id": "t",
                "follower_status": [
                    {"userid": "a", "status": "1"},
                    {"userid": "b", "status": "0"},
                ],
            },
            False,
        ),
        # follower 表空 → 「读不到跟进状态」，不是「没人需要跟进所以算完成」
        ({"todo_id": "t"}, False),
        # 认不出的取值 → 当未完成（多发一条提醒，而不是静默停掉）
        ({"todo_id": "t", "follower_status": {"a": "某种新状态"}}, False),
    ],
)
async def test_finished_semantics(row: dict[str, Any], expected: bool) -> None:
    adapter, _ = _adapter({("todo", "get"): {"items": [row]}})
    out = await adapter.get_batch([TodoId("t")])
    assert out[0].finished is expected


# --- 二进制缺失（今日真实状态）----------------------------------------------


async def test_binary_missing_is_terminal_not_a_switch_signal() -> None:
    """待办域没有第二条传输：``BINARY_MISSING`` 就是"这条能力不存在"。

    与 ``tapd_cli`` 相反——那边同一个码触发回落到 MCP。这里没有可回落的地方，
    所以它必须表现为普通的不可用，由调用方在群内明示缺失。
    """
    spawn = FakeSpawn({("users", "search"): FileNotFoundError("wecom-cli")})
    executor = CliExecutor(
        workspace_id=WS,
        binaries={"wecom": "wecom-cli-not-installed-xyz"},
        specs=WECOM_COMMANDS,
        spawn=spawn,
        retry_sleep=spawn.record_sleep,
    )
    adapter = WecomTodoAdapter(executor, workspace_id=WS)

    assert await adapter.probe() is False
    with pytest.raises(WecomTodoUnavailable) as err:
        await adapter.resolve_user("张三")
    assert err.value.error_code == "BINARY_MISSING"
    assert err.value.port == "wecom.todo"


async def test_probe_false_when_subdomain_denied() -> None:
    """二进制在、子域受限 → probe 仍为 False，但日志区分得开。

    这两种"不可用"的处置不同：没装要装，没权限要申请。probe 的返回值一样，
    是因为对 ``container.py`` 而言结论一样：E-4 / E-5 今日不交付。
    """
    adapter, _ = _adapter({("users", "search"): (1, "", "403 permission denied")})
    assert await adapter.probe() is False


async def test_probe_true_when_probe_name_simply_not_found() -> None:
    """查不到 ``__wetag_probe__`` 这个人是**预期结果**：它证明子域通了。"""
    adapter, _ = _adapter({("users", "search"): {"users": []}})
    assert await adapter.probe() is True


# --- 删除类不可达（裁决 #10）------------------------------------------------


def test_no_delete_command_registered() -> None:
    """待办删除类命令一律不登记——不可达优于运行时拒绝。"""
    keys = [s.key for s in WECOM_COMMANDS]
    assert not any("delete" in k or "remove" in k for k in keys), keys
    # 同时确认没有任何一条被标成 DESTRUCTIVE（registry 里有全局 assert，这里是局部复核）
    assert {s.key for s in WECOM_COMMANDS} == {
        "wecom.todo.user_search",
        "wecom.todo.create",
        "wecom.todo.get",
        "wecom.todo.update",
    }
