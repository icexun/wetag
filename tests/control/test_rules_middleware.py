"""covers: TC-047, TC-050

TC-047 任务留痕关联当次生效规则版本 (US6-4)
TC-050 超长规则拒绝装配并回退上一版本 (US6-7)
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.control.rules.assembler import RulesAssembler, VersionedCache
from app.control.rules.middleware import RulesMiddleware
from app.contracts.task import Task, TaskKind, TaskSpec, TriggerSource
from app.platform.observability.alerting import AlertKind
from app.runtime.worker.agent_runner import AgentRunner
from app.runtime.worker.pool import WorkerPool

MAX_CHARS = 50


def _spec(**kw) -> TaskSpec:
    kw.setdefault("kind", TaskKind.AGENTIC)
    kw.setdefault("job_name", "probe")
    kw.setdefault("trigger_source", TriggerSource.MENTION)
    kw.setdefault("chat_id", "chat-x")
    return TaskSpec(**kw)


def _task(chat_id: str) -> Task:
    """中间件直测用的最小 Task。"""
    return Task(
        id=uuid4(),
        workspace_id="alpha",
        job_name="probe",
        kind=TaskKind.AGENTIC,
        trigger_source=TriggerSource.MENTION,
        chat_id=chat_id,
        scheduled_at=datetime.now(UTC),
    )


class FakeAgent:
    async def reply(self, msg):
        return type("Reply", (), {"usage": {}})()


class FakeFactory:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return FakeAgent()


def _middleware(rules_repo, max_chars: int = MAX_CHARS) -> RulesMiddleware:
    return RulesMiddleware(
        RulesAssembler(rules_repo, cache=VersionedCache()), max_chars=max_chars
    )


class TestOversizedFallback:
    """TC-050: 超长规则拒绝装配 + 告警 + 回退上一版本，不截断。"""

    async def test_oversized_rejected_and_falls_back(self, rules_repo, alerts):
        middleware = _middleware(rules_repo)

        # 先建立上一版本有效规则
        await rules_repo.save_group_rules("chat-a", "群规: 简短有效")
        good = await middleware.apply(_task("chat-a"))
        assert "群规: 简短有效" in good.prompt

        # 超长版本被拒绝：回退上一版本，告警，不截断
        await rules_repo.save_group_rules("chat-a", "群规: " + "长" * MAX_CHARS)
        fallen = await middleware.apply(_task("chat-a"))

        assert fallen.prompt == good.prompt  # 完整回退，非截断
        assert fallen.rule_version == good.rule_version
        degrade = [a for a in alerts if a.kind == AlertKind.DEGRADE]
        assert len(degrade) == 1
        assert "超长" in degrade[0].title
        assert degrade[0].fields["rejected_rule_version"] != good.rule_version
        assert degrade[0].fields["fallback_rule_version"] == good.rule_version

    async def test_oversized_without_previous_version_gives_empty(self, rules_repo, alerts):
        middleware = _middleware(rules_repo)
        await rules_repo.save_group_rules("chat-b", "群规: " + "长" * MAX_CHARS)

        result = await middleware.apply(_task("chat-b"))

        assert result.prompt == ""
        assert result.rule_version == "org:0|biz:0|group:0|topic:0"
        assert any(a.kind == AlertKind.DEGRADE for a in alerts)

    async def test_recovery_after_oversized(self, rules_repo, alerts):
        """超长版本被修正后，下一次装配恢复正常内容。"""
        middleware = _middleware(rules_repo)
        await rules_repo.save_group_rules("chat-c", "群规: 初版")
        await middleware.apply(_task("chat-c"))
        await rules_repo.save_group_rules("chat-c", "群规: " + "长" * MAX_CHARS)
        await middleware.apply(_task("chat-c"))  # 拒绝 + 回退

        await rules_repo.save_group_rules("chat-c", "群规: 修正版")
        recovered = await middleware.apply(_task("chat-c"))
        assert "群规: 修正版" in recovered.prompt


class TestRuleVersionInTaskTrace:
    """TC-047: 任务留痕可查询当次生效的规则版本指纹。"""

    async def test_rule_version_written_to_task(self, task_repo, rules_repo):
        ov = await rules_repo.save_org_rules("组织规: 留痕")
        bv = await rules_repo.save_biz_rules("业务规: 留痕")
        gv = await rules_repo.save_group_rules("chat-x", "群规: 留痕")

        factory = FakeFactory()
        runner = AgentRunner(agent_factory=factory, rules=_middleware(rules_repo, max_chars=8000))

        async def handler(ctx):
            return await runner.run(ctx.task)

        pool = WorkerPool(task_repo, {"probe": handler}, renew_interval=3600)
        task = await task_repo.create(_spec())
        updated = await pool.execute(task)

        expected = f"org:{ov}|biz:{bv}|group:{gv}|topic:0"
        assert updated.rule_version == expected
        # 留痕可查询：重新读取仍是指纹
        persisted = await task_repo.get(task.id)
        assert persisted.rule_version == expected
        # 生效规则确实拼进了 system_prompt（Rules 在前）
        prompt = factory.calls[0]["system_prompt"]
        assert "组织规: 留痕" in prompt and "群规: 留痕" in prompt
