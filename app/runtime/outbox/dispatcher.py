"""四道关卡编排（§4.4）。

submit（入站侧，同步落库）：
0. 形态校验：msg_type 不属于 {markdown, template_card} → 拒绝 + 告警，
   不入库不进后续环节（US5-7）
1. 合并窗口：URGENT 或无 merge_key 直通（US5-3）；其余进 Merger

dispatch_due（派发侧，扫描 not_before 到期的 PENDING 行）：
2. 会话态校验：DROP / SUSPEND / PASS（US5-4/US5-5/US5-9）
3. 令牌桶：取不到令牌按 retry_after 重排 not_before，不丢弃（US5-6）
4. 发送与重试：1s→5s→30s 递增退避，第 4 次失败标 FAILED + 告警（US5-8）。
   ``IsolationViolation``（含载荷非法）立即终止，不重试

发送原语（gateway）以 Sender 替身注入，本次不实现真实企微发送。
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from app.contracts.envelope import Envelope, MsgType, Priority
from app.contracts.errors import IsolationViolation, error_digest
from app.platform.observability.alerting import AlertKind, alert
from app.runtime.outbox.merger import Merger
from app.runtime.outbox.session_guard import GuardVerdict, SessionGuard
from app.runtime.outbox.throttle import Throttle
from app.runtime.repository import DropReason, OutboxMessage, OutboxRepository

logger = logging.getLogger(__name__)

# 发送退避序列（秒）：第 n 次失败后等待 backoff[n-1]；达 send_max_attempt 标 FAILED
SEND_RETRY_BACKOFF_SECONDS = (1.0, 5.0, 30.0, 300.0)
DEFAULT_SEND_MAX_ATTEMPT = 4

# 发送原语：入参为 outbox 行，返回平台 msg_id；异常即发送失败
Sender = Callable[[OutboxMessage], Awaitable[str]]


def validate_msg_type(value: Any) -> MsgType:
    """形态闭集校验（US5-7）：只放行 markdown / template_card。"""
    if isinstance(value, MsgType):
        return value
    try:
        return MsgType(str(value))
    except ValueError:
        raise ValueError(f"非法出站形态: {value!r}，仅支持 markdown / template_card") from None


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Dispatcher:
    """出站编排：submit 走形态校验与合并；dispatch_due 走会话态、节流与发送。"""

    def __init__(
        self,
        repo: OutboxRepository,
        *,
        merger: Merger | None = None,
        guard: SessionGuard | None = None,
        throttle: Throttle | None = None,
        sender: Sender | None = None,
        send_max_attempt: int = DEFAULT_SEND_MAX_ATTEMPT,
    ) -> None:
        self._repo = repo
        self._merger = merger
        self._guard = guard
        self._throttle = throttle
        self._sender = sender
        self.send_max_attempt = send_max_attempt

    def bind_sender(self, sender: Sender) -> None:
        """运行期绑定真实发送原语。

        装配顺序是 Runtime（Container）先于 Channel（gateway），真实发送能力
        在 build_channel 之后才存在——bootstrap 在两者装好后调本方法接线。
        """
        self._sender = sender

    # ---- 入站侧 ----

    async def submit(self, envelope: Envelope, *, now: datetime | None = None) -> OutboxMessage | None:
        """形态校验 → 合并窗口。非法形态拒绝并告警，不入库（US5-7）。"""
        now = now or _utcnow()
        try:
            validate_msg_type(envelope.msg_type)
        except ValueError as exc:
            alert(
                AlertKind.FAILURE,
                "非法出站形态，已拒绝",
                error_digest(exc),
                workspace_id=self._repo.workspace_id,
                chat_id=envelope.chat_id,
                msg_type=repr(envelope.msg_type),
            )
            return None
        # URGENT 跳过合并窗口（US5-3）；无 merge_key 无可并对象
        if envelope.priority == Priority.URGENT or not envelope.merge_key or self._merger is None:
            return await self._repo.create(envelope, not_before=now)
        return await self._merger.merge_or_create(envelope, now=now)

    # ---- 派发侧 ----

    async def dispatch_due(
        self, *, now: datetime | None = None, limit: int = 100
    ) -> list[OutboxMessage]:
        """扫描到期 PENDING 行，逐条过会话态 → 节流 → 发送。"""
        now = now or _utcnow()
        results = []
        for msg in await self._repo.due(now, limit=limit):
            results.append(await self._process(msg, now=now))
        return results

    async def _process(self, msg: OutboxMessage, *, now: datetime) -> OutboxMessage:
        if self._guard is not None:
            verdict = await self._guard.check(msg, now=now)
            if verdict == GuardVerdict.DROP:
                return await self._guard.drop(msg, DropReason.NOT_ACTIVATED, now=now)
            if verdict == GuardVerdict.SUSPEND:
                return await self._guard.suspend(msg, now=now)
        if self._throttle is not None:
            wait = self._throttle.acquire(
                msg.chat_id, urgent=msg.priority == Priority.URGENT.value
            )
            if wait is not None:
                # 限流重排而非丢弃（US5-6）
                updated = await self._repo.reschedule(msg.id, now + timedelta(seconds=wait))
                return updated or msg
        return await self.send_with_retry(msg, now=now)

    async def send_with_retry(self, msg: OutboxMessage, *, now: datetime | None = None) -> OutboxMessage:
        """发送一次；失败按 1s→5s→30s 退避重排，第 send_max_attempt 次失败标 FAILED + 告警（US5-8）。"""
        now = now or _utcnow()
        try:
            if self._sender is None:
                raise RuntimeError("发送原语未绑定（gateway 不在本次范围）")
            sent_msg_id = await self._sender(msg)
        except IsolationViolation as exc:
            # 归属拒绝或卡片载荷非法：同一条重试不会变好
            attempt = msg.attempt + 1
            alert(
                AlertKind.FAILURE,
                "出站发送被拒绝，已终止",
                error_digest(exc),
                workspace_id=self._repo.workspace_id,
                chat_id=msg.chat_id,
                msg_id=str(msg.id),
                attempt=attempt,
            )
            updated = await self._repo.mark_failed(msg.id, error_digest(exc), attempt=attempt)
            return updated or msg
        except Exception as exc:  # noqa: BLE001 — 发送失败统一走退避/终止
            attempt = msg.attempt + 1
            if attempt >= self.send_max_attempt:
                alert(
                    AlertKind.FAILURE,
                    f"出站发送连续 {attempt} 次失败，已终止",
                    error_digest(exc),
                    workspace_id=self._repo.workspace_id,
                    chat_id=msg.chat_id,
                    msg_id=str(msg.id),
                    attempt=attempt,
                )
                updated = await self._repo.mark_failed(msg.id, error_digest(exc), attempt=attempt)
                return updated or msg
            delay = SEND_RETRY_BACKOFF_SECONDS[min(attempt - 1, len(SEND_RETRY_BACKOFF_SECONDS) - 1)]
            updated = await self._repo.reschedule(
                msg.id, now + timedelta(seconds=delay), attempt=attempt
            )
            return updated or msg
        updated = await self._repo.mark_sent(msg.id, sent_msg_id)
        return updated or msg
