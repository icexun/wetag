"""covers: TC-017

TC-017 额度水位触发同步降级并自动恢复（US2-10）
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.contracts.task import Task, TaskKind, TriggerSource
from app.platform.degradation import QuotaWatermark, degrade_key, quota_key
from app.platform.observability.alerting import AlertKind
from app.runtime.jobs.base import JobContext
from app.runtime.jobs.tapd_sync import LAST_RUN_KEY, TapdSyncJob


class _RequirementPort:
    """RequirementPort 替身：额度余量可变。"""

    def __init__(self, remaining: int) -> None:
        self.remaining = remaining

    async def quota_watermark(self) -> dict:
        return {"remaining": self.remaining}


def _ctx(workspace_id: str, state_store) -> JobContext:
    task = Task(
        id=uuid4(),
        workspace_id=workspace_id,
        job_name="tapd_sync",
        kind=TaskKind.DETERMINISTIC,
        trigger_source=TriggerSource.SCHEDULE,
        scheduled_at=datetime.now(UTC),
    )
    return JobContext(task=task, workspace_id=workspace_id, state=state_store)


class TestQuotaDegradation:
    """TC-017: 安全水位降频告警 / 停用水位停跑 / 状态可查 / 恢复自动解除。"""

    async def test_watermark_lifecycle(self, task_repo, state_store, alerts):
        port = _RequirementPort(remaining=1000)
        synced: list[int] = []

        async def sync(ctx, p) -> int:
            synced.append(1)
            return 3

        job = TapdSyncJob(
            port,
            watermark=QuotaWatermark(safe=100, disabled=50),
            reduced_interval_s=1800,
            sync_handler=sync,
        )
        ws = task_repo.workspace_id

        # 1. 额度正常：同步执行，无降级
        result = await job.run(_ctx(ws, state_store))
        assert result.metrics["synced"] == 3
        assert await state_store.get(degrade_key("tapd_sync")) is None
        quota = await state_store.get(quota_key("tapd"))
        assert quota["remaining"] == 1000  # 水位对运维可见

        # 2. 低于安全水位：降频 + 告警 + 状态可查；过密轮次跳过
        port.remaining = 80
        result = await job.run(_ctx(ws, state_store))
        assert result.metrics["degraded"] == "reduced"
        assert result.metrics["synced"] == 0  # 距上次同步不足降频间隔，跳过
        state = await state_store.get(degrade_key("tapd_sync"))
        assert state["active"] and state["level"] == "reduced"  # 降级状态可查询
        assert any(
            a.kind == AlertKind.DEGRADE and "reduced" in a.title for a in alerts
        )

        # 降频间隔过后仍同步一次（降频而非停止）
        await state_store.set(
            LAST_RUN_KEY,
            {"at": (datetime.now(UTC) - timedelta(seconds=1801)).isoformat()},
        )
        result = await job.run(_ctx(ws, state_store))
        assert result.metrics["synced"] == 3
        assert result.metrics["degraded"] == "reduced"

        # 3. 低于停用水位：停跑；「进行中」仅依据文档更新时间
        port.remaining = 10
        result = await job.run(_ctx(ws, state_store))
        assert result.metrics["degraded"] == "disabled"
        assert result.metrics["synced"] == 0
        assert result.metrics["in_progress_basis"] == "doc_updated_at"
        state = await state_store.get(degrade_key("tapd_sync"))
        assert state["level"] == "disabled"

        # 4. 额度恢复：自动解除降级并告警
        port.remaining = 1000
        before = len(alerts)
        result = await job.run(_ctx(ws, state_store))
        assert result.metrics["synced"] == 3
        assert await state_store.get(degrade_key("tapd_sync")) is None  # 自动解除
        assert any(
            a.kind == AlertKind.DEGRADE and a.fields.get("resolved")
            for a in alerts[before:]
        )

    async def test_degrade_alert_fires_once_per_level(self, task_repo, state_store, alerts):
        """同级降级不重复告警（级别变化才告）。"""
        port = _RequirementPort(remaining=80)
        job = TapdSyncJob(port, watermark=QuotaWatermark(safe=100, disabled=50))
        ws = task_repo.workspace_id
        await job.run(_ctx(ws, state_store))
        await job.run(_ctx(ws, state_store))
        degrade_alerts = [a for a in alerts if a.kind == AlertKind.DEGRADE]
        assert len(degrade_alerts) == 1
