"""出站发送原语：归属校验、会话窗与降级。"""

from __future__ import annotations

import pytest

from app.contracts.ids import ChatType
from app.gateway.cards.builder import build_alert_card
from app.gateway.outbound import ConversationNotActivated, OutboundRejected
from tests.conftest import message_frame
from tests.gateway.conftest import activate

# ===== 主动推送 =====


async def test_push_to_activated_group(sender, ws_client, space_repo):
    activate(space_repo)

    result = await sender.push_markdown("chat_001", "**变更通知**")

    assert result.ok
    pushed = ws_client.of_kind("push")[0]
    assert pushed["chat_id"] == "chat_001"
    assert pushed["body"]["chat_type"] == 2
    assert pushed["body"]["markdown"]["content"] == "**变更通知**"


async def test_push_to_single_chat_uses_chat_type_one(sender, ws_client, space_repo):
    """兼容模式会优先按群聊解析，单聊必须显式给 1。"""
    activate(space_repo, chat_id="user_777", chat_type=ChatType.SINGLE)

    await sender.push_markdown("user_777", "hi")

    assert ws_client.of_kind("push")[0]["body"]["chat_type"] == 1


async def test_push_to_never_activated_chat_is_refused(sender):
    """企微要求会话先有过入站消息，机器人才能主动推送。"""
    with pytest.raises(ConversationNotActivated):
        await sender.push_markdown("chat_unknown", "hi")


async def test_push_to_foreign_workspace_is_refused(sender, space_repo):
    """跨业务线发送显式拒绝，不寻找替代目标。"""
    activate(space_repo, workspace_id="beta")

    with pytest.raises(OutboundRejected):
        await sender.push_markdown("chat_001", "hi")


async def test_push_to_foreign_bot_is_refused(sender, space_repo):
    activate(space_repo, bot_id="bot_beta")

    with pytest.raises(OutboundRejected):
        await sender.push_markdown("chat_001", "hi")


async def test_push_outside_session_window_is_refused(sender, space_repo):
    """跨 24 小时推送能否成立待 M0 实测，开关默认开启。"""
    activate(space_repo, last_inbound_hours_ago=25)

    with pytest.raises(ConversationNotActivated):
        await sender.push_markdown("chat_001", "hi")


async def test_push_card_carries_template_card(sender, ws_client, space_repo):
    activate(space_repo)
    card = build_alert_card("c_1", "冲突预警", "发现 2 处冲突")

    await sender.push_card("chat_001", card)

    body = ws_client.of_kind("push")[0]["body"]
    assert body["msgtype"] == "template_card"
    assert body["template_card"]["task_id"] == "c_1"


async def test_send_outbox_builds_approval_card(sender, ws_client, space_repo, card_repo):
    """审批 Envelope 只带语义字段（card_type/tool_name/tool_input）：
    发送侧必须构造出合法卡片，否则空卡片被企微拒收（41016 missing title）。

    同时登记 card_task 安全绑定——没有它按钮点击被判 NOT_FOUND，用户点了没反应。
    """
    from types import SimpleNamespace
    from uuid import uuid4

    activate(space_repo)
    msg = SimpleNamespace(
        id=uuid4(),
        chat_id="chat_001",
        msg_type="template_card",
        task_id=uuid4(),
        payload={
            "card_type": "approval",
            "approval_id": "appr-1",
            "tool_name": "tapd_bug_create",
            "tool_input": {"title": "冲突修复", "workspace_id": 1000001},
        },
    )

    await sender.send_outbox(msg)

    card = ws_client.of_kind("push")[0]["body"]["template_card"]
    # 语义化标题与摘要：审批人看动作与对象，不看函数名与裸 JSON
    assert card["main_title"]["title"] == "审批请求：创建 TAPD 缺陷"
    assert card["main_title"]["desc"] == "创建 TAPD 缺陷《冲突修复》"
    assert card["task_id"] == "appr-1"  # 按钮回调按它定位审批记录
    assert card["button_list"]  # 确认/取消按钮
    # 关键参数以条目展示
    labels = [i["keyname"] for i in card["horizontal_content_list"]]
    assert "标题" in labels

    binding = card_repo.cards.get("appr-1")
    assert binding is not None
    assert set(binding.allowed_actions) == {"confirm", "cancel"}
    assert binding.payload["kind"] == "approval"
    assert binding.payload["task_id"] == str(msg.task_id)


