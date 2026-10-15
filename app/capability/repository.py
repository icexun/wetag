"""Capability 表的读写 SQL。

每个方法都落在 ``platform.db.Repository`` 的语句层护栏内：SQL 必须含
``workspace_id``，返回行再逐行二次校验来源业务线。

**写法上的两条硬约束**，都是为了让"重跑一次"不产生垃圾数据：

1. ``refresh_index`` 用条件 upsert + ``RETURNING (xmax = 0)``：内容没变则
   **零写入**，老 revision **绝不覆盖**新 revision。
2. ``claim`` 用 ``ON CONFLICT DO NOTHING RETURNING`` 后回读判定，**不做
   应用层先查后写**——后者在并发下必然重复建卡。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Sequence

import asyncpg

from app.contracts.ids import DocumentId, RequirementId
from app.contracts.ports import StatusCategory
from app.platform.db.repository import Repository

logger = logging.getLogger(__name__)

# stale_reason 尾部编码的连续失败次数：``fetch_failed:X#n``
_FAIL_COUNT = re.compile(r"#(\d+)$")

SUBSCRIPTION_SOURCES = ("requirement", "document")
ALL_TARGETS = "*"


class IndexRefresh(StrEnum):
    """一次 :meth:`CapabilityRepository.refresh_index` 的四种下场。

    「没写」有两种相反的含义，调用方处置方向也相反：

    * :attr:`UNCHANGED`——库里内容就是文档当前内容，**已核对**，可推进新鲜度。
    * :attr:`REJECTED_STALE`——来的是更旧的版本，被挡掉了，这一次**没核对过**。
    """

    INSERTED = "inserted"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    REJECTED_STALE = "rejected_stale"

    @property
    def verified(self) -> bool:
        return self is not IndexRefresh.REJECTED_STALE


def as_list(value: Any) -> list[dict[str, Any]]:
    """jsonb 列可能是 str（未装 codec）或已解析的 list。"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    return [v for v in (value or []) if isinstance(v, dict)]


# jsonb 列一律传**原生** dict/list：连接池注册了 jsonb codec（pool.py），
# 再 ``json.dumps`` 一次会把值二次编码成 jsonb 标量字符串。


