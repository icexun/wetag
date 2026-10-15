"""会话态校验；SESSION_WINDOW_ENFORCE 开关。

出站四道关卡第 2 关（plan「出站四道关卡」）：
- 群从未激活（从未有人 @）→ DROPPED + 告警，保留可查，激活后不补发（US5-4）
- 已激活但超会话窗且约束开启 → SUSPENDED，等下次入站补发；
  超过保留期（默认 7 天）转 DROPPED + 告警（US5-5）
- 约束关闭 → 直接放行（US5-9）

群的「最后入站时刻」归 Space（chat_group 不在本次范围），因此以
last_inbound_at 提供者注入；返回 None 表示该群从未激活。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from app.platform.observability.alerting import AlertKind, alert
from app.runtime.repository import DropReason, OutboxMessage, OutboxRepository

# 群 -> 最后一次入站（@ 机器人）时刻；None 表示从未激活
LastInboundProvider = Callable[[str], Awaitable[datetime | None]]

DEFAULT_SESSION_WINDOW_HOURS = 48.0
DEFAULT_SUSPEND_RETENTION_DAYS = 7


class GuardVerdict(StrEnum):
    PASS = "PASS"  # 放行，进入节流
    SUSPEND = "SUSPEND"  # 挂起待补发
    DROP = "DROP"  # 永不补发


class SessionGuard:
    """会话态校验（US5-4/US5-5/US5-9）。"""

    def __init__(
        self,
        repo: OutboxRepository,
        last_inbound_at: LastInboundProvider,
        *,
        enforce: bool = True,
        window_hours: float = DEFAULT_SESSION_WINDOW_HOURS,
        retention_days: int = DEFAULT_SUSPEND_RETENTION_DAYS,
    ) -> None:
        self._repo = repo
        self._last_inbound_at = last_inbound_at
        self.enforce = enforce
        self.window = timedelta(hours=window_hours)
        self.retention = timedelta(days=retention_days)

    async def check(self, msg: OutboxMessage, *, now: datetime | None = None) -> GuardVerdict:
        now = now or datetime.now(UTC)
        last = await self._last_inbound_at(msg.chat_id)
        if last is None:
            return GuardVerdict.DROP  # 从未激活：永不补发（US5-4）
        if not self.enforce:
            return GuardVerdict.PASS  # 约束关闭：超窗照常发送（US5-9）
        if now - last > self.window:
            return GuardVerdict.SUSPEND  # 超会话窗：挂起等补发（US5-5）
        return GuardVerdict.PASS

    async def suspend(self, msg: OutboxMessage, *, now: datetime | None = None) -> OutboxMessage:
        now = now or datetime.now(UTC)
        updated = await self._repo.mark_suspended(msg.id, now)
        return updated or msg

    async def drop(
        self, msg: OutboxMessage, reason: DropReason, *, now: datetime | None = None
    ) -> OutboxMessage:
        updated = await self._repo.mark_dropped(msg.id, reason)
        if reason == DropReason.NOT_ACTIVATED:
            alert(
                AlertKind.FAILURE,
                "群未激活，出站消息已丢弃",
                "目标群从未 @ 过机器人；需群内先 @ 一次完成激活，被丢弃内容不补发",
                workspace_id=self._repo.workspace_id,
                chat_id=msg.chat_id,
                msg_id=str(msg.id),
            )
        elif reason == DropReason.RETENTION_EXPIRED:
            alert(
                AlertKind.FAILURE,
                "挂起消息超保留期，已丢弃",
                f"挂起超过 {self.retention.days} 天未补发，此后激活也不再补发",
                workspace_id=self._repo.workspace_id,
                chat_id=msg.chat_id,
                msg_id=str(msg.id),
            )
        return updated or msg

    async def resume_on_inbound(
        self, chat_id: str, *, now: datetime | None = None
    ) -> dict[str, int]:
        """群内下次入站时调用：保留期内的挂起行补发（回 PENDING），过期的转 DROPPED（US5-5）。"""
        now = now or datetime.now(UTC)
        resumed = dropped = 0
        for msg in await self._repo.suspended_for_chat(chat_id):
            suspended_at = msg.suspended_at or msg.created_at
            if now - suspended_at > self.retention:
                await self.drop(msg, DropReason.RETENTION_EXPIRED, now=now)
                dropped += 1
            else:
                await self._repo.resume(msg.id, now)
                resumed += 1
        return {"resumed": resumed, "dropped": dropped}
