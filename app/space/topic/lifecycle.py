"""事项状态与最后活跃时间。

状态迁移集中在这里，路由只管「归到哪个事项」。归档后的结论提炼与记忆写入
归 Control，本模块只负责状态本身。
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from app.contracts.ids import ChatId, TopicId
from app.space.repository import SpaceRepository, Topic
from config.properties import TopicProperties


class TopicLifecycle:
    def __init__(self, repository: SpaceRepository):
        self._repo = repository
        self._props = TopicProperties.instance()

    async def touch(
        self,
        topic: Topic,
        event_id: Optional[str] = None,
        ref_keys: Sequence[str] = (),
    ) -> None:
        """刷新活跃时间，并把本次消息与外部标识并入事项的命中键。"""
        await self._repo.touch_topic(topic.topic_id, event_id, ref_keys)

    async def reactivate(self, topic: Topic) -> None:
        """再次被明确提及时唤醒已归档事项。"""
        await self._repo.reactivate_topic(topic.topic_id)

    async def archive_idle(
        self, chat_id: ChatId, idle_hours: Optional[int] = None
    ) -> List[TopicId]:
        hours = (
            idle_hours
            if idle_hours is not None
            else int(self._props.idle_archive_hours)
        )
        return await self._repo.archive_idle_topics(chat_id, hours)
