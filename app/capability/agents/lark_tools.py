"""对话 Agent 的飞书文档工具（``lark-cli`` 实现）。

不走飞书 MCP 的理由：CLI 调用已由 ``CliExecutor`` 收口——参数数组执行、
命令白名单（删除类不登记）、凭据最小注入、审计落库。这四个函数只是把
白名单里的命令包装成 Agent 工具，安全纪律与后台管道完全一致。

工具名即审批分类依据：``*_fetch`` / ``*_search`` 命中读启发式直通，
``*_create`` / ``*_append`` / ``*_update`` 落 HIGH_WRITE 走审批闸门。
"""

from __future__ import annotations

import contextvars
import json
import os
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from app.contracts.task import TaskKind, TaskSpec, TriggerSource
from app.platform.cli.executor import CliExecutor
from app.platform.cli.registry import LARK_COMMANDS


def _bare_token(token_or_url: str) -> str:
    """parent_token 只收裸 token；误传完整链接时取路径最后一段（wiki/docx/folder 同理）。"""
    if "://" in token_or_url:
        return urlsplit(token_or_url).path.rstrip("/").rsplit("/", 1)[-1]
    return token_or_url

_executor: CliExecutor | None = None

# 派生任务通道（群聊改文档 → 立即刷新快照并通知订阅群）。由 Container.build 接线；
# 未接线时（脚本、单测）静默跳过——兜底触发仍在 doc_poll 轮询侧。
_derived_sink: Callable[[TaskSpec], Awaitable[None]] | None = None


def bind_derived_sink(sink: Callable[[TaskSpec], Awaitable[None]] | None) -> None:
    """绑定派生任务落库+入队通道。进程级单例，与 executor 同生命周期。"""
    global _derived_sink
    _derived_sink = sink


# 当前任务的归属上下文（发起人与群）。工具是进程级单例，拿不到任务对象；
# 由 AgentRunner 在 reply 前绑定（与 ApprovalGate.bind_task 同型），
# 派生任务时取出，发起人与群随派生链传递。
_task_context: contextvars.ContextVar[dict[str, str | None] | None] = (
    contextvars.ContextVar("lark_tools_task_ctx", default=None)
)


def bind_task_context(
    *, actor_user_id: str | None, chat_id: str | None, topic_id: str | None
) -> contextvars.Token:
    return _task_context.set(
        {"actor_user_id": actor_user_id, "chat_id": chat_id, "topic_id": topic_id}
    )


def unbind_task_context(token: contextvars.Token) -> None:
    _task_context.reset(token)


async def _notify_doc_changed(doc_url: str, *, summary: str) -> None:
    """写成功 → 立即派生 index_refresh：快照即时更新，订阅群即时收到变更。

    不自带版本号做幂等：refresh 本身幂等（内容未变即 UNCHANGED、零写入、不推送）。
    """
    if _derived_sink is None:
        return
    ctx = _task_context.get() or {}
    try:
        await _derived_sink(
            TaskSpec(
                job_name="index_refresh",
                kind=TaskKind.AGENTIC,
                trigger_source=TriggerSource.DERIVED,
                chat_id=ctx.get("chat_id") or None,
                topic_id=ctx.get("topic_id") or None,
                payload={
                    "document_ids": [_bare_token(doc_url)],
                    "source": "chat_write",
                    "change_summary": summary[:200],
                    "initiator_user_id": ctx.get("actor_user_id") or "",
                },
            )
        )
    except Exception:  # noqa: BLE001
        # 通知失败不拖垮写操作本身：文档已经写进去了，轮询 3 分钟后会兜底。
        import logging

        logging.getLogger(__name__).warning(
            "文档变更即时通知失败，等 doc_poll 兜底", exc_info=True
        )


def _get_executor() -> CliExecutor:
    """进程级单例（utils 的工具装配同样是一进程一次）。"""
    global _executor
    if _executor is None:
        _executor = CliExecutor(
            workspace_id=os.environ.get("WORKSPACE_ID", "default"),
            binaries={"lark": "lark-cli"},
            secrets={"LARK_CLI_TOKEN": os.environ.get("LARK_CLI_TOKEN", "")},
            specs=LARK_COMMANDS,
            timeout_s=60.0,
        )
    return _executor


