"""三层写入与回复后自动沉淀（US8-4 / US8-6 / US8-11）。

- 写入层级由调用点给定，而非模型自选：`scope` 仅允许 GROUP / BIZ；
  指定 ORG 一律拒绝直接写入，只生成组织管理员审批提案（US8-6）。
- BIZ 且置信度低于阈值 → 进 pending_memory 暂存并发确认卡片，批准后才转正（US8-5，
  审批流本体在 policy/pending_review.py）。
- 写入前相似度去重：已有等价记忆则更新而非新增（US8-11）。
- 与既有记忆矛盾：两条都保留，创建冲突标记待人工裁决（US8-8，
  台账本体在 policy/conflict_mark.py）。
- 自动沉淀：`MemorySedimenter.post_reply` 在每轮回复结束后提炼新决策/偏好
  写群级记忆，不要求 Agent 显式调用（US8-4）；沉淀失败只记日志，不拖垮任务。
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.contracts.errors import MemoryUnavailable
from app.contracts.task import Task
from app.control.memory.backend import MemoryBackend
from app.control.memory.collections import MemoryCollections
from config import get

logger = logging.getLogger(__name__)


class MemoryScope(StrEnum):
    GROUP = "group"
    BIZ = "biz"
    ORG = "org"


class RememberStatus(StrEnum):
    WRITTEN = "written"  # 直接写入
    UPDATED = "updated"  # 等价记忆已存在，更新而非新增（US8-11）
    PENDING = "pending"  # 低置信度进待审批暂存（US8-5）
    PROPOSAL = "proposal"  # 组织级仅提案（US8-6）
    UNAVAILABLE = "unavailable"  # 记忆服务不可用，显式降级（US8-9）


@dataclass(frozen=True)
class RememberResult:
    status: RememberStatus
    memory_id: str | None = None
    pending_id: str | None = None
    conflict_id: str | None = None


# 矛盾检测器：给定新内容与同层候选，返回与之矛盾的既有 memory_id（无则 None）
ConflictDetector = Callable[[str, list[dict[str, Any]]], Awaitable[str | None]]


def _dedup_score() -> float:
    return float(get("MEMORY_DEDUP_SCORE", 0.95))


def _confidence_threshold() -> float:
    return float(get("MEMORY_CONFIDENCE_THRESHOLD", 0.6))


async def remember(
    content: str,
    scope: MemoryScope,
    *,
    collections: MemoryCollections,
    backend: MemoryBackend,
    chat_id: str | None = None,
    topic_id: str | None = None,
    confidence: float = 1.0,
    pending_review: Any = None,
    conflict_detector: ConflictDetector | None = None,
    conflict_marker: Any = None,
) -> RememberResult:
    """按调用点给定的层级写入记忆。返回结果显式表达每种分流结局。"""
    # US8-6：组织级直接写入一律被拒绝，只能生成审批提案
    if scope == MemoryScope.ORG:
        if pending_review is None:
            return RememberResult(RememberStatus.PROPOSAL)
        pending = await pending_review.submit(
            content, "org", confidence=confidence, chat_id=chat_id, topic_id=topic_id
        )
        return RememberResult(RememberStatus.PROPOSAL, pending_id=str(pending.id))

    # US8-5：业务线级低置信度 → 待审批暂存 + 确认卡片
    if scope == MemoryScope.BIZ and confidence < _confidence_threshold():
        if pending_review is None:
            raise MemoryUnavailable("pending review channel not wired for biz memory")
        pending = await pending_review.submit(
            content, "biz", confidence=confidence, chat_id=chat_id, topic_id=topic_id
        )
        return RememberResult(RememberStatus.PENDING, pending_id=str(pending.id))

    try:
        layers = collections.resolve()
    except MemoryUnavailable:
        logger.warning("memory unavailable, skip remember (scope=%s)", scope)
        return RememberResult(RememberStatus.UNAVAILABLE)

    if scope == MemoryScope.GROUP:
        metadata = {**layers.group_filter(chat_id or ""), "topic_id": topic_id}
    else:
        metadata = {**layers.biz_filter(), "topic_id": topic_id}
    metadata = {k: v for k, v in metadata.items() if v is not None}

    try:
        # US8-11：相似度去重——已有等价记忆则更新而非累积副本
        candidates = await backend.search(
            layers.biz_collection, content, filter={"layer": scope.value}, limit=5
        )
        for cand in candidates:
            if float(cand.get("score") or 0.0) >= _dedup_score():
                memory_id = await backend.upsert(
                    layers.biz_collection,
                    content,
                    metadata=metadata,
                    memory_id=str(cand["id"]),
                )
                return RememberResult(RememberStatus.UPDATED, memory_id=memory_id)

        # US8-8：矛盾检测——两条都保留，创建冲突标记待人工裁决
        conflict_with: str | None = None
        if conflict_detector is not None:
            conflict_with = await conflict_detector(content, candidates)

        memory_id = await backend.upsert(layers.biz_collection, content, metadata=metadata)

        conflict_id: str | None = None
        if conflict_with is not None and conflict_marker is not None:
            conflict = await conflict_marker.mark(
                conflict_with, memory_id, layer=scope.value, chat_id=chat_id
            )
            conflict_id = str(conflict.id)
        return RememberResult(RememberStatus.WRITTEN, memory_id=memory_id, conflict_id=conflict_id)
    except MemoryUnavailable:
        logger.warning("memory backend failed, skip remember (scope=%s)", scope)
        return RememberResult(RememberStatus.UNAVAILABLE)


# ---- 回复后自动沉淀（US8-4） ----

# 提炼器：从本轮任务与回复中提炼「新确立的决策/偏好」结论列表；无则不写
ConclusionExtractor = Callable[[Task, str], Awaitable[list[str]]]


def _reply_text(reply: Any) -> str:
    if isinstance(reply, str):
        return reply
    getter = getattr(reply, "get_text_content", None)
    if callable(getter):
        return getter() or ""
    return str(getattr(reply, "content", "") or "")


class MemorySedimenter:
    """回复结束后的记忆沉淀钩子。只对会话任务（有 chat_id）生效。

    沉淀全程容错：任何失败只记日志，绝不让记忆缺失拖垮任务（US8-9）。
    """

    def __init__(
        self,
        collections: MemoryCollections,
        backend: MemoryBackend,
        *,
        extractor: ConclusionExtractor | None = None,
        **remember_kwargs: Any,
    ) -> None:
        self._collections = collections
        self._backend = backend
        self._extractor = extractor
        self._remember_kwargs = remember_kwargs

    async def post_reply(self, task: Task, reply: Any) -> None:
        """本轮回复结束后调用：提炼到新决策/偏好则写群级记忆（US8-4）。"""
        if not task.chat_id or self._extractor is None:
            return
        try:
            conclusions = await self._extractor(task, _reply_text(reply))
            for conclusion in conclusions or []:
                await remember(
                    conclusion,
                    MemoryScope.GROUP,
                    collections=self._collections,
                    backend=self._backend,
                    chat_id=task.chat_id,
                    topic_id=task.topic_id,
                    **self._remember_kwargs,
                )
        except Exception:  # noqa: BLE001 - 沉淀失败不拖垮任务
            logger.exception("memory sediment failed (task=%s)", task.id)

    def hook_for(self, task: Task) -> Callable[..., Awaitable[None]]:
        """agentscope post_reply 实例钩子形态：hook(self, kwargs, output) -> None。"""

        async def _hook(_agent: Any, _kwargs: dict[str, Any], output: Any) -> None:
            await self.post_reply(task, output)

        return _hook


def register_instance_hook(agent: Any, sedimenter: MemorySedimenter, task: Task) -> bool:
    """向 agentscope agent 注册 post_reply 沉淀钩子；不支持时返回 False，
    由调用侧在 reply 后内联调用 `sedimenter.post_reply` 兜底。"""
    register = getattr(agent, "register_instance_hook", None)
    if register is None:
        return False
    register("post_reply", "wetag_memory_sediment", sedimenter.hook_for(task))
    return True
