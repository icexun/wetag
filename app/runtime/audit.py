"""audit_log 唯一落点（F-3，全模块经此写入，不得自建表或直接 INSERT）。

只存参数摘要与结果摘要，不存明文凭据与完整正文（NFR-008 / US3-7）。
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from app.contracts.errors import payload_digest
from app.platform.db.pool import Pool
from app.platform.db.repository import BaseRepository

# audit_log.action 的 CHECK 约束闭集
ACTIONS = frozenset({"tool_call", "memory_write", "external_req", "approval"})
OUTCOMES = frozenset({"success", "failure", "rejected", "pending"})


def digest(value: Any, max_len: int = 200) -> str:
    """参数/结果摘要：截断 + 长度标注，不落完整正文。"""
    return payload_digest(value, max_len)


class AuditWriter:
    """全模块唯一审计写入面。写入失败不拖垮业务路径（记日志）。"""

    def __init__(self, pool: Pool, workspace_id: str | None = None) -> None:
        self._repo = BaseRepository(pool, workspace_id)
        self.workspace_id = self._repo.workspace_id

    async def write(
        self,
        *,
        action: str,
        outcome: str,
        target: str | None = None,
        actor: str | None = None,
        trace_id: str | None = None,
        task_id: UUID | None = None,
        chat_id: str | None = None,
        topic_id: str | None = None,
        params: Any = None,
        result: Any = None,
        rule_version: str | None = None,
        approval_id: UUID | None = None,
    ) -> None:
        if action not in ACTIONS:
            raise ValueError(f"非法 audit action: {action!r}（闭集 {sorted(ACTIONS)}）")
        if outcome not in OUTCOMES:
            raise ValueError(f"非法 audit outcome: {outcome!r}")
        await self._repo.execute(
            """
            INSERT INTO audit_log (workspace_id, trace_id, task_id, chat_id, topic_id,
                                   actor, action, target, params_digest, result_digest,
                                   outcome, rule_version, approval_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            """,
            trace_id,
            task_id,
            chat_id,
            topic_id,
            actor,
            action,
            target,
            digest(params) if params is not None else None,
            digest(result) if result is not None else None,
            outcome,
            rule_version,
            approval_id,
        )
