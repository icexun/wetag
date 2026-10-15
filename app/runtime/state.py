"""runtime_state：降级开关、配额水位、Job 游标与新鲜度。

RuntimeStateRepository 实现 degradation.StateStore 协议（get/set/delete），
是 DegradationSwitch / FreshnessTracker 的生产存储面。

连续失败告警（US3-9）：record_failure_streak 达阈值告警并标红，
clear_failure_streak 在成功后解除标红。

调度轮次记录（US2-6~US2-9）：record_missed / record_skipped 把错过与跳过
写入 runtime_state 供控制台查询；多次错过合并为一个候选（coalesce 语义）。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from app.platform.db.pool import Pool
from app.platform.db.repository import BaseRepository
from app.platform.observability.alerting import alert, AlertKind

FAIL_ALERT_STREAK = 3


def fail_streak_key(job_name: str) -> str:
    return f"fail_streak:{job_name}"


def missed_key(job_name: str) -> str:
    return f"missed:{job_name}"


def skipped_key(job_name: str) -> str:
    return f"skipped:{job_name}"


def _iso(dt: datetime) -> str:
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(UTC).isoformat()


async def record_missed(
    store: Any,
    job_name: str,
    *,
    round_at: datetime,
    reason: str = "超过宽限期，记为错过",
    workspace_id: str | None = None,
) -> dict[str, Any]:
    """记录一轮「错过」：多次错过合并为一个候选（count 累加、last_round_at 取最新，US2-7）。"""
    key = missed_key(job_name)
    value = await store.get(key) or {}
    merged = {
        "count": int(value.get("count", 0)) + 1,
        "last_round_at": _iso(round_at),
        "reason": reason,
    }
    await store.set(key, merged)
    return merged


async def record_skipped(
    store: Any,
    job_name: str,
    *,
    round_at: datetime,
    reason: str = "上一轮未结束，本轮跳过",
    workspace_id: str | None = None,
) -> dict[str, Any]:
    """记录一轮「因上一轮未结束而跳过」（US2-8）；跳过的轮次不是补跑候选。"""
    key = skipped_key(job_name)
    value = await store.get(key) or {}
    merged = {
        "count": int(value.get("count", 0)) + 1,
        "last_round_at": _iso(round_at),
        "reason": reason,
    }
    await store.set(key, merged)
    return merged


class RuntimeStateRepository(BaseRepository):
    """runtime_state 键值读写；同时是 DegradationSwitch 的 StateStore。"""

    def __init__(self, pool: Pool, workspace_id: str | None = None) -> None:
        super().__init__(pool, workspace_id)

    async def get(self, key: str) -> dict[str, Any] | None:
        value = await self.fetchval(
            "SELECT state_value FROM runtime_state WHERE workspace_id = $1 AND state_key = $2",
            key,
        )
        if value is None:
            return None
        return json.loads(value) if isinstance(value, str) else dict(value)

    async def set(self, key: str, value: dict[str, Any]) -> None:
        await self.execute(
            """
            INSERT INTO runtime_state (workspace_id, state_key, state_value)
            VALUES ($1, $2, $3::jsonb)
            ON CONFLICT (workspace_id, state_key)
            DO UPDATE SET state_value = $3::jsonb, updated_at = now()
            """,
            key,
            value,
        )

    async def delete(self, key: str) -> None:
        await self.execute(
            "DELETE FROM runtime_state WHERE workspace_id = $1 AND state_key = $2",
            key,
        )


async def record_failure_streak(
    store: Any,
    job_name: str,
    *,
    threshold: int = FAIL_ALERT_STREAK,
    workspace_id: str | None = None,
    error: str | None = None,
) -> int:
    """连续失败计数 +1；达阈值时发 FAILURE 告警并置标红位（只告一次）。返回当前计数。"""
    key = fail_streak_key(job_name)
    value = await store.get(key) or {}
    streak = int(value.get("count", 0)) + 1
    alerted = bool(value.get("alerted"))
    if streak >= threshold and not alerted:
        alert(
            AlertKind.FAILURE,
            f"定时任务 {job_name} 连续 {streak} 次失败",
            error or "",
            workspace_id=workspace_id,
            job_name=job_name,
            streak=streak,
        )
        alerted = True
    await store.set(
        key,
        {"count": streak, "alerted": alerted, "last_error": error or value.get("last_error")},
    )
    return streak


async def clear_failure_streak(
    store: Any,
    job_name: str,
    *,
    workspace_id: str | None = None,
) -> bool:
    """成功后清除计数；若曾标红则发恢复告警（标红自动解除，US3-9）。返回是否曾标红。"""
    key = fail_streak_key(job_name)
    value = await store.get(key)
    if not value:
        return False
    was_alerted = bool(value.get("alerted"))
    await store.delete(key)
    if was_alerted:
        alert(
            AlertKind.FAILURE,
            f"定时任务 {job_name} 已恢复",
            "连续失败标红自动解除",
            workspace_id=workspace_id,
            job_name=job_name,
            resolved=True,
        )
    return was_alerted
