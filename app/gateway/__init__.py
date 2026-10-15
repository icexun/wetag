"""Channel 模块：企微协议接入与消息可靠性。

只处理企微协议和消息可靠性：不判定意图、不选工具、不解释业务结果
（§3.1 纪律 1）。

拥有 ``card_task`` 与 ``feedback_record`` 两张表。出站队列归 Runtime
（裁决 #7），本模块只提供发送原语；去重归 ``task.idem_key``（裁决 #6），
本模块不建 ``event_inbox``。

它禁止 import ``space/`` 及其下游：归属解析与会话查询走契约里的
``SpaceContextProvider`` 与 ``ChatDirectory``。
"""

from app.gateway.actions import CardActionHandler
from app.gateway.cards import (
    CardBinding,
    CardClaimResult,
    CardItem,
    build_alert_card,
    build_confirm_card,
    build_processing_card,
    new_card_task_id,
)
from app.gateway.client import WecomClient
from app.gateway.event import AgentEvent, EventKind, InboundMessage
from app.gateway.feedback import FeedbackHandler
from app.gateway.inbound import (
    ForeignBotFrame,
    InboundDispatcher,
    NotAddressedError,
    normalize_frame,
)
from app.gateway.outbound import (
    ConversationNotActivated,
    MessageSender,
    OutboundRejected,
)
from app.gateway.reply_window import ReplyWindow
from app.gateway.repository import (
    CardStatus,
    CardTask,
    CardTaskRepository,
    FeedbackRepository,
)

__all__ = [
    "AgentEvent",
    "CardActionHandler",
    "CardBinding",
    "CardClaimResult",
    "CardItem",
    "CardStatus",
    "CardTask",
    "CardTaskRepository",
    "ConversationNotActivated",
    "EventKind",
    "FeedbackHandler",
    "FeedbackRepository",
    "ForeignBotFrame",
    "InboundDispatcher",
    "InboundMessage",
    "MessageSender",
    "NotAddressedError",
    "OutboundRejected",
    "ReplyWindow",
    "WecomClient",
    "build_alert_card",
    "build_confirm_card",
    "build_processing_card",
    "new_card_task_id",
    "normalize_frame",
]
