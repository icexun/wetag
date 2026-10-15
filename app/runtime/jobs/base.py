"""Job 基类：只返回 JobResult，不直调 Agent、不直发消息（§4.3）。

WorkerPool 执行的是「job_name → handler」映射；handler 可以是 Job 实例
（用其 run 方法与 timeout_s/kind 元数据）或任意 async callable(ctx) -> JobResult。

外部能力一律经 require_port 获取：Port 未绑定时抛 PortUnavailable，
向调用方显式报告「未完成 + 外部能力不可用」，不返回假成功（NFR-002 / TC-089）。
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.contracts.envelope import Envelope, MsgType, Priority
from app.contracts.errors import PortUnavailable
from app.contracts.job_result import JobResult
from app.contracts.task import Task, TaskKind

if TYPE_CHECKING:
    from app.runtime.worker.lease import LeaseKeeper

logger = logging.getLogger(__name__)


@dataclass
class JobContext:
    """一次任务执行的上下文：任务本体 + 进程级依赖。"""

    task: Task
    workspace_id: str
    state: Any = None  # StateStore（runtime_state 读写面）
    trace_id: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)  # 外部 Port / capability 处理器


def require_port(ctx: JobContext, name: str) -> Any:
    """取外部能力 Port；未绑定时显式失败而非静默空实现（NFR-002 / TC-089）。"""
    port = ctx.extras.get(name) if ctx.extras else None
    if port is None:
        raise PortUnavailable(
            f"外部能力 {name!r} 未绑定：任务未完成（外部能力不可用）"
        )
    return port


class Job(ABC):
    """确定性 / Agentic Job 的统一基类。业务逻辑在 run()，产物只经 JobResult 声明。"""

    name: str = ""
    kind: TaskKind = TaskKind.DETERMINISTIC
    timeout_s: float | None = None  # None 时用 WorkerPool 的类型默认
    default_cron: str | None = None

    @abstractmethod
    async def run(self, ctx: JobContext) -> JobResult:
        """执行并声明派生任务与待发消息；不直接调 Agent、不直接发消息。"""


class CapabilityJob(Job):
    """Agentic Job 骨架：业务逻辑归 Capability，经 extras['capability:<name>'] 注入。

    未注入时抛 PortUnavailable 显式报告未完成（TC-089），不返回假成功。
    """

    kind = TaskKind.AGENTIC
    default_cron = None

    async def run(self, ctx: JobContext) -> JobResult:
        handler = require_port(ctx, f"capability:{self.name}")
        return await handler(ctx)


class LeaseReaperJob(Job):
    """确定性：回收租约过期的僵尸任务为 PENDING，attempt 保留不清零（US3-5）。"""

    name = "lease_reaper"
    kind = TaskKind.DETERMINISTIC
    timeout_s = 30.0
    default_cron = "* * * * *"

    def __init__(self, keeper: LeaseKeeper | None = None) -> None:
        self._keeper = keeper

    async def run(self, ctx: JobContext) -> JobResult:
        keeper = self._keeper or (ctx.extras or {}).get("lease_keeper")
        if keeper is None:
            raise PortUnavailable("LeaseKeeper 未接线：僵尸任务回收未完成")
        reaped = await keeper.reap_expired()
        return JobResult(metrics={"reaped": len(reaped)})


class ApprovalExpiryJob(Job):
    """确定性：扫描超时审批并收尾（US7-6）。

    对每条到期审批：置 EXPIRED（仓储层条件更新）、原确认卡片原地更新为
    「已超时取消」、对应任务以「审批超时取消」进 DEAD —— 不计 attempt、
    不进重试队列。过期后的点击因记录已非 PENDING 而不生效。
    """

    name = "approval_expiry"
    kind = TaskKind.DETERMINISTIC
    timeout_s = 30.0
    default_cron = "* * * * *"

    def __init__(
        self,
        approvals: Any = None,
        tasks: Any = None,
        outbox_submit: Any = None,
    ) -> None:
        self._approvals = approvals
        self._tasks = tasks
        self._outbox_submit = outbox_submit

    async def run(self, ctx: JobContext) -> JobResult:
        extras = ctx.extras or {}
        approvals = self._approvals or extras.get("approval_repo")
        tasks = self._tasks or extras.get("task_repo")
        outbox_submit = self._outbox_submit or extras.get("outbox_submit")
        if approvals is None or tasks is None:
            raise PortUnavailable("审批/任务仓储未接线：审批超时扫描未完成")
        expired = await approvals.expire_due()
        failed = 0
        for approval in expired:
            # 审批已 EXPIRED、不再被扫描，任务终态必须本轮必达：
            # 先 mark_dead 再更新卡片（卡片失败不影响任务终态）；
            # 单条失败隔离告警，不跳过同批其余审批的收尾。
            try:
                await tasks.mark_dead(approval.task_id, "审批超时取消")
                if approval.card_msg_id and outbox_submit is not None:
                    await outbox_submit(
                        Envelope(
                            chat_id=approval.chat_id,
                            topic_id=approval.topic_id,
                            msg_type=MsgType.TEMPLATE_CARD,
                            priority=Priority.URGENT,
                            update_of=approval.card_msg_id,
                            payload={
                                "card_type": "approval_result",
                                "approval_id": str(approval.id),
                                "status": "expired",
                                "text": "已超时取消",
                            },
                            task_id=approval.task_id,
                        )
                    )
            except Exception:
                failed += 1
                logger.exception(
                    "审批超时收尾失败：approval_id=%s task_id=%s",
                    approval.id,
                    approval.task_id,
                )
        return JobResult(metrics={"expired": len(expired), "failed": failed})
