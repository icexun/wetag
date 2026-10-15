"""入口分派的三条纪律（``app/capability/router.py``）。

这里守的不是"分派对不对"——那种错会在第一次联调就暴露。守的是三种**看起来正常
返回、实则出事了**的失败：漏登记的 kind 被当成空成功、能力没装配被渲染成"一切
正常"、越权被当成普通失败退避重试。三者的共同点是线上没有红灯，所以只能由断言
盯着。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.capability.router import CapabilityRouter
from app.container import CapabilityContainer, PortStatus
from app.contracts.errors import IsolationViolation
from app.contracts.job_result import JobResult
from app.contracts.ids import (
    BotId,
    ChatId,
    IdemKey,
    OrganizationId,
    TaskId,
    WorkspaceId,
)
from app.contracts.ports import WorkspaceConfig
from app.contracts.space_context import SpaceContext
from app.contracts.task import (
    AgenticTask,
    DeterministicJob,
    JobKind,
    WorkflowKind,
)

WS = "alpha"


def _context(workspace_id: str = WS) -> SpaceContext:
    return SpaceContext(
        organization_id=OrganizationId("org1"),
        workspace_id=WorkspaceId(workspace_id),
        bot_id=BotId("bot1"),
        chat_id=ChatId("chat1"),
    )


def _container(**kw: object) -> CapabilityContainer:
    """一个只装了身份的容器。

    其余端口默认 None，正是今日最常见的形态（R1/R2/R4），也是纪律二要守的那一种。
    """
    return CapabilityContainer(
        workspace=WorkspaceConfig(workspace_id=WorkspaceId(WS)),
        cli=None,  # type: ignore[arg-type]  # 本文件不触发任何 CLI
        statuses=(
            PortStatus("db", False, reason="PG 未装配"),
            PortStatus("documents", False, reason="lark-cli 未授权"),
            PortStatus("requirements", False, reason="tapd-cli 不存在"),
        ),
        **kw,  # type: ignore[arg-type]
    )


def _task(kind: WorkflowKind, **payload: object) -> AgenticTask:
    return AgenticTask(
        task_id=TaskId("t1"),
        kind=kind,
        context=_context(),
        payload=dict(payload),
        idem_key=IdemKey("k1"),
    )


# -- 纪律一：未知类型不是空结果 ---------------------------------------------
async def test_unregistered_task_kind_is_incomplete_not_empty_success() -> None:
    """漏登记的 WorkflowKind 必须报未完成，且 reason 里带出是哪个 kind。

    若返回 ``completed=True`` 且无 envelope，Runtime 会把这条 Task 标成成功并删掉。
    没有任何人收到提示，那条需求变更就此消失——这是本仓最难发现的一类缺陷。
    """
    router = CapabilityRouter(_container())

    # QUERY_ANSWER 属 Control，刻意不在分派表里
    result = await router.handle_task(_task(WorkflowKind.QUERY_ANSWER))

    assert result.completed is False
    assert "query_answer" in (result.incomplete_reason or "")
    assert not result.has_effects


async def test_unregistered_job_kind_is_incomplete() -> None:
    router = CapabilityRouter(_container())
    job = DeterministicJob(kind=JobKind.DOC_POLL, context=_context())

    result = await router.handle_job(job)

    assert result.completed is False
    assert "doc_poll" in (result.incomplete_reason or "")



async def test_ports_unavailable_reasons_reach_the_caller() -> None:
    """端口不可用时，reason 必须带出装配期记录的具体原因。

    卡片上写"执行失败"等于让人去翻日志；写"tapd-cli 不存在"才可能有人去装它。
    """
    router = CapabilityRouter(_container())

    created = await router.handle_task(_task(WorkflowKind.REQUIREMENT_CREATE))
    updated = await router.handle_task(_task(WorkflowKind.DOC_UPDATE))

    assert created.completed is False
    assert "tapd-cli 不存在" in (created.incomplete_reason or "")
    assert updated.completed is False
    assert "lark-cli 未授权" in (updated.incomplete_reason or "")


async def test_not_delivered_is_distinct_from_unregistered() -> None:
    """今日不交付 ≠ 没实现。

    ``REQUIREMENT_UPDATE`` 留在分派表里并显式说明外部依赖缺失（R1）。从表里删掉它
    会走"未登记"分支，说法变成"无对应处理器"——那是实现缺口，Demo 里要承认的东西
    不一样。
    """
    router = CapabilityRouter(_container())

    result = await router.handle_task(_task(WorkflowKind.REQUIREMENT_UPDATE))

    assert result.completed is False
    reason = result.incomplete_reason or ""
    assert "今日不可用" in reason and "TAPD" in reason
    assert "无对应处理器" not in reason


# -- 纪律三：IsolationViolation 穿透 ----------------------------------------
async def test_isolation_violation_is_not_converted_to_job_result() -> None:
    """归属不符必须抛出，不得收敛成"未完成"。

    收敛掉它会让 Runtime 按普通失败退避重试，一次配置错误变成一串越权尝试，而监控
    上只看到"某任务重试了几次"。§4.2 要求它告警且不重试，所以它穿透入口向上抛。
    """
    router = CapabilityRouter(_container())
    foreign = AgenticTask(
        task_id=TaskId("t1"),
        kind=WorkflowKind.DOC_UPDATE,
        context=_context("beta"),  # 容器是 alpha
    )

    with pytest.raises(IsolationViolation):
        await router.handle_task(foreign)


async def test_ownership_checked_before_handler_lookup() -> None:
    """未知 kind 也要先过归属校验。

    否则一条串了 workspace 的任务只要 kind 恰好没登记，就会安静地返回"未登记"，
    而那次越权没有任何记录。
    """
    router = CapabilityRouter(_container())
    foreign = AgenticTask(
        task_id=TaskId("t1"),
        kind=WorkflowKind.QUERY_ANSWER,
        context=_context("beta"),
    )

    with pytest.raises(IsolationViolation):
        await router.handle_task(foreign)

    job = DeterministicJob(kind=JobKind.DOC_POLL, context=_context("beta"))
    with pytest.raises(IsolationViolation):
        await router.handle_job(job)



# -- 其余异常：收敛但不静默 -------------------------------------------------
async def test_unexpected_exception_becomes_incomplete_with_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未预期异常收敛成未完成，且 reason 里带类型名与位置。

    收敛是为了不让一个 bug 把 Runtime 的 worker 打挂；带类型名是为了收敛之后还能
    查得到。两者缺一都不行——静默收敛比崩溃更难查。
    """
    import app.capability.workflows.doc_update as du

    async def _boom(self: object, ctx: object, **kw: object) -> object:
        raise RuntimeError("文档链路炸了")

    monkeypatch.setattr(du.DocUpdateWorkflow, "execute", _boom)
    router = CapabilityRouter(_container(documents=object()))

    result = await router.handle_task(_task(WorkflowKind.DOC_UPDATE))

    assert result.completed is False
    reason = result.incomplete_reason or ""
    assert "RuntimeError" in reason and "doc_update" in reason