class CapabilityRepository(Repository):
    """Capability 的唯一数据出入口。"""

    # === requirement ====================================================

    async def upsert_requirement(
        self,
        conn: asyncpg.Connection,
        *,
        tapd_id: str,
        title: str,
        status: str,
        status_category: StatusCategory,
        category: str | None = None,
        owners: Sequence[str] = (),
        code_hints: Sequence[str] = (),
        doc_id: str | None = None,
        raw: dict[str, Any] | None = None,
        tapd_updated_at: datetime | None = None,
    ) -> RequirementId:
        """按 ``(workspace_id, tapd_id)`` 幂等写入。``requirement_id`` 生成后永不变。"""
        row = await self.fetchrow_in(
            conn,
            """
            INSERT INTO requirement (
                workspace_id, tapd_id, title, status, status_category,
                category, owners, code_hints, doc_id, raw,
                tapd_updated_at, synced_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11, now())
            ON CONFLICT (workspace_id, tapd_id) DO UPDATE SET
                title           = EXCLUDED.title,
                status          = EXCLUDED.status,
                status_category = EXCLUDED.status_category,
                category        = EXCLUDED.category,
                owners          = EXCLUDED.owners,
                code_hints      = EXCLUDED.code_hints,
                doc_id          = EXCLUDED.doc_id,
                raw             = EXCLUDED.raw,
                tapd_updated_at = EXCLUDED.tapd_updated_at,
                synced_at       = now()
            RETURNING requirement_id, workspace_id
            """,
            self.ws,
            tapd_id,
            title,
            status,
            status_category.value,
            category,
            self.labels_of(owners),
            self.labels_of(code_hints),
            doc_id,
            raw or {},
            tapd_updated_at,
            where="upsert_requirement",
        )
        assert row is not None
        return RequirementId(str(row["requirement_id"]))

    async def get_requirement(self, tapd_id: str) -> asyncpg.Record | None:
        return await self.fetchrow(
            "SELECT * FROM requirement WHERE workspace_id = $1 AND tapd_id = $2",
            self.ws,
            tapd_id,
            where="get_requirement",
        )

    async def get_requirement_brief(self, requirement_id: str) -> asyncpg.Record | None:
        """按内部 id 取标题与负责人。"""
        return await self.fetchrow(
            """
            SELECT workspace_id, requirement_id, tapd_id, title, owners
              FROM requirement
             WHERE workspace_id = $1 AND requirement_id = $2::uuid
            """,
            self.ws,
            requirement_id,
            where="get_requirement_brief",
        )

    async def requirements_by_tapd_ids(
        self, tapd_ids: Sequence[str]
    ) -> dict[str, asyncpg.Record]:
        """批量取上一版投影，供变更比对。"""
        if not tapd_ids:
            return {}
        rows = await self.fetch(
            """
            SELECT workspace_id, tapd_id, title, status, owners, category, doc_id
              FROM requirement
             WHERE workspace_id = $1 AND tapd_id = ANY($2::text[])
            """,
            self.ws,
            list(tapd_ids),
            where="requirements_by_tapd_ids",
        )
        return {str(r["tapd_id"]): r for r in rows}

    async def requirement_doc_ids(self) -> list[str]:
        """非终态需求关联的文档，并入 doc_poll 监听清单。"""
        rows = await self.fetch(
            """
            SELECT DISTINCT doc_id, $1::text AS workspace_id
              FROM requirement
             WHERE workspace_id = $1 AND doc_id IS NOT NULL
               AND status_category <> 'TERMINAL'
            """,
            self.ws,
            where="requirement_doc_ids",
        )
        return [str(r["doc_id"]) for r in rows]

    # === doc_index =======================================================

    async def get_index(self, document_id: DocumentId) -> asyncpg.Record | None:
        return await self.fetchrow(
            """
            SELECT * FROM doc_index
             WHERE workspace_id = $1 AND document_id = $2 AND archived_at IS NULL
            """,
            self.ws,
            str(document_id),
            where="get_index",
        )

    async def refresh_index(
        self,
        conn: asyncpg.Connection,
        *,
        document_id: DocumentId,
        title: str | None,
        sections: Sequence[dict[str, Any]],
        embedding: Sequence[float] | None,
        doc_revision: int,
        content_hash: str | None,
        doc_modified_at: datetime | None,
    ) -> IndexRefresh:
        """刷新文档快照。**唯一允许的写法。**

        * **老版本绝不覆盖新版本**：revision 可比时它是权威时序；revision 相等
          （含双方都是 0）时才退回内容比对。写成 ``老 revision OR 内容不同``
          会让真实回滚（更旧 revision + 不同内容）被写回去。
        * **内容没变就零写入**。
        * 命中即 ``stale = false``——刷新成功是**唯一**能摘掉 stale 的动作。
        """
        row = await self.fetchrow_in(
            conn,
            """
            INSERT INTO doc_index (
                workspace_id, document_id, title, sections, embedding,
                doc_revision, content_hash, doc_modified_at,
                stale, stale_reason, refreshed_at
            ) VALUES ($1,$2,$3,$4::jsonb,$5,$6,$7,$8,false,NULL,now())
            ON CONFLICT (workspace_id, document_id) DO UPDATE SET
                title           = EXCLUDED.title,
                sections        = EXCLUDED.sections,
                embedding       = COALESCE(EXCLUDED.embedding, doc_index.embedding),
                doc_revision    = EXCLUDED.doc_revision,
                content_hash    = EXCLUDED.content_hash,
                doc_modified_at = EXCLUDED.doc_modified_at,
                stale           = false,
                stale_reason    = NULL,
                archived_at     = NULL,
                refreshed_at    = now()
            WHERE EXCLUDED.doc_revision > doc_index.doc_revision
               OR (
                    EXCLUDED.doc_revision = doc_index.doc_revision
                AND doc_index.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                  )
            RETURNING id, (xmax = 0) AS inserted, workspace_id
            """,
            self.ws,
            str(document_id),
            title,
            list(sections),
            list(embedding) if embedding else None,
            doc_revision,
            content_hash,
            doc_modified_at,
            where="refresh_index",
        )
        if row is not None:
            return IndexRefresh.INSERTED if row["inserted"] else IndexRefresh.UPDATED

        cur = await self.fetchrow_in(
            conn,
            """
            SELECT content_hash, doc_revision, workspace_id
              FROM doc_index
             WHERE workspace_id = $1 AND document_id = $2
            """,
            self.ws,
            str(document_id),
            where="refresh_index.classify",
        )
        if cur is None:
            return IndexRefresh.REJECTED_STALE
        if content_hash is not None and cur["content_hash"] == content_hash:
            return IndexRefresh.UNCHANGED
        logger.warning(
            "索引刷新被挡回：来的 revision=%s 不新于库里的 %s（doc=%s）",
            doc_revision,
            cur["doc_revision"],
            document_id,
        )
        return IndexRefresh.REJECTED_STALE

    async def touch_index(
        self, conn: asyncpg.Connection, *, document_id: DocumentId
    ) -> None:
        """内容未变但**已核对**：推进 ``refreshed_at`` 并摘掉 stale。"""
        await self.execute_in(
            conn,
            """
            UPDATE doc_index
               SET refreshed_at = now(), stale = false, stale_reason = NULL
             WHERE workspace_id = $1 AND document_id = $2
            """,
            self.ws,
            str(document_id),
        )

    async def backfill_embedding(
        self,
        conn: asyncpg.Connection,
        *,
        document_id: DocumentId,
        embedding: Sequence[float],
    ) -> bool:
        """只补空向量，已有值零写入。"""
        row = await self.fetchrow_in(
            conn,
            """
            UPDATE doc_index SET embedding = $3
             WHERE workspace_id = $1 AND document_id = $2 AND embedding IS NULL
            RETURNING workspace_id
            """,
            self.ws,
            str(document_id),
            list(embedding),
            where="backfill_embedding",
        )
        return row is not None

    async def mark_stale(
        self, conn: asyncpg.Connection, *, document_id: DocumentId, reason: str
    ) -> int:
        """标记快照过期。拉取失败时必须调用，不能静默跳过。"""
        status = await self.execute_in(
            conn,
            """
            UPDATE doc_index
               SET stale = true, stale_reason = $3
             WHERE workspace_id = $1 AND document_id = $2 AND archived_at IS NULL
            """,
            self.ws,
            str(document_id),
            reason[:200],
        )
        return _affected(status)

    async def mark_stale_or_archive(
        self,
        conn: asyncpg.Connection,
        *,
        document_id: DocumentId,
        reason: str,
        max_failures: int = 3,
    ) -> str:
        """标脏并累计连续失败次数；达阈值改为归档。返回 ``stale``/``archived``/``none``。

        文档已删、长期无权限等永久性失败靠重刷救不回来。连续失败次数编码在
        stale_reason 尾部（``fetch_failed:X#n``），刷新成功即归零。归档后退出
        监听清单（不删除，留痕）。
        """
        row = await conn.fetchrow(
            """
            SELECT stale, stale_reason
              FROM doc_index
             WHERE workspace_id = $1 AND document_id = $2 AND archived_at IS NULL
            """,
            self.ws,
            str(document_id),
        )
        if row is None:
            return "none"
        count = 1
        prev = row["stale_reason"] or ""
        m = _FAIL_COUNT.search(prev)
        if row["stale"] and m and prev[: m.start()].startswith("fetch_failed"):
            count = int(m.group(1)) + 1
        if count >= max_failures:
            await self.execute_in(
                conn,
                """
                UPDATE doc_index
                   SET archived_at = now(), stale_reason = $3
                 WHERE workspace_id = $1 AND document_id = $2 AND archived_at IS NULL
                """,
                self.ws,
                str(document_id),
                f"archived_after_{count}_failures:{reason}"[:200],
            )
            return "archived"
        await self.execute_in(
            conn,
            """
            UPDATE doc_index
               SET stale = true, stale_reason = $3
             WHERE workspace_id = $1 AND document_id = $2 AND archived_at IS NULL
            """,
            self.ws,
            str(document_id),
            f"{reason}#{count}"[:200],
        )
        return "stale"

    async def mark_stale_unverified(
        self, conn: asyncpg.Connection, *, document_id: DocumentId, reason: str
    ) -> int:
        """端口不可用等**非文档自身**的失败：只标干净行，不推进归档计数。"""
        status = await self.execute_in(
            conn,
            """
            UPDATE doc_index
               SET stale = true, stale_reason = $3
             WHERE workspace_id = $1 AND document_id = $2
               AND archived_at IS NULL AND stale = false
            """,
            self.ws,
            str(document_id),
            reason[:200],
        )
        return _affected(status)

    async def list_indexed_docs(self) -> list[asyncpg.Record]:
        """``doc_poll`` 监听清单：全部未归档文档。"""
        return await self.fetch(
            """
            SELECT workspace_id, document_id, title, doc_revision,
                   doc_modified_at, stale
              FROM doc_index
             WHERE workspace_id = $1 AND archived_at IS NULL
             ORDER BY document_id
            """,
            self.ws,
            where="list_indexed_docs",
        )

    async def index_freshness(self) -> tuple[int, datetime | None]:
        """:returns: ``(stale 条数, 最早的 refreshed_at)``。"""
        row = await self.fetchrow(
            """
            SELECT count(*) FILTER (WHERE stale) AS stale_count,
                   min(refreshed_at)             AS oldest,
                   $1::text                      AS workspace_id
              FROM doc_index
             WHERE workspace_id = $1 AND archived_at IS NULL
            """,
            self.ws,
            where="index_freshness",
        )
        if row is None:
            return 0, None
        return int(row["stale_count"] or 0), row["oldest"]

    # === chat_subscription ===============================================

    async def subscribe(
        self, *, chat_id: str, source: str, target: str, created_by: str | None
    ) -> bool:
        """返回是否新增（重复订阅幂等）。"""
        _check_source(source)
        row = await self.fetchrow(
            """
            INSERT INTO chat_subscription (workspace_id, chat_id, source, target, created_by)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (workspace_id, chat_id, source, target) DO NOTHING
            RETURNING workspace_id
            """,
            self.ws,
            chat_id,
            source,
            target,
            created_by,
            where="subscribe",
        )
        return row is not None

    async def unsubscribe(self, *, chat_id: str, source: str, target: str) -> int:
        _check_source(source)
        status = await self.execute(
            """
            DELETE FROM chat_subscription
             WHERE workspace_id = $1 AND chat_id = $2 AND source = $3 AND target = $4
            """,
            self.ws,
            chat_id,
            source,
            target,
        )
        return _affected(status)

    async def list_subscriptions(self, chat_id: str) -> list[asyncpg.Record]:
        return await self.fetch(
            """
            SELECT workspace_id, source, target, created_by, created_at
              FROM chat_subscription
             WHERE workspace_id = $1 AND chat_id = $2
             ORDER BY id
            """,
            self.ws,
            chat_id,
            where="list_subscriptions",
        )

    async def subscribers(self, source: str, target: str) -> list[str]:
        """订阅了该对象（或该类全部对象）的群。"""
        _check_source(source)
        rows = await self.fetch(
            """
            SELECT DISTINCT chat_id, $1::text AS workspace_id
              FROM chat_subscription
             WHERE workspace_id = $1 AND source = $2 AND target IN ($3, '*')
             ORDER BY chat_id
            """,
            self.ws,
            source,
            target,
            where="subscribers",
        )
        return [str(r["chat_id"]) for r in rows]

    async def subscribed_doc_ids(self) -> list[str]:
        """被具体订阅的文档，并入 doc_poll 监听清单。"""
        rows = await self.fetch(
            """
            SELECT DISTINCT target, $1::text AS workspace_id
              FROM chat_subscription
             WHERE workspace_id = $1 AND source = 'document' AND target <> '*'
            """,
            self.ws,
            where="subscribed_doc_ids",
        )
        return [str(r["target"]) for r in rows]

    async def latest_active_chat_id(self) -> str | None:
        """最近活跃的群——无会话归属的系统通知的推送目标。

        ``chat_group`` 归 Space，这里是只读跨界查询。从未有人 @ 过机器人时返回 None。
        """
        row = await self.fetchrow(
            """
            SELECT chat_id, workspace_id FROM chat_group
             WHERE workspace_id = $1
             ORDER BY last_inbound_at DESC
             LIMIT 1
            """,
            self.ws,
            where="latest_active_chat_id",
        )
        return str(row["chat_id"]) if row else None

    # === Loop-3 ==========================================================

    async def loop_down_votes(self, *, window_days: int = 7) -> list[asyncpg.Record]:
        """窗口内的 👎 反馈。``feedback_record`` 归 Channel，同 schema 只读跨界。"""
        return await self.fetch(
            """
            SELECT fr.actor_user_id, fr.reason, fr.comment, fr.created_at,
                   $1::text AS workspace_id
              FROM feedback_record fr
             WHERE lower(fr.result) IN ('down', 'dislike')
               AND fr.created_at >= now() - make_interval(days => $2)
             ORDER BY fr.id
            """,
            self.ws,
            window_days,
            where="loop_down_votes",
        )

    async def record_rule_change(
        self,
        *,
        kind: str,
        rule_version_old: str | None,
        rule_version_new: str | None,
        detail: dict[str, Any],
        sample_size: int | None = None,
        actor: str = "loop_digest",
    ) -> None:
        """Loop 自动变更存档，审计与回滚的依据。"""
        await self.execute(
            """
            INSERT INTO rule_change_log (
                workspace_id, kind, rule_version_old, rule_version_new,
                detail, sample_size, actor
            ) VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
            """,
            self.ws,
            kind,
            rule_version_old,
            rule_version_new,
            detail,
            sample_size,
            actor,
        )

    # === idempotency_record ==============================================

    async def claim(
        self, conn: asyncpg.Connection, *, key: str, operation: str
    ) -> tuple[bool, asyncpg.Record | None]:
        """抢占幂等键。``ON CONFLICT DO NOTHING RETURNING`` + 回读，不做先查后写。

        :returns: ``(claimed, record)``。``claimed=False`` 时 ``record`` 是已
            存在的那条——``succeeded`` 则复用 ``result``，``in_flight`` 说明另一个
            进程正在做，``failed`` 且未过期才允许重试。
        """
        row = await self.fetchrow_in(
            conn,
            """
            INSERT INTO idempotency_record (key, workspace_id, operation)
            VALUES ($1, $2, $3)
            ON CONFLICT (key) DO NOTHING
            RETURNING key, workspace_id, status, attempt
            """,
            key,
            self.ws,
            operation,
            where="claim.insert",
        )
        if row is not None:
            return True, row
        existing = await self.fetchrow_in(
            conn,
            "SELECT * FROM idempotency_record WHERE key = $1 AND workspace_id = $2",
            key,
            self.ws,
            where="claim.lookup",
        )
        return False, existing

    async def settle(
        self,
        conn: asyncpg.Connection,
        *,
        key: str,
        status: str,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> None:
        if status not in ("succeeded", "failed"):
            raise ValueError(f"settle 只接受 succeeded/failed，收到 {status!r}")
        await self.execute_in(
            conn,
            """
            UPDATE idempotency_record
               SET status = $3, result = $4::jsonb, error_code = $5,
                   updated_at = now(),
                   attempt = attempt + CASE WHEN $3 = 'failed' THEN 1 ELSE 0 END
             WHERE key = $1 AND workspace_id = $2
            """,
            key,
            self.ws,
            status,
            result,
            error_code,
        )

    async def lookup(self, key: str) -> asyncpg.Record | None:
        return await self.fetchrow(
            "SELECT * FROM idempotency_record WHERE key = $1 AND workspace_id = $2",
            key,
            self.ws,
            where="lookup",
        )

    # === requirement_relation ============================================

    async def link(
        self,
        conn: asyncpg.Connection,
        *,
        requirement_id: RequirementId,
        relation_kind: str,
        target_ref: str,
        source: str = "tapd_field",
    ) -> None:
        await self.execute_in(
            conn,
            """
            INSERT INTO requirement_relation (
                workspace_id, requirement_id, relation_kind, target_ref, source
            ) VALUES ($1,$2,$3,$4,$5)
            ON CONFLICT (workspace_id, requirement_id, relation_kind, target_ref)
            DO NOTHING
            """,
            self.ws,
            str(requirement_id),
            relation_kind,
            target_ref,
            source,
        )

    async def relations(
        self, requirement_id: RequirementId, *, relation_kind: str | None = None
    ) -> list[asyncpg.Record]:
        return await self.fetch(
            """
            SELECT * FROM requirement_relation
             WHERE workspace_id = $1 AND requirement_id = $2
               AND ($3::text IS NULL OR relation_kind = $3)
             ORDER BY created_at
            """,
            self.ws,
            str(requirement_id),
            relation_kind,
            where="relations",
        )


def _check_source(source: str) -> None:
    if source not in SUBSCRIPTION_SOURCES:
        raise ValueError(f"未知订阅源：{source!r}")


def _affected(status: str) -> int:
    """``UPDATE 3`` → ``3``。"""
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):
        return 0


__all__ = [
    "ALL_TARGETS",
    "CapabilityRepository",
    "IndexRefresh",
    "SUBSCRIPTION_SOURCES",
    "as_list",
]
