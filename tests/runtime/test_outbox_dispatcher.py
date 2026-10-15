"""Outbox Dispatcher 形态校验测试。

covers: TC-041
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.contracts.envelope import Envelope, MsgType, Priority
from app.runtime.outbox.dispatcher import Dispatcher, validate_msg_type
from app.runtime.repository import OutboxStatus

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _envelope(**kw) -> Envelope:
    defaults = {"chat_id": "chat-1", "msg_type": MsgType.MARKDOWN, "payload": {"text": "hi"}}
    return Envelope(**(defaults | kw))


class _SpyMerger:
    def __init__(self) -> None:
        self.calls: list = []

    async def merge_or_create(self, envelope, *, now=None):
        self.calls.append(envelope)
        raise AssertionError("非法形态不应进入合并环节")


def test_validate_msg_type_closed_set():
    assert validate_msg_type(MsgType.MARKDOWN) is MsgType.MARKDOWN
    assert validate_msg_type("template_card") is MsgType.TEMPLATE_CARD
    with pytest.raises(ValueError):
        validate_msg_type("image")
    with pytest.raises(ValueError):
        validate_msg_type(None)


async def test_invalid_msg_type_rejected_before_pipeline(outbox_repo, alerts):
    """TC-041：非法形态在合并前拒绝并告警，不入库、不进合并/会话/节流。"""
    merger = _SpyMerger()
    dispatcher = Dispatcher(outbox_repo, merger=merger)
    # model_construct 绕过 pydantic 校验，模拟上游产出非法形态
    bad = Envelope.model_construct(
        chat_id="chat-1",
        msg_type="image",
        payload={"url": "http://x/y.png"},
        merge_key="chat-1:topic:push",
        priority=Priority.NORMAL,
    )
    result = await dispatcher.submit(bad, now=T0)

    assert result is None
    assert merger.calls == []  # 未进入合并环节
    count = await outbox_repo.fetchval(
        "SELECT count(*) FROM outbox WHERE workspace_id = $1"
    )
    assert count == 0  # 不入库
    assert len(alerts) == 1
    assert "非法出站形态" in alerts[0].title
    assert "image" in alerts[0].fields["msg_type"]


async def test_valid_msg_types_accepted(outbox_repo, alerts):
    """TC-041 对照：markdown 与 template_card 正常入库为 PENDING。"""
    dispatcher = Dispatcher(outbox_repo)
    m1 = await dispatcher.submit(_envelope(), now=T0)
    m2 = await dispatcher.submit(
        _envelope(msg_type=MsgType.TEMPLATE_CARD, payload={"card": {}}), now=T0
    )
    assert m1 is not None and m1.status == OutboxStatus.PENDING
    assert m2 is not None and m2.status == OutboxStatus.PENDING
    assert m1.not_before == T0  # 无 merge_key 直通，不等窗口
    assert alerts == []
