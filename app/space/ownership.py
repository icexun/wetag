"""org.chat_ownership 原子登记：ON CONFLICT DO NOTHING 后回读判定。

「一个真实群只属于一条业务线」是唯一的跨业务线约束（裁决 #5）。登记与判定
必须在同一次往返内完成，不做「先查后写」——两条业务线同时首次 @ 同一个群时，
先查后写会让两边都认为自己是拥有者。
"""

from __future__ import annotations

from app.contracts.errors import ChatOwnershipConflict
from app.contracts.ids import ChatType
from app.space.repository import SpaceRepository
from app.space.resolver import BotBinding


class ChatOwnership:
    def __init__(self, repository: SpaceRepository):
        self._repo = repository

    async def require(
        self,
        chat_id: str,
        chat_type: ChatType,
        binding: BotBinding,
        actor_user_id: str,
    ) -> None:
        """登记归属并确认该群属于本业务线，否则抛 ``ChatOwnershipConflict``。

        只对真实群成立：单聊的 ``chat_id`` 就是用户 ``userid``，同一个人分别与
        两条业务线的 Bot 单聊是正常场景，登记会把两者互斥掉。
        """
        if chat_type is not ChatType.GROUP:
            return

        owner = await self._repo.register_ownership(
            chat_id=chat_id,
            workspace_id=binding.workspace_id,
            bot_id=binding.bot_id,
            chat_type=chat_type,
            actor_user_id=actor_user_id,
        )
        if owner != binding.workspace_id:
            raise ChatOwnershipConflict(chat_id, owner, binding.workspace_id)
