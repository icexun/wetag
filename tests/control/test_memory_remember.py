"""T041：记忆写入与回复后自动沉淀（记忆替身运行）。

covers: TC-064, TC-066, TC-071
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.contracts.task import Task, TaskKind, TriggerSource
from app.control.memory.collections import MemoryCollections
from app.control.memory.policy.pending_review import (
    PendingMemoryRepository,
    PendingMemoryReview,
    PendingMemoryStatus,
)
from app.control.memory.remember import (
    MemoryScope,
    MemorySedimenter,
    RememberStatus,
    remember,
)
from app.runtime.worker.agent_runner import AgentRunner


def _collections() -> MemoryCollections:
    return MemoryCollections("alpha", biz_collection="wetag-alpha", org_collection="wetag-org")


def _task(**kw) -> Task:
    return Task(
        id=uuid4(),
        workspace_id="alpha",
        job_name="chat",
        kind=TaskKind.AGENTIC,
        trigger_source=TriggerSource.MENTION,
        scheduled_at=datetime.now(UTC),
        **kw,
    )


# ---- TC-064 回复后的新决策自动沉淀为群记忆 (US8-4) ----


class FakeAgent:
    """不支持实例钩子的 agent 替身：走 reply 后内联沉淀兜底。"""

    async def reply(self, msg):
        return "好的，发布窗口固定为周四"


class HookableAgent(FakeAgent):
    """支持 agentscope 实例钩子的替身：reply 内触发 post_reply 钩子。"""

    def __init__(self) -> None:
        self._hooks: dict[str, object] = {}

    def register_instance_hook(self, hook_type, hook_name, hook) -> None:
        assert hook_type == "post_reply"
        self._hooks[hook_name] = hook

    async def reply(self, msg):
        output = await super().reply(msg)
        for hook in self._hooks.values():
            await hook(self, {}, output)
        return output


async def _extractor(task, reply_text):
    return ["发布窗口固定为周四"] if "周四" in reply_text else []


class TestAutoSediment:
    async def test_post_reply_sediments_group_memory(self, memory_backend):
        """covers: TC-064 —— 回复确立新决策后自动写群级记忆，
        无需 Agent 显式调用（内联兜底路径）。"""
        sedimenter = MemorySedimenter(_collections(), memory_backend, extractor=_extractor)
        runner = AgentRunner(agent_factory=lambda **kw: FakeAgent(), memory=sedimenter)
        task = _task(chat_id="c1", topic_id="t1", payload={"input": "定一下发布窗口"})

        await runner.run(task)

        records = memory_backend.all_records("wetag-alpha")
        assert len(records) == 1
        assert records[0]["content"] == "发布窗口固定为周四"
        assert records[0]["metadata"]["layer"] == "group"
        assert records[0]["metadata"]["chat_id"] == "c1"

    async def test_post_reply_via_instance_hook(self, memory_backend):
        """covers: TC-064 —— agent 支持 register_instance_hook 时经钩子沉淀。"""
        agent = HookableAgent()
        sedimenter = MemorySedimenter(_collections(), memory_backend, extractor=_extractor)
        runner = AgentRunner(agent_factory=lambda **kw: agent, memory=sedimenter)

        await runner.run(_task(chat_id="c1", payload={"input": "定窗口"}))

        assert "wetag_memory_sediment" in agent._hooks
        assert len(memory_backend.all_records("wetag-alpha")) == 1

    async def test_no_conclusion_no_write(self, memory_backend):
        """covers: TC-064 —— 本轮未确立新决策/偏好时不写记忆。"""

        class NoDecisionAgent:
            async def reply(self, msg):
                return "你好"

        sedimenter = MemorySedimenter(_collections(), memory_backend, extractor=_extractor)
        runner = AgentRunner(agent_factory=lambda **kw: NoDecisionAgent(), memory=sedimenter)

        await runner.run(_task(chat_id="c1", payload={"input": "hi"}))
        assert memory_backend.all_records("wetag-alpha") == []

    async def test_sediment_failure_does_not_fail_task(self, memory_backend):
        """covers: TC-064/TC-069 —— 记忆服务不可用时沉淀静默降级，任务不失败。"""
        memory_backend.fail = True
        sedimenter = MemorySedimenter(_collections(), memory_backend, extractor=_extractor)
        runner = AgentRunner(agent_factory=lambda **kw: FakeAgent(), memory=sedimenter)

        result = await runner.run(_task(chat_id="c1", payload={"input": "定窗口"}))
        assert result is not None  # 任务正常完成


# ---- TC-066 组织级记忆写入只能形成提案 (US8-6) ----


class TestOrgWriteProposalOnly:
    async def test_org_scope_rejected_to_proposal(self, pool, memory_backend):
        """covers: TC-066 —— 指定 ORG 的直接写入被拒绝，仅生成 pending_memory
        组织级提案；语义记忆轨无写入。"""
        review = PendingMemoryReview(PendingMemoryRepository(pool))
        result = await remember(
            "全公司统一变更规范",
            MemoryScope.ORG,
            collections=_collections(),
            backend=memory_backend,
            chat_id="c1",
            pending_review=review,
        )

        assert result.status == RememberStatus.PROPOSAL
        assert result.pending_id is not None
        assert memory_backend.all_records("wetag-alpha") == []
        pending = await PendingMemoryRepository(pool).get(UUID(result.pending_id))
        assert pending is not None and pending.target_scope == "org"
        assert pending.status == PendingMemoryStatus.PENDING

    async def test_group_and_biz_write_directly(self, memory_backend):
        """covers: TC-066 —— 群级与高置信度业务线级按各自策略直接处理。"""
        group = await remember(
            "群 决策 周四 发布",
            MemoryScope.GROUP,
            collections=_collections(),
            backend=memory_backend,
            chat_id="c1",
        )
        biz = await remember(
            "业务线 规范 灰度 先行",
            MemoryScope.BIZ,
            collections=_collections(),
            backend=memory_backend,
            confidence=1.0,
        )

        assert group.status == RememberStatus.WRITTEN
        assert biz.status == RememberStatus.WRITTEN
        layers = {r["metadata"]["layer"] for r in memory_backend.all_records("wetag-alpha")}
        assert layers == {"group", "biz"}


# ---- TC-071 重复结论更新既有记忆而不累积副本 (US8-11) ----


async def test_duplicate_conclusion_updates_in_place(memory_backend):
    """covers: TC-071 —— 同一结论再次沉淀时更新既有记忆（同一 memory_id），
    不新增内容相同的第二条。"""
    first = await remember(
        "发布窗口固定为周四",
        MemoryScope.GROUP,
        collections=_collections(),
        backend=memory_backend,
        chat_id="c1",
    )
    second = await remember(
        "发布窗口固定为周四",
        MemoryScope.GROUP,
        collections=_collections(),
        backend=memory_backend,
        chat_id="c1",
    )

    assert first.status == RememberStatus.WRITTEN
    assert second.status == RememberStatus.UPDATED
    assert second.memory_id == first.memory_id
    assert len(memory_backend.all_records("wetag-alpha")) == 1
