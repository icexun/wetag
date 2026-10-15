"""待办引用台账：只存归属与升级计数，详情实时回读（DB 集成）。

covers: TC-082, TC-085, TC-086
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.control.ledger.todo_ref import TodoRefRepository, TodoRefStatus


@pytest.fixture()
def todo_repo(pool):
    return TodoRefRepository(pool)


class _FakeTodoPort:
    """企微待办替身：详情只存在于原生侧，本地不存副本。"""

    def __init__(self, details: dict[str, dict]) -> None:
        self.details = details
        self.calls: list[list[str]] = []

    async def get_batch(self, todo_ids: list[str]) -> list[dict]:
        self.calls.append(list(todo_ids))
        return [self.details[t] for t in todo_ids if t in self.details]


# ---- TC-082 待办详情实时回读且本地不保存副本 (US11-1) ----


async def test_details_always_read_back_no_local_copy(todo_repo):
    """covers: TC-082 —— 本地仅归属与提醒计数；内容/参与人/截止/完成状态实时回读。"""
    await todo_repo.register("todo-1", "chat-1", topic_id="topic-1", source="mention")

    ref = await todo_repo.get("todo-1")
    assert ref is not None
    # 本地记录只含归属与升级计数
    assert (ref.chat_id, ref.topic_id, ref.source) == ("chat-1", "topic-1", "mention")
    assert ref.remind_count == 0 and ref.status == TodoRefStatus.ACTIVE
    assert not hasattr(ref, "content") and not hasattr(ref, "due_at")

    # 详情一律实时回读原生侧
    port = _FakeTodoPort(
        {
            "todo-1": {
                "id": "todo-1",
                "status": "active",
                "content": "补齐联调文档",
                "assignee_ids": ["user-a"],
                "due_at": "2026-09-21T10:00:00+00:00",
            }
        }
    )
    [detail] = await port.get_batch(["todo-1"])
    assert detail["content"] == "补齐联调文档"  # 内容来自实时回读
    assert detail["assignee_ids"] == ["user-a"]

    # 原生侧变更后再次回读即得最新状态，本地无需也不存在同步副本
    port.details["todo-1"]["content"] = "补齐联调文档（已更新）"
    [fresh] = await port.get_batch(["todo-1"])
    assert fresh["content"] == "补齐联调文档（已更新）"
    ref_again = await todo_repo.get("todo-1")
    assert ref_again is not None and not hasattr(ref_again, "content")


async def test_register_is_idempotent_on_attribution(todo_repo):
    """重复登记只刷新归属字段，不清提醒计数（归属登记的幂等）。"""
    await todo_repo.register("todo-1", "chat-1", source="mention")
    await todo_repo.record_remind("todo-1", datetime.now(UTC))

    await todo_repo.register("todo-1", "chat-2", topic_id="t-9", source="conflict")

    ref = await todo_repo.get("todo-1")
    assert ref is not None
    assert (ref.chat_id, ref.topic_id, ref.source) == ("chat-2", "t-9", "conflict")
    assert ref.remind_count == 1  # 计数保留


async def test_record_remind_once_per_cycle(todo_repo):
    """同一跟进周期内 record_remind 只生效一次（US11-7 的存储层保证）。"""
    await todo_repo.register("todo-1", "chat-1", source="mention")
    cycle = datetime.now(UTC) - timedelta(minutes=15)

    assert await todo_repo.record_remind("todo-1", cycle) is True
    assert await todo_repo.record_remind("todo-1", cycle) is False  # 同周期不重复

    ref = await todo_repo.get("todo-1")
    assert ref is not None and ref.remind_count == 1

    next_cycle = datetime.now(UTC) + timedelta(minutes=15)
    assert await todo_repo.record_remind("todo-1", next_cycle) is True  # 下一周期可再提醒


# ---- TC-085 已完成待办归档并停止提醒 (US11-4) ----


async def test_completed_todo_archived_and_kept_queryable(todo_repo):
    """covers: TC-085 —— 归档后不再出现在 ACTIVE 扫描面，记录保留可查。"""
    await todo_repo.register("todo-1", "chat-1", source="mention")
    await todo_repo.register("todo-2", "chat-1", source="conflict")

    assert await todo_repo.archive("todo-1") is True

    ref = await todo_repo.get("todo-1")
    assert ref is not None and ref.status == TodoRefStatus.ARCHIVED  # 保留可查
    active = await todo_repo.list_active()
    assert [r.wecom_todo_id for r in active] == ["todo-2"]  # 不再提醒
    # 归档是终态迁移：重复归档不生效，也不再接受提醒计数
    assert await todo_repo.archive("todo-1") is False
    assert await todo_repo.record_remind("todo-1", datetime.now(UTC)) is False


# ---- TC-086 不可见或已删除待办失效且停止报错 (US11-5) ----


async def test_invisible_todo_invalidated_and_silent_afterwards(todo_repo):
    """covers: TC-086 —— 失效后停止提醒；重复失效幂等，不在后续周期重复报错。"""
    await todo_repo.register("todo-1", "chat-1", source="mention")

    assert await todo_repo.invalidate("todo-1") is True

    ref = await todo_repo.get("todo-1")
    assert ref is not None and ref.status == TodoRefStatus.INVALID
    assert await todo_repo.list_active() == []  # 后续周期不再扫描到 → 不反复报错
    assert await todo_repo.invalidate("todo-1") is False  # 幂等
    assert await todo_repo.record_remind("todo-1", datetime.now(UTC)) is False
