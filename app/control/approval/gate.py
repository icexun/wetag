"""执行闸门（plan Decision 2）：唯一的工具调用把关点。

注册进 `Toolkit.register_middleware` 的洋葱中间件：
- READ → 直通（US7-1）
- LOW_WRITE → 直通 + 审计，不发群消息（US7-2）
- HIGH_WRITE → 已批准则原子消费后放行（US7-7）；命中 Auto Mode 直通并标注
  规则版本（US7-4）；否则写 pending_approval、发审批卡片、抛 ApprovalRequired
  让出执行（US7-3）
- FORBIDDEN（删除类）→ 直接拒绝，不进入确认流程，Auto Mode 对它无效（US7-5）

任务上下文经 contextvars 绑定（AgentRunner 在 reply 前 bind_task），
并发任务之间互不串线。
"""
from __future__ import annotations

import contextvars
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from app.contracts.envelope import Envelope, MsgType, Priority
from app.contracts.errors import ApprovalRequired
from app.control.approval.auto_mode import AutoApproveDecision, AutoModeMatcher
from app.control.approval.classifier import RiskLevel, classify
from app.control.approval.pending import (
    APPROVAL_TIMEOUT_MINUTES,
    ApprovalStatus,
    PendingApprovalRepository,
    call_digest,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GateContext:
    """一次任务执行内闸门所需的上下文（中间件 kwargs 里没有，经 bind 注入）。"""

    task_id: UUID
    chat_id: str
    topic_id: str | None = None
    trace_id: str | None = None
    actor: str | None = None


def _text_response(text: str) -> Any:
    """构造一个文本 ToolResponse（惰性 import，避免模块期拉起 agentscope）。"""
    from agentscope.tool import ToolResponse

    return ToolResponse(content=[{"type": "text", "text": text}])


def _suspend_response(approval_id: Any) -> Any:
    """挂起前 yield 的工具结果：给 Agent 留下真实记录。

    不 yield 直接 raise 时，agentscope `_acting` 的 finally 会把一条
    **空 output** 的 tool_result 落进持久记忆——恢复执行后 Agent 误读为
    「接口无返回」而换工具/改入参/放弃，已批准的记录（按调用摘要对账）
    永远匹配不上、不会被消费。
    """
    return _text_response(
        f"本操作已挂起等待人工审批（approval_id={approval_id}），并非接口异常。"
        "审批通过后任务会自动继续，届时请用相同工具、相同入参原样重新发起本调用"
        "即可完成执行；不要更换工具或修改入参（审批按调用摘要对账，变了匹配不上）。"
    )


class ApprovalGate:
    """四级分档执行闸门。repo 之外全部可选注入（替身友好）。"""

    def __init__(
        self,
        repo: PendingApprovalRepository,
        *,
        auto_matcher: AutoModeMatcher | None = None,
        risk_table: dict[str, RiskLevel] | None = None,
        audit: Any = None,
        outbox_submit: Callable[[Envelope], Awaitable[Any]] | None = None,
        timeout_minutes: float = APPROVAL_TIMEOUT_MINUTES,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repo = repo
        self._auto = auto_matcher
        self._risk_table = risk_table
        self._audit = audit
        self._outbox_submit = outbox_submit
        self._timeout = timedelta(minutes=timeout_minutes)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._ctx: contextvars.ContextVar[GateContext | None] = contextvars.ContextVar(
            f"approval_gate_ctx_{id(self)}", default=None
        )

    # ---- 上下文绑定（AgentRunner 接线点） ----

    def bind(self, ctx: GateContext) -> contextvars.Token:
        return self._ctx.set(ctx)

    def bind_task(self, task: Any) -> contextvars.Token:
        """从 Task 构造上下文并绑定（runtime 侧无需 import 本模块的类型）。"""
        return self.bind(
            GateContext(
                task_id=task.id,
                chat_id=task.chat_id or "",
                topic_id=task.topic_id,
                trace_id=task.trace_id,
                actor=(task.payload or {}).get("actor"),
            )
        )

    def unbind(self, token: contextvars.Token) -> None:
        self._ctx.reset(token)

    # ---- agentscope Toolkit 洋葱中间件 ----

    async def middleware(
        self, kwargs: dict, next_handler: Callable[..., Any]
    ) -> AsyncGenerator[Any, None]:
        tool_call = kwargs["tool_call"]
        name = tool_call["name"]
        tool_input = tool_call.get("input") or {}
        level = classify(name, tool_input, table=self._risk_table)
        ctx = self._ctx.get()

        if level is RiskLevel.FORBIDDEN:
            # 删除类：直接拒绝并告知原因，不执行、不进确认流程，Auto Mode 无效（US7-5）
            await self._audit_write(ctx, name, tool_input, outcome="rejected")
            yield _text_response(
                f"操作被拒绝：{name} 属删除类操作，本系统一律不执行删除"
            )
            return

        if level is RiskLevel.READ:
            async for chunk in await next_handler(**kwargs):
                yield chunk
            return

        if level is RiskLevel.LOW_WRITE:
            async for chunk in await next_handler(**kwargs):
                yield chunk
            # 执行成功（无异常）后只写审计，不发群消息（US7-2）
            await self._audit_write(ctx, name, tool_input, outcome="success")
            return

        # HIGH_WRITE：重放消费 → Auto Mode → 挂起让出
        digest = call_digest(name, tool_input)
        existing = (
            await self._repo.find_by_call(ctx.task_id, digest) if ctx is not None else None
        )
        if existing is not None:
            if existing.status == ApprovalStatus.APPROVED:
                consumed = await self._repo.consume(existing.id)
                if consumed is None:
                    # 重复点击/并发重放：只生效一次（US7-7）
                    yield _text_response("该审批已被消费，本次不重复执行")
                    return
                try:
                    async for chunk in await next_handler(**kwargs):
                        yield chunk
                except Exception:
                    # 执行失败：退还消费占位，任务重试时可重新放行（不落入
                    # CONSUMED 永久挂起）；审计按真实结果记录（NFR-008）
                    try:
                        await self._repo.release(existing.id)
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "审批 %s 消费回滚失败，记录保持 CONSUMED", existing.id
                        )
                    await self._audit_write(
                        ctx, name, tool_input,
                        outcome="failure", approval_id=existing.id,
                    )
                    raise
                # 执行成功后写成功审计：消费与外部写同源生效（US7-7）
                await self._audit_write(
                    ctx, name, tool_input, outcome="success", approval_id=existing.id
                )
                return
            if existing.status == ApprovalStatus.CONSUMED:
                # 显式幂等分支：已消费即已执行成功，重放不重复执行也不挂起
                yield _text_response("该审批已消费，操作已执行，本次不重复执行")
                return
            if existing.status == ApprovalStatus.REJECTED:
                # 拒绝结果告知 Agent（US7-3）
                yield _text_response(
                    f"操作已被 {existing.decided_by or '审批人'} 拒绝，本次不执行"
                )
                return
            if existing.status == ApprovalStatus.EXPIRED:
                yield _text_response("该审批请求已过期，请重新发起操作")
                return
            # PENDING：同一调用只挂起一次，直接让出等回调
            yield _suspend_response(existing.id)
            raise ApprovalRequired(existing.id)

        decision = (
            self._auto.match(name, tool_input)
            if self._auto is not None
            else AutoApproveDecision(hit=False)
        )
        if decision.hit:
            # 命中自动批准：直通并标注规则版本（US7-4）
            await self._audit_write(
                ctx, name, tool_input, outcome="success",
                rule_version=decision.rule_version,
            )
            async for chunk in await next_handler(**kwargs):
                yield chunk
            return

        # 未命中：挂起让出（US7-3）——写记录、发卡片、抛 ApprovalRequired
        if ctx is None:
            raise ApprovalRequired(
                UUID(int=0), "高危写缺少任务上下文，无法发起审批"
            )
        approval = await self._repo.create(
            task_id=ctx.task_id,
            chat_id=ctx.chat_id,
            topic_id=ctx.topic_id,
            tool_name=name,
            tool_call_digest=digest,
            tool_call_payload=tool_input,
            expires_at=self._clock() + self._timeout,
        )
        if approval.status == ApprovalStatus.PENDING:
            await self._send_approval_card(ctx, approval, name, tool_input)
        await self._audit_write(
            ctx, name, tool_input, outcome="pending",
            action="approval", approval_id=approval.id,
        )
        yield _suspend_response(approval.id)
        raise ApprovalRequired(approval.id)

    # ---- 内部：审批卡片 / 审计 ----

    async def _send_approval_card(
        self,
        ctx: GateContext,
        approval: Any,
        name: str,
        tool_input: dict[str, Any],
    ) -> None:
        """审批卡片 Envelope：URGENT 跳过合并窗口（plan 时序图）。"""
        if self._outbox_submit is None or not ctx.chat_id:
            return
        submitted = await self._outbox_submit(
            Envelope(
                chat_id=ctx.chat_id,
                topic_id=ctx.topic_id,
                msg_type=MsgType.TEMPLATE_CARD,
                priority=Priority.URGENT,
                payload={
                    "card_type": "approval",
                    "approval_id": str(approval.id),
                    "tool_name": name,
                    "tool_input": tool_input,
                    "expires_at": approval.expires_at.isoformat(),
                },
                task_id=ctx.task_id,
            )
        )
        if submitted is not None:
            await self._repo.set_card_msg_id(approval.id, str(submitted))

    async def _audit_write(
        self,
        ctx: GateContext | None,
        name: str,
        tool_input: dict[str, Any],
        *,
        outcome: str,
        action: str = "tool_call",
        rule_version: str | None = None,
        approval_id: UUID | None = None,
    ) -> None:
        if self._audit is None:
            return
        await self._audit.write(
            action=action,
            outcome=outcome,
            target=name,
            actor=ctx.actor if ctx else None,
            trace_id=ctx.trace_id if ctx else None,
            task_id=ctx.task_id if ctx else None,
            chat_id=ctx.chat_id if ctx else None,
            topic_id=ctx.topic_id if ctx else None,
            params={"tool": name, "input": tool_input},
            rule_version=rule_version,
            approval_id=approval_id,
        )
