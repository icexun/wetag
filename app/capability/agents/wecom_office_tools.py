"""对话 Agent 的企业微信协作工具（``wecom-cli``）：通讯录、会话、消息、日程、会议、文档、
在线表格、智能表格、微盘、邮件。

请求体在这里按强类型参数拼装，模型拿不到任意字段：本地文件路径（附件、正文文件）、
删除类操作都不开放，登记表的 ``WecomBodyArgs`` 再兜一道。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Any

from agentscope.tool import ToolResponse

from app.capability.agents.tool_result import tool_text
from app.platform.cli.executor import CliExecutor
from app.platform.cli.registry import WECOM_OFFICE_COMMANDS

_executor: Any | None = None
# 每次响应都附带的身份说明，对模型是噪声
_DROP_KEYS = ("extra_identity_context",)
_FMT = "%Y-%m-%d %H:%M:%S"


def bind_dependencies(executor: Any | None) -> None:
    global _executor
    _executor = executor


def _get_executor() -> Any:
    global _executor
    if _executor is None:
        _executor = CliExecutor(
            workspace_id=os.environ.get("WORKSPACE_ID", "default"),
            binaries={"wecom": os.environ.get("WECOM_CLI_BIN", "wecom-cli")},
            secrets={"WECOM_CLI_TOKEN": os.environ.get("WECOM_CLI_TOKEN", "")},
            specs=WECOM_OFFICE_COMMANDS,
            timeout_s=60.0,
        )
    return _executor


def _compact(body: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in body.items() if v not in (None, "", [], {})}


async def _call(key: str, body: dict[str, Any]) -> ToolResponse:
    r = await _get_executor().run(key, {"json": json.dumps(_compact(body), ensure_ascii=False)})
    if not r.ok:
        out = {"ok": False, "error_code": str(r.error_code)}
        if isinstance(r.data, dict):
            # help_message 是企微给的授权指引，须原样转给用户
            out.update(r.data)
        else:
            out["stderr"] = r.stderr_digest
        return tool_text(out)
    data = r.data if isinstance(r.data, dict) else {"data": r.data}
    return tool_text({k: v for k, v in data.items() if k not in _DROP_KEYS})


def _ids(value: str | list[str] | None) -> list[str]:
    if not value:
        return []
    items = value if isinstance(value, list) else str(value).replace("，", ",").split(",")
    return [str(x).strip() for x in items if str(x).strip()]


# --- 通讯录 / 会话 / 消息 -----------------------------------------------------


async def wecom_user_search(name: str) -> ToolResponse:
    """按姓名/拼音/英文名搜索企业微信成员，返回 userid（日程、会议、消息、邮件用它）。"""
    return await _call("wecom.contact.search", {"keywords": [name], "search_mode": "list"})


async def wecom_chat_list(days: int = 7) -> ToolResponse:
    """列出最近有消息的会话（最多 7 天），返回 chat_id 供 wecom_message_send 使用。"""
    end = datetime.now()
    begin = end - timedelta(days=max(1, min(int(days), 7)))
    return await _call(
        "wecom.chat.groups", {"begin_time": begin.strftime(_FMT), "end_time": end.strftime(_FMT)}
    )


async def wecom_message_send(chat_id: str, text: str) -> ToolResponse:
    """向另一个单聊（成员 userid）或群聊（chat_id）发送文本消息，≤2048 字。回复当前群不要用它。"""
    return await _call(
        "wecom.message.send",
        {"chat_id": chat_id, "msg_type": "text", "text": {"content": text[:2048]}},
    )


# --- 日程 / 会议 -------------------------------------------------------------


async def wecom_schedule_list(begin_time: str = "", end_time: str = "") -> ToolResponse:
    """查询授权用户的日程（默认今后 7 天）。时间格式 YYYY-MM-DD HH:MM:SS。"""
    if not begin_time and not end_time:
        now = datetime.now()
        begin_time = now.strftime(_FMT)
        end_time = (now + timedelta(days=7)).strftime(_FMT)
    return await _call("wecom.calendar.list", {"begin_time": begin_time, "end_time": end_time})


async def wecom_schedule_get(schedule_ids: list[str]) -> ToolResponse:
    """按 ID 查看日程详情。"""
    return await _call("wecom.calendar.get", {"schedule_ids": _ids(schedule_ids)})


async def wecom_schedule_create(
    subject: str, begin_time: str, end_time: str, attendee_userids: str = "",
    location: str = "", description: str = "",
) -> ToolResponse:
    """创建企业微信日程（不含会议链接）。时间 YYYY-MM-DD HH:MM:SS；参与人 userid 逗号分隔。"""
    return await _call(
        "wecom.calendar.create",
        {
            "subject": subject, "begin_time": begin_time, "end_time": end_time,
            "attendees": [{"userid": u} for u in _ids(attendee_userids)],
            "location": location, "description": description,
        },
    )


async def wecom_meeting_list(begin_time: str = "", end_time: str = "", limit: int = 20) -> ToolResponse:
    """查询授权用户的在线会议（默认今后 30 天）。"""
    return await _call(
        "wecom.meeting.list",
        {"begin_time": begin_time, "end_time": end_time, "limit": max(1, min(int(limit), 100))},
    )


async def wecom_meeting_get(meeting_id: str) -> ToolResponse:
    """查看在线会议详情（含参会人、智能纪要与录制地址）。"""
    return await _call("wecom.meeting.get", {"meeting_ids": [{"meeting_id": meeting_id}]})


async def wecom_meeting_create(
    subject: str, begin_time: str, end_time: str, attendee_userids: str = "",
    location: str = "", description: str = "",
) -> ToolResponse:
    """创建企业微信在线会议（带入会链接）。时间 YYYY-MM-DD HH:MM:SS；参与人 userid 逗号分隔。"""
    return await _call(
        "wecom.meeting.create",
        {
            "subject": subject, "begin_time": begin_time, "end_time": end_time,
            "attendees": [{"userid": u} for u in _ids(attendee_userids)],
            "location": location, "description": description,
        },
    )


# --- 文档 / 在线表格 / 智能表格 / 微盘 ---------------------------------------------


async def wecom_doc_search(keywords: str, limit: int = 10) -> ToolResponse:
    """按关键词搜索企业微信文档（标题+正文），关键词逗号分隔。"""
    return await _call(
        "wecom.doc.search", {"keywords": _ids(keywords), "limit": max(1, min(int(limit), 100))}
    )


async def wecom_doc_read(docid: str) -> ToolResponse:
    """读取企业微信文档正文（Markdown）。docid 可传文档链接。"""
    return await _call("wecom.doc.get", {"docid": docid, "content_type": "markdown"})


async def wecom_sheet_info(docid: str) -> ToolResponse:
    """查看在线表格的工作表列表（sheet_id、标题、行列数）。"""
    return await _call("wecom.sheet.get", {"docid": docid})


async def wecom_sheet_read(docid: str, sheet_id: str, range: str = "") -> ToolResponse:
    """读取在线表格数据（CSV）。range 用 A1 表示法，省略读全部。"""
    return await _call(
        "wecom.sheet.range_get",
        {"docid": docid, "sheet_id": sheet_id, "range": range, "mode": "csv"},
    )


async def wecom_sheet_append_row(docid: str, sheet_id: str, values: list[str]) -> ToolResponse:
    """在在线表格末尾追加一行文本，values 按列顺序。"""
    cells = [{"cell_value": {"text": str(v)}, "cell_format": {}} for v in values]
    return await _call(
        "wecom.sheet.rows_append", {"docid": docid, "sheet_id": sheet_id, "row": {"values": cells}}
    )


async def wecom_smartsheet_info(docid: str) -> ToolResponse:
    """查看智能表格的子表与字段概要。"""
    return await _call("wecom.smartsheet.get", {"docid": docid})


async def wecom_smartsheet_records(docid: str, sheet_id: str, limit: int = 50) -> ToolResponse:
    """读取智能表格子表记录。"""
    return await _call(
        "wecom.smartsheet.records_list",
        {"docid": docid, "sheet_id": sheet_id, "type": "records", "key_type": "field_title",
         "limit": max(1, min(int(limit), 1000))},
    )


async def wecom_smartsheet_add(
    docid: str, sheet_id: str, records: list[dict[str, Any]]
) -> ToolResponse:
    """向智能表格新增记录。records 每项为 {字段标题: 值}；文本 "x"，数字 1，单选 [{"text":"选项"}]。"""
    return await _call(
        "wecom.smartsheet.records_add",
        {"docid": docid, "sheet_id": sheet_id, "key_type": "field_title",
         "records": [{"values": dict(r)} for r in records or []]},
    )


async def wecom_smartsheet_update(
    docid: str, sheet_id: str, records: list[dict[str, Any]]
) -> ToolResponse:
    """更新智能表格记录。records 每项为 {"record_id": "...", "values": {字段标题: 值}}。"""
    rows = [
        {"record_id": str(r.get("record_id") or ""), "values": dict(r.get("values") or {})}
        for r in records or []
    ]
    return await _call(
        "wecom.smartsheet.records_update",
        {"docid": docid, "sheet_id": sheet_id, "key_type": "field_title", "records": rows},
    )


async def wecom_disk_search(keywords: str, limit: int = 10) -> ToolResponse:
    """在企业微信微盘中搜索文件/文件夹，关键词逗号分隔。"""
    return await _call(
        "wecom.disk.search", {"keywords": _ids(keywords), "limit": max(1, min(int(limit), 100))}
    )


# --- 邮件 -----------------------------------------------------------------


async def wecom_mail_search(
    keywords: str = "", sender: str = "", only_unread: bool = False, limit: int = 20
) -> ToolResponse:
    """搜索授权用户的邮件，关键词逗号分隔。"""
    return await _call(
        "wecom.mail.search",
        {"keywords": _ids(keywords), "sender": sender, "only_unread": only_unread or None,
         "limit": max(1, min(int(limit), 50))},
    )


async def wecom_mail_get(mail_id: str) -> ToolResponse:
    """读取一封邮件的详情（mail_id 来自 wecom_mail_search）。"""
    return await _call("wecom.mail.get", {"mail_ids": [mail_id]})


async def wecom_mail_send(
    subject: str, content_markdown: str, to_userids: str = "", to_emails: str = "",
    cc_userids: str = "", cc_emails: str = "",
) -> ToolResponse:
    """以授权用户身份发送邮件（Markdown 正文，不支持附件）。收件人 userid 或邮箱，逗号分隔。"""
    def _who(userids: str, emails: str) -> dict[str, list[str]]:
        return _compact({"userids": _ids(userids), "emails": _ids(emails)})

    return await _call(
        "wecom.mail.send",
        {
            "subject": subject, "content": content_markdown, "content_type": "markdown",
            "to": _who(to_userids, to_emails), "cc": _who(cc_userids, cc_emails),
        },
    )


#: 注册到共享 Toolkit 的企业微信协作工具
AGENT_WECOM_OFFICE_TOOLS = (
    wecom_user_search,
    wecom_chat_list,
    wecom_message_send,
    wecom_schedule_list,
    wecom_schedule_get,
    wecom_schedule_create,
    wecom_meeting_list,
    wecom_meeting_get,
    wecom_meeting_create,
    wecom_doc_search,
    wecom_doc_read,
    wecom_sheet_info,
    wecom_sheet_read,
    wecom_sheet_append_row,
    wecom_smartsheet_info,
    wecom_smartsheet_records,
    wecom_smartsheet_add,
    wecom_smartsheet_update,
    wecom_disk_search,
    wecom_mail_search,
    wecom_mail_get,
    wecom_mail_send,
)

__all__ = ["AGENT_WECOM_OFFICE_TOOLS", "bind_dependencies"]
