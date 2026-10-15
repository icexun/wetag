"""Envelope 模型 + MsgType / Priority 枚举（§4.4）。

出站消息的唯一形态，经 Outbox 四道关卡发送；枚举取值与 outbox 表 CHECK 约束一致。
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class MsgType(StrEnum):
    MARKDOWN = "markdown"
    TEMPLATE_CARD = "template_card"


class Priority(StrEnum):
    URGENT = "urgent"  # 跳过合并窗口，可取令牌桶预留余量（US5-3 / TC-090）
    NORMAL = "normal"


class Envelope(BaseModel):
    chat_id: str
    msg_type: MsgType
    payload: dict[str, Any]
    topic_id: str | None = None
    priority: Priority = Priority.NORMAL
    merge_key: str | None = None  # 同键在窗口内合并（US5-1）；URGENT 必须为空
    update_of: str | None = None  # 原地更新目标 msg_id（US4-2）
    task_id: UUID | None = None  # 来源任务
    extra: dict[str, Any] = Field(default_factory=dict)
