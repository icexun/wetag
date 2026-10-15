"""归档提炼（US8-7）：事项超时无活动归档时，只把关键结论提炼为群级记忆。

不将事项全文作为记忆保存——提炼器（生产为 LLM 调用，测试注入替身）
返回结论列表，逐条经 `remember(scope=GROUP)` 写入，自动携带去重与
冲突标记策略（US8-11 / US8-8）。
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from app.control.memory.remember import MemoryScope, RememberResult, remember
from config import get

logger = logging.getLogger(__name__)


class ArchivedTopic(Protocol):
    """归档事项的最小视图（topic* 归 Space，Control 只读，explore.md 裁决 #19）。"""

    topic_id: str
    chat_id: str
    last_active_at: datetime
    content: str  # 事项全文，仅供提炼器消费，不直接入记忆


# 提炼器：从归档事项提炼关键结论列表；无可提炼结论返回空列表
Distiller = Callable[[ArchivedTopic], Awaitable[list[str]]]


def _inactive_hours() -> float:
    return float(get("MEMORY_ARCHIVE_INACTIVE_HOURS", 24.0))


@dataclass(frozen=True)
class DistillResult:
    distilled: bool
    conclusions: list[str] = field(default_factory=list)
    writes: list[RememberResult] = field(default_factory=list)
    skipped_reason: str | None = None


class ArchiveDistiller:
    """归档策略执行体：超阈值无活动 → 提炼结论写群级记忆。"""

    def __init__(
        self,
        distiller: Distiller,
        *,
        remember_fn: Callable[..., Awaitable[RememberResult]] = remember,
        clock: Callable[[], datetime] | None = None,
        **remember_kwargs: Any,
    ) -> None:
        self._distiller = distiller
        self._remember = remember_fn
        self._remember_kwargs = remember_kwargs
        self._clock = clock or (lambda: datetime.now(UTC))

    async def distill(self, topic: ArchivedTopic) -> DistillResult:
        """事项超过配置时长无活动才提炼；结论入群级记忆，全文不入。"""
        inactive = self._clock() - topic.last_active_at
        if inactive.total_seconds() < _inactive_hours() * 3600:
            return DistillResult(distilled=False, skipped_reason="topic still active")
        conclusions = await self._distiller(topic)
        writes: list[RememberResult] = []
        for conclusion in conclusions or []:
            result = await self._remember(
                conclusion,
                MemoryScope.GROUP,
                chat_id=topic.chat_id,
                topic_id=topic.topic_id,
                **self._remember_kwargs,
            )
            writes.append(result)
        return DistillResult(distilled=bool(conclusions), conclusions=conclusions or [], writes=writes)