async def test_send_outbox_builds_notice_card(sender, ws_client, space_repo, card_repo):
    """降级/升级等语义卡片只有 text：发送前必须补上 main_title，否则企微 41016。"""
    from types import SimpleNamespace
    from uuid import uuid4

    activate(space_repo)
    msg = SimpleNamespace(
        id=uuid4(),
        chat_id="chat_001",
        msg_type="template_card",
        task_id=uuid4(),
        topic_id=None,
        payload={
            "card_type": "capability_unavailable",
            "capability": "企微待办",
            "text": "企微待办能力暂不可用，恢复后将自动继续跟进",
            "reason": "down",
        },
    )

    await sender.send_outbox(msg)

    card = ws_client.of_kind("push")[0]["body"]["template_card"]
    assert card["main_title"]["title"] == "企微待办暂不可用"
    assert "暂不可用" in card["main_title"]["desc"]
    assert card["button_list"] == [{"text": "知道了", "style": 4, "key": "ack"}]
    binding = card_repo.cards[card["task_id"]]
    assert binding.payload["kind"] == "notice"


async def test_send_outbox_rejects_card_without_title(sender, space_repo):
    """拼不出标题就本地拒绝，不把空卡片打到企微。"""
    from types import SimpleNamespace
    from uuid import uuid4

    activate(space_repo)
    msg = SimpleNamespace(
        id=uuid4(),
        chat_id="chat_001",
        msg_type="template_card",
        task_id=None,
        payload={"card_type": "unknown_notice"},
    )

    with pytest.raises(OutboundRejected, match="main_title"):
        await sender.send_outbox(msg)


async def test_approval_card_shows_names_not_ids(
    sender, ws_client, space_repo, card_repo, monkeypatch
):
    """审批卡片展示名称而非 ID：项目 ID 经 workspace 配置映射，story_id 经标题解析器。"""
    from types import SimpleNamespace
    from uuid import uuid4

    monkeypatch.setenv("TAPD_PROJECT_ID", "1000002")
    monkeypatch.setenv("TAPD_PROJECT_NAME", "示例B（beta）")

    async def resolver(story_id: str) -> str:
        assert story_id == "1020999559001038931"
        return "周年促销挽留弹窗规则调整"

    sender._story_title_resolver = resolver
    activate(space_repo)
    msg = SimpleNamespace(
        id=uuid4(),
        chat_id="chat_001",
        msg_type="template_card",
        task_id=uuid4(),
        payload={
            "card_type": "approval",
            "approval_id": "appr-2",
            "tool_name": "tapd_story_update",
            "tool_input": {
                "story_id": "1020999559001038931",
                "workspace_id": 1000002,
            },
        },
    )

    await sender.send_outbox(msg)

    card = ws_client.of_kind("push")[0]["body"]["template_card"]
    # desc 用解析出的需求标题，不是裸 ID
    assert card["main_title"]["desc"] == "更新 TAPD 需求《周年促销挽留弹窗规则调整》"
    items = {i["keyname"]: i["value"] for i in card["horizontal_content_list"]}
    assert items["需求"] == "周年促销挽留弹窗规则调整"
    assert items["项目"] == "示例B（beta）"


