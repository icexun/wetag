"""Channel 入站编排：准入、隔离、幂等与拒绝路径。"""

from __future__ import annotations

from app.gateway.event import EventKind
from tests.conftest import event_frame, message_frame

# ===== 消息 =====


async def test_group_mention_registers_chat_and_submits_task(harness):
    await harness.ws.emit("message", message_frame())

    assert len(harness.sink.accepted) == 1
    event, context = harness.sink.accepted[0]
    assert event.event_type is EventKind.MESSAGE
    assert context.workspace_id == "alpha"
    assert context.chat_id == "chat_001"
    assert context.topic_id
    # 群登记以首次 @ 为起点，不依赖「被拉群」事件。
    assert "chat_001" in harness.space.chat_groups
    # 回复窗口已就绪，Runtime 可随时用 event_id 回执。
    assert harness.inbound.reply_window.frame_of(event.event_id) is not None


async def test_duplicate_msgid_is_deduplicated(harness):
    await harness.ws.emit("message", message_frame(msgid="msg_dup"))
    await harness.ws.emit("message", message_frame(msgid="msg_dup"))

    assert len(harness.sink.accepted) == 1


async def test_frame_from_foreign_bot_is_rejected(harness):
    await harness.ws.emit("message", message_frame(bot_id="bot_beta"))

    assert harness.sink.accepted == []
    assert harness.space.chat_groups == {}


async def test_chat_owned_by_another_workspace_is_refused(harness):
    """一个真实群只属于一条业务线：不执行任务，只提示。"""
    harness.space.ownership["chat_001"] = "beta"

    await harness.ws.emit("message", message_frame())

    assert harness.sink.accepted == []
    replies = harness.ws.of_kind("stream")
    assert len(replies) == 1
    assert "只保留一个业务线 Bot" in replies[0]["content"]


async def test_single_chat_is_out_of_scope_by_default(harness):
    """私聊本期不做，回引导语而不是静默丢弃（待确认 #15 兜底）。"""
    await harness.ws.emit(
        "message", message_frame(chattype="single", user_id="user_777")
    )

    assert harness.sink.accepted == []
    replies = harness.ws.of_kind("stream")
    assert len(replies) == 1
    assert replies[0]["finish"] is True


# ===== 卡片 =====


async def test_card_click_claims_and_acknowledges(harness):
    harness.cards.seed("c_1")

    await harness.ws.emit(
        "event",
        event_frame("template_card_event", {"task_id": "c_1", "event_key": "useful"}),
    )

    # 5 秒内把原卡片更新为受理态，业务结论稍后另发。
    updates = harness.ws.of_kind("update_card")
    assert len(updates) == 1
    assert updates[0]["card"]["task_id"] == "c_1"
    assert len(harness.sink.accepted) == 1
    assert harness.sink.accepted[0][1].topic_id == "t_1"


async def test_repeated_card_click_executes_once(harness):
    """同一按钮点两次：外部写操作执行 1 次，受理更新最多 1 次（数字走查 #2）。"""
    harness.cards.seed("c_1")
    frame = event_frame(
        "template_card_event", {"task_id": "c_1", "event_key": "useful"}
    )

    await harness.ws.emit("event", frame)
    await harness.ws.emit("event", frame)

    assert len(harness.ws.of_kind("update_card")) == 1
    assert len(harness.sink.accepted) == 1


# ===== 审批卡片决策 =====


def _seed_approval(harness, task_id):
    from uuid import uuid4

    approval_id = uuid4()
    harness.cards.seed(
        str(approval_id),
        allowed_actions=("confirm", "cancel"),
        payload={
            "kind": "approval",
            "approval_id": str(approval_id),
            "task_id": str(task_id),
            "title": "审批请求：创建飞书文档",
        },
    )
    return approval_id


async def test_approval_confirm_approves_and_requeues(harness):
    """点确认：受理回显 + APPROVED 落库 + 挂起任务重入队，不派发新任务。"""
    from uuid import uuid4

    task_id = uuid4()
    approval_id = _seed_approval(harness, task_id)

    await harness.ws.emit(
        "event",
        event_frame(
            "template_card_event",
            {"task_id": str(approval_id), "event_key": "confirm"},
        ),
    )

    updates = harness.ws.of_kind("update_card")
    assert len(updates) == 1
    assert updates[0]["card"]["main_title"]["desc"] == "已确认，执行中（完成后另行通知）"
    assert harness.approvals.decisions == [("approve", approval_id, "user_123")]
    assert harness.tasks.requeued[0][0] == task_id
    assert harness.tasks.requeued[0][3] == {
        "resumed_from_approval": True,
        "approval_title": "审批请求：创建飞书文档",
    }
    assert harness.queue.pushed and harness.queue.pushed[0]["id"] == task_id  # 运行期直接入队
    assert harness.sink.accepted == []  # 审批卡片不派发新任务


