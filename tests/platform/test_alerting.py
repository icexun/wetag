"""T008：告警统一出口、TraceContext、降级开关。"""
from __future__ import annotations

import pytest

from app.platform.degradation import DegradationSwitch
from app.platform.observability.alerting import (
    Alert,
    AlertKind,
    alert,
    clear_sinks,
    register_sink,
)
from app.platform.observability.trace_context import (
    TraceContext,
    chat_thread_id,
    sched_thread_id,
)


@pytest.fixture(autouse=True)
def _clean_sinks():
    clear_sinks()
    yield
    clear_sinks()


class TestAlerting:
    def test_alert_reaches_sink_with_kind(self):
        received: list[Alert] = []
        register_sink(received.append)
        item = alert(AlertKind.SECURITY, "疑似越权", "detail", workspace_id="alpha")
        assert received == [item]
        assert item.kind == AlertKind.SECURITY
        assert item.workspace_id == "alpha"

    def test_alert_kinds_closed_set(self):
        assert {k.value for k in AlertKind} == {"SECURITY", "FAILURE", "DEGRADE"}

    def test_sink_failure_does_not_propagate(self):
        def bad_sink(_: Alert) -> None:
            raise RuntimeError("webhook down")

        register_sink(bad_sink)
        alert(AlertKind.FAILURE, "x")  # 不抛异常


class TestTraceContext:
    def test_create_generates_trace_id(self):
        ctx = TraceContext.create("alpha", chat_id="c1", topic_id="t1", task_id="task-1")
        assert ctx.trace_id
        assert ctx.workspace_id == "alpha"

    def test_thread_id_namespaces_separated(self):
        assert chat_thread_id("c1", "t1") == "chat:c1:t1"
        assert sched_thread_id("alpha", "doc_poll", "run-1") == "sched:alpha:doc_poll:run-1"
        assert chat_thread_id("c1") == "chat:c1:default"


class _MemStore:
    def __init__(self):
        self.data: dict[str, dict] = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value):
        self.data[key] = value

    async def delete(self, key):
        self.data.pop(key, None)


class TestDegradationSwitch:
    async def test_activate_query_release(self):
        switch = DegradationSwitch(_MemStore())
        assert await switch.is_active("tapd_sync") is False

        await switch.activate("tapd_sync", reason="额度低于停用水位")
        assert await switch.is_active("tapd_sync") is True
        snap = await switch.snapshot("tapd_sync")
        assert snap["active"] is True
        assert "额度" in snap["reason"]

        await switch.release("tapd_sync")
        assert await switch.is_active("tapd_sync") is False
