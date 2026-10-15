"""topic_correction 纠正样本采集（仅评测，不喂 Prompt）。

今日降级：只写 ``topic_correction`` 供离线评测，不即时迁移消息与任务
（§10.3）。补做迁移时调用方不变。
"""

from __future__ import annotations

import re

from app.contracts.ids import TopicId
from app.contracts.ports import InboundRef
from app.space.repository import SpaceRepository

# 自然语言纠正的触发词。
_CORRECTION_HINT = re.compile(
    r"(不是这个事|不是同一个|另一个需求|换个事项|这属于|应该归到|搞错了|说的是另)"
)


class CorrectionCollector:
    def __init__(self, repository: SpaceRepository):
        self._repo = repository

    async def maybe_record(self, ref: InboundRef, current_topic_id: TopicId) -> bool:
        """识别用户对事项划分的纠正并留样，返回是否记了一条。"""
        if not _CORRECTION_HINT.search(ref.text or ""):
            return False
        await self._repo.record_correction(
            chat_id=ref.chat_id,
            actor_user_id=ref.actor_user_id,
            raw_text=ref.text,
            from_topic_id=current_topic_id,
            to_topic_id=None,
        )
        return True
