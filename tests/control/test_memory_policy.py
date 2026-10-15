"""T042：记忆策略层 —— 低置信度审批、归档提炼、矛盾双保留（DB 集成 + 记忆替身）。

covers: TC-065, TC-067, TC-068
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.control.memory.collections import MemoryCollections
from app.control.memory.policy.archive_distill import ArchiveDistiller
from app.control.memory.policy.conflict_mark import (
    ConflictMarker,
    ConflictStatus,
    MemoryConflictRepository,
)
from app.control.memory.policy.pending_review import (
    PendingMemoryRepository,
    PendingMemoryReview,
    PendingMemoryStatus,
)
from app.control.memory.remember import MemoryScope, RememberStatus, remember


def _collections() -> MemoryCollections:
    return MemoryCollections("alpha", biz_collection="wetag-alpha", org_collection="wetag-org")


class _Outbox:
    def __init__(self) -> None:
        self.envelopes: list = []

    async def submit(self, envelope):
        self.envelopes.append(envelope)
        return "msg-1"


# ---- TC-065 低置信度业务线记忆经审批才转正 (US8-5) ----


class TestPendingReview:
    async def test_low_confidence_goes_pending_then_approved(
        self, pool, memory_backend
    ):
        """covers: TC-065 —— 低置信度写入仅进暂存并发确认卡片；批准后转正为
        正式业务线记忆并回填 memory_id。"""
        outbox = _Outbox()
        repo = PendingMemoryRepository(pool)
        review = PendingMemoryReview(
            repo, backend=memory_backend, collections=_collections(),
            outbox_submit=outbox.submit,
        )

        result = await remember(
            "业务线 发布 规范 草案",
            MemoryScope.BIZ,
            collections=_collections(),
            backend=memory_backend,
            chat_id="c1",
            topic_id="t1",
            confidence=0.4,  # 低于默认阈值 0.6
            pending_review=review,
        )

        # 初始仅暂存，语义记忆轨无写入；确认卡片已发
        assert result.status == RememberStatus.PENDING
        assert memory_backend.all_records("wetag-alpha") == []
        assert len(outbox.envelopes) == 1
        assert outbox.envelopes[0].payload["card_type"] == "memory_review"

        # 批准 → 转正
        approved = await review.approve(UUID(result.pending_id), "biz-admin")
        assert approved is not None and approved.status == PendingMemoryStatus.APPROVED
        assert approved.memory_id is not None
        records = memory_backend.all_records("wetag-alpha")
        assert len(records) == 1 and records[0]["metadata"]["layer"] == "biz"

        # 重复批准不生效（条件更新抢占）
        assert await review.approve(UUID(result.pending_id), "biz-admin") is None

    async def test_rejected_memory_not_effective_but_recorded(self, pool, memory_backend):
        """covers: TC-065 —— 拒绝后内容不生效，拒绝记录保留。"""
        repo = PendingMemoryRepository(pool)
        review = PendingMemoryReview(repo, backend=memory_backend, collections=_collections())

        result = await remember(
            "业务线 存疑 结论",
            MemoryScope.BIZ,
            collections=_collections(),
            backend=memory_backend,
            chat_id="c1",
            confidence=0.3,
            pending_review=review,
        )
        rejected = await review.reject(UUID(result.pending_id), "biz-admin")
        assert rejected is not None and rejected.status == PendingMemoryStatus.REJECTED
        assert rejected.decided_by == "biz-admin"
        # 不生效：语义记忆轨无写入；拒绝记录保留可查
        assert memory_backend.all_records("wetag-alpha") == []
        kept = await repo.get(UUID(result.pending_id))
        assert kept is not None and kept.status == PendingMemoryStatus.REJECTED


# ---- TC-067 归档事项只提炼结论为群记忆 (US8-7) ----


@dataclass
class _Topic:
    topic_id: str
    chat_id: str
    last_active_at: datetime
    content: str


async def _distiller(topic) -> list[str]:
    return ["结论：发布窗口固定周四", "结论：灰度先行"]


class TestArchiveDistill:
    async def test_archived_topic_distills_conclusions_only(self, memory_backend):
        """covers: TC-067 —— 超 24h 无活动的事项归档时，关键结论写入群级记忆，
        事项全文不作为记忆保存。"""
        distiller = ArchiveDistiller(
            _distiller,
            collections=_collections(),
            backend=memory_backend,
        )
        topic = _Topic(
            topic_id="t1",
            chat_id="c1",
            last_active_at=datetime.now(UTC) - timedelta(hours=30),
            content="（很长的讨论全文……发布窗口定周四……灰度先行……）",
        )

        result = await distiller.distill(topic)

        assert result.distilled is True
        records = memory_backend.all_records("wetag-alpha")
        assert len(records) == 2
        assert all(r["metadata"]["layer"] == "group" for r in records)
        assert all(r["metadata"]["topic_id"] == "t1" for r in records)
        # 全文未入记忆
        assert all(topic.content not in r["content"] for r in records)

    async def test_active_topic_skipped(self, memory_backend):
        """covers: TC-067 —— 未超无活动阈值的事项不提炼。"""
        distiller = ArchiveDistiller(
            _distiller, collections=_collections(), backend=memory_backend
        )
        topic = _Topic(
            topic_id="t2",
            chat_id="c1",
            last_active_at=datetime.now(UTC) - timedelta(hours=1),
            content="讨论中",
        )

        result = await distiller.distill(topic)

        assert result.distilled is False
        assert result.skipped_reason == "topic still active"
        assert memory_backend.all_records("wetag-alpha") == []


# ---- TC-068 矛盾记忆双保留并创建人工裁决标记 (US8-8) ----


async def _detector(content: str, candidates: list[dict]) -> str | None:
    for cand in candidates:
        if "周四" in cand["content"] and "周五" in content:
            return str(cand["id"])
    return None


class TestConflictMark:
    async def test_contradiction_keeps_both_and_marks(self, pool, memory_backend):
        """covers: TC-068 —— 新记忆与既有记忆矛盾：两条均保留，创建 OPEN
        冲突标记并提示人工裁决，旧记忆不被覆盖。"""
        outbox = _Outbox()
        marker = ConflictMarker(MemoryConflictRepository(pool), outbox_submit=outbox.submit)
        first = await remember(
            "发布 窗口 固定 周四",
            MemoryScope.GROUP,
            collections=_collections(),
            backend=memory_backend,
            chat_id="c1",
        )
        second = await remember(
            "发布 窗口 改到 周五",
            MemoryScope.GROUP,
            collections=_collections(),
            backend=memory_backend,
            chat_id="c1",
            conflict_detector=_detector,
            conflict_marker=marker,
        )

        assert second.status == RememberStatus.WRITTEN
        assert second.conflict_id is not None
        # 两条都保留，旧记忆内容未被覆盖
        records = memory_backend.all_records("wetag-alpha")
        contents = {r["content"] for r in records}
        assert contents == {"发布 窗口 固定 周四", "发布 窗口 改到 周五"}
        # 冲突标记 OPEN 且提示已发
        conflict = await MemoryConflictRepository(pool).get(UUID(second.conflict_id))
        assert conflict is not None and conflict.status == ConflictStatus.OPEN
        assert conflict.memory_id_a == first.memory_id
        assert conflict.memory_id_b == second.memory_id
        assert outbox.envelopes[0].payload["card_type"] == "memory_conflict"

    async def test_same_pair_marked_once(self, pool, memory_backend):
        """covers: TC-068 —— 同一对矛盾只记一次（ux_memory_conflict_pair）。"""
        repo = MemoryConflictRepository(pool)
        marker = ConflictMarker(repo)

        a = await marker.mark("mem-1", "mem-2", layer="group", chat_id="c1")
        b = await marker.mark("mem-1", "mem-2", layer="group", chat_id="c1")

        assert a.id == b.id
        assert len(await repo.list_open()) == 1
