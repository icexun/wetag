"""card_task / feedback_record 两表的读写。

这两张表是 Channel 拥有的全部存储。出站队列、任务表、审计表均归 Runtime。
绑定校验与 CAS 的判定语义在 ``cards/binding.py``，本模块只发 SQL。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence

from app.contracts.ids import ChatId, TopicId, WorkspaceId
from app.platform.db import Database


class CardStatus(str, Enum):
    ACTIVE = "ACTIVE"
    PROCESSING = "PROCESSING"
    DONE = "DONE"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class CardTask:
    task_id: str
    workspace_id: WorkspaceId
    bot_id: str
    chat_id: ChatId
    topic_id: TopicId
    allowed_actions: List[str]
    allowed_actors: List[str]
    status: CardStatus
    payload: Dict[str, Any]
    acted_by: Optional[str] = None
    acted_key: Optional[str] = None


class CardTaskRepository:
    def __init__(self, db: Database):
        self._db = db

    async def create(
        self,
        task_id: str,
        workspace_id: WorkspaceId,
        bot_id: str,
        chat_id: ChatId,
        topic_id: TopicId,
        allowed_actions: Sequence[str],
        allowed_actors: Sequence[str] = (),
        payload: Optional[Dict[str, Any]] = None,
        ttl_hours: int = 24,
    ) -> CardTask:
        row = await self._db.fetchrow(
            """
            INSERT INTO card_task
                (task_id, workspace_id, bot_id, chat_id, topic_id,
                 allowed_actions, allowed_actors, payload, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9)
            ON CONFLICT (task_id) DO UPDATE
            SET task_id = EXCLUDED.task_id
            WHERE card_task.workspace_id = EXCLUDED.workspace_id
              AND card_task.bot_id = EXCLUDED.bot_id
              AND card_task.chat_id = EXCLUDED.chat_id
              AND card_task.topic_id = EXCLUDED.topic_id
              AND card_task.allowed_actions = EXCLUDED.allowed_actions
              AND card_task.allowed_actors = EXCLUDED.allowed_actors
              AND card_task.payload = EXCLUDED.payload
            RETURNING *
            """,
            task_id,
            workspace_id,
            bot_id,
            chat_id,
            topic_id,
            list(allowed_actions),
            list(allowed_actors),
            payload or {},
            datetime.now(timezone.utc) + timedelta(hours=ttl_hours),
        )
        if row is None:
            # 同 task_id 却是另一份绑定，不可覆盖：否则攻击者可借重试改写按钮权限。
            raise ValueError(f"card task_id collision with different binding: {task_id}")
        return _to_card_task(row)

    async def get(self, task_id: str) -> Optional[CardTask]:
        row = await self._db.fetchrow(
            "SELECT * FROM card_task WHERE task_id = $1", task_id
        )
        return _to_card_task(row) if row else None

    async def cas_claim(
        self, task_id: str, actor_user_id: str, event_key: str
    ) -> Optional[CardTask]:
        """把 ACTIVE 卡片抢占为 PROCESSING，抢不到返回 None。

        卡片事件只推送一次且必须 5 秒内响应，因此状态迁移必须是单次往返的
        条件更新，不做「先查后写」。
        """
        row = await self._db.fetchrow(
            """
            UPDATE card_task
            SET status = 'PROCESSING',
                acted_by = $2,
                acted_key = $3,
                acted_at = now()
            WHERE task_id = $1 AND status = 'ACTIVE'
            RETURNING *
            """,
            task_id,
            actor_user_id,
            event_key,
        )
        return _to_card_task(row) if row else None

    async def finish(self, task_id: str, status: CardStatus) -> None:
        await self._db.execute(
            "UPDATE card_task SET status = $2 WHERE task_id = $1",
            task_id,
            status.value,
        )


class FeedbackRepository:
    def __init__(self, db: Database):
        self._db = db

    async def record(
        self,
        response_id: str,
        chat_id: ChatId,
        topic_id: Optional[TopicId],
        actor_user_id: str,
        result: str,
        reason: Sequence[str] = (),
        comment: Optional[str] = None,
    ) -> bool:
        """落一条反馈，返回是否为新记录。

        幂等靠 ``uq_feedback`` 唯一约束，重复反馈被数据库吞掉。
        """
        row = await self._db.fetchrow(
            """
            INSERT INTO feedback_record
                (response_id, chat_id, topic_id, actor_user_id, result, reason, comment)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT ON CONSTRAINT uq_feedback DO NOTHING
            RETURNING id
            """,
            response_id,
            chat_id,
            topic_id,
            actor_user_id,
            result,
            list(reason),
            comment,
        )
        return row is not None


def _to_card_task(row) -> CardTask:
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return CardTask(
        task_id=row["task_id"],
        workspace_id=row["workspace_id"],
        bot_id=row["bot_id"],
        chat_id=row["chat_id"],
        topic_id=row["topic_id"],
        allowed_actions=list(row["allowed_actions"]),
        allowed_actors=list(row["allowed_actors"]),
        status=CardStatus(row["status"]),
        payload=payload or {},
        acted_by=row["acted_by"],
        acted_key=row["acted_key"],
    )
