"""Agent 企微待办工具。身份与群从当前任务绑定，模型不能伪造。"""

from __future__ import annotations

import contextvars
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from app.contracts.ids import IdemKey
from app.contracts.ports import TodoCreate

_todos: Any | None = None
_todo_refs: Any | None = None
_task_context: contextvars.ContextVar[dict[str, str | None] | None] = (
    contextvars.ContextVar("wecom_tools_task_ctx", default=None)
)


def bind_dependencies(todos: Any | None, todo_refs: Any | None = None) -> None:
    global _todos, _todo_refs
    _todos = todos
    _todo_refs = todo_refs


def bind_task_context(
    *,
    task_id: str,
    actor_user_id: str | None,
    chat_id: str | None,
    topic_id: str | None,
) -> contextvars.Token:
    return _task_context.set(
        {
            "task_id": task_id,
            "actor_user_id": actor_user_id,
            "chat_id": chat_id,
            "topic_id": topic_id,
        }
    )


def unbind_task_context(token: contextvars.Token) -> None:
    _task_context.reset(token)


def current_task_context() -> dict[str, str | None]:
    return dict(_task_context.get() or {})


def _response(value: dict[str, Any]) -> ToolResponse:
    return ToolResponse(
        content=[
            TextBlock(
                type="text",
                text=json.dumps(value, ensure_ascii=False, default=str),
            )
        ]
    )


async def _resolve_assignees(
    names: list[str], actor: str
) -> tuple[list[str], list[str], list[str]]:
    assignee_ids: list[str] = []
    resolved_names: list[str] = []
    unresolved: list[str] = []
    for name in names:
        clean = str(name).strip()
        if not clean:
            continue
        try:
            ref = await _todos.resolve_user(clean)
            assignee_ids.append(str(ref.user_id))
            resolved_names.append(str(getattr(ref, "display_name", "") or clean))
        except Exception as exc:  # noqa: BLE001
            unresolved.append(f"{clean}（{getattr(exc, 'reason', exc)}）")
    if not assignee_ids and actor:
        assignee_ids = [actor]
    return assignee_ids, resolved_names, unresolved


async def wecom_todo_create(
    title: str,
    description: str = "",
    assignee_names: list[str] | None = None,
    due_days: int = 2,
) -> ToolResponse:
    """创建企微原生待办。默认派给当前发起人；指定姓名时先精确解析，禁止改用 TAPD。"""
    if _todos is None:
        return _response({"ok": False, "error": "企微待办能力未装配"})
    ctx = _task_context.get() or {}
    actor = str(ctx.get("actor_user_id") or "")
    assignee_ids, resolved_names, unresolved = await _resolve_assignees(
        assignee_names or [], actor
    )
    if not assignee_ids:
        return _response(
            {"ok": False, "error": "无法确定待办负责人", "unresolved": unresolved}
        )

    days = max(0, min(int(due_days), 365))
    try:
        ref = await _todos.create(
            TodoCreate(
                title=title.strip()[:128],
                assignee_ids=tuple(assignee_ids),
                chat_id=str(ctx.get("chat_id") or "") or None,
                detail=description[:4000] or None,
                due_at=datetime.now(UTC) + timedelta(days=days),
            ),
            IdemKey(f"agent_todo:{ctx.get('task_id') or 'unknown'}"),
        )
    except Exception as exc:  # noqa: BLE001
        return _response({"ok": False, "error": getattr(exc, "reason", str(exc))})

    if _todo_refs is not None and ctx.get("chat_id"):
        await _todo_refs.register(
            str(ref.todo_id),
            str(ctx["chat_id"]),
            topic_id=str(ctx.get("topic_id") or "") or None,
            source="agent",
        )
    return _response(
        {
            "ok": True,
            "todo_id": str(ref.todo_id),
            "assignees": resolved_names or assignee_ids,
            "unresolved": unresolved,
        }
    )


AGENT_WECOM_TOOLS = (wecom_todo_create,)

__all__ = [
    "AGENT_WECOM_TOOLS",
    "bind_dependencies",
    "bind_task_context",
    "unbind_task_context",
    "wecom_todo_create",
]
