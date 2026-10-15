"""入站：aibotid 校验 → msgid 排重 → 转 AgentEvent。

归一化只做结构转换与字段提取，不判定意图、不解释内容——这是 Channel
「不懂业务」的边界所在（§3.1 纪律 1）。

企微对模板卡片事件与反馈事件的 body 嵌套形态在长连接文档中未逐字段给出，
因此这里按「子对象优先、平铺兜底」两种形态解析，并始终把原始帧保留在
``AgentEvent.raw`` 中，便于实测后校准而不丢信息。

排重不在本模块自建表：``task.idem_key`` 的唯一索引就是唯一去重点（裁决 #6），
``TaskSink.submit`` 返回 DUPLICATE 即为命中。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.contracts.errors import ChatOwnershipConflict, IsolationViolation
from app.contracts.ids import ChatType
from app.contracts.ports import SpaceContextProvider
from app.contracts.task import SubmitResult, TaskSink
from app.gateway.actions import CardActionHandler
from app.gateway.event import (
    AgentEvent,
    CardAction,
    Conversation,
    EventKind,
    Feedback,
    InboundMessage,
)
from app.gateway.feedback import FeedbackHandler
from app.gateway.outbound import MessageSender
from app.gateway.quote import extract_quote
from app.gateway.reply_window import (
    EVENT_REPLY_TTL_SECONDS,
    MESSAGE_REPLY_TTL_SECONDS,
    ReplyWindow,
)
from config.properties import WecomProperties

try:
    from agentrun.utils.log import logger
except ImportError:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

WelcomeHandler = Callable[[AgentEvent], Awaitable[Optional[Dict[str, Any]]]]


class NotAddressedError(ValueError):
    """无法从帧中解析出会话地址，属协议异常而非业务异常。"""


class ForeignBotFrame(IsolationViolation):
    """回调的 aibotid 不是本进程的 Bot。

    长连接由持有凭据的进程自建，理论上不会收到他人回调。真发生了就是隔离
    出了问题，按安全事件记录，不静默丢弃。
    """


# ===== 协议帧 → AgentEvent =====


def normalize_frame(frame: Dict[str, Any]) -> AgentEvent:
    body = frame.get("body") or {}
    bot_id = body.get("aibotid", "")
    chat_type = chat_type_of(body.get("chattype"))
    actor_user_id = (body.get("from") or {}).get("userid", "")

    # 群聊用 chatid，单聊用对方 userid —— 两者都是 aibot_send_msg 的投递地址。
    chat_id = body.get("chatid") or actor_user_id
    if not chat_id:
        raise NotAddressedError("frame has neither chatid nor from.userid")

    conversation = Conversation(type=chat_type, id=chat_id)
    occurred_at = int(body.get("create_time") or time.time()) * 1000
    common = {
        "event_id": body.get("msgid", ""),
        "bot_id": bot_id,
        "conversation": conversation,
        "actor_user_id": actor_user_id,
        "occurred_at": occurred_at,
        "raw": frame,
    }

    if body.get("msgtype") == "event":
        return _normalize_event(body, common)
    return _normalize_message(body, common)


def chat_type_of(chattype: Optional[str]) -> ChatType:
    return ChatType.GROUP if chattype == "group" else ChatType.SINGLE


def event_type_of(frame: Dict[str, Any]) -> Optional[str]:
    """读取事件帧的 eventtype，不做归一化。"""
    body = frame.get("body") or {}
    return (body.get("event") or {}).get("eventtype")


def _normalize_message(body: Dict[str, Any], common: Dict[str, Any]) -> AgentEvent:
    msgtype = body.get("msgtype", "text")
    text = ""
    media: Dict[str, Any] = {}

    if msgtype == "text":
        text = (body.get("text") or {}).get("content", "")
    elif msgtype == "mixed":
        text = _flatten_mixed(body.get("mixed") or {})
        media = body.get("mixed") or {}
    elif msgtype == "voice":
        # 语音由企微转写为文本后下发，业务侧与文本消息同构。
        voice = body.get("voice") or {}
        text = voice.get("content", "")
        media = voice
    elif msgtype in ("image", "file", "video"):
        # url 5 分钟内有效，aeskey 每个链接唯一，下载解密交业务层按需处理。
        media = body.get(msgtype) or {}

    quote = extract_quote(body)
    return AgentEvent(
        event_type=EventKind.MESSAGE,
        message=InboundMessage(
            text=text,
            msgtype=msgtype,
            quote_message_id=quote.message_id,
            quote_content=quote.content,
            media=media,
        ),
        **common,
    )


def _normalize_event(body: Dict[str, Any], common: Dict[str, Any]) -> AgentEvent:
    event = body.get("event") or {}
    event_type = event.get("eventtype", "")

    if event_type == "template_card_event":
        detail = event.get("template_card_event") or event
        return AgentEvent(
            event_type=EventKind.CARD_ACTION,
            card_action=CardAction(
                task_id=detail.get("task_id", ""),
                event_key=detail.get("event_key", ""),
                selected_items=_selected_items(detail),
            ),
            **common,
        )

    if event_type == "feedback_event":
        detail = event.get("feedback_event") or event
        return AgentEvent(
            event_type=EventKind.FEEDBACK,
            feedback=Feedback(
                response_id=(
                    detail.get("feedback_id")
                    or detail.get("id")
                    or (detail.get("feedback") or {}).get("id", "")
                ),
                result=str(detail.get("result") or detail.get("type") or ""),
                reason=_as_list(detail.get("reason")),
                comment=detail.get("comment"),
            ),
            **common,
        )

    # enter_chat 与 disconnected_event 都归为连接/会话类事件，
    # 不产生业务任务。
    return AgentEvent(
        event_type=EventKind.CONNECTION,
        message=InboundMessage(text="", msgtype=event_type),
        **common,
    )


def _flatten_mixed(mixed: Dict[str, Any]) -> str:
    parts: List[str] = []
    for item in mixed.get("msg_item") or []:
        if not isinstance(item, dict):
            continue
        if item.get("msgtype") == "text":
            parts.append((item.get("text") or {}).get("content", ""))
    return "".join(parts)


def _selected_items(detail: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = detail.get("selected_items")
    if isinstance(raw, dict):
        raw = raw.get("selected_item")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


# ===== 入站编排 =====


class InboundDispatcher:
    """一帧回调的完整入站路径：准入 → 归属 → 登记回复窗口 → 投递任务。"""

    def __init__(
        self,
        bot_id: str,
        space: SpaceContextProvider,
        task_sink: TaskSink,
        sender: MessageSender,
        card_actions: CardActionHandler,
        feedbacks: FeedbackHandler,
        reply_window: Optional[ReplyWindow] = None,
        welcome_handler: Optional[WelcomeHandler] = None,
    ):
        self._bot_id = bot_id
        self._space = space
        self._sink = task_sink
        self._sender = sender
        self._card_actions = card_actions
        self._feedbacks = feedbacks
        # 空的 ReplyWindow 布尔值为假（它实现了 __len__），不能用 `or` 兜底，
        # 否则调用方传进来的窗口会被悄悄换成另一个实例。
        self._window = reply_window if reply_window is not None else ReplyWindow()
        self._welcome_handler = welcome_handler
        self._props = WecomProperties.instance()

    @property
    def reply_window(self) -> ReplyWindow:
        return self._window

    async def on_message(self, frame: Dict[str, Any]) -> None:
        async with _guard("message"):
            event = self._accept(frame)
            if event is None:
                return

            if not self._single_chat_allowed(event):
                await self._reply_single_chat_guide(event, frame)
                return

            try:
                resolution = await self._space.resolve_message(event.to_ref())
            except ChatOwnershipConflict as conflict:
                await self._reject_foreign_chat(event, frame, conflict)
                return

            # 先登记回复窗口再投递任务：Runtime 可能同步很快就回执，
            # 反过来会出现「回执时窗口还没建好」。
            self._window.register(
                event.event_id, frame, ttl_seconds=MESSAGE_REPLY_TTL_SECONDS
            )

            result = await self._sink.submit(
                event.with_context(resolution.context), resolution.context
            )
            if result is SubmitResult.DUPLICATE:
                # 重复回调，任务已存在。窗口留着，正在执行的任务仍需回复。
                logger.info("Duplicate inbound message %s", event.event_id)

    async def on_event(self, frame: Dict[str, Any]) -> None:
        async with _guard("event"):
            event = self._accept(frame)
            if event is None:
                return

            self._window.register(
                event.event_id, frame, ttl_seconds=EVENT_REPLY_TTL_SECONDS
            )

            eventtype = event_type_of(frame)
            if event.event_type is EventKind.CARD_ACTION:
                await self._card_actions.handle(event)
            elif event.event_type is EventKind.FEEDBACK:
                await self._feedbacks.handle(event)
            elif eventtype == "enter_chat":
                await self._handle_enter_chat(event)
            else:
                logger.debug("Ignoring unhandled event type: %s", eventtype)

    def _accept(self, frame: Dict[str, Any]) -> Optional[AgentEvent]:
        """协议层准入：解析帧并校验 aibotid 属于本进程。"""
        try:
            event = normalize_frame(frame)
        except NotAddressedError as exc:
            logger.warning("Dropping malformed frame: %s", exc)
            return None

        if event.bot_id != self._bot_id:
            logger.error(
                "%s", ForeignBotFrame(
                    f"aibotid {event.bot_id} does not belong to this process "
                    f"(current={self._bot_id})"
                )
            )
            return None
        return event

    async def _handle_enter_chat(self, event: AgentEvent) -> None:
        """首次进入会话的欢迎语（A-9）。

        本期延后交付，未注入 handler 时不回复——欢迎语必须在 5 秒内发出，
        没有内容可发时保持沉默好过发一条空话。
        """
        if self._welcome_handler is None:
            return
        body = await self._welcome_handler(event)
        if body:
            await self._sender.reply_welcome(event.event_id, body)

    # ===== 覆盖面与拒绝路径 =====

    def _single_chat_allowed(self, event: AgentEvent) -> bool:
        if event.conversation.type is ChatType.GROUP:
            return True
        return bool(self._props.single_chat_enabled)

    async def _reply_single_chat_guide(
        self, event: AgentEvent, frame: Dict[str, Any]
    ) -> None:
        """私聊不纳入本期（待确认 #15），回一句引导语而不是静默丢弃。"""
        self._window.register(
            event.event_id, frame, ttl_seconds=MESSAGE_REPLY_TTL_SECONDS
        )
        await self._sender.reply_stream(
            event.event_id, self._props.single_chat_guide, finish=True
        )

    async def _reject_foreign_chat(
        self,
        event: AgentEvent,
        frame: Dict[str, Any],
        conflict: ChatOwnershipConflict,
    ) -> None:
        """该群已属于另一条业务线：不初始化记忆、不执行任务，只做提示。"""
        logger.warning("Chat ownership conflict: %s", conflict)
        self._window.register(
            event.event_id, frame, ttl_seconds=MESSAGE_REPLY_TTL_SECONDS
        )
        await self._sender.reply_stream(
            event.event_id,
            "本群已归属另一条业务线的 WeTag 机器人，请在群内只保留一个业务线 Bot。",
            finish=True,
        )


class _guard:
    """入站处理的异常边界。

    任何一帧处理失败都不能让长连接的接收循环中断，也不能静默——
    「失败不静默」是全局纪律（§9.2）。
    """

    def __init__(self, scope: str):
        self._scope = scope

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if exc is None:
            return False
        if isinstance(exc, asyncio.CancelledError):
            return False
        logger.error("Inbound %s handling failed: %s", self._scope, exc, exc_info=True)
        return True
