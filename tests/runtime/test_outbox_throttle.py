"""Outbox 令牌桶限流与发送重试测试。

covers: TC-040, TC-042
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.contracts.envelope import Envelope, MsgType, Priority
from app.contracts.errors import IsolationViolation
from app.runtime.outbox.dispatcher import Dispatcher
from app.runtime.outbox.throttle import Throttle, TokenBucket
from app.runtime.repository import OutboxStatus

T0 = datetime(2026, 1, 1, tzinfo=UTC)
CHAT = "chat-1"


def _envelope(text: str = "hi", **kw) -> Envelope:
    defaults = {"chat_id": CHAT, "msg_type": MsgType.MARKDOWN, "payload": {"text": text}}
    return Envelope(**(defaults | kw))


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeSender:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list = []
        self.fail = fail

    async def __call__(self, msg) -> str:
        self.calls.append(msg)
        if self.fail:
            raise RuntimeError("wecom send failed")
        return f"sent-{len(self.calls)}"


async def test_throttled_message_rescheduled_not_dropped(outbox_repo):
    """TC-040：令牌桶无可用令牌时，消息保持 PENDING 重排到下一可发时刻。"""
    clock = _FakeClock()
    # 1/min：第一条取走后，下一条需等约 60s
    throttle = Throttle(
        per_minute=1, per_hour=100, urgent_per_minute=1, urgent_per_hour=100, clock=clock
    )
    sender = _FakeSender()
    dispatcher = Dispatcher(outbox_repo, throttle=throttle, sender=sender)

    await dispatcher.submit(_envelope("第一条"), now=T0)
    second = await dispatcher.submit(_envelope("第二条"), now=T0)

    results = await dispatcher.dispatch_due(now=T0)
    assert results[0].status == OutboxStatus.SENT
    # 第二条被重排：仍 PENDING，not_before 推到下一可发时刻，未丢弃
    assert results[1].status == OutboxStatus.PENDING
    assert results[1].not_before > T0
    assert results[1].not_before <= T0 + timedelta(seconds=61)
    assert len(sender.calls) == 1

    # 到达重排时刻后正常发出
    clock.advance(61)
    later = await dispatcher.dispatch_due(now=T0 + timedelta(seconds=61))
    assert len(later) == 1 and later[0].status == OutboxStatus.SENT
    assert len(sender.calls) == 2
    assert (await outbox_repo.get(second.id)).status == OutboxStatus.SENT


async def test_urgent_uses_reserved_headroom(outbox_repo):
    """TC-040 边界：常规令牌被普通消息占满时，urgent 可透支预留余量即时发出。"""
    clock = _FakeClock()
    # 硬限 2/min，常规限 1/min：普通消息取后须留 1 个给 urgent
    throttle = Throttle(
        per_minute=1, per_hour=100, urgent_per_minute=2, urgent_per_hour=100, clock=clock
    )
    sender = _FakeSender()
    dispatcher = Dispatcher(outbox_repo, throttle=throttle, sender=sender)

    await dispatcher.submit(_envelope("普通-1"), now=T0)
    await dispatcher.submit(_envelope("普通-2"), now=T0)
    await dispatcher.submit(_envelope("紧急回执", priority=Priority.URGENT), now=T0)

    results = await dispatcher.dispatch_due(now=T0)
    by_payload = {r.payload["text"]: r for r in results}
    assert by_payload["普通-1"].status == OutboxStatus.SENT
    assert by_payload["普通-2"].status == OutboxStatus.PENDING  # 常规额度用尽，重排
    assert by_payload["紧急回执"].status == OutboxStatus.SENT  # 预留余量直通


async def test_send_retry_backoff_then_failed(outbox_repo, alerts):
    """TC-042：发送连续失败按 1s→5s→30s 退避重试，第 4 次失败标 FAILED 并告警。"""
    sender = _FakeSender(fail=True)
    dispatcher = Dispatcher(outbox_repo, sender=sender, send_max_attempt=4)
    msg = await dispatcher.submit(_envelope(), now=T0)

    expected_backoffs = [1.0, 5.0, 30.0]
    cursor = T0
    for i, backoff in enumerate(expected_backoffs, start=1):
        results = await dispatcher.dispatch_due(now=cursor)
        assert len(results) == 1
        current = results[0]
        assert current.status == OutboxStatus.PENDING
        assert current.attempt == i
        assert current.not_before == cursor + timedelta(seconds=backoff)
        cursor = current.not_before

    # 第 4 次失败：FAILED + 告警，不再重排
    final = await dispatcher.dispatch_due(now=cursor)
    assert final[0].status == OutboxStatus.FAILED
    stored = await outbox_repo.get(msg.id)
    assert stored.status == OutboxStatus.FAILED
    assert stored.attempt == 4
    assert len(sender.calls) == 4
    assert any("已终止" in a.title for a in alerts)

    # 终止后不再被派发
    assert await dispatcher.dispatch_due(now=cursor + timedelta(hours=1)) == []


async def test_rejected_payload_fails_immediately(outbox_repo, alerts):
    """载荷非法是永久性失败：一次即 FAILED，不再打到发送侧重试。"""

    class _Reject:
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, msg) -> str:
            self.calls += 1
            raise IsolationViolation("模板卡片缺少 main_title.title")

    sender = _Reject()
    dispatcher = Dispatcher(outbox_repo, sender=sender, send_max_attempt=4)
    msg = await dispatcher.submit(_envelope(), now=T0)

    final = await dispatcher.dispatch_due(now=T0)

    assert sender.calls == 1
    assert final[0].status == OutboxStatus.FAILED
    assert final[0].attempt == 1
    stored = await outbox_repo.get(msg.id)
    assert stored is not None and stored.status == OutboxStatus.FAILED
    assert any("被拒绝" in a.title for a in alerts)
    assert await dispatcher.dispatch_due(now=T0 + timedelta(hours=1)) == []


def test_token_bucket_refill():
    """令牌桶单元行为：消耗后按速率补充，wait_seconds 给出重排时长。"""
    clock = _FakeClock()
    bucket = TokenBucket(capacity=2, refill_per_second=0.5, clock=clock)  # 2s 补 1 个
    assert bucket.wait_seconds(1) == 0.0
    bucket.consume()
    bucket.consume()
    assert bucket.wait_seconds(1) == 2.0
    clock.advance(3)
    assert bucket.wait_seconds(1) == 0.0  # 已补 1.5 个
