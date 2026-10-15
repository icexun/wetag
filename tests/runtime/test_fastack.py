"""covers: TC-030, TC-031, TC-032, TC-033, TC-034, TC-090

TC-030 五秒内完成的用户任务直接回复结果（US4-1）
TC-031 长任务以同一消息回执、更新进度和结果（US4-2）
TC-032 长任务不阻塞同一用户后续请求（US4-3）
TC-033 进度更新失败不重试且不阻断最终结果（US4-4）
TC-034 长任务失败或超时更新明确失败说明（US4-5）
TC-090 回执不被合并窗口与普通推送限流延迟（US4-2×US5）
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

from app.contracts.envelope import Envelope, MsgType, Priority
from app.contracts.job_result import JobResult
from app.contracts.task import TaskKind, TaskSpec, TaskStatus, TriggerSource
from app.runtime.fastack import FastAck
from app.runtime.outbox.dispatcher import Dispatcher
from app.runtime.outbox.merger import Merger
from app.runtime.outbox.throttle import Throttle
from app.runtime.repository import OutboxStatus
from app.runtime.worker.pool import WorkerPool


def _spec(source=TriggerSource.MENTION, **kw) -> TaskSpec:
    kw.setdefault("kind", TaskKind.DETERMINISTIC)
    kw.setdefault("job_name", "probe")
    kw.setdefault("chat_id", "chat-1")
    return TaskSpec(trigger_source=source, **kw)


def _result_envelope(text: str = "最终结果") -> Envelope:
    return Envelope(chat_id="chat-1", msg_type=MsgType.MARKDOWN, payload={"text": text})


class SubmitRecorder:
    """submit 替身：记录 Envelope 并返回带 id 的对象；可指定第 n 次调用抛错。"""

    def __init__(self, fail_at: int | None = None) -> None:
        self.envelopes: list[Envelope] = []
        self.calls = 0
        self._fail_at = fail_at

    async def __call__(self, envelope: Envelope) -> SimpleNamespace | None:
        self.calls += 1
        if self._fail_at is not None and self.calls == self._fail_at:
            raise RuntimeError("gateway down")
        self.envelopes.append(envelope)
        return SimpleNamespace(id=f"msg-{len(self.envelopes)}")


def _pool(task_repo, handlers, submit, **kw) -> WorkerPool:
    kw.setdefault("renew_interval", 3600)
    kw.setdefault("outbox_submit", submit)
    return WorkerPool(task_repo, handlers, **kw)


class TestFastTaskNoAck:
    """TC-030: 5 秒内完成 → 仅发送最终结果，不创建额外回执消息。"""

    async def test_fast_task_sends_result_only(self, task_repo):
        recorder = SubmitRecorder()

        async def handler(ctx):
            return JobResult(envelopes=[_result_envelope()])

        pool = _pool(task_repo, {"probe": handler}, recorder, fastack_seconds=30)
        task = await task_repo.create(_spec())
        updated = await pool.execute(task)

        assert updated.status == TaskStatus.SUCCEEDED
        assert len(recorder.envelopes) == 1  # 仅最终结果
        assert recorder.envelopes[0].update_of is None
        assert recorder.envelopes[0].payload["text"] == "最终结果"


class TestLongTaskAckProgressResult:
    """TC-031: 超 5 秒 → 回执 → 15s 原地更新 → 完成时原地更新为结果，不新增消息。"""

    async def test_ack_progress_result_share_one_message(self, task_repo):
        recorder = SubmitRecorder()

        async def handler(ctx):
            await asyncio.sleep(0.9)
            return JobResult(envelopes=[_result_envelope()])

        pool = _pool(
            task_repo, {"probe": handler}, recorder,
            fastack_seconds=0.2, fastack_progress_interval=0.3,
        )
        task = await task_repo.create(_spec())
        updated = await pool.execute(task)

        assert updated.status == TaskStatus.SUCCEEDED
        envs = recorder.envelopes
        assert len(envs) >= 3  # 回执 + 至少一次进度 + 最终结果
        ack = envs[0]
        assert ack.update_of is None  # 回执是唯一的新消息
        assert ack.priority == Priority.URGENT and ack.merge_key is None
        assert "已受理" in ack.payload["text"] and str(task.id) not in ack.payload["text"]
        ack_id = "msg-1"
        for progress in envs[1:-1]:  # 进度全部原地更新到回执
            assert progress.update_of == ack_id
            assert "处理中" in progress.payload["text"]
        final = envs[-1]  # 最终结果也原地更新到同一条消息
        assert final.update_of == ack_id
        assert final.payload["text"] == "最终结果"


class TestLongTaskNotBlocking:
    """TC-032: 长任务执行期间，同一用户新 @ 被正常受理并创建独立任务。"""

    async def test_new_request_accepted_while_long_task_running(self, task_repo):
        recorder = SubmitRecorder()

        async def long_handler(ctx):
            await asyncio.sleep(0.5)
            return JobResult()

        async def quick_handler(ctx):
            return JobResult()

        pool = _pool(
            task_repo, {"long": long_handler, "quick": quick_handler}, recorder,
            fastack_seconds=0.1, fastack_progress_interval=60,
        )
        long_task = await task_repo.create(_spec(job_name="long"))
        running = asyncio.create_task(pool.execute(long_task))
        await asyncio.sleep(0.05)  # 长任务已进入执行

        # 同一用户再次 @：正常受理，创建独立任务
        new_task = await task_repo.create(_spec(job_name="quick"))
        assert new_task is not None
        assert new_task.status == TaskStatus.PENDING
        assert new_task.id != long_task.id
        assert new_task.parent_id is None

        first = await running
        assert first.status == TaskStatus.SUCCEEDED
        # 长任务结束后新任务正常执行完成（受理不被丢弃）
        second = await pool.execute(await task_repo.get(new_task.id))
        assert second.status == TaskStatus.SUCCEEDED


class TestProgressFailureNoRetry:
    """TC-033: 进度更新失败不重试，最终结果仍送达并更新回执。"""

    async def test_failed_progress_not_retried_final_delivered(self, task_repo):
        # 时间线：回执 0.1s（第 1 次提交），进度 0.3s（第 2 次，失败），任务 0.45s 完成
        recorder = SubmitRecorder(fail_at=2)

        async def handler(ctx):
            await asyncio.sleep(0.45)
            return JobResult(envelopes=[_result_envelope()])

        pool = _pool(
            task_repo, {"probe": handler}, recorder,
            fastack_seconds=0.1, fastack_progress_interval=0.2,
        )
        task = await task_repo.create(_spec())
        updated = await pool.execute(task)

        assert updated.status == TaskStatus.SUCCEEDED
        assert recorder.calls == 3  # 回执 + 进度（失败） + 最终结果；失败进度未重试
        assert len(recorder.envelopes) == 2
        ack, final = recorder.envelopes
        assert "已受理" in ack.payload["text"]
        assert final.update_of == "msg-1"  # 最终结果仍原地更新到回执
        assert final.payload["text"] == "最终结果"


class TestTerminalFailureUpdatesAck:
    """TC-034: 最终失败或超时 → 回执更新为明确失败说明及原因，不停留在「处理中」。"""

    async def test_failure_updates_ack_with_reason(self, task_repo):
        recorder = SubmitRecorder()

        async def handler(ctx):
            await asyncio.sleep(0.2)  # 越过回执点
            raise RuntimeError("boom")

        pool = _pool(
            task_repo, {"probe": handler}, recorder,
            fastack_seconds=0.1, fastack_progress_interval=60,
        )
        task = await task_repo.create(_spec(max_attempt=1))
        updated = await pool.execute(task)

        assert updated.status == TaskStatus.DEAD
        envs = recorder.envelopes
        assert len(envs) == 2  # 回执 + 失败更新
        ack, failure = envs
        assert "已受理" in ack.payload["text"]
        assert failure.update_of == "msg-1"
        text = failure.payload["text"]
        assert "失败" in text and "boom" in text  # 明确失败说明与原因
        assert "处理中" not in text

    async def test_timeout_updates_ack_with_reason(self, task_repo):
        recorder = SubmitRecorder()

        async def handler(ctx):
            await asyncio.sleep(30)
            return JobResult()

        pool = _pool(
            task_repo, {"probe": handler}, recorder,
            fastack_seconds=0.1, fastack_progress_interval=60,
            deterministic_timeout=0.3,
        )
        task = await task_repo.create(_spec(max_attempt=1))
        updated = await pool.execute(task)

        assert updated.status == TaskStatus.DEAD
        failure = recorder.envelopes[-1]
        assert failure.update_of == "msg-1"
        text = failure.payload["text"]
        assert "失败" in text and "TimeoutError" in text
        assert "处理中" not in text


class TestAckBypassesMergeAndThrottle:
    """TC-090: 回执 urgent 无 merge_key 直通：不进合并窗口、不被重排、用预留余量即时发出。"""

    async def test_ack_not_delayed_by_merge_window_and_throttle(self, task_repo, outbox_repo):
        merger = Merger(outbox_repo, window_seconds=60)
        throttle = Throttle()  # 24/min、800/h；urgent 硬限 30/min、1000/h
        sent = []

        async def sender(msg):
            sent.append(msg)
            return f"platform-{msg.id}"

        dispatcher = Dispatcher(outbox_repo, merger=merger, throttle=throttle, sender=sender)

        # 同 chat 普通推送处于合并窗口内
        normal = await dispatcher.submit(
            Envelope(
                chat_id="chat-1",
                msg_type=MsgType.MARKDOWN,
                payload={"text": "普通推送"},
                merge_key="chat-1:doc:push",
            )
        )
        assert normal.not_before > datetime.now(UTC)  # 在窗口内，未到期

        # 常规令牌被普通消息占满（24/min 之后普通须保留 urgent 余量）
        for _ in range(24):
            assert throttle.acquire("chat-1", urgent=False) is None
        assert throttle.acquire("chat-1", urgent=False) is not None  # 第 25 条普通被限流

        # 用户 @ 触发的长任务到达回执点：经 Dispatcher 发回执
        task = await task_repo.create(_spec())
        fastack = FastAck(task, dispatcher.submit, ack_seconds=0.1, progress_interval=60)

        async def work():
            await asyncio.sleep(0.2)
            return JobResult(envelopes=[_result_envelope()])

        await fastack.run(work())
        assert fastack.ack_msg_id is not None

        # 回执直通：urgent、无 merge_key、not_before 未被合并窗口重排
        ack_row = await outbox_repo.get(UUID(fastack.ack_msg_id))
        assert ack_row.priority == Priority.URGENT.value
        assert ack_row.merge_key is None
        assert ack_row.not_before <= datetime.now(UTC)

        # 派发：普通推送仍在窗口内不派发；回执用预留余量即时发出
        await dispatcher.dispatch_due()
        ack_row = await outbox_repo.get(UUID(fastack.ack_msg_id))
        assert ack_row.status == OutboxStatus.SENT
        assert ack_row.id in {m.id for m in sent}
        normal_row = await outbox_repo.get(normal.id)
        assert normal_row.status == OutboxStatus.PENDING  # 窗口未到期，未被回执影响


class TestAckUserFriendly:
    """回执面向用户：不带 task_id，附问题摘要；进度间隔 ≤0 时不发「仍在处理中」。"""

    async def test_ack_has_summary_no_task_id_and_no_progress(self, task_repo):
        recorder = SubmitRecorder()

        async def handler(ctx):
            await asyncio.sleep(0.5)
            return JobResult(envelopes=[_result_envelope()])

        pool = _pool(
            task_repo, {"probe": handler}, recorder,
            fastack_seconds=0.1, fastack_progress_interval=0,
        )
        task = await task_repo.create(
            _spec(payload={"event": {"message": {"text": "@WeTag 帮我看下提现需求有没有冲突"}}})
        )
        updated = await pool.execute(task)

        assert updated.status == TaskStatus.SUCCEEDED
        ack, final = recorder.envelopes  # 只有回执 + 最终结果，没有进度
        assert ack.payload["text"] == "📨 已受理「帮我看下提现需求有没有冲突」，正在处理"
        assert str(task.id) not in ack.payload["text"]
        assert final.payload["text"] == "最终结果"

    async def test_long_question_truncated_in_ack(self, task_repo):
        recorder = SubmitRecorder()
        long_q = "需求" * 40

        async def handler(ctx):
            await asyncio.sleep(0.3)
            return JobResult(envelopes=[_result_envelope()])

        pool = _pool(
            task_repo, {"probe": handler}, recorder,
            fastack_seconds=0.1, fastack_progress_interval=0,
        )
        task = await task_repo.create(
            _spec(payload={"event": {"message": {"text": f"@WeTag {long_q}"}}})
        )
        await pool.execute(task)

        text = recorder.envelopes[0].payload["text"]
        assert "…" in text and long_q not in text

    async def test_ack_without_user_text_falls_back(self, task_repo):
        recorder = SubmitRecorder()

        async def handler(ctx):
            await asyncio.sleep(0.3)
            return JobResult()

        pool = _pool(
            task_repo, {"probe": handler}, recorder,
            fastack_seconds=0.1, fastack_progress_interval=0,
        )
        task = await task_repo.create(_spec())
        await pool.execute(task)

        ack, done = recorder.envelopes
        assert ack.payload["text"] == "📨 已受理，正在处理"
        assert done.payload["text"] == "✅ 已完成"


class StreamRecorder:
    """reply_stream 替身（与 MessageSender.reply_stream 同签名）：记录 (content, finish)。

    degraded 模拟窗口过期（发送侧降级推送），fail 模拟发送异常。
    """

    def __init__(self, degraded: bool = False, fail: bool = False) -> None:
        self.calls: list[tuple[str, bool]] = []
        self.degraded = degraded
        self.fail = fail

    async def __call__(
        self, event_id: str, content: str, finish: bool = False, chat_id=None,
        feedback_id=None,
    ) -> SimpleNamespace:
        if self.fail:
            raise RuntimeError("ws down")
        assert event_id == "evt-1" and chat_id is not None  # 闭包绑定生效
        assert feedback_id  # 点赞/点踩回传关联任务（A-5）
        self.calls.append((content, finish))
        return SimpleNamespace(ok=True, degraded_to_push=self.degraded)


def _event_spec(**kw) -> TaskSpec:
    """带 event_id 的用户消息任务（流式回复定位回复窗口用）。"""
    kw.setdefault(
        "payload", {"event": {"event_id": "evt-1", "message": {"text": "@WeTag 查下冲突"}}}
    )
    return _spec(**kw)


class TestStreamFirstFastAck:
    """流式优先：回执/终态刷新同一条消息；窗口过期或发送失败降级 Outbox 推送。"""

    async def test_ack_and_final_via_stream(self, task_repo):
        outbox = SubmitRecorder()
        stream = StreamRecorder()

        async def handler(ctx):
            await asyncio.sleep(0.3)
            return JobResult(envelopes=[_result_envelope()])

        pool = _pool(
            task_repo, {"probe": handler}, outbox,
            fastack_seconds=0.1, fastack_progress_interval=0,
        )
        pool.bind_stream_reply(stream)
        task = await task_repo.create(_event_spec())
        updated = await pool.execute(task)

        assert updated.status == TaskStatus.SUCCEEDED
        assert outbox.envelopes == []  # 全程流式，未走推送
        assert len(stream.calls) == 2  # 回执（不 finish）+ 最终结果（finish）
        ack_call, final_call = stream.calls
        assert ack_call[1] is False and "已受理" in ack_call[0]
        assert final_call[1] is True and final_call[0] == "最终结果"

    async def test_expired_window_degrades_to_outbox(self, task_repo):
        outbox = SubmitRecorder()
        stream = StreamRecorder(degraded=True)  # 窗口过期：发送侧降级推送送达

        async def handler(ctx):
            await asyncio.sleep(0.3)
            return JobResult(envelopes=[_result_envelope()])

        pool = _pool(
            task_repo, {"probe": handler}, outbox,
            fastack_seconds=0.1, fastack_progress_interval=0,
        )
        pool.bind_stream_reply(stream)
        task = await task_repo.create(_event_spec())
        updated = await pool.execute(task)

        assert updated.status == TaskStatus.SUCCEEDED
        assert len(stream.calls) == 1  # 仅回执尝试过流式（已降级推送）
        assert len(outbox.envelopes) == 1  # 最终结果走 Outbox 推送
        assert outbox.envelopes[0].payload["text"] == "最终结果"

    async def test_stream_failure_falls_back_to_outbox(self, task_repo):
        outbox = SubmitRecorder()
        stream = StreamRecorder(fail=True)

        async def handler(ctx):
            await asyncio.sleep(0.3)
            return JobResult(envelopes=[_result_envelope()])

        pool = _pool(
            task_repo, {"probe": handler}, outbox,
            fastack_seconds=0.1, fastack_progress_interval=0,
        )
        pool.bind_stream_reply(stream)
        task = await task_repo.create(_event_spec())
        updated = await pool.execute(task)

        assert updated.status == TaskStatus.SUCCEEDED
        assert stream.calls == []  # 流式发送异常，未产生有效流
        texts = [e.payload["text"] for e in outbox.envelopes]
        assert any("已受理" in t for t in texts) and "最终结果" in texts

    async def test_no_event_id_uses_outbox_only(self, task_repo):
        outbox = SubmitRecorder()
        stream = StreamRecorder()

        async def handler(ctx):
            await asyncio.sleep(0.3)
            return JobResult(envelopes=[_result_envelope()])

        pool = _pool(
            task_repo, {"probe": handler}, outbox,
            fastack_seconds=0.1, fastack_progress_interval=0,
        )
        pool.bind_stream_reply(stream)
        task = await task_repo.create(_spec())  # 无 event 上下文
        await pool.execute(task)

        assert stream.calls == []
        assert len(outbox.envelopes) == 2  # 回执 + 最终结果均走推送