async def test_approval_card_falls_back_to_ids_when_resolve_fails(
    sender, ws_client, space_repo, card_repo
):
    """标题解析失败回退显示原始 ID：易读性增强不能拖垮发卡。"""

    async def broken(story_id: str):
        raise RuntimeError("台账不可用")

    from types import SimpleNamespace
    from uuid import uuid4

    sender._story_title_resolver = broken
    activate(space_repo)
    msg = SimpleNamespace(
        id=uuid4(),
        chat_id="chat_001",
        msg_type="template_card",
        task_id=uuid4(),
        payload={
            "card_type": "approval",
            "approval_id": "appr-3",
            "tool_name": "tapd_story_update",
            "tool_input": {"story_id": "1020999559001038931", "workspace_id": 1},
        },
    )

    await sender.send_outbox(msg)

    card = ws_client.of_kind("push")[0]["body"]["template_card"]
    items = {i["keyname"]: i["value"] for i in card["horizontal_content_list"]}
    assert items["需求"] == "1020999559001038931"  # 回退原样
    assert items["项目"] == "1"  # 未登记的项目 ID 原样显示


# ===== 回复 =====


async def test_stream_reply_reuses_stream_id(sender, ws_client, window):
    """同一条流式消息的多次刷新必须复用 stream.id，否则会刷出多条消息。"""
    frame = message_frame(msgid="msg_1")
    window.register("msg_1", frame)

    await sender.reply_stream("msg_1", "正在查询…")
    await sender.reply_stream("msg_1", "正在查询…已找到 2 处")
    await sender.reply_stream("msg_1", "共 2 处冲突", finish=True)

    streams = ws_client.of_kind("stream")
    assert len({s["stream_id"] for s in streams}) == 1
    assert streams[-1]["finish"] is True
    # finish 后窗口释放，帧不再可用。
    assert window.frame_of("msg_1") is None


async def test_stream_reply_feedback_only_on_first_frame(sender, ws_client, window):
    """SDK 约定 feedback 仅首帧有效：续帧带 feedback 企微不应用该帧更新，
    终态卡片停在受理文案（M0 实测）。"""
    frame = message_frame(msgid="msg_1")
    window.register("msg_1", frame)

    await sender.reply_stream("msg_1", "已受理，正在处理", feedback_id="fb_1")
    await sender.reply_stream("msg_1", "最终结果", finish=True, feedback_id="fb_1")

    streams = ws_client.of_kind("stream")
    assert streams[0]["feedback"] == {"id": "fb_1"}
    assert streams[1]["feedback"] is None  # 续帧不再带 feedback


async def test_expired_reply_window_degrades_to_push(sender, ws_client, space_repo):
    """流式窗口过期不能把结论丢掉，改为主动推送送达。"""
    activate(space_repo)

    result = await sender.reply_stream(
        "msg_gone", "任务完成", finish=True, chat_id="chat_001"
    )

    assert result.ok
    assert result.degraded_to_push
    assert len(ws_client.of_kind("push")) == 1


async def test_expired_window_without_fallback_address_raises(sender):
    with pytest.raises(ConversationNotActivated):
        await sender.reply_stream("msg_gone", "任务完成", finish=True)


async def test_update_card_requires_live_window(sender):
    """卡片必须在 5 秒内更新，窗口过了就更新不了。"""
    with pytest.raises(ConversationNotActivated):
        await sender.update_card("evt_gone", {"card_type": "text_notice"})


async def test_wecom_error_code_surfaces(sender, ws_client, window, monkeypatch):
    window.register("msg_1", message_frame(msgid="msg_1"))

    async def failing(*args, **kwargs):
        return {"errcode": 45009, "errmsg": "api freq out of limit"}

    monkeypatch.setattr(ws_client, "reply_stream", failing)
    result = await sender.reply_stream("msg_1", "hi")

    assert not result.ok
    assert result.errcode == 45009


def test_project_name_maps_only_current_workspace_project(monkeypatch):
    from app.gateway.outbound import _project_name

    monkeypatch.setenv("TAPD_PROJECT_ID", "1000001")
    monkeypatch.setenv("TAPD_PROJECT_NAME", "演示项目")

    assert _project_name(1000001) == "演示项目"
    assert _project_name("2000002") is None
