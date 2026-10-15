"""低置信度记忆审批流（US8-5 / US8-6）：pending_memory 暂存与转正。

- 业务线级记忆置信度低于阈值、或组织级写入提案，都先落 pending_memory
  （PENDING）并发确认卡片；本表只留审批记录，不是记忆本体。
- 状态迁移用条件更新按影响行数判定抢占，重复点击只生效一次（同 US7-7 口径）。
- 批准后内容写入语义记忆轨并回填 memory_id；拒绝后不生效但保留拒绝记录。
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


class PendingMemoryStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class PendingMemory(BaseModel):
    """pending_memory 行模型（data-model.md「待审批记忆」）。"""

    id: UUID
    workspace_id: str
    target_scope: str  # biz / org
    content: str
    confidence: float
    source_chat_id: str | None = None
    source_topic_id: str | None = None
    status: PendingMemoryStatus = PendingMemoryStatus.PENDING
    decided_by: str | None = None
    decided_at: datetime | None = None
    memory_id: str | None = None
    created_at: datetime | None = None


def _row_to_pending(row: asyncpg.Record) -> PendingMemory:
    return PendingMemory(**dict(row))


class PendingMemoryRepository(BaseRepository):
    """pending_memory 读写。$1 固定为 workspace_id（基类强制）。"""

    def __init__(self, pool: Pool, workspace_id: str | None = None) -> None:
        super().__init__(pool, workspace_id)

    async def create(
        self,
        *,
        target_scope: str,
        content: str,
        confidence: float,
        source_chat_id: str | None = None,
        source_topic_id: str | None = None,
    ) -> PendingMemory:
        row = await self.fetchrow(
            """
            INSERT INTO pending_memory (id, workspace_id, target_scope, content,
                                        confidence, source_chat_id, source_topic_id)
            VALUES ($2, $1, $3, $4, $5, $6, $7)
            RETURNING *
            """,
            uuid4(),
            target_scope,
            content,
            confidence,
            source_chat_id,
            source_topic_id,
        )
        return _row_to_pending(row)

    async def get(self, pending_id: UUID) -> PendingMemory | None:
        row = await self.fetchrow(
            "SELECT * FROM pending_memory WHERE workspace_id = $1 AND id = $2",
            pending_id,
        )
        return _row_to_pending(row) if row else None

    async def approve(self, pending_id: UUID, decided_by: str) -> PendingMemory | None:
        """PENDING → APPROVED；已决时影响行数 0，返回 None（重复点击只生效一次）。"""
        row = await self.fetchrow(
            """
            UPDATE pending_memory
               SET status = 'APPROVED', decided_by = $3, decided_at = now()
             WHERE workspace_id = $1 AND id = $2 AND status = 'PENDING'
            RETURNING *
            """,
            pending_id,
            decided_by,
        )
        return _row_to_pending(row) if row else None

    async def reject(self, pending_id: UUID, decided_by: str) -> PendingMemory | None:
        """PENDING → REJECTED；拒绝记录保留可查（US8-5）。"""
        row = await self.fetchrow(
            """
            UPDATE pending_memory
               SET status = 'REJECTED', decided_by = $3, decided_at = now()
             WHERE workspace_id = $1 AND id = $2 AND status = 'PENDING'
            RETURNING *
            """,
            pending_id,
            decided_by,
        )
        return _row_to_pending(row) if row else None

    async def set_memory_id(self, pending_id: UUID, memory_id: str) -> None:
        await self.execute(
            """
            UPDATE pending_memory SET memory_id = $3
             WHERE workspace_id = $1 AND id = $2
            """,
            pending_id,
            memory_id,
        )


class PendingMemoryReview:
    """待审批记忆的提交与裁决。批准后写入语义记忆轨（biz 提案写业务线 collection）。

    org 提案批准后的组织级写入需组织管理员通道与组织 collection 写凭据，
    本类只留审批记录并回填标记，实际写入归控制台/联调（见 phase-notes）。
    """

    def __init__(
        self,
        repo: PendingMemoryRepository,
        *,
        backend: Any = None,
        collections: Any = None,
        outbox_submit: Callable[[Envelope], Awaitable[Any]] | None = None,
    ) -> None:
        self._repo = repo
        self._backend = backend
        self._collections = collections
        self._outbox_submit = outbox_submit

    async def submit(
        self,
        content: str,
        target_scope: str,
        *,
        confidence: float,
        chat_id: str | None = None,
        topic_id: str | None = None,
    ) -> PendingMemory:
        """暂存一条待审批记忆并发送确认卡片（US8-5）。"""
        pending = await self._repo.create(
            target_scope=target_scope,
            content=content,
            confidence=confidence,
            source_chat_id=chat_id,
            source_topic_id=topic_id,
        )
        await self._send_confirm_card(pending)
        return pending

    async def approve(self, pending_id: UUID, decided_by: str) -> PendingMemory | None:
        """批准：条件更新抢占成功后写入语义记忆轨并回填 memory_id。"""
        pending = await self._repo.approve(pending_id, decided_by)
        if pending is None:
            return None
        if pending.target_scope == "biz" and self._backend is not None and self._collections is not None:
            layers = self._collections.resolve()
            memory_id = await self._backend.upsert(
                layers.biz_collection,
                pending.content,
                metadata={
                    **layers.biz_filter(),
                    "topic_id": pending.source_topic_id,
                    "source_chat_id": pending.source_chat_id,
                },
            )
            await self._repo.set_memory_id(pending_id, memory_id)
            pending = await self._repo.get(pending_id)
        return pending

    async def reject(self, pending_id: UUID, decided_by: str) -> PendingMemory | None:
        """拒绝：内容不生效，拒绝记录与留痕保留（US8-5）。"""
        return await self._repo.reject(pending_id, decided_by)

    async def _send_confirm_card(self, pending: PendingMemory) -> None:
        if self._outbox_submit is None or not pending.source_chat_id:
            return
        try:
            await self._outbox_submit(
                Envelope(
                    chat_id=pending.source_chat_id,
                    topic_id=pending.source_topic_id,
                    msg_type=MsgType.TEMPLATE_CARD,
                    priority=Priority.URGENT,
                    payload={
                        "card_type": "memory_review",
                        "pending_memory_id": str(pending.id),
                        "target_scope": pending.target_scope,
                        "confidence": pending.confidence,
                        "text": f"待确认{pending.target_scope}级记忆（置信度 {pending.confidence:.2f}）：{pending.content}",
                    },
                )
            )
        except Exception:  # noqa: BLE001 - 卡片发送失败不影响暂存事实
            logger.exception("memory review card submit failed (pending=%s)", pending.id)
