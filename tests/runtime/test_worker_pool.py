"""covers: TC-002, TC-005, TC-006, TC-007, TC-020, TC-021, TC-023, TC-025, TC-091

TC-002 漏失业务线条件时终止任务并告警（US1-2）
TC-005 短暂存储不可达不使已受理任务失败（US1-5）
TC-006 持续存储不可达时拒绝新请求并重试既有任务（US1-6）
TC-007 连接池饱和超时后退避并告警（US1-7）
TC-020 失败未达上限按类别退避重试（US3-3）
TC-021 达到最大失败次数后终止且可查询原因（US3-4）
TC-023 同群串行且跨群并行执行（US3-6）
TC-025 执行超时按失败路径计次重试（US3-8）
TC-091 定时任务与用户任务成本分别归集（NFR-004）
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.contracts.errors import (
    ApprovalRequired,
    IsolationViolation,
    PoolExhausted,
    RateLimited,
    StorageUnavailable,
)
from app.contracts.job_result import JobResult
from app.contracts.task import (
    PRIORITY_USER,
    TaskKind,
    TaskSpec,
    TaskStatus,
    TriggerSource,
)
from app.platform.db.pool import create_pool
from app.platform.observability.alerting import AlertKind
from app.runtime.repository import TaskRepository
from app.runtime.worker.pool import (
    BACKOFF_BASE_SECONDS,
    RATE_LIMIT_BACKOFF_BASE_SECONDS,
    WorkerPool,
)
from app.runtime.worker.queue import TaskQueue


def _spec(source=TriggerSource.MENTION, **kw) -> TaskSpec:
    kw.setdefault("kind", TaskKind.DETERMINISTIC)
    kw.setdefault("job_name", "probe")
    return TaskSpec(trigger_source=source, **kw)


def _pool(task_repo, handlers, **kw) -> WorkerPool:
    kw.setdefault("renew_interval", 3600)  # 测试内不依赖续租节奏
    return WorkerPool(task_repo, handlers, **kw)


class TestIsolationViolation:
    """TC-002: 越权 → DEAD + SECURITY 告警 + 不进重试。"""

    async def test_isolation_violation_goes_dead_no_retry(self, task_repo, alerts):
        async def handler(ctx):
            raise IsolationViolation("疑似跨业务线越权: 缺少 workspace 过滤")

        pool = _pool(task_repo, {"probe": handler})
        task = await task_repo.create(_spec(max_attempt=3))
        updated = await pool.execute(task)

        assert updated.status == TaskStatus.DEAD
        assert updated.attempt == 0  # 禁重试：不消耗尝试次数
        security = [a for a in alerts if a.kind == AlertKind.SECURITY]
        assert len(security) == 1
        # 不再被任何机制领取
        assert await task_repo.claim(task.id, 60) is None


class TestTransientStorageFailure:
    """TC-005: 30 秒内恢复的存储故障不使任务失败。"""

    async def test_transient_storage_error_recovers(self, task_repo, alerts):
        calls = 0

        async def handler(ctx):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise StorageUnavailable("connection reset")
            return JobResult()

        pool = _pool(task_repo, {"probe": handler})
        task = await task_repo.create(_spec())

        first = await pool.execute(task)
        assert first.status == TaskStatus.PENDING  # 退回待执行而非失败
        assert first.attempt == 0  # 存储故障不是任务的错，不计次
        assert not [a for a in alerts if a.kind == AlertKind.FAILURE]

        recovered = await pool.execute(await task_repo.get(task.id))
        assert recovered.status == TaskStatus.SUCCEEDED
        final = await task_repo.get(task.id)
        assert final.status == TaskStatus.SUCCEEDED
        assert final.error is None  # 用户侧不留失败痕迹


class TestPersistentStorageOutage:
    """TC-006: 持续不可达 → 新请求显式拒绝且未入库；既有任务恢复后继续。"""

    async def test_new_request_rejected_when_storage_down(self, pg_schema, task_repo):
        # 指向不可达端口，模拟存储持续不可达；min_size=0 延迟到首次取连接才失败
        bad_dsn = "postgresql://postgres:postgres@localhost:1/wetag"
        bad_pool = await create_pool(bad_dsn, pg_schema.workspace, min_size=0, max_size=2)
        try:
            repo = TaskRepository(bad_pool)
            with pytest.raises(StorageUnavailable):
                await repo.create(_spec())
        finally:
            await bad_pool.close()
        # 未入库：正常库中查不到
        assert await task_repo.count_by_status(TaskStatus.PENDING) == 0

    async def test_existing_task_retries_after_recovery(self, task_repo):
        state = {"down": True}

        async def handler(ctx):
            if state["down"]:
                raise StorageUnavailable("server closed the connection")
            return JobResult()

        pool = _pool(task_repo, {"probe": handler})
        task = await task_repo.create(_spec())
        # 故障期：反复退避重排，不终止
        for _ in range(3):
            updated = await pool.execute(await task_repo.get(task.id))
            assert updated.status == TaskStatus.PENDING
        # 恢复后继续执行至成功
        state["down"] = False
        final = await pool.execute(await task_repo.get(task.id))
        assert final.status == TaskStatus.SUCCEEDED


class TestPoolExhausted:
    """TC-007: 池饱和 → 回 PENDING + 退避 + 告警。"""

    async def test_pool_exhausted_requeues_with_backoff(self, task_repo, alerts):
        async def handler(ctx):
            raise PoolExhausted("获取连接超过 5s")

        pool = _pool(task_repo, {"probe": handler})
        task = await task_repo.create(_spec())
        before = datetime.now(UTC)
        updated = await pool.execute(task)

        assert updated.status == TaskStatus.PENDING
        assert updated.attempt == 0
        assert updated.scheduled_at > before  # 退避后的执行时刻
        assert any("连接池饱和" in a.title for a in alerts)


class TestRetryBackoff:
    """TC-020: 普通异常 vs 限流的退避差异；TC-021: 达上限终止可查询。"""

    async def test_generic_failure_retries_with_backoff(self, task_repo):
        async def handler(ctx):
            raise RuntimeError("boom")

        pool = _pool(task_repo, {"probe": handler})
        task = await task_repo.create(_spec(max_attempt=3))
        before = datetime.now(UTC)
        updated = await pool.execute(task)

        assert updated.status == TaskStatus.PENDING
        assert updated.attempt == 1
        eta = (updated.scheduled_at - before).total_seconds()
        assert eta >= BACKOFF_BASE_SECONDS * 2 - 1  # min(60*2^1, 1800)

    async def test_rate_limited_uses_longer_backoff(self, task_repo):
        async def generic(ctx):
            raise RuntimeError("boom")

        async def limited(ctx):
            raise RateLimited("429")

        pool = _pool(task_repo, {"probe": generic, "limited": limited})
        t1 = await task_repo.create(_spec(max_attempt=3))
        t2 = await task_repo.create(_spec(job_name="limited", max_attempt=3))

        before = datetime.now(UTC)
        r1 = await pool.execute(t1)
        r2 = await pool.execute(t2)

        assert r1.attempt == 1 and r2.attempt == 1
        assert r1.status == r2.status == TaskStatus.PENDING
        # 限流类的下次执行时刻晚于普通异常
        assert r2.scheduled_at > r1.scheduled_at
        eta_rate = (r2.scheduled_at - before).total_seconds()
        assert eta_rate >= RATE_LIMIT_BACKOFF_BASE_SECONDS * 2 - 1

    async def test_dead_after_max_attempt_with_queryable_reason(self, task_repo, alerts):
        """TC-021: 最后一次失败 → DEAD + 立即告警 + 原因可查。"""
        async def handler(ctx):
            raise RuntimeError("permanent failure reason")

        pool = _pool(task_repo, {"probe": handler})
        task = await task_repo.create(_spec(max_attempt=2))

        first = await pool.execute(task)
        assert first.status == TaskStatus.PENDING and first.attempt == 1

        # 退避到期后人为领取第二次（测试不等退避）
        await task_repo.requeue(task.id, 0)
        second = await pool.execute(await task_repo.get(task.id))

        assert second.status == TaskStatus.DEAD
        assert second.attempt == 2
        assert "permanent failure reason" in second.error  # 最后错误摘要可查
        assert second.finished_at is not None
        dead_alerts = [a for a in alerts if a.kind == AlertKind.FAILURE and "终止" in a.title]
        assert len(dead_alerts) == 1
        # 终止态不再被领取
        assert await task_repo.claim(task.id, 60) is None


class TestFailureRequeue:
    """失败重试/重排落库后推回内存队列，退避到期被 worker 实际重新领取执行。"""

    async def _run_until_succeeded(self, task_repo, pool, task_id) -> None:
        run_task = asyncio.create_task(pool.run())
        try:
            for _ in range(100):
                await asyncio.sleep(0.05)
                if (await task_repo.get(task_id)).status == TaskStatus.SUCCEEDED:
                    return
            pytest.fail("退避到期后任务未被 worker 重新领取执行")
        finally:
            pool.stop()
            await run_task

    async def test_failed_task_reclaimed_via_queue(self, task_repo, monkeypatch):
        """US3-4: 普通失败退避到期后无需重启即被重新领取。"""
        monkeypatch.setattr("app.runtime.worker.pool.BACKOFF_BASE_SECONDS", 0.05)
        calls = 0

        async def handler(ctx):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("boom")
            return JobResult()

        queue = TaskQueue()
        pool = _pool(task_repo, {"probe": handler}, workers=1, queue=queue)
        task = await task_repo.create(_spec(max_attempt=3))
        queue.push(task)

        await self._run_until_succeeded(task_repo, pool, task.id)

        assert calls == 2  # 首次失败 + 到期重领成功
        final = await task_repo.get(task.id)
        assert final.attempt == 1

    async def test_requeued_task_reclaimed_via_queue(self, task_repo, monkeypatch):
        """US1-7: 池饱和重排退避到期后无需重启即被重新领取。"""
        monkeypatch.setattr("app.runtime.worker.pool.POOL_RETRY_SECONDS", 0.05)
        calls = 0

        async def handler(ctx):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise PoolExhausted("获取连接超过 5s")
            return JobResult()

        queue = TaskQueue()
        pool = _pool(task_repo, {"probe": handler}, workers=1, queue=queue)
        task = await task_repo.create(_spec(max_attempt=3))
        queue.push(task)

        await self._run_until_succeeded(task_repo, pool, task.id)

        assert calls == 2
        final = await task_repo.get(task.id)
        assert final.attempt == 0  # 重排不消耗 attempt

    @pytest.mark.parametrize("exc", [StorageUnavailable, PoolExhausted])
    async def test_claim_stage_failure_reclaimed_via_queue(
        self, task_repo, monkeypatch, exc
    ):
        """US1-6/US1-7: 领取阶段存储/池故障不丢任务，恢复后经队列自动重领。"""
        monkeypatch.setattr("app.runtime.worker.pool.STORAGE_RETRY_SECONDS", 0.05)
        monkeypatch.setattr("app.runtime.worker.pool.POOL_RETRY_SECONDS", 0.05)
        state = {"down": True}
        real_claim = task_repo.claim

        async def flaky_claim(task_id, lease_seconds):
            if state["down"]:
                raise exc("存储暂不可用")
            return await real_claim(task_id, lease_seconds)

        monkeypatch.setattr(task_repo, "claim", flaky_claim)
        calls = 0

        async def handler(ctx):
            nonlocal calls
            calls += 1
            return JobResult()

        queue = TaskQueue()
        pool = _pool(task_repo, {"probe": handler}, workers=1, queue=queue)
        task = await task_repo.create(_spec(max_attempt=3))
        queue.push(task)

        run_task = asyncio.create_task(pool.run())
        try:
            # 故障期：claim 反复失败，任务不被执行也不丢失
            await asyncio.sleep(0.3)
            assert calls == 0
            assert (await task_repo.get(task.id)).status == TaskStatus.PENDING
            # 恢复后：无需重启即被 worker 重新领取执行
            state["down"] = False
            for _ in range(100):
                await asyncio.sleep(0.05)
                if (await task_repo.get(task.id)).status == TaskStatus.SUCCEEDED:
                    break
            else:
                pytest.fail("存储恢复后任务未被 worker 重新领取执行")
        finally:
            pool.stop()
            await run_task

        assert calls == 1
        final = await task_repo.get(task.id)
        assert final.attempt == 0  # 领取阶段故障不消耗 attempt


class TestChatSerialization:
    """TC-023: 同群串行、跨群并行。"""

    async def test_same_chat_serial_cross_chat_parallel(self, task_repo):
        events: list[str] = []
        gate = asyncio.Event()

        async def handler(ctx):
            mark = f"{ctx.task.chat_id}:{ctx.task.id.hex[:4]}"
            events.append(f"{mark}-start")
            if ctx.task.chat_id == "chat-a":
                await asyncio.wait_for(gate.wait(), timeout=2)  # 阻塞首任务
            else:
                gate.set()  # chat-b 不受阻，证明跨群并行
            events.append(f"{mark}-end")
            return JobResult()

        pool = _pool(task_repo, {"probe": handler})
        t1 = await task_repo.create(_spec(chat_id="chat-a"))
        t2 = await task_repo.create(_spec(chat_id="chat-a"))
        t3 = await task_repo.create(_spec(chat_id="chat-b"))

        await asyncio.gather(pool.execute(t1), pool.execute(t2), pool.execute(t3))

        m1 = f"chat-a:{t1.id.hex[:4]}"
        m2 = f"chat-a:{t2.id.hex[:4]}"
        # 同群两个任务不重叠：一个完整结束后另一个才开始
        i1s, i1e = events.index(f"{m1}-start"), events.index(f"{m1}-end")
        i2s, i2e = events.index(f"{m2}-start"), events.index(f"{m2}-end")
        assert i1e < i2s or i2e < i1s
        # 跨群并行：chat-b 在 chat-a 首任务结束前已启动
        mb = f"chat-b:{t3.id.hex[:4]}"
        assert events.index(f"{mb}-start") < max(i1e, i2e)


class TestTimeout:
    """TC-025: 超时按失败处理并计次；未达上限重试、达上限 DEAD。"""

    async def test_timeout_counts_attempt_and_retries(self, task_repo):
        async def handler(ctx):
            await asyncio.sleep(5)

        pool = _pool(task_repo, {"probe": handler}, deterministic_timeout=0.05)
        task = await task_repo.create(_spec(max_attempt=3))
        updated = await pool.execute(task)

        assert updated.status == TaskStatus.PENDING
        assert updated.attempt == 1
        assert "Timeout" in updated.error

    async def test_timeout_to_dead_at_max_attempt(self, task_repo, alerts):
        async def handler(ctx):
            await asyncio.sleep(5)

        pool = _pool(task_repo, {"probe": handler}, deterministic_timeout=0.05)
        task = await task_repo.create(_spec(max_attempt=1))
        updated = await pool.execute(task)

        assert updated.status == TaskStatus.DEAD
        assert updated.attempt == 1
        assert "Timeout" in updated.error
        assert any(a.kind == AlertKind.FAILURE for a in alerts)


class TestWaitConfirm:
    """ApprovalRequired → WAIT_CONFIRM：不计 attempt、不进重试（US7-3 前置）。"""

    async def test_approval_required_suspends(self, task_repo):
        async def handler(ctx):
            raise ApprovalRequired(uuid4())

        pool = _pool(task_repo, {"probe": handler})
        task = await task_repo.create(_spec(max_attempt=3))
        updated = await pool.execute(task)

        assert updated.status == TaskStatus.WAIT_CONFIRM
        assert updated.attempt == 0
        assert await task_repo.claim(task.id, 60) is None  # 不被重试领取

    async def test_resumed_from_approval_skips_fastack(self, task_repo):
        """审批通过重跑的任务不再发「已受理」回执（审批卡片已回显「已确认，执行中」）。"""

        async def submit(envelope):
            return None

        pool = _pool(task_repo, {}, outbox_submit=submit)
        task = await task_repo.create(
            _spec(chat_id="chat-1", payload={"resumed_from_approval": True})
        )
        assert pool._fastack_for(task) is None

    async def test_requeue_with_payload_patch_merges(self, task_repo):
        """requeue 的 payload_patch 合并进 payload（审批恢复标记的落库通路）。"""
        task = await task_repo.create(_spec(payload={"a": 1}))
        updated = await task_repo.requeue(
            task.id, 0, reason="审批通过", payload_patch={"resumed_from_approval": True}
        )
        assert updated.status == TaskStatus.PENDING
        assert updated.payload == {"a": 1, "resumed_from_approval": True}


class TestApprovalSettledNotice:
    """审批恢复任务的终态通知：卡片原地更新超出 5 秒窗口不可用，终态另发一条。"""

    @staticmethod
    def _collector():
        sent = []

        async def submit(envelope):
            sent.append(envelope)
            return None

        return sent, submit

    async def test_success_sends_no_completion_notice(self, task_repo):
        """审批恢复成功**不发**终态通知：Agent 的业务回复已送达，
        「✅ 执行完成：审批请求：xxx」是重复噪音（2026-09-28 用户实测反馈）。"""
        sent, submit = self._collector()

        async def handler(ctx):
            return JobResult()

        pool = _pool(task_repo, {"probe": handler}, outbox_submit=submit)
        task = await task_repo.create(
            _spec(
                chat_id="chat-1",
                payload={
                    "resumed_from_approval": True,
                    "approval_title": "审批请求：更新 TAPD 需求",
                },
            )
        )
        updated = await pool.execute(task)

        assert updated.status == TaskStatus.SUCCEEDED
        assert sent == []

    async def test_terminal_failure_sends_failure_notice(self, task_repo):
        sent, submit = self._collector()

        async def handler(ctx):
            raise RuntimeError("tapd 接口 500")

        pool = _pool(task_repo, {"probe": handler}, outbox_submit=submit)
        task = await task_repo.create(
            _spec(
                chat_id="chat-1",
                max_attempt=1,
                payload={"resumed_from_approval": True, "approval_title": "审批请求：建卡"},
            )
        )
        updated = await pool.execute(task)

        assert updated.status == TaskStatus.DEAD
        assert len(sent) == 1
        assert "❌ 执行失败" in sent[0].payload["text"]

    async def test_retryable_failure_stays_silent(self, task_repo):
        """退避重试的非终态失败不发通知：任务还没输。"""
        sent, submit = self._collector()

        async def handler(ctx):
            raise RuntimeError("抖动")

        pool = _pool(task_repo, {"probe": handler}, outbox_submit=submit)
        task = await task_repo.create(
            _spec(
                chat_id="chat-1",
                max_attempt=3,
                payload={"resumed_from_approval": True},
            )
        )
        updated = await pool.execute(task)

        assert updated.status == TaskStatus.PENDING
        assert sent == []

    async def test_plain_task_sends_nothing(self, task_repo):
        """非审批恢复任务不发终态通知。"""
        sent, submit = self._collector()

        async def handler(ctx):
            return JobResult()

        pool = _pool(task_repo, {"probe": handler}, outbox_submit=submit)
        task = await task_repo.create(_spec(chat_id="chat-1"))
        await pool.execute(task)

        assert sent == []


class TestCostAttribution:
    """TC-091: 按 kind 分别归集 token_cost；确定性工作为零。"""

    async def test_cost_aggregated_by_kind(self, task_repo, pool):
        async def det_handler(ctx):
            return JobResult()  # 确定性：零 token

        async def agentic_handler(ctx):
            return JobResult(metrics={"token_cost": 42})

        wp = _pool(task_repo, {"det": det_handler, "agent": agentic_handler})
        t_user = await task_repo.create(
            _spec(TriggerSource.MENTION, job_name="agent", kind=TaskKind.AGENTIC,
                  priority=PRIORITY_USER)
        )
        t_sched = await task_repo.create(
            _spec(TriggerSource.SCHEDULE, job_name="det", kind=TaskKind.DETERMINISTIC)
        )
        await wp.execute(t_user)
        await wp.execute(t_sched)

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT kind, count(*), coalesce(sum(token_cost), 0) AS cost "
                "FROM task WHERE workspace_id = $1 AND status = 'SUCCEEDED' "
                "GROUP BY kind",
                task_repo.workspace_id,
            )
        by_kind = {r["kind"]: r["cost"] for r in rows}
        assert by_kind["agentic"] == 42
        assert by_kind["deterministic"] == 0  # 确定性工作 token 成本为零
