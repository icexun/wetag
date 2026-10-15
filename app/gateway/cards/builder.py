"""模板卡片构造。

对应企微「模板卡片类型」（101032）。本模块只做结构拼装，标题与条目文案由
业务层给出——Channel 不解释业务结果。

一次改动影响多个需求时，群内只应收到一张多条目卡片而不是连发多张
（PRD E-13）。多条目统一渲染在 ``horizontal_content_list`` 中。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

# 企微对这两个列表都有长度上限，超出会整条消息发送失败。
HORIZONTAL_CONTENT_LIMIT = 6
BUTTON_LIMIT = 6
TASK_ID_MAX_BYTES = 128


class ButtonStyle:
    BLUE = 1
    RED = 2
    BLACK = 3
    GREY = 4


@dataclass(frozen=True)
class CardItem:
    """卡片上的一个条目，例如一条变更或一项待办。"""

    label: str
    value: str
    url: Optional[str] = None


@dataclass(frozen=True)
class CardButton:
    key: str
    text: str
    style: int = ButtonStyle.BLUE


def build_alert_card(
    task_id: str,
    title: str,
    desc: str,
    items: Sequence[CardItem] = (),
    buttons: Sequence[CardButton] = (),
    feedback_id: Optional[str] = None,
    source_desc: Optional[str] = None,
    sub_title: Optional[str] = None,
) -> Dict[str, Any]:
    """按钮交互卡片，用于需要用户确认的通知（如审批）。

    ``sub_title`` 走 ``sub_title_text``：模板卡片里**唯一能多行折行**的文本区
    （``main_title.desc`` 与条目 value 都会单行截断成省略号）。长正文
    （如描述全文）放这里，``desc`` 只留一句话摘要。
    """
    require_valid_task_id(task_id)

    card: Dict[str, Any] = {
        "card_type": "button_interaction",
        "task_id": task_id,
        "main_title": {"title": title, "desc": desc},
    }
    if source_desc:
        card["source"] = {"desc": source_desc, "desc_color": 0}

    overflow = len(items) - HORIZONTAL_CONTENT_LIMIT
    sub_parts = [sub_title] if sub_title else []
    if overflow > 0:
        sub_parts.append(f"共 {len(items)} 项，以下展示前 {HORIZONTAL_CONTENT_LIMIT} 项")
    if sub_parts:
        card["sub_title_text"] = "\n".join(sub_parts)

    content = _horizontal_content(items)
    if content:
        card["horizontal_content_list"] = content

    if buttons:
        card["button_list"] = [
            {"text": b.text, "style": b.style, "key": b.key}
            for b in buttons[:BUTTON_LIMIT]
        ]
    if feedback_id:
        card["feedback"] = {"id": feedback_id}
    return card


def build_processing_card(
    task_id: str,
    title: str,
    acted_key: str,
    acted_text: str = "已确认，处理中",
    items: Sequence[CardItem] = (),
) -> Dict[str, Any]:
    """卡片点击后的受理态。

    这是**交互受理结果**，不是业务最终结果。慢操作完成后另发结果消息，
    不把受理态直接改写成结论，否则用户无法区分「已收到」与「已做完」。

    仍用 ``button_interaction``：``text_notice`` 的 ``card_action`` 是必填项
    （101032），而受理态没有可跳转的目标，硬凑一个 url 是假信息。长连接文档
    更新卡片的示例走的也是改按钮文案这条路。

    按钮的 key 沿用被点击的那个：用户重复点击时仍落在 ``allowed_actions``
    内，由 ``card_task`` 的 CAS 判为重复而非越权，两者的处置并不相同。
    """
    require_valid_task_id(task_id)

    card: Dict[str, Any] = {
        "card_type": "button_interaction",
        "task_id": task_id,
        "main_title": {"title": title, "desc": acted_text},
        "button_list": [
            {"text": acted_text, "style": ButtonStyle.GREY, "key": acted_key}
        ],
    }
    content = _horizontal_content(items)
    if content:
        card["horizontal_content_list"] = content
    return card


def build_confirm_card(
    task_id: str,
    title: str,
    desc: str,
    items: Sequence[CardItem] = (),
    confirm_key: str = "confirm",
    cancel_key: str = "cancel",
    feedback_id: Optional[str] = None,
) -> Dict[str, Any]:
    """高危写操作的群内审批卡片。

    今日审批降级为 Auto Mode 全通（§10.3），补做时调用方不变。
    """
    return build_alert_card(
        task_id=task_id,
        title=title,
        desc=desc,
        items=items,
        buttons=(
            CardButton(key=confirm_key, text="确认执行", style=ButtonStyle.BLUE),
            CardButton(key=cancel_key, text="取消", style=ButtonStyle.GREY),
        ),
        feedback_id=feedback_id,
    )


def build_markdown(content: str, feedback_id: Optional[str] = None) -> Dict[str, Any]:
    """markdown 消息体。内容上限 20480 字节，超出由调用方负责裁剪。"""
    markdown: Dict[str, Any] = {"content": content}
    if feedback_id:
        markdown["feedback"] = {"id": feedback_id}
    return {"msgtype": "markdown", "markdown": markdown}


def require_valid_task_id(task_id: str) -> None:
    """``task_id`` 在同一 Bot 下唯一，最长 128 字节。

    超限时企微直接拒收，卡片不会出现在群里——属于必须在发送前拦下的错误。
    """
    if not task_id:
        raise ValueError("card task_id must not be empty")
    if len(task_id.encode("utf-8")) > TASK_ID_MAX_BYTES:
        raise ValueError(
            f"card task_id exceeds {TASK_ID_MAX_BYTES} bytes: {task_id[:32]}..."
        )


def _horizontal_content(items: Sequence[CardItem]) -> List[Dict[str, Any]]:
    content: List[Dict[str, Any]] = []
    for item in items[:HORIZONTAL_CONTENT_LIMIT]:
        entry: Dict[str, Any] = {"keyname": item.label, "value": item.value}
        if item.url:
            # type=1 表示 value 可点击跳转 url。
            entry["type"] = 1
            entry["url"] = item.url
        content.append(entry)
    return content
