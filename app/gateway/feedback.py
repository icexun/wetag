"""feedback_record 落库（A-5）。

反馈是 Loop 数据面的来源之一，因此只做原样留痕：``result`` 与 ``reason``
透传企微取值，Channel 不解释「有用 / 误报」在业务上意味着什么。

事项归属从 ``card_task`` 的绑定回查——反馈事件本身只带 response_id。
"""

from __future__ import annotations

from typing import Optional

from app.contracts.ids import TopicId
from app.gateway.cards.binding import CardBinding
from app.gateway.event import AgentEvent
from app.gateway.repository import FeedbackRepository

try:
    from agentrun.utils.log import logger
except ImportError:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)


class FeedbackHandler:
    def __init__(self, binding: CardBinding, repository: FeedbackRepository):
        self._binding = binding
        self._repo = repository

    async def handle(self, event: AgentEvent) -> None:
        feedback = event.feedback
        topic_id: Optional[TopicId] = None
        card = await self._binding.get(feedback.response_id)
        if card is not None:
            topic_id = card.topic_id

        created = await self._repo.record(
            response_id=feedback.response_id,
            chat_id=event.conversation.id,
            topic_id=topic_id,
            actor_user_id=event.actor_user_id,
            result=feedback.result,
            reason=feedback.reason,
            comment=feedback.comment,
        )
        if not created:
            logger.info("Duplicate feedback on %s", feedback.response_id)
