"""审批闸门：低危写事后告知、高危写挂起/批准/拒绝、Auto Mode 直通（DB 集成）。

covers: TC-052, TC-053, TC-054
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.contracts.envelope import Priority
from app.contracts.errors import ApprovalRequired
from app.contracts.job_result import JobResult
from app.contracts.task import TaskKind, TaskSpec, TaskStatus, TriggerSource
from app.control.approval.auto_mode import AutoModeMatcher
from app.control.approval.gate import ApprovalGate
from app.control.approval.pending import ApprovalStatus, call_digest
from app.runtime.audit import AuditWriter
from app.runtime.jobs.base import JobContext
from app.runtime.worker.pool import WorkerPool


def _chunk(text: str):
    return SimpleNamespace(content=[{"type": "text", "text": text}])


def _next_handler(executed: list[str]):
    """仿 agentscope：next_handler 是协程，返回 async generator。"""

    async def _gen(**kwargs):
        executed.append(kwargs["tool_call"]["name"])
        yield _chunk("ok")

    async def _handler(**kwargs):
        return _gen(**kwargs)

    return _handler


async def _drive(gate: ApprovalGate, task, tool_call: dict, next_handler) -> list:
    token = gate.bind_task(task)
    try:
        chunks = []
        async for chunk in gate.middleware({"tool_call": tool_call}, next_handler):
            chunks.append(chunk)
        return chunks
    finally:
        gate.unbind(token)


class _Outbox:
    """出站替身：收集 Envelope，返回递增 msg_id。"""

    def __init__(self) -> None:
        self.envelopes: list = []

    async def submit(self, envelope):
        self.envelopes.append(envelope)
        return f"msg-{len(self.envelopes)}"


async def _audit_rows(pool, workspace_id: str) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT action, outcome, target, rule_version, approval_id
              FROM audit_log WHERE workspace_id = $1 ORDER BY id
            """,
            workspace_id,
        )
    return [dict(r) for r in rows]


async def _make_task(task_repo, **payload) -> object:
    return await task_repo.create(
        TaskSpec(
            job_name="agent_job",
            kind=TaskKind.AGENTIC,
            trigger_source=TriggerSource.MENTION,
            chat_id="chat-1",
            payload=payload,
        )
    )


# ---- TC-052 低危写直接执行并审计、不发群消息 (US7-2) ----


async def test_low_write_passthrough_audit_only(pool, approval_repo, task_repo):
    """covers: TC-052 —— 创建待办直通执行；不发群消息；审计可追溯。"""
    outbox = _Outbox()
    gate = ApprovalGate(
        approval_repo, audit=AuditWriter(pool), outbox_submit=outbox.submit
    )
    task = await _make_task(task_repo, actor="user-a")
    executed: list[str] = []

    chunks = await _drive(
        gate, task, {"name": "todo.create", "input": {"title": "t"}}, _next_handler(executed)
    )

    assert executed == ["todo.create"]  # 直接执行
    assert chunks[-1].content[0]["text"] == "ok"
    # 低危写不打扰群聊：无出站消息，不建审批
    assert outbox.envelopes == []
    rows = await _audit_rows(pool, task_repo.workspace_id)
    assert [(r["action"], r["outcome"], r["target"]) for r in rows] == [
        ("tool_call", "success", "todo.create")
    ]


# ---- TC-053 未自动批准的高危写须批准才执行 (US7-3) ----


