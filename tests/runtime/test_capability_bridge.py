"""Runtime → Capability 接线桥（``app/runtime/capability_bridge.py``）。

断言两件事：
1. ``register_capability`` 把 Capability 分派表里的 WorkflowKind 注册成 pool
   handler——派生任务不再以「未注册的 job」收场；
2. handler 把 Task 投影成 AgenticTask：kind 正确、payload 透传、归属上下文
   优先取 payload 里的 space_context，缺失时落系统占位且 workspace 不串。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.contracts.job_result import JobResult
from app.contracts.task import Task, TaskKind, TriggerSource, WorkflowKind
from app.runtime.capability_bridge import (
    make_task_handler,
    register_capability,
)


class FakeRouter:
    def __init__(self) -> None:
        self.seen: list[Any] = []

    async def handle_task(self, task: Any) -> JobResult:
        self.seen.append(task)
        return JobResult(metrics={"ok": 1})


def _task(job_name: str, payload: dict | None = None) -> Task:
    return Task(
        id=uuid4(),
        workspace_id="alpha",
        job_name=job_name,
        kind=TaskKind.AGENTIC,
        trigger_source=TriggerSource.DERIVED,
        payload=payload or {},
        scheduled_at=datetime.now(UTC),
    )


async def test_task_handler_projects_task_to_agentic() -> None:
    router = FakeRouter()
    handler = make_task_handler(
        router, WorkflowKind.DOC_UPDATE, workspace_id="alpha", bot_id="bot-1"
    )
    ctx = type(
        "Ctx",
        (),
        {"task": _task("doc_update", {"document_id": "doc1"})},
    )()

    result = await handler(ctx)

    assert result.metrics["ok"] == 1
    agentic = router.seen[0]
    assert agentic.kind is WorkflowKind.DOC_UPDATE
    assert agentic.payload["document_id"] == "doc1"
    # 派生任务无会话归属：系统占位 context，workspace 必须是本进程业务线
    assert agentic.context.workspace_id == "alpha"
    assert agentic.context.chat_id == "system"
    assert agentic.context.bot_id == "bot-1"


async def test_task_handler_prefers_persisted_space_context() -> None:
    router = FakeRouter()
    handler = make_task_handler(
        router, WorkflowKind.DOC_UPDATE, workspace_id="alpha", bot_id="bot-1"
    )
    ctx = type(
        "Ctx",
        (),
        {
            "task": _task(
                "doc_update",
                {
                    "space_context": {
                        "organizationId": "org-1",
                        "workspaceId": "alpha",
                        "botId": "bot-9",
                        "chatId": "chat-1",
                        "topicId": "topic-1",
                        "actorUserId": None,
                        "effectiveConfigVersion": None,
                    }
                },
            )
        },
    )()

    await handler(ctx)

    context = router.seen[0].context
    assert context.chat_id == "chat-1"
    assert context.bot_id == "bot-9"
    assert context.topic_id == "topic-1"


def test_register_capability_registers_all_router_kinds() -> None:
    from app.capability.router import _JOB_HANDLERS, _TASK_HANDLERS

    handlers: dict[str, Any] = {}
    extras: dict[str, Any] = {}
    registered = register_capability(
        handlers, extras, object(), workspace_id="alpha", bot_id="bot-1"
    )

    # 分派表里的每个 WorkflowKind 都有 pool handler
    assert set(registered) == {k.value for k in _TASK_HANDLERS}
    assert "doc_update" in handlers
    # 每个 JobKind 都有 capability:<name> 桥
    for kind in _JOB_HANDLERS:
        assert f"capability:{kind.value}" in extras
