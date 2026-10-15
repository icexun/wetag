"""TAPD 额度水位（US2-10）：``quota.py`` 与适配器的 ``quota_watermark``。

TAPD 没有"查剩余额度"的接口，水位是本地滑窗估算。本文件盯三条：

1. 估算口径诚实——调一次扣一次，成败都扣；
2. 服务端真信号（``QUOTA_EXCEEDED``）必须盖过本地估算，直接归零；
3. 滑窗老化后自动恢复，不需要人工复位。
"""

from __future__ import annotations

import json
from typing import Sequence

import pytest

from app.capability.integration.requirement.quota import QuotaMeter
from app.capability.integration.requirement.tapd_cli import TapdCliAdapter
from app.contracts.errors import PortUnavailable
from app.contracts.ids import WorkspaceId
from app.platform.cli.executor import CliExecutor
from app.platform.cli.registry import TAPD_COMMANDS

WS = WorkspaceId("alpha")


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# QuotaMeter 本体
# ---------------------------------------------------------------------------


def test_calls_draw_down_remaining() -> None:
    clock = FakeClock()
    meter = QuotaMeter(limit=100, clock=clock)

    for _ in range(30):
        meter.record()

    wm = meter.watermark()
    assert wm["remaining"] == 70
    assert wm["used"] == 30
    assert wm["limit"] == 100


def test_remaining_never_goes_negative() -> None:
    meter = QuotaMeter(limit=5, clock=FakeClock())
    for _ in range(8):
        meter.record()
    assert meter.watermark()["remaining"] == 0


def test_aged_calls_recover_the_budget() -> None:
    """滑窗外的调用不再占额度——恢复是自然的，不需要复位动作。"""
    clock = FakeClock()
    meter = QuotaMeter(limit=10, window_s=60.0, clock=clock)

    for _ in range(10):
        meter.record()
    assert meter.watermark()["remaining"] == 0

    clock.advance(61.0)
    assert meter.watermark()["remaining"] == 10


def test_server_exhaustion_zeroes_the_watermark() -> None:
    """服务端报耗尽是唯一真信号：哪怕本地估算还有余量，也直接归零。"""
    clock = FakeClock()
    meter = QuotaMeter(limit=1000, window_s=60.0, clock=clock)
    meter.record()

    meter.note_exhausted()

    assert meter.watermark()["remaining"] == 0


def test_exhaustion_recovers_with_the_window() -> None:
    clock = FakeClock()
    meter = QuotaMeter(limit=1000, window_s=60.0, clock=clock)
    meter.record()
    meter.note_exhausted()

    clock.advance(61.0)

    assert meter.watermark()["remaining"] == 1000


# ---------------------------------------------------------------------------
# CLI 适配器：调用计数 + QUOTA_EXCEEDED 归零
# ---------------------------------------------------------------------------


class FakeSpawn:
    def __init__(self, result: tuple[int, str, str]) -> None:
        self.result = result

    async def __call__(
        self, argv: Sequence[str], *, env: dict[str, str], cwd: str | None
    ) -> tuple[int, bytes, bytes]:
        code, out, err = self.result
        return code, out.encode(), err.encode()


def _cli_adapter(
    spawn: FakeSpawn, meter: QuotaMeter
) -> TapdCliAdapter:
    executor = CliExecutor(
        workspace_id=str(WS),
        binaries={"tapd": "tapd-cli"},
        secrets={"TAPD_TOKEN": "token"},
        specs=TAPD_COMMANDS,
        spawn=spawn,
        max_attempts=1,
    )
    return TapdCliAdapter(executor, workspace_id=WS, project_id="6001", quota=meter)


async def test_cli_adapter_counts_calls() -> None:
    meter = QuotaMeter(limit=100, clock=FakeClock())
    spawn = FakeSpawn(
        (0, json.dumps({"status": 1, "data": {"status": {"options": {"s1": "开发中"}}}}), "")
    )
    adapter = _cli_adapter(spawn, meter)

    await adapter.project_config("6001")

    wm = await adapter.quota_watermark()
    assert wm["used"] == 1
    assert wm["remaining"] == 99


async def test_cli_adapter_zeroes_on_quota_exceeded() -> None:
    """stderr 命中 QUOTA_EXCEEDED 模式 → 水位归零，而不是按本地计数报 99。"""
    meter = QuotaMeter(limit=100, clock=FakeClock())
    spawn = FakeSpawn((1, "", "429 too many requests: quota exceeded"))
    adapter = _cli_adapter(spawn, meter)

    with pytest.raises(PortUnavailable):
        await adapter.project_config("6001")

    assert (await adapter.quota_watermark())["remaining"] == 0