def _fmt(result: Any) -> ToolResponse:
    """统一返回：成功给 data，失败给错误码与摘要（模型可读）。

    agentscope 要求工具函数返回 ``ToolResponse``，裸 str 会被拒绝。
    """
    if result.ok:
        text = json.dumps(result.data, ensure_ascii=False, default=str)
    else:
        text = json.dumps(
            {
                "ok": False,
                "error_code": str(result.error_code),
                "stderr": result.stderr_digest,
            },
            ensure_ascii=False,
        )
    return ToolResponse(content=[TextBlock(type="text", text=text)])


async def feishu_doc_fetch(doc_url: str) -> ToolResponse:
    """读取飞书文档/Wiki/电子表格内容，返回带 block id 的 Markdown（block id 供 feishu_doc_update 定位）。doc_url 用完整链接，不要手工截断 token。"""
    r = await _get_executor().run(
        "lark.docs.fetch", {"doc": doc_url, "detail": "with-ids"}
    )
    return _fmt(r)


async def feishu_doc_create(
    title: str, content_markdown: str, parent_token: str = ""
) -> ToolResponse:
    """创建飞书文档，返回文档链接与 document_id。parent_token 为文件夹 token 或 wiki 节点 token（可选，传完整链接也可以，会自动提取 token）。"""
    args: dict[str, Any] = {"title": title, "content": content_markdown}
    if parent_token:
        args["parent_token"] = _bare_token(parent_token)
    r = await _get_executor().run("lark.docs.create", args)
    if r.ok:
        # 新建文档同样需要立即入索引并检测——它是"新需求说法"的来源。
        # token 从响应里取（url 或 document_id 字段；网关可能再包一层 data），
        # 取不到就等轮询兜底。
        node = r.data if isinstance(r.data, dict) else {}
        inner = node.get("data") if isinstance(node.get("data"), dict) else {}
        token = str(
            inner.get("document_id") or inner.get("url")
            or node.get("document_id") or node.get("url") or ""
        )
        if token:
            await _notify_doc_changed(token, summary=f"新建：{title}")
    return _fmt(r)


async def feishu_doc_append(doc_url: str, content_markdown: str) -> ToolResponse:
    """向飞书文档末尾追加 Markdown 内容。"""
    r = await _get_executor().run(
        "lark.docs.update",
        {
            "doc": doc_url,
            "command": "append",
            "content": content_markdown,
            "doc_format": "markdown",
        },
    )
    if r.ok:
        await _notify_doc_changed(doc_url, summary=f"追加：{content_markdown[:150]}")
    return _fmt(r)


async def feishu_doc_update(
    doc_url: str,
    command: str,
    content_markdown: str,
    pattern: str = "",
    block_id: str = "",
) -> ToolResponse:
    """原位修改飞书文档内容。

    command 三选一：
    - str_replace：把 pattern（旧文本，必须在文中唯一）替换为 content_markdown，改表格单元格/句子用这个；
    - block_replace：整体替换 block_id 指定的块；
    - block_insert_after：在 block_id 块之后插入新内容。
    block_id 由 feishu_doc_fetch 返回内容里的块 id 获取。
    """
    args: dict[str, Any] = {
        "doc": doc_url,
        "command": command,
        "content": content_markdown,
        "doc_format": "markdown",
    }
    if pattern:
        args["pattern"] = pattern
    if block_id:
        args["block_id"] = block_id
    r = await _get_executor().run("lark.docs.update", args)
    if r.ok:
        await _notify_doc_changed(doc_url, summary=f"{command}：{content_markdown[:150]}")
    return _fmt(r)


async def feishu_doc_search(query: str) -> ToolResponse:
    """搜索飞书文档/Wiki/电子表格，返回标题与链接列表。"""
    r = await _get_executor().run("lark.docs.search", {"query": query})
    return _fmt(r)


#: utils.ensure_tools_loaded 注册到共享 Toolkit 的全部飞书工具
AGENT_LARK_TOOLS = (
    feishu_doc_fetch,
    feishu_doc_create,
    feishu_doc_append,
    feishu_doc_update,
    feishu_doc_search,
)
