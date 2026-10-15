"""Outbox 合并窗口测试。

covers: TC-035, TC-036, TC-037
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.contracts.envelope import Envelope, MsgType, Priority
from app.runtime.outbox.dispatcher import Dispatcher
from app.runtime.outbox.merger import Merger
from app.runtime.repository import OutboxStatus

T0 = datetime(2026, 1, 1, tzinfo=UTC)
WINDOW = 60.0


def _envelope(text: str, **kw) -> Envelope:
    defaults = {
        "chat_id": "chat-1",
        "msg_type": MsgType.MARKDOWN,
        "payload": {"text": text},
        "merge_key": "chat-1:topic-1:change_push",
    }
    return Envelope(**(defaults | kw))


class _FakeSender:
    def __init__(self) -> None:
        self.calls: list = []

    async def __call__(self, msg) -> str:
        self.calls.append(msg)
        return f"sent-{len(self.calls)}"


def _dispatcher(outbox_repo, sender) -> Dispatcher:
    return Dispatcher(
        outbox_repo, merger=Merger(outbox_repo, window_seconds=WINDOW), sender=sender
    )


async def test_same_key_merges_into_one_message(outbox_repo):
    """TC-035：同群同事项同类型的多条普通内容在窗口内合并为一条。"""
    sender = _FakeSender()
    dispatcher = _dispatcher(outbox_repo, sender)
    for i in range(3):
        await dispatcher.submit(_envelope(f"变更-{i}"), now=T0 + timedelta(seconds=i))

    assert await outbox_repo.count_by_status(OutboxStatus.PENDING) == 1
    assert await outbox_repo.count_by_status(OutboxStatus.MERGED) == 2

    # 窗口到期：只发一条，包含全部条目
    results = await dispatcher.dispatch_due(now=T0 + timedelta(seconds=WINDOW + 1))
    assert len(results) == 1
    assert results[0].status == OutboxStatus.SENT
    assert len(sender.calls) == 1
    items = sender.calls[0].payload["items"]
    assert [it["text"] for it in items] == ["变更-0", "变更-1", "变更-2"]


async def test_window_not_extended_by_new_content(outbox_repo):
    """TC-036：持续进入内容不延后首个合并窗口的 not_before。"""
    dispatcher = _dispatcher(outbox_repo, _FakeSender())
    first = await dispatcher.submit(_envelope("首条"), now=T0)
    assert first.not_before == T0 + timedelta(seconds=WINDOW)

    for lag in (10, 30, 50):
        survived = await dispatcher.submit(
            _envelope(f"后续-{lag}"), now=T0 + timedelta(seconds=lag)
        )
        assert survived.id == first.id
        assert survived.not_before == first.not_before  # 保持首次值，不顺延

    # 窗口时刻一到即可发，不因 50s 处还有新内容而推迟
    early = await dispatcher.dispatch_due(now=T0 + timedelta(seconds=WINDOW - 1))
    assert early == []
    due = await dispatcher.dispatch_due(now=T0 + timedelta(seconds=WINDOW))
    assert len(due) == 1 and due[0].status == OutboxStatus.SENT


async def test_urgent_bypasses_merge_window(outbox_repo):
    """TC-037：紧急内容跳过合并窗口优先发送；普通内容仍等待窗口。"""
    sender = _FakeSender()
    dispatcher = _dispatcher(outbox_repo, sender)
    normal = await dispatcher.submit(_envelope("普通推送"), now=T0)
    urgent = await dispatcher.submit(
        _envelope("紧急冲突预警", priority=Priority.URGENT, merge_key=None), now=T0
    )

    # urgent 不等窗口；normal 等窗口
    assert urgent.not_before == T0
    assert normal.not_before == T0 + timedelta(seconds=WINDOW)

    # 窗口前派发：只有 urgent 发出，普通内容仍 PENDING
    results = await dispatcher.dispatch_due(now=T0)
    assert [r.id for r in results] == [urgent.id]
    assert results[0].status == OutboxStatus.SENT
    assert len(sender.calls) == 1
    assert await outbox_repo.get(normal.id) is not None
    assert (await outbox_repo.get(normal.id)).status == OutboxStatus.PENDING


async def test_urgent_with_merge_key_not_merged(outbox_repo):
    """TC-037 边界：URGENT 即使带 merge_key 也不并入窗口（契约：URGENT 必须无 merge_key）。"""
    sender = _FakeSender()
    dispatcher = _dispatcher(outbox_repo, sender)
    await dispatcher.submit(_envelope("普通"), now=T0)
    urgent = await dispatcher.submit(
        _envelope("紧急", priority=Priority.URGENT), now=T0  # 同 merge_key
    )
    assert urgent.status == OutboxStatus.PENDING  # 独立成行，未 MERGED
    assert urgent.not_before == T0
    assert "items" not in urgent.payload  # 未走合并包装
