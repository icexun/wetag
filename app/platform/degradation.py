"""降级开关读写（落 runtime_state），§9.3 降级矩阵的统一查询点。

开关键约定 `degrade:{name}`，值为 {"active": bool, "reason": str, "since": iso}。
状态存储以 Protocol 注入：生产为 RuntimeStateRepository（Phase 1），测试用内存替身。

FreshnessTracker：索引/快照新鲜度（US3-10 / US3-11）。
- `last_success_at:{name}` 记录最近一次成功时刻
- `stale:{name}` 为过期标记；过期时不输出「无变化」结论，结论须附快照时间
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol


class StateStore(Protocol):
    """runtime_state 的最小读写面。"""

    async def get(self, key: str) -> dict[str, Any] | None:
        ...

    async def set(self, key: str, value: dict[str, Any]) -> None:
        ...

    async def delete(self, key: str) -> None:
        ...


def degrade_key(name: str) -> str:
    return f"degrade:{name}"


def quota_key(system: str) -> str:
    """外部系统额度水位的存储键（US2-10），如 quota:tapd。"""
    return f"quota:{system}"


@dataclass(frozen=True)
class QuotaWatermark:
    """外部额度水位线：低于 safe 降频，低于 disabled 停跑（US2-10）。"""

    safe: int
    disabled: int


async def apply_degrade(
    store: StateStore,
    name: str,
    *,
    level: str,
    reason: str = "",
    workspace_id: str | None = None,
) -> dict[str, Any]:
    """激活/调整降级：状态落 runtime_state 对运维可见；级别变化时发 DEGRADE 告警。"""
    from app.platform.observability.alerting import alert, AlertKind

    existing = await store.get(degrade_key(name))
    since = (existing or {}).get("since") or datetime.now(UTC).isoformat()
    value = {"active": True, "level": level, "reason": reason, "since": since}
    await store.set(degrade_key(name), value)
    if not existing or not existing.get("active") or existing.get("level") != level:
        alert(
            AlertKind.DEGRADE,
            f"降级生效：{name}（{level}）",
            reason,
            workspace_id=workspace_id,
            level=level,
        )
    return value


async def release_degrade(
    store: StateStore,
    name: str,
    *,
    workspace_id: str | None = None,
) -> bool:
    """解除降级（额度恢复后自动解除，US2-10）；曾激活则发恢复告警并返回 True。"""
    from app.platform.observability.alerting import alert, AlertKind

    existing = await store.get(degrade_key(name))
    if not existing or not existing.get("active"):
        return False
    await store.delete(degrade_key(name))
    alert(
        AlertKind.DEGRADE,
        f"降级解除：{name}",
        "外部额度恢复，降级自动解除",
        workspace_id=workspace_id,
        resolved=True,
    )
    return True


class DegradationSwitch:
    """单个降级开关的读写；激活/解除都应伴随 DEGRADE 告警（由调用方发）。"""

    def __init__(self, store: StateStore) -> None:
        self._store = store

    async def is_active(self, name: str) -> bool:
        value = await self._store.get(degrade_key(name))
        return bool(value and value.get("active"))

    async def activate(self, name: str, reason: str = "") -> None:
        await self._store.set(
            degrade_key(name),
            {
                "active": True,
                "reason": reason,
                "since": datetime.now(UTC).isoformat(),
            },
        )

    async def release(self, name: str) -> None:
        await self._store.delete(degrade_key(name))

    async def snapshot(self, name: str) -> dict[str, Any] | None:
        """降级状态对运维可见（US2-10）：返回原始状态值。"""
        return await self._store.get(degrade_key(name))


def last_success_key(name: str) -> str:
    return f"last_success_at:{name}"


def render_capability_unavailable_card(capability: str, reason: str = "") -> dict[str, Any]:
    """能力整体不可用时群内明示的卡片载荷（US11-6）：明示「暂不可用」，不伪装已交付。"""
    return {
        "card_type": "capability_unavailable",
        "capability": capability,
        "text": f"{capability}能力暂不可用，恢复后将自动继续跟进",
        "reason": reason,
    }


def stale_key(name: str) -> str:
    return f"stale:{name}"


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value))


class FreshnessTracker:
    """索引/快照新鲜度：超过 max_age 未成功即过期（US3-10），结论须附快照时间（US3-11）。"""

    def __init__(self, store: StateStore) -> None:
        self._store = store

    async def record_success(self, name: str) -> None:
        """记录一次成功并解除过期标记（US3-10「下一次成功后解除过期」）。"""
        await self._store.set(
            last_success_key(name), {"at": datetime.now(UTC).isoformat()}
        )
        await self._store.delete(stale_key(name))

    async def mark_stale(self, name: str, reason: str = "") -> None:
        """显式标记过期；调用方应同时发 DEGRADE 告警。"""
        await self._store.set(
            stale_key(name),
            {"stale": True, "reason": reason, "since": datetime.now(UTC).isoformat()},
        )

    async def is_index_stale(self, name: str, max_age: timedelta) -> bool:
        """过期判定：有显式标记，或从未成功，或距上次成功超过 max_age。"""
        if await self._store.get(stale_key(name)):
            return True
        last = _parse_ts((await self._store.get(last_success_key(name)) or {}).get("at"))
        if last is None:
            return True
        return datetime.now(UTC) - last > max_age

    async def snapshot_age(self, name: str) -> timedelta | None:
        """距最近成功快照的时长；从未成功返回 None（US3-11 结论附快照时间用）。"""
        last = _parse_ts((await self._store.get(last_success_key(name)) or {}).get("at"))
        return None if last is None else datetime.now(UTC) - last
