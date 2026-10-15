"""pending_approval 状态迁移：条件更新抢占与重启连续（DB 集成）。

covers: TC-057, TC-058
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.control.approval.pending import ApprovalStatus, PendingApprovalRepository, call_digest
from app.platform.db.pool import create_pool


def _create_kw(task_id=None, **over):
    kw = {
        "task_id": task_id or uuid4(),
        "chat_id": "chat-1",
        "tool_name": "doc.update",
        "tool_call_digest": call_digest("doc.update", {"doc_id": "d1"}),
        "tool_call_payload": {"doc_id": "d1"},
        "expires_at": datetime.now(UTC) + timedelta(minutes=30),
    }
    kw.update(over)
    return kw


# ---- TC-057 重复批准同一请求只消费一次 (US7-7) ----


async def test_duplicate_approve_consumed_once(approval_repo):
    """covers: TC-057 —— 重复批准只生效一次；并发 consume 仅一个抢到。"""
    approval = await approval_repo.create(**_create_kw())

    approved = await approval_repo.approve(approval.id, "user-a")
    assert approved is not None and approved.status == ApprovalStatus.APPROVED
    assert approved.decided_by == "user-a"

    # 重复批准：已非 PENDING，不生效
    assert await approval_repo.approve(approval.id, "user-b") is None

    # 并发消费：仅一个完成 APPROVED → CONSUMED
    first, second = await asyncio.gather(
        approval_repo.consume(approval.id), approval_repo.consume(approval.id)
    )
    assert [r is not None for r in (first, second)].count(True) == 1
    final = await approval_repo.get(approval.id)
    assert final.status == ApprovalStatus.CONSUMED
    # 再消费无效
    assert await approval_repo.consume(approval.id) is None


async def test_same_call_suspended_once(approval_repo):
    """covers: TC-057 —— 同一任务同一调用只挂起一次（唯一索引兜底）。"""
    task_id = uuid4()
    first = await approval_repo.create(**_create_kw(task_id=task_id))
    second = await approval_repo.create(**_create_kw(task_id=task_id))
    assert first.id == second.id  # 冲突回读已存在行，不新增


# ---- TC-058 重启后未过期审批仍可继续执行 (US7-8) ----


async def test_approve_after_restart_within_expiry(pg_schema, approval_repo):
    """covers: TC-058 —— 审批状态在库；新进程（新连接池/仓储）内批准仍有效，
    剩余超时按原 expires_at 连续计算。"""
    expires_at = datetime.now(UTC) + timedelta(minutes=30)
    approval = await approval_repo.create(**_create_kw(expires_at=expires_at))

    # 模拟进程重启：新建连接池与仓储实例，状态全部来自 DB
    pool2 = await create_pool(pg_schema.admin_dsn, pg_schema.workspace, min_size=1, max_size=2)
    try:
        repo2 = PendingApprovalRepository(pool2)
        reloaded = await repo2.get(approval.id)
        assert reloaded.status == ApprovalStatus.PENDING
        assert abs((reloaded.expires_at - expires_at).total_seconds()) < 1  # 连续计算

        approved = await repo2.approve(approval.id, "user-a")
        assert approved is not None and approved.status == ApprovalStatus.APPROVED
        consumed = await repo2.consume(approval.id)
        assert consumed is not None  # 操作可继续执行
    finally:
        await pool2.close()


async def test_expired_approval_cannot_be_decided(approval_repo):
    """covers: TC-058/TC-056 —— 已过期的 PENDING 批准与拒绝均不生效。"""
    approval = await approval_repo.create(
        **_create_kw(expires_at=datetime.now(UTC) - timedelta(hours=1))
    )
    assert await approval_repo.approve(approval.id, "user-a") is None
    assert await approval_repo.reject(approval.id, "user-a") is None
    assert (await approval_repo.get(approval.id)).status == ApprovalStatus.PENDING
    # 超时扫描收口为 EXPIRED
    expired = await approval_repo.expire_due()
    assert [a.id for a in expired] == [approval.id]
    assert (await approval_repo.get(approval.id)).status == ApprovalStatus.EXPIRED
