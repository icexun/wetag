"""Capability 的唯一入口：Task/Job → 处理器分派（§4.3 / 裁决 #20）。

Capability 不自建 Scheduler / Queue / Worker。Runtime 拿到一条 ``AgenticTask`` 或
``DeterministicJob`` 就交给这里，拿回一个 ``JobResult``；入队、推送、重试都在
Runtime 那侧。所以这个文件里没有循环、没有 sleep、没有重试——它只是一张分派表
加三条入口纪律。

三条纪律，每条都对应一种"看起来正常返回、实则出事了"的失败：

**一、未知类型不是空结果。**
分派表里没有的 kind 一律 ``completed=False`` 带 reason。写成"没有处理器就返回一个
空的成功"会让新增 WorkflowKind 时忘记登记的那次变成静默丢任务：Runtime 看到
``completed=True``、没有 envelope，于是把 Task 标成成功并删掉。没有人会收到任何
提示，而那条需求变更就此消失。

**二、能力没装配不是"没查到"。**
端口不可用时必须以 ``completed=False`` + 原因收尾。不可用与查无结果不能共用
一个返回值，否则渲染层会把前者说成"一切正常"。

**三、``IsolationViolation`` 不转成 ``JobResult``。**
其余异常都收敛成"未完成 + 原因"，因为它们是可以重试、可以降级、可以告诉用户
"这次没跑成"的事。跨业务线归属不符不是这种事：它是安全事件，要告警且**不得重试**
（§4.2）。把它塞进 ``JobResult.incomplete`` 会让 Runtime 按普通失败退避重试，
于是一次配置错误变成一串越权尝试，而监控上只看到"某任务重试了几次"。所以它**穿透**
这里向上抛。
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from app.container import CapabilityContainer
from app.contracts.errors import IsolationViolation, PortUnavailable
from app.contracts.job_result import JobResult
from app.contracts.space_context import SpaceContext
from app.contracts.task import AgenticTask, DeterministicJob, JobKind, WorkflowKind

logger = logging.getLogger(__name__)

Handler = Callable[["CapabilityRouter", Any], Awaitable[JobResult]]


class CapabilityRouter:
    """Task/Job → 处理器。一个进程一个业务线，所以容器是构造期绑定的。"""

    def __init__(self, container: CapabilityContainer) -> None:
        self._c = container

    # -- 入口 --------------------------------------------------------------
    async def handle_task(self, task: AgenticTask) -> JobResult:
        """处理一条 Agentic Task。"""
        self._assert_ownership(task.context, f"task.{task.kind.value}")
        handler = _TASK_HANDLERS.get(task.kind)
        if handler is None:
            # 纪律一。注意 reason 里带上 kind——排查时要知道是哪个类型漏登记了。
            return JobResult.incomplete(
                f"未登记的 WorkflowKind：{task.kind.value}（Capability 无对应处理器）"
            )
        return await self._run(handler, task, f"task.{task.kind.value}")

    async def handle_job(self, job: DeterministicJob) -> JobResult:
        """处理一条确定性 Job（零 token）。

        今日登记两条：``INDEX_REFRESH``（索引刷新与文档变更通知）与
        ``LOOP_DIGEST``（Loop-3 回复规约）。其余四条（doc_poll / tapd_sync /
        repo_sync / todo_readback）的"取数 + 比对"部分在 Runtime 的 jobs/ 下，
        它们调用 Capability 的端口而不是反过来。这里留着分派表是为了让缺失以
        "未登记"的形态出现，而不是让 Runtime 拿到一个空成功。
        """
        self._assert_ownership(job.context, f"job.{job.kind.value}")
        handler = _JOB_HANDLERS.get(job.kind)
        if handler is None:
            return JobResult.incomplete(
                f"未登记的 JobKind：{job.kind.value}（Capability 无对应处理器）"
            )
        return await self._run(handler, job, f"job.{job.kind.value}")

    async def _run(self, handler: Handler, unit: Any, where: str) -> JobResult:
        """统一的异常收敛。``IsolationViolation`` 例外——见模块 docstring 纪律三。"""
        try:
            return await handler(self, unit)
        except IsolationViolation:
            raise
        except PortUnavailable as exc:
            # 端口不可用是**可预期**的今日状态（R1/R2/R4），所以它不是 exception 级
            # 日志，但必须带着端口名进 reason，否则卡片上只会写"执行失败"。
            logger.warning("%s 端口不可用：%s", where, exc)
            return JobResult.incomplete(f"{exc.port} 不可用：{exc.reason}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("%s 未预期异常", where)
            return JobResult.incomplete(f"{where}: {type(exc).__name__}: {exc}")

    def _assert_ownership(self, context: SpaceContext, where: str) -> None:
        """§4.2 的入口校验。

        Capability 是链路末端，Space 已经校验过一次。这里再校验一次不是冗余：
        Task 在 Runtime 里落过库、出过队，中间任何一处串了 workspace 都会在这里
        被截住，而截住的代价只是一次字符串比较。
        """
        context.assert_workspace(str(self._c.workspace.workspace_id), where)

    # -- 处理器 ------------------------------------------------------------
    async def _not_delivered(self, unit: Any, feature: str, gap: str) -> JobResult:
        """外部依赖不可用的能力：显式处理器，不与"未登记"的实现缺口混为一谈。"""
        return JobResult.incomplete(f"{feature} 今日不可用（{gap}）")

    async def _requirement_create(self, task: AgenticTask) -> JobResult:
        if self._c.requirements is None:
            return JobResult.incomplete(
                f"需求端口不可用：{self._c.status('requirements').reason}"
            )
        from app.capability.workflows.requirement_create import (
            RequirementCreateContext,
            RequirementCreateWorkflow,
        )

        wf = RequirementCreateWorkflow(
            self._c.requirements,
            structuring=self._c.structuring,
            repository=self._c.repository,
            idempotency=self._c.idempotency,
        )
        ctx = RequirementCreateContext(
            payload=dict(task.payload),
            workspace=self._c.workspace,
            context=task.context,
            idem_key=str(task.idem_key) if task.idem_key else None,
            trace_id=task.trace_id,
        )
        return await wf.execute(ctx, context=task.context)

    async def _doc_update(self, task: AgenticTask) -> JobResult:
        if self._c.documents is None:
            return JobResult.incomplete(
                f"文档端口不可用：{self._c.status('documents').reason}"
            )
        from app.capability.workflows.doc_update import DocUpdateContext, DocUpdateWorkflow

        wf = DocUpdateWorkflow(self._c.documents)
        ctx = DocUpdateContext(
            payload=dict(task.payload),
            workspace=self._c.workspace,
            context=task.context,
            idem_key=str(task.idem_key) if task.idem_key else None,
            trace_id=task.trace_id,
        )
        return await wf.execute(ctx, context=task.context)

    async def _index_refresh(self, job: DeterministicJob) -> JobResult:
        """文档快照刷新 + 变更通知。零 token：只取数、比对、落库。"""
        doc_ids = tuple(job.payload.get("document_ids") or ())
        if self._c.repository is None:
            return JobResult.incomplete(f"PG 不可用：{self._c.status('db').reason}")
        if self._c.documents is None:
            # 「本轮没刷成」必须落到快照上，否则它看起来是干净的。
            await self._mark_unverified(doc_ids)
            return JobResult.incomplete(
                f"文档端口不可用，无法刷新索引：{self._c.status('documents').reason}"
            )
        from app.capability.workflows.change_notify import Notifier
        from app.capability.workflows.doc_update import refresh_index

        return await refresh_index(
            repository=self._c.repository,
            documents=self._c.documents,
            document_ids=doc_ids,
            # 不传 embedder 不会擦掉已有向量（upsert 里 COALESCE），只是新文档没向量。
            embedder=self._c.embedder,
            notifier=Notifier(self._c.repository),
        )

    async def _loop_digest(self, job: DeterministicJob) -> JobResult:
        """Loop-3：👎 踩因达阈值自动提炼回复规约，写 biz 层规则并留痕。

        没有任何新信息时不推群——空转的周报就是噪音。
        """
        repo = self._c.repository
        if repo is None:
            return JobResult.incomplete(f"PG 不可用：{self._c.status('db').reason}")
        if self._c.rules_writer is None:
            return JobResult(metrics={"note": "规则写入端未装配，跳过 Loop-3"})

        from app.analytics.loops.loop3_push import render_push_notice, run_push_loop

        report = await run_push_loop(
            repo, self._c.rules_writer, self._c.agent, window_days=7, trace_id=job.trace_id
        )
        metrics = {
            "push_down_votes": report.down_votes,
            "push_rules_applied": [a.key for a in report.applied],
        }
        if not report.worth_pushing:
            return JobResult(metrics=metrics)

        chat_id = job.context.chat_id if job.context.chat_id != "system" else None
        if chat_id is None:
            provider = self._c.alert_chat_provider
            chat_id = await provider() if provider is not None else None
        if chat_id is None:
            # 规则已生效并留痕；只是没有可推送的群，不猜群。
            logger.warning("loop_digest 无可推送群，结果仅落库：%s", metrics)
            return JobResult.incomplete("无活跃群可推送 Loop 周报", metrics=metrics)

        from app.contracts.envelope import Envelope, MsgType

        return JobResult(
            envelopes=[
                Envelope(
                    chat_id=chat_id,
                    msg_type=MsgType.MARKDOWN,
                    payload={"text": render_push_notice(report)},
                )
            ],
            metrics=metrics,
        )

    async def _mark_unverified(self, doc_ids: Any) -> None:
        """端口不可用时把请求到的文档标为「未核实」。标脏失败不阻断主结论。"""
        repo = self._c.repository
        if repo is None or not doc_ids:
            return
        from app.contracts.ids import DocumentId

        try:
            async with repo.db.tx() as conn:
                for did in doc_ids:
                    await repo.mark_stale_unverified(
                        conn,
                        document_id=DocumentId(str(did)),
                        reason="refresh_skipped:documents_port_down",
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("未核实标脏失败（%d 篇）：%r", len(doc_ids), exc)

    async def _requirement_update(self, task: AgenticTask) -> JobResult:
        return await self._not_delivered(
            task, "需求回写（TAPD 写操作）", "需求端口未提供结构化写入，请由 Agent 调 TAPD 工具"
        )


# 分派表放在类外、模块级：它是一份可以一眼扫完的登记表，要和 WorkflowKind 枚举逐条对齐。
_TASK_HANDLERS: dict[WorkflowKind, Handler] = {
    WorkflowKind.REQUIREMENT_CREATE: CapabilityRouter._requirement_create,
    WorkflowKind.REQUIREMENT_UPDATE: CapabilityRouter._requirement_update,
    WorkflowKind.DOC_UPDATE: CapabilityRouter._doc_update,
    # QUERY_ANSWER 不在此表：它是 Control 的记忆召回 + 问答，不属于 Capability。
}

_JOB_HANDLERS: dict[JobKind, Handler] = {
    JobKind.INDEX_REFRESH: CapabilityRouter._index_refresh,
    JobKind.LOOP_DIGEST: CapabilityRouter._loop_digest,
}


__all__ = ["CapabilityRouter"]
