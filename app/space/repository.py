"""chat_group / topic / topic_correction / org.chat_ownership 读写。

三张业务线私有表走当前进程的 schema；``org.chat_ownership`` 是唯一的跨业务线
共享表，只允许 ``SELECT`` 与 ``INSERT``。

本模块只做数据访问：登记语义在 ``ownership.py`` 与 ``registry.py``，事项状态
迁移语义在 ``topic/lifecycle.py``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence

from app.contracts.ids import ChatId, ChatType, TopicId, WorkspaceId
from app.platform.db import Database


@dataclass(frozen=True)
class ChatGroup:
    chat_id: ChatId
    workspace_id: WorkspaceId
    bot_id: str
    chat_type: ChatType
    display_name: Optional[str]
    first_actor_user_id: str
    activated_at: datetime
    last_inbound_at: datetime

    def within_session_window(self, hours: int) -> bool:
        """是否仍在企微的主动推送会话窗内。

        「需要用户在会话中给机器人发消息，后续机器人才能主动推送」是平台约束，
        跨窗能力待 M0 实测（待确认 #1）。
        """
        return datetime.now(timezone.utc) - self.last_inbound_at < timedelta(hours=hours)


@dataclass(frozen=True)
class Topic:
    topic_id: TopicId
    chat_id: ChatId
    title: str
    status: str
    ref_keys: List[str]
    known_msg_ids: List[str]
    created_at: datetime
    last_active_at: datetime


class SpaceRepository:
    def __init__(self, db: Database):
        self._db = db

    # ===== org.chat_ownership =====

    async def register_ownership(
        self,
        chat_id: ChatId,
        workspace_id: WorkspaceId,
        bot_id: str,
        chat_type: ChatType,
        actor_user_id: str,
    ) -> WorkspaceId:
        """原子登记群归属，返回该群实际所属的业务线。

        返回值不等于入参 ``workspace_id`` 即表示该群已被另一条业务线登记，
        判定与拒绝在 ``ownership.py``。
        """
        await self._db.execute(
            """
            INSERT INTO org.chat_ownership
                (chat_id, workspace_id, bot_id, chat_type, first_actor_user_id)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (chat_id) DO NOTHING
            """,
            chat_id,
            workspace_id,
            bot_id,
            chat_type.value,
            actor_user_id,
        )
        owner = await self._db.fetchval(
            "SELECT workspace_id FROM org.chat_ownership WHERE chat_id = $1",
            chat_id,
        )
        # 回读必然有值：要么本次插入成功，要么已存在他人登记。
        return owner

    # ===== chat_group =====

    async def upsert_chat_group(
        self,
        chat_id: ChatId,
        workspace_id: WorkspaceId,
        bot_id: str,
        chat_type: ChatType,
        actor_user_id: str,
    ) -> ChatGroup:
        """首次 @ 时登记群，此后每条入站消息刷新 ``last_inbound_at``。

        群登记不依赖「机器人被拉群事件」——那个事件不可靠，首次群内 @ 才是
        可靠起点。
        """
        row = await self._db.fetchrow(
            """
            INSERT INTO chat_group
                (chat_id, workspace_id, bot_id, chat_type, first_actor_user_id)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (chat_id) DO UPDATE SET last_inbound_at = now()
            RETURNING *
            """,
            chat_id,
            workspace_id,
            bot_id,
            chat_type.value,
            actor_user_id,
        )
        return _to_chat_group(row)

    async def get_chat_group(self, chat_id: ChatId) -> Optional[ChatGroup]:
        row = await self._db.fetchrow(
            "SELECT * FROM chat_group WHERE chat_id = $1", chat_id
        )
        return _to_chat_group(row) if row else None

    async def set_display_name(self, chat_id: ChatId, display_name: str) -> None:
        """群名只能由用户告知——机器人读不到群名（平台约束）。"""
        await self._db.execute(
            "UPDATE chat_group SET display_name = $2 WHERE chat_id = $1",
            chat_id,
            display_name,
        )

    # ===== topic =====

    async def create_topic(
        self,
        topic_id: TopicId,
        chat_id: ChatId,
        title: str,
        ref_keys: Sequence[str] = (),
        known_msg_ids: Sequence[str] = (),
    ) -> Topic:
        row = await self._db.fetchrow(
            """
            INSERT INTO topic (topic_id, chat_id, title, ref_keys, known_msg_ids)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING *
            """,
            topic_id,
            chat_id,
            title,
            list(ref_keys),
            list(known_msg_ids),
        )
        return _to_topic(row)

    async def get_topic(self, topic_id: TopicId) -> Optional[Topic]:
        row = await self._db.fetchrow(
            "SELECT * FROM topic WHERE topic_id = $1", topic_id
        )
        return _to_topic(row) if row else None

    async def find_topic_by_msg_id(
        self, chat_id: ChatId, msg_id: str
    ) -> Optional[Topic]:
        """L1：被引用消息所属的事项。"""
        row = await self._db.fetchrow(
            """
            SELECT * FROM topic
            WHERE chat_id = $1 AND $2 = ANY (known_msg_ids)
            ORDER BY last_active_at DESC
            LIMIT 1
            """,
            chat_id,
            msg_id,
        )
        return _to_topic(row) if row else None

    async def find_topic_by_outbound_text(
        self, chat_id: ChatId, text_prefix: str
    ) -> Optional[Topic]:
        """L1 兜底：引用帧不带被引用消息 msgid 时，用引用内容前缀匹配本群
        bot 出站消息（outbox 自带 topic_id），定位它所属的事项。"""
        row = await self._db.fetchrow(
            """
            SELECT t.* FROM topic t
            JOIN outbox o ON o.topic_id = t.topic_id
            WHERE o.chat_id = $1 AND starts_with(o.payload->>'text', $2)
            ORDER BY o.id DESC
            LIMIT 1
            """,
            chat_id,
            text_prefix,
        )
        return _to_topic(row) if row else None

    async def find_topic_by_ref_keys(
        self, chat_id: ChatId, ref_keys: Sequence[str]
    ) -> Optional[Topic]:
        """L2：命中 TAPD 编号、飞书链接等外部标识的事项。"""
        if not ref_keys:
            return None
        row = await self._db.fetchrow(
            """
            SELECT * FROM topic
            WHERE chat_id = $1 AND ref_keys && $2::text[]
            ORDER BY last_active_at DESC
            LIMIT 1
            """,
            chat_id,
            list(ref_keys),
        )
        return _to_topic(row) if row else None

    async def list_active_topics(self, chat_id: ChatId, limit: int) -> List[Topic]:
        rows = await self._db.fetch(
            """
            SELECT * FROM topic
            WHERE chat_id = $1 AND status = 'ACTIVE'
            ORDER BY last_active_at DESC
            LIMIT $2
            """,
            chat_id,
            limit,
        )
        return [_to_topic(row) for row in rows]

    async def touch_topic(
        self,
        topic_id: TopicId,
        msg_id: Optional[str] = None,
        ref_keys: Sequence[str] = (),
    ) -> None:
        """刷新活跃时间，并把本次消息与外部标识并入事项的命中键。"""
        await self._db.execute(
            """
            UPDATE topic
            SET last_active_at = now(),
                known_msg_ids = (
                    SELECT ARRAY(SELECT DISTINCT unnest(known_msg_ids || $2::text[]))
                ),
                ref_keys = (
                    SELECT ARRAY(SELECT DISTINCT unnest(ref_keys || $3::text[]))
                )
            WHERE topic_id = $1
            """,
            topic_id,
            [msg_id] if msg_id else [],
            list(ref_keys),
        )

    async def archive_idle_topics(
        self, chat_id: ChatId, idle_hours: int
    ) -> List[TopicId]:
        """归档超时无活动的事项，返回被归档的 topic_id。

        归档后的结论提炼与记忆写入归 Control，本方法只负责状态迁移。
        """
        rows = await self._db.fetch(
            """
            UPDATE topic
            SET status = 'ARCHIVED', archived_at = now()
            WHERE chat_id = $1
              AND status = 'ACTIVE'
              AND last_active_at < now() - make_interval(hours => $2)
            RETURNING topic_id
            """,
            chat_id,
            idle_hours,
        )
        return [row["topic_id"] for row in rows]

    async def reactivate_topic(self, topic_id: TopicId) -> None:
        """再次被明确提及时唤醒已归档事项。"""
        await self._db.execute(
            """
            UPDATE topic
            SET status = 'ACTIVE', archived_at = NULL, last_active_at = now()
            WHERE topic_id = $1
            """,
            topic_id,
        )

    # ===== topic_correction =====

    async def record_correction(
        self,
        chat_id: ChatId,
        actor_user_id: str,
        raw_text: str,
        from_topic_id: Optional[TopicId],
        to_topic_id: Optional[TopicId],
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO topic_correction
                (chat_id, from_topic_id, to_topic_id, actor_user_id, raw_text)
            VALUES ($1, $2, $3, $4, $5)
            """,
            chat_id,
            from_topic_id,
            to_topic_id,
            actor_user_id,
            raw_text,
        )


def _to_chat_group(row) -> ChatGroup:
    return ChatGroup(
        chat_id=row["chat_id"],
        workspace_id=row["workspace_id"],
        bot_id=row["bot_id"],
        chat_type=ChatType(row["chat_type"]),
        display_name=row["display_name"],
        first_actor_user_id=row["first_actor_user_id"],
        activated_at=row["activated_at"],
        last_inbound_at=row["last_inbound_at"],
    )


def _to_topic(row) -> Topic:
    return Topic(
        topic_id=row["topic_id"],
        chat_id=row["chat_id"],
        title=row["title"],
        status=row["status"],
        ref_keys=list(row["ref_keys"]),
        known_msg_ids=list(row["known_msg_ids"]),
        created_at=row["created_at"],
        last_active_at=row["last_active_at"],
    )
