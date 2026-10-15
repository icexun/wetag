"""Task → 一次 AgentScope 执行。

thread_id 命名空间隔离（trace_context）：
- 定时任务用一次性 thread（sched:{ws}:{job}:{run_id}，用完即弃）
- 会话任务用 chat:{chat_id}:{topic_id}

agent_factory 可注入（测试用替身）；默认惰性引用 utils.create_agent，
避免 import 期拉起 config / agentscope 重依赖。
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from app.contracts.envelope import Envelope, MsgType
from app.contracts.job_result import JobResult
from app.contracts.task import Task, TriggerSource
from app.platform.observability.entry_span import entry_span
from app.platform.observability.trace_context import chat_thread_id, sched_thread_id

logger = logging.getLogger(__name__)


def build_thread_id(task: Task) -> str:
    """定时与会话隔离的 thread_id 命名空间。"""
    if task.trigger_source == TriggerSource.SCHEDULE:
        return sched_thread_id(task.workspace_id, task.job_name, run_id=task.id.hex)
    return chat_thread_id(task.chat_id or "unknown", task.topic_id)


def create_agent(system_prompt: str | None = None, thread_id: str | None = None) -> Any:
    """默认 agent 工厂：惰性引用 utils.create_agent（Rules 在前、Skill 在后）。"""
    from utils import create_agent as _create_agent

    return _create_agent(system_prompt=system_prompt, thread_id=thread_id)


def extract_user_text(task: Task) -> str:
    """从 queue.py 的落库形态取用户原文：payload.event.message.text。

    群 @ 消息带「@机器人 」前缀，剥掉再喂给 Agent。
    """
    event = (task.payload or {}).get("event") or {}
    message = event.get("message") or {}
    text = message.get("text") or ""
    if " " in text and text.startswith("@"):
        text = text.split(" ", 1)[1]
    return text.strip()


def _build_input(task: Task) -> Any:
    """构造 Agent 输入。优先 agentscope Msg；缺失时退化为纯文本（替身可消费）。

    审批恢复重跑时改写为重放指令：工作记忆尾部是「已挂起等待审批」的
    tool_result，原样重发用户原文会让模型误读为「仍在等待」而只复述状态，
    真实写调用永远不会重放（闸门按调用摘要消费 APPROVED，必须模型重新发起）。
    """
    text = (
        task.payload.get("input")
        or task.payload.get("text")
        or extract_user_text(task)
        or task.job_name
    )
    # 引用内容必须进输入：网关已把 quote_content 落进 payload（供事项路由 L1），
    # 但不拼进这里，Agent 就永远看不到「用户引用了哪条消息」——引用机器人发的
    # 预警卡片问"帮我改一下"时，卡片里的文档链接恰恰只在引用体里。
    quote = (
        ((task.payload or {}).get("event") or {}).get("message") or {}
    ).get("quote_content") or ""
    quote = quote.strip()
    if quote:
        text = f"【用户引用的消息】\n{quote[:500]}\n\n【用户说】\n{text}"
    if (task.payload or {}).get("resumed_from_approval"):
        text = (
            "【审批已通过】你此前被挂起的工具调用已获人工批准。请立即用相同工具、"
            "相同入参原样重新发起该调用完成执行，不要只复述等待状态。\n"
            f"用户原始请求：{text}"
        )
    try:
        from agentscope.message import Msg

        return Msg("user", text, role="user")
    except ImportError:  # pragma: no cover - agentscope 本地已装
        return text


# 模型发起工具调用时常在文本块输出过程旁白（如「💡 使用 JSON 工具: xxx」、
# 「✅ 正在搜索相关内容」），属实现细节，不对用户可见（提示词约束之外的防御性过滤）。
# 「正在…」旁白限 20 字内的短行：正式结论里不会出现孤零零一行的短「正在」句，
# 放宽长度会把正文里的进行时长句误吃掉。
_TOOL_NARRATION = re.compile(
    r"^(?:💡|🛠|⚙️).*?(?:工具|tool).*$"
    r"|.*使用\s*JSON\s*工具.*$"
    r"|^(?:[✅🔍🔎⏳🔄]\s*)?正在.{0,20}$",
    re.IGNORECASE,
)


def _strip_tool_narration(text: str) -> str:
    lines = [ln for ln in text.splitlines() if not _TOOL_NARRATION.match(ln.strip())]
    return "\n".join(lines).strip()


def _reply_envelopes(task: Task, reply: Any) -> list[Envelope]:
    """把 Agent 回复文本包成出站 Envelope——不回群的 Agent 执行等于白跑。

    无群上下文（定时任务）或空回复时不产出：前者没有收件人，后者没有内容。
    文案不带 @发起人：aibot 协议的 markdown 不解析 <@userid>（实测两种 ID
    体系均原样渲染），裸 ID 文本只是噪音。发起人身份仍随派生链穿透，
    用于待办派发，不进消息文案。
    """
    if not task.chat_id:
        return []
    text = ""
    if reply is not None and hasattr(reply, "get_text_content"):
        text = reply.get_text_content() or ""
    elif isinstance(reply, str):
        text = reply
    text = _strip_tool_narration(text)
    if not text.strip():
        return []
    return [
        Envelope(
            chat_id=task.chat_id,
            topic_id=task.topic_id,
            msg_type=MsgType.MARKDOWN,
            task_id=task.id,
            payload={"text": text},
        )
    ]


class AgentRunner:
    """把 Task 转成一次 AgentScope 执行；entry_span 的 session_id 取 task_id。

    注入 RulesMiddleware 时：装配四层规则拼入 system_prompt（Rules 在前、
    Skill 在后），生效规则版本指纹经 JobResult.metrics['rule_version'] 写入
    task 留痕（US6-4，WorkerPool._on_success 落库）。

    注入 ApprovalGate 时：reply 前绑定任务上下文（contextvars），并向 agent
    的 toolkit 注册闸门中间件；ApprovalRequired 异常原样上抛给 WorkerPool
    置 WAIT_CONFIRM（plan Decision 2）。

    注入 MemorySedimenter 时：优先经 agentscope `register_instance_hook`
    注册 post_reply 沉淀钩子；agent 不支持实例钩子时在 reply 后内联调用
    兜底（US8-4）。沉淀失败不拖垮任务（US8-9）。
    """

    def __init__(
        self,
        agent_factory: Callable[..., Any] = create_agent,
        rules: Any = None,
        approval_gate: Any = None,
        memory: Any = None,
    ) -> None:
        self._factory = agent_factory
        self._rules = rules
        self._gate = approval_gate
        self._memory = memory

    async def run(self, task: Task, *, system_prompt: str | None = None) -> JobResult:
        thread_id = build_thread_id(task)
        rule_version: str | None = None
        if self._rules is not None:
            assembled = await self._rules.apply(task)
            rule_version = assembled.rule_version
            if assembled.prompt:
                system_prompt = (
                    f"{assembled.prompt}\n\n{system_prompt}" if system_prompt else assembled.prompt
                )
        gate_token = self._gate.bind_task(task) if self._gate is not None else None
        # 工具是进程级单例，拿不到任务对象；把归属上下文绑进 contextvar，
        # 让 lark 工具派生检测任务时能把「发起人 / 群」传下去。
        from app.capability.agents import lark_tools as _lark_tools
        from app.capability.agents import wecom_tools as _wecom_tools

        actor_user_id = ((task.payload or {}).get("event") or {}).get("actor_user_id")
        ctx_token = _lark_tools.bind_task_context(
            actor_user_id=actor_user_id,
            chat_id=task.chat_id,
            topic_id=task.topic_id,
        )
        todo_ctx_token = _wecom_tools.bind_task_context(
            task_id=str(task.id),
            actor_user_id=actor_user_id,
            chat_id=task.chat_id,
            topic_id=task.topic_id,
        )
        try:
            async with entry_span(str(task.id), name=f"wetag:{task.job_name}"):
                agent = self._factory(system_prompt=system_prompt, thread_id=thread_id)
                self._register_gate(agent)
                hooked = self._register_memory_hook(agent, task)
                input_msg = _build_input(task)
                memory = getattr(agent, "memory", None)
                if memory is not None:
                    await self._repair_memory(memory, task)
                    await self._drop_duplicate_input(memory, input_msg)
                reply = await agent.reply(input_msg)
                if not hooked:
                    await self._sediment(task, reply)
        finally:
            _lark_tools.unbind_task_context(ctx_token)
            _wecom_tools.unbind_task_context(todo_ctx_token)
            if gate_token is not None:
                self._gate.unbind(gate_token)
        token_cost = self._extract_token_cost(reply)
        return JobResult(
            envelopes=_reply_envelopes(task, reply),
            metrics={"token_cost": token_cost, "rule_version": rule_version},
        )

    def _register_gate(self, agent: Any) -> None:
        """向 agent 的 toolkit 注册审批闸门中间件（Toolkit.register_middleware）。

        toolkit 是进程级共享单例（get_shared_toolkit），而本方法每次运行都会
        被调用——不防重的话闸门会逐次叠加成 N 层。多层闸门在重放已批准调用时
        是灾难：最外层消费审批（APPROVED→CONSUMED）后交给内层，内层读到
        CONSUMED 直接短路返回「已执行」，真实工具一次都没跑，审计却记 success。
        闸门按 ContextVar 绑定任务，单例注册一次即可正确工作。
        """
        if self._gate is None:
            return
        toolkit = getattr(agent, "toolkit", None)
        if toolkit is not None and hasattr(toolkit, "register_middleware"):
            if getattr(toolkit, "_wetag_gate_registered", False):
                return
            toolkit.register_middleware(self._gate.middleware)
            toolkit._wetag_gate_registered = True

    def _register_memory_hook(self, agent: Any, task: Task) -> bool:
        """注册 post_reply 记忆沉淀钩子；agent 不支持实例钩子时返回 False，
        由 run() 在 reply 后内联调用兜底（US8-4）。"""
        if self._memory is None:
            return False
        from app.control.memory.remember import register_instance_hook

        return register_instance_hook(agent, self._memory, task)

    async def _sediment(self, task: Task, reply: Any) -> None:
        if self._memory is not None:
            await self._memory.post_reply(task, reply)

    async def _repair_memory(self, memory: Any, task: Task) -> None:
        """修复持久化工作记忆里的工具调用残骸。

        两类污染都来自工具执行被 ApprovalRequired / 异常 / 进程重启打断：
        1. 悬挂 tool_use：assistant 的 tool_use 没有齐数 tool_result，带这种
           序列请求模型网关会被 400 拒绝，持久化让该线程永久中毒、重试必败。
           修复 = 截掉中断点之后的消息 + 补「执行被中断」的 synthetic tool_result。
        2. 空 output 的 tool_result：agentscope `_acting` 的 finally 会把空结果
           落库，Agent 误读为「接口无返回」而换工具/改入参/放弃（审批记录按
           调用摘要对账，入参变了永远匹配不上）。修复 = 原地改写为中断说明。
        """
        try:
            msgs = await memory.get_memory(prepend_summary=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取工作记忆失败，跳过修复 task_id=%s: %s", task.id, exc)
            return
        await self._fix_dangling_tool_use(memory, msgs, task)
        await self._patch_empty_tool_results(memory, msgs, task)

    async def _fix_dangling_tool_use(
        self, memory: Any, msgs: list, task: Task
    ) -> None:
        for i, msg in enumerate(msgs):
            if msg.role != "assistant" or not isinstance(msg.content, list):
                continue
            uses = [b for b in msg.content if b.get("type") == "tool_use"]
            if not uses:
                continue
            covered: set[str] = set()
            j = i + 1
            while j < len(msgs):
                blocks = msgs[j].content if isinstance(msgs[j].content, list) else []
                if not blocks or any(b.get("type") != "tool_result" for b in blocks):
                    break
                covered.update(b.get("id") for b in blocks)
                j += 1
            missing = [b for b in uses if b["id"] not in covered]
            if not missing:
                continue
            if j < len(msgs):
                await memory.delete([m.id for m in msgs[j:]])
            from agentscope.message import Msg

            for b in missing:
                await memory.add(
                    Msg(
                        "system",
                        [
                            {
                                "type": "tool_result",
                                "id": b["id"],
                                "name": b.get("name", ""),
                                "output": "该工具调用被中断（等待审批或进程重启），未执行完成。",
                            }
                        ],
                        "system",
                    )
                )
            logger.warning(
                "修复悬挂工具调用 task_id=%s 补齐=%d 截断=%d",
                task.id,
                len(missing),
                max(0, len(msgs) - j),
            )
            break

    async def _patch_empty_tool_results(
        self, memory: Any, msgs: list, task: Task
    ) -> None:
        """空 output 的 tool_result 只会来自被中断的调用，原地改写为中断说明。"""
        if not hasattr(memory, "update_msg"):
            return
        patched = 0
        for msg in msgs:
            blocks = msg.content if isinstance(msg.content, list) else []
            if not blocks or any(b.get("type") != "tool_result" for b in blocks):
                continue
            empties = [b for b in blocks if not b.get("output")]
            if not empties:
                continue
            for b in empties:
                b["output"] = (
                    "该工具调用曾被中断（等待人工审批，非接口异常），未执行完成；"
                    "若审批已通过，请用相同工具、相同入参原样重新发起。"
                )
            await memory.update_msg(msg.id, msg)
            patched += len(empties)
        if patched:
            logger.warning("修补空工具结果 task_id=%s 条数=%d", task.id, patched)

    async def _drop_duplicate_input(self, memory: Any, input_msg: Any) -> None:
        """重试时 reply 会再追加一次用户输入；记忆尾部已是同一条则先删掉，避免重复。"""
        try:
            msgs = await memory.get_memory(prepend_summary=False)
        except Exception:  # noqa: BLE001
            return
        if not msgs or msgs[-1].role != "user":
            return
        new_text = (
            input_msg.get_text_content()
            if hasattr(input_msg, "get_text_content")
            else str(input_msg)
        )
        if (msgs[-1].get_text_content() or "") == (new_text or ""):
            await memory.delete([msgs[-1].id])

    @staticmethod
    def _extract_token_cost(reply: Any) -> int:
        """从回复对象提取 token 用量；取不到按 0（NFR-004 归集由 task.token_cost 承载）。"""
        usage = getattr(reply, "usage", None) or getattr(reply, "metrics", None)
        if isinstance(usage, dict):
            return int(usage.get("total_tokens") or usage.get("token_cost") or 0)
        return int(getattr(usage, "total_tokens", 0) or 0)
