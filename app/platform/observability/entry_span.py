"""任务级 Entry Span（OpenTelemetry）；session_id 取 task_id。

``opentelemetry-api`` 为可选依赖：未安装时降级为 no-op。
安装后由使用方自行配置 exporter（如 ``opentelemetry-instrument``）。
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

logger = logging.getLogger(__name__)

try:
    from opentelemetry import trace as _otel_trace

    OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - 取决于部署环境
    _otel_trace = None
    OTEL_AVAILABLE = False
    logger.info("opentelemetry 不可用，entry_span 降级为 no-op")


def is_available() -> bool:
    """OpenTelemetry 是否可用；不可用时 entry_span 为 no-op。"""
    return OTEL_AVAILABLE


def _tracer() -> Any:
    return _otel_trace.get_tracer("wetag")


@asynccontextmanager
async def entry_span(task_id: str, name: str = "wetag-task") -> AsyncIterator[Any]:
    """一次任务执行的 Entry Span。异常记入 span 后原样上抛。"""
    if not OTEL_AVAILABLE:
        yield None
        return
    with _tracer().start_as_current_span(
        name, attributes={"session.id": task_id, "wetag.job": name}
    ) as span:
        yield span
