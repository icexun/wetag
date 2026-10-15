"""三层召回：当前群 + 当前业务线 + 组织级（plan Decision 5）。

- 范围固定，绝不跨业务线：进程只持有本业务线 collection 名（US8-2）；
  命中结果再按 metadata.workspace_id 做一次防御性过滤。
- 每条命中标注来源层级、来源群、事项与时间（US8-1）；业务线层命中来自
  其他群时 `cross_group=True`（US8-3）。
- 服务不可用 → 显式降级：返回空结果 + degraded 标记 + DEGRADE 告警，
  任务不失败，回答层用 `note` 明示「本次未使用历史记忆」（US8-9）。
- 召回为空 → `note` 明示「未找到相关历史记忆」，不沉默略过（US8-10）。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.contracts.errors import MemoryUnavailable
from app.control.memory.backend import MemoryBackend
from app.control.memory.collections import MemoryCollections
from app.platform.observability.alerting import AlertKind, alert

logger = logging.getLogger(__name__)

# 层级权重：群 > 业务线 > 组织（plan §control/memory）
_LAYER_WEIGHT = {"group": 1.0, "biz": 0.7, "org": 0.5}

DEGRADED_NOTE = "本次未使用历史记忆"
EMPTY_NOTE = "未找到相关历史记忆"


def layer_weight(layer: str) -> float:
    return _LAYER_WEIGHT.get(layer, 0.0)


@dataclass(frozen=True)
class MemoryHit:
    """一条召回命中，携带完整来源元数据（US8-1）。"""

    memory_id: str
    content: str
    layer: str  # group / biz / org
    score: float
    source_chat_id: str | None = None
    source_topic_id: str | None = None
    created_at: datetime | None = None
    cross_group: bool = False  # 来自同业务线的其他群（US8-3）

    @property
    def weighted_score(self) -> float:
        return self.score * layer_weight(self.layer)


@dataclass(frozen=True)
class RecallResult:
    """召回结果。degraded=True 表示记忆服务不可用、结果为空是降级所致。"""

    hits: list[MemoryHit] = field(default_factory=list)
    degraded: bool = False

    @property
    def empty(self) -> bool:
        return not self.hits

    @property
    def note(self) -> str | None:
        """回答层的显式说明文案；有命中时为 None。"""
        if self.degraded:
            return DEGRADED_NOTE
        if self.empty:
            return EMPTY_NOTE
        return None


def _to_hit(raw: dict[str, Any], *, layer: str, current_chat_id: str) -> MemoryHit | None:
    metadata = raw.get("metadata") or {}
    source_chat = metadata.get("chat_id")
    return MemoryHit(
        memory_id=str(raw.get("id") or ""),
        content=str(raw.get("content") or ""),
        layer=layer,
        score=float(raw.get("score") or 0.0),
        source_chat_id=source_chat,
        source_topic_id=metadata.get("topic_id"),
        created_at=raw.get("created_at"),
        cross_group=layer == "biz" and source_chat is not None and source_chat != current_chat_id,
    )


async def recall(
    collections: MemoryCollections,
    backend: MemoryBackend,
    query: str,
    *,
    chat_id: str,
    limit: int = 10,
) -> RecallResult:
    """并发查三层，按「层级权重 × 相关度」排序。

    记忆服务不可用（无 collection 名 / 后端异常）时返回 degraded 空结果并告警，
    不向上抛错——记忆缺失不拖垮任务（US8-9）。
    """
    try:
        layers = collections.resolve()
    except MemoryUnavailable as exc:
        alert(
            AlertKind.DEGRADE,
            "语义记忆服务不可用",
            str(exc),
            workspace_id=collections.workspace_id,
        )
        return RecallResult(degraded=True)

    async def _search(collection: str, filter: dict[str, Any] | None) -> list[dict[str, Any]]:
        return await backend.search(collection, query, filter=filter, limit=limit)

    try:
        group_raw, biz_raw, org_raw = await asyncio.gather(
            _search(layers.biz_collection, layers.group_filter(chat_id)),
            _search(layers.biz_collection, layers.biz_filter()),
            _search(layers.org_collection, None) if layers.org_collection else _empty(),
        )
    except MemoryUnavailable as exc:
        alert(
            AlertKind.DEGRADE,
            "语义记忆召回失败",
            str(exc),
            workspace_id=layers.workspace_id,
        )
        return RecallResult(degraded=True)

    hits: list[MemoryHit] = []
    for layer, raws in (("group", group_raw), ("biz", biz_raw), ("org", org_raw)):
        for raw in raws:
            metadata = raw.get("metadata") or {}
            # 防御性过滤：绝不接受其他业务线的命中（US8-2）
            if metadata.get("workspace_id") not in (None, layers.workspace_id):
                continue
            hit = _to_hit(raw, layer=layer, current_chat_id=chat_id)
            if hit is not None and hit.content:
                hits.append(hit)
    hits.sort(key=lambda h: h.weighted_score, reverse=True)
    return RecallResult(hits=hits[:limit])


async def _empty() -> list[dict[str, Any]]:
    return []
