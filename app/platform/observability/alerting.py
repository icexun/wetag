"""告警发送；§9.2 告警条件的判定在各模块，发送收口在此。

统一出口：结构化日志 + 可注册 sink（生产绑定企微 webhook，测试绑定内存 sink）。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class AlertKind(StrEnum):
    SECURITY = "SECURITY"  # 越权（US1-2）
    FAILURE = "FAILURE"  # 连续失败（US3-9）/ 任务终止（US3-4）/ 出站失败（US5-8）
    DEGRADE = "DEGRADE"  # 降级生效（US2-10 / US8-9 / US11-6）


@dataclass(frozen=True)
class Alert:
    kind: AlertKind
    title: str
    detail: str = ""
    workspace_id: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


Sink = Callable[[Alert], None]

_sinks: list[Sink] = []


def register_sink(sink: Sink) -> None:
    """注册告警出口（gateway webhook / 测试内存 sink）。可注册多个。"""
    _sinks.append(sink)


def clear_sinks() -> None:
    _sinks.clear()


def alert(
    kind: AlertKind,
    title: str,
    detail: str = "",
    *,
    workspace_id: str | None = None,
    **fields: Any,
) -> Alert:
    """告警统一入口。sink 异常不冒泡——告警失败不得拖垮业务路径。"""
    item = Alert(kind=kind, title=title, detail=detail, workspace_id=workspace_id, fields=fields)
    logger.warning("[ALERT][%s] %s | %s | %s", kind, title, detail, fields)
    for sink in list(_sinks):
        try:
            sink(item)
        except Exception:  # noqa: BLE001
            logger.exception("alert sink 发送失败")
    return item