async def test_approval_cancel_rejects_and_kills_task(harness):
    """点取消：REJECTED 落库 + 任务终止。"""
    from uuid import uuid4

    task_id = uuid4()
    approval_id = _seed_approval(harness, task_id)

    await harness.ws.emit(
        "event",
        event_frame(
            "template_card_event",
            {"task_id": str(approval_id), "event_key": "cancel"},
        ),
    )

    updates = harness.ws.of_kind("update_card")
    assert updates[0]["card"]["main_title"]["desc"] == "已取消"
    assert harness.approvals.decisions == [("reject", approval_id, "user_123")]
    assert harness.tasks.dead[0][0] == task_id
    assert harness.tasks.requeued == []


async def test_approval_already_decided_does_not_requeue(harness):
    """审批已决/已过期（repo 返回 None）：有回显，但不重入队也不终止。"""
    from uuid import uuid4

    task_id = uuid4()
    approval_id = _seed_approval(harness, task_id)
    harness.approvals.next = None

    await harness.ws.emit(
        "event",
        event_frame(
            "template_card_event",
            {"task_id": str(approval_id), "event_key": "confirm"},
        ),
    )

    assert len(harness.ws.of_kind("update_card")) == 1
    assert harness.tasks.requeued == []


# ===== 交互卡片回调 =====


async def test_card_update_failure_does_not_drop_the_task(harness, monkeypatch):
    """回显失败不连坐业务任务。

    M0 实测：受理态卡片被企微以 42045 拒收后，异常冒到 _guard 把整次点击吞了——
    卡片已被 CAS 认领，重复点击又会判重，这次点击既没回显也永远不会执行。
    """
    harness.cards.seed("c_1")

    async def boom(*args, **kwargs):
        raise RuntimeError("Reply ack error: errcode=42045")

    monkeypatch.setattr(harness.ws, "update_template_card", boom)

    await harness.ws.emit(
        "event",
        event_frame("template_card_event", {"task_id": "c_1", "event_key": "useful"}),
    )

    assert len(harness.sink.accepted) == 1


async def test_card_click_with_undeclared_key_is_refused(harness):
    harness.cards.seed("c_1", allowed_actions=("useful",))

    await harness.ws.emit(
        "event",
        event_frame("template_card_event", {"task_id": "c_1", "event_key": "drop_db"}),
    )

    assert harness.ws.of_kind("update_card") == []
    assert harness.sink.accepted == []


async def test_card_click_from_another_chat_is_refused(harness):
    """卡片绑定了会话，不接受跨群回放。"""
    harness.cards.seed("c_1", chat_id="chat_other")

    await harness.ws.emit(
        "event",
        event_frame("template_card_event", {"task_id": "c_1", "event_key": "useful"}),
    )

    assert harness.sink.accepted == []


async def test_card_click_by_unauthorised_actor_is_refused(harness):
    harness.cards.seed("c_1", allowed_actors=("user_approver",))

    await harness.ws.emit(
        "event",
        event_frame("template_card_event", {"task_id": "c_1", "event_key": "useful"}),
    )

    assert harness.sink.accepted == []


async def test_unknown_card_task_is_refused(harness):
    await harness.ws.emit(
        "event",
        event_frame("template_card_event", {"task_id": "ghost", "event_key": "useful"}),
    )

    assert harness.sink.accepted == []


# ===== 反馈 =====


async def test_feedback_is_recorded_and_linked_to_topic(harness):
    harness.cards.seed("fb_1", topic_id="t_42")

    await harness.ws.emit(
        "event",
        event_frame(
            "feedback_event",
            {"feedback_id": "fb_1", "result": "dislike", "reason": ["结论不对"]},
        ),
    )

    assert len(harness.feedbacks.records) == 1
    assert harness.feedbacks.records[0]["topic_id"] == "t_42"
    assert harness.feedbacks.records[0]["reason"] == ["结论不对"]


async def test_duplicate_feedback_is_idempotent(harness):
    frame = event_frame(
        "feedback_event", {"feedback_id": "fb_1", "result": "like"}
    )

    await harness.ws.emit("event", frame)
    await harness.ws.emit("event", frame)

    assert len(harness.feedbacks.records) == 1


# ===== 连接 =====


async def test_disconnected_event_stops_reconnecting(harness):
    """被新连接踢掉后必须停止重连，否则两个实例会互相踢。"""
    harness.ws.connected = True

    await harness.ws.emit("event", event_frame("disconnected_event"))

    assert harness.client.evicted
    assert harness.ws.connected is False


async def test_enter_chat_without_handler_stays_silent(harness):
    """欢迎语本期延后：没有内容可发时保持沉默，不发空话。"""
    await harness.ws.emit("event", event_frame("enter_chat", chattype="single"))

    assert harness.ws.sent == []


async def test_malformed_frame_does_not_break_the_loop(harness):
    await harness.ws.emit("message", {"headers": {}, "body": {}})

    assert harness.sink.accepted == []
    # 后续正常帧仍能处理。
    await harness.ws.emit("message", message_frame())
    assert len(harness.sink.accepted) == 1
