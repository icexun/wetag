"""T018：AgentRunner —— Task → 一次 AgentScope 执行，thread_id 命名空间隔离。

本地无内网包与模型凭据：agent 以替身注入，验证编排语义（thread_id、
entry_span no-op 降级、token_cost 归集），真实 AgentScope 链路在联调环境验证。
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.contracts.task import Task, TaskKind, TriggerSource
from app.runtime.worker.agent_runner import AgentRunner, _reply_envelopes, build_thread_id


def _task(source: TriggerSource, **kw) -> Task:
    return Task(
        id=uuid4(),
        workspace_id="alpha",
        job_name="probe",
        kind=TaskKind.AGENTIC,
        trigger_source=source,
        scheduled_at=datetime.now(UTC),
        **kw,
    )


class FakeAgent:
    def __init__(self, reply_usage: dict | None = None) -> None:
        self.reply_usage = reply_usage
        self.received = None

    async def reply(self, msg):
        self.received = msg
        return type("Reply", (), {"usage": self.reply_usage or {}})()


class FakeFactory:
    """记录构造参数的 agent 工厂替身。"""

    def __init__(self, usage: dict | None = None) -> None:
        self.usage = usage
        self.calls: list[dict] = []
        self.agent = FakeAgent(usage)

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.agent


class FakeToolkit:
    """共享 toolkit 替身：记录已注册的中间件。"""

    def __init__(self) -> None:
        self.middlewares: list = []

    def register_middleware(self, mw) -> None:
        self.middlewares.append(mw)


class FakeGate:
    def bind_task(self, task):
        return object()

    def unbind(self, token) -> None:
        pass

    async def middleware(self, kwargs, next_handler):
        async for chunk in await next_handler(**kwargs):
            yield chunk


class FakeMemory:
    """工作记忆替身：get_memory / delete / add 最小语义。"""

    def __init__(self, msgs: list) -> None:
        self.msgs = list(msgs)

    async def get_memory(self, **kwargs):
        return list(self.msgs)

    async def delete(self, msg_ids, **kwargs):
        self.msgs = [m for m in self.msgs if m.id not in msg_ids]
        return len(msg_ids)

    async def add(self, memories, **kwargs):
        if isinstance(memories, list):
            self.msgs.extend(memories)
        else:
            self.msgs.append(memories)

    async def update_msg(self, msg_id, msg, **kwargs):
        self.msgs = [msg if m.id == msg_id else m for m in self.msgs]


def _tool_use(call_id: str, name: str) -> dict:
    return {"type": "tool_use", "id": call_id, "name": name, "input": {}}


def _tool_result(call_id: str, name: str, output) -> dict:
    return {"type": "tool_result", "id": call_id, "name": name, "output": output}


class TestThreadIdNamespace:
    def test_scheduled_task_uses_onetime_thread(self):
        task = _task(TriggerSource.SCHEDULE)
        tid = build_thread_id(task)
        assert tid == f"sched:alpha:probe:{task.id.hex}"

    def test_chat_task_uses_chat_thread(self):
        task = _task(TriggerSource.MENTION, chat_id="c1", topic_id="t1")
        assert build_thread_id(task) == "chat:c1:t1"

    def test_chat_task_without_topic_uses_default(self):
        task = _task(TriggerSource.CARD_ACTION, chat_id="c1")
        assert build_thread_id(task) == "chat:c1:default"


class TestAgentRunner:
    async def test_run_builds_agent_with_thread_and_prompt(self):
        factory = FakeFactory(usage={"total_tokens": 17})
        runner = AgentRunner(agent_factory=factory)
        task = _task(TriggerSource.MENTION, chat_id="c1", topic_id="t1",
                     payload={"input": "查一下冲突"})

        result = await runner.run(task, system_prompt="规则片段")

        assert factory.calls == [
            {"system_prompt": "规则片段", "thread_id": "chat:c1:t1"}
        ]
        assert factory.agent.received is not None
        # token 用量归集进 JobResult.metrics（NFR-004）
        assert result.token_cost == 17

    async def test_quoted_message_content_reaches_the_agent(self):
        """引用体必须拼进 Agent 输入。

        网关在 inbound 提取 quote_content 落进 payload（供事项路由 L1），但
        若不拼进输入，Agent 永远看不到「用户引用了哪条消息」——引用通知卡片
        说"帮我改一下"时，文档链接恰恰只在引用体里（2026-09-28 实测漏检）。
        """
        factory = FakeFactory()
        runner = AgentRunner(agent_factory=factory)
        task = _task(
            TriggerSource.MENTION,
            chat_id="c1",
            topic_id="t1",
            payload={
                "event": {
                    "message": {
                        "text": "@机器人 帮我更新下当前文档，改成黑金风格",
                        "quote_content": "⚠️ 检测到 1 条需求冲突……冲突文档：[查看飞书文档](https://example.feishu.cn/docx/ABC123)",
                    }
                }
            },
        )

        await runner.run(task)

        text = factory.agent.received.get_text_content()
        assert "用户引用的消息" in text
        assert "https://example.feishu.cn/docx/ABC123" in text
        assert "改成黑金风格" in text

    async def test_run_without_usage_defaults_zero(self):
        runner = AgentRunner(agent_factory=FakeFactory())
        task = _task(TriggerSource.SCHEDULE)
        result = await runner.run(task)
        assert result.token_cost == 0

    async def test_resumed_from_approval_rewrites_input_as_replay_directive(self):
        """审批恢复重跑：输入改写为「重放指令 + 用户原始请求」。

        记忆尾部是「已挂起等待审批」的 tool_result，原样重发用户原文会让模型
        误读为仍在等待而只复述状态，真实写调用永不重放（闸门按调用摘要消费
        APPROVED，必须模型重新发起）。
        """
        factory = FakeFactory()
        runner = AgentRunner(agent_factory=factory)
        task = _task(
            TriggerSource.MENTION,
            chat_id="c1",
            topic_id="t1",
            payload={"input": "把背景改为黑金", "resumed_from_approval": True},
        )

        await runner.run(task)

        text = factory.agent.received.get_text_content()
        assert "审批已通过" in text and "重新发起" in text
        assert "把背景改为黑金" in text

    async def test_run_repairs_dangling_tool_use(self):
        """工具突发被审批中断留下悬挂 tool_use：修复补齐 tool_result 并截掉中断点后的消息。

        不修复的话，持久化记忆带着非法序列请求模型网关会被 400 拒绝，线程永久中毒。
        """
        from agentscope.message import Msg

        memory = FakeMemory([
            Msg("user", "原始问题", "user"),
            Msg("assistant", [_tool_use("u1", "updateStory"), _tool_use("u2", "addComment")],
                "assistant"),
            Msg("system", [_tool_result("u1", "updateStory", [])], "system"),
            Msg("user", "原始问题", "user"),  # 上次重试重复入列的输入
        ])
        factory = FakeFactory()
        factory.agent.memory = memory
        runner = AgentRunner(agent_factory=factory)
        task = _task(TriggerSource.MENTION, chat_id="c1", topic_id="t1",
                     payload={"input": "原始问题"})

        await runner.run(task)

        roles = [m.role for m in memory.msgs]
        assert roles == ["user", "assistant", "system", "system"]  # 尾部重复输入被截掉
        patched = memory.msgs[-1].content
        assert patched[0]["type"] == "tool_result" and patched[0]["id"] == "u2"
        assert "中断" in patched[0]["output"]

    async def test_run_dedupes_repeat_user_input_on_retry(self):
        """重试时记忆尾部已是同一条用户输入：先删掉，避免 reply 再追加造成重复。"""
        from agentscope.message import Msg

        memory = FakeMemory([Msg("user", "原始问题", "user")])
        factory = FakeFactory()
        factory.agent.memory = memory
        runner = AgentRunner(agent_factory=factory)
        task = _task(TriggerSource.MENTION, chat_id="c1", topic_id="t1",
                     payload={"input": "原始问题"})

        await runner.run(task)

        # 尾部重复输入已删除（真实 reply 会再追加一条，净效果为不重复）
        assert memory.msgs == []

    async def test_gate_middleware_registered_once_on_shared_toolkit(self):
        """共享 toolkit 上多次运行只注册一次闸门中间件。

        重复注册会把闸门叠成多层：重放已批准调用时外层消费审批、内层读到
        CONSUMED 短路返回「已执行」，真实工具一次都不跑（审计却记 success）。
        """
        toolkit = FakeToolkit()
        factory = FakeFactory()
        factory.agent.toolkit = toolkit
        runner = AgentRunner(agent_factory=factory, approval_gate=FakeGate())

        await runner.run(_task(TriggerSource.MENTION, chat_id="c1", topic_id="t1"))
        await runner.run(_task(TriggerSource.MENTION, chat_id="c1", topic_id="t1"))
        await runner.run(_task(TriggerSource.MENTION, chat_id="c1", topic_id="t1"))

        assert len(toolkit.middlewares) == 1

    async def test_run_patches_empty_tool_result(self):
        """空 output 的 tool_result（中断残留）被原地改写为中断说明，避免 Agent 误读为接口无返回。"""
        from agentscope.message import Msg

        memory = FakeMemory([
            Msg("user", "查需求", "user"),
            Msg("assistant", [_tool_use("u1", "updateStory")], "assistant"),
            Msg("system", [_tool_result("u1", "updateStory", "")], "system"),
        ])
        factory = FakeFactory()
        factory.agent.memory = memory
        runner = AgentRunner(agent_factory=factory)
        task = _task(TriggerSource.MENTION, chat_id="c1", topic_id="t1",
                     payload={"input": "查需求"})

        await runner.run(task)

        patched = memory.msgs[2].content
        assert patched[0]["output"] != ""
        assert "中断" in patched[0]["output"]

    async def test_run_keeps_healthy_memory_untouched(self):
        """健康记忆（tool_use 齐数 tool_result）不做任何修复。"""
        from agentscope.message import Msg

        memory = FakeMemory([
            Msg("user", "问题一", "user"),
            Msg("assistant", [_tool_use("u1", "searchStory")], "assistant"),
            Msg("system", [_tool_result("u1", "searchStory", "ok")], "system"),
            Msg("assistant", "答案", "assistant"),
        ])
        factory = FakeFactory()
        factory.agent.memory = memory
        runner = AgentRunner(agent_factory=factory)
        task = _task(TriggerSource.MENTION, chat_id="c1", topic_id="t1",
                     payload={"input": "新问题"})

        await runner.run(task)

        assert len(memory.msgs) == 4  # 无增删

    async def test_entry_span_noop_without_otel(self, monkeypatch):
        """OpenTelemetry 缺失时 entry_span 降级 no-op，任务正常执行。

        降级路径由 monkeypatch **强制**进入，而不是断言本机恰好没装：
        否则在装了 OTel 的机器上该用例必然失败，失败的是环境而非被测行为。
        """
        from app.platform.observability import entry_span as span_mod

        monkeypatch.setattr(span_mod, "OTEL_AVAILABLE", False)
        assert span_mod.is_available() is False
        runner = AgentRunner(agent_factory=FakeFactory())
        result = await runner.run(_task(TriggerSource.MENTION, chat_id="c1"))
        assert result.token_cost == 0

    async def test_entry_span_opens_real_span_when_otel_present(self, monkeypatch):
        """装了 OpenTelemetry 时开真实 span：session.id = task_id。"""
        from contextlib import contextmanager

        from app.platform.observability import entry_span as span_mod

        opened: list[tuple[str, dict]] = []

        class FakeTracer:
            @contextmanager
            def start_as_current_span(self, name, *, attributes=None):
                opened.append((name, dict(attributes or {})))
                yield object()

        monkeypatch.setattr(span_mod, "OTEL_AVAILABLE", True)
        monkeypatch.setattr(span_mod, "_tracer", lambda: FakeTracer())

        task = _task(TriggerSource.SCHEDULE)
        result = await AgentRunner(agent_factory=FakeFactory()).run(task)

        name = f"wetag:{task.job_name}"
        assert opened == [(name, {"session.id": str(task.id), "wetag.job": name})]
        assert result.token_cost == 0


class TestInitiatorContext:
    """发起人归属：reply 期间绑定进 lark_tools 的 contextvar（供派生链穿透）。"""

    async def test_task_context_is_bound_during_reply(self):
        """工具是进程级单例，靠 contextvar 感知当前任务——绑定必须覆盖 reply 全程。"""
        from app.capability.agents import lark_tools

        seen: list[dict] = []

        class CtxProbeAgent(FakeAgent):
            async def reply(self, msg):
                seen.append(lark_tools._task_context.get() or {})
                return await super().reply(msg)

        factory = FakeFactory()
        factory.agent = CtxProbeAgent()
        runner = AgentRunner(agent_factory=factory)
        task = _task(
            TriggerSource.MENTION,
            chat_id="chat1",
            topic_id="t1",
            payload={"event": {"actor_user_id": "user_asker"}},
        )
        await runner.run(task)

        assert seen == [
            {"actor_user_id": "user_asker", "chat_id": "chat1", "topic_id": "t1"}
        ]
        # reply 结束后必须解绑，否则下一个任务读到上一个任务的发起人
        assert lark_tools._task_context.get() is None

    def test_reply_never_mentions_the_initiator(self):
        """消息文案不出现 <@...>：aibot markdown 不解析提及语法（两套 ID 实测均
        原样渲染），裸 ID 只是噪音。发起人身份走派生链穿透，不进文案。"""
        for chat_id, actor in (("chat1", "user_asker"), ("user_asker", "user_asker")):
            task = _task(
                TriggerSource.MENTION,
                chat_id=chat_id,
                payload={"event": {"actor_user_id": actor}},
            )
            envs = _reply_envelopes(task, "检测完成，无冲突。")
            assert envs[0].payload["text"] == "检测完成，无冲突。"


class TestStripToolNarration:
    """模型工具调用旁白（「💡 使用 JSON 工具: xxx」）不应对用户可见。"""

    def test_narration_line_removed_result_kept(self):
        task = _task(TriggerSource.MENTION, chat_id="c1")
        reply = (
            "已收到 3 个候选需求的精排结果，其中 2 个存在冲突。\n"
            "💡 使用 JSON 工具: TapdBugController_createBug"
        )
        envs = _reply_envelopes(task, reply)
        assert len(envs) == 1
        text = envs[0].payload["text"]
        assert "使用 JSON 工具" not in text
        assert "精排结果" in text

    def test_pure_narration_yields_no_envelope(self):
        task = _task(TriggerSource.MENTION, chat_id="c1")
        assert _reply_envelopes(task, "💡 使用 JSON 工具: xxx") == []

    def test_progress_narration_removed_result_kept(self):
        """「正在搜索相关内容」这类过程旁白不应对用户可见。"""
        task = _task(TriggerSource.MENTION, chat_id="c1")
        reply = "✅ 正在搜索相关内容\n结论：与示例B需求 1020 无冲突。"
        envs = _reply_envelopes(task, reply)
        assert len(envs) == 1
        text = envs[0].payload["text"]
        assert "正在搜索" not in text
        assert "无冲突" in text

    def test_long_progress_sentence_in_body_kept(self):
        """正文里的进行时长句不是旁白，不能误吃。"""
        task = _task(TriggerSource.MENTION, chat_id="c1")
        reply = "正在灰度发布中的需求还有 3 个，预计本周五全量，期间请勿改动相关配置项。"
        envs = _reply_envelopes(task, reply)
        assert len(envs) == 1
        assert "正在灰度发布中" in envs[0].payload["text"]