async def test_isolation_violation_from_inside_a_handler_still_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """越权不只发生在入口校验。处理器里任何一次落库都可能抛它，也必须穿透。

    ``_run`` 的 ``except Exception`` 会把它收敛成"未完成 + 原因"，于是 Runtime 按
    普通失败退避重试。入口校验那条测试杀不到这里：入口校验发生在 ``_run`` 之外。
    """
    import app.capability.workflows.doc_update as du

    async def _boom(self: object, ctx: object, **kw: object) -> object:
        raise IsolationViolation("doc_index", "alpha", "beta")

    monkeypatch.setattr(du.DocUpdateWorkflow, "execute", _boom)
    router = CapabilityRouter(_container(documents=object()))

    with pytest.raises(IsolationViolation):
        await router.handle_task(_task(WorkflowKind.DOC_UPDATE))


async def test_partial_index_refresh_failure_is_not_claimed_as_fresh() -> None:
    """单篇失败不中断整轮，但整轮**不得**报成功。

    单篇失败继续是对的：一篇文档权限不足不该让其余几十篇都刷不上。但只要有失败，
    "索引已是最新"这个结论就会掩盖掉那几篇一直刷不上的文档——于是它们永远 stale，
    而它们的变更通知永久停摆，没有人知道为什么。失败类型要能从 metrics 里查回。
    """

    class Boom:
        def __getattr__(self, name: str) -> object:
            raise RuntimeError("端口炸了")

    router = CapabilityRouter(_container(repository=Boom(), documents=Boom()))
    job = DeterministicJob(
        kind=JobKind.INDEX_REFRESH,
        context=_context(),
        payload={"document_ids": ["r1"]},
    )

    result = await router.handle_job(job)

    assert result.completed is False
    assert "索引不完整" in (result.incomplete_reason or "")
    assert result.metrics["failed"] == 1
    assert any("RuntimeError" in f for f in result.metrics["failures"])


