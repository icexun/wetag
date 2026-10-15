"""矛盾记忆冲突标记（US8-8）：两条都保留 + 台账标记 + 提示人工裁决。

语义记忆轨本身不承载裁决状态，冲突事实落 `memory_conflict` 台账；
同一对矛盾由 `ux_memory_conflict_pair` 唯一索引保证只记一次。
不覆盖旧记忆——写入侧（remember.py）先写新记忆再调 `mark`。
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

import asyncpg
from pydantic import BaseModel

from app.contracts.envelope import Envelope, MsgType, Priority
from app.platform.db.pool import Pool
from app.platform.db.repository import BaseRepository

logger = logging.getLogger(__name__)


class ConflictStatus(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


class MemoryConflict(BaseModel):
    """memory_conflict 行模型（data-model.md「记忆冲突标记」）。"""

    id: UUID
    workspace_id: str
    memory_id_a: str  # 既有记忆
    memory_id_b: str  # 新写入记忆
    layer: str  # group / biz / org
    chat_id: str | None = None
    status: ConflictStatus = ConflictStatus.OPEN
    resolution: str | None = None
    created_at: datetime | None = None


def _row_to_conflict(row: asyncpg.Record) -> MemoryConflict:
    return MemoryConflict(**dict(row))


class MemoryConflictRepository(BaseRepository):
    """memory_conflict 读写。$1 固定为 workspace_id（基类强制）。"""

    def __init__(self, pool: Pool, workspace_id: str | None = None) -> None:
        super().__init__(pool, workspace_id)

    async def create(
        self,
        *,
        memory_id_a: str,
        memory_id_b: str,
        layer: str,
        chat_id: str | None = None,
    ) -> MemoryConflict:
        """登记一对矛盾；同一对只记一次（ux_memory_conflict_pair），冲突回读已存在行。"""
        row = await self.fetchrow(
            """
            INSERT INTO memory_conflict (id, workspace_id, memory_id_a, memory_id_b, layer, chat_id)
            VALUES ($2, $1, $3, $4, $5, $6)
            ON CONFLICT (workspace_id, memory_id_a, memory_id_b) DO NOTHING
            RETURNING *
            """,
            uuid4(),
            memory_id_a,
            memory_id_b,
            layer,
            chat_id,
        )
        if row is not None:
            return _row_to_conflict(row)
        existing = await self.fetchrow(
            """
            SELECT * FROM memory_conflict
             WHERE workspace_id = $1 AND memory_id_a = $2 AND memory_id_b = $3
            """,
            memory_id_a,
            memory_id_b,
        )
        if existing is None:  # pragma: no cover - 唯一索引冲突后必可读回
            raise RuntimeError("memory_conflict 冲突后回读失败")
        return _row_to_conflict(existing)

    async def get(self, conflict_id: UUID) -> MemoryConflict | None:
        row = await self.fetchrow(
            "SELECT * FROM memory_conflict WHERE workspace_id = $1 AND id = $2",
            conflict_id,
        )
        return _row_to_conflict(row) if row else None

    async def list_open(self) -> list[MemoryConflict]:
        rows = await self.fetch(
            """
            SELECT * FROM memory_conflict
             WHERE workspace_id = $1 AND status = 'OPEN'
             ORDER BY created_at
            """,
        )
        return [_row_to_conflict(r) for r in rows]

    async def resolve(self, conflict_id: UUID, resolution: str) -> MemoryConflict | None:
        """人工裁决：OPEN → RESOLVED，记录裁决结论。"""
        row = await self.fetchrow(
            """
            UPDATE memory_conflict SET status = 'RESOLVED', resolution = $3
             WHERE workspace_id = $1 AND id = $2 AND status = 'OPEN'
            RETURNING *
            """,
            conflict_id,
            resolution,
        )
        return _row_to_conflict(row) if row else None


class ConflictMarker:
    """冲突标记写入面：登记台账 + 提示人工裁决（卡片经 Outbox，可选）。"""

    def __init__(
        self,
        repo: MemoryConflictRepository,
        *,
        outbox_submit: Callable[[Envelope], Awaitable[Any]] | None = None,
    ) -> None:
        self._repo = repo
        self._outbox_submit = outbox_submit

    async def mark(
        self,
        memory_id_a: str,
        memory_id_b: str,
        *,
        layer: str,
        chat_id: str | None = None,
    ) -> MemoryConflict:
        """两条记忆均保留，仅创建冲突标记并提示人工裁决（US8-8）。"""
        conflict = await self._repo.create(
            memory_id_a=memory_id_a,
            memory_id_b=memory_id_b,
            layer=layer,
            chat_id=chat_id,
        )
        await self._notify(conflict)
        return conflict

    async def _notify(self, conflict: MemoryConflict) -> None:
        if self._outbox_submit is None or not conflict.chat_id:
            return
        try:
            await self._outbox_submit(
                Envelope(
                    chat_id=conflict.chat_id,
                    msg_type=MsgType.TEMPLATE_CARD,
                    priority=Priority.URGENT,
                    payload={
                        "card_type": "memory_conflict",
                        "conflict_id": str(conflict.id),
                        "memory_id_a": conflict.memory_id_a,
                        "memory_id_b": conflict.memory_id_b,
                        "text": "检测到相互矛盾的记忆，两条均已保留，请人工裁决。",
                    },
                )
            )
        except Exception:  # noqa: BLE001 - 通知失败不影响台账事实
            logger.exception("memory conflict notify failed (conflict=%s)", conflict.id)
