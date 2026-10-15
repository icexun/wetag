"""需求创建编排（doc2 §6.1 / §6.2）：结构化 → 澄清闸门 → 建卡 → 落库。

这条链路是全模块唯一一条**对外写两个系统**的链路（TAPD 建需求、飞书建文档），
所以它承担了两个最难的问题。

## 一、部分成功不回滚

TAPD 建成了、飞书文档没建成，这时**不撤销 TAPD 那条需求**。理由不是"回滚太难"，
是撤销本身是一次外部写：撤销请求也会失败，而失败时我们既不知道需求还在不在，也
已经把"原始失败原因"埋在两层异常底下。更糟的是 TAPD 的删除操作在
``CliExecutor`` 里根本没登记（裁决 #10），所以"回滚"在这里连代码路径都不存在。

取而代之：记下到哪一步、把缺的那一步做成补偿任务，**复用同一个幂等键**。于是重试
不会建出第二条需求——``Idempotency.guard`` 看到键已 ``succeeded`` 就重放旧结果。
这也是为什么每一个外部写都各有自己的子键（``:tapd`` / ``:doc``）：共用一个键会让
"TAPD 成了、文档没成"无法表达，重试时要么两个都跳过，要么两个都重做。

## 二、澄清闸门在外部写之前

``draft.ready_for_creation`` 为假时链路**停在这里**，产出澄清问题而不是建卡。
把这个判断放到建卡之后（"先建了再补"）看起来更顺畅，代价是 TAPD 里堆积标题为
"待补充"的僵尸需求，且会被下一轮 ``list_active`` 同步进来，触发无意义的变更通知。

注意区分 ``ok=False``（结构化节点本身没跑成）与 ``ready_for_creation=False``
（跑成了但字段不够）：前者不该向群里提澄清问题——模型不可用时问出来的问题是随机的，
人回答了也没用。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from app.capability.workflows.base import Step, StepOutcome, Workflow
from app.contracts.ids import IdemKey, WorkspaceId
from app.contracts.ports import RequirementDraft as PortDraft
from app.contracts.ports import WorkspaceConfig
from app.contracts.space_context import SpaceContext

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RequirementCreateContext:
    """链路上下文。

    ``created`` / ``doc_created`` 是"外部世界已经被改了"的记录。它们只在成功后
    置真，且置真之后**任何失败路径都不得清空它们**——清空等于假装没建过，下一次
    重试就会建出第二条。
    """

    payload: Mapping[str, Any]
    workspace: WorkspaceConfig
    context: SpaceContext | None = None
    idem_key: str | None = None
    trace_id: str | None = None

    draft: Any = None
    created: Any = None  # Requirement（TAPD 已建）
    doc_ref: Any = None  # DocumentRef（飞书已建）
    clarifications: tuple[Any, ...] = ()
    steps_done: list[str] = field(default_factory=list)


class RequirementCreateWorkflow(Workflow[RequirementCreateContext]):
    """§6.1 建需求链路。"""

    name = "requirement_create"

    def __init__(
        self,
        requirements: Any,
        *,
        structuring: Any = None,
        repository: Any = None,
        idempotency: Any = None,
        documents: Any = None,
    ) -> None:
        self._req = requirements
        self._structuring = structuring
        self._repo = repository
        self._idem = idempotency
        self._docs = documents

    def steps(
        self, ctx: RequirementCreateContext
    ) -> Sequence[Step[RequirementCreateContext]]:
        return (
            Step("structure", self._structure, calls_external=True),
            Step("gate", self._gate),
            Step("create_tapd", self._create_tapd, calls_external=True),
            # 文档非关键：TAPD 那条已经建好了，文档失败不该让整条链路报"失败"并让
            # 用户以为需求没建成——他会手动再建一条。
            Step("create_doc", self._create_doc, calls_external=True, critical=False),
            Step("persist", self._persist, opens_tx=True),
        )

    # -- steps ------------------------------------------------------------
    async def _structure(self, ctx: RequirementCreateContext) -> StepOutcome:
        if self._structuring is None:
            return StepOutcome.fail("结构化节点不可用（语义节点未装配），无法抽取需求字段")

        messages = list(ctx.payload.get("messages") or ())
        if not messages:
            return StepOutcome.fail("payload 无消息，无从抽取")

        project = None
        if self._req is not None and ctx.workspace.tapd_project_id:
            try:
                project = await self._req.project_config(
                    str(ctx.workspace.tapd_project_id)
                )
            except Exception as exc:  # noqa: BLE001
                # 取不到项目配置不致命：它只影响 tapd_category 的取值范围。
                # 但要记下来——分类留空会让需求落到默认分类里。
                logger.warning("project_config 获取失败，分类将留空：%r", exc)

        ctx.draft = await self._structuring.extract(
            workspace_id=WorkspaceId(str(ctx.workspace.workspace_id)),
            chat_id=ctx.context.chat_id if ctx.context else "",
            topic_id=ctx.context.topic_id if ctx.context else None,
            messages=messages,
            project=project,
            trace_id=ctx.trace_id,
        )
        if not ctx.draft.ok:
            # 节点没跑成 ≠ 字段不够。见模块 docstring 第二节。
            return StepOutcome.fail(f"结构化未完成：{ctx.draft.reason}")
        return StepOutcome(metrics={"draft.confidence": ctx.draft.confidence})

    async def _gate(self, ctx: RequirementCreateContext) -> StepOutcome:
        """澄清闸门。不够就停，**在任何外部写之前**。"""
        if ctx.draft.ready_for_creation:
            return StepOutcome()
        ctx.clarifications = tuple(ctx.draft.clarifications)
        missing = "、".join(ctx.draft.missing_fields) or "置信度不足"
        return StepOutcome.fail(
            f"字段不足待澄清：{missing}",
            metrics={
                "clarifications": [q.question for q in ctx.clarifications],
                "missing_fields": list(ctx.draft.missing_fields),
            },
        )

    async def _create_tapd(self, ctx: RequirementCreateContext) -> StepOutcome:
        """建 TAPD 需求。

        ``max_attempts=1`` 在命令登记表里已经钉死（TAPD 无幂等键），重试等于第二条
        需求。判重责任在 ``Idempotency.guard``，不在这里。
        """
        if self._req is None:
            return StepOutcome.fail("需求端口不可用，无法建卡")

        draft = _to_port_draft(ctx.draft)
        key = IdemKey(f"{ctx.idem_key or 'req'}:tapd")

        async def action() -> Any:
            return await self._req.create(draft, key)

        if self._idem is None:
            # 没有 PG 就没有幂等台账。**照旧执行**，但把"这次没有防重保护"写进
            # 结果——不执行等于因为观测缺失而放弃能力；静默执行则会在重试时建出
            # 第二条需求而无人知晓。
            created = await action()
            ctx.created = created
            ctx.steps_done.append("tapd")
            return StepOutcome(
                note="幂等台账不可用（PG 未装配）：本次建卡无防重保护，重试会产生重复需求",
                metrics={"tapd_id": _tapd_id(created)},
            )

        outcome = await self._idem.guard(
            key,
            "tapd.requirement.create",
            action,
            to_record=lambda r: {"tapd_id": getattr(r, "tapd_id", None)},
        )
        ctx.created = outcome.value
        ctx.steps_done.append("tapd")
        return StepOutcome(
            note="重放已有需求（幂等命中）" if outcome.replayed else "",
            metrics={"tapd_id": _tapd_id(outcome.value), "replayed": outcome.replayed},
        )

    async def _create_doc(self, ctx: RequirementCreateContext) -> StepOutcome:
        """建飞书文档。**非关键步骤**——失败留补偿任务，不撤销 TAPD。"""
        if self._docs is None:
            return StepOutcome.skip("文档端口未装配，跳过建文档（需求已建）")
        # 建文档走 docs +create，而 DocumentPort 只声明了 fetch/batch_meta/
        # list_tree/update 四个方法（doc2 §5.1）。新增 create 要动 Protocol，
        # 那是接口变更，不在本次范围。记为已知缺口，而不是偷偷用 update 去拼。
        return StepOutcome.skip(
            "DocumentPort 未声明 create（doc2 §5.1 四方法），"
            "文档需人工创建后回填 doc_ref"
        )

    async def _persist(self, ctx: RequirementCreateContext) -> StepOutcome:
        """落库。只写引用与标识，不写原文（§8）。"""
        if self._repo is None:
            return StepOutcome.skip("PG 未装配，需求台账未落库")
        tapd_id = _tapd_id(ctx.created)
        if not tapd_id:
            return StepOutcome.skip("无 tapd_id，跳过落库")
        return StepOutcome(
            state_patch={"last_created_requirement": str(tapd_id)},
            metrics={"persisted": str(tapd_id)},
        )


def _tapd_id(value: Any) -> str | None:
    """从 ``Requirement`` 或幂等重放出来的 dict 里取 tapd_id。

    两种形态都要支持：正常路径拿到的是 ``Requirement`` 对象，幂等重放路径拿到的是
    ``result`` jsonb 还原的 dict（``guard`` 未传 ``from_record`` 时就是 dict）。
    """
    if value is None:
        return None
    if isinstance(value, Mapping):
        got = value.get("tapd_id")
    else:
        got = getattr(value, "tapd_id", None)
    return str(got) if got else None


def _to_port_draft(draft: Any) -> PortDraft:
    """``intelligence.RequirementDraft`` → ``ports.RequirementDraft``。

    两个同名类型是刻意的：``intelligence`` 那个带 ``ready_for_creation`` /
    ``clarifications`` 这些**决定要不要写**的字段，``ports`` 那个只带**写什么**。
    转换发生在闸门之后，于是端口层拿不到"还没准备好"的草稿——它连表达这件事的
    字段都没有。
    """
    return PortDraft(
        title=draft.title,
        background=draft.background,
        goal=draft.goal,
        scope_in=tuple(draft.scope_in),
        scope_out=tuple(draft.scope_out),
        acceptance_criteria=tuple(draft.acceptance_criteria),
        owners=tuple(draft.owners),
        workspace_id=draft.workspace_id,
        chat_id=draft.chat_id,
        topic_id=draft.topic_id,
        tapd_category=draft.tapd_category,
        affected_clients=tuple(draft.affected_clients),
        source_message_ids=tuple(draft.source_message_ids),
        confidence=float(draft.confidence),
        missing_fields=tuple(draft.missing_fields),
    )


__all__ = ["RequirementCreateContext", "RequirementCreateWorkflow"]
