"""模板卡片渲染。"""

from __future__ import annotations

import pytest

from app.gateway.cards.builder import (
    HORIZONTAL_CONTENT_LIMIT,
    ButtonStyle,
    CardButton,
    CardItem,
    build_alert_card,
    build_confirm_card,
    build_markdown,
    build_processing_card,
    require_valid_task_id,
)


def test_alert_card_renders_items_and_buttons():
    card = build_alert_card(
        task_id="c_1",
        title="变更通知",
        desc="登录改造与支付需求存在冲突",
        items=[
            CardItem("TAPD-1001", "手机号校验规则不一致", "https://tapd/1001"),
            CardItem("代码维度", "auth/login.py:42"),
        ],
        buttons=[
            CardButton("useful", "有用"),
            CardButton("false_alarm", "误报", ButtonStyle.RED),
        ],
        feedback_id="fb_1",
    )

    assert card["card_type"] == "button_interaction"
    assert card["main_title"]["title"] == "变更通知"
    assert len(card["horizontal_content_list"]) == 2
    # 带 url 的条目可点击跳转。
    assert card["horizontal_content_list"][0]["type"] == 1
    assert "type" not in card["horizontal_content_list"][1]
    assert [b["key"] for b in card["button_list"]] == ["useful", "false_alarm"]
    assert card["feedback"]["id"] == "fb_1"


def test_many_items_collapse_into_one_card():
    """一次改动影响多个需求时只发一张卡片，超出上限的条目折叠提示（E-13）。"""
    items = [CardItem(f"TAPD-{i}", f"变更 {i}") for i in range(10)]

    card = build_alert_card("c_1", "变更通知", "", items=items)

    assert len(card["horizontal_content_list"]) == HORIZONTAL_CONTENT_LIMIT
    assert "共 10 项" in card["sub_title_text"]


def test_processing_card_states_acceptance_not_result():
    """受理态说的是「已确认」，不能冒充业务结论。"""
    card = build_processing_card("c_1", "变更通知", acted_key="useful")

    assert card["main_title"]["desc"] == "已确认，处理中"
    assert card["task_id"] == "c_1"


def test_processing_card_stays_button_interaction():
    """text_notice 的 card_action 必填，受理态没有可跳转目标。

    M0 实测：更新成不带 card_action 的 text_notice 会被企微以 42045
    Template_Card card_action Missing or Invalid 拒收。
    """
    card = build_processing_card("c_1", "变更通知", acted_key="useful")

    assert card["card_type"] == "button_interaction"
    assert "card_action" not in card
    # button_list 是 button_interaction 的必填项，且 key 沿用被点击的那个，
    # 重复点击才会落在 allowed_actions 内被判为重复而非越权。
    assert card["button_list"] == [
        {"text": "已确认，处理中", "style": ButtonStyle.GREY, "key": "useful"}
    ]


def test_confirm_card_has_confirm_and_cancel():
    card = build_confirm_card("c_1", "确认写入飞书文档", "将更新 3 处章节")

    keys = [b["key"] for b in card["button_list"]]
    assert keys == ["confirm", "cancel"]


def test_markdown_body():
    body = build_markdown("# 标题", feedback_id="fb_1")

    assert body["msgtype"] == "markdown"
    assert body["markdown"]["feedback"]["id"] == "fb_1"


def test_oversized_task_id_is_rejected_before_send():
    """超过 128 字节企微直接拒收，必须在发送前拦下。"""
    with pytest.raises(ValueError):
        require_valid_task_id("c_" * 100)

    with pytest.raises(ValueError):
        require_valid_task_id("")
