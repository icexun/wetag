"""建需求链路（``app/capability/workflows/requirement_create.py``）。

这是全 Capability 唯一一条**对外写两个系统**的链路（TAPD 建需求、飞书建文档），
所以它守的不是"算得对"，而是"写出去的东西撤不回来"：

* 澄清闸门必须在任何外部写**之前**。放到建卡之后，TAPD 里会堆积标题为"待补充"的
  僵尸需求，而它们会被下一轮 ``list_active`` 同步进台账。
* 部分成功**不回滚**。撤销本身是一次外部写，它也会失败；失败时既不知道需求还在不
  在，也已经把原始失败原因埋在两层异常底下。取而代之是记下到哪一步。
* 每个外部写各有自己的子键。共用一个键会让"TAPD 成了、文档没成"无法表达。

``tests/capability/test_workflows.py`` 已覆盖闸门、子键、文档失败三条主干。本文件
补的是**协作面**：幂等重放、``_tapd_id`` / ``_to_port_draft`` 两个转换函数，
以及链路自身的结构性声明（谁开事务、谁非关键）。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.capability.workflows.requirement_create import (
    RequirementCreateContext,
    RequirementCreateWorkflow,
    _tapd_id,
    _to_port_draft,
)
from app.contracts.ports import WorkspaceConfig

# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

WS = WorkspaceConfig(workspace_id="alpha", tapd_project_id="6001")


class FakeDraft:
    """结构化产出的替身。字段名与 ``intelligence.RequirementDraft`` 对齐。"""

    def __init__(self, **kw: Any) -> None:
        self.ok = True
        self.ready_for_creation = True
        self.reason = ""
        self.confidence = 0.9
        self.title = "额度中心支持分期"
        self.background = "用户希望大额消费可分期"
        self.goal = "上线分期能力"
        self.scope_in = ("分期试算",)
        self.scope_out = ()
        self.acceptance_criteria = ("试算结果与账单一致",)
        self.owners = ("u1",)
        self.workspace_id = "alpha"
        self.chat_id = "oc_1"
        self.topic_id = None
        self.tapd_category = "基础能力"
        self.affected_clients = ("android",)
        self.source_message_ids = ("om_1",)
        self.missing_fields: tuple[str, ...] = ()
        self.clarifications: tuple[Any, ...] = ()
        for k, v in kw.items():
            setattr(self, k, v)


class FakeStructuring:
    def __init__(self, draft: Any) -> None:
        self._draft = draft
        self.calls = 0
        self.kwargs: dict[str, Any] = {}

    async def extract(self, **kw: Any) -> Any:
        self.calls += 1
        self.kwargs = kw
        return self._draft


class FakeRequirements:
    def __init__(self, *, config_boom: bool = False, tapd_id: Any = "1001") -> None:
        self.creates: list[tuple[Any, str]] = []
        self._config_boom = config_boom
        self._tapd_id = tapd_id

    async def project_config(self, pid: str) -> Any:
        if self._config_boom:
            raise RuntimeError("TAPD 不可达")
        return object()

    async def create(self, draft: Any, key: Any) -> Any:
        self.creates.append((draft, str(key)))
        tapd_id = self._tapd_id
        return type("Created", (), {"tapd_id": tapd_id})()


class FakeIdemOutcome:
    def __init__(self, value: Any, replayed: bool) -> None:
        self.value = value
        self.replayed = replayed


class FakeIdempotency:
    """``guard`` 的两种形态：正常执行，或命中台账直接重放旧结果。"""

    def __init__(self, *, replay: Any = None) -> None:
        self._replay = replay
        self.keys: list[str] = []

    async def guard(self, key: Any, op: str, action: Any, **kw: Any) -> FakeIdemOutcome:
        self.keys.append(str(key))
        if self._replay is not None:
            return FakeIdemOutcome(self._replay, True)
        return FakeIdemOutcome(await action(), False)


def wf(**kw: Any) -> tuple[RequirementCreateWorkflow, FakeRequirements]:
    req = kw.pop("requirements", None) or FakeRequirements()
    kw.setdefault("structuring", FakeStructuring(FakeDraft()))
    return RequirementCreateWorkflow(req, **kw), req


def ctx(**kw: Any) -> RequirementCreateContext:
    kw.setdefault("payload", {"messages": [{"message_id": "om_1", "text": "分期"}]})
    kw.setdefault("workspace", WS)
    return RequirementCreateContext(**kw)


# ---------------------------------------------------------------------------
# 一、幂等：重放不建第二条
# ---------------------------------------------------------------------------


async def test_an_idempotency_hit_replays_instead_of_creating_again() -> None:
    """键已 ``succeeded`` 时重放旧结果，``create`` 一次都不调。

    TAPD 没有幂等键，所以"重试"在这里等于"第二条需求"。判重责任全压在
    ``Idempotency.guard`` 上，链路本身不做任何先查后写的补救。
    """
    idem = FakeIdempotency(replay={"tapd_id": "1001"})
    w, req = wf(idempotency=idem)
    c = ctx(idem_key="task-42")

    result = await w.execute(c, context=None)

    assert result.completed is True
    assert req.creates == [], "幂等命中却又建了一条"
    assert result.metrics["replayed"] is True
    assert result.metrics["tapd_id"] == "1001"
    assert "重放" in str(result.metrics)


async def test_a_replayed_dict_result_still_yields_a_tapd_id() -> None:
    """重放路径拿到的是 ``result`` jsonb 还原出来的 dict，不是 ``Requirement``。

    只写 ``getattr(value, "tapd_id")`` 的话，重放时 tapd_id 恒为 None，落库那步
    以"无 tapd_id"跳过——一条已经建好的需求从此不进台账，变更也永远不会被通知。
    """
    idem = FakeIdempotency(replay={"tapd_id": "1001"})
    w, _ = wf(idempotency=idem, repository=object())
    c = ctx(idem_key="task-42")

    result = await w.execute(c, context=None)

    assert result.state_patch["last_created_requirement"] == "1001"


async def test_the_default_key_is_still_namespaced_per_write() -> None:
    """没有 ``idem_key`` 时也要带 ``:tapd`` 后缀——否则文档那步会撞上同一个键。"""
    idem = FakeIdempotency()
    w, _ = wf(idempotency=idem)
    await w.execute(ctx(), context=None)
    assert idem.keys == ["req:tapd"]


# ---------------------------------------------------------------------------
# 二、前置不可用：区分"停下"与"降级"
# ---------------------------------------------------------------------------


async def test_a_missing_structuring_node_stops_the_chain() -> None:
    """结构化节点未装配 ⇒ 停。

    这一步不能降级成"用标题当需求建卡"：那会在 TAPD 里建出一条内容是群聊片段的
    需求，署着 NPC 的名字。
    """
    w = RequirementCreateWorkflow(FakeRequirements(), structuring=None)
    result = await w.execute(ctx(), context=None)
    assert result.completed is False
    assert "结构化节点不可用" in (result.incomplete_reason or "")


async def test_an_empty_payload_stops_before_the_model_call() -> None:
    st = FakeStructuring(FakeDraft())
    w, _ = wf(structuring=st)
    result = await w.execute(ctx(payload={"messages": []}), context=None)
    assert result.completed is False and st.calls == 0


async def test_a_missing_requirement_port_stops_at_the_write() -> None:
    w = RequirementCreateWorkflow(None, structuring=FakeStructuring(FakeDraft()))
    result = await w.execute(ctx(), context=None)
    assert result.completed is False
    assert "需求端口不可用" in (result.incomplete_reason or "")


async def test_an_unreachable_project_config_is_not_fatal() -> None:
    """取不到项目配置只影响 ``tapd_category`` 的取值范围，不该停链路。

    停下来的代价是 TAPD 一抖动就没人能建需求；继续的代价只是分类落到默认值。
    """
    req = FakeRequirements(config_boom=True)
    w, _ = wf(requirements=req)
    result = await w.execute(ctx(), context=None)
    assert result.completed is True
    assert len(req.creates) == 1


async def test_the_project_config_is_passed_into_structuring() -> None:
    """分类枚举必须传进结构化节点，否则模型会编一个不在 TAPD 里的分类。"""
    st = FakeStructuring(FakeDraft())
    w, _ = wf(structuring=st)
    await w.execute(ctx(), context=None)
    assert st.kwargs["project"] is not None


async def test_no_project_id_means_no_config_lookup() -> None:
    st = FakeStructuring(FakeDraft())
    w, _ = wf(structuring=st)
    await w.execute(ctx(workspace=WorkspaceConfig(workspace_id="alpha")), context=None)
    assert st.kwargs["project"] is None


# ---------------------------------------------------------------------------
# 三、链路的结构性声明
# ---------------------------------------------------------------------------


def test_the_persisting_step_never_awaits_an_external_call() -> None:
    """落库那步开事务，于是它**不能**声明 ``calls_external``。

    ``Step.__post_init__`` 会在装配期拒绝两者同真，但那只挡住"两个都写"。这条盯
    的是相反的疏漏：把外部调用悄悄搬进 ``_persist`` 而忘了改声明位，那时事务里就
    套了一次 CLI 调用，对端变慢会耗干连接池。
    """
    steps = {s.name: s for s in wf()[0].steps(ctx())}
    assert steps["persist"].opens_tx is True
    assert steps["persist"].calls_external is False
    for name in ("structure", "create_tapd", "create_doc"):
        assert steps[name].calls_external is True
        assert steps[name].opens_tx is False


def test_only_the_document_step_is_non_critical() -> None:
    """文档失败不该让用户以为需求没建成——他会手动再建一条。

    反过来把 ``create_tapd`` 也标成非关键会更糟：建卡失败而链路报成功，群里回
    "已建单"，而 TAPD 里什么都没有。
    """
    steps = wf()[0].steps(ctx())
    assert [s.name for s in steps if not s.critical] == ["create_doc"]


def test_the_order_is_gate_before_every_external_write() -> None:
    names = [s.name for s in wf()[0].steps(ctx())]
    assert names.index("gate") < names.index("create_tapd")
    assert names.index("gate") < names.index("create_doc")
    assert names == ["structure", "gate", "create_tapd", "create_doc", "persist"]


def test_no_step_declares_a_compensation() -> None:
    """**刻意没有补偿**。撤销 TAPD 需求本身是一次外部写，而它在 ``CliExecutor``
    的命令登记表里根本没登记（裁决 #10）——"回滚"在这里连代码路径都不存在。
    """
    assert all(s.compensate is None for s in wf()[0].steps(ctx()))


async def test_the_document_reference_is_never_fabricated() -> None:
    """文档没建成就是没建成。伪造一个 doc_ref 会让索引去 fetch 一个不存在的文档，
    于是每 3 分钟一次 ``NOT_FOUND``，而这条需求永远拿不到可比对的章节。
    """
    for docs, want in ((None, "未装配"), (object(), "人工创建")):
        w, _ = wf(documents=docs)
        c = ctx()
        result = await w.execute(c, context=None)
        assert result.completed is True
        assert c.doc_ref is None
        assert want in str(result.metrics)


async def test_the_document_step_does_not_improvise_a_create() -> None:
    """``DocumentPort`` 只声明了 fetch/batch_meta/list_tree/update 四个方法
    （doc2 §5.1）。没有 create 就是没有——用 update 去拼一个出来是偷偷改接口语义，
    而接口变更不在本次范围。记为已知缺口比"看起来能用"诚实。
    """
    class Docs:
        def __getattr__(self, name: str):
            raise AssertionError(f"文档这步不该调用端口的 {name}")

    w, _ = wf(documents=Docs())
    result = await w.execute(ctx(), context=None)
    assert result.completed is True


async def test_a_create_without_a_tapd_id_is_not_persisted() -> None:
    """建卡回包里没有 tapd_id 时**不落库**，而不是把 ``"None"`` 写成需求标识。

    这一路是真会发生的：CLI / MCP 的回包字段改了名，创建其实成功了但我们认不出是
    哪一条。此时落库比不落库更糟——``last_created_requirement`` 指向一个不存在的
    标识，链路一路报成功，下一次重试时也看不出"上次到底建成没建成"。跳过并留下
    原因，才能让补偿任务有东西可查。
    """
    for bad in (None, ""):
        req = FakeRequirements(tapd_id=bad)
        w, _ = wf(requirements=req, repository=object())
        c = ctx()

        result = await w.execute(c, context=None)

        assert req.creates, "建卡这步根本没跑，这条测的就不是落库了"
        assert result.completed is True
        assert "last_created_requirement" not in (result.state_patch or {}), (
            f"tapd_id={bad!r} 却把它写成了需求标识"
        )
        assert "tapd_id" in str(result.metrics), "跳过落库却没留下原因"


# ---------------------------------------------------------------------------
# 四、两个转换函数
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, want",
    [
        ({"tapd_id": "1001"}, "1001"),
        ({"tapd_id": 1001}, "1001"),
        ({"tapd_id": None}, None),
        ({}, None),
        (None, None),
    ],
)
def test_tapd_id_reads_both_shapes(value: Any, want: str | None) -> None:
    assert _tapd_id(value) == want


def test_tapd_id_reads_an_object() -> None:
    class R:
        tapd_id = "1001"

    assert _tapd_id(R()) == "1001"
    assert _tapd_id(object()) is None


def test_the_port_draft_cannot_express_not_ready() -> None:
    """闸门之后的草稿连"还没准备好"这个字段都没有。

    两个同名 ``RequirementDraft`` 是刻意的：``intelligence`` 那个带
    ``ready_for_creation`` / ``clarifications``（决定要不要写），``ports`` 那个只
    带写什么。于是端口层拿不到一份未就绪的草稿——它无从表达。
    """
    port = _to_port_draft(FakeDraft())
    assert not hasattr(port, "ready_for_creation")
    assert not hasattr(port, "clarifications")
    assert port.title == "额度中心支持分期"
