"""对话 Agent 的飞书协作工具（``lark-cli``）：表格、多维表格、任务、日历、审批（只读）、
知识库、通讯录。

调用经 ``CliExecutor``（白名单、参数 schema、凭据最小注入、审计）。审批只开放查询，
同意 / 拒绝 / 转交不登记——那是审批人本人的决定，不由机器人代做。
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any
from urllib.parse import urlsplit

from agentscope.tool import ToolResponse

from app.capability.agents.tool_result import tool_text
from app.capability.agents.wecom_tools import current_task_context
from app.platform.cli.executor import CliExecutor
from app.platform.cli.registry import LARK_OFFICE_COMMANDS

_executor: Any | None = None


def bind_dependencies(executor: Any | None) -> None:
    global _executor
    _executor = executor


def _get_executor() -> Any:
    global _executor
    if _executor is None:
        _executor = CliExecutor(
            workspace_id=os.environ.get("WORKSPACE_ID", "default"),
            binaries={"lark": os.environ.get("LARK_CLI_BIN", "lark-cli")},
            secrets={"LARK_CLI_TOKEN": os.environ.get("LARK_CLI_TOKEN", "")},
            specs=LARK_OFFICE_COMMANDS,
            timeout_s=60.0,
        )
    return _executor


async def _call(key: str, args: dict[str, Any]) -> ToolResponse:
    clean = {k: v for k, v in args.items() if v not in (None, "", [])}
    r = await _get_executor().run(key, clean)
    if r.ok:
        return tool_text(r.data)
    return tool_text({"ok": False, "error_code": str(r.error_code), "stderr": r.stderr_digest})


def _token(url_or_token: str) -> str:
    """多维表格链接 → base token（路径最后一段）；裸 token 原样。"""
    if "://" in url_or_token:
        return urlsplit(url_or_token).path.rstrip("/").rsplit("/", 1)[-1]
    return url_or_token.strip()


def _sheet(sheet_name: str, sheet_id: str) -> dict[str, str]:
    return {"sheet_id": sheet_id} if sheet_id else {"sheet_name": sheet_name}


# --- 电子表格 -------------------------------------------------------------


async def feishu_sheet_read(
    url: str, sheet_name: str = "", sheet_id: str = "", range: str = ""
) -> ToolResponse:
    """读取飞书电子表格为 CSV。sheet_name 与 sheet_id 给一个；range 如 A1:F30，省略读整张表。"""
    return await _call(
        "lark.sheets.csv_get", {"url": url, **_sheet(sheet_name, sheet_id), "range": range}
    )


async def feishu_sheet_search(
    url: str, find: str, sheet_name: str = "", sheet_id: str = ""
) -> ToolResponse:
    """在飞书电子表格中查找包含 find 的单元格。"""
    return await _call(
        "lark.sheets.cells_search", {"url": url, **_sheet(sheet_name, sheet_id), "find": find}
    )


async def feishu_sheet_write_csv(
    url: str, start_cell: str, csv: str, sheet_name: str = "", sheet_id: str = ""
) -> ToolResponse:
    """把 CSV 写入飞书电子表格，start_cell 为左上角（如 A1）。会覆盖目标区域。"""
    return await _call(
        "lark.sheets.csv_put",
        {"url": url, **_sheet(sheet_name, sheet_id), "start_cell": start_cell, "csv": csv},
    )


# --- 多维表格 -------------------------------------------------------------


async def feishu_base_tables(base: str) -> ToolResponse:
    """列出多维表格里的数据表。base 为多维表格链接或 token。"""
    return await _call("lark.base.table_list", {"base_token": _token(base)})


async def feishu_base_fields(base: str, table_id: str) -> ToolResponse:
    """列出数据表的字段（名称、类型、选项）。写记录前先查。"""
    return await _call(
        "lark.base.field_list", {"base_token": _token(base), "table_id": table_id}
    )


async def feishu_base_records(
    base: str, table_id: str, view_id: str = "", filter_json: str = "",
    limit: int = 50, offset: int = 0,
) -> ToolResponse:
    """读取多维表格记录。filter_json 同视图筛选 JSON；limit ≤ 200。"""
    return await _call(
        "lark.base.record_list",
        {
            "base_token": _token(base), "table_id": table_id, "view_id": view_id,
            "filter_json": filter_json, "limit": limit, "offset": offset,
        },
    )


async def feishu_base_upsert(
    base: str, table_id: str, fields: dict[str, Any], record_id: str = ""
) -> ToolResponse:
    """新建（不传 record_id）或更新一条多维表格记录。fields 为 {字段名: 值}；单选 ["选项"]，人员 [{"id":"ou_xxx"}]。"""
    return await _call(
        "lark.base.record_upsert",
        {
            "base_token": _token(base), "table_id": table_id, "record_id": record_id,
            "json": json.dumps(fields or {}, ensure_ascii=False),
        },
    )


# --- 通讯录 / 任务 -----------------------------------------------------------


async def feishu_user_search(query: str) -> ToolResponse:
    """按姓名/邮箱搜索飞书用户，返回 open_id（任务负责人、日程参与人用它）。"""
    return await _call("lark.contact.search", {"query": query})


async def feishu_task_create(
    summary: str, description: str = "", due: str = "", assignee_open_id: str = ""
) -> ToolResponse:
    """创建飞书任务。due 支持 YYYY-MM-DD 或 relative:+2d；assignee_open_id 先用 feishu_user_search 查。"""
    ctx = current_task_context()
    seed = f"{ctx.get('task_id') or ''}|{summary}|{due}|{assignee_open_id}"
    return await _call(
        "lark.task.create",
        {
            "summary": summary, "description": description, "due": due,
            "assignee": assignee_open_id,
            # 同一任务内重放（重试 / 模型重复调用）不重复建
            "idempotency_key": "wetag-" + hashlib.sha256(seed.encode()).hexdigest()[:32],
        },
    )


async def feishu_task_my_open(query: str = "", due_end: str = "") -> ToolResponse:
    """列出授权用户名下未完成的飞书任务。due_end 只看此日期前到期的。"""
    return await _call("lark.task.my_open", {"query": query, "due_end": due_end})


async def feishu_task_search(query: str) -> ToolResponse:
    """按关键词搜索飞书任务。"""
    return await _call("lark.task.search", {"query": query})


async def feishu_task_complete(task_id: str) -> ToolResponse:
    """把飞书任务标为已完成。task_id 为任务 GUID 或任务链接。"""
    return await _call("lark.task.complete", {"task_id": task_id})


# --- 日历 -----------------------------------------------------------------


async def feishu_calendar_agenda(start: str = "", end: str = "") -> ToolResponse:
    """查看授权用户的日程（默认今天）。时间用 ISO 8601。"""
    return await _call("lark.calendar.agenda", {"start": start, "end": end})


async def feishu_calendar_freebusy(
    start: str = "", end: str = "", user_open_id: str = ""
) -> ToolResponse:
    """查询某人的忙闲（默认授权用户本人、今天）。"""
    return await _call(
        "lark.calendar.freebusy", {"start": start, "end": end, "user_id": user_open_id}
    )


async def feishu_calendar_search(query: str = "", start: str = "", end: str = "") -> ToolResponse:
    """按关键词与时间范围搜索日程。"""
    return await _call("lark.calendar.search", {"query": query, "start": start, "end": end})


async def feishu_calendar_create(
    summary: str, start: str, end: str, description: str = "", attendee_ids: str = ""
) -> ToolResponse:
    """创建飞书日程并邀请参与人。attendee_ids 逗号分隔（用户 ou_、群 oc_、会议室 omm_）。"""
    return await _call(
        "lark.calendar.create",
        {
            "summary": summary, "start": start, "end": end,
            "description": description, "attendee_ids": attendee_ids,
        },
    )


# --- 审批（只读） ----------------------------------------------------------


async def feishu_approval_tasks(topic: str = "1") -> ToolResponse:
    """查询授权用户的审批任务。topic：1 待办 / 2 已办 / 3 已发起 / 17 未读知会 / 18 已读知会。"""
    return await _call("lark.approval.tasks", {"topic": topic})


async def feishu_approval_instance(instance_code: str) -> ToolResponse:
    """查看一个审批实例的详情与进度。"""
    return await _call("lark.approval.instance", {"instance_code": instance_code})


# --- 知识库 ---------------------------------------------------------------


async def feishu_wiki_nodes(space_id: str, parent_node_token: str = "") -> ToolResponse:
    """列出知识库节点。不传 parent_node_token 列根节点；space_id 用 my_library 表示个人文档库。"""
    return await _call(
        "lark.wiki.node_list", {"space_id": space_id, "parent_node_token": parent_node_token}
    )


async def feishu_wiki_node_create(
    title: str, space_id: str = "", parent_node_token: str = "", obj_type: str = "docx"
) -> ToolResponse:
    """在知识库下新建节点。obj_type：docx / sheet / bitable / mindnote。"""
    return await _call(
        "lark.wiki.node_create",
        {
            "title": title, "space_id": space_id,
            "parent_node_token": parent_node_token, "obj_type": obj_type,
        },
    )


#: 注册到共享 Toolkit 的飞书协作工具
AGENT_LARK_OFFICE_TOOLS = (
    feishu_sheet_read,
    feishu_sheet_search,
    feishu_sheet_write_csv,
    feishu_base_tables,
    feishu_base_fields,
    feishu_base_records,
    feishu_base_upsert,
    feishu_user_search,
    feishu_task_create,
    feishu_task_my_open,
    feishu_task_search,
    feishu_task_complete,
    feishu_calendar_agenda,
    feishu_calendar_freebusy,
    feishu_calendar_search,
    feishu_calendar_create,
    feishu_approval_tasks,
    feishu_approval_instance,
    feishu_wiki_nodes,
    feishu_wiki_node_create,
)

__all__ = ["AGENT_LARK_OFFICE_TOOLS", "bind_dependencies"]
