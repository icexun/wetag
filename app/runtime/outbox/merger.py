"""合并窗口：同 merge_key 聚合，窗口不因新事件延长（E-13，不降级）。

语义（plan「出站四道关卡」第 1 关）：
- 同 (chat_id, merge_key) 的窗口内内容并入幸存行的 payload.items（US5-1）
- 幸存行的 not_before 保持首次设定值，不因新内容顺延（US5-2）
- 每条被并入的内容仍落一行 status=MERGED，保留可查
- 并发同键提交用 pg_advisory_xact_lock 串行化，避免双双新建窗口
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.contracts.envelope import Envelope
from app.runtime.repository import OutboxMessage, OutboxRepository, _row_to_msg

DEFAULT_MERGE_WINDOW_SECONDS = 60.0


class Merger:
    """合并窗口（US5-1/US5-2）。URGENT 与无 merge_key 的消息不进本关（US5-3）。"""

    def __init__(
        self, repo: OutboxRepository, *, window_seconds: float = DEFAULT_MERGE_WINDOW_SECONDS
    ) -> None:
        self._repo = repo
        self.window_seconds = window_seconds

    async def merge_or_create(
        self, envelope: Envelope, *, now: datetime | None = None
    ) -> OutboxMessage:
        """同键窗口存在则并入并返回幸存行；否则新开窗口，not_before = now + window。"""
        now = now or datetime.now(UTC)
        ws = self._repo.workspace_id
        async with self._repo.short_tx() as conn:
            # 同键合并串行化：并发提交只允许一个创建窗口
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"outbox-merge:{ws}:{envelope.chat_id}:{envelope.merge_key}",
            )
            window = await conn.fetchrow(
                """
                SELECT * FROM outbox
                 WHERE workspace_id = $1 AND chat_id = $2 AND merge_key = $3
                   AND status = 'PENDING'
                 ORDER BY created_at
                 LIMIT 1
                 FOR UPDATE
                """,
                ws,
                envelope.chat_id,
                envelope.merge_key,
            )
            if window is None:
                row = await conn.fetchrow(
                    """
                    INSERT INTO outbox (id, workspace_id, chat_id, topic_id, merge_key,
                                        msg_type, priority, payload, update_of, status,
                                        not_before, task_id)
                    VALUES ($2, $1, $3, $4, $5, $6, $7, $8::jsonb, $9, 'PENDING', $10, $11)
                    RETURNING *
                    """,
                    ws,
                    uuid4(),
                    envelope.chat_id,
                    envelope.topic_id,
                    envelope.merge_key,
                    envelope.msg_type.value,
                    envelope.priority.value,
                    {"items": [envelope.payload]},
                    envelope.update_of,
                    now + timedelta(seconds=self.window_seconds),
                    envelope.task_id,
                )
                return _row_to_msg(row)
            # 并入幸存行：items 追加，not_before 不动（US5-2）
            survived = await conn.fetchrow(
                """
                UPDATE outbox
                   SET payload = jsonb_set(
                           payload, '{items}',
                           COALESCE(payload -> 'items', '[]'::jsonb) || $3::jsonb)
                 WHERE workspace_id = $1 AND id = $2
                RETURNING *
                """,
                ws,
                window["id"],
                [envelope.payload],
            )
            # 被并入的内容留一行 MERGED，保留可查
            await conn.execute(
                """
                INSERT INTO outbox (id, workspace_id, chat_id, topic_id, merge_key,
                                    msg_type, priority, payload, update_of, status,
                                    not_before, task_id)
                VALUES ($2, $1, $3, $4, $5, $6, $7, $8::jsonb, $9, 'MERGED', $10, $11)
                """,
                ws,
                uuid4(),
                envelope.chat_id,
                envelope.topic_id,
                envelope.merge_key,
                envelope.msg_type.value,
                envelope.priority.value,
                envelope.payload,
                envelope.update_of,
                window["not_before"],
                envelope.task_id,
            )
            return _row_to_msg(survived)
