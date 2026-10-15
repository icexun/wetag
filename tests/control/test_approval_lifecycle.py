"""审批生命周期：超时取消与外部写幂等核对（DB 集成）。

covers: TC-056, TC-059
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.contracts.job_result import JobResult
from app.contracts.task import TaskKind, TaskSpec, TaskStatus, TriggerSource
from app.control.approval.pending import (
    ApprovalStatus,
    check_idempotency,
    mark_idem_done,
)
from app.runtime.jobs.base import ApprovalExpiryJob, JobContext
from app.runtime.worker.pool import WorkerPool


class _Outbox:
    def __init__(self) -> None:
        self.envelopes: list = []

    async def submit(self, envelope):
        self.envelopes.append(envelope)
        return f"msg-{len(self.envelopes)}"


async def _make_task(task_repo, **payload):
    return await task_repo.create(
        TaskSpec(
            job_name="agent_job",
            kind=TaskKind.AGENTIC,
            trigger_source=TriggerSource.MENTION,
            chat_id="chat-1",
            payload=payload,
        )
    )


# ---- TC-056 审批超时终止任务且过期点击无效 (US7-6) ----


async def test_expiry_cancels_task_and_late_click_invalid(
    approval_repo, task_repo
):
    """covers: TC-056 —— 超时扫描置 EXPIRED、原卡片更新为已超时取消、
    任务以审批超时取消进 DEAD（attempt 不变、不入重试）；过期点击不生效。"""
    outbox = _Outbox()
    task = await _make_task(task_repo)
    # 任务进入等待确认（不计 attempt）
    await task_repo.claim(task.id, 60)
    waiting = await task_repo.wait_confirm(task.id)
    assert waiting.status == TaskStatus.WAIT_CONFIRM

    approval = await approval_repo.create(
        task_id=task.id,
        chat_id="chat-1",
        tool_name="doc.update",
        tool_call_digest="d-expired",
        tool_call_payload={"doc_id": "d1"},
        expires_at=datetime.now(UTC) - timedelta(hours=1),  # 已超 30 分钟窗（余量抗时钟漂移）
    )
    await approval_repo.set_card_msg_id(approval.id, "card-1")

    job = ApprovalExpiryJob(
        approvals=approval_repo, tasks=task_repo, outbox_submit=outbox.submit
    )
    result = await job.run(JobContext(task=task, workspace_id=task_repo.workspace_id))

    assert result.metrics["expired"] == 1
    assert (await approval_repo.get(approval.id)).status == ApprovalStatus.EXPIRED
    # 原确认消息被更新为「已超时取消」
    assert len(outbox.envelopes) == 1
    notice = outbox.envelopes[0]
    assert notice.update_of == "card-1"
    assert notice.payload["text"] == "已超时取消"
    # 任务终止：DEAD + 原因，attempt 不变
    dead = await task_repo.get(task.id)
    assert dead.status == TaskStatus.DEAD
    assert "审批超时取消" in dead.error
    assert dead.attempt == 0
    # 过期点击不再生效
    assert await approval_repo.approve(approval.id, "user-a") is None
    assert await approval_repo.reject(approval.id, "user-a") is None


async def test_expiry_job_skips_pending_within_window(approval_repo, task_repo):
    """covers: TC-056 —— 未到期审批不被扫描处理。"""
    task = await _make_task(task_repo)
    approval = await approval_repo.create(
        task_id=task.id,
        chat_id="chat-1",
        tool_name="doc.update",
        tool_call_digest="d-alive",
        tool_call_payload={},
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    job = ApprovalExpiryJob(approvals=approval_repo, tasks=task_repo)
    result = await job.run(JobContext(task=task, workspace_id=task_repo.workspace_id))
    assert result.metrics["expired"] == 0
    assert (await approval_repo.get(approval.id)).status == ApprovalStatus.PENDING


async def _make_expired_approval(approval_repo, task_repo, digest: str):
    task = await _make_task(task_repo)
    approval = await approval_repo.create(
        task_id=task.id,
        chat_id="chat-1",
        tool_name="doc.update",
        tool_call_digest=digest,
        tool_call_payload={"doc_id": "d1"},
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )
    await approval_repo.set_card_msg_id(approval.id, f"card-{digest}")
    return task, approval


async def test_expiry_card_failure_still_finalizes_tasks(approval_repo, task_repo):
    """covers: TC-056 —— 卡片更新失败不阻断任务终态（先 mark_dead 再发卡），
    且单条失败不跳过同批其余审批的收尾。"""

    async def failing_submit(envelope):
        raise RuntimeError("card update failed")

    task_a, approval_a = await _make_expired_approval(approval_repo, task_repo, "d-fail-a")
    task_b, approval_b = await _make_expired_approval(approval_repo, task_repo, "d-fail-b")

    job = ApprovalExpiryJob(
        approvals=approval_repo, tasks=task_repo, outbox_submit=failing_submit
    )
    result = await job.run(JobContext(task=task_a, workspace_id=task_repo.workspace_id))

    assert result.metrics["expired"] == 2
    assert result.metrics["failed"] == 2
    # 两条任务都已到达终态 DEAD，审批均为 EXPIRED
    for task, approval in ((task_a, approval_a), (task_b, approval_b)):
        dead = await task_repo.get(task.id)
        assert dead.status == TaskStatus.DEAD
        assert "审批超时取消" in dead.error
        assert (await approval_repo.get(approval.id)).status == ApprovalStatus.EXPIRED


class _FlakyTasks:
    """包装 task_repo：对指定 task_id 的 mark_dead 抛错，其余透传。"""

    def __init__(self, inner, fail_task_ids):
        self._inner = inner
        self._fail_task_ids = set(fail_task_ids)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def mark_dead(self, task_id, error):
        if task_id in self._fail_task_ids:
            raise RuntimeError("db down")
        return await self._inner.mark_dead(task_id, error)


async def test_expiry_mark_dead_failure_isolated(approval_repo, task_repo):
    """covers: TC-056 —— 单条 mark_dead 失败被隔离告警，
    不跳过同批其余审批的收尾。"""
    task_a, _ = await _make_expired_approval(approval_repo, task_repo, "d-iso-a")
    task_b, approval_b = await _make_expired_approval(approval_repo, task_repo, "d-iso-b")

    flaky = _FlakyTasks(task_repo, fail_task_ids=[task_a.id])
    job = ApprovalExpiryJob(approvals=approval_repo, tasks=flaky)
    result = await job.run(JobContext(task=task_a, workspace_id=task_repo.workspace_id))

    assert result.metrics["expired"] == 2
    assert result.metrics["failed"] == 1
    # 失败的一条未被误杀，成功的一条正常收尾
    assert (await task_repo.get(task_a.id)).status == TaskStatus.PENDING
    assert (await task_repo.get(task_b.id)).status == TaskStatus.DEAD
    assert (await approval_repo.get(approval_b.id)).status == ApprovalStatus.EXPIRED


# ---- TC-059 外部写重试先校验幂等与目标状态 (US7-9) ----


async def test_check_idempotency_local_and_remote(state_store):
    """covers: TC-059 —— 本地幂等记录或远端已存在时判定已完成，不重发。"""
    # 无记录且远端不存在 → 需要执行
    assert await check_idempotency(
        state_store, "k1", check_remote=lambda: _false()
    ) is False
    # 远端已创建 → 不重发，并回补本地记录
    assert await check_idempotency(
        state_store, "k1", check_remote=lambda: _true()
    ) is True
    assert (await state_store.get("idem:k1"))["status"] == "done"
    # 本地记录已完成 → 直接不重发（无需远端核对）
    await mark_idem_done(state_store, "k2")
    assert await check_idempotency(state_store, "k2") is True


async def _false() -> bool:
    return False


async def _true() -> bool:
    return True


async def test_worker_retry_checks_idempotency_before_rewrite(
    task_repo, state_store, fake_ports
):
    """covers: TC-059 —— 首次写结果不确定（远端已建、响应丢失），
    重试时先核对幂等记录与外部对象状态，不重复创建。"""
    port = fake_ports.requirement
    key = "req:create:d1"

    async def handler(ctx: JobContext) -> JobResult:
        done = await check_idempotency(
            ctx.state,
            key,
            check_remote=lambda: _remote_has_card(port),
        )
        if done:
            return JobResult(metrics={"skipped": True})
        await port.create_card({"title": "t"})
        raise RuntimeError("response lost")  # 写成功但结果不确定

    pool_workers = WorkerPool(
        task_repo,
        handlers={"agent_job": handler},
        state=state_store,
        renew_interval=3600,
    )
    task = await _make_task(task_repo, idempotency_key=key)

    first = await pool_workers.execute(task)
    assert first.status in (TaskStatus.PENDING, TaskStatus.FAILED)  # 失败退避
    assert len([c for c in port.calls if c[0] == "create_card"]) == 1

    second = await pool_workers.execute(await task_repo.get(task.id))
    assert second.status == TaskStatus.SUCCEEDED
    # 未重复创建：远端核对命中后跳过
    assert len([c for c in port.calls if c[0] == "create_card"]) == 1
    # 成功后幂等记录落库，后续重试直接命中本地记录
    assert (await state_store.get(f"idem:{key}"))["status"] == "done"


async def _remote_has_card(port) -> bool:
    return any(c[0] == "create_card" for c in port.calls)