async def test_high_write_suspend_approve_replay(pool, approval_repo, task_repo):
    """covers: TC-053 —— 初始调用不执行且任务 WAIT_CONFIRM；创建确认请求；
    批准后仅继续该操作。"""
    outbox = _Outbox()
    gate = ApprovalGate(
        approval_repo, audit=AuditWriter(pool), outbox_submit=outbox.submit
    )
    executed: list[str] = []
    tool_call = {"name": "doc.update", "input": {"doc_id": "d1", "content": "x"}}

    async def handler(ctx: JobContext) -> JobResult:
        await _drive(gate, ctx.task, tool_call, _next_handler(executed))
        return JobResult()

    pool_workers = WorkerPool(
        task_repo, handlers={"agent_job": handler}, renew_interval=3600
    )
    task = await _make_task(task_repo, actor="user-a")
    updated = await pool_workers.execute(task)

    # 挂起让出：任务 WAIT_CONFIRM、不计 attempt；工具未执行
    assert updated.status == TaskStatus.WAIT_CONFIRM
    assert updated.attempt == 0
    assert executed == []
    # 确认请求已创建（PENDING）且卡片以 URGENT 发出
    approval = await approval_repo.find_by_call(
        task.id, call_digest("doc.update", tool_call["input"])
    )
    assert approval is not None
    assert approval.status == ApprovalStatus.PENDING
    assert len(outbox.envelopes) == 1
    card = outbox.envelopes[0]
    assert card.priority == Priority.URGENT
    assert card.payload["card_type"] == "approval"
    assert card.payload["approval_id"] == str(approval.id)

    # 批准后重放：同一工具调用被放行且仅执行该操作
    approved = await approval_repo.approve(approval.id, "user-a")
    assert approved is not None
    chunks = await _drive(gate, task, tool_call, _next_handler(executed))
    assert executed == ["doc.update"]
    assert chunks[-1].content[0]["text"] == "ok"
    assert (await approval_repo.get(approval.id)).status == ApprovalStatus.CONSUMED
    # 审计含 approval_id 关联
    rows = await _audit_rows(pool, task_repo.workspace_id)
    assert any(
        r["action"] == "approval" and r["outcome"] == "pending" for r in rows
    )
    assert any(r["approval_id"] == approval.id for r in rows)


async def test_suspend_yields_note_before_raise(pool, approval_repo, task_repo):
    """挂起让出前先 yield 一条说明：agentscope 会把它作为 tool_result 落记忆，
    Agent 恢复执行时看到的是「等待审批」而不是空结果（空结果会被误读为接口异常）。"""
    gate = ApprovalGate(approval_repo, audit=AuditWriter(pool))
    task = await _make_task(task_repo)
    tool_call = {"name": "doc.update", "input": {"doc_id": "d7", "content": "x"}}

    chunks: list = []
    token = gate.bind_task(task)
    try:
        with pytest.raises(ApprovalRequired):
            async for chunk in gate.middleware({"tool_call": tool_call}, _next_handler([])):
                chunks.append(chunk)
    finally:
        gate.unbind(token)

    assert len(chunks) == 1
    text = chunks[0].content[0]["text"]
    assert "等待人工审批" in text and "并非接口异常" in text


async def test_high_write_rejected_informs_agent(pool, approval_repo, task_repo):
    """covers: TC-053 —— 拒绝后取消操作并把拒绝结果告知 Agent。"""
    gate = ApprovalGate(approval_repo, audit=AuditWriter(pool))
    executed: list[str] = []
    tool_call = {"name": "doc.update", "input": {"doc_id": "d2"}}
    task = await _make_task(task_repo)

    with pytest.raises(ApprovalRequired):
        await _drive(gate, task, tool_call, _next_handler(executed))

    approval = await approval_repo.find_by_call(
        task.id, call_digest("doc.update", tool_call["input"])
    )
    rejected = await approval_repo.reject(approval.id, "user-b")
    assert rejected is not None and rejected.status == ApprovalStatus.REJECTED

    chunks = await _drive(gate, task, tool_call, _next_handler(executed))
    assert executed == []  # 不执行
    assert "拒绝" in chunks[-1].content[0]["text"]  # 拒绝结果告知 Agent


