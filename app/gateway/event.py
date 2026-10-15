"""AgentEvent 模型（Channel 内部形态，出 Channel 即转 Task）。

Channel 把企微协议帧归一化为 AgentEvent 后交 Runtime 落 ``task`` 表，此后
全链路不再感知企微原始报文。归属线索出 Channel 时收敛成契约里的
``InboundRef``——Space 与 Runtime 都不认识本文件的类型。

标识统一为 ``chat_id`` / ``topic_id``，不出现 ``session``（§4.1）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Dict, List, Optional

from app.contracts.ids import ChatId, ChatType
from app.contracts.ports import InboundRef
from app.contracts.space_context import SpaceContext


class EventKind(str, Enum):
    MESSAGE = "MESSAGE"
    CARD_ACTION = "CARD_ACTION"
    FEEDBACK = "FEEDBACK"
    CONNECTION = "CONNECTION"


@dataclass(frozen=True)
class Conversation:
    type: ChatType
    id: ChatId
    """群聊为 chatid，单聊为对方 userid。两者都是 aibot_send_msg 的投递地址。"""


@dataclass(frozen=True)
class InboundMessage:
    text: str = ""
    msgtype: str = "text"
    quote_message_id: Optional[str] = None
    quote_content: Optional[str] = None
    media: Dict[str, Any] = field(default_factory=dict)
    """image / file / video 等非文本结构体原文，含 url 与 aeskey。"""


@dataclass(frozen=True)
class CardAction:
    task_id: str
    event_key: str
    selected_items: List[Dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class Feedback:
    response_id: str
    result: str
    """企微反馈取值，如 like / dislike。原样透传，不在 Channel 做业务解释。"""
    reason: List[str] = field(default_factory=list)
    comment: Optional[str] = None


@dataclass(frozen=True)
class AgentEvent:
    event_id: str
    """企微回调的 msgid。Runtime 用它作 ``task.idem_key`` 去重（裁决 #6）。"""
    event_type: EventKind
    bot_id: str
    conversation: Conversation
    actor_user_id: str
    occurred_at: int
    space_context: Optional[SpaceContext] = None
    message: Optional[InboundMessage] = None
    card_action: Optional[CardAction] = None
    feedback: Optional[Feedback] = None
    raw: Dict[str, Any] = field(default_factory=dict)
    """企微原始帧。仅供 Channel 内部回复定位与审计，业务层不得解析。"""

    def with_context(self, context: SpaceContext) -> "AgentEvent":
        return replace(self, space_context=context)

    def to_ref(self) -> InboundRef:
        """收敛成 Space 能消费的归属线索。"""
        return InboundRef(
            event_id=self.event_id,
            bot_id=self.bot_id,
            chat_id=self.conversation.id,
            chat_type=self.conversation.type,
            actor_user_id=self.actor_user_id,
            text=self.message.text if self.message else "",
            quote_message_id=(
                self.message.quote_message_id if self.message else None
            ),
            quote_content=(
                self.message.quote_content if self.message else None
            ),
        )

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "eventId": self.event_id,
            "eventType": self.event_type.value,
            "botId": self.bot_id,
            "conversation": {
                "type": self.conversation.type.value,
                "id": self.conversation.id,
            },
            "actorUserId": self.actor_user_id,
            "occurredAt": self.occurred_at,
            "spaceContext": (
                self.space_context.to_dict() if self.space_context else {}
            ),
        }
        if self.message:
            payload["message"] = {
                "text": self.message.text,
                "msgtype": self.message.msgtype,
                "quoteMessageId": self.message.quote_message_id,
                "quoteContent": self.message.quote_content,
                "media": self.message.media,
            }
        if self.card_action:
            payload["cardAction"] = {
                "taskId": self.card_action.task_id,
                "eventKey": self.card_action.event_key,
                "selectedItems": self.card_action.selected_items,
            }
        if self.feedback:
            payload["feedback"] = {
                "responseId": self.feedback.response_id,
                "result": self.feedback.result,
                "reason": self.feedback.reason,
                "comment": self.feedback.comment,
            }
        return payload
