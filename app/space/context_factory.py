"""组装 SpaceContext，全链路归属凭证的唯一出生地。

归属推导链固定为 ``Bot → 业务线 → 群 → 会话事项``，事项不重复保存 botId 或
workspaceId，避免多份归属状态各自漂移。

其他模块只接收、只校验、不构造 SpaceContext——这一条比任何运行时检查都更能
守住 §4.2 的等式。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.contracts.ids import TopicId
from app.contracts.ports import InboundRef
from app.contracts.space_context import SpaceContext
from app.space.config_assembler import ConfigVersionProvider
from app.space.ownership import ChatOwnership
from app.space.registry import ChatRegistry
from app.space.repository import ChatGroup, SpaceRepository
from app.space.resolver import BotBindingRegistry
from app.space.topic.correction import CorrectionCollector
from app.space.topic.router import TopicRoute, TopicRouter
from config.properties import WorkspaceProperties


@dataclass(frozen=True)
class Resolution:
    context: SpaceContext
    chat_group: ChatGroup
    route: Optional[TopicRoute]
    correction_recorded: bool = False

    @property
    def is_new_chat_group(self) -> bool:
        return self.chat_group.activated_at == self.chat_group.last_inbound_at


class SpaceContextFactory:
    def __init__(
        self,
        repository: SpaceRepository,
        bindings: BotBindingRegistry,
        topic_router: TopicRouter,
        config_versions: Optional[ConfigVersionProvider] = None,
    ):
        self._bindings = bindings
        self._router = topic_router
        self._config_versions = config_versions
        self._ownership = ChatOwnership(repository)
        self._registry = ChatRegistry(repository)
        self._corrections = CorrectionCollector(repository)
        self._organization_id = WorkspaceProperties.instance().organization_id

    async def resolve_message(self, ref: InboundRef) -> Resolution:
        """消息事件：登记归属后走完整的事项路由。"""
        chat_group = await self._register(ref)
        route = await self._router.resolve(ref)
        context = await self._build_context(
            chat_group, route.topic.topic_id, ref.actor_user_id
        )
        corrected = await self._corrections.maybe_record(ref, route.topic.topic_id)
        return Resolution(context, chat_group, route, corrected)

    async def resolve_known_topic(
        self, ref: InboundRef, topic_id: TopicId
    ) -> Resolution:
        """卡片与反馈事件：事项由 ``card_task`` 绑定给出，不重新路由。"""
        chat_group = await self._register(ref)
        context = await self._build_context(chat_group, topic_id, ref.actor_user_id)
        return Resolution(context, chat_group, None)

    async def _register(self, ref: InboundRef) -> ChatGroup:
        binding = self._bindings.require(ref.bot_id)
        await self._ownership.require(
            chat_id=ref.chat_id,
            chat_type=ref.chat_type,
            binding=binding,
            actor_user_id=ref.actor_user_id,
        )
        return await self._registry.activate(
            chat_id=ref.chat_id,
            chat_type=ref.chat_type,
            binding=binding,
            actor_user_id=ref.actor_user_id,
        )

    async def _build_context(
        self, chat_group: ChatGroup, topic_id: TopicId, actor_user_id: str
    ) -> SpaceContext:
        version = 0
        if self._config_versions is not None:
            version = await self._config_versions.effective_version(
                chat_group.chat_id
            )
        return SpaceContext(
            organization_id=self._organization_id,
            workspace_id=chat_group.workspace_id,
            bot_id=chat_group.bot_id,
            chat_id=chat_group.chat_id,
            topic_id=topic_id,
            actor_user_id=actor_user_id,
            effective_config_version=version,
        )