async def test_index_refresh_forwards_the_embedder_or_it_erases_vectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3 分钟一轮的刷新必须把 embedder 带下去，否则它会**擦掉**已有向量。

    upsert 用 COALESCE 保住旧向量，但内容变过的文档需要新向量；不传 embedder，
    语义检索会随着文档被编辑而慢慢失效，全程无异常、无日志。
    """
    import app.capability.workflows.doc_update as du

    seen: dict[str, object] = {}

    async def _spy(**kw: object) -> JobResult:
        seen.update(kw)
        return JobResult()

    monkeypatch.setattr(du, "refresh_index", _spy)
    sentinel = object()
    router = CapabilityRouter(
        _container(repository=object(), documents=object(), embedder=sentinel)
    )

    await router.handle_job(DeterministicJob(kind=JobKind.INDEX_REFRESH, context=_context()))

    assert "embedder" in seen, "刷新没传 embedder，下一轮会把索引里的向量擦成 NULL"
    assert seen["embedder"] is sentinel


async def test_index_refresh_requires_both_pg_and_documents() -> None:
    """索引刷新缺任一依赖都要说清缺的是哪个。

    "索引未刷新"这句话本身没有可操作性：缺 PG 与缺文档授权的处置完全不同。
    """
    router = CapabilityRouter(_container())
    job = DeterministicJob(kind=JobKind.INDEX_REFRESH, context=_context())

    result = await router.handle_job(job)

    assert result.completed is False
    assert "PG 不可用" in (result.incomplete_reason or "")

    router2 = CapabilityRouter(_container(repository=object()))
    result2 = await router2.handle_job(job)
    assert "lark-cli 未授权" in (result2.incomplete_reason or "")


async def test_index_refresh_marks_unverified_when_documents_port_is_down() -> None:
    """文档端口不可用时，请求到的文档必须标「未核实」，不能静默返回。

    端口故障时直接返回的后果：任务 SUCCEEDED、doc_poll 的版本基线已推进，
    这篇文档要等下一次内容变更才会再被刷新——期间它看起来是干净的。
    """

    class _Tx:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *a: object) -> bool:
            return False

    class Repo:
        def __init__(self) -> None:
            self.db = SimpleNamespace(tx=lambda: _Tx())
            self.marked: list[tuple[str, str]] = []

        async def mark_stale_unverified(self, conn: object, *, document_id: object, reason: str) -> int:
            self.marked.append((str(document_id), reason))
            return 1

    repo = Repo()
    router = CapabilityRouter(_container(repository=repo))  # documents=None
    job = DeterministicJob(
        kind=JobKind.INDEX_REFRESH,
        context=_context(),
        payload={"document_ids": ["doc-gone-1", "doc-gone-2"]},
    )

    result = await router.handle_job(job)

    assert result.completed is False
    assert repo.marked == [
        ("doc-gone-1", "refresh_skipped:documents_port_down"),
        ("doc-gone-2", "refresh_skipped:documents_port_down"),
    ], "端口不可用时请求到的文档没有被标「未核实」"


# -- 建卡链路的装配 ----------------------------------------------------------
class _Structuring:
    """够用的结构化替身：直接给一份字段齐全、可建卡的草稿。"""

    async def extract(self, **kw: object) -> object:
        return type(
            "Draft",
            (),
            {
                "ok": True,
                "ready_for_creation": True,
                "reason": "",
                "confidence": 0.9,
                "title": "额度中心支持分期",
                "background": "用户希望大额消费可分期",
                "goal": "上线分期能力",
                "scope_in": ("分期试算",),
                "scope_out": (),
                "acceptance_criteria": ("试算结果与账单一致",),
                "owners": ("u1",),
                "workspace_id": WS,
                "chat_id": "chat1",
                "topic_id": None,
                "tapd_category": "基础能力",
                "affected_clients": ("android",),
                "source_message_ids": ("om_1",),
                "missing_fields": (),
                "clarifications": (),
            },
        )()


async def test_the_create_workflow_gets_the_idempotency_key() -> None:
    """Task 的幂等键必须接到建卡链路上。

    丢了它，``Idempotency.guard`` 用的是默认键 ``req:tapd``——于是同一个业务线里
    **所有**建卡共用一个键：第一条建成之后，第二条不同的需求会被判成幂等命中并
    重放第一条的结果。这比"重试建出两条"更隐蔽：没有重复数据，只是有需求凭空消失。
    """
    seen: list[object] = []

    class Requirements:
        async def project_config(self, pid: str) -> object:
            raise AssertionError("本测不该拉配置")

        async def create(self, draft: object, key: object) -> object:
            seen.append(str(key))
            return type("Created", (), {"tapd_id": "1001"})()

    router = CapabilityRouter(
        _container(requirements=Requirements(), structuring=_Structuring())
    )

    await router.handle_task(
        AgenticTask(
            task_id=TaskId("t1"),
            kind=WorkflowKind.REQUIREMENT_CREATE,
            context=_context(),
            payload={"messages": [{"message_id": "om_1", "text": "要分期"}]},
            idem_key=IdemKey("chat1:om_1"),
        )
    )

    assert seen == ["chat1:om_1:tapd"], f"幂等键没接上：{seen}"


# -- Loop-3 回复规约（loop_digest）------------------------------------------


class _LoopRepo:
    def __init__(self, *, down_votes=()):
        self._down_votes = list(down_votes)
        self.rule_changes = []

    async def loop_down_votes(self, *, window_days=7):
        return list(self._down_votes)

    async def record_rule_change(self, **kw):
        self.rule_changes.append(kw)


def _loop_job(chat_id: str = "chat1") -> DeterministicJob:
    return DeterministicJob(
        kind=JobKind.LOOP_DIGEST,
        context=SpaceContext(
            organization_id=OrganizationId("org1"),
            workspace_id=WorkspaceId(WS),
            bot_id=BotId("bot1"),
            chat_id=ChatId(chat_id),
        ),
    )


async def test_loop_digest_incomplete_without_repository() -> None:
    """PG 未装配时必须显式未完成，而不是空成功（纪律二）。"""
    router = CapabilityRouter(_container())

    result = await router.handle_job(_loop_job())

    assert result.completed is False
    assert "PG" in (result.incomplete_reason or "")


class _RulesWriterFake:
    """biz 层规则存取的最小假实现（对齐 RulesRepository 签名）。"""

    def __init__(self):
        self.content = ""
        self.version = 0

    async def load_biz_rules(self):
        if self.version == 0:
            return None
        return SimpleNamespace(content=self.content, version=self.version)

    async def save_biz_rules(self, content, *, updated_by=None):
        self.content = content
        self.version += 1
        return self.version


class _Loop3Agent:
    async def complete(self, schema, *, system, user, trace_id=None):
        from app.analytics.loops.loop3_push import RuleDraft
        from app.capability.agents.structured import StructuredResult

        return StructuredResult(ok=True, data=RuleDraft(
            key="no-narration", rule="回复只给结论，不描述工具过程", rationale="测试",
        ))



_VOTES = [{"reason": ["太啰嗦"], "comment": "说了半天没结论"} for _ in range(4)]


async def test_loop_digest_push_loop_applies_prompt_rule() -> None:
    """Loop-3：👎 超阈值 → 提炼规约写入 biz 层并留痕，并推送通知。"""
    repo = _LoopRepo(down_votes=_VOTES)
    writer = _RulesWriterFake()
    router = CapabilityRouter(_container(repository=repo, rules_writer=writer, agent=_Loop3Agent()))

    result = await router.handle_job(_loop_job())

    assert result.completed
    assert result.metrics["push_rules_applied"] == ["loop3-no-narration"]
    assert "loop3-no-narration: 回复只给结论" in writer.content
    assert any(c["kind"] == "prompt" for c in repo.rule_changes)
    assert result.envelopes[0].chat_id == "chat1"
    assert "回复规约" in result.envelopes[0].payload["text"]


async def test_loop_digest_silent_when_nothing_new() -> None:
    """没有 👎：不推群——空转的周报是噪音。"""
    router = CapabilityRouter(
        _container(repository=_LoopRepo(), rules_writer=_RulesWriterFake(), agent=_Loop3Agent())
    )

    result = await router.handle_job(_loop_job())

    assert result.completed
    assert not result.envelopes


async def test_loop_digest_falls_back_to_alert_chat_provider() -> None:
    """调度触发的任务没有群上下文（chat_id=system）：回退到最近活跃群。"""

    async def _provider() -> str:
        return "chat_ops"

    router = CapabilityRouter(
        _container(
            repository=_LoopRepo(down_votes=_VOTES),
            rules_writer=_RulesWriterFake(),
            agent=_Loop3Agent(),
            alert_chat_provider=_provider,
        )
    )

    result = await router.handle_job(_loop_job("system"))

    assert result.envelopes[0].chat_id == "chat_ops"
