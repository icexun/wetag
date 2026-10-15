"""确定性：TAPD 状态同步。

外部额度水位降级（US2-10）：
- 低于安全水位 → 降频（reduced）：按 reduced_interval 跳过过密轮次，并发 DEGRADE 告警
- 低于停用水位 → 停跑（disabled）：本轮不同步，「进行中」结论仅依据文档更新时间
- 额度恢复 → 自动解除降级并告警
降级状态落 runtime_state（degrade:tapd_sync / quota:tapd），对运维与控制台可见。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import config
from app.contracts.job_result import JobResult
from app.contracts.task import TaskKind
from app.platform.degradation import (
    QuotaWatermark,
    apply_degrade,
    quota_key,
    release_degrade,
)
from app.runtime.jobs.base import Job, JobContext, require_port

LAST_RUN_KEY = "tapd_sync:last_run"


def _watermark_from_config() -> QuotaWatermark:
    return QuotaWatermark(
        safe=config.get_int("TAPD_QUOTA_SAFE", 100),
        disabled=config.get_int("TAPD_QUOTA_DISABLED", 50),
    )


class TapdSyncJob(Job):
    """TAPD 同步骨架：同步业务逻辑由 sync_handler（Capability）承接；降级判定在此实现。"""

    name = "tapd_sync"
    kind = TaskKind.DETERMINISTIC
    timeout_s = 120.0
    default_cron = "*/5 * * * *"

    def __init__(
        self,
        requirement_port: Any = None,
        *,
        watermark: QuotaWatermark | None = None,
        reduced_interval_s: float | None = None,
        sync_handler: Callable[[JobContext, Any], Awaitable[Any]] | None = None,
    ) -> None:
        self._requirement = requirement_port
        self._watermark = watermark
        self._reduced_interval_s = reduced_interval_s
        self._sync_handler = sync_handler

    async def run(self, ctx: JobContext) -> JobResult:
        port = self._requirement or require_port(ctx, "requirement")
        wm = self._watermark or _watermark_from_config()
        reduced_interval = (
            self._reduced_interval_s
            if self._reduced_interval_s is not None
            else config.get_float("TAPD_SYNC_REDUCED_INTERVAL", 1800.0)
        )

        quota = await port.quota_watermark()
        remaining = int(quota.get("remaining", 0))
        now = datetime.now(UTC)
        if ctx.state is not None:
            await ctx.state.set(
                quota_key("tapd"), {"remaining": remaining, "at": now.isoformat()}
            )

        if remaining < wm.disabled:
            # 停跑：本轮不同步；「进行中」仅依据文档更新时间（US2-10）
            if ctx.state is not None:
                await apply_degrade(
                    ctx.state, self.name,
                    level="disabled",
                    reason=f"TAPD 额度 {remaining} 低于停用水位 {wm.disabled}，同步停跑",
                    workspace_id=ctx.workspace_id,
                )
            return JobResult(metrics={
                "degraded": "disabled",
                "remaining": remaining,
                "synced": 0,
                "in_progress_basis": "doc_updated_at",
            })

        if remaining < wm.safe:
            # 降频：距上次同步不足 reduced_interval 的轮次跳过
            if ctx.state is not None:
                await apply_degrade(
                    ctx.state, self.name,
                    level="reduced",
                    reason=f"TAPD 额度 {remaining} 低于安全水位 {wm.safe}，同步降频",
                    workspace_id=ctx.workspace_id,
                )
            last_run = None
            if ctx.state is not None:
                raw = (await ctx.state.get(LAST_RUN_KEY) or {}).get("at")
                last_run = datetime.fromisoformat(raw) if raw else None
            if last_run is not None and (now - last_run).total_seconds() < reduced_interval:
                return JobResult(metrics={
                    "degraded": "reduced",
                    "remaining": remaining,
                    "synced": 0,
                    "skipped": True,
                })
        elif ctx.state is not None:
            # 额度正常：若曾降级则自动解除（US2-10）
            await release_degrade(ctx.state, self.name, workspace_id=ctx.workspace_id)

        synced = 0
        derived: list[Any] = []
        envelopes: list[Any] = []
        handler_metrics: dict[str, Any] = {}
        if self._sync_handler is not None:
            # 兼容两种返回：int（只同步条数）或 JobResult（携带变更通知，
            # 见 capability/workflows/tapd_watch）。
            out = await self._sync_handler(ctx, port)
            if isinstance(out, JobResult):
                synced = int(out.metrics.get("synced", 0))
                derived = list(out.derived_tasks)
                envelopes = list(out.envelopes)
                handler_metrics = {
                    k: v for k, v in out.metrics.items() if k != "synced"
                }
            else:
                synced = int(out)
        if ctx.state is not None:
            await ctx.state.set(LAST_RUN_KEY, {"at": now.isoformat()})
        metrics: dict[str, Any] = {"remaining": remaining, "synced": synced}
        metrics.update(handler_metrics)
        if remaining < wm.safe:
            metrics["degraded"] = "reduced"
        return JobResult(derived_tasks=derived, envelopes=envelopes, metrics=metrics)
