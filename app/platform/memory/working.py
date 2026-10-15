"""PG 持久化工作记忆（agentscope ``MemoryBase`` 实现）。

替代进程内 ``InMemoryMemory``：进程重启后对话上下文仍在，多轮追问不再断片。
行为语义逐条对齐 InMemoryMemory（去重、marks 过滤、压缩摘要前置），
存储走业务线专属 schema（连接 search_path 已限定，表名不带 schema 前缀）。

压缩摘要持久化在 meta 表：``update_compressed_summary`` 同步落库，
``get_memory`` 时随消息一同取回。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from agentscope.memory._working_memory._base import MemoryBase
from agentscope.message import Msg

logger = logging.getLogger(__name__)


class PgMemory(MemoryBase):
    """按 ``thread_id`` 隔离的 PG 工作记忆。一线程一实例，由工厂创建。"""

    def __init__(self, db: Any, thread_id: str) -> None:
        super().__init__()
        self._db = db
        self._thread_id = thread_id
        self._summary_loaded = False

    async def add(
        self,
        memories: Msg | list[Msg] | None,
        marks: str | list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        if memories is None:
            return
        if isinstance(memories, Msg):
            memories = [memories]
        if marks is None:
            mark_list: list[str] = []
        elif isinstance(marks, str):
            mark_list = [marks]
        else:
            mark_list = list(marks)
        for msg in memories:
            # ON CONFLICT 对齐 InMemoryMemory 的按 id 去重
            await self._db.execute(
                """
                INSERT INTO agent_working_memory (thread_id, msg_id, marks, msg)
                VALUES ($1, $2, $3, $4::jsonb)
                ON CONFLICT (thread_id, msg_id) DO NOTHING
                """,
                self._thread_id,
                msg.id,
                mark_list,
                json.dumps(msg.to_dict(), ensure_ascii=False),
            )

    async def delete(self, msg_ids: list[str], **kwargs: Any) -> int:
        if not msg_ids:
            return 0
        result = await self._db.execute(
            "DELETE FROM agent_working_memory WHERE thread_id = $1 AND msg_id = ANY($2)",
            self._thread_id,
            list(msg_ids),
        )
        return _affected(result)

    async def update_msg(self, msg_id: str, msg: Msg, **kwargs: Any) -> None:
        """原地更新消息内容（修补空 tool_result 等历史污染，不改变消息顺序）。"""
        await self._db.execute(
            "UPDATE agent_working_memory SET msg = $3::jsonb"
            " WHERE thread_id = $1 AND msg_id = $2",
            self._thread_id,
            msg_id,
            json.dumps(msg.to_dict(), ensure_ascii=False),
        )

    async def delete_by_mark(self, mark: str | list[str], **kwargs: Any) -> int:
        marks = [mark] if isinstance(mark, str) else list(mark)
        result = await self._db.execute(
            "DELETE FROM agent_working_memory WHERE thread_id = $1 AND marks && $2",
            self._thread_id,
            marks,
        )
        return _affected(result)

    async def size(self) -> int:
        return int(
            await self._db.fetchval(
                "SELECT count(*) FROM agent_working_memory WHERE thread_id = $1",
                self._thread_id,
            )
        )

    async def clear(self) -> None:
        await self._db.execute(
            "DELETE FROM agent_working_memory WHERE thread_id = $1", self._thread_id
        )
        await self._db.execute(
            "DELETE FROM agent_working_memory_meta WHERE thread_id = $1",
            self._thread_id,
        )
        self._compressed_summary = ""

    async def get_memory(
        self,
        mark: str | None = None,
        exclude_mark: str | None = None,
        prepend_summary: bool = True,
        **kwargs: Any,
    ) -> list[Msg]:
        sql = "SELECT msg FROM agent_working_memory WHERE thread_id = $1"
        args: list[Any] = [self._thread_id]
        if mark is not None:
            args.append([mark])
            sql += f" AND marks @> ${len(args)}"
        if exclude_mark is not None:
            args.append([exclude_mark])
            sql += f" AND NOT marks @> ${len(args)}"
        sql += " ORDER BY id"
        rows = await self._db.fetch(sql, *args)
        messages = [Msg.from_dict(json.loads(r["msg"])) for r in rows]
        if prepend_summary:
            summary = await self._load_summary()
            if summary:
                return [Msg("user", summary, "user"), *messages]
        return messages

    async def update_messages_mark(
        self,
        new_mark: str | None,
        old_mark: str | None = None,
        msg_ids: list[str] | None = None,
    ) -> int:
        # 语义对齐 InMemoryMemory：new_mark=None 表示移除 old_mark；
        # 否则替换 old_mark（无 old_mark 约束则追加 new_mark）
        sql = "UPDATE agent_working_memory SET marks = "
        args: list[Any] = [self._thread_id]
        if new_mark is None:
            sql += "array_remove(marks, $2)"
            args.append(old_mark)
        elif old_mark is not None:
            sql += "array_append(array_remove(marks, $2), $3)"
            args.extend([old_mark, new_mark])
        else:
            sql += "CASE WHEN marks @> $2 THEN marks ELSE array_append(marks, $2) END"
            args.append(new_mark)
        sql += " WHERE thread_id = $1"
        if old_mark is not None:
            args.append([old_mark])
            sql += f" AND marks @> ${len(args)}"
        if msg_ids is not None:
            args.append(list(msg_ids))
            sql += f" AND msg_id = ANY(${len(args)})"
        result = await self._db.execute(sql, *args)
        return _affected(result)

    async def update_compressed_summary(self, summary: str) -> None:
        await super().update_compressed_summary(summary)
        await self._db.execute(
            """
            INSERT INTO agent_working_memory_meta (thread_id, summary)
            VALUES ($1, $2)
            ON CONFLICT (thread_id) DO UPDATE SET summary = $2, updated_at = now()
            """,
            self._thread_id,
            summary,
        )
        self._summary_loaded = True

    async def _load_summary(self) -> str:
        if not self._summary_loaded:
            self._compressed_summary = (
                await self._db.fetchval(
                    "SELECT summary FROM agent_working_memory_meta WHERE thread_id = $1",
                    self._thread_id,
                )
                or ""
            )
            self._summary_loaded = True
        return self._compressed_summary


def _affected(result: str) -> int:
    """asyncpg execute 返回形如 'DELETE 3' / 'UPDATE 2' 的状态串。"""
    try:
        return int(result.rsplit(" ", 1)[1])
    except (IndexError, ValueError):
        return 0
