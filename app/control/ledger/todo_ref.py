"""todo_ref：只存归属与升级计数，不存待办副本（US11-1）。

本地只登记 wecom_todo_id 与群/事项/来源的归属关系及 remind_count；
内容、参与人、截止时间、完成状态一律经 TodoPort.get_batch 实时回读，
因此不存在两侧状态不一致。原生侧已完成 → ARCHIVED（保留可查，US11-4）；
不可见/已删除 → INVALID（停止提醒，不反复报错，US11-5）。
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum

import asyncpg
from pydantic import BaseModel

from app.platform.db.pool import Pool
from app.platform.db.repository import BaseRepository


class TodoRefStatus(StrEnum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"  # 已完成归档，保留可查（US11-4）
    INVALID = "INVALID"  # 原生侧不可见/已删除（US11-5）


class TodoRef(BaseModel):
    """一条待办归属记录：归属 + 升级计数，无任何详情副本（US11-1）。"""

    wecom_todo_id: str
    chat_id: str
    topic_id: str | None = None
    source: str
    remind_count: int = 0
    last_remind_at: datetime | None = None
    status: TodoRefStatus = TodoRefStatus.ACTIVE


def _row_to_ref(row: asyncpg.Record) -> TodoRef:
    return TodoRef(
        wecom_todo_id=row["wecom_todo_id"],
        chat_id=row["chat_id"],
        topic_id=row["topic_id"],
        source=row["source"],
        remind_count=int(row["remind_count"]),
        last_remind_at=row["last_remind_at"],
        status=TodoRefStatus(row["status"]),
    )


class TodoRefRepository(BaseRepository):
    """todo_ref 读写。$1 固定为 workspace_id（基类强制）。"""

    def __init__(self, pool: Pool, workspace_id: str | None = None) -> None:
        super().__init__(pool, workspace_id)

    async def register(
        self,
        wecom_todo_id: str,
        chat_id: str,
        *,
        topic_id: str | None = None,
        source: str,
    ) -> TodoRef:
        """登记归属；重复登记只刷新归属字段，不动提醒计数与状态。"""
        row = await self.fetchrow(
            """
            INSERT INTO todo_ref (workspace_id, wecom_todo_id, chat_id, topic_id, source)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (workspace_id, wecom_todo_id) DO UPDATE
            SET chat_id = EXCLUDED.chat_id, topic_id = EXCLUDED.topic_id,
                source = EXCLUDED.source
            RETURNING wecom_todo_id, chat_id, topic_id, source,
                      remind_count, last_remind_at, status
            """,
            wecom_todo_id,
            chat_id,
            topic_id,
            source,
        )
        return _row_to_ref(row)

    async def get(self, wecom_todo_id: str) -> TodoRef | None:
        row = await self.fetchrow(
            """
            SELECT wecom_todo_id, chat_id, topic_id, source,
                   remind_count, last_remind_at, status
              FROM todo_ref
             WHERE workspace_id = $1 AND wecom_todo_id = $2
            """,
            wecom_todo_id,
        )
        return _row_to_ref(row) if row else None

    async def list_active(self) -> list[TodoRef]:
        """全部 ACTIVE 归属记录（回读跟进的扫描面）。"""
        rows = await self.fetch(
            """
            SELECT wecom_todo_id, chat_id, topic_id, source,
                   remind_count, last_remind_at, status
              FROM todo_ref
             WHERE workspace_id = $1 AND status = 'ACTIVE'
             ORDER BY created_at
            """,
        )
        return [_row_to_ref(r) for r in rows]

    async def record_remind(self, wecom_todo_id: str, cycle_started_at: datetime) -> bool:
        """记一次提醒：计数 +1、last_remind_at = now()。

        条件更新保证同一跟进周期（cycle_started_at 标识）内只生效一次
        （US11-7）：本周期已提醒过的行不匹配、返回 False。
        """
        result = await self.execute(
            """
            UPDATE todo_ref
               SET remind_count = remind_count + 1, last_remind_at = now()
             WHERE workspace_id = $1 AND wecom_todo_id = $2 AND status = 'ACTIVE'
               AND (last_remind_at IS NULL OR last_remind_at < $3)
            """,
            wecom_todo_id,
            cycle_started_at,
        )
        return result.endswith(" 1")

    async def archive(self, wecom_todo_id: str) -> bool:
        """已完成归档（US11-4）：ACTIVE → ARCHIVED，保留可查。返回是否生效。"""
        return await self._transition(wecom_todo_id, TodoRefStatus.ARCHIVED)

    async def invalidate(self, wecom_todo_id: str) -> bool:
        """原生侧不可见/已删除（US11-5）：ACTIVE → INVALID，停止提醒。返回是否生效。"""
        return await self._transition(wecom_todo_id, TodoRefStatus.INVALID)

    async def _transition(self, wecom_todo_id: str, target: TodoRefStatus) -> bool:
        result = await self.execute(
            """
            UPDATE todo_ref
               SET status = $3
             WHERE workspace_id = $1 AND wecom_todo_id = $2 AND status = 'ACTIVE'
            """,
            wecom_todo_id,
            target.value,
        )
        return result.endswith(" 1")
