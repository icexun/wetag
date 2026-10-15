"""Outbox 会话态校验测试。

covers: TC-038, TC-039, TC-043
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.contracts.envelope import Envelope, MsgType
from app.runtime.outbox.dispatcher import Dispatcher
from app.runtime.outbox.session_guard import SessionGuard
from app.runtime.repository import DropReason, OutboxStatus

T0 = datetime(2026, 1, 1, tzinfo=UTC)
CHAT = "chat-1"


def _envelope(**kw) -> Envelope:
    defaults = {"chat_id": CHAT, "msg_type": MsgType.MARKDOWN, "payload": {"text": "hi"}}
    return Envelope(**(defaults | kw))


class _FakeSender:
    def __init__(self) -> None:
        self.calls: list = []

    async def __call__(self, msg) -> str:
        self.calls.append(msg)
        return f"sent-{len(self.calls)}"


def _guard(outbox_repo, last_inbound: dict, *, enforce: bool = True) -> SessionGuard:
    async def provider(chat_id: str):
        return last_inbound.get(chat_id)

    return SessionGuard(
        outbox_repo, provider, enforce=enforce, window_hours=48.0, retention_days=7
    )


async def test_never_activated_chat_dropped_forever(outbox_repo, alerts):
    """TC-038：未激活群的推送永久丢弃并告警；激活后不自动补发。"""
    last_inbound: dict = {}  # 从未 @ 过机器人
    sender = _FakeSender()
    dispatcher = Dispatcher(outbox_repo, guard=_guard(outbox_repo, last_inbound), sender=sender)

    msg = await dispatcher.submit(_envelope(), now=T0)
    results = await dispatcher.dispatch_due(now=T0)

    assert results[0].status == OutboxStatus.DROPPED
    dropped = await outbox_repo.get(msg.id)
    assert dropped.status == OutboxStatus.DROPPED  # 保留可查
    assert dropped.drop_reason == DropReason.NOT_ACTIVATED.value
    assert sender.calls == []
    assert any("激活" in a.detail for a in alerts)

    # 随后该群完成激活：被丢弃内容不补发
    last_inbound[CHAT] = T0 + timedelta(hours=1)
    again = await dispatcher.dispatch_due(now=T0 + timedelta(hours=1, seconds=1))
    assert again == []
    assert (await outbox_repo.get(msg.id)).status == OutboxStatus.DROPPED
    assert sender.calls == []


async def test_suspended_then_resent_within_retention(outbox_repo, alerts):
    """TC-039 分支一：超会话窗挂起；保留期内随下次入站补发为 SENT。"""
    last_inbound = {CHAT: T0 - timedelta(hours=49)}  # 已激活但超 48h 会话窗
    sender = _FakeSender()
    guard = _guard(outbox_repo, last_inbound)
    dispatcher = Dispatcher(outbox_repo, guard=guard, sender=sender)

    msg = await dispatcher.submit(_envelope(), now=T0)
    results = await dispatcher.dispatch_due(now=T0)
    assert results[0].status == OutboxStatus.SUSPENDED
    assert sender.calls == []

    # 保留期内群内再次 @：挂起内容补发
    inbound_at = T0 + timedelta(days=1)
    last_inbound[CHAT] = inbound_at
    summary = await guard.resume_on_inbound(CHAT, now=inbound_at)
    assert summary == {"resumed": 1, "dropped": 0}

    resent = await dispatcher.dispatch_due(now=inbound_at)
    assert len(resent) == 1 and resent[0].status == OutboxStatus.SENT
    assert len(sender.calls) == 1
    assert (await outbox_repo.get(msg.id)).status == OutboxStatus.SENT


async def test_suspended_expired_dropped_and_never_resent(outbox_repo, alerts):
    """TC-039 分支二：挂起超保留期（7 天）转 DROPPED 并告警；此后激活也不补发。"""
    last_inbound = {CHAT: T0 - timedelta(hours=49)}
    sender = _FakeSender()
    guard = _guard(outbox_repo, last_inbound)
    dispatcher = Dispatcher(outbox_repo, guard=guard, sender=sender)

    msg = await dispatcher.submit(_envelope(), now=T0)
    await dispatcher.dispatch_due(now=T0)
    assert (await outbox_repo.get(msg.id)).status == OutboxStatus.SUSPENDED

    # 保留期外才等到下一次入站
    late_inbound = T0 + timedelta(days=8)
    last_inbound[CHAT] = late_inbound
    summary = await guard.resume_on_inbound(CHAT, now=late_inbound)
    assert summary == {"resumed": 0, "dropped": 1}
    dropped = await outbox_repo.get(msg.id)
    assert dropped.status == OutboxStatus.DROPPED
    assert dropped.drop_reason == DropReason.RETENTION_EXPIRED.value
    assert any("保留期" in a.title for a in alerts)

    # 此后即便再激活也不补发
    later = await dispatcher.dispatch_due(now=late_inbound + timedelta(days=1))
    assert later == []
    assert sender.calls == []


async def test_enforce_off_sends_beyond_window(outbox_repo, alerts):
    """TC-043：会话窗约束关闭后，超窗内容不挂起直接发送。"""
    last_inbound = {CHAT: T0 - timedelta(hours=72)}  # 超窗
    sender = _FakeSender()
    dispatcher = Dispatcher(
        outbox_repo,
        guard=_guard(outbox_repo, last_inbound, enforce=False),
        sender=sender,
    )

    await dispatcher.submit(_envelope(), now=T0)
    results = await dispatcher.dispatch_due(now=T0)

    assert results[0].status == OutboxStatus.SENT  # 不挂起，直接进发送
    assert len(sender.calls) == 1
    assert await outbox_repo.count_by_status(OutboxStatus.SUSPENDED) == 0
