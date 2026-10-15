"""card_task 安全绑定与 CAS 状态迁移（卡片事件幂等，裁决 #6）。

卡片按钮群内任何人都点得到，且企微只推送一次事件、要求 5 秒内响应，因此
绑定校验与状态迁移必须在同一条路径上走完：先判定是否越权，再用一次条件更新
抢占处理权。

「重复点击」与「越权点击」的处置并不相同，所以结果分成四档而不是布尔值。
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Optional, Tuple

from app.contracts.ids import ChatId
from app.gateway.repository import CardTask, CardTaskRepository


class CardClaimResult(str, Enum):
    CLAIMED = "CLAIMED"
    DUPLICATE = "DUPLICATE"
    """CAS 未命中：同一按钮被重复点击，或卡片已处理。属正常现象，不告警。"""
    NOT_FOUND = "NOT_FOUND"
    FORBIDDEN = "FORBIDDEN"
    """绑定校验不通过：跨会话、跨 Bot、非授权操作者或未声明的按钮。"""


class CardBinding:
    def __init__(self, repository: CardTaskRepository):
        self._repo = repository

    async def get(self, task_id: str) -> Optional[CardTask]:
        return await self._repo.get(task_id)

    async def claim(
        self,
        task_id: str,
        bot_id: str,
        chat_id: ChatId,
        actor_user_id: str,
        event_key: str,
    ) -> Tuple[CardClaimResult, Optional[CardTask]]:
        """校验卡片绑定并抢占一次处理权。"""
        card = await self._repo.get(task_id)
        if card is None:
            return CardClaimResult.NOT_FOUND, None

        # 跨 Bot、跨会话、未声明按钮、非授权操作者，一律显式拒绝，
        # 不寻找替代目标。
        if card.bot_id != bot_id or card.chat_id != chat_id:
            return CardClaimResult.FORBIDDEN, card
        if card.allowed_actions and event_key not in card.allowed_actions:
            return CardClaimResult.FORBIDDEN, card
        if card.allowed_actors and actor_user_id not in card.allowed_actors:
            return CardClaimResult.FORBIDDEN, card

        claimed = await self._repo.cas_claim(task_id, actor_user_id, event_key)
        if claimed is None:
            return CardClaimResult.DUPLICATE, card
        return CardClaimResult.CLAIMED, claimed


def new_card_task_id() -> str:
    """生成卡片 task_id。同一 Bot 下唯一，长度远低于 128 字节上限。"""
    return f"c_{uuid.uuid4().hex}"
