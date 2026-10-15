"""Agent 变更订阅工具。群取自当前任务绑定，模型不能替别的群订阅。"""

from __future__ import annotations

import json
from typing import Any

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from app.capability.agents.wecom_tools import current_task_context
from app.capability.repository import ALL_TARGETS, SUBSCRIPTION_SOURCES

_repository: Any | None = None


def bind_dependencies(repository: Any | None) -> None:
    global _repository
    _repository = repository


def _response(value: dict[str, Any]) -> ToolResponse:
    return ToolResponse(
        content=[
            TextBlock(type="text", text=json.dumps(value, ensure_ascii=False, default=str))
        ]
    )


def _prepare(source: str) -> tuple[str | None, dict[str, Any] | None]:
    if _repository is None:
        return None, {"ok": False, "error": "订阅存储不可用（PG 未配置）"}
    if source not in SUBSCRIPTION_SOURCES:
        return None, {"ok": False, "error": f"source 只能是 {'/'.join(SUBSCRIPTION_SOURCES)}"}
    chat_id = current_task_context().get("chat_id")
    if not chat_id:
        return None, {"ok": False, "error": "当前会话无群归属，无法订阅"}
    return str(chat_id), None


def _target(target: str | None) -> str:
    return (target or "").strip() or ALL_TARGETS


async def wetag_subscribe_changes(source: str, target: str = ALL_TARGETS) -> ToolResponse:
    """订阅变更，有变化时推送到当前群。

    Args:
        source (`str`): ``requirement``（TAPD 需求字段变更）或 ``document``（飞书文档内容变更）。
        target (`str`): TAPD 需求 ID 或飞书文档 token；``*`` 表示该类全部。
    """
    chat_id, err = _prepare(source)
    if err:
        return _response(err)
    target = _target(target)
    ctx = current_task_context()
    created = await _repository.subscribe(
        chat_id=chat_id, source=source, target=target, created_by=ctx.get("actor_user_id")
    )
    return _response({"ok": True, "created": created, "source": source, "target": target})


async def wetag_unsubscribe_changes(source: str, target: str = ALL_TARGETS) -> ToolResponse:
    """取消当前群的变更订阅。

    Args:
        source (`str`): ``requirement`` 或 ``document``。
        target (`str`): 订阅时的目标；``*`` 表示该类全部。
    """
    chat_id, err = _prepare(source)
    if err:
        return _response(err)
    removed = await _repository.unsubscribe(
        chat_id=chat_id, source=source, target=_target(target)
    )
    return _response({"ok": True, "removed": removed})


async def wetag_list_subscriptions() -> ToolResponse:
    """列出当前群的变更订阅。"""
    chat_id, err = _prepare(SUBSCRIPTION_SOURCES[0])
    if err:
        return _response(err)
    rows = await _repository.list_subscriptions(chat_id)
    return _response(
        {"ok": True, "subscriptions": [{"source": r["source"], "target": r["target"]} for r in rows]}
    )


AGENT_SUBSCRIPTION_TOOLS = (
    wetag_subscribe_changes,
    wetag_unsubscribe_changes,
    wetag_list_subscriptions,
)

__all__ = [
    "AGENT_SUBSCRIPTION_TOOLS",
    "bind_dependencies",
    "wetag_list_subscriptions",
    "wetag_subscribe_changes",
    "wetag_unsubscribe_changes",
]
