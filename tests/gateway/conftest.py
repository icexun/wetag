"""Channel 的装配替身。

装配顺序与 ``app/container.py`` 一致：客户端 → 回复窗口 → 发送器 → 各事件
处理器 → 入站编排。顺序一致才能让单测里暴露的问题在生产装配里同样暴露。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from app.contracts.ids import ChatType
from app.gateway.actions import CardActionHandler
from app.gateway.cards.binding import CardBinding
from app.gateway.client import WecomClient
from app.gateway.feedback import FeedbackHandler
from app.gateway.inbound import InboundDispatcher
from app.gateway.outbound import MessageSender
from app.gateway.reply_window import ReplyWindow
from app.runtime.worker.queue import InMemoryTaskSink
from app.space.context_factory import SpaceContextFactory
from app.space.registry import ChatRegistry
from app.space.repository import ChatGroup
from app.space.resolver import BotBindingRegistry
from app.space.topic.router import TopicRouter


def activate(
    space_repo,
    chat_id="chat_001",
    workspace_id="alpha",
    bot_id="bot_alpha",
    chat_type=ChatType.GROUP,
    last_inbound_hours_ago=0.0,
) -> None:
    """把会话置为已激活，让主动推送通过归属与会话窗校验。"""
    now = datetime.now(timezone.utc)
    space_repo.chat_groups[chat_id] = ChatGroup(
        chat_id=chat_id,
        workspace_id=workspace_id,
        bot_id=bot_id,
        chat_type=chat_type,
        display_name=None,
        first_actor_user_id="user_123",
        activated_at=now - timedelta(hours=last_inbound_hours_ago),
        last_inbound_at=now - timedelta(hours=last_inbound_hours_ago),
    )


class FakeApprovalRepo:
    """PendingApprovalRepository 的内存替身：记录决策，next 控制是否生效。"""

    def __init__(self):
        self.decisions: list = []
        self.next: object = object()

    async def approve(self, approval_id, decided_by):
        self.decisions.append(("approve", approval_id, decided_by))
        return self.next

    async def reject(self, approval_id, decided_by):
        self.decisions.append(("reject", approval_id, decided_by))
        return self.next


class FakeQueue:
    """TaskQueue 的内存替身：只记 push。"""

    def __init__(self):
        self.pushed: list = []

    def push(self, task):
        self.pushed.append(task)
        return True


class FakeTaskRepo:
    """TaskRepository 的内存替身：只记 requeue / mark_dead / create。"""
    def __init__(self):
        self.requeued: list = []
        self.dead: list = []
        self.created: list = []

    async def requeue(self, task_id, delay_seconds, *, reason=None, payload_patch=None):
        self.requeued.append((task_id, delay_seconds, reason, payload_patch))
        return {"id": task_id, "payload_patch": payload_patch}  # 仿 Task 返回供入队

    async def mark_dead(self, task_id, error):
        self.dead.append((task_id, error))

    async def create(self, spec):
        from types import SimpleNamespace

        self.created.append(spec)
        return SimpleNamespace(id=f"task-{len(self.created)}", job_name=spec.job_name)


@dataclass
class Harness:
    client: WecomClient
    ws: object
    inbound: InboundDispatcher
    sender: MessageSender
    sink: InMemoryTaskSink
    window: ReplyWindow
    space: object
    cards: object
    feedbacks: object
    approvals: object
    tasks: object
    queue: object


@pytest.fixture
def window() -> ReplyWindow:
    return ReplyWindow()


@pytest.fixture
def chats(space_repo) -> ChatRegistry:
    return ChatRegistry(space_repo)


@pytest.fixture
def sender(ws_client, window, chats, card_repo) -> MessageSender:
    return MessageSender(ws_client, window, chats, "alpha", "bot_alpha", card_tasks=card_repo)


@pytest.fixture
def harness(space_repo, card_repo, feedback_repo, ws_client, window, sender) -> Harness:
    bindings = BotBindingRegistry.from_config()
    binding = bindings.current

    space = SpaceContextFactory(space_repo, bindings, TopicRouter(space_repo))
    cards = CardBinding(card_repo)
    sink = InMemoryTaskSink()
    approvals = FakeApprovalRepo()
    tasks = FakeTaskRepo()
    queue = FakeQueue()

    client = WecomClient(ws_client)
    inbound = InboundDispatcher(
        bot_id=binding.bot_id,
        space=space,
        task_sink=sink,
        sender=sender,
        card_actions=CardActionHandler(
            cards, space, sink, sender,
            approval_repo=approvals, task_repo=tasks, task_queue=queue,
        ),
        feedbacks=FeedbackHandler(cards, feedback_repo),
        reply_window=window,
    )
    client.subscribe(inbound.on_message, inbound.on_event)

    return Harness(
        client=client,
        ws=ws_client,
        inbound=inbound,
        sender=sender,
        sink=sink,
        window=window,
        space=space_repo,
        cards=card_repo,
        feedbacks=feedback_repo,
        approvals=approvals,
        tasks=tasks,
        queue=queue,
    )
