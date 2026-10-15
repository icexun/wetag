"""真 PostgreSQL 上的仓储测试（``-m pg``）。

这里测的每一条都**只有真库能证明**：

* ``ON CONFLICT ... DO UPDATE ... WHERE`` 的 ``WHERE`` 到底生效没有——它是
  "老 revision 不覆盖新 revision" 和 "内容未变零写入" 两条纪律的唯一落点，
  写错了在内存替身里看不出来，在生产里表现为每 3 分钟整表重写一次。
* ``xmax = 0`` 判插入还是更新——这是 PG 的物理行版本字段，没有替身。
* ``UNIQUE`` 约束在真并发下唯一——两个协程同时抢一个幂等键。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest

from app.capability.repository import CapabilityRepository, IndexRefresh
from app.contracts.errors import IsolationViolation
from app.contracts.ids import DocumentId
from app.contracts.ports import StatusCategory

pytestmark = pytest.mark.pg

_SECTIONS = [
    {"anchor": "a1", "heading": "结论", "excerpt": "x", "hash": "s1"},
    {"anchor": "a2", "heading": "背景", "excerpt": "y", "hash": "s2"},
]


def _now() -> datetime:
    return datetime.now(UTC)


async def _refresh(
    repo: CapabilityRepository,
    doc_id: str,
    *,
    title: str = "文档",
    sections: list[dict] | None = None,
    embedding: list[float] | None = None,
    doc_revision: int = 1,
    content_hash: str | None = "h1",
) -> IndexRefresh:
    async with repo.db.tx() as conn:
        return await repo.refresh_index(
            conn,
            document_id=DocumentId(doc_id),
            title=title,
            sections=_SECTIONS if sections is None else sections,
            embedding=embedding,
            doc_revision=doc_revision,
            content_hash=content_hash,
            doc_modified_at=_now(),
        )


async def _row(repo: CapabilityRepository, doc_id: str):
    return await repo.db.fetchrow(
        "SELECT * FROM doc_index WHERE workspace_id = $1 AND document_id = $2",
        repo.ws,
        doc_id,
    )


# --- requirement ------------------------------------------------------------


async def test_same_tapd_id_keeps_one_row_and_one_requirement_id(
    repo: CapabilityRepository,
) -> None:
    """轮询会反复看到同一条需求；``requirement_id`` 必须**永不变**。"""
    async with repo.db.tx() as conn:
        first = await repo.upsert_requirement(
            conn, tapd_id="T-1", title="原标题", status="规划中",
            status_category=StatusCategory.ACTIVE,
        )
    async with repo.db.tx() as conn:
        second = await repo.upsert_requirement(
            conn, tapd_id="T-1", title="改过的标题", status="开发中",
            status_category=StatusCategory.ACTIVE, owners=["bob", "alice"],
            category="基础能力", doc_id="doc_T1",
        )

    assert first == second, "同一 tapd_id 换了 requirement_id：下游全部关联失效"
    row = await repo.get_requirement("T-1")
    assert row is not None
    assert row["title"] == "改过的标题", "标题没更新——upsert 退化成 DO NOTHING"
    assert row["status"] == "开发中"
    assert await repo.db.fetchval("SELECT count(*) FROM requirement") == 1


async def test_previous_projection_is_what_change_diff_reads(
    repo: CapabilityRepository,
) -> None:
    """``requirements_by_tapd_ids`` 是需求变更比对的"上一版"，字段必须齐。"""
    async with repo.db.tx() as conn:
        await repo.upsert_requirement(
            conn, tapd_id="T-2", title="t", status="开发中",
            status_category=StatusCategory.ACTIVE, owners=["alice"],
            category="基础能力", doc_id="doc_T2",
        )

    got = await repo.requirements_by_tapd_ids(["T-2", "T-none"])

    assert set(got) == {"T-2"}
    prev = got["T-2"]
    assert (prev["title"], prev["status"], prev["category"], prev["doc_id"]) == (
        "t", "开发中", "基础能力", "doc_T2",
    )
    assert list(prev["owners"]) == ["alice"]
    assert await repo.requirements_by_tapd_ids([]) == {}


async def test_requirement_doc_ids_skips_terminal_requirements(
    repo: CapabilityRepository,
) -> None:
    """已结束的需求不再监听其文档——否则监听清单只增不减。"""
    async with repo.db.tx() as conn:
        await repo.upsert_requirement(
            conn, tapd_id="A", title="a", status="开发中",
            status_category=StatusCategory.ACTIVE, doc_id="doc_A",
        )
        await repo.upsert_requirement(
            conn, tapd_id="B", title="b", status="已上线",
            status_category=StatusCategory.TERMINAL, doc_id="doc_B",
        )
        await repo.upsert_requirement(
            conn, tapd_id="C", title="c", status="开发中",
            status_category=StatusCategory.ACTIVE,
        )

    assert await repo.requirement_doc_ids() == ["doc_A"]


# --- refresh_index 的 WHERE 子句 -------------------------------------------


async def test_first_refresh_reports_inserted(repo: CapabilityRepository) -> None:
    assert await _refresh(repo, "d1") is IndexRefresh.INSERTED


async def test_unchanged_content_writes_nothing(repo: CapabilityRepository) -> None:
    """内容没变 → 零写入，且报告为已核对。"""
    await _refresh(repo, "d2")
    before = await repo.db.fetchval(
        "SELECT xmin::text FROM doc_index WHERE workspace_id = $1", repo.ws
    )

    outcome = await _refresh(repo, "d2")

    assert outcome is IndexRefresh.UNCHANGED, f"内容相同却不是 UNCHANGED（{outcome}）"
    assert outcome.verified is True
    after = await repo.db.fetchval(
        "SELECT xmin::text FROM doc_index WHERE workspace_id = $1", repo.ws
    )
    assert after == before, "内容未变却重写了一遍行"


async def test_newer_revision_does_overwrite(repo: CapabilityRepository) -> None:
    await _refresh(repo, "d3", doc_revision=5, content_hash="h5")

    outcome = await _refresh(repo, "d3", title="新版", doc_revision=6, content_hash="h6")

    assert outcome is IndexRefresh.UPDATED
    row = await _row(repo, "d3")
    assert row["doc_revision"] == 6 and row["title"] == "新版"


async def test_a_rollback_carrying_different_content_still_cannot_overwrite(
    repo: CapabilityRepository,
) -> None:
    """真实回滚是「旧 revision + 不同内容」：必须被挡回且不报已核对。

    判据一度写成 ``老 revision < 新 OR 内容不同``，于是回滚**总能**覆盖新版本。
    """
    await _refresh(repo, "d4", title="新版", doc_revision=10, content_hash="h10")

    outcome = await _refresh(repo, "d4", title="回滚版", doc_revision=3, content_hash="h3")

    assert outcome is IndexRefresh.REJECTED_STALE
    assert outcome.verified is False
    row = await _row(repo, "d4")
    assert (row["doc_revision"], row["title"], row["content_hash"]) == (10, "新版", "h10")


async def test_content_change_still_writes_when_the_platform_gives_no_revision(
    repo: CapabilityRepository,
) -> None:
    """飞书 metas 不返回 revision（恒为 0）时，内容比对仍是有效的写入依据。"""
    await _refresh(repo, "d5", doc_revision=0, content_hash="h0")

    outcome = await _refresh(repo, "d5", title="改过的", doc_revision=0, content_hash="h0b")

    assert outcome is IndexRefresh.UPDATED, "revision 不可比时内容变化写不进去"
    assert (await _row(repo, "d5"))["title"] == "改过的"


async def test_without_a_content_hash_a_no_write_is_never_called_verified(
    repo: CapabilityRepository,
) -> None:
    """两边 hash 都是 NULL 时不许冒充"内容一致"。"""
    await _refresh(repo, "d6", doc_revision=5, content_hash=None)

    outcome = await _refresh(repo, "d6", doc_revision=2, content_hash=None)

    assert outcome is IndexRefresh.REJECTED_STALE
    assert outcome.verified is False


async def test_refresh_keeps_the_old_embedding_when_none_is_given(
    repo: CapabilityRepository,
) -> None:
    """不传 embedder 的刷新不得擦掉已有向量。"""
    await _refresh(repo, "d7", embedding=[1.0, 0.0])

    await _refresh(repo, "d7", doc_revision=2, content_hash="h2", embedding=None)

    assert (await _row(repo, "d7"))["embedding"] == pytest.approx([1.0, 0.0])


async def test_sections_are_real_jsonb_arrays(repo: CapabilityRepository) -> None:
    """jsonb 列里必须是真 array，不是二次编码的字符串（连接池已注册 codec）。"""
    await _refresh(repo, "d8")

    row = await repo.db.fetchrow(
        "SELECT jsonb_typeof(sections) AS t, jsonb_array_length(sections) AS n "
        "FROM doc_index WHERE workspace_id = $1",
        repo.ws,
    )
    assert (row["t"], row["n"]) == ("array", 2)
    got = await repo.get_index(DocumentId("d8"))
    assert got["sections"][0]["hash"] == "s1"


# --- stale / archive --------------------------------------------------------


async def test_a_rejected_refresh_does_not_clear_an_existing_stale_flag(
    repo: CapabilityRepository,
) -> None:
    await _refresh(repo, "s1", doc_revision=9, content_hash="h9")
    async with repo.db.tx() as conn:
        await repo.mark_stale(conn, document_id=DocumentId("s1"), reason="auth_failed")

    outcome = await _refresh(repo, "s1", doc_revision=2, content_hash="h2")

    assert outcome is IndexRefresh.REJECTED_STALE
    stale_count, _ = await repo.index_freshness()
    assert stale_count == 1, "一次被挡回的刷新把 stale 洗白了"


async def test_refresh_clears_stale(repo: CapabilityRepository) -> None:
    """刷新成功是**唯一**能摘掉 stale 的动作。"""
    await _refresh(repo, "s2")
    async with repo.db.tx() as conn:
        await repo.mark_stale(conn, document_id=DocumentId("s2"), reason="auth_failed")

    assert await _refresh(repo, "s2", doc_revision=2, content_hash="h2") is IndexRefresh.UPDATED
    stale_count, oldest = await repo.index_freshness()
    assert stale_count == 0 and oldest is not None


async def test_mark_stale_or_archive_counts_then_archives(
    repo: CapabilityRepository,
) -> None:
    """连续拉取失败达阈值 → 归档，退出监听清单与新鲜度统计。"""
    await _refresh(repo, "s3")
    for expect in ("stale", "stale", "archived"):
        async with repo.db.tx() as conn:
            outcome = await repo.mark_stale_or_archive(
                conn, document_id=DocumentId("s3"), reason="fetch_failed:PortUnavailable"
            )
        assert outcome == expect

    stale_count, _ = await repo.index_freshness()
    assert stale_count == 0
    assert all(r["document_id"] != "s3" for r in await repo.list_indexed_docs())
    assert await repo.get_index(DocumentId("s3")) is None


async def test_an_archived_doc_comes_back_on_a_successful_refresh(
    repo: CapabilityRepository,
) -> None:
    """归档不是删除：文档恢复访问后，新版本写入即重新纳入监听。"""
    await _refresh(repo, "s4")
    async with repo.db.tx() as conn:
        await repo.mark_stale_or_archive(
            conn, document_id=DocumentId("s4"), reason="fetch_failed:NOT_FOUND",
            max_failures=1,
        )

    await _refresh(repo, "s4", doc_revision=2, content_hash="h2")

    assert any(r["document_id"] == "s4" for r in await repo.list_indexed_docs())


async def test_mark_stale_or_archive_successful_refresh_resets_count(
    repo: CapabilityRepository,
) -> None:
    await _refresh(repo, "s5")
    async with repo.db.tx() as conn:
        for _ in range(2):
            await repo.mark_stale_or_archive(
                conn, document_id=DocumentId("s5"), reason="fetch_failed:PortUnavailable"
            )
    await _refresh(repo, "s5", doc_revision=2, content_hash="h2")

    for _ in range(2):
        async with repo.db.tx() as conn:
            outcome = await repo.mark_stale_or_archive(
                conn, document_id=DocumentId("s5"), reason="fetch_failed:PortUnavailable"
            )
        assert outcome == "stale", "成功刷新后计数没归零，提前归档了"


async def test_mark_stale_unverified_preserves_the_failure_counter(
    repo: CapabilityRepository,
) -> None:
    """端口不可用：只标脏干净行，不推进也不抹掉 fetch_failed 计数。"""
    doc = DocumentId("s6")
    await _refresh(repo, "s6")
    async with repo.db.tx() as conn:
        assert await repo.mark_stale_unverified(
            conn, document_id=doc, reason="refresh_skipped:documents_port_down"
        ) == 1
    async with repo.db.tx() as conn:
        for _ in range(2):
            await repo.mark_stale_or_archive(
                conn, document_id=doc, reason="fetch_failed:PortUnavailable"
            )
    async with repo.db.tx() as conn:
        assert await repo.mark_stale_unverified(
            conn, document_id=doc, reason="refresh_skipped:documents_port_down"
        ) == 0
    async with repo.db.tx() as conn:
        outcome = await repo.mark_stale_or_archive(
            conn, document_id=doc, reason="fetch_failed:PortUnavailable"
        )
    assert outcome == "archived", "端口抖动把归档计数洗掉了"


async def test_touch_index_moves_refreshed_at_but_not_revision(
    repo: CapabilityRepository,
) -> None:
    await _refresh(repo, "s7")
    await repo.db.execute(
        "UPDATE doc_index SET refreshed_at = now() - interval '2 hours' WHERE workspace_id = $1",
        repo.ws,
    )
    _, before = await repo.index_freshness()
    async with repo.db.tx() as conn:
        await repo.touch_index(conn, document_id=DocumentId("s7"))
    _, after = await repo.index_freshness()

    assert after > before
    assert (await _row(repo, "s7"))["doc_revision"] == 1


# --- backfill_embedding ------------------------------------------------------


async def test_backfill_fills_a_null_vector_without_touching_freshness(
    repo: CapabilityRepository,
) -> None:
    """回填只补向量列：没有重新核对正文，不许推进 ``refreshed_at`` 或摘 stale。"""
    await _refresh(repo, "b1")
    await repo.db.execute(
        "UPDATE doc_index SET refreshed_at = now() - interval '2 hours', "
        "stale = true, stale_reason = 'auth_failed' WHERE workspace_id = $1",
        repo.ws,
    )
    before = await _row(repo, "b1")

    async with repo.db.tx() as conn:
        wrote = await repo.backfill_embedding(
            conn, document_id=DocumentId("b1"), embedding=[0.5, 0.25]
        )

    assert wrote is True
    after = await _row(repo, "b1")
    assert after["embedding"] == pytest.approx([0.5, 0.25])
    for col in ("refreshed_at", "stale", "stale_reason", "content_hash", "doc_revision"):
        assert after[col] == before[col], f"回填顺手改了 {col}"


async def test_backfill_is_zero_write_when_a_vector_exists(
    repo: CapabilityRepository,
) -> None:
    await _refresh(repo, "b2", embedding=[1.0, 0.0])

    async with repo.db.tx() as conn:
        wrote = await repo.backfill_embedding(
            conn, document_id=DocumentId("b2"), embedding=[0.0, 1.0]
        )

    assert wrote is False
    assert (await _row(repo, "b2"))["embedding"] == pytest.approx([1.0, 0.0])


async def test_backfill_does_not_create_a_row_for_an_unknown_document(
    repo: CapabilityRepository,
) -> None:
    async with repo.db.tx() as conn:
        wrote = await repo.backfill_embedding(
            conn, document_id=DocumentId("nope"), embedding=[0.5]
        )
    assert wrote is False
    assert await repo.db.fetchval("SELECT count(*) FROM doc_index") == 0


# --- chat_subscription -------------------------------------------------------


async def test_subscribe_is_idempotent_and_unsubscribe_removes(
    repo: CapabilityRepository,
) -> None:
    kw = dict(chat_id="c1", source="document", target="doc1")
    assert await repo.subscribe(**kw, created_by="u1") is True
    assert await repo.subscribe(**kw, created_by="u2") is False, "重复订阅新增了一行"
    assert len(await repo.list_subscriptions("c1")) == 1

    assert await repo.unsubscribe(**kw) == 1
    assert await repo.unsubscribe(**kw) == 0
    assert await repo.list_subscriptions("c1") == []


async def test_subscribers_include_wildcard_and_exclude_other_sources(
    repo: CapabilityRepository,
) -> None:
    await repo.subscribe(chat_id="c-one", source="document", target="doc1", created_by=None)
    await repo.subscribe(chat_id="c-all", source="document", target="*", created_by=None)
    await repo.subscribe(chat_id="c-req", source="requirement", target="*", created_by=None)
    await repo.subscribe(chat_id="c-other", source="document", target="doc2", created_by=None)

    assert await repo.subscribers("document", "doc1") == ["c-all", "c-one"]
    assert await repo.subscribers("requirement", "T-1") == ["c-req"]
    assert sorted(await repo.subscribed_doc_ids()) == ["doc1", "doc2"]


async def test_unknown_subscription_source_is_refused(
    repo: CapabilityRepository,
) -> None:
    with pytest.raises(ValueError):
        await repo.subscribe(chat_id="c1", source="tag", target="*", created_by=None)


# --- 幂等键 -----------------------------------------------------------------


async def test_only_one_of_two_concurrent_claims_wins(
    repo: CapabilityRepository,
) -> None:
    """同一幂等键并发抢占只能有一个赢。

    这条**必须**用真库并发跑。``ON CONFLICT DO NOTHING RETURNING`` + 回读的
    全部意义就在这里：应用层"先 SELECT 没有再 INSERT"在两个进程间必然双写，
    而每个键对应一次外部副作用——双写就是用户看到两张 TAPD 卡。
    """
    key = "idem:create:story:42"

    async def attempt() -> bool:
        async with repo.db.tx() as conn:
            claimed, _ = await repo.claim(conn, key=key, operation="create_story")
            return claimed

    results = await asyncio.gather(attempt(), attempt())
    assert sorted(results) == [False, True], f"并发抢占结果 {results}"
    assert await repo.db.fetchval(
        "SELECT count(*) FROM idempotency_record WHERE workspace_id = $1", repo.ws
    ) == 1


async def test_a_failed_attempt_bumps_the_counter_and_a_success_does_not(
    repo: CapabilityRepository,
) -> None:
    """``attempt`` 只在失败时 +1。

    成功也加的话，"重试了几次"这个数字就没法用来判断"这个键是不是在打死循环"。
    """
    async with repo.db.tx() as conn:
        await repo.claim(conn, key="k1", operation="op")
    assert (await repo.lookup("k1"))["attempt"] == 1, "claim 时 attempt 应为 1"

    async with repo.db.tx() as conn:
        await repo.settle(conn, key="k1", status="failed", error_code="RATE_LIMITED")
    assert (await repo.lookup("k1"))["attempt"] == 2, "失败后 attempt 未推进"

    async with repo.db.tx() as conn:
        await repo.settle(conn, key="k1", status="succeeded", result={"id": "S-1"})
    row = await repo.lookup("k1")
    assert row["attempt"] == 2, "成功也把 attempt 加了"
    assert row["status"] == "succeeded"
    assert row["result"] is not None


async def test_settle_refuses_a_status_outside_the_closed_set(
    repo: CapabilityRepository,
) -> None:
    """``in_flight`` 不能由 settle 写回。

    settle 的语义是"这次外部副作用有了确定结果"。允许写回 in_flight 就等于
    允许把一个已完成的键重新变成"别人正在做"，另一个进程会跟着等下去。
    """
    async with repo.db.tx() as conn:
        await repo.claim(conn, key="k2", operation="op")
        with pytest.raises(ValueError):
            await repo.settle(conn, key="k2", status="in_flight")


# --- 语句层护栏在真库上仍然成立 --------------------------------------------


async def test_row_level_guard_raises_on_a_foreign_row(
    repo: CapabilityRepository,
) -> None:
    """第 3 层（行级校验）是覆盖共享表的唯一一层。

    这里用同一个 schema 里塞一条别人 workspace_id 的行来模拟：权限层和连接层
    都拦不住它（表在自己 schema 里），只有行级校验能。**抛异常而不是过滤**——
    过滤会让上游过滤失效这件事永远不被发现。
    """
    async with repo.db.tx() as conn:
        rid = await repo.upsert_requirement(
            conn,
            tapd_id="F1",
            title="t",
            status="s",
            status_category=StatusCategory.ACTIVE,
        )
    await repo.db.execute(
        "UPDATE requirement SET workspace_id = 'other_ws' WHERE requirement_id = $1",
        uuid.UUID(str(rid)),
    )
    sql = "SELECT * FROM requirement WHERE workspace_id IS NOT NULL"
    # 裸 Database 拿得到这行——护栏不在 SQL 里，就在 Repository 这一层
    assert len(await repo.db.fetch(sql)) == 1
    with pytest.raises(IsolationViolation):
        await repo.fetch(sql, where="test.guard")


async def test_search_path_excludes_public(repo: CapabilityRepository) -> None:
    """连接层：``search_path`` 不含 ``public``。

    含了 public，漏写 schema 前缀的 SQL 会静默落到 public 建表/写数据——
    数据去了哪里要等到有人发现自己 schema 里少了一张表才知道。
    """
    sp = await repo.db.fetchval("SHOW search_path")
    assert "public" not in sp
    assert repo.ws in sp
