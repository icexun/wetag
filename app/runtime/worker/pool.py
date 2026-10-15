"""WorkerPool：领任务、加锁、执行、留痕、重试。

主循环分支（plan「worker 主循环」）：
- 领取阶段 PoolExhausted/StorageUnavailable → 任务仍 PENDING，按退避重排回内存队列 + 告警，不计 attempt（US1-6/US1-7）
- 成功 → SUCCEEDED，落 derived_tasks 与 envelopes
- ApprovalRequired → WAIT_CONFIRM，不计 attempt（US7-3）
- IsolationViolation → DEAD + SECURITY 告警，不重试（US1-2）
- PoolExhausted → 回 PENDING 走退避 + 告警，不计 attempt（US1-7）
- StorageUnavailable → 回 PENDING 短退避 + DEGRADE 告警，不计 attempt（US1-5/US1-6）
- RateLimited → attempt+1，退避基数放大（US3-3）
- 超时 / 其他异常 → attempt+1；未达上限回 PENDING 退避，达上限 DEAD + 告警（US3-4/US3-8）
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from app.contracts.envelope import Envelope, MsgType, Priority
from app.contracts.ids import ChatId
from app.contracts.errors import (
    ApprovalRequired,
    IsolationViolation,
    PoolExhausted,
    RateLimited,
    StorageUnavailable,
    error_digest,
)
from app.contracts.job_result import JobResult
from app.contracts.task import Task, TaskKind, TaskStatus, TriggerSource
from app.platform.concurrency.locks import KeyedLock
from app.platform.observability.alerting import alert, AlertKind
from app.runtime.fastack import FAST_ACK_SECONDS, PROGRESS_INTERVAL_SECONDS, FastAck
from app.runtime.jobs.base import Job, JobContext
from app.runtime.repository import TaskRepository
from app.runtime.state import clear_failure_streak, record_failure_streak
from app.runtime.worker.lease import LeaseKeeper
from app.runtime.worker.queue import TaskQueue

logger = logging.getLogger(__name__)

Handler = Callable[[JobContext], Awaitable[JobResult]]

# 退避参数（秒）：min(base * 2**attempt, cap)
BACKOFF_BASE_SECONDS = 60.0
BACKOFF_CAP_SECONDS = 1800.0
RATE_LIMIT_BACKOFF_BASE_SECONDS = 600.0  # 限流类退避基数放大到 10 分钟（US3-3）
STORAGE_RETRY_SECONDS = 5.0  # 存储不可达短退避（US1-5/US1-6）
POOL_RETRY_SECONDS = 10.0  # 池饱和退避（US1-7）

DEFAULT_AGENTIC_TIMEOUT = 600.0
DEFAULT_DETERMINISTIC_TIMEOUT = 300.0


class FailureClass(StrEnum):
    ISOLATION = "isolation"  # 禁重试
    WAIT_CONFIRM = "wait_confirm"  # 不计 attempt
    POOL_EXHAUSTED = "pool_exhausted"  # 不计 attempt
    STORAGE = "storage"  # 不计 attempt
    RATE_LIMITED = "rate_limited"  # 长退避
    TIMEOUT = "timeout"
    GENERIC = "generic"


class WorkerPool:
    """N 协程 worker：队列 → CAS 领取 → 同群串行 → 超时与重试分流。"""

    def __init__(
        self,
        repo: TaskRepository,
        handlers: dict[str, Job | Handler],
        *,
        queue: TaskQueue | None = None,
        state: Any = None,
        chat_locks: KeyedLock | None = None,
        outbox_submit: Callable[[Envelope], Awaitable[Any]] | None = None,
        workers: int = 4,
        lease_seconds: float = 60.0,
        renew_interval: float = 30.0,
        agentic_timeout: float = DEFAULT_AGENTIC_TIMEOUT,
        deterministic_timeout: float = DEFAULT_DETERMINISTIC_TIMEOUT,
        fail_streak_threshold: int = 3,
        process_bot_id: str | None = None,
        ports: dict[str, Any] | None = None,
        fastack_seconds: float = FAST_ACK_SECONDS,
        fastack_progress_interval: float = PROGRESS_INTERVAL_SECONDS,
    ) -> None:
        self._repo = repo
        self._handlers = handlers
        # 空 TaskQueue 因 __len__ 而为 falsy，须用 is None 判定，否则注入的共享队列被静默丢弃
        self._queue = queue if queue is not None else TaskQueue()
        self._state = state
        self._chat_locks = chat_locks or KeyedLock()
        self._outbox_submit = outbox_submit
        self._ports = ports or {}
        self.workers = workers
        self.lease_seconds = lease_seconds
        self.renew_interval = renew_interval
        self.agentic_timeout = agentic_timeout
        self.deterministic_timeout = deterministic_timeout
        self.fail_streak_threshold = fail_streak_threshold
        self.process_bot_id = process_bot_id
        self.fastack_seconds = fastack_seconds
        self.fastack_progress_interval = fastack_progress_interval
        self._stream_reply_fn: Callable[..., Awaitable[Any]] | None = None
        self._stopping = False

    def bind_stream_reply(self, fn: Callable[..., Awaitable[Any]]) -> None:
        """装配层在 Channel 建成后注入流式回复原语（``MessageSender.reply_stream``）。

        注入后 FastAck 优先走 response_url 流式通道（回执/进度/终态刷新同一条
        消息）；回复窗口过期由发送侧自动降级主动推送，FastAck 检测到降级后
        切回 Outbox 通道。
        """
        self._stream_reply_fn = fn

    # ---- 主循环 ----

    async def run(self) -> None:
        """起 N 个 worker 协程直到 stop()。每个协程顶层兜底，异常不回抛（Decision 1）。"""
        self._stopping = False
        async with asyncio.TaskGroup() as tg:
            for i in range(self.workers):
                tg.create_task(self._worker_loop(i))

    def stop(self) -> None:
        self._stopping = True

    async def _worker_loop(self, idx: int) -> None:
        while not self._stopping:
            try:
                task = await self._queue.pop(timeout=0.5)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                alert(AlertKind.FAILURE, f"worker-{idx} 取任务异常", error_digest(exc),
                      workspace_id=self._repo.workspace_id)
                continue
            try:
                await self.execute(task)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                alert(AlertKind.FAILURE, f"worker-{idx} 执行异常", error_digest(exc),
                      workspace_id=self._repo.workspace_id)

    # ---- 单任务执行 ----

    async def execute(self, task: Task) -> Task | None:
        """领取并执行一个任务，返回执行后的最新形态。"""
        try:
            claimed = await self._repo.claim(task.id, self.lease_seconds)
        except (PoolExhausted, StorageUnavailable) as exc:
            # 领取阶段存储/池故障：任务仍 PENDING，按退避重排回内存队列（US1-6/US1-7）。
            # 故障期不可再写库（requeue 会同样失败），只推内存队列；DB 仍 PENDING，
            # 重启时 restore_pending 可恢复。退避后的 scheduled_at 使 pop 等待而非
            # 忙轮询，故障期不会抽空队列。
            delay = self._backoff(0, self._classify_failure(exc))
            alert(AlertKind.DEGRADE, "任务领取失败：存储暂不可用", error_digest(exc),
                  workspace_id=self._repo.workspace_id, retry_in_s=delay)
            requeued = task.model_copy(
                update={"scheduled_at": datetime.now(UTC) + timedelta(seconds=delay)}
            )
            self._push_retry(requeued)
            return None
        if claimed is None:
            return None  # CAS 失败：被别的 worker 领走
        task = claimed

        keeper = LeaseKeeper(
            self._repo,
            lease_seconds=self.lease_seconds,
            renew_interval=self.renew_interval,
        )
        renew_task = asyncio.create_task(keeper.renew_loop(task.id))
        fastack = self._fastack_for(task)
        try:
            self._check_context(task)
            if fastack is not None:
                result = await fastack.run(self._run_with_lock(task))
            else:
                result = await self._run_with_lock(task)
        except ApprovalRequired:
            renew_task.cancel()
            updated = await self._repo.wait_confirm(task.id)
            if fastack is not None:
                await fastack.finalize(note="已挂起，等待审批确认")
            logger.info("任务 %s 挂起等待审批（不计 attempt）", task.id)
            return updated
        except Exception as exc:  # noqa: BLE001 — 分流在 _on_failure 内完成
            renew_task.cancel()
            updated = await self._on_failure(task, exc)
            if fastack is not None:
                # 终态（DEAD）更新为失败说明；退避重试则标注将自动重试（US4-5）
                terminal = updated is not None and updated.status in (
                    TaskStatus.DEAD,
                    TaskStatus.FAILED,
                )
                await fastack.finalize(error=exc, will_retry=not terminal)
            return updated
        renew_task.cancel()
        return await self._on_success(task, result)

    def _fastack_for(self, task: Task) -> FastAck | None:
        """用户触发（@ / 卡片动作）且有群上下文、出站已接线时才启用快回执（US4）。

        审批通过重跑的任务不再发回执：审批卡片已回显「已确认，执行中」，
        再发「已受理」会出现两条受理消息。
        """
        if self._outbox_submit is None or not task.chat_id:
            return None
        if task.trigger_source not in (TriggerSource.MENTION, TriggerSource.CARD_ACTION):
            return None
        if (task.payload or {}).get("resumed_from_approval"):
            return None
        return FastAck(
            task,
            self._outbox_submit,
            ack_seconds=self.fastack_seconds,
            progress_interval=self.fastack_progress_interval,
            stream_reply=self._stream_reply_for(task),
        )

    def _stream_reply_for(self, task: Task) -> Callable[[str, bool], Awaitable[Any]] | None:
        """按任务闭包绑定 event_id / chat_id；缺事件上下文时返回 None（纯推送）。"""
        if self._stream_reply_fn is None:
            return None
        event_id = str((task.payload or {}).get("event", {}).get("event_id") or "")
        if not event_id:
            return None
        fn = self._stream_reply_fn
        chat_id = ChatId(task.chat_id)
        feedback_id = str(task.id)  # 点赞/点踩事件回传关联任务（A-5）

        async def stream_reply(text: str, finish: bool) -> Any:
            return await fn(
                event_id, text, finish=finish, chat_id=chat_id, feedback_id=feedback_id
            )

        return stream_reply

    def _check_context(self, task: Task) -> None:
        """协议层校验：payload 携带 space_context 时必须与本进程业务线一致。"""
        ctx = task.payload.get("space_context")
        if not ctx:
            return
        ctx_ws = ctx.get("workspace_id")
        if ctx_ws and ctx_ws != self._repo.workspace_id:
            raise IsolationViolation(
                f"任务上下文业务线 {ctx_ws!r} 与进程 {self._repo.workspace_id!r} 不一致"
            )
        ctx_bot = ctx.get("bot_id")
        if self.process_bot_id and ctx_bot and ctx_bot != self.process_bot_id:
            raise IsolationViolation(
                f"任务上下文 bot {ctx_bot!r} 与进程 bot {self.process_bot_id!r} 不一致"
            )

    async def _run_with_lock(self, task: Task) -> JobResult:
        """同群串行（chat_locks），跨群并行（US3-6）；无群任务不加锁。"""
        if task.chat_id:
            async with self._chat_locks.hold(task.chat_id):
                return await self._run_handler(task)
        return await self._run_handler(task)

    async def _run_handler(self, task: Task) -> JobResult:
        handler = self._handlers.get(task.job_name)
        if handler is None:
            raise KeyError(f"未注册的 job: {task.job_name!r}")
        run = handler.run if isinstance(handler, Job) else handler
        timeout = self._timeout_for(task, handler)
        ctx = JobContext(
            task=task,
            workspace_id=self._repo.workspace_id,
            state=self._state,
            trace_id=task.trace_id,
            extras=dict(self._ports),
        )
        if timeout:
            return await asyncio.wait_for(run(ctx), timeout=timeout)
        return await run(ctx)

    def _timeout_for(self, task: Task, handler: Any) -> float:
        if isinstance(handler, Job) and handler.timeout_s:
            return handler.timeout_s
        if task.kind == TaskKind.AGENTIC:
            return self.agentic_timeout
        return self.deterministic_timeout

    # ---- 结果分支 ----

    async def _on_success(self, task: Task, result: JobResult) -> Task | None:
        updated = await self._repo.complete(
            task.id,
            progress=result.metrics.get("progress"),
            token_cost=result.token_cost,
            rule_version=result.metrics.get("rule_version"),
        )
        # 派生任务落库：ON CONFLICT 去重，可追溯 parent_id（US2-2/US3-2）
        for spec in result.derived_tasks:
            if spec.parent_id is None:
                spec.parent_id = task.id
            created = await self._repo.create(spec, trace_id=task.trace_id)
            if created is None:
                continue  # idem_key 命中：已存在，不重复入队
            try:
                # 运行期直接入队执行，不等进程重启恢复（US2-2 派生链路）
                self._queue.push(created)
            except Exception as exc:  # noqa: BLE001
                alert(AlertKind.FAILURE, "派生任务入队失败，待重启恢复",
                      error_digest(exc),
                      workspace_id=self._repo.workspace_id,
                      task_id=str(created.id), job_name=created.job_name)
        # 外部写成功后落幂等记录：后续重试先核对，不盲目重发（US7-9）
        idem_key = (task.payload or {}).get("idempotency_key")
        if idem_key and self._state is not None:
            from app.control.approval.pending import mark_idem_done

            await mark_idem_done(self._state, str(idem_key))
        # 待发消息交出站（Phase 3 接入 Outbox；未接线时只计数不发送）
        if self._outbox_submit:
            for envelope in result.envelopes:
                if envelope.task_id is None:
                    envelope.task_id = task.id
                await self._outbox_submit(envelope)
        if self._state and task.trigger_source == TriggerSource.SCHEDULE:
            await clear_failure_streak(
                self._state, task.job_name, workspace_id=self._repo.workspace_id
            )
        await self._notify_approval_settled(task, ok=True)
        return updated

    async def _on_failure(self, task: Task, exc: BaseException) -> Task | None:
        cls = self._classify_failure(exc)
        ws = self._repo.workspace_id
        if cls == FailureClass.ISOLATION:
            # 越权是代码缺陷：直接 DEAD + SECURITY 告警，禁重试（US1-2）
            updated = await self._repo.mark_dead(task.id, error_digest(exc))
            alert(AlertKind.SECURITY, "疑似跨业务线越权，任务终止", error_digest(exc),
                  workspace_id=ws, task_id=str(task.id), job_name=task.job_name)
            return updated
        if cls in (FailureClass.POOL_EXHAUSTED, FailureClass.STORAGE):
            # 基础设施故障不是任务的错：回 PENDING 短退避，不消耗 attempt
            delay = self._backoff(0, cls)
            updated = await self._repo.requeue(task.id, delay, reason=error_digest(exc))
            alert(
                AlertKind.DEGRADE if cls == FailureClass.STORAGE else AlertKind.FAILURE,
                "存储暂不可用，任务退避重排" if cls == FailureClass.STORAGE
                else "连接池饱和，任务退避重排",
                error_digest(exc),
                workspace_id=ws, task_id=str(task.id), retry_in_s=delay,
            )
            self._push_retry(updated)
            return updated
        # 失败路径：attempt+1，按类别退避；达上限进 DEAD + 告警（US3-3/US3-4/US3-8）
        backoff = self._backoff(task.attempt + 1, cls)
        updated = await self._repo.fail(task.id, error_digest(exc), backoff)
        self._push_retry(updated)
        if self._state and task.trigger_source == TriggerSource.SCHEDULE:
            await record_failure_streak(
                self._state, task.job_name,
                threshold=self.fail_streak_threshold,
                workspace_id=ws, error=error_digest(exc),
            )
        if updated and updated.status == TaskStatus.DEAD:
            alert(AlertKind.FAILURE,
                  f"任务 {task.job_name} 达最大失败次数（{updated.attempt}），已终止",
                  error_digest(exc),
                  workspace_id=ws, task_id=str(task.id), job_name=task.job_name,
                  attempt=updated.attempt)
        if updated and updated.status in (TaskStatus.DEAD, TaskStatus.FAILED):
            await self._notify_approval_settled(task, ok=False, error=error_digest(exc))
        return updated

    async def _notify_approval_settled(
        self, task: Task, *, ok: bool, error: str | None = None
    ) -> None:
        """审批恢复任务的终态通知——**只发失败**。

        成功不发：审批恢复后 Agent 会产出业务回复（「已更新完成 ✅…」），
        再发一条「✅ 执行完成：审批请求：xxx」是纯噪音，且标题是动作名而
        不是结果，用户真正关心的内容在 Agent 回复里。失败必须发：任务异常
        时没有业务回复，审批卡片又早已过了原地更新的 5 秒窗口，不发这条
        用户会以为还在执行。退避重试的非终态失败不发：任务还没输。
        """
        if self._outbox_submit is None or not task.chat_id:
            return
        if not (task.payload or {}).get("resumed_from_approval"):
            return
        if ok:
            return
        title = str((task.payload or {}).get("approval_title") or "审批操作")
        text = f"❌ 执行失败：{title}（{error or '未知原因'}）"
        try:
            await self._outbox_submit(
                Envelope(
                    chat_id=task.chat_id,
                    topic_id=task.topic_id,
                    msg_type=MsgType.MARKDOWN,
                    priority=Priority.URGENT,
                    payload={"text": text},
                    task_id=task.id,
                )
            )
        except Exception as notify_exc:  # noqa: BLE001 终态通知失败不拖垮任务收尾
            logger.warning("审批任务终态通知失败 task_id=%s: %s", task.id, notify_exc)

    def _push_retry(self, task: Task | None) -> None:
        """失败重试/重排落库后推回内存队列，运行期即可到期重领，不必等重启恢复。

        与 _on_success 派生入队同一兜底语义：push 失败只告警，任务仍在 DB 的
        PENDING 中，重启时 restore_pending 可恢复；DEAD 等终态不入队。
        """
        if task is None or task.status != TaskStatus.PENDING:
            return
        try:
            self._queue.push(task)
        except Exception as exc:  # noqa: BLE001
            alert(AlertKind.FAILURE, "重试任务入队失败，待重启恢复",
                  error_digest(exc),
                  workspace_id=self._repo.workspace_id,
                  task_id=str(task.id), job_name=task.job_name)

    # ---- 分类与退避 ----

    def _classify_failure(self, exc: BaseException) -> FailureClass:
        if isinstance(exc, IsolationViolation):
            return FailureClass.ISOLATION
        if isinstance(exc, ApprovalRequired):
            return FailureClass.WAIT_CONFIRM
        if isinstance(exc, PoolExhausted):
            return FailureClass.POOL_EXHAUSTED
        if isinstance(exc, StorageUnavailable):
            return FailureClass.STORAGE
        if isinstance(exc, RateLimited):
            return FailureClass.RATE_LIMITED
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            return FailureClass.TIMEOUT
        return FailureClass.GENERIC

    def _backoff(self, attempt: int, cls: FailureClass) -> float:
        """退避秒数：普通 min(60*2^attempt, 1800)；限流基数 10 分钟（US3-3）。"""
        if cls == FailureClass.STORAGE:
            return STORAGE_RETRY_SECONDS
        if cls == FailureClass.POOL_EXHAUSTED:
            return POOL_RETRY_SECONDS
        base = (
            RATE_LIMIT_BACKOFF_BASE_SECONDS
            if cls == FailureClass.RATE_LIMITED
            else BACKOFF_BASE_SECONDS
        )
        return min(base * (2 ** attempt), BACKOFF_CAP_SECONDS)
