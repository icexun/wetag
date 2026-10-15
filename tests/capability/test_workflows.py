"""三条编排链路的失败纪律（``app/capability/workflows/``）。

这三个文件的正常路径很短，难的全在失败路径上，而失败路径恰好是线上不会报错的那些：
乐观锁冲突重试到把别人的段落搅乱、零写入被当成索引过期、旧版本被当成变更推送、
建卡前没拦住字段不全于是 TAPD 里长出僵尸需求。所以这里的断言几乎都在断言**拒绝**。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.capability.workflows.doc_update import (
    DocUpdateContext,
    DocUpdateWorkflow,
    refresh_index,
)
from app.capability.workflows.requirement_create import (
    RequirementCreateContext,
    RequirementCreateWorkflow,
)
from app.capability.repository import IndexRefresh
from app.capability.workflows.change_notify import Notifier
from app.contracts.ids import DocumentId, WorkspaceId
from app.contracts.ports import (
    DocumentMeta,
    DocumentSection,
    DocumentSnapshot,
    FetchScope,
    WorkspaceConfig,
    WriteResult,
)

WS = WorkspaceConfig(workspace_id=WorkspaceId("alpha"))


def _snapshot(revision: int, scope: FetchScope = FetchScope.OUTLINE) -> DocumentSnapshot:
    return DocumentSnapshot(
        meta=DocumentMeta(
            document_id=DocumentId("doc1"),
            title="支付超时策略",
            revision_id=revision,
            modified_time=datetime(2026, 9, 1, tzinfo=timezone.utc),
            content_hash="h1",
        ),
        scope=scope,
        sections=(DocumentSection(anchor="a1", heading="超时", text="30 秒", level=2),),
        outline=(("a1", "超时"),),
    )


class FakeDocuments:
    """可编排冲突次数的文档端口替身。"""

    def __init__(self, conflicts: int = 0) -> None:
        self._conflicts = conflicts
        self.revision = 7
        self.fetches: list[FetchScope] = []
        self.updates: list[Any] = []

    async def fetch(self, ref: Any, scope: FetchScope) -> DocumentSnapshot:
        self.fetches.append(scope)
        return _snapshot(self.revision, scope)

    async def update(self, cmd: Any, idem_key: Any) -> WriteResult:
        self.updates.append(cmd)
        if self._conflicts > 0:
            self._conflicts -= 1
            self.revision += 1  # 别人又改了一版
            return WriteResult(ok=False, conflict=True)
        return WriteResult(ok=True, revision_id=self.revision + 1)


# -- doc_update --------------------------------------------------------------
async def test_write_reads_outline_not_whole_document() -> None:
    """写入前只读 ``OUTLINE``。

    这一步只需要 revision 与章节锚点。用 ``WHOLE`` 在长文档上既慢又可能超
    ``max_output_bytes``，而超了之后的表现是写入前读取失败——一次纯粹自找的故障。
    """
    docs = FakeDocuments()
    wf = DocUpdateWorkflow(docs)
    ctx = DocUpdateContext(
        payload={"document_id": "doc1", "command": "append", "content": "新增一段"},
        workspace=WS,
    )

    result = await wf.execute(ctx, context=None)

    assert result.completed is True
    assert docs.fetches == [FetchScope.OUTLINE]
    assert FetchScope.WHOLE not in docs.fetches


async def test_optimistic_conflict_retries_exactly_once() -> None:
    """一次冲突重算 diff 再试；成功即止。"""
    docs = FakeDocuments(conflicts=1)
    wf = DocUpdateWorkflow(docs)
    ctx = DocUpdateContext(
        payload={"document_id": "doc1", "command": "append", "content": "x"},
        workspace=WS,
    )

    result = await wf.execute(ctx, context=None)

    assert result.completed is True
    assert ctx.retried is True
    assert len(docs.updates) == 2
    # 第二次必须带**重读到的**新 revision，否则重试必然再冲突一次
    assert docs.updates[1].base_revision_id > docs.updates[0].base_revision_id


async def test_second_conflict_stops_instead_of_looping() -> None:
    """连续两次冲突就停手，且明说改动没写进去。

    第二次冲突说明有人正在编辑。第三次第四次只会把我们的改动插进他正在写的段落
    中间——这里追求的不是送达率，是不把别人的文档搅乱。
    """
    docs = FakeDocuments(conflicts=5)
    wf = DocUpdateWorkflow(docs)
    ctx = DocUpdateContext(
        payload={"document_id": "doc1", "command": "append", "content": "x"},
        workspace=WS,
    )

    result = await wf.execute(ctx, context=None)

    assert result.completed is False
    assert len(docs.updates) == 2  # 不是 3、不是 5
    reason = result.incomplete_reason or ""
    assert "改动未写入" in reason and "人工确认" in reason


async def test_unknown_command_is_refused_not_downgraded_to_append() -> None:
    """不认识的命令一律拒绝，**不回退 append**。

    回退会把一次"替换某段"变成在文末追加一段，于是文档里同时存在新旧两份说法，
    而两份都像是有意写的。
    """
    docs = FakeDocuments()
    wf = DocUpdateWorkflow(docs)
    ctx = DocUpdateContext(
        payload={"document_id": "doc1", "command": "overwrite", "content": "x"},
        workspace=WS,
    )

    result = await wf.execute(ctx, context=None)

    assert result.completed is False
    assert "未知的更新命令" in (result.incomplete_reason or "")
    assert docs.updates == []  # 一次外部写都没发生


# -- refresh_index -----------------------------------------------------------
class FakeConn:
    async def __aenter__(self) -> "FakeConn":
        return self

    async def __aexit__(self, *a: object) -> None:
        return None


class FakeDb:
    def tx(self) -> FakeConn:
        return FakeConn()


class FakeRepo:
    def __init__(
        self,
        outcome: IndexRefresh,
        *,
        previous: list[dict[str, Any]] | None = None,
        subscribers: list[str] | None = None,
    ) -> None:
        self.db = FakeDb()
        self._outcome = outcome
        self._previous = previous
        self._subscribers = subscribers or []
        self.touched: list[str] = []
        self.written: list[dict[str, Any]] = []
        self.staled: list[str] = []
        self.stale_reasons: list[str] = []
        self.stale_max_failures: list[int] = []
        self.archive_outcome = "stale"

    async def get_index(self, document_id: Any) -> Any:
        if self._previous is None:
            return None
        return {"sections": self._previous, "workspace_id": "alpha"}

    async def refresh_index(self, conn: Any, **kw: Any) -> IndexRefresh:
        self.written.append(kw)
        return self._outcome

    async def touch_index(self, conn: Any, **kw: Any) -> None:
        self.touched.append(str(kw["document_id"]))

    async def mark_stale(self, conn: Any, **kw: Any) -> int:
        self.staled.append(str(kw["document_id"]))
        self.stale_reasons.append(str(kw["reason"]))
        return 1

    async def mark_stale_or_archive(self, conn: Any, **kw: Any) -> str:
        self.staled.append(str(kw["document_id"]))
        self.stale_reasons.append(str(kw["reason"]))
        self.stale_max_failures.append(kw.get("max_failures", 3))
        return self.archive_outcome

    async def backfill_embedding(self, conn: Any, **kw: Any) -> bool:
        return False

    async def subscribers(self, source: str, target: str) -> list[str]:
        assert (source, target) == ("document", "doc1")
        return list(self._subscribers)


async def _refresh(repo: FakeRepo, docs: Any = None, ids=("doc1",)):
    return await refresh_index(
        repository=repo,
        documents=docs or FakeDocuments(),
        document_ids=list(ids),
        notifier=Notifier(repo),
    )


async def test_zero_write_still_touches_refreshed_at() -> None:
    """内容没变时必须 ``touch_index``，否则稳定会被当成故障。"""
    repo = FakeRepo(IndexRefresh.UNCHANGED, subscribers=["chat-a"])

    result = await _refresh(repo)

    assert result.completed is True
    assert repo.touched == ["doc1"]
    assert result.metrics["touched"] == 1
    assert result.metrics["written"] == 0
    assert result.envelopes == [], "内容没变不该推送"


async def test_an_older_revision_is_marked_stale_not_touched_fresh() -> None:
    """被挡回的刷新必须标脏，**绝不能**走 ``touch_index``，也不推送。

    ``touch_index`` 推进 ``refreshed_at`` 并摘掉 stale；对一次被挡回的刷新这样做，
    就是把"根本没核对过内容"记成"已核对且新鲜"。
    """
    repo = FakeRepo(IndexRefresh.REJECTED_STALE, previous=[], subscribers=["chat-a"])

    result = await _refresh(repo)

    assert repo.touched == []
    assert repo.staled == ["doc1"]
    assert result.metrics["rejected"] == 1
    assert result.envelopes == []


async def test_content_change_notifies_subscribers_with_section_diff() -> None:
    old = [
        {"anchor": "a1", "heading": "超时", "excerpt": "10 秒", "hash": "old"},
        {"anchor": "a0", "heading": "背景", "excerpt": "x", "hash": "keep"},
    ]
    repo = FakeRepo(IndexRefresh.UPDATED, previous=old, subscribers=["chat-a", "chat-b"])

    result = await _refresh(repo)

    assert result.metrics["written"] == 1
    assert [e.chat_id for e in result.envelopes] == ["chat-a", "chat-b"]
    text = result.envelopes[0].payload["text"]
    assert "支付超时策略" in text
    assert "修改章节：超时" in text
    assert "删除章节：背景" in text
    assert "https://example.feishu.cn/docx/doc1" in text
    assert result.metrics["notified"] == 2


async def test_first_index_builds_baseline_without_notifying() -> None:
    """没有上一版就没有"变更"：首次入库只建基线。"""
    repo = FakeRepo(IndexRefresh.INSERTED, previous=None, subscribers=["chat-a"])

    result = await _refresh(repo)

    assert result.metrics["written"] == 1
    assert result.envelopes == []


async def test_title_filled_via_batch_meta() -> None:
    """fetch 拿不到 title 时用 batch_meta 补——doc token 不得顶替标题。"""
    from dataclasses import replace

    class NoTitleDocs(FakeDocuments):
        async def fetch(self, ref: Any, scope: FetchScope) -> DocumentSnapshot:
            snap = await super().fetch(ref, scope)
            return replace(snap, meta=replace(snap.meta, title=""))

        async def batch_meta(self, refs: Any) -> list[DocumentMeta]:
            return [DocumentMeta(document_id=DocumentId("doc1"), title="首页进度统一")]

    repo = FakeRepo(IndexRefresh.UPDATED, previous=[], subscribers=["chat-a"])

    result = await _refresh(repo, NoTitleDocs())

    assert repo.written[0]["title"] == "首页进度统一"
    assert "首页进度统一" in result.envelopes[0].payload["text"]


async def test_title_falls_back_to_doc_id_when_meta_unavailable() -> None:
    from dataclasses import replace

    class NoTitleDocs(FakeDocuments):
        async def fetch(self, ref: Any, scope: FetchScope) -> DocumentSnapshot:
            snap = await super().fetch(ref, scope)
            return replace(snap, meta=replace(snap.meta, title=""))

        async def batch_meta(self, refs: Any) -> list[DocumentMeta]:
            raise PermissionError("metas 无权限")

    repo = FakeRepo(IndexRefresh.UPDATED, previous=[], subscribers=["chat-a"])

    result = await _refresh(repo, NoTitleDocs())

    assert result.completed is True
    assert "[doc1]" in result.envelopes[0].payload["text"]


async def test_a_fetch_failure_marks_the_index_stale() -> None:
    """拉不到文档就必须标脏——``failures`` 计数随本轮消失，快照行却仍是干净的。"""

    class Boom(FakeDocuments):
        async def fetch(self, ref: Any, scope: FetchScope) -> DocumentSnapshot:
            raise PermissionError("403 无权限")

    repo = FakeRepo(IndexRefresh.UPDATED)

    result = await _refresh(repo, Boom())

    assert result.completed is False
    assert repo.staled == ["doc1"]
    assert result.metrics["stale_marked"] == 1
    assert "fetch_failed" in repo.stale_reasons[0]
    assert result.metrics["failed"] == 1


async def test_a_failure_to_mark_stale_is_itself_reported() -> None:
    class Boom(FakeDocuments):
        async def fetch(self, ref: Any, scope: FetchScope) -> DocumentSnapshot:
            raise PermissionError("403")

    repo = FakeRepo(IndexRefresh.UPDATED)

    async def no_stale(conn: Any, **kw: Any) -> str:
        raise RuntimeError("连接池空了")

    repo.mark_stale_or_archive = no_stale  # type: ignore[method-assign]

    result = await _refresh(repo, Boom())

    assert result.completed is False
    assert result.metrics["stale_marked"] == 0
    assert any("标脏也失败" in f for f in result.metrics["failures"])


async def test_persistent_fetch_failure_archives_instead_of_stale() -> None:
    class Boom(FakeDocuments):
        async def fetch(self, ref: Any, scope: FetchScope) -> DocumentSnapshot:
            raise PermissionError("404 文档不存在")

    repo = FakeRepo(IndexRefresh.UPDATED)
    repo.archive_outcome = "archived"

    result = await _refresh(repo, Boom())

    assert result.metrics["stale_marked"] == 0
    assert result.metrics["archived_failed"] == 1


async def test_not_found_fetch_failure_archives_immediately() -> None:
    """NOT_FOUND（文档已删除）是永久性失败：第一次就请求归档（max_failures=1）。"""
    from app.contracts.errors import PortUnavailable
    from app.platform.cli.result import CliErrorCode

    class Gone(FakeDocuments):
        async def fetch(self, ref: Any, scope: FetchScope) -> DocumentSnapshot:
            raise PortUnavailable(
                "lark.docs", CliErrorCode.NOT_FOUND.value, "Document page has been deleted"
            )

    repo = FakeRepo(IndexRefresh.UPDATED)
    repo.archive_outcome = "archived"

    result = await _refresh(repo, Gone())

    assert repo.stale_max_failures == [1]
    assert "NOT_FOUND" in repo.stale_reasons[0]
    assert result.metrics["archived_failed"] == 1


async def test_transient_fetch_failure_still_uses_the_slow_path() -> None:
    class Flaky(FakeDocuments):
        async def fetch(self, ref: Any, scope: FetchScope) -> DocumentSnapshot:
            raise TimeoutError("30s 未返回")

    repo = FakeRepo(IndexRefresh.UPDATED)

    await _refresh(repo, Flaky())

    assert repo.stale_max_failures == [3]


async def test_index_refresh_reads_whole_but_stores_only_excerpt() -> None:
    """快照要全文来切章节，但落库只留短摘与哈希（文档原文不落库）。"""
    repo = FakeRepo(IndexRefresh.UPDATED)
    docs = FakeDocuments()

    result = await _refresh(repo, docs)

    assert docs.fetches == [FetchScope.WHOLE]
    for d in repo.written[0]["sections"]:
        assert len(d["excerpt"]) <= 200
        assert d["anchor"] and d["hash"]
    assert result.metrics["written"] == 1
    assert result.metrics["touched"] == 0
    assert repo.staled == []


async def test_empty_document_list_is_not_a_failure() -> None:
    result = await refresh_index(
        repository=FakeRepo(IndexRefresh.UPDATED), documents=FakeDocuments()
    )
    assert result.completed is True
    assert result.metrics["refreshed"] == 0


# -- requirement_create ------------------------------------------------------
class FakeDraft:
    def __init__(self, *, ok: bool = True, ready: bool = True) -> None:
        self.ok = ok
        self.ready_for_creation = ready
        self.reason = "结构化节点不可用"
        self.confidence = 0.9
        self.title = "支付超时改 60 秒"
        self.background = "背景"
        self.goal = "目标"
        self.scope_in = ("a",)
        self.scope_out = ()
        self.acceptance_criteria = ("c",)
        self.owners = ("u1",)
        self.workspace_id = "alpha"
        self.chat_id = "chat1"
        self.topic_id = None
        self.tapd_category = "支付"
        self.affected_clients = ("android",)
        self.source_message_ids = ("m1",)
        self.missing_fields = () if ready else ("acceptance_criteria",)
        self.clarifications = ()


class FakeStructuring:
    def __init__(self, draft: FakeDraft) -> None:
        self._draft = draft

    async def extract(self, **kw: Any) -> FakeDraft:
        return self._draft


class FakeRequirements:
    def __init__(self) -> None:
        self.creates: list[Any] = []

    async def project_config(self, pid: str) -> object:
        return object()

    async def create(self, draft: Any, key: Any) -> Any:
        self.creates.append((draft, str(key)))

        class Created:
            tapd_id = "1001"

        return Created()


async def test_gate_stops_before_any_external_write() -> None:
    """字段不全时停在闸门前，**一次外部写都不发生**。

    "先建了再补"看起来更顺畅，代价是 TAPD 里堆积标题为"待补充"的僵尸需求，而它们
    会被下一轮同步进台账——一条没人填完的草稿开始干扰真实需求的变更通知。
    """
    req = FakeRequirements()
    wf = RequirementCreateWorkflow(
        req, structuring=FakeStructuring(FakeDraft(ready=False))
    )
    ctx = RequirementCreateContext(payload={"messages": ["m"]}, workspace=WS)

    result = await wf.execute(ctx, context=None)

    assert result.completed is False
    assert "待澄清" in (result.incomplete_reason or "")
    assert req.creates == []
    assert ctx.created is None


async def test_node_failure_does_not_produce_clarifications() -> None:
    """``ok=False``（节点没跑成）不等于字段不够，不该向群里提澄清问题。

    模型不可用时问出来的问题是随机的，人回答了也没用——更糟的是他会以为自己在
    推进这件事。
    """
    wf = RequirementCreateWorkflow(
        FakeRequirements(), structuring=FakeStructuring(FakeDraft(ok=False))
    )
    ctx = RequirementCreateContext(payload={"messages": ["m"]}, workspace=WS)

    result = await wf.execute(ctx, context=None)

    assert result.completed is False
    assert "结构化未完成" in (result.incomplete_reason or "")
    assert ctx.clarifications == ()


async def test_missing_idempotency_ledger_writes_but_says_so() -> None:
    """没有幂等台账时照旧建卡，但把"无防重保护"写进结果。

    不执行等于因为观测缺失而放弃能力；静默执行则会在重试时建出第二条需求而无人
    知晓。两者都不可接受，所以是"执行 + 声明"。
    """
    req = FakeRequirements()
    wf = RequirementCreateWorkflow(
        req, structuring=FakeStructuring(FakeDraft()), idempotency=None
    )
    ctx = RequirementCreateContext(
        payload={"messages": ["m"]}, workspace=WS, idem_key="task-42"
    )

    result = await wf.execute(ctx, context=None)

    assert result.completed is True
    assert len(req.creates) == 1
    assert "无防重保护" in str(result.metrics)


async def test_each_external_write_gets_its_own_sub_key() -> None:
    """TAPD 与文档各用自己的子键。

    共用一个键会让"TAPD 成了、文档没成"无法表达：重试时要么两个都跳过，要么两个
    都重做——后者会建出第二条需求。
    """
    req = FakeRequirements()
    wf = RequirementCreateWorkflow(req, structuring=FakeStructuring(FakeDraft()))
    ctx = RequirementCreateContext(
        payload={"messages": ["m"]}, workspace=WS, idem_key="task-42"
    )

    await wf.execute(ctx, context=None)

    assert req.creates[0][1] == "task-42:tapd"


async def test_doc_failure_keeps_the_created_requirement() -> None:
    """文档这步失败不撤销 TAPD，且整条链路仍算成功。

    撤销本身是一次外部写：它也会失败，而失败时我们既不知道需求还在不在，也已经把
    原始失败原因埋在两层异常底下。取而代之是记下到哪一步——``steps_done`` 里的
    "tapd" 是"外部世界已经被改了"的凭据，任何失败路径都不得清空它。
    """
    req = FakeRequirements()
    wf = RequirementCreateWorkflow(
        req, structuring=FakeStructuring(FakeDraft()), documents=None
    )
    ctx = RequirementCreateContext(payload={"messages": ["m"]}, workspace=WS)

    result = await wf.execute(ctx, context=None)

    assert result.completed is True  # 文档非关键步骤
    assert "tapd" in ctx.steps_done
    assert ctx.created is not None
    assert ctx.doc_ref is None  # 没建成就是没建成，不伪造引用
