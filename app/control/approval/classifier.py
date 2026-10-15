"""四级分档判定：只读 / 低危写 / 高危写 / 删除（§4.6，裁决 #13）。

分档表配在业务线上（控制台可改，经 risk_table 注入）；未命中分档表的
工具按名称启发式归类，未知写操作一律按高危处理（安全侧默认，不 fail-open）。
"""
from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Mapping


class RiskLevel(StrEnum):
    READ = "read"  # 只读：直通（US7-1）
    LOW_WRITE = "low_write"  # 低危写：先执行后告知（US7-2）
    HIGH_WRITE = "high_write"  # 高危写：先批后写（US7-3）
    FORBIDDEN = "forbidden"  # 删除类：直接拒绝，Auto Mode 无效（US7-5）


# 默认分档表：工具名 → 风险级。业务线覆盖经 classify(table=...) 注入。
DEFAULT_RISK_TABLE: dict[str, RiskLevel] = {
    # 只读（US7-1：读文档 / 检索代码 / 召回记忆）
    "doc.get": RiskLevel.READ,
    "doc.list_meta": RiskLevel.READ,
    "repo.search": RiskLevel.READ,
    "memory.recall": RiskLevel.READ,
    "todo.get_batch": RiskLevel.READ,
    "requirement.get": RiskLevel.READ,
    "tapd_fields": RiskLevel.READ,
    "tapd_story_changes": RiskLevel.READ,
    "tapd_story_related_bugs": RiskLevel.READ,
    # 低危写（US7-2：创建需求卡片 / 创建待办）
    # TAPD 需求创建与更新无需审批，直通后只留审计
    "requirement.create_card": RiskLevel.LOW_WRITE,
    "tapd_story_create": RiskLevel.LOW_WRITE,
    "tapd_story_update": RiskLevel.LOW_WRITE,
    "tapd_bug_create": RiskLevel.LOW_WRITE,
    "tapd_bug_update": RiskLevel.LOW_WRITE,
    "tapd_task_create": RiskLevel.LOW_WRITE,
    "tapd_task_update": RiskLevel.LOW_WRITE,
    "tapd_comment_add": RiskLevel.LOW_WRITE,
    "todo.create": RiskLevel.LOW_WRITE,
    "wecom_todo_create": RiskLevel.LOW_WRITE,
    # 订阅只影响当前群收什么通知，不改外部系统
    "wetag_subscribe_changes": RiskLevel.LOW_WRITE,
    "wetag_unsubscribe_changes": RiskLevel.LOW_WRITE,
    # 高危写（US7-3：写文档 / 变更需求状态）
    "doc.update": RiskLevel.HIGH_WRITE,
    "requirement.update_status": RiskLevel.HIGH_WRITE,
    **dict.fromkeys(
        (
            "feishu_sheet_read", "feishu_sheet_search", "feishu_base_tables",
            "feishu_base_fields", "feishu_base_records", "feishu_user_search",
            "feishu_task_my_open", "feishu_task_search", "feishu_calendar_agenda",
            "feishu_calendar_freebusy", "feishu_calendar_search", "feishu_approval_tasks",
            "feishu_approval_instance", "feishu_wiki_nodes",
            "wecom_user_search", "wecom_chat_list", "wecom_schedule_list",
            "wecom_schedule_get", "wecom_meeting_list", "wecom_meeting_get",
            "wecom_doc_search", "wecom_doc_read", "wecom_sheet_info", "wecom_sheet_read",
            "wecom_smartsheet_info", "wecom_smartsheet_records", "wecom_disk_search",
            "wecom_mail_search", "wecom_mail_get",
        ),
        RiskLevel.READ,
    ),
    # 只动授权用户自己的待办 / 日程，可撤销
    **dict.fromkeys(
        (
            "feishu_task_create", "feishu_task_complete", "feishu_calendar_create",
            "wecom_schedule_create", "wecom_meeting_create",
        ),
        RiskLevel.LOW_WRITE,
    ),
    # 改共享数据或对外发出，先批后写
    **dict.fromkeys(
        (
            "feishu_sheet_write_csv", "feishu_base_upsert", "feishu_wiki_node_create",
            "wecom_message_send", "wecom_sheet_append_row", "wecom_smartsheet_add",
            "wecom_smartsheet_update", "wecom_mail_send",
        ),
        RiskLevel.HIGH_WRITE,
    ),
}

_DELETE_HINTS = ("delete", "remove", "destroy", "drop")
_READ_HINTS = ("get", "list", "search", "read", "recall", "query", "fetch", "find")


def classify(
    tool_name: str,
    tool_input: Mapping[str, Any] | None = None,
    *,
    table: Mapping[str, RiskLevel] | None = None,
) -> RiskLevel:
    """判定一次工具调用的风险级。

    优先查分档表（业务线配置覆盖默认表）；memory.remember 按 scope 参数分档
    （群级=低危写，业务线级=高危写，US7-2/US7-3）；未命中按名称启发式，
    删除语义优先判 FORBIDDEN，未知工具默认 HIGH_WRITE（不 fail-open）。
    """
    effective = dict(DEFAULT_RISK_TABLE)
    if table:
        effective.update({k: RiskLevel(v) for k, v in table.items()})
    if tool_name in effective:
        return effective[tool_name]

    if tool_name == "memory.remember":
        scope = str((tool_input or {}).get("scope", "")).lower()
        return RiskLevel.LOW_WRITE if scope == "group" else RiskLevel.HIGH_WRITE

    # 外接 MCP 工具名形态各异：下划线、驼峰（fooController_batchGetBars）。
    # 统一归一成 snake_case 再匹配，
    # 否则读操作落入默认 HIGH_WRITE 被审批闸门误拦
    lowered = re.sub(r"(?<!^)(?=[A-Z])", "_", tool_name).lower()
    if any(hint in lowered for hint in _DELETE_HINTS):
        return RiskLevel.FORBIDDEN
    if any(
        lowered.startswith(hint) or f".{hint}" in lowered or f"_{hint}" in lowered
        for hint in _READ_HINTS
    ):
        return RiskLevel.READ
    if "create" in lowered:
        return RiskLevel.LOW_WRITE
    return RiskLevel.HIGH_WRITE
