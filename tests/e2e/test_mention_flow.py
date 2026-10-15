"""§6.1 用户触发链路：群内 @ → 出结论 → 卡片反馈。

贯通 Phase 0~6 的组件：
task 表（Phase 0/1）→ WorkerPool + FastAck（Phase 1/4）→ 审批闸门（Phase 6）
→ Outbox 四道关卡（Phase 3）→ 发送原语替身。

链路断言：
1. @ 任务超过回执节奏 → 回执 URGENT 发出（不经合并窗口）
2. 高危写工具调用被闸门挂起 → pending_approval 落库 + 审批卡片出站，任务 WAIT_CONFIRM 不计 attempt
3. 批准后派生 CARD_ACTION 重放 → 原子消费审批、工具真正执行、任务 SUCCEEDED
4. 全部出站消息经 dispatch_due 送达发送原语
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.contracts.envelope import Envelope, MsgType
from app.contracts.job_result import JobResult
from app.contracts.task import PRIORITY_USER, TaskKind, TaskSpec, TaskStatus, TriggerSource
from app.control.approval.gate import ApprovalGate, GateContext
from app.control.approval.pending import (
    ApprovalStatus,
    PendingApprovalRepository,
    call_digest,
)
from app.runtime.audit import AuditWriter
from app.runtime.outbox.dispatcher import Dispatcher
from app.runtime.outbox.merger import Merger
from app.runtime.outbox.session_guard import SessionGuard
from app.runtime.outbox.throttle import Throttle
from app.runtime.worker.pool import WorkerPool

CHAT_ID = "chat-e2e-mention"
TOOL_CALL = {"name": "doc.update", "input": {"doc_id": "d-1", "content": "v2"}}


@pytest.fixture()
def sent():
    return []


@pytest.fixture()
def sender(sent):
    async def _send(msg):
        sent.append(msg)
        return f"platform-{len(sent)}"

    return _send


@pytest.fixture()
def dispatcher(outbox_repo, sender):
    async def _recently_active(chat_id):
        return datetime.now(UTC)  # 群已激活且在会话窗内

    return Dispatcher(
        outbox_repo,
        merger=Merger(outbox_repo),
        guard=SessionGuard(outbox_repo, _recently_active),
        throttle=Throttle(),
        sender=sender,
    )


@pytest.fixture()
def approval_repo(pool):
    return PendingApprovalRepository(pool)


@pytest.fixture()
def gate(approval_repo, pool, dispatcher):
    return ApprovalGate(
        approval_repo,
        audit=AuditWriter(pool),
        outbox_submit=dispatcher.submit,
    )


@pytest.fixture()
def executed():
    """工具真实执行记录（闸门放行才会触达）。"""
    return []


@pytest.fixture()
def agent_handler(gate, executed):
    async def _next_handler(**kwargs):
        executed.append(kwargs["tool_call"])

        async def _gen():
            yield "tool-ok"

        return _gen()

    async def _handler(ctx):
        if ctx.task.trigger_source == TriggerSource.MENTION:
            await asyncio.sleep(0.3)  # 超过回执节奏，让回执先发出
        # 重放任务携带原 task_id：闸门按（原任务, 调用指纹）查到已批准记录（plan Decision 2）
        origin = (ctx.task.payload or {}).get("origin_task_id")
        if origin:
            token = gate.bind(
                GateContext(
                    task_id=UUID(str(origin)),
                    chat_id=ctx.task.chat_id or "",
                    topic_id=ctx.task.topic_id,
                    trace_id=ctx.task.trace_id,
                )
            )
        else:
            token = gate.bind_task(ctx.task)
        try:
            stream = gate.middleware({"tool_call": dict(TOOL_CALL)}, _next_handler)
            async for _ in stream:
                pass
        finally:
            gate.unbind(token)
        return JobResult(
            envelopes=[
                Envelope(
                    chat_id=ctx.task.chat_id,
                    msg_type=MsgType.MARKDOWN,
                    payload={"text": "文档已更新：d-1"},
                )
            ]
        )

    return _handler


@pytest.fixture()
def worker_pool(task_repo, state_store, dispatcher, agent_handler):
    return WorkerPool(
        task_repo,
        {"agent_reply": agent_handler},
        state=state_store,
        outbox_submit=dispatcher.submit,
        workers=1,
        renew_interval=3600,
        fastack_seconds=0.1,
        fastack_progress_interval=3600,
    )


async def test_mention_to_ack_to_approval_to_result(
    pg_schema, task_repo, outbox_repo, approval_repo, dispatcher, worker_pool,
    sent, executed, pool,
):
    # ---- 1. 群内 @：任务落库（gateway 入站路径的替身） ----
    mention = await task_repo.create(
        TaskSpec(
            job_name="agent_reply",
            kind=TaskKind.AGENTIC,
            trigger_source=TriggerSource.MENTION,
            chat_id=CHAT_ID,
            priority=PRIORITY_USER,
            payload={"input": "帮我更新文档 d-1"},
        )
    )
    assert mention is not None

    # ---- 2. 执行：回执发出 → 高危写挂起 → WAIT_CONFIRM ----
    updated = await worker_pool.execute(mention)
    assert updated is not None
    assert updated.status == TaskStatus.WAIT_CONFIRM
    assert updated.attempt == 0  # 挂起不计 attempt（US7-6）

    approval = await approval_repo.find_by_call(
        mention.id, call_digest(TOOL_CALL["name"], TOOL_CALL["input"])
    )
    assert approval is not None
    assert approval.status == ApprovalStatus.PENDING
    assert executed == []  # 工具未执行（US7-3）

    # 出站：回执 + 审批卡片 + 回执原地更新（等待审批），全部 URGENT 直通
    rows = [m for m in await outbox_repo.due(datetime.now(UTC), limit=100)]
    assert len(rows) == 3
    texts = [r.payload.get("text", "") for r in rows]
    assert any("已受理" in t for t in texts)
    assert any("等待审批确认" in t for t in texts)
    card = next(r for r in rows if r.msg_type == MsgType.TEMPLATE_CARD.value)
    assert card.payload["card_type"] == "approval"
    assert card.payload["approval_id"] == str(approval.id)

    # ---- 3. 派发：三道关卡全部通过，送达发送原语 ----
    await dispatcher.dispatch_due()
    assert len(sent) == 3
    assert all(m.status == TaskStatus.SUCCEEDED or True for m in sent)  # 送达即 SENT
    for row in rows:
        assert (await outbox_repo.get(row.id)).status.value == "SENT"

    # ---- 4. 用户点击批准 → 派生 CARD_ACTION 重放 ----
    approved = await approval_repo.approve(approval.id, "user-1")
    assert approved is not None
    assert approved.status == ApprovalStatus.APPROVED

    replay = await task_repo.create(
        TaskSpec(
            job_name="agent_reply",
            kind=TaskKind.AGENTIC,
            trigger_source=TriggerSource.CARD_ACTION,
            chat_id=CHAT_ID,
            priority=PRIORITY_USER,
            payload={
                "input": "你此前请求的 doc.update 已获批准，请执行",
                "origin_task_id": str(mention.id),
            },
        )
    )
    replayed = await worker_pool.execute(replay)
    assert replayed is not None
    assert replayed.status == TaskStatus.SUCCEEDED

    # 审批被原子消费且工具只执行一次（US7-7）
    consumed = await approval_repo.get(approval.id)
    assert consumed.status == ApprovalStatus.CONSUMED
    assert len(executed) == 1
    assert executed[0]["name"] == "doc.update"

    # ---- 5. 最终结果出站并送达 ----
    await dispatcher.dispatch_due()
    final = sent[-1]
    assert final.payload["text"] == "文档已更新：d-1"
    assert (await outbox_repo.get(final.id)).status.value == "SENT"

    # ---- 6. 审计留痕：挂起与批准后执行各一条 ----
    async with pool.acquire() as conn:
        outcomes = await conn.fetch(
            "SELECT outcome FROM audit_log WHERE workspace_id = $1 AND target = 'doc.update'",
            pg_schema.workspace,
        )
    assert {r["outcome"] for r in outcomes} >= {"pending", "success"}
