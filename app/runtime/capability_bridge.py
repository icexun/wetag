"""Runtime → Capability 的接线桥。

Runtime 的 WorkerPool 只认识 ``handlers[job_name]``；Capability 的 WorkflowKind
不注册进去，派生任务落库入队后会以 ``KeyError: 未注册的 job`` 收场。本模块就是
那层接线：

* 每个 Capability 已登记的 ``WorkflowKind`` → 一个 pool handler
  （``Task`` → ``AgenticTask`` → ``router.handle_task``）；
* ``extras["capability:<job>"]`` → ``DeterministicJob`` 桥
  （``CapabilityJob`` 骨架如 ``IndexRefreshJob`` 经它进 ``router.handle_job``）。

SpaceContext 的重建顺序：``payload["space_context"]`` / ``payload["context"]``
（Channel 入站时落的 to_dict）→ 系统占位 context。派生任务没有会话归属，
占位 context 的 chat_id 为 ``system``——Router 只校验 workspace 一致性（§4.2
的 bot 等式在 Channel 入口已成立，派生任务不引入新的归属事实）。
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from app.contracts.ids import (
    BotId,
    ChatId,
    IdemKey,
    OrganizationId,
    TaskId,
    TraceId,
    WorkspaceId,
)
from app.contracts.job_result import JobResult
from app.contracts.space_context import SpaceContext
from app.contracts.task import (
    AgenticTask,
    DeterministicJob,
    JobKind,
    Task,
    WorkflowKind,
)

logger = logging.getLogger(__name__)


def _context_for(task: Task, workspace_id: str, bot_id: str) -> SpaceContext:
    """从 task 重建归属上下文；缺失时构造系统占位（派生任务无会话）。"""
    raw = (task.payload or {}).get("space_context") or (task.payload or {}).get("context")
    if isinstance(raw, dict) and raw.get("workspaceId"):
        try:
            return SpaceContext.from_dict(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("space_context 反序列化失败，退回系统占位：%r", exc)
    return SpaceContext(
        organization_id=OrganizationId(workspace_id),
        workspace_id=WorkspaceId(workspace_id),
        bot_id=BotId(bot_id),
        chat_id=ChatId(task.chat_id or "system"),
        topic_id=task.topic_id,
    )


def make_task_handler(
    router: Any,
    kind: WorkflowKind,
    *,
    workspace_id: str,
    bot_id: str,
) -> Callable[[Any], Awaitable[JobResult]]:
    """``job_name == kind.value`` 的 pool handler：Task → AgenticTask → Router。"""

    async def handler(ctx: Any) -> JobResult:
        task: Task = ctx.task
        agentic = AgenticTask(
            task_id=TaskId(str(task.id)),
            kind=kind,
            context=_context_for(task, workspace_id, bot_id),
            payload=dict(task.payload or {}),
            idem_key=IdemKey(task.idem_key) if task.idem_key else None,
            trace_id=TraceId(task.trace_id) if task.trace_id else None,
            status=task.status,
            attempt=task.attempt,
            scheduled_at=task.scheduled_at,
        )
        return await router.handle_task(agentic)

    return handler


def make_job_handler(
    router: Any,
    kind: JobKind,
    *,
    workspace_id: str,
    bot_id: str,
) -> Callable[[Any], Awaitable[JobResult]]:
    """``extras["capability:<name>"]`` 桥：JobContext → DeterministicJob → Router。"""

    async def handler(ctx: Any) -> JobResult:
        job = DeterministicJob(
            kind=kind,
            context=_context_for(ctx.task, workspace_id, bot_id),
            payload=dict(ctx.task.payload or {}),
            trace_id=ctx.trace_id,
        )
        return await router.handle_job(job)

    return handler


def register_capability(
    handlers: dict[str, Any],
    extras: dict[str, Any],
    capability: Any,
    *,
    workspace_id: str,
    bot_id: str,
) -> list[str]:
    """把 Capability 分派表里的全部 WorkflowKind 注册进 pool handlers。

    返回注册成功的 job_name 列表。Capability 分派表里没有的 kind 不注册——
    让它们在 Router 侧以「未登记」形态显式失败，而不是在 Runtime 侧 KeyError。
    """
    from app.capability.router import _JOB_HANDLERS, _TASK_HANDLERS, CapabilityRouter

    router = CapabilityRouter(capability)
    registered: list[str] = []
    for kind in _TASK_HANDLERS:
        handlers[kind.value] = make_task_handler(
            router, kind, workspace_id=workspace_id, bot_id=bot_id
        )
        registered.append(kind.value)
    for kind in _JOB_HANDLERS:
        extras[f"capability:{kind.value}"] = make_job_handler(
            router, kind, workspace_id=workspace_id, bot_id=bot_id
        )
    return registered


__all__ = [
    "register_capability",
    "make_task_handler",
    "make_job_handler",
]
