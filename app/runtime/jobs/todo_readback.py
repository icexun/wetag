"""确定性：逾期待办回读跟进（US11）。

本地只存归属（todo_ref），内容/参与人/截止/完成状态一律经 TodoPort.get_batch
实时回读（US11-1）。逾期待办按周期提醒责任人（US11-2），计数达阈值升级抄送
需求负责人（US11-3）；已完成归档（US11-4）、不可见失效（US11-5）；能力整体
不可用时群内卡片明示「暂不可用」，不以本地数据伪装已交付（US11-6）；同一跟进
周期（以任务轮次 scheduled_at 标识）对同一待办只提醒一次（US11-7）。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.contracts.envelope import Envelope, MsgType, Priority
from app.contracts.errors import PortUnavailable, error_digest
from app.contracts.job_result import JobResult
from app.contracts.ports import TodoSnapshot
from app.contracts.task import TaskKind
from app.platform.degradation import apply_degrade, render_capability_unavailable_card
from app.runtime.jobs.base import Job, JobContext

TODO_ESCALATE_THRESHOLD = 3  # 缺省升级阈值；业务线经 TODO_ESCALATE_THRESHOLD 配置覆盖

_DONE_STATUSES = {"completed", "done"}
_GONE_STATUSES = {"deleted", "invisible", "gone"}

DEGRADE_NAME = "todo_capability"


def should_remind(ref: Any, cycle_started_at: datetime) -> bool:
    """本周期是否尚未提醒过该待办（US11-7）。"""
    return ref.last_remind_at is None or ref.last_remind_at < cycle_started_at


def _remind_envelope(ref: Any, snap: TodoSnapshot, *, task_id: Any = None) -> Envelope:
    """群内普通提醒（US11-2）。

    文案不带 <@userid>：aibot markdown 不解析提及语法，开放 ID 原样
    渲染只是乱码。assignee_ids 仍留在 payload 供下游使用。
    """
    assignees = list(snap.assignee_ids)
    subject = snap.content or ref.wecom_todo_id
    return Envelope(
        chat_id=ref.chat_id,
        topic_id=ref.topic_id,
        msg_type=MsgType.MARKDOWN,
        priority=Priority.NORMAL,
        payload={
            "text": f"待办已逾期，请跟进：{subject}",
            "mentions": assignees,
            "wecom_todo_id": ref.wecom_todo_id,
            "remind_count": ref.remind_count + 1,
        },
        task_id=task_id,
    )


def escalate(ref: Any, snap: TodoSnapshot, *, task_id: Any = None) -> Envelope:
    """升级提醒：达阈值后抄送需求负责人（US11-3）。"""
    assignees = list(snap.assignee_ids)
    subject = snap.content or ref.wecom_todo_id
    return Envelope(
        chat_id=ref.chat_id,
        topic_id=ref.topic_id,
        msg_type=MsgType.TEMPLATE_CARD,
        priority=Priority.NORMAL,
        payload={
            "card_type": "todo_escalation",
            "wecom_todo_id": ref.wecom_todo_id,
            "text": f"待办多次提醒未闭环，已升级：{subject}",
            "mentions": assignees,
            "cc": [snap.owner_id] if snap.owner_id else [],
            "remind_count": ref.remind_count + 1,
        },
        task_id=task_id,
    )


class TodoReadbackJob(Job):
    """待办回读跟进：只声明待发消息与台账迁移，不直接发送（US2-2）。"""

    name = "todo_readback"
    kind = TaskKind.DETERMINISTIC
    timeout_s = 120.0
    default_cron = "*/15 * * * *"

    def __init__(
        self,
        todo_port: Any = None,
        todo_repo: Any = None,
        escalate_threshold: int | None = None,
    ) -> None:
        self._todo = todo_port
        self._repo = todo_repo
        self._threshold = escalate_threshold or TODO_ESCALATE_THRESHOLD

    async def run(self, ctx: JobContext) -> JobResult:
        extras = ctx.extras or {}
        repo = self._repo or extras.get("todo_ref_repo")
        if repo is None:
            raise PortUnavailable("todo_ref 仓储未接线：待办回读跟进未完成")
        refs = await repo.list_active()
        if not refs:
            return JobResult(metrics={"checked": 0})

        port = self._todo or extras.get("todo")
        details: dict[str, TodoSnapshot] = {}
        if port is not None:
            try:
                batch = await port.get_batch([r.wecom_todo_id for r in refs])
                details = {str(snap.todo_id): snap for snap in batch}
            except PortUnavailable as exc:  # 能力整体不可用 → 显式降级（US11-6）
                return await self._capability_unavailable(ctx, refs, error_digest(exc))
        else:
            return await self._capability_unavailable(ctx, refs, "TodoPort 未绑定")

        cycle = ctx.task.scheduled_at or datetime.now(UTC)  # 跟进周期以任务轮次标识
        now = datetime.now(UTC)
        envelopes: list[Envelope] = []
        reminded = escalated = archived = invalidated = 0
        for ref in refs:
            snap = details.get(ref.wecom_todo_id)
            status = (snap.status if snap is not None else "").lower()
            if snap is None or status in _GONE_STATUSES:
                # 原生侧不可见/已删除：失效并停止提醒，不反复报错（US11-5）
                if await repo.invalidate(ref.wecom_todo_id):
                    invalidated += 1
                continue
            if snap.finished or status in _DONE_STATUSES:
                # 已完成：归档保留可查，不再提醒（US11-4）
                if await repo.archive(ref.wecom_todo_id):
                    archived += 1
                continue
            due = snap.due_at
            if due is not None and due.tzinfo is None:
                due = due.replace(tzinfo=UTC)
            if due is None or due > now:
                continue  # 未逾期
            if not should_remind(ref, cycle):
                continue  # 本周期已提醒（US11-7）
            if not await repo.record_remind(ref.wecom_todo_id, cycle):
                continue  # 并发/重放的同周期执行已提醒过
            if ref.remind_count + 1 >= self._threshold:
                envelopes.append(escalate(ref, snap, task_id=ctx.task.id))
                escalated += 1
            else:
                envelopes.append(_remind_envelope(ref, snap, task_id=ctx.task.id))
                reminded += 1
        return JobResult(
            envelopes=envelopes,
            metrics={
                "checked": len(refs),
                "reminded": reminded,
                "escalated": escalated,
                "archived": archived,
                "invalidated": invalidated,
            },
        )

    async def _capability_unavailable(
        self, ctx: JobContext, refs: list[Any], reason: str
    ) -> JobResult:
        """待办能力整体不可用：相关群卡片明示「暂不可用」+ 降级状态可见（US11-6）。

        不以本地归属数据伪装成已交付：本轮不做任何提醒/归档判定。
        """
        envelopes = [
            Envelope(
                chat_id=chat_id,
                msg_type=MsgType.TEMPLATE_CARD,
                priority=Priority.NORMAL,
                payload=render_capability_unavailable_card("企微待办", reason),
                task_id=ctx.task.id,
            )
            for chat_id in sorted({r.chat_id for r in refs})
        ]
        if ctx.state is not None:
            await apply_degrade(
                ctx.state,
                DEGRADE_NAME,
                level="disabled",
                reason=reason,
                workspace_id=ctx.workspace_id,
            )
        return JobResult(
            envelopes=envelopes,
            metrics={"checked": 0, "degraded": True, "reason": reason},
        )
