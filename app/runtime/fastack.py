"""5s 回执 / 15s 原地更新进度（A-1 / A-2，plan「fastack」）。

生命周期：
1. 用户触发任务开始执行，同时起 FAST_ACK_SECONDS（默认 5s）计时
2. 任务先完成 → 直接发最终结果，不发回执（US4-1）
3. 计时到点未完成 → 发回执，记 ack_msg_id
4. PROGRESS_INTERVAL_SECONDS > 0 时每隔该秒数原地更新进度（US4-2）；默认 0 = 不发进度
5. 任务结束 → 原地更新为最终结果或失败原因（US4-5）；进度更新失败不重试（US4-4）

双通道（流式优先，推送兜底）：
- 通道一 ``stream_reply``（response_url 流式回复）：回执、进度、终态刷新同一条
  消息，体验最好；但窗口 10 分钟、帧在内存，重启即失效。
- 通道二 Outbox 主动推送：无窗口期，但每条都是新消息，``update_of`` 无等价物。
- 注入 stream_reply 后优先走通道一；窗口过期（发送侧自动降级推送）或发送失败
  时置 _stream_broken，后续一律走通道二，保证终态必达。

面向用户的文案不带 task_id（用户看不懂），task_id 只进日志。

回执/进度/终态一律 URGENT 且无 merge_key：跳过合并窗口、可透支令牌桶
预留余量，不被普通推送挤占（US4×US5 交互，TC-090）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.contracts.envelope import Envelope, MsgType, Priority
from app.contracts.errors import error_digest
from app.contracts.job_result import JobResult
from app.contracts.task import Task

logger = logging.getLogger(__name__)

FAST_ACK_SECONDS = 5.0
PROGRESS_INTERVAL_SECONDS = 0.0  # ≤0 关闭进度更新
ACK_SUMMARY_MAX_CHARS = 40

# 提交回调：返回出站消息（取其 id 作为 update_of 目标）或平台 msg_id 或 None
SubmitFn = Callable[[Envelope], Awaitable[Any]]

# 流式回复回调：(text, finish) -> 发送结果。duck-typed 取 .ok / .degraded_to_push，
# 不 import gateway 的 SendResult（§4 import 规则：Runtime 不认识 Channel 形态）
StreamReplyFn = Callable[[str, bool], Awaitable[Any]]


def _msg_id_of(submitted: Any) -> str | None:
    """从提交返回值提取消息 id：OutboxMessage 取 .id，字符串直用，None 即未知。"""
    if submitted is None:
        return None
    if isinstance(submitted, str):
        return submitted
    return str(getattr(submitted, "id", submitted))


def _ack_text(task: Task) -> str:
    """受理回执：附上用户问题摘要（原文压成一行并截断），让用户知道收到的是哪件事。"""
    from app.runtime.worker.agent_runner import extract_user_text

    summary = " ".join(extract_user_text(task).split())
    if not summary:
        return "📨 已受理，正在处理"
    if len(summary) > ACK_SUMMARY_MAX_CHARS:
        summary = summary[:ACK_SUMMARY_MAX_CHARS] + "…"
    return f"📨 已受理「{summary}」，正在处理"


def _failure_text(error: BaseException, will_retry: bool) -> str:
    reason = error_digest(error)
    if will_retry:
        return f"⚠️ 处理遇到问题，将自动重试\n原因：{reason}"
    return f"❌ 任务失败\n原因：{reason}"


def render_failure(
    task: Task, error: BaseException, ack_msg_id: str, *, will_retry: bool = False
) -> Envelope:
    """失败终态/中途的原地更新（US4-5）：明确说明与原因，不停留在「处理中」。"""
    logger.info("任务失败回执 task_id=%s will_retry=%s", task.id, will_retry)
    text = _failure_text(error, will_retry)
    return Envelope(
        chat_id=task.chat_id or "",
        topic_id=task.topic_id,
        msg_type=MsgType.MARKDOWN,
        priority=Priority.URGENT,
        update_of=ack_msg_id,
        task_id=task.id,
        payload={"text": text},
    )


class FastAck:
    """单个用户任务的回执生命周期：包住 handler 协程，按节奏发回执与进度。"""

    def __init__(
        self,
        task: Task,
        submit: SubmitFn,
        *,
        ack_seconds: float = FAST_ACK_SECONDS,
        progress_interval: float = PROGRESS_INTERVAL_SECONDS,
        stream_reply: StreamReplyFn | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._task = task
        self._submit = submit
        self.ack_seconds = ack_seconds
        self.progress_interval = progress_interval
        self._stream_reply = stream_reply
        self._clock = clock
        self.ack_msg_id: str | None = None
        self._stream_active = False  # 回执已走上流式通道，终态可原地刷新
        self._stream_broken = False  # 窗口过期/发送失败，后续只走 Outbox
        self._ticker: asyncio.Task | None = None
        self._started_at: float | None = None

    async def run(self, coro: Awaitable[JobResult]) -> JobResult:
        """执行 handler 并按节奏回执。成功时把终态原地更新到回执消息；异常取消计时后原样上抛。"""
        self._started_at = self._clock()
        self._ticker = asyncio.create_task(self._progress_ticker())
        try:
            result = await coro
        except BaseException:
            await self._cancel_ticker()
            raise
        await self._cancel_ticker()
        await self.finalize(result)
        return result

    async def finalize(
        self,
        result: JobResult | None = None,
        *,
        error: BaseException | None = None,
        will_retry: bool = False,
        note: str | None = None,
    ) -> None:
        """任务终止：已发回执则原地更新为终态；未发回执则不动（短任务直接发结果，US4-1）。

        - error：更新为失败说明（will_retry 区分中途重试与最终终止）
        - note：中性终态说明（如等待审批）
        - result.envelopes 非空：盖上 update_of/URGENT 由调用方随正常流程提交
        - 以上皆无：补一条通用「已完成」更新
        """
        if self.ack_msg_id is None:
            return
        if self._stream_active:
            await self._finalize_stream(result, error=error, will_retry=will_retry, note=note)
            return
        if error is not None:
            await self._submit_safe(
                render_failure(self._task, error, self.ack_msg_id, will_retry=will_retry)
            )
            return
        if note is not None:
            await self._submit_safe(self._update_envelope(f"⏸ {note}"))
            return
        if result and result.envelopes:
            for envelope in result.envelopes:
                envelope.update_of = self.ack_msg_id
                envelope.priority = Priority.URGENT
                envelope.merge_key = None  # 原地更新必须直通，不进合并窗口
            return
        await self._submit_safe(self._update_envelope("✅ 已完成"))

    # ---- 内部 ----

    async def _finalize_stream(
        self,
        result: JobResult | None,
        *,
        error: BaseException | None,
        will_retry: bool,
        note: str | None,
    ) -> None:
        """终态走流式通道：刷新同一条消息并 finish；发送失败降级 Outbox 保证必达。"""
        if error is not None:
            res = await self._stream_safe(_failure_text(error, will_retry), finish=True)
            if res is None or not getattr(res, "ok", False):
                await self._submit_safe(
                    render_failure(self._task, error, self.ack_msg_id, will_retry=will_retry)
                )
            return
        if note is not None:
            await self._stream_safe(f"⏸ {note}", finish=True)
            return
        if result and result.envelopes:
            remaining: list[Envelope] = []
            for i, envelope in enumerate(result.envelopes):
                last = i == len(result.envelopes) - 1
                res = await self._stream_safe(
                    str(envelope.payload.get("text", "")), finish=last
                )
                if res is None or not getattr(res, "ok", False):
                    remaining = result.envelopes[i:]  # 本条未送达，从它开始降级
                elif getattr(res, "degraded_to_push", False):
                    # 窗口中途过期：本条已被发送侧降级推送送达，剩余走 Outbox
                    self._stream_broken = True
                    remaining = result.envelopes[i + 1 :]
                if remaining:
                    break
            for envelope in remaining:
                await self._submit_safe(envelope)
            result.envelopes = []  # 已全部送达，避免 _on_success 重复出站
            return
        await self._stream_safe("✅ 已完成", finish=True)

    async def _stream_safe(self, text: str, *, finish: bool) -> Any:
        """流式回复失败只记日志不重试（US4-4 同旨）；返回 None 表示未送达。"""
        try:
            return await self._stream_reply(text, finish)
        except Exception as exc:  # noqa: BLE001
            logger.warning("流式回复失败（降级处理）: %s", error_digest(exc))
            return None

    async def _progress_ticker(self) -> None:
        """到点发回执，此后每 interval 原地更新进度；回执未发出则下轮补发。"""
        await asyncio.sleep(self.ack_seconds)
        if self.progress_interval <= 0:
            await self._send_ack()
            return
        while True:
            if self.ack_msg_id is None:
                await self._send_ack()
            else:
                # US4-4：进度更新失败不重试，本次进度放弃
                elapsed = int(self._clock() - (self._started_at or self._clock()))
                text = f"⏳ 仍在处理中…（已运行 {elapsed}s）"
                if self._stream_active:
                    res = await self._stream_safe(text, finish=False)
                    if (
                        res is None
                        or not getattr(res, "ok", False)
                        or getattr(res, "degraded_to_push", False)
                    ):
                        # 流中断：后续不再刷进度（推送通道每条都是新消息，刷屏）
                        self._stream_active = False
                        self._stream_broken = True
                else:
                    await self._submit_safe(self._update_envelope(text))
            await asyncio.sleep(self.progress_interval)

    async def _send_ack(self) -> None:
        text = _ack_text(self._task)
        if self._stream_reply is not None and not self._stream_broken:
            res = await self._stream_safe(text, finish=False)
            if res is not None and getattr(res, "ok", False):
                if getattr(res, "degraded_to_push", False):
                    # 回复窗口已过期：发送侧已降级推送送达，终态走 Outbox
                    self._stream_broken = True
                    self.ack_msg_id = "push"
                else:
                    self._stream_active = True
                    self.ack_msg_id = "stream"
                logger.info(
                    "发送受理回执 task_id=%s stream=%s", self._task.id, self._stream_active
                )
                return
            self._stream_broken = True  # 发送失败，降级 Outbox
        envelope = Envelope(
            chat_id=self._task.chat_id or "",
            topic_id=self._task.topic_id,
            msg_type=MsgType.MARKDOWN,
            priority=Priority.URGENT,  # 跳过合并窗口、可透支预留余量（TC-090）
            task_id=self._task.id,
            payload={"text": text},
        )
        logger.info("发送受理回执 task_id=%s", self._task.id)
        submitted = await self._submit_safe(envelope)
        self.ack_msg_id = _msg_id_of(submitted)

    def _update_envelope(self, text: str) -> Envelope:
        return Envelope(
            chat_id=self._task.chat_id or "",
            topic_id=self._task.topic_id,
            msg_type=MsgType.MARKDOWN,
            priority=Priority.URGENT,
            update_of=self.ack_msg_id,
            task_id=self._task.id,
            payload={"text": text},
        )

    async def _submit_safe(self, envelope: Envelope) -> Any:
        """提交失败只记日志不重试（US4-4）；回执/进度不挤占重试配额。"""
        try:
            return await self._submit(envelope)
        except Exception as exc:  # noqa: BLE001
            logger.warning("回执/进度提交失败（不重试）: %s", error_digest(exc))
            return None

    async def _cancel_ticker(self) -> None:
        if self._ticker is None:
            return
        self._ticker.cancel()
        try:
            await self._ticker
        except asyncio.CancelledError:
            pass
        self._ticker = None