async def test_high_write_tool_failure_releases_consumption(
    pool, approval_repo, task_repo
):
    """covers: TC-053 —— 批准后工具执行失败：消费占位退还、审计记 failure、
    任务重试可重新放行（不落入 CONSUMED 永久挂起）。"""
    gate = ApprovalGate(approval_repo, audit=AuditWriter(pool))
    task = await _make_task(task_repo)
    tool_call = {"name": "doc.update", "input": {"doc_id": "d9"}}
    executed: list[str] = []

    with pytest.raises(ApprovalRequired):
        await _drive(gate, task, tool_call, _next_handler(executed))
    approval = await approval_repo.find_by_call(
        task.id, call_digest("doc.update", tool_call["input"])
    )
    await approval_repo.approve(approval.id, "user-a")

    async def _failing_handler(**kwargs):
        async def _gen():
            raise RuntimeError("tool boom")
            yield  # pragma: no cover

        return _gen()

    with pytest.raises(RuntimeError):
        await _drive(gate, task, tool_call, _failing_handler)

    # 消费占位被退还，重试可重新放行；审计按真实结果记 failure
    assert (await approval_repo.get(approval.id)).status == ApprovalStatus.APPROVED
    rows = await _audit_rows(pool, task_repo.workspace_id)
    assert any(
        r["outcome"] == "failure" and r["approval_id"] == approval.id for r in rows
    )
    assert not any(
        r["outcome"] == "success" and r["approval_id"] == approval.id for r in rows
    )

    chunks = await _drive(gate, task, tool_call, _next_handler(executed))
    assert executed == ["doc.update"]  # 重试重新放行并执行
    assert chunks[-1].content[0]["text"] == "ok"
    assert (await approval_repo.get(approval.id)).status == ApprovalStatus.CONSUMED


async def test_consumed_replay_is_idempotent(pool, approval_repo, task_repo):
    """covers: TC-053 —— CONSUMED 重放：不重复执行、不挂起，返回幂等提示。"""
    gate = ApprovalGate(approval_repo, audit=AuditWriter(pool))
    task = await _make_task(task_repo)
    tool_call = {"name": "doc.update", "input": {"doc_id": "d10"}}
    executed: list[str] = []

    with pytest.raises(ApprovalRequired):
        await _drive(gate, task, tool_call, _next_handler(executed))
    approval = await approval_repo.find_by_call(
        task.id, call_digest("doc.update", tool_call["input"])
    )
    await approval_repo.approve(approval.id, "user-a")
    await _drive(gate, task, tool_call, _next_handler(executed))
    assert executed == ["doc.update"]

    chunks = await _drive(gate, task, tool_call, _next_handler(executed))
    assert executed == ["doc.update"]  # 不重复执行
    assert "不重复执行" in chunks[-1].content[0]["text"]  # 幂等提示而非挂起


# ---- TC-054 命中自动批准规则的高危写直接执行并标注版本 (US7-4) ----


async def test_auto_approved_high_write_passthrough(pool, approval_repo, task_repo):
    """covers: TC-054 —— 命中规则直接执行；不发确认请求；审计标注规则版本。"""
    outbox = _Outbox()
    matcher = AutoModeMatcher(
        [{"tool": "doc.update", "params": {"doc_id": "^doc-.*"}}],
        rule_version="v7",
        workspace_id=task_repo.workspace_id,
    )
    gate = ApprovalGate(
        approval_repo,
        auto_matcher=matcher,
        audit=AuditWriter(pool),
        outbox_submit=outbox.submit,
    )
    task = await _make_task(task_repo, actor="user-a")
    executed: list[str] = []

    chunks = await _drive(
        gate,
        task,
        {"name": "doc.update", "input": {"doc_id": "doc-9"}},
        _next_handler(executed),
    )

    assert executed == ["doc.update"]  # 直接执行
    assert chunks[-1].content[0]["text"] == "ok"
    assert outbox.envelopes == []  # 不发确认请求
    rows = await _audit_rows(pool, task_repo.workspace_id)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "success"
    assert rows[0]["rule_version"] == "v7"  # 标注命中规则版本
