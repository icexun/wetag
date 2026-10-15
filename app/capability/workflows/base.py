"""Workflow 基座（doc2 §6 引言 / §9.4-3）。

分工是硬的：**凡是「顺序固定、失败需补偿、涉及外部写」的链路一律用 Workflow
固化，Agent 只在需要语义判断的单个节点被调用**（doc2 §5 正文）。不交给
ReActAgent 自由规划，因为 ReAct 的执行顺序不可复现——同一份输入，这次是
"读旧版本 → diff → 检测 → 更新"，下次可能先更新再检测。对只读链路那只是浪费，
对带外部写的链路那是把"先校验再落盘"这条保证直接取消掉。

本模块提供三件事，每件都对应一个已经踩过的坑：

1. ``Step`` / ``Workflow.execute()``——顺序由声明决定，不由模型决定。
2. **短事务纪律的结构化保证**。``execute()`` 自己永不开事务（§9.4-3）。每个 step
   要用库就自己开一个短的。``Step.opens_tx`` 与 ``Step.calls_external`` 两个声明
   位同时为真会在**装配期**就报错——不是运行期，因为"事务里套了个 LLM 调用"这种
   代码往往在压测时才暴露，而那时它已经上线了。
3. ``StepOutcome`` 的失败传播。任一 step 未完成，整条 Workflow 的 ``JobResult``
   就是 ``completed=False`` 带 reason，**绝不会退化成一个空的成功结果**——空的
   成功结果会被渲染层说成"没有发现问题"。

补偿（``compensate``）是 best-effort 且**永远不抛异常**：补偿本身失败不该覆盖掉
原始失败原因，否则日志里看到的是"补偿时 XX 异常"，真正的根因丢了。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Generic, Sequence, TypeVar

from app.contracts.envelope import Envelope
from app.contracts.job_result import JobResult
from app.contracts.space_context import SpaceContext
from app.contracts.task import AgenticTask, spec_from_agentic

logger = logging.getLogger(__name__)

C = TypeVar("C")  # 链路上下文类型


@dataclass(slots=True)
class StepOutcome:
    """一个 step 的结果。

    ``completed=False`` 会终止后续 step 并向上传播 reason。``skipped`` 是
    "条件不满足所以没跑"，与失败区分——前者不阻塞链路。
    """

    completed: bool = True
    reason: str | None = None
    skipped: bool = False
    note: str = ""
    envelopes: tuple[Envelope, ...] = ()
    derived_tasks: tuple[AgenticTask, ...] = ()
    state_patch: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def fail(cls, reason: str, **kw: Any) -> "StepOutcome":
        return cls(completed=False, reason=reason, **kw)

    @classmethod
    def skip(cls, note: str = "") -> "StepOutcome":
        return cls(completed=True, skipped=True, note=note)


@dataclass(frozen=True, slots=True)
class Step(Generic[C]):
    """一个固定顺序的步骤。

    :param opens_tx: 本步骤会开数据库事务。
    :param calls_external: 本步骤会 await LLM / CLI / HTTP。
    :param compensate: 后续步骤失败时的补偿动作（逆序执行，best-effort）。
    :param critical: 失败是否终止链路。``False`` 用于"锦上添花"的步骤，
        例如附带文档——它失败不该让已经建好的需求丢掉。
    """

    name: str
    run: Callable[[C], Awaitable[StepOutcome]]
    opens_tx: bool = False
    calls_external: bool = False
    compensate: Callable[[C], Awaitable[None]] | None = None
    critical: bool = True

    def __post_init__(self) -> None:
        if self.opens_tx and self.calls_external:
            # 装配期拒绝。见模块 docstring 第 2 点。
            raise ValueError(
                f"step {self.name!r} 同时声明 opens_tx 与 calls_external："
                "事务内 await 外部调用会在对端变慢时耗干连接池，"
                "把一次外部抖动放大成全局故障。请拆成两个 step。"
            )


class Workflow(Generic[C]):
    """顺序固定的编排基座。

    子类只需在 ``steps()`` 里声明步骤。``execute()`` 不可被覆盖的部分是：
    不开事务、顺序执行、失败即停并补偿、结果收敛成 ``JobResult``。
    """

    name: str = "workflow"

    def steps(self, ctx: C) -> Sequence[Step[C]]:  # pragma: no cover - 抽象
        raise NotImplementedError

    async def execute(self, ctx: C, *, context: SpaceContext | None = None) -> JobResult:
        """跑完整条链路。

        **本方法永不开事务**（§9.4-3）。它会 await 外部调用，如果它持有一个事务，
        那么整条链路期间连接都被占着——这正是 ``Step.__post_init__`` 在单步层面
        禁止的事，在链路层面同样禁止。
        """
        started = time.monotonic()
        done: list[Step[C]] = []
        envelopes: list[Envelope] = []
        tasks: list[AgenticTask] = []
        state: dict[str, Any] = {}
        metrics: dict[str, Any] = {}
        notes: list[str] = []
        log_fields = context.log_fields() if context is not None else {}

        for step in self.steps(ctx):
            t0 = time.monotonic()
            try:
                outcome = await step.run(ctx)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "workflow=%s step=%s 异常 %s", self.name, step.name, log_fields
                )
                await self._compensate(done, ctx)
                return JobResult.incomplete(
                    f"{step.name}: {type(exc).__name__}: {exc}",
                    envelopes=tuple(envelopes),
                    derived_tasks=[spec_from_agentic(t) for t in tasks],
                    state_patch=state,
                    metrics={**metrics, "failed_step": step.name},
                )

            elapsed = int((time.monotonic() - t0) * 1000)
            metrics[f"step.{step.name}.ms"] = elapsed
            envelopes.extend(outcome.envelopes)
            tasks.extend(outcome.derived_tasks)
            state.update(outcome.state_patch)
            metrics.update(outcome.metrics)
            if outcome.note:
                notes.append(outcome.note)

            if outcome.completed:
                if not outcome.skipped:
                    done.append(step)
                continue

            reason = outcome.reason or f"{step.name} 未完成"
            if not step.critical:
                # 非关键步骤失败：记入 notes 继续跑。它的产出（如影响分析）缺失，
                # 但已经拿到的结论仍然要送出去。
                logger.warning(
                    "workflow=%s 非关键 step=%s 失败：%s %s",
                    self.name, step.name, reason, log_fields,
                )
                notes.append(f"{step.name} 降级：{reason}")
                continue

            logger.warning(
                "workflow=%s step=%s 未完成：%s %s",
                self.name, step.name, reason, log_fields,
            )
            await self._compensate(done, ctx)
            return JobResult.incomplete(
                reason,
                envelopes=tuple(envelopes),
                derived_tasks=[spec_from_agentic(t) for t in tasks],
                state_patch=state,
                metrics={**metrics, "failed_step": step.name, "notes": notes},
            )

        metrics["workflow.ms"] = int((time.monotonic() - started) * 1000)
        if notes:
            metrics["notes"] = notes
        return JobResult(
            derived_tasks=[spec_from_agentic(t) for t in tasks],
            envelopes=tuple(envelopes),
            state_patch=state,
            completed=True,
            metrics=metrics,
        )

    # -- 内部 -------------------------------------------------------------
    async def _compensate(self, done: Sequence[Step[C]], ctx: C) -> None:
        """逆序补偿。**每个补偿都吞掉自己的异常**，见模块 docstring。"""
        for step in reversed(done):
            if step.compensate is None:
                continue
            try:
                await step.compensate(ctx)
                logger.info("workflow=%s 已补偿 step=%s", self.name, step.name)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "workflow=%s 补偿 step=%s 失败（不覆盖原始失败原因）",
                    self.name, step.name,
                )


__all__ = ["Step", "StepOutcome", "Workflow"]
