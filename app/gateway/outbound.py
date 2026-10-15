"""出站原语：Outbox 调用它发送，它不持有队列（裁决 #7）。

合并窗口、令牌桶限流、退避重试都只在 Runtime 的 ``outbox`` 实现一次。本模块
负责的是另外两件事，它们必须贴着协议做、无法上移：

1. 归属校验——目标会话必须属于本进程的业务线且已激活，否则显式拒绝，
   不寻找跨 Bot 或跨群的替代目标；
2. 回复窗口降级——原始帧过期后自动改走主动推送。

会话归属事实从契约的 ``ChatDirectory`` 取：``chat_group`` 归 Space，Channel
禁止 import ``space/``。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from app.contracts.envelope import MsgType
from app.contracts.errors import IsolationViolation
from app.contracts.ids import ChatId, ChatType, WorkspaceId
from app.contracts.ports import ChatBinding, ChatDirectory
from app.gateway.cards.binding import new_card_task_id
from app.gateway.cards.builder import (
    ButtonStyle,
    CardButton,
    build_alert_card,
    build_confirm_card,
    build_markdown,
)
from app.gateway.reply_window import ReplyWindow
from config.properties import WecomProperties

try:
    from agentrun.utils.log import logger
except ImportError:  # pragma: no cover - 脚手架之外的运行环境
    import logging

    logger = logging.getLogger(__name__)


class ConversationNotActivated(RuntimeError):
    """目标会话未登记或已超出会话窗。

    企微要求「用户先在会话中给机器人发过消息」才允许主动推送，
    因此这不是可重试的错误。
    """


class OutboundRejected(IsolationViolation):
    """归属校验不通过。属安全事件，不降级、不重试。"""


@dataclass(frozen=True)
class SendResult:
    ok: bool
    errcode: int = 0
    errmsg: str = "ok"
    degraded_to_push: bool = False
    """回复窗口已过期，本次改用主动推送送达。"""


def send_code_of(chat_type: ChatType) -> int:
    """``aibot_send_msg`` 的 ``chat_type``：1 单聊，2 群聊。

    文档允许填 0 兼容，但兼容模式优先按群聊解析，会把单聊 userid 误判，
    因此这里始终给出明确值。
    """
    return 2 if chat_type is ChatType.GROUP else 1


class MessageSender:
    def __init__(
        self,
        client: Any,
        reply_window: ReplyWindow,
        chat_directory: ChatDirectory,
        workspace_id: WorkspaceId,
        bot_id: str,
        card_tasks: Any = None,
        story_title_resolver: Any = None,
    ):
        self._client = client
        self._window = reply_window
        self._chats = chat_directory
        self._workspace_id = workspace_id
        self._bot_id = bot_id
        # 审批卡片的安全绑定登记处；缺省时审批卡片仍可发，但按钮点击会被判 NOT_FOUND
        self._card_tasks = card_tasks
        # 审批卡片的需求标题解析器（story_id → 标题）；缺省时卡片回退显示原始 ID
        self._story_title_resolver = story_title_resolver
        props = WecomProperties.instance()
        self._enforce_window = bool(props.session_window_enforce)
        self._window_hours = int(props.session_window_hours)

    # ===== 回复：依赖回调帧的 req_id =====

    async def reply_stream(
        self,
        event_id: str,
        content: str,
        finish: bool = False,
        chat_id: Optional[ChatId] = None,
        feedback_id: Optional[str] = None,
    ) -> SendResult:
        """流式回复。首次调用创建消息，后续调用刷新同一条消息。

        长连接模式下企微不再轮询回调要内容，全靠开发者主动推送刷新，
        因此 FastAck 回执与 15 秒进度更新都走这里。
        """
        frame = self._window.frame_of(event_id)
        if frame is None:
            # 超过 10 分钟的流式窗口。结论不能丢，改为主动推送新消息。
            if chat_id is None:
                raise ConversationNotActivated(
                    f"reply window expired for {event_id} and no chat_id to fall back"
                )
            result = await self.push_markdown(chat_id, content)
            return SendResult(
                ok=result.ok,
                errcode=result.errcode,
                errmsg=result.errmsg,
                degraded_to_push=True,
            )

        stream_id = self._window.stream_id_of(event_id) or ""
        first_frame = not stream_id
        if not stream_id:
            stream_id = f"s_{uuid.uuid4().hex[:16]}"
            self._window.set_stream_id(event_id, stream_id)

        ack = await self._client.reply_stream(
            frame,
            stream_id=stream_id,
            content=content,
            finish=finish,
            # SDK 约定「feedback 仅在首次回复时设置」：续帧带 feedback
            # 企微传输层照收（ack errcode=0）但不应用该帧——实测终态
            # 卡片停在受理文案，群里永远看不到最终结果（41016 同期的
            # M0 实测结论）。
            feedback={"id": feedback_id} if feedback_id and first_frame else None,
        )
        if finish:
            self._window.finish_stream(event_id)
        return _to_result(ack)

    async def reply_card(
        self, event_id: str, card: Dict[str, Any], feedback_id: Optional[str] = None
    ) -> SendResult:
        frame = self._require_frame(event_id)
        ack = await self._client.reply_template_card(
            frame,
            template_card=card,
            feedback={"id": feedback_id} if feedback_id else None,
        )
        return _to_result(ack)

    async def reply_welcome(self, event_id: str, body: Dict[str, Any]) -> SendResult:
        """回复欢迎语。仅适用于 enter_chat，且必须在 5 秒内发出。"""
        frame = self._require_frame(event_id)
        ack = await self._client.reply_welcome(frame, body)
        return _to_result(ack)

    async def update_card(self, event_id: str, card: Dict[str, Any]) -> SendResult:
        """更新模板卡片。仅适用于卡片点击事件，且必须在 5 秒内发出。

        ``card.task_id`` 必须与回调中的一致，否则企微无法定位要更新的卡片。
        """
        frame = self._require_frame(event_id)
        ack = await self._client.update_template_card(frame, template_card=card)
        return _to_result(ack)

    # ===== 主动推送：不依赖回调帧 =====

    async def send_outbox(self, msg: Any) -> str:
        """Outbox 派发侧的发送原语（runtime.outbox.dispatcher 的 Sender 协议）。

        按 msg_type 分发到推送原语；企微侧无平台 msg_id 可回时返回空串。
        ``update_of`` 的原地更新依赖回调帧，推送路径没有等价物，退化为新发一条。
        """
        # feedback_id 取 task_id：企微只对带 feedback.id 的消息回传点赞/点踩事件，
        # 不带则 feedback_record 永远收不到（A-5 数据面断流）
        feedback_id = str(msg.task_id) if msg.task_id else None
        if msg.msg_type == MsgType.MARKDOWN.value:
            result = await self.push_markdown(
                ChatId(msg.chat_id), str(msg.payload.get("text", "")), feedback_id
            )
        elif msg.msg_type == MsgType.TEMPLATE_CARD.value:
            card = msg.payload.get("template_card")
            if not card and msg.payload.get("card_type") == "approval":
                # control 层只带语义字段（card_type/tool_name/tool_input），
                # 卡片形态在 gateway 构造——否则空卡片被企微拒收（41016 missing title）
                tool_name = str(msg.payload.get("tool_name", ""))
                tool_input = msg.payload.get("tool_input")
                approval_id = str(msg.payload.get("approval_id") or msg.id)
                title = f"审批请求：{_tool_display_name(tool_name)}"
                names = await self._resolve_approval_names(tool_input)
                desc, items = _summarize_approval(tool_name, tool_input, names=names)
                card = build_confirm_card(
                    task_id=approval_id,
                    title=title,
                    desc=desc,
                    items=items,
                )
                # 登记安全绑定：没有 card_task 行，按钮点击会被判 NOT_FOUND 拒掉，
                # 用户看到的就是"点了没反应"
                if self._card_tasks is not None:
                    await self._card_tasks.create(
                        task_id=approval_id,
                        workspace_id=self._workspace_id,
                        bot_id=self._bot_id,
                        chat_id=ChatId(msg.chat_id),
                        topic_id=getattr(msg, "topic_id", None) or "",
                        allowed_actions=("confirm", "cancel"),
                        payload={
                            "kind": "approval",
                            "approval_id": approval_id,
                            "task_id": str(msg.task_id or ""),
                            "tool_name": tool_name,
                            "title": title,
                        },
                    )
            if not card:
                card = _build_notice_card(msg)
                if card is not None and self._card_tasks is not None:
                    await self._card_tasks.create(
                        task_id=card["task_id"],
                        workspace_id=self._workspace_id,
                        bot_id=self._bot_id,
                        chat_id=ChatId(msg.chat_id),
                        topic_id=getattr(msg, "topic_id", None) or "",
                        allowed_actions=("ack",),
                        payload={
                            "kind": "notice",
                            "card_type": str(msg.payload.get("card_type") or ""),
                            "title": card["main_title"]["title"],
                        },
                    )
            if not _card_title(card):
                # 空卡片打到企微必被 41016 missing title 拒收，重试同一份载荷不会变好
                raise OutboundRejected(
                    "模板卡片缺少 main_title.title，拒绝发送: "
                    f"card_type={msg.payload.get('card_type')!r}"
                )
            result = await self.push_card(ChatId(msg.chat_id), dict(card), feedback_id)
        else:
            raise OutboundRejected(f"不支持的出站形态: {msg.msg_type!r}")
        if not result.ok:
            raise OutboundRejected(
                f"企微拒收: errcode={result.errcode} {result.errmsg}"
            )
        return ""

    async def push_markdown(
        self, chat_id: ChatId, content: str, feedback_id: Optional[str] = None
    ) -> SendResult:
        return await self._push(chat_id, build_markdown(content, feedback_id))

    async def push_card(
        self,
        chat_id: ChatId,
        card: Dict[str, Any],
        feedback_id: Optional[str] = None,
    ) -> SendResult:
        payload = dict(card)
        if feedback_id:
            payload["feedback"] = {"id": feedback_id}
        return await self._push(
            chat_id, {"msgtype": "template_card", "template_card": payload}
        )

    async def _push(self, chat_id: ChatId, body: Dict[str, Any]) -> SendResult:
        binding = await self._chats.binding_of(chat_id)
        if binding is None:
            raise ConversationNotActivated(
                f"chat {chat_id} has never interacted with bot {self._bot_id}"
            )
        if binding.workspace_id != self._workspace_id:
            raise OutboundRejected(
                f"chat {chat_id} belongs to workspace {binding.workspace_id}, "
                f"refusing to send from {self._workspace_id}"
            )
        if binding.bot_id != self._bot_id:
            raise OutboundRejected(
                f"chat {chat_id} is bound to bot {binding.bot_id}, "
                f"refusing to send from {self._bot_id}"
            )
        if self._enforce_window and not self._within_session_window(binding):
            # 跨窗推送是否有效待 M0 实测（待确认 #1）。实测通过后把
            # SESSION_WINDOW_ENFORCE 关掉即可，无需改代码。
            raise ConversationNotActivated(
                f"chat {chat_id} is outside the {self._window_hours}h session window"
            )

        ack = await self._client.send_message(
            chat_id,
            {"chat_type": send_code_of(binding.chat_type), **body},
        )
        return _to_result(ack)

    def _within_session_window(self, binding: ChatBinding) -> bool:
        elapsed = datetime.now(timezone.utc) - binding.last_inbound_at
        return elapsed < timedelta(hours=self._window_hours)

    def _require_frame(self, event_id: str) -> Dict[str, Any]:
        frame = self._window.frame_of(event_id)
        if frame is None:
            raise ConversationNotActivated(
                f"reply window expired for event {event_id}"
            )
        return frame

    async def _resolve_approval_names(self, tool_input: Any) -> Dict[str, str]:
        """把审批入参里的 ID 解析成人可读名称（目前只有 story_id → 需求标题）。

        解析失败回退空表，卡片显示原始 ID——名称是易读性增强，不能拖垮发卡。
        """
        if not isinstance(tool_input, dict) or self._story_title_resolver is None:
            return {}
        story_id = tool_input.get("story_id")
        if not story_id:
            return {}
        try:
            title = await self._story_title_resolver(str(story_id))
        except Exception:
            logger.warning("需求标题解析失败 story_id=%s", story_id, exc_info=True)
            return {}
        return {"story_id": title} if title else {}


_NOTICE_TITLES = {
    "capability_unavailable": "能力暂不可用",
    "todo_escalation": "待办升级",
    "approval_result": "审批结果",
    "memory_conflict": "记忆冲突",
    "memory_review": "记忆待确认",
}


def _build_notice_card(msg: Any) -> Optional[Dict[str, Any]]:
    """语义通知卡片：payload 只有 card_type / text，发送前补上企微必填标题。

    ``button_interaction`` 的 ``button_list`` 也是必填。通知没有业务按钮，
    放一个「知道了」；点击只回显，不派生任务（kind=notice）。
    """
    payload = getattr(msg, "payload", None) or {}
    card_type = str(payload.get("card_type") or "")
    if card_type not in _NOTICE_TITLES:
        return None
    text = str(payload.get("text") or "").strip()
    if card_type == "capability_unavailable":
        capability = str(payload.get("capability") or "能力").strip() or "能力"
        title = f"{capability}暂不可用"
    else:
        title = _NOTICE_TITLES[card_type]
    outbox_id = str(getattr(msg, "id", "") or "")
    task_id = f"nt_{outbox_id}" if outbox_id else new_card_task_id()
    return build_alert_card(
        task_id=task_id,
        title=title,
        desc=text or title,
        buttons=(CardButton(key="ack", text="知道了", style=ButtonStyle.GREY),),
    )


def _card_title(card: Any) -> str:
    if not isinstance(card, dict):
        return ""
    main = card.get("main_title") or {}
    if not isinstance(main, dict):
        return ""
    return str(main.get("title") or "").strip()


def _tool_display_name(tool_name: str) -> str:
    """工具名 → 审批人可读的动作名。未知工具原样返回。"""
    return _TOOL_DISPLAY.get(tool_name, tool_name)


# 审批卡片标题里的动作名：审批人看的是"要干什么"，不是函数名
_TOOL_DISPLAY = {
    "feishu_doc_create": "创建飞书文档",
    "feishu_doc_append": "追加飞书文档内容",
    "feishu_doc_update": "修改飞书文档内容",
    "tapd_story_create": "创建 TAPD 需求",
    "tapd_story_update": "更新 TAPD 需求",
    "tapd_bug_create": "创建 TAPD 缺陷",
    "tapd_bug_update": "更新 TAPD 缺陷",
    "tapd_task_create": "创建 TAPD 任务",
    "tapd_task_update": "更新 TAPD 任务",
    "tapd_comment_add": "添加 TAPD 评论",
    "feishu_sheet_write_csv": "写入飞书表格",
    "feishu_base_upsert": "写入飞书多维表格",
    "feishu_wiki_node_create": "创建飞书知识库页面",
    "wecom_message_send": "发送企业微信消息",
    "wecom_sheet_append_row": "追加企业微信表格行",
    "wecom_smartsheet_add": "新增智能表格记录",
    "wecom_smartsheet_update": "更新智能表格记录",
    "wecom_mail_send": "发送邮件",
}

# 入参里需要在卡片条目上展示的标签名
_FIELD_LABELS = {
    "title": "标题",
    "name": "名称",
    "content_markdown": "内容",
    "content": "内容",
    "doc_url": "文档",
    "story_id": "需求",
    "bug_id": "缺陷 ID",
    "task_id": "任务 ID",
    "workspace_id": "项目",
    "fields": "变更字段",
    "description": "描述",
    "parent_token": "目标位置",
    "subject": "主题",
    "text": "内容",
    "csv": "数据",
    "records": "记录",
    "values": "数据",
    "to_userids": "收件人",
    "to_emails": "收件人邮箱",
    "chat_id": "会话",
}

def _project_name(project_id: Any) -> Optional[str]:
    """TAPD 项目 ID → 项目名（审批卡片展示用，审批人看不懂裸 ID）。

    取当前 workspace 的 ``TAPD_PROJECT_ID`` / ``TAPD_PROJECT_NAME``；未命中时原样显示 ID。
    """
    import config

    pid = str(project_id).strip()
    if pid and pid == str(config.get("TAPD_PROJECT_ID", "")).strip():
        return config.get("TAPD_PROJECT_NAME")
    return None


def _summarize_approval(
    tool_name: str,
    tool_input: Any,
    *,
    max_items: int = 4,
    names: Optional[Dict[str, str]] = None,
) -> tuple[str, list]:
    """审批卡片的 desc + 参数条目：让审批人一眼看出"要审批什么"。

    desc 是一句话动作摘要；items 是关键参数（长值截断），原始 JSON 不再上卡片。
    ``names`` 携带已解析的名称（如 story_id → 需求标题），命中时条目显示名称
    而非 ID；``workspace_id`` 项目 ID 经 ``_project_name`` 映射。
    """
    from app.gateway.cards.builder import CardItem

    if not isinstance(tool_input, dict) or not tool_input:
        return "（无参数）", []

    names = names or {}
    # 一句话摘要：优先用具名字段拼，其次用解析出的需求标题，兜底用动作名
    subject = (
        tool_input.get("title") or tool_input.get("name") or tool_input.get("subject")
        or names.get("story_id") or ""
    )
    action = _tool_display_name(tool_name)
    desc = f"{action}《{subject}》" if subject else action

    items: list = []
    for key, value in tool_input.items():
        if len(items) >= max_items:
            break
        display: Any = names.get(key)
        if display is None and key == "workspace_id":
            display = _project_name(value)
        text = str(display if display is not None else value).replace("\n", " ").strip()
        if not text:
            continue
        if len(text) > 100:
            text = text[:100] + "…"
        items.append(CardItem(label=_FIELD_LABELS.get(key, key), value=text))
    return desc, items


def _to_result(ack: Optional[Dict[str, Any]]) -> SendResult:
    if not ack:
        return SendResult(ok=True)
    errcode = int(ack.get("errcode", 0) or 0)
    errmsg = ack.get("errmsg", "ok")
    if errcode != 0:
        logger.warning("WeCom rejected outbound frame: %s %s", errcode, errmsg)
    return SendResult(ok=errcode == 0, errcode=errcode, errmsg=errmsg)
