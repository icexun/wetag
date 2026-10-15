"""契约对象可构造性 + 枚举取值与 data-model.md 一致性。"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.contracts import (
    ApprovalRequired,
    Envelope,
    IsolationViolation,
    JobResult,
    MsgType,
    Priority,
    SpaceContext,
    Task,
    TaskKind,
    TaskSpec,
    TaskStatus,
    TodoPort,
    TriggerSource,
)


def _ctx(**overrides) -> SpaceContext:
    base = dict(
        organization_id="org1",
        workspace_id="alpha",
        bot_id="bot-alpha",
        chat_id="chat-1",
        topic_id="topic-1",
        actor_user_id="user-1",
        effective_config_version=1,
    )
    base.update(overrides)
    return SpaceContext(**base)


class TestEnums:
    """枚举取值即 data-model.md 的 CHECK 约束集合。"""

    def test_task_kind(self):
        assert {k.value for k in TaskKind} == {"deterministic", "agentic"}

    def test_trigger_source(self):
        assert {t.value for t in TriggerSource} == {
            "mention",
            "schedule",
            "card_action",
            "derived",
        }

    def test_task_status(self):
        assert {s.value for s in TaskStatus} == {
            "PENDING",
            "RUNNING",
            "WAIT_CONFIRM",
            "SUCCEEDED",
            "FAILED",
            "DEAD",
        }

    def test_msg_type_closed_set(self):
        assert {m.value for m in MsgType} == {"markdown", "template_card"}

    def test_priority(self):
        assert {p.value for p in Priority} == {"urgent", "normal"}


class TestSpaceContext:
    def test_frozen(self):
        ctx = _ctx()
        with pytest.raises(ValidationError):
            ctx.chat_id = "other"

    def test_consistency_ok(self):
        _ctx().require_consistent(event_bot_id="bot-alpha", process_bot_id="bot-alpha")

    def test_consistency_violation(self):
        with pytest.raises(IsolationViolation):
            _ctx().require_consistent(event_bot_id="bot-beta", process_bot_id="bot-alpha")

    def test_workspace_mismatch(self):
        with pytest.raises(IsolationViolation):
            _ctx().require_workspace("beta")


class TestTask:
    def test_task_roundtrip(self):
        task = Task(
            id=uuid4(),
            workspace_id="alpha",
            job_name="doc_poll",
            kind=TaskKind.DETERMINISTIC,
            trigger_source=TriggerSource.SCHEDULE,
            scheduled_at=datetime.now(UTC),
        )
        assert task.status == TaskStatus.PENDING
        assert task.priority == 5
        assert task.attempt == 0

    def test_task_spec_defaults(self):
        spec = TaskSpec(job_name="index_refresh", kind=TaskKind.AGENTIC)
        assert spec.trigger_source == TriggerSource.DERIVED
        assert spec.max_attempt == 3

    def test_spec_from_agentic_propagates_the_initiator(self):
        """派生任务必须带走发起人：后续回复/预警/待办才知道「是谁发起的」。

        上游已穿透的 ``initiator_user_id`` 优先于本任务的 context.actor——
        派生链上 actor 会逐跳退化成系统占位，原始发起人只在 payload 里。
        """
        from app.contracts.task import AgenticTask, WorkflowKind, spec_from_agentic

        direct = AgenticTask(
            task_id="t1",
            kind=WorkflowKind.DOC_UPDATE,
            context=_ctx(actor_user_id="user_asker"),
        )
        assert spec_from_agentic(direct).payload["initiator_user_id"] == "user_asker"

        upstream = AgenticTask(
            task_id="t2",
            kind=WorkflowKind.DOC_UPDATE,
            context=_ctx(actor_user_id=None),  # 派生任务的占位 context：无 actor
            payload={"initiator_user_id": "user_original"},
        )
        spec = spec_from_agentic(upstream)
        assert spec.payload["initiator_user_id"] == "user_original"

        no_actor = AgenticTask(
            task_id="t3",
            kind=WorkflowKind.DOC_UPDATE,
            context=_ctx(actor_user_id=None),
        )
        assert spec_from_agentic(no_actor).payload["initiator_user_id"] == ""


class TestEnvelopeAndJobResult:
    def test_envelope_defaults(self):
        env = Envelope(chat_id="c1", msg_type=MsgType.MARKDOWN, payload={"text": "hi"})
        assert env.priority == Priority.NORMAL
        assert env.merge_key is None

    def test_job_result_token_cost(self):
        r = JobResult(metrics={"token_cost": 42})
        assert r.token_cost == 42
        assert JobResult().token_cost == 0


class TestErrors:
    def test_approval_required_carries_id(self):
        aid = uuid4()
        exc = ApprovalRequired(aid)
        assert exc.approval_id == aid


class _FakeTodo:
    async def resolve_user(self, display_name):
        return display_name

    async def create(self, cmd, idem_key):
        return "todo-1"

    async def get_batch(self, todo_ids):
        return [{"id": t} for t in todo_ids]


def test_port_protocol_runtime_checkable():
    assert isinstance(_FakeTodo(), TodoPort)
