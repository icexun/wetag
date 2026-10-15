"""待审批操作：pending_approval 状态迁移与审批生命周期（§10.4，plan Decision 2）。

状态机：PENDING ─批准→ APPROVED ─重放消费→ CONSUMED；PENDING ─拒绝→ REJECTED；
PENDING ─超时扫描→ EXPIRED；CONSUMED ─执行失败回滚→ APPROVED（release，失败补偿）。所有迁移用条件更新 `UPDATE ... WHERE status=<前态>`
按影响行数判定抢占，不做应用层 CAS（US7-7）；expires_at 为绝对时间，
进程重启后剩余超时连续计算（US7-8）。

外部写幂等（US7-9）：重试前先核对 runtime_state 的幂等记录与外部对象
实际状态，已完成则不重发，不盲目重复创建。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping
from uuid import UUID, uuid4

import asyncpg
from pydantic import BaseModel

from app.platform.db.pool import Pool
from app.platform.db.repository import BaseRepository

# 审批默认超时：30 分钟，业务线管理员可配（APPROVAL_TIMEOUT_MINUTES，US7-6）
APPROVAL_TIMEOUT_MINUTES = 30


class ApprovalStatus(StrEnum):
    """pending_approval 状态机（data-model.md「待审批操作」）。"""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CONSUMED = "CONSUMED"


class PendingApproval(BaseModel):
    """pending_approval 行模型。"""

    id: UUID
    workspace_id: str
    task_id: UUID
    chat_id: str
    topic_id: str | None = None
    tool_name: str
    tool_call_digest: str
    tool_call_payload: dict[str, Any]
    risk_level: str = "high_write"
    status: ApprovalStatus = ApprovalStatus.PENDING
    decided_by: str | None = None
    decided_at: datetime | None = None
    expires_at: datetime
    card_msg_id: str | None = None
    created_at: datetime | None = None


def call_digest(tool_name: str, tool_input: Mapping[str, Any] | None) -> str:
    """工具调用指纹：工具名 + 规范化参数的 sha256，重放时据此匹配。"""
    canonical = json.dumps(
        {"tool": tool_name, "input": dict(tool_input or {})},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _row_to_approval(row: asyncpg.Record) -> PendingApproval:
    data = dict(row)
    if isinstance(data.get("tool_call_payload"), str):
        data["tool_call_payload"] = json.loads(data["tool_call_payload"])
    return PendingApproval(**data)


class PendingApprovalRepository(BaseRepository):
    """pending_approval 读写。$1 固定为 workspace_id（基类强制）。"""

    def __init__(self, pool: Pool, workspace_id: str | None = None) -> None:
        super().__init__(pool, workspace_id)

    async def create(
        self,
        *,
        task_id: UUID,
        chat_id: str,
        tool_name: str,
        tool_call_digest: str,
        tool_call_payload: Mapping[str, Any],
        expires_at: datetime,
        topic_id: str | None = None,
        risk_level: str = "high_write",
    ) -> PendingApproval:
        """挂起一条高危写；同一任务同一调用只挂起一次（ux_pending_approval_call），
        冲突时返回已存在行。"""
        row = await self.fetchrow(
            """
            INSERT INTO pending_approval (id, workspace_id, task_id, chat_id, topic_id,
                                          tool_name, tool_call_digest, tool_call_payload,
                                          risk_level, expires_at)
            VALUES ($2, $1, $3, $4, $5, $6, $7, $8::jsonb, $9, $10)
            ON CONFLICT (task_id, tool_call_digest) DO NOTHING
            RETURNING *
            """,
            uuid4(),
            task_id,
            chat_id,
            topic_id,
            tool_name,
            tool_call_digest,
            dict(tool_call_payload),
            risk_level,
            expires_at,
        )
        if row is not None:
            return _row_to_approval(row)
        existing = await self.find_by_call(task_id, tool_call_digest)
        if existing is None:  # pragma: no cover - 唯一索引冲突后必可读回
            raise RuntimeError("pending_approval 冲突后回读失败")
        return existing

    async def get(self, approval_id: UUID) -> PendingApproval | None:
        row = await self.fetchrow(
            "SELECT * FROM pending_approval WHERE workspace_id = $1 AND id = $2",
            approval_id,
        )
        return _row_to_approval(row) if row else None

    async def find_by_call(
        self, task_id: UUID, tool_call_digest: str
    ) -> PendingApproval | None:
        row = await self.fetchrow(
            """
            SELECT * FROM pending_approval
             WHERE workspace_id = $1 AND task_id = $2 AND tool_call_digest = $3
            """,
            task_id,
            tool_call_digest,
        )
        return _row_to_approval(row) if row else None

    async def set_card_msg_id(self, approval_id: UUID, card_msg_id: str) -> None:
        await self.execute(
            """
            UPDATE pending_approval SET card_msg_id = $3
             WHERE workspace_id = $1 AND id = $2
            """,
            approval_id,
            card_msg_id,
        )

    async def approve(
        self, approval_id: UUID, decided_by: str
    ) -> PendingApproval | None:
        """PENDING 且未过期 → APPROVED。已决或已过期时影响行数 0，返回 None（US7-6/7）。"""
        row = await self.fetchrow(
            """
            UPDATE pending_approval
               SET status = 'APPROVED', decided_by = $3, decided_at = now()
             WHERE workspace_id = $1 AND id = $2
               AND status = 'PENDING' AND expires_at > now()
            RETURNING *
            """,
            approval_id,
            decided_by,
        )
        return _row_to_approval(row) if row else None

    async def reject(
        self, approval_id: UUID, decided_by: str
    ) -> PendingApproval | None:
        """PENDING 且未过期 → REJECTED；过期后点击不生效（US7-6）。"""
        row = await self.fetchrow(
            """
            UPDATE pending_approval
               SET status = 'REJECTED', decided_by = $3, decided_at = now()
             WHERE workspace_id = $1 AND id = $2
               AND status = 'PENDING' AND expires_at > now()
            RETURNING *
            """,
            approval_id,
            decided_by,
        )
        return _row_to_approval(row) if row else None

    async def consume(self, approval_id: UUID) -> PendingApproval | None:
        """APPROVED → CONSUMED 一次性消费：影响行数 0 即已被消费（US7-7）。"""
        row = await self.fetchrow(
            """
            UPDATE pending_approval SET status = 'CONSUMED'
             WHERE workspace_id = $1 AND id = $2 AND status = 'APPROVED'
            RETURNING *
            """,
            approval_id,
        )
        return _row_to_approval(row) if row else None

    async def release(self, approval_id: UUID) -> PendingApproval | None:
        """CONSUMED → APPROVED 消费回滚：放行后工具执行失败时退还占位，
        使任务重试可重新放行，避免 CONSUMED 态永久挂起（US7-7 失败补偿）。"""
        row = await self.fetchrow(
            """
            UPDATE pending_approval SET status = 'APPROVED'
             WHERE workspace_id = $1 AND id = $2 AND status = 'CONSUMED'
            RETURNING *
            """,
            approval_id,
        )
        return _row_to_approval(row) if row else None

    async def expire_due(self, now: datetime | None = None) -> list[PendingApproval]:
        """超时扫描（US7-6）：PENDING 且 expires_at 到点 → EXPIRED，返回被处理的行。"""
        rows = await self.fetch(
            """
            UPDATE pending_approval SET status = 'EXPIRED'
             WHERE workspace_id = $1 AND status = 'PENDING'
               AND expires_at <= COALESCE($2::timestamptz, now())
            RETURNING *
            """,
            now,
        )
        return [_row_to_approval(r) for r in rows]


async def replay_approved_call(
    repo: PendingApprovalRepository,
    approval_id: UUID,
    execute: Callable[[PendingApproval], Awaitable[Any]],
) -> bool:
    """批准后重放：原子消费 APPROVED → CONSUMED，抢到才执行（US7-7）。

    返回 True 表示本次重放执行了外部写；False 表示已被消费或未批准，不重复执行。
    """
    consumed = await repo.consume(approval_id)
    if consumed is None:
        return False
    await execute(consumed)
    return True


# ---- 外部写幂等核对（US7-9） ----


def idem_state_key(key: str) -> str:
    return f"idem:{key}"


async def check_idempotency(
    state: Any,
    key: str,
    *,
    check_remote: Callable[[], Awaitable[bool]] | None = None,
) -> bool:
    """外部写重试前核对（US7-9）：本地幂等记录已完成，或外部对象实际已存在
    （check_remote 为真）时返回 True —— 不重发、不重复创建；并回补本地记录。"""
    if state is not None:
        record = await state.get(idem_state_key(key))
        if record and record.get("status") == "done":
            return True
    if check_remote is not None and await check_remote():
        if state is not None:
            await state.set(
                idem_state_key(key), {"status": "done", "source": "remote_check"}
            )
        return True
    return False


async def mark_idem_done(state: Any, key: str, *, result: Any = None) -> None:
    """外部写成功后落幂等记录，供后续重试核对（US7-9）。"""
    if state is None:
        return
    await state.set(
        idem_state_key(key),
        {"status": "done", "result": str(result) if result is not None else None},
    )
