"""入站归一化。"""

from __future__ import annotations

import pytest

from app.contracts.ids import ChatType
from app.gateway.event import EventKind
from app.gateway.inbound import NotAddressedError, normalize_frame
from tests.conftest import event_frame, message_frame


def test_group_text_message():
    event = normalize_frame(message_frame(text="@WeTag 建个需求卡"))

    assert event.event_type is EventKind.MESSAGE
    assert event.conversation.type is ChatType.GROUP
    assert event.conversation.id == "chat_001"
    assert event.actor_user_id == "user_123"
    assert event.message.text == "@WeTag 建个需求卡"


def test_single_chat_uses_userid_as_address():
    """单聊没有 chatid，投递地址就是对方 userid。"""
    frame = message_frame(chattype="single", user_id="user_999")

    event = normalize_frame(frame)

    assert event.conversation.type is ChatType.SINGLE
    assert event.conversation.id == "user_999"


def test_quote_is_preserved_for_topic_routing():
    event = normalize_frame(message_frame(quote_msgid="msg_000"))

    assert event.message.quote_message_id == "msg_000"
    assert event.message.quote_content == "被引用的内容"


def test_quote_nested_shape_without_msgid():
    """长连接实际帧：quote 不带 msgid，内容嵌套在 quote.text.content。"""
    frame = message_frame()
    frame["body"]["quote"] = {
        "msgtype": "text",
        "text": {"content": "情况汇总如下：……"},
    }

    event = normalize_frame(frame)

    assert event.message.quote_message_id is None
    assert event.message.quote_content == "情况汇总如下：……"


def test_media_message_keeps_url_and_aeskey():
    """长连接下每个资源链接自带独立 aeskey，解密交业务层按需处理。"""
    frame = message_frame()
    frame["body"]["msgtype"] = "image"
    frame["body"].pop("text")
    frame["body"]["image"] = {"url": "https://example/x", "aeskey": "KEY"}

    event = normalize_frame(frame)

    assert event.message.msgtype == "image"
    assert event.message.media["aeskey"] == "KEY"


def test_mixed_message_flattens_text_items():
    frame = message_frame()
    frame["body"]["msgtype"] = "mixed"
    frame["body"].pop("text")
    frame["body"]["mixed"] = {
        "msg_item": [
            {"msgtype": "text", "text": {"content": "看这张图"}},
            {"msgtype": "image", "image": {"url": "https://example/x"}},
            {"msgtype": "text", "text": {"content": "有问题吗"}},
        ]
    }

    event = normalize_frame(frame)

    assert event.message.text == "看这张图有问题吗"


def test_card_event_nested_detail():
    frame = event_frame(
        "template_card_event",
        {
            "task_id": "c_1",
            "event_key": "useful",
            "selected_items": {"selected_item": [{"question_key": "q1"}]},
        },
    )

    event = normalize_frame(frame)

    assert event.event_type is EventKind.CARD_ACTION
    assert event.card_action.task_id == "c_1"
    assert event.card_action.event_key == "useful"
    assert event.card_action.selected_items == [{"question_key": "q1"}]


def test_card_event_flat_detail():
    """企微未逐字段定义嵌套形态，平铺写法也必须能解析。"""
    frame = event_frame("template_card_event")
    frame["body"]["event"].update({"task_id": "c_2", "event_key": "false_alarm"})

    event = normalize_frame(frame)

    assert event.card_action.task_id == "c_2"
    assert event.card_action.event_key == "false_alarm"


def test_feedback_event():
    frame = event_frame(
        "feedback_event",
        {"feedback_id": "fb_1", "result": "dislike", "reason": ["结论不对"]},
    )

    event = normalize_frame(frame)

    assert event.event_type is EventKind.FEEDBACK
    assert event.feedback.response_id == "fb_1"
    assert event.feedback.result == "dislike"
    assert event.feedback.reason == ["结论不对"]


def test_enter_chat_is_connection_kind():
    event = normalize_frame(event_frame("enter_chat"))

    assert event.event_type is EventKind.CONNECTION


def test_frame_without_address_is_rejected():
    frame = message_frame(chattype="single", user_id="")
    frame["body"]["from"] = {}

    with pytest.raises(NotAddressedError):
        normalize_frame(frame)


def test_event_carries_ref_for_space():
    """出 Channel 的归属线索只含契约字段，Space 不接触 AgentEvent。"""
    event = normalize_frame(message_frame(text="TAPD-1001", quote_msgid="msg_000"))

    ref = event.to_ref()

    assert ref.chat_id == "chat_001"
    assert ref.chat_type is ChatType.GROUP
    assert ref.text == "TAPD-1001"
    assert ref.quote_message_id == "msg_000"
