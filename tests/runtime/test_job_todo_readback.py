"""逾期待办回读跟进：提醒、升级抄送、能力降级、同周期不重复（DB 集成）。

covers: TC-083, TC-084, TC-087, TC-088
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.contracts.errors import PortUnavailable
from app.contracts.ids import TodoId
from app.contracts.ports import TodoSnapshot
from app.contracts.task import Task, TaskKind, TriggerSource
from app.control.ledger.todo_ref import TodoRefRepository, TodoRefStatus
from app.platform.degradation import degrade_key
from app.runtime.jobs.base import JobContext
from app.runtime.jobs.todo_readback import TodoReadbackJob

_NOW = datetime.now(UTC)
# 跟进周期锚点取过去时刻：record_remind 以 DB now() 落 last_remind_at，
# 锚点在过去可吸收宿主与 DB 的时钟漂移（同周期去重断言不受漂移影响）
_CYCLE = _NOW - timedelta(minutes=10)


def _snapshot(detail: dict) -> TodoSnapshot:
    due = detail.get("due_at")
    if isinstance(due, str):
        due = datetime.fromisoformat(due)
    status = str(detail.get("status") or "")
    return TodoSnapshot(
        todo_id=TodoId(detail["id"]),
        finished=status.lower() in {"completed", "done"},
        content=str(detail.get("content") or ""),
        assignee_ids=tuple(detail.get("assignee_ids") or ()),
        owner_id=str(detail.get("owner_id") or detail.get("creator") or ""),
        due_at=due,
        status=status,
    )


class _TodoPort:
    """企微待办替身：回读结果是 TodoSnapshot，与真实端口一致。"""

    def __init__(self, details: dict[str, dict]) -> None:
        self.details = details
        self.fail: Exception | None = None
        self.calls = 0

    async def get_batch(self, todo_ids: list[str]) -> list[TodoSnapshot]:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return [_snapshot(self.details[t]) for t in todo_ids if t in self.details]


def _overdue(todo_id: str, **overrides) -> dict:
    detail = {
        "id": todo_id,
        "status": "active",
        "content": "补齐联调文档",
        "assignee_ids": ["user-a"],
        "owner_id": "owner-1",
        "due_at": (_NOW - timedelta(hours=1)).isoformat(),
    }
    detail.update(overrides)
    return detail


def _ctx(pool, state_store=None, scheduled_at=_CYCLE) -> JobContext:
    task = Task(
        id=uuid4(),
        workspace_id=pool.workspace_id,
        job_name="todo_readback",
        kind=TaskKind.DETERMINISTIC,
        trigger_source=TriggerSource.SCHEDULE,
        scheduled_at=scheduled_at,
    )
    return JobContext(task=task, workspace_id=pool.workspace_id, state=state_store)


@pytest.fixture()
def todo_repo(pool):
    return TodoRefRepository(pool)


# ---- TC-083 未达升级阈值的逾期待办提醒责任人 (US11-2) ----


async def test_overdue_todo_reminds_assignee(todo_repo, pool):
    """covers: TC-083 —— 逾期且计数低于阈值：群内 @ 责任人，提醒计数 +1。"""
    await todo_repo.register("todo-1", "chat-1", topic_id="topic-1", source="mention")
    job = TodoReadbackJob(todo_port=_TodoPort({"todo-1": _overdue("todo-1")}), todo_repo=todo_repo)

    result = await job.run(_ctx(pool))

    assert result.metrics["reminded"] == 1
    [env] = result.envelopes
    assert env.chat_id == "chat-1" and env.topic_id == "topic-1"
    assert env.msg_type == "markdown"
    assert env.payload["mentions"] == ["user-a"]  # @ 责任人
    assert "补齐联调文档" in env.payload["text"]
    ref = await todo_repo.get("todo-1")
    assert ref is not None and ref.remind_count == 1 and ref.last_remind_at is not None


async def test_not_overdue_todo_not_reminded(todo_repo, pool):
    """未逾期待办不提醒（仅逾期待办进入跟进）。"""
    await todo_repo.register("todo-1", "chat-1", source="mention")
    future = (_NOW + timedelta(hours=1)).isoformat()
    job = TodoReadbackJob(todo_port=_TodoPort({"todo-1": _overdue("todo-1", due_at=future)}), todo_repo=todo_repo)

    result = await job.run(_ctx(pool))

    assert result.envelopes == []
    ref = await todo_repo.get("todo-1")
    assert ref is not None and ref.remind_count == 0


# ---- TC-084 达升级阈值的逾期待办抄送负责人 (US11-3) ----


async def test_threshold_reached_escalates_to_owner(todo_repo, pool):
    """covers: TC-084 —— 计数达阈值：升级提醒并抄送需求负责人，计数更新。"""
    await todo_repo.register("todo-1", "chat-1", source="mention")
    # 已提醒 2 次（直接置计数，绕过同周期守卫），本次为第 3 次（达缺省阈值 3）
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE todo_ref SET remind_count = 2 "
            "WHERE workspace_id = $1 AND wecom_todo_id = $2",
            pool.workspace_id,
            "todo-1",
        )
    job = TodoReadbackJob(todo_port=_TodoPort({"todo-1": _overdue("todo-1")}), todo_repo=todo_repo)

    result = await job.run(_ctx(pool))

    assert result.metrics["escalated"] == 1 and result.metrics["reminded"] == 0
    [env] = result.envelopes
    assert env.msg_type == "template_card"
    assert env.payload["card_type"] == "todo_escalation"
    assert env.payload["cc"] == ["owner-1"]  # 抄送需求负责人
    assert env.payload["mentions"] == ["user-a"]
    ref = await todo_repo.get("todo-1")
    assert ref is not None and ref.remind_count == 3  # 计数更新


async def test_threshold_configurable(todo_repo, pool):
    """阈值由配置注入（US11-3：业务线管理员可配），非写死。"""
    await todo_repo.register("todo-1", "chat-1", source="mention")
    job = TodoReadbackJob(
        todo_port=_TodoPort({"todo-1": _overdue("todo-1")}), todo_repo=todo_repo, escalate_threshold=1
    )

    result = await job.run(_ctx(pool))

    assert result.metrics["escalated"] == 1  # 阈值 1：首次提醒即升级


# ---- TC-088 同一跟进周期对同一待办只提醒一次 (US11-7) ----


async def test_same_cycle_reminds_only_once(todo_repo, pool):
    """covers: TC-088 —— 同周期重复执行不重复提醒；下一周期可再次提醒。"""
    await todo_repo.register("todo-1", "chat-1", source="mention")
    job = TodoReadbackJob(todo_port=_TodoPort({"todo-1": _overdue("todo-1")}), todo_repo=todo_repo)

    first = await job.run(_ctx(pool))
    second = await job.run(_ctx(pool))  # 同一任务轮次重放

    assert len(first.envelopes) == 1
    assert second.envelopes == []  # 不产生第二条提醒
    ref = await todo_repo.get("todo-1")
    assert ref is not None and ref.remind_count == 1  # 计数不重复增加

    # 下一跟进周期（新轮次）正常再次提醒
    third = await job.run(_ctx(pool, scheduled_at=_CYCLE + timedelta(minutes=15)))
    assert len(third.envelopes) == 1
    ref = await todo_repo.get("todo-1")
    assert ref is not None and ref.remind_count == 2


# ---- TC-087 待办能力不可用时群内明确降级 (US11-6) ----


async def test_capability_unavailable_sends_explicit_card(todo_repo, pool, state_store):
    """covers: TC-087 —— 能力整体不可用：群内卡片明示「暂不可用」，
    不以本地数据伪装已交付（不提醒、不计数、不归档）。"""
    await todo_repo.register("todo-1", "chat-1", source="mention")
    await todo_repo.register("todo-2", "chat-2", source="conflict")
    port = _TodoPort({})
    port.fail = PortUnavailable("wecom.todo", "DOWN", "wecom todo api down")
    job = TodoReadbackJob(todo_port=port, todo_repo=todo_repo)

    result = await job.run(_ctx(pool, state_store=state_store))

    assert result.metrics["degraded"] is True and result.metrics["checked"] == 0
    # 每个受影响群收到「暂不可用」卡片
    assert {e.chat_id for e in result.envelopes} == {"chat-1", "chat-2"}
    for env in result.envelopes:
        assert env.msg_type == "template_card"
        assert env.payload["card_type"] == "capability_unavailable"
        assert "暂不可用" in env.payload["text"]
    # 不伪装：本地归属原样保留，无提醒计数、无归档/失效迁移
    for tid in ("todo-1", "todo-2"):
        ref = await todo_repo.get(tid)
        assert ref is not None
        assert ref.status == TodoRefStatus.ACTIVE and ref.remind_count == 0
    # 降级状态对运维可见
    degrade = await state_store.get(degrade_key("todo_capability"))
    assert degrade and degrade["active"] is True


async def test_programming_error_is_not_capability_degrade(todo_repo, pool, state_store):
    """代码错误不能伪装成能力降级，否则会给群发一张空卡片。"""
    await todo_repo.register("todo-1", "chat-1", source="mention")

    class _BadPort:
        async def get_batch(self, todo_ids: list[str]) -> list[TodoSnapshot]:
            raise AttributeError("TodoSnapshot has no get")

    job = TodoReadbackJob(todo_port=_BadPort(), todo_repo=todo_repo)

    with pytest.raises(AttributeError):
        await job.run(_ctx(pool, state_store=state_store))

    ref = await todo_repo.get("todo-1")
    assert ref is not None
    assert ref.status == TodoRefStatus.ACTIVE and ref.remind_count == 0
    assert await state_store.get(degrade_key("todo_capability")) is None


async def test_port_unbound_also_explicit_degrade(todo_repo, pool):
    """covers: TC-087 —— TodoPort 未绑定同样走显式降级，而非静默成功。"""
    await todo_repo.register("todo-1", "chat-1", source="mention")
    job = TodoReadbackJob(todo_repo=todo_repo)  # 未注入 port

    result = await job.run(_ctx(pool))

    assert result.metrics["degraded"] is True
    [env] = result.envelopes
    assert env.payload["card_type"] == "capability_unavailable"


# ---- 归档与失效在 Job 链路上的端到端行为（US11-4 / US11-5） ----


async def test_completed_and_gone_todos_transition(todo_repo, pool):
    """已完成归档、不可见失效，且二者均不再产生提醒。"""
    await todo_repo.register("todo-done", "chat-1", source="mention")
    await todo_repo.register("todo-gone", "chat-1", source="mention")
    await todo_repo.register("todo-active", "chat-1", source="mention")
    port = _TodoPort(
        {
            "todo-done": _overdue("todo-done", status="completed"),
            # todo-gone 不在回读结果中（原生侧不可见）
            "todo-active": _overdue("todo-active"),
        }
    )
    job = TodoReadbackJob(todo_port=port, todo_repo=todo_repo)

    result = await job.run(_ctx(pool))

    assert result.metrics["archived"] == 1 and result.metrics["invalidated"] == 1
    assert result.metrics["reminded"] == 1  # 仅 todo-active
    [env] = result.envelopes
    assert env.payload["wecom_todo_id"] == "todo-active"
    assert (await todo_repo.get("todo-done")).status == TodoRefStatus.ARCHIVED  # type: ignore[union-attr]
    assert (await todo_repo.get("todo-gone")).status == TodoRefStatus.INVALID  # type: ignore[union-attr]
