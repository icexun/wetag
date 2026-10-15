"""chat_group upsert：群名、activated_at、last_inbound_at。

同时是 ``ChatDirectory`` 的实现：Channel 出站前要校验目标会话的归属与会话窗，
但它禁止 import ``space/``，只能通过契约里的 ``ChatDirectory`` 取到这几项事实。
"""

from __future__ import annotations

from typing import Optional

from app.contracts.ids import ChatId, ChatType
from app.contracts.ports import ChatBinding
from app.space.repository import ChatGroup, SpaceRepository
from app.space.resolver import BotBinding


class ChatRegistry:
    def __init__(self, repository: SpaceRepository):
        self._repo = repository

    async def activate(
        self,
        chat_id: ChatId,
        chat_type: ChatType,
        binding: BotBinding,
        actor_user_id: str,
    ) -> ChatGroup:
        """首次 @ 登记群，此后每条入站消息刷新 ``last_inbound_at``。"""
        return await self._repo.upsert_chat_group(
            chat_id=chat_id,
            workspace_id=binding.workspace_id,
            bot_id=binding.bot_id,
            chat_type=chat_type,
            actor_user_id=actor_user_id,
        )

    async def get(self, chat_id: ChatId) -> Optional[ChatGroup]:
        return await self._repo.get_chat_group(chat_id)

    async def rename(self, chat_id: ChatId, display_name: str) -> None:
        """群名只能由用户告知——机器人读不到群名（平台约束）。"""
        await self._repo.set_display_name(chat_id, display_name)

    async def binding_of(self, chat_id: ChatId) -> Optional[ChatBinding]:
        chat_group = await self._repo.get_chat_group(chat_id)
        if chat_group is None:
            return None
        return ChatBinding(
            chat_id=chat_group.chat_id,
            workspace_id=chat_group.workspace_id,
            bot_id=chat_group.bot_id,
            chat_type=chat_group.chat_type,
            last_inbound_at=chat_group.last_inbound_at,
        )
