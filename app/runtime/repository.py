"""task / outbox / audit_log / runtime_state 读写。

TaskRepository 约定（data-model.md「任务」）：
- 去重靠 ux_task_idem 唯一索引 + ON CONFLICT DO NOTHING，不做应用层先查后写（US3-2）
- 领取是 CAS：UPDATE ... WHERE status='PENDING'，影响行数 0 即放弃
- fail 由 SQL 原子完成 attempt+1 与 PENDING/DEAD 分流；requeue 不动 attempt（US1-7）

OutboxRepository 约定（data-model.md「出站队列」）：
- 状态机 PENDING / MERGED / SENT / SUSPENDED / DROPPED / FAILED（SUSPENDED 与 DROPPED 分列，US5-4/US5-5）
- not_before 是唯一的调度时刻：合并窗口、限流重排、发送退避都只改它，不新增状态
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

import asyncpg
from pydantic import BaseModel

from app.contracts.envelope import Envelope
from app.contracts.task import Task, TaskSpec, TaskStatus
from app.platform.db.pool import Pool
from app.platform.db.repository import BaseRepository


def _row_to_task(row: asyncpg.Record) -> Task:
    data = dict(row)
    if isinstance(data.get("payload"), str):
        data["payload"] = json.loads(data["payload"])
    return Task(**data)


class TaskRepository(BaseRepository):
    """task 表读写。$1 固定为 workspace_id（基类强制）。"""

    def __init__(self, pool: Pool, workspace_id: str | None = None) -> None:
        super().__init__(pool, workspace_id)

    async def create(self, spec: TaskSpec, *, trace_id: str | None = None) -> Task | None:
        """插入任务；idem_key 命中唯一索引时返回 None（已存在，丢弃重复）。"""
        row = await self.fetchrow(
            """
            INSERT INTO task (id, workspace_id, job_name, kind, trigger_source,
                              parent_id, idem_key, chat_id, topic_id, payload,
                              priority, status, max_attempt, scheduled_at, trace_id)
            VALUES ($2, $1, $3, $4, $5, $6, $7, $8, $9, $10::jsonb,
                    $11, 'PENDING', $12, COALESCE($13, now()), $14)
            ON CONFLICT (idem_key) WHERE idem_key IS NOT NULL DO NOTHING
            RETURNING *
            """,
            uuid4(),
            spec.job_name,
            spec.kind.value,
            spec.trigger_source.value,
            spec.parent_id,
            spec.idem_key,
            spec.chat_id,
            spec.topic_id,
            spec.payload,
            spec.priority,
            spec.max_attempt,
            spec.scheduled_at,
            trace_id,
        )
        return _row_to_task(row) if row else None

    async def get(self, task_id: UUID) -> Task | None:
        row = await self.fetchrow(
            "SELECT * FROM task WHERE workspace_id = $1 AND id = $2", task_id
        )
        return _row_to_task(row) if row else None

    async def get_by_idem_key(self, idem_key: str) -> Task | None:
        """按幂等键回读：调度补跑前判定该轮次任务是否已存在及其状态（US2-9）。"""
        row = await self.fetchrow(
            "SELECT * FROM task WHERE workspace_id = $1 AND idem_key = $2", idem_key
        )
        return _row_to_task(row) if row else None

    async def claim(self, task_id: UUID, lease_seconds: float) -> Task | None:
        """CAS 领取：PENDING → RUNNING + 租约。被领走返回 None。"""
        row = await self.fetchrow(
            """
            UPDATE task
               SET status = 'RUNNING', started_at = now(),
                   lease_expire_at = now() + ($3 * interval '1 second')
             WHERE workspace_id = $1 AND id = $2 AND status = 'PENDING'
            RETURNING *
            """,
            task_id,
            lease_seconds,
        )
        return _row_to_task(row) if row else None

    async def complete(
        self,
        task_id: UUID,
        *,
        progress: str | None = None,
        token_cost: int | None = None,
        rule_version: str | None = None,
    ) -> Task | None:
        row = await self.fetchrow(
            """
            UPDATE task
               SET status = 'SUCCEEDED', finished_at = now(), lease_expire_at = NULL,
                   error = NULL,
                   progress = COALESCE($3, progress),
                   token_cost = COALESCE($4, token_cost),
                   rule_version = COALESCE($5, rule_version)
             WHERE workspace_id = $1 AND id = $2 AND status = 'RUNNING'
            RETURNING *
            """,
            task_id,
            progress,
            token_cost,
            rule_version,
        )
        return _row_to_task(row) if row else None

    async def fail(self, task_id: UUID, error: str, backoff_seconds: float) -> Task | None:
        """失败分流：attempt+1；达上限进 DEAD，否则回 PENDING 并按退避推后 scheduled_at。"""
        row = await self.fetchrow(
            """
            UPDATE task
               SET attempt = attempt + 1,
                   error = $3,
                   status = CASE WHEN attempt + 1 >= max_attempt THEN 'DEAD' ELSE 'PENDING' END,
                   finished_at = CASE WHEN attempt + 1 >= max_attempt THEN now() ELSE finished_at END,
                   scheduled_at = now() + ($4 * interval '1 second'),
                   lease_expire_at = NULL
             WHERE workspace_id = $1 AND id = $2
            RETURNING *
            """,
            task_id,
            error,
            backoff_seconds,
        )
        return _row_to_task(row) if row else None

    async def requeue(
        self,
        task_id: UUID,
        delay_seconds: float,
        *,
        reason: str | None = None,
        payload_patch: dict | None = None,
    ) -> Task | None:
        """退回 PENDING 但不消耗 attempt：池饱和（US1-7）与存储不可达（US1-5/6）专用。

        payload_patch：合并进 payload（JSONB ||），用于审批恢复等场景打标记。
        """
        row = await self.fetchrow(
            """
            UPDATE task
               SET status = 'PENDING',
                   scheduled_at = now() + ($3 * interval '1 second'),
                   lease_expire_at = NULL,
                   error = COALESCE($4, error),
                   payload = CASE WHEN $5::jsonb IS NULL THEN payload
                                  ELSE payload || $5::jsonb END
             WHERE workspace_id = $1 AND id = $2
            RETURNING *
            """,
            task_id,
            delay_seconds,
            reason,
            payload_patch,  # jsonb codec 直接收 dict，勿 dumps（二次编码会变标量字符串）
        )
        return _row_to_task(row) if row else None

    async def wait_confirm(self, task_id: UUID) -> Task | None:
        """RUNNING → WAIT_CONFIRM：等审批，不计 attempt、不进重试（US7-3/US7-6）。"""
        row = await self.fetchrow(
            """
            UPDATE task SET status = 'WAIT_CONFIRM', lease_expire_at = NULL
             WHERE workspace_id = $1 AND id = $2 AND status = 'RUNNING'
            RETURNING *
            """,
            task_id,
        )
        return _row_to_task(row) if row else None

    async def mark_dead(self, task_id: UUID, error: str) -> Task | None:
        """直接终止（越权等禁重试场景），不消耗 attempt。"""
        row = await self.fetchrow(
            """
            UPDATE task
               SET status = 'DEAD', error = $3, finished_at = now(), lease_expire_at = NULL
             WHERE workspace_id = $1 AND id = $2
            RETURNING *
            """,
            task_id,
            error,
        )
        return _row_to_task(row) if row else None

    async def renew_lease(self, task_id: UUID, lease_seconds: float) -> bool:
        """续租：仅当仍在 RUNNING。返回是否续上。"""
        status = await self.execute(
            """
            UPDATE task SET lease_expire_at = now() + ($3 * interval '1 second')
             WHERE workspace_id = $1 AND id = $2 AND status = 'RUNNING'
            """,
            task_id,
            lease_seconds,
        )
        return status.endswith(" 1")

    async def reap_expired(self) -> list[Task]:
        """回收租约过期的 RUNNING 任务为 PENDING；attempt 保留不清零（US3-5）。"""
        rows = await self.fetch(
            """
            UPDATE task SET status = 'PENDING', lease_expire_at = NULL
             WHERE workspace_id = $1 AND status = 'RUNNING' AND lease_expire_at < now()
            RETURNING *
            """
        )
        return [_row_to_task(r) for r in rows]

    async def pending_tasks(self) -> list[Task]:
        """启动恢复用：全部 PENDING，按优先级与可执行时刻排序（ix_task_poll 路径）。"""
        rows = await self.fetch(
            """
            SELECT * FROM task
             WHERE workspace_id = $1 AND status = 'PENDING'
             ORDER BY priority, scheduled_at
            """
        )
        return [_row_to_task(r) for r in rows]

    async def count_by_status(self, status: TaskStatus) -> int:
        return int(
            await self.fetchval(
                "SELECT count(*) FROM task WHERE workspace_id = $1 AND status = $2",
                status.value,
            )
        )


# ---- Outbox ----


class OutboxStatus(StrEnum):
    """outbox 状态机（data-model.md「出站队列」）。"""

    PENDING = "PENDING"
    MERGED = "MERGED"  # 已并入同 merge_key 的幸存行
    SENT = "SENT"
    SUSPENDED = "SUSPENDED"  # 超会话窗待补发（US5-5）
    DROPPED = "DROPPED"  # 永不补发（US5-4 / 保留期过期）
    FAILED = "FAILED"  # 发送重试达上限（US5-8）


class DropReason(StrEnum):
    NOT_ACTIVATED = "not_activated"  # 群从未激活（US5-4）
    RETENTION_EXPIRED = "retention_expired"  # 挂起超保留期（US5-5）
    INVALID_MSG_TYPE = "invalid_msg_type"  # 形态非法（US5-7）


class OutboxMessage(BaseModel):
    """outbox 行模型。payload 为 jsonb；合并窗口行的 payload 形如 {"items": [...]}。"""

    id: UUID
    workspace_id: str
    chat_id: str
    topic_id: str | None = None
    merge_key: str | None = None
    msg_type: str
    priority: str
    payload: dict[str, Any]
    update_of: str | None = None
    status: OutboxStatus
    not_before: datetime
    suspended_at: datetime | None = None
    attempt: int = 0
    drop_reason: str | None = None
    sent_msg_id: str | None = None
    task_id: UUID | None = None
    created_at: datetime


def _row_to_msg(row: asyncpg.Record) -> OutboxMessage:
    data = dict(row)
    if isinstance(data.get("payload"), str):
        data["payload"] = json.loads(data["payload"])
    return OutboxMessage(**data)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class OutboxRepository(BaseRepository):
    """outbox 表读写。$1 固定为 workspace_id（基类强制）。"""

    def __init__(self, pool: Pool, workspace_id: str | None = None) -> None:
        super().__init__(pool, workspace_id)

    async def create(
        self,
        envelope: Envelope,
        *,
        not_before: datetime,
        status: OutboxStatus = OutboxStatus.PENDING,
        payload: dict[str, Any] | None = None,
    ) -> OutboxMessage:
        row = await self.fetchrow(
            """
            INSERT INTO outbox (id, workspace_id, chat_id, topic_id, merge_key,
                                msg_type, priority, payload, update_of, status,
                                not_before, task_id)
            VALUES ($2, $1, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11, $12)
            RETURNING *
            """,
            uuid4(),
            envelope.chat_id,
            envelope.topic_id,
            envelope.merge_key,
            envelope.msg_type.value,
            envelope.priority.value,
            payload if payload is not None else envelope.payload,
            envelope.update_of,
            status.value,
            not_before,
            envelope.task_id,
        )
        return _row_to_msg(row)

    async def get(self, msg_id: UUID) -> OutboxMessage | None:
        row = await self.fetchrow(
            "SELECT * FROM outbox WHERE workspace_id = $1 AND id = $2", msg_id
        )
        return _row_to_msg(row) if row else None

    async def due(self, now: datetime, *, limit: int = 100) -> list[OutboxMessage]:
        """到期可派发的 PENDING 行（ix_outbox_due 路径）。"""
        rows = await self.fetch(
            """
            SELECT * FROM outbox
             WHERE workspace_id = $1 AND status = 'PENDING' AND not_before <= $2
             ORDER BY not_before
             LIMIT $3
            """,
            now,
            limit,
        )
        return [_row_to_msg(r) for r in rows]

    async def mark_sent(self, msg_id: UUID, sent_msg_id: str) -> OutboxMessage | None:
        row = await self.fetchrow(
            """
            UPDATE outbox SET status = 'SENT', sent_msg_id = $3
             WHERE workspace_id = $1 AND id = $2 AND status = 'PENDING'
            RETURNING *
            """,
            msg_id,
            sent_msg_id,
        )
        return _row_to_msg(row) if row else None

    async def mark_failed(
        self, msg_id: UUID, error: str, *, attempt: int | None = None
    ) -> OutboxMessage | None:
        row = await self.fetchrow(
            """
            UPDATE outbox SET status = 'FAILED', drop_reason = $3,
                   attempt = COALESCE($4, attempt)
             WHERE workspace_id = $1 AND id = $2
            RETURNING *
            """,
            msg_id,
            error,
            attempt,
        )
        return _row_to_msg(row) if row else None

    async def mark_dropped(self, msg_id: UUID, reason: DropReason) -> OutboxMessage | None:
        row = await self.fetchrow(
            """
            UPDATE outbox SET status = 'DROPPED', drop_reason = $3
             WHERE workspace_id = $1 AND id = $2
            RETURNING *
            """,
            msg_id,
            reason.value,
        )
        return _row_to_msg(row) if row else None

    async def mark_suspended(self, msg_id: UUID, suspended_at: datetime) -> OutboxMessage | None:
        row = await self.fetchrow(
            """
            UPDATE outbox SET status = 'SUSPENDED', suspended_at = $3
             WHERE workspace_id = $1 AND id = $2 AND status = 'PENDING'
            RETURNING *
            """,
            msg_id,
            suspended_at,
        )
        return _row_to_msg(row) if row else None

    async def reschedule(
        self, msg_id: UUID, not_before: datetime, *, attempt: int | None = None
    ) -> OutboxMessage | None:
        """重排到下一可发时刻：限流（US5-6）与发送退避（US5-8）共用，不动状态。"""
        row = await self.fetchrow(
            """
            UPDATE outbox
               SET not_before = $3,
                   attempt = COALESCE($4, attempt)
             WHERE workspace_id = $1 AND id = $2 AND status = 'PENDING'
            RETURNING *
            """,
            msg_id,
            not_before,
            attempt,
        )
        return _row_to_msg(row) if row else None

    async def suspended_for_chat(self, chat_id: str) -> list[OutboxMessage]:
        """群内全部挂起行（ix_outbox_suspended 路径），供下次入站补发判定。"""
        rows = await self.fetch(
            """
            SELECT * FROM outbox
             WHERE workspace_id = $1 AND chat_id = $2 AND status = 'SUSPENDED'
             ORDER BY suspended_at
            """,
            chat_id,
        )
        return [_row_to_msg(r) for r in rows]

    async def resume(self, msg_id: UUID, not_before: datetime) -> OutboxMessage | None:
        """SUSPENDED → PENDING：群内下次入站时补发（US5-5）。"""
        row = await self.fetchrow(
            """
            UPDATE outbox SET status = 'PENDING', not_before = $3
             WHERE workspace_id = $1 AND id = $2 AND status = 'SUSPENDED'
            RETURNING *
            """,
            msg_id,
            not_before,
        )
        return _row_to_msg(row) if row else None

    async def count_by_status(self, status: OutboxStatus) -> int:
        return int(
            await self.fetchval(
                "SELECT count(*) FROM outbox WHERE workspace_id = $1 AND status = $2",
                status.value,
            )
        )
